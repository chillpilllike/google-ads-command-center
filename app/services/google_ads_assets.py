from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AdDraft,
    GoogleAdsAccount,
    GoogleAdsAssetAutomationPreference,
    GoogleAnalyticsDataSnapshot,
    GoogleAdsGeneratedAsset,
    OdooProductPageSignal,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.odoo_sales import _authenticate_store, _many2one_id
from app.services.page_feed_restrictions import get_restricted_title_terms_sync, restricted_title_match


ASSET_TYPE_LABELS = {
    "sitelink": "Sitelink",
    "callout": "Callout",
    "structured_snippet": "Structured snippet",
    "price": "Price",
    "promotion": "Promotion",
    "business_message": "Business message",
    "business_name": "Business name",
    "headline": "Headline text",
    "description": "Description text",
    "pmax_search_theme": "PMax search theme",
}
ASSET_SCOPE_LIMITS = {
    "sitelink": 20,
    "callout": 20,
    "structured_snippet": 10,
    "price": 20,
    "promotion": 20,
    "headline": 15,
    "description": 5,
    "business_message": 1,
    "business_name": 1,
}
PRICE_OFFERING_MIN_ITEMS = 3
PRICE_OFFERING_MAX_ITEMS = 8
MIN_PRICE_ASSET_AMOUNT = 5.0
GOOGLE_LIMIT_NOTES = {
    "sitelink": "Google Help recommends at least 4 and allows up to 20 sitelinks per account, campaign, or ad group.",
    "price": "Price assets require at least 3 offerings and can show up to 8 cards.",
    "business_message": "Google Ads API allows one active business message asset per customer/provider type.",
}
STOP_WORDS = {
    "and",
    "caps",
    "capsules",
    "caplets",
    "extra",
    "for",
    "from",
    "high",
    "mg",
    "ml",
    "new",
    "organic",
    "pack",
    "plus",
    "powder",
    "pure",
    "softgels",
    "strength",
    "supplement",
    "tablet",
    "tablets",
    "the",
    "with",
}
GENERATOR_VERSION = "asset_mapper_v2"
GA4_ITEM_ECOMMERCE_DATASET = "ga4_item_ecommerce"
CATEGORY_SITELINK_RETIRE_BATCH_SIZE = 5
IMMUTABLE_GOOGLE_ASSET_TYPES = {
    "business_message",
    "business_name",
    "callout",
    "price",
    "promotion",
    "sitelink",
    "structured_snippet",
}
INTERNAL_AD_COPY_TERMS = {
    "conversion signal",
    "google conversion signal",
    "odoo sales signal",
    "popular recent seller",
    "strong margin signal",
    "useful product page",
    "real customer demand",
    "demand signal",
    "saved signal",
    "sales signal",
}
PUBLIC_TEXT_FALLBACKS = {
    "sitelink_link_text": "Shop Products",
    "sitelink_description1": "Shop online",
    "sitelink_description2": "Product details",
    "price_description": "Browse products",
    "callout": "Shop Online",
    "structured_snippet": "",
    "headline": "",
    "description": "",
}
PROMOTION_MODEL_CANDIDATES = (
    "loyalty.program",
    "coupon.program",
    "sale.coupon.program",
    "sale.promotion.program",
)


@dataclass
class GeneratedAssetDraft:
    asset_type: str
    name: str
    source_type: str
    source_key: str
    payload_json: dict[str, Any]
    source_json: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    campaign_id: int | None = None
    campaign_name: str = ""


@dataclass
class CategorySitelinkCandidate:
    category_id: int
    name: str
    final_url: str
    parent_id: int = 0
    parent_name: str = ""
    website_id: int = 0
    website_name: str = ""
    store_id: int = 0
    store_name: str = ""
    source_names: list[str] = field(default_factory=list)
    source_category_ids: list[int] = field(default_factory=list)
    order_count: int = 0
    sales_amount: float = 0.0
    add_to_carts: float = 0.0
    items_purchased: float = 0.0
    item_revenue: float = 0.0

    @property
    def score(self) -> float:
        return (
            float(self.item_revenue or 0) * 3
            + float(self.sales_amount or 0) * 2
            + float(self.items_purchased or 0) * 120
            + float(self.order_count or 0) * 35
            + float(self.add_to_carts or 0) * 15
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def text_limit(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= limit:
        return text.rstrip(" &,/-")
    trimmed = text[:limit].rstrip(" &,/-")
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0].rstrip(" &,/-")
    return trimmed[:limit].rstrip(" &,/-") or text[:limit].rstrip(" &,/-")


def _public_ad_text(value: Any, limit: int, *, fallback: str = "", title_case: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or _contains_internal_ad_copy(text):
        text = fallback
    text = _policy_safe_title(text) if title_case else text
    text = text_limit(text, limit)
    if _contains_internal_ad_copy(text):
        text = text_limit(_policy_safe_title(fallback), limit) if fallback else ""
    return text


def source_key(*parts: Any) -> str:
    raw = "|".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def clean_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _score_signal(signal: OdooProductPageSignal) -> float:
    return (
        float(signal.google_conversion_value or 0) * 3
        + float(signal.google_conversions or 0) * 120
        + float(signal.margin_amount or 0) * 2
        + float(signal.sales_amount or 0)
        + float(signal.order_count or 0) * 25
        + float(signal.google_clicks or 0) * 0.25
    )


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _canonical_label(value: Any, limit: int = 25) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\s*/\s*", " ", text)
    text = re.sub(r"\s*>\s*", " ", text)
    if _contains_internal_ad_copy(text):
        return ""
    return text_limit(_policy_safe_title(text), limit)


def _policy_safe_title(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    words: list[str] = []
    for word in text.split(" "):
        if not word:
            continue
        pieces = re.split(r"([-&+/])", word)
        fixed = []
        for piece in pieces:
            if not piece or re.fullmatch(r"[-&+/]", piece):
                fixed.append(piece)
            elif re.search(r"[A-Za-z]", piece):
                fixed.append(piece[:1].upper() + piece[1:].lower())
            else:
                fixed.append(piece)
        words.append("".join(fixed))
    output = " ".join(words)
    output = re.sub(r"\b(Ca|Au|Us|Nz)\$", lambda match: match.group(1).upper() + "$", output)
    return output


def _contains_internal_ad_copy(value: Any) -> bool:
    key = _normal_key(value)
    return any(term in key for term in INTERNAL_AD_COPY_TERMS)


def _public_sitelink_descriptions(signal: OdooProductPageSignal) -> tuple[str, str]:
    categories = [_policy_safe_title(item) for item in _source_categories(signal)]
    categories = [item for item in categories if item]
    brand = _policy_safe_title(_source_brand(signal))
    primary = categories[0] if categories else brand
    if primary:
        desc1 = text_limit(f"Shop {primary}", 35)
    else:
        desc1 = "Shop online"
    desc2_candidates = []
    if brand and brand.lower() not in desc1.lower():
        desc2_candidates.append(f"{brand} range")
    desc2_candidates.extend(["Product details", "Browse online"])
    desc2 = next((text_limit(item, 35) for item in desc2_candidates if item and not _contains_internal_ad_copy(item)), "Product details")
    return desc1, desc2


def _public_price_description(header: Any) -> str:
    label = _policy_safe_title(header)
    for candidate in (f"Browse {label}", f"Shop {label}", f"{label} options"):
        text = text_limit(candidate, 25)
        if 3 <= len(text) <= 25 and not _contains_internal_ad_copy(text):
            return text
    return "Browse products"


def _business_name_for_account(account: GoogleAdsAccount) -> str:
    source = str(account.name or "").strip()
    source = re.sub(r"\s*-\s*(aud|cad|usd|gbp|inr|eur|nzd)\s*(account)?\s*$", "", source, flags=re.I)
    source = re.sub(r"\baccount\b", "", source, flags=re.I)
    source = re.sub(r"\s+", " ", source).strip(" -")
    return text_limit(_policy_safe_title(source or "Business"), 25)


def _source_brand(signal: OdooProductPageSignal) -> str:
    source = signal.source_json or {}
    candidates = [
        source.get("brand"),
        source.get("brand_name"),
        source.get("product_brand"),
        source.get("manufacturer"),
    ]
    for candidate in candidates:
        label = _canonical_label(candidate, 25)
        if label and _normal_key(label) not in {"false", "none", "brand"} and not _contains_internal_ad_copy(label):
            return label
    return ""


def _source_categories(signal: OdooProductPageSignal) -> list[str]:
    source = signal.source_json or {}
    values: list[str] = []
    for raw in (source.get("categories"), source.get("category_names"), source.get("public_categories")):
        if isinstance(raw, list):
            values.extend(str(item or "") for item in raw)
        elif raw:
            values.append(str(raw))
    values.extend(_tokens(signal.product_name)[:2])
    clean = [_canonical_label(value, 25) for value in values]
    return list(dict.fromkeys(value for value in clean if value and _normal_key(value) not in STOP_WORDS))[:6]


def _source_public_category_ids(signal: OdooProductPageSignal) -> list[int]:
    source = signal.source_json or {}
    ids: list[int] = []
    for raw in (source.get("public_category_ids"), source.get("public_categ_ids")):
        if not isinstance(raw, list):
            continue
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ids.append(value)
    return list(dict.fromkeys(ids))


def _many2many_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        if isinstance(item, int):
            ids.append(item)
            continue
        if isinstance(item, (list, tuple)) and item:
            try:
                ids.append(int(item[0]))
            except (TypeError, ValueError):
                continue
    return [value for value in ids if value > 0]


def _absolute_url(domain_url: str, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return clean_url(raw)
    root = str(domain_url or "").strip().rstrip("/")
    if not root:
        return ""
    if not urlparse(root).scheme:
        root = f"https://{root.lstrip('/')}"
    return clean_url(f"{root}/{raw.lstrip('/')}")


def _category_slug(name: Any, category_id: int) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return f"{text or 'category'}-{int(category_id)}"


def _category_frontend_url(domain_url: str, row: dict[str, Any]) -> str:
    for field_name in ("website_url", "url", "website_path"):
        url = _absolute_url(domain_url, row.get(field_name))
        if url:
            return url
    category_id = int(row.get("id") or 0)
    if not category_id:
        return ""
    return _absolute_url(domain_url, f"/shop/category/{_category_slug(row.get('name'), category_id)}")


def _latest_ga4_item_category_intent(session: Session, account_id: int) -> dict[str, dict[str, float]]:
    snapshots = session.scalars(
        select(GoogleAnalyticsDataSnapshot)
        .where(
            GoogleAnalyticsDataSnapshot.account_id == int(account_id),
            GoogleAnalyticsDataSnapshot.dataset_key == GA4_ITEM_ECOMMERCE_DATASET,
        )
        .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
        .limit(5)
    ).all()
    output: dict[str, dict[str, float]] = {}
    for snapshot in snapshots:
        payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            category = _policy_safe_title(row.get("itemCategory") or "")
            key = _normal_key(category)
            if not key or _contains_internal_ad_copy(category):
                continue
            entry = output.setdefault(
                key,
                {
                    "add_to_carts": 0.0,
                    "items_purchased": 0.0,
                    "item_revenue": 0.0,
                },
            )
            entry["add_to_carts"] += float(row.get("itemsAddedToCart") or row.get("addToCarts") or 0)
            entry["items_purchased"] += float(row.get("itemsPurchased") or row.get("ecommercePurchases") or 0)
            entry["item_revenue"] += float(row.get("itemRevenue") or row.get("purchaseRevenue") or 0)
    return output


def _source_price(signal: OdooProductPageSignal) -> float | None:
    source = signal.source_json or {}
    for field in ("list_price", "price", "sale_price", "amount"):
        try:
            value = float(source.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value >= MIN_PRICE_ASSET_AMOUNT:
            return value
    if signal.quantity and signal.sales_amount:
        unit = float(signal.sales_amount or 0) / max(float(signal.quantity or 0), 1.0)
        if unit >= MIN_PRICE_ASSET_AMOUNT:
            return unit
    return None


def _currency_from_domain(domain: str) -> str:
    host = urlparse(str(domain or "")).netloc.lower() or str(domain or "").lower()
    if host.endswith(".ca"):
        return "CAD"
    if host.endswith(".com.au") or host.endswith(".au"):
        return "AUD"
    if host.endswith(".co.uk") or host.endswith(".uk"):
        return "GBP"
    if host.endswith(".co.nz") or host.endswith(".nz"):
        return "NZD"
    if host.endswith(".in"):
        return "INR"
    if host.endswith(".ie"):
        return "EUR"
    return ""


def _signal_currency(signal: OdooProductPageSignal, account: GoogleAdsAccount | None = None) -> str:
    source = signal.source_json or {}
    for value in (
        source.get("website_currency_code"),
        source.get("pricelist_currency_code"),
        signal.currency_code,
        _currency_from_domain(signal.domain or signal.product_url),
        account.currency_code if account else "",
    ):
        code = str(value or "").strip().upper()
        if code:
            return code
    return "USD"


def _scope_payload(level: str, *, key: str = "", name: str = "", campaign_name: str = "", ad_group_name: str = "") -> dict[str, str]:
    return {
        "level": level,
        "key": key,
        "name": name,
        "campaign_name": campaign_name,
        "ad_group_name": ad_group_name,
    }


def _mapping_payload(
    *,
    scope: dict[str, str],
    dynamic_source: str,
    source_key_value: str,
    replace_on_change: bool = True,
    delete_when_source_missing: bool = True,
) -> dict[str, Any]:
    return {
        "generator": GENERATOR_VERSION,
        "scope": scope,
        "dynamic_source": dynamic_source,
        "source_key": source_key_value,
        "replace_on_change": bool(replace_on_change),
        "delete_when_source_missing": bool(delete_when_source_missing),
    }


def _clean_name(value: Any) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", str(value or ""))
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|kg|ml|oz|caps?|capsules?|tabs?|tablets?|servings?)\b", " ", text, flags=re.I)
    text = re.sub(r"[^A-Za-z0-9&+ -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -")


def _tokens(value: Any) -> list[str]:
    output = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9&+.-]{2,}", _clean_name(value)):
        normalized = token.strip("-.").lower()
        if normalized in STOP_WORDS or normalized.isdigit():
            continue
        output.append(token.strip("-."))
    return output


def sitelink_payload_for_signal(signal: OdooProductPageSignal) -> dict[str, Any] | None:
    url = clean_url(signal.product_url)
    if not url:
        return None
    title_source = _policy_safe_title(_clean_name(signal.product_name) or signal.product_code or "Shop products")
    link_text = _public_ad_text(title_source, 25, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_link_text"])
    if len(link_text) < 1:
        return None
    description1, description2 = _public_sitelink_descriptions(signal)
    return {
        "link_text": link_text,
        "final_url": url,
        "description1": _public_ad_text(description1, 35, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_description1"]),
        "description2": _public_ad_text(description2, 35, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_description2"]),
    }


def _sitelink_drafts(
    signals: list[OdooProductPageSignal],
    *,
    limit: int = ASSET_SCOPE_LIMITS["sitelink"],
    scope: dict[str, str] | None = None,
    source_prefix: str = "sitelink",
) -> list[GeneratedAssetDraft]:
    drafts: list[GeneratedAssetDraft] = []
    seen_urls: set[str] = set()
    seen_texts: set[str] = set()
    scope = scope or _scope_payload("account", key="account", name="Account")
    for signal in signals:
        payload = sitelink_payload_for_signal(signal)
        if not payload:
            continue
        url = payload["final_url"]
        text_key = _normal_key(payload.get("link_text"))
        if url in seen_urls or text_key in seen_texts:
            continue
        seen_urls.add(url)
        seen_texts.add(text_key)
        skey = source_key(source_prefix, scope.get("level"), scope.get("key"), url)
        payload = {
            **payload,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source="odoo_product_page_signal",
                source_key_value=skey,
            ),
            "payload_hash": payload_hash(payload),
            "google_limit_note": GOOGLE_LIMIT_NOTES["sitelink"],
        }
        drafts.append(
            GeneratedAssetDraft(
                asset_type="sitelink",
                name=payload["link_text"],
                source_type="odoo_product_page_signal",
                source_key=skey,
                payload_json=payload,
                source_json={
                    "signal_id": signal.id,
                    "product_code": signal.product_code,
                    "product_name": signal.product_name,
                    "label": signal.label,
                    "website_name": signal.website_name,
                    "order_count": signal.order_count,
                    "margin_amount": signal.margin_amount,
                    "margin_percent": signal.margin_percent,
                    "google_conversions": signal.google_conversions,
                    "reason": signal.reason,
                    "scope": scope,
                },
                campaign_name=scope.get("campaign_name", ""),
            )
        )
        if len(drafts) >= limit:
            break
    return drafts


def _read_odoo_public_category_candidates(
    *,
    session: Session,
    account: GoogleAdsAccount,
    signals: list[OdooProductPageSignal],
) -> tuple[list[CategorySitelinkCandidate], list[str]]:
    target_ids: set[int] = set()
    target_names: set[str] = set()
    signal_metrics_by_name: dict[str, dict[str, Any]] = {}
    signal_metrics_by_id: dict[int, dict[str, Any]] = {}
    for signal in signals:
        if signal.label not in {"winner", "watch", "fallback"}:
            continue
        category_names = _source_categories(signal)
        public_category_ids = _source_public_category_ids(signal)
        for category_id in public_category_ids:
            target_ids.add(category_id)
            entry = signal_metrics_by_id.setdefault(
                category_id,
                {"order_count": 0, "sales_amount": 0.0, "source_names": [], "source_category_ids": []},
            )
            entry["order_count"] += int(signal.order_count or 0)
            entry["sales_amount"] += float(signal.sales_amount or 0)
            if category_id not in entry["source_category_ids"]:
                entry["source_category_ids"].append(category_id)
        for category in category_names:
            key = _normal_key(category)
            if not key:
                continue
            target_names.add(key)
            entry = signal_metrics_by_name.setdefault(
                key,
                {"order_count": 0, "sales_amount": 0.0, "source_names": [], "source_category_ids": []},
            )
            entry["order_count"] += int(signal.order_count or 0)
            entry["sales_amount"] += float(signal.sales_amount or 0)
            if category not in entry["source_names"]:
                entry["source_names"].append(category)
            for category_id in public_category_ids:
                if category_id not in entry["source_category_ids"]:
                    entry["source_category_ids"].append(category_id)

    ga4_intent = _latest_ga4_item_category_intent(session, account.id)
    target_names.update(ga4_intent.keys())
    notes: list[str] = []
    if not target_ids and not target_names:
        return [], ["No sold or add-to-cart public category signals were available for sitelink generation."]

    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping)
        .where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
        .order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)
    ).all()
    candidates: dict[str, CategorySitelinkCandidate] = {}
    for mapping in mappings:
        store = mapping.store
        website = None
        if mapping.website_id:
            website = session.scalar(
                select(OdooWebsite).where(
                    OdooWebsite.store_id == store.id,
                    OdooWebsite.website_id == mapping.website_id,
                    OdooWebsite.is_active.is_(True),
                )
            )
        domain_url = str((website.domain if website else "") or store.base_url or "").strip()
        try:
            _base_url, uid, models = _authenticate_store(store)
        except Exception as exc:  # noqa: BLE001 - other mapped stores can still contribute category sitelinks.
            notes.append(f"{store.name}: Odoo category lookup failed: {str(exc)[:180]}")
            continue
        meta = _safe_fields_get(models, store, uid, "product.public.category")
        if not meta:
            notes.append(f"{store.name}: product.public.category is unavailable.")
            continue
        fields = [
            field_name
            for field_name in (
                "id",
                "name",
                "parent_id",
                "child_id",
                "website_id",
                "website_url",
                "url",
                "website_path",
                "active",
            )
            if field_name in meta
        ]
        if "id" not in fields:
            fields.insert(0, "id")
        if "name" not in fields:
            fields.append("name")
        try:
            rows = models.execute_kw(
                store.database,
                uid,
                store.api_key,
                "product.public.category",
                "search_read",
                [[]],
                {"fields": fields, "limit": 3000},
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{store.name}: category read failed: {str(exc)[:180]}")
            continue
        rows_by_id = {int(row.get("id") or 0): row for row in rows if row.get("id")}
        included_ids: set[int] = set()
        for row in rows:
            category_id = int(row.get("id") or 0)
            if not category_id:
                continue
            if "active" in row and row.get("active") is False:
                continue
            row_website_id = _many2one_id(row.get("website_id"))
            if row_website_id and mapping.website_id and row_website_id != int(mapping.website_id):
                continue
            name = _policy_safe_title(row.get("name") or "")
            name_key = _normal_key(name)
            if category_id in target_ids or name_key in target_names:
                included_ids.add(category_id)
                parent_id = _many2one_id(row.get("parent_id"))
                while parent_id:
                    included_ids.add(parent_id)
                    parent_row = rows_by_id.get(parent_id) or {}
                    parent_id = _many2one_id(parent_row.get("parent_id"))
        for category_id in sorted(included_ids):
            row = rows_by_id.get(category_id) or {}
            if not row:
                continue
            name = _policy_safe_title(row.get("name") or "")
            if not name or _contains_internal_ad_copy(name):
                continue
            final_url = _category_frontend_url(domain_url, row)
            if not final_url:
                continue
            name_key = _normal_key(name)
            metrics_by_name = signal_metrics_by_name.get(name_key, {})
            metrics_by_id = signal_metrics_by_id.get(category_id, {})
            ga4 = ga4_intent.get(name_key, {})
            parent = row.get("parent_id")
            parent_id = _many2one_id(parent)
            parent_name = _policy_safe_title(parent[1]) if isinstance(parent, (list, tuple)) and len(parent) > 1 else ""
            key = clean_url(final_url)
            existing = candidates.get(key)
            candidate = existing or CategorySitelinkCandidate(
                category_id=category_id,
                name=name,
                final_url=key,
                parent_id=parent_id or 0,
                parent_name=parent_name,
                website_id=int(mapping.website_id or 0),
                website_name=website.name if website else "All websites",
                store_id=store.id,
                store_name=store.name,
            )
            candidate.order_count += int(metrics_by_name.get("order_count") or 0) + int(metrics_by_id.get("order_count") or 0)
            candidate.sales_amount += float(metrics_by_name.get("sales_amount") or 0) + float(metrics_by_id.get("sales_amount") or 0)
            candidate.add_to_carts += float(ga4.get("add_to_carts") or 0)
            candidate.items_purchased += float(ga4.get("items_purchased") or 0)
            candidate.item_revenue += float(ga4.get("item_revenue") or 0)
            source_names = list(metrics_by_name.get("source_names") or [])
            if name not in source_names:
                source_names.append(name)
            candidate.source_names = list(dict.fromkeys([*candidate.source_names, *source_names]))
            candidate.source_category_ids = list(
                dict.fromkeys(
                    [
                        *candidate.source_category_ids,
                        category_id,
                        *list(metrics_by_name.get("source_category_ids") or []),
                        *list(metrics_by_id.get("source_category_ids") or []),
                    ]
                )
            )
            candidates[key] = candidate
    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            -item.score,
            0 if item.parent_id else 1,
            item.name,
        ),
    )
    return ordered, notes


def _category_sitelink_payload(candidate: CategorySitelinkCandidate) -> dict[str, Any] | None:
    final_url = clean_url(candidate.final_url)
    if not final_url:
        return None
    link_text = _public_ad_text(candidate.name, 25, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_link_text"])
    if not link_text:
        return None
    if candidate.parent_name and _normal_key(candidate.parent_name) != _normal_key(candidate.name):
        desc1 = f"Shop {candidate.parent_name}"
    else:
        desc1 = f"Shop {candidate.name}"
    if candidate.add_to_carts > 0 and candidate.items_purchased <= 0:
        desc2 = "Popular in carts"
    elif candidate.order_count > 0 or candidate.items_purchased > 0:
        desc2 = "Customer favorites"
    else:
        desc2 = "Browse products"
    return {
        "link_text": link_text,
        "final_url": final_url,
        "description1": _public_ad_text(desc1, 35, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_description1"]),
        "description2": _public_ad_text(desc2, 35, fallback=PUBLIC_TEXT_FALLBACKS["sitelink_description2"]),
    }


def _category_sitelink_drafts(
    candidates: list[CategorySitelinkCandidate],
    *,
    limit: int = ASSET_SCOPE_LIMITS["sitelink"],
    scope: dict[str, str] | None = None,
    source_prefix: str = "category_sitelink",
) -> list[GeneratedAssetDraft]:
    drafts: list[GeneratedAssetDraft] = []
    seen_urls: set[str] = set()
    seen_texts: set[str] = set()
    scope = scope or _scope_payload("account", key="account", name="Account")
    for candidate in candidates:
        payload = _category_sitelink_payload(candidate)
        if not payload:
            continue
        url = payload["final_url"]
        text_key = _normal_key(payload.get("link_text"))
        if url in seen_urls or text_key in seen_texts:
            continue
        seen_urls.add(url)
        seen_texts.add(text_key)
        skey = source_key(source_prefix, scope.get("level"), scope.get("key"), candidate.store_id, candidate.website_id, candidate.category_id, url)
        payload = {
            **payload,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source="odoo_public_category",
                source_key_value=skey,
            ),
            "payload_hash": payload_hash(payload),
            "google_limit_note": GOOGLE_LIMIT_NOTES["sitelink"],
        }
        drafts.append(
            GeneratedAssetDraft(
                asset_type="sitelink",
                name=payload["link_text"],
                source_type="odoo_public_category",
                source_key=skey,
                payload_json=payload,
                source_json={
                    "category_id": candidate.category_id,
                    "category_name": candidate.name,
                    "parent_id": candidate.parent_id,
                    "parent_name": candidate.parent_name,
                    "website_id": candidate.website_id,
                    "website_name": candidate.website_name,
                    "store_id": candidate.store_id,
                    "store_name": candidate.store_name,
                    "source_names": candidate.source_names,
                    "source_category_ids": candidate.source_category_ids,
                    "order_count": candidate.order_count,
                    "sales_amount": candidate.sales_amount,
                    "add_to_carts": candidate.add_to_carts,
                    "items_purchased": candidate.items_purchased,
                    "item_revenue": candidate.item_revenue,
                    "scope": scope,
                },
                campaign_name=scope.get("campaign_name", ""),
            )
        )
        if len(drafts) >= limit:
            break
    return drafts


def _account_category_sitelink_drafts(
    session: Session,
    account: GoogleAdsAccount,
    signals: list[OdooProductPageSignal],
) -> tuple[list[GeneratedAssetDraft], list[str]]:
    candidates, notes = _read_odoo_public_category_candidates(session=session, account=account, signals=signals)
    drafts = _category_sitelink_drafts(
        candidates,
        scope=_scope_payload("account", key="account", name="Account"),
    )
    if not drafts and not notes:
        notes.append("No eligible Odoo category URLs qualified for account-level sitelinks.")
    return drafts, notes


def _brand_or_type_values(signals: list[OdooProductPageSignal]) -> tuple[str, list[str]]:
    brand_scores: dict[str, float] = {}
    type_scores: dict[str, float] = {}
    for signal in signals:
        brand = _source_brand(signal)
        if brand:
            existing = next((item for item in brand_scores if _normal_key(item) == _normal_key(brand)), brand)
            brand_scores[existing] = brand_scores.get(existing, 0.0) + max(_score_signal(signal), 1.0)
        tokens = _tokens(signal.product_name)
        if tokens:
            phrase = _policy_safe_title(text_limit(" ".join(tokens[:2]), 25))
            if phrase:
                type_scores[phrase] = type_scores.get(phrase, 0.0) + max(_score_signal(signal), 1.0)
    if len(brand_scores) >= 3:
        values = [item[0] for item in sorted(brand_scores.items(), key=lambda item: item[1], reverse=True)]
        return "Brands", values[:10]
    values = [item[0] for item in sorted(type_scores.items(), key=lambda item: item[1], reverse=True)]
    return "Types", list(dict.fromkeys(values))[:10]


def _structured_values_by_header(signals: list[OdooProductPageSignal]) -> dict[str, list[str]]:
    brand_header, brand_or_type_values = _brand_or_type_values(signals)
    type_scores: dict[str, float] = {}
    for signal in signals:
        score = max(_score_signal(signal), 1.0)
        for category in _source_categories(signal):
            value = _canonical_label(category, 25)
            if value:
                existing = next((item for item in type_scores if _normal_key(item) == _normal_key(value)), value)
                type_scores[existing] = type_scores.get(existing, 0.0) + score
        tokens = _tokens(signal.product_name)
        if tokens:
            value = _policy_safe_title(text_limit(" ".join(tokens[:2]), 25))
            if value:
                type_scores[value] = type_scores.get(value, 0.0) + score
    values: dict[str, list[str]] = {}
    if brand_header == "Brands":
        values["Brands"] = brand_or_type_values
    values["Types"] = [item[0] for item in sorted(type_scores.items(), key=lambda item: item[1], reverse=True)]
    if brand_header != "Brands":
        values["Types"] = list(dict.fromkeys(brand_or_type_values + values["Types"]))
    return values


def _valid_structured_snippet_value(value: str) -> bool:
    text = _policy_safe_title(value)
    key = _normal_key(text)
    if len(text) < 3 or len(text) > 25:
        return False
    if key in {"all", "com", "www", "none", "misc", "other", "category", "categories", "false", "true"}:
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if _contains_internal_ad_copy(text):
        return False
    return True


def _structured_snippet_header(header: str) -> str:
    if header in {"Brands", "Types"}:
        return header
    if header in {"Categories", "Category", "Product Categories", "Products"}:
        return "Types"
    return "Types"


def _structured_snippet_drafts(
    signals: list[OdooProductPageSignal],
    *,
    scope: dict[str, str] | None = None,
    source_prefix: str = "structured_snippet",
) -> list[GeneratedAssetDraft]:
    scope = scope or _scope_payload("account", key="account", name="Account")
    drafts: list[GeneratedAssetDraft] = []
    for header, raw_values in _structured_values_by_header(signals).items():
        snippet_header = _structured_snippet_header(header)
        values = [_policy_safe_title(value) for value in list(dict.fromkeys(raw_values)) if _valid_structured_snippet_value(value)][:10]
        if len(values) < 3:
            continue
        skey = source_key(source_prefix, scope.get("level"), scope.get("key"), snippet_header, ",".join(values))
        payload = {
            "header": snippet_header,
            "values": values,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source="odoo_product_page_signals",
                source_key_value=skey,
            ),
        }
        payload["payload_hash"] = payload_hash(payload)
        drafts.append(
            GeneratedAssetDraft(
                asset_type="structured_snippet",
                name=f"{snippet_header}: {', '.join(values[:3])}",
                source_type="odoo_product_page_signals",
                source_key=skey,
                payload_json=payload,
                source_json={
                    "source_signal_ids": [signal.id for signal in signals[:50]],
                    "basis": "top Odoo page-feed products by label, margin, conversion value, brand, and category",
                    "scope": scope,
                },
                campaign_name=scope.get("campaign_name", ""),
            )
        )
        if len(drafts) >= ASSET_SCOPE_LIMITS["structured_snippet"]:
            break
    return drafts


def _term_score(term: str, signal: OdooProductPageSignal) -> float:
    return (
        float(signal.google_conversion_value or 0)
        + float(signal.sales_amount or 0)
        + float(signal.margin_amount or 0) * 3
        + float(signal.order_count or 0) * 10
    )


def _pmax_search_theme_drafts(signals: list[OdooProductPageSignal], *, limit: int = 25) -> list[GeneratedAssetDraft]:
    scores: dict[str, dict[str, Any]] = {}
    for signal in signals:
        source = signal.source_json or {}
        terms = [str(item or "").strip() for item in source.get("exact_terms") or []]
        if not terms:
            clean_name = _clean_name(signal.product_name)
            terms = [clean_name] if clean_name else []
        for raw_term in terms:
            term = text_limit(raw_term, 80)
            if len(term) < 3:
                continue
            key = term.lower()
            entry = scores.setdefault(
                key,
                {
                    "term": term,
                    "score": 0.0,
                    "signal_ids": [],
                    "product_urls": [],
                },
            )
            entry["score"] += _term_score(term, signal)
            entry["signal_ids"].append(signal.id)
            if signal.product_url:
                entry["product_urls"].append(clean_url(signal.product_url))
    drafts: list[GeneratedAssetDraft] = []
    for entry in sorted(scores.values(), key=lambda item: item["score"], reverse=True)[:limit]:
        term = entry["term"]
        payload = {
            "search_theme": term,
            "target": "performance_max",
            "note": "Use as PMax search theme, not as a keyword.",
        }
        drafts.append(
            GeneratedAssetDraft(
                asset_type="pmax_search_theme",
                name=term,
                source_type="odoo_product_page_signals",
                source_key=source_key("pmax_search_theme", term),
                payload_json=payload,
                source_json={
                    "source_signal_ids": list(dict.fromkeys(entry["signal_ids"]))[:20],
                    "product_urls": list(dict.fromkeys(entry["product_urls"]))[:20],
                    "score": round(float(entry["score"] or 0), 4),
                    "basis": "Odoo winners/watch products and cached converting terms",
                },
            )
        )
    return drafts


def _callout_texts(signals: list[OdooProductPageSignal], offer_callouts: list[str] | None = None) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for text in offer_callouts or []:
        value = text_limit(text, 25)
        if value:
            entries.append((value, "odoo_cart_offer"))
    brand_values = [_source_brand(signal) for signal in signals if _source_brand(signal)]
    for brand in list(dict.fromkeys(brand_values))[:8]:
        text = text_limit(f"{brand} Range", 25)
        if len(text) >= 6:
            entries.append((text, "odoo_brand_attributes"))
    category_values = [category for signal in signals for category in _source_categories(signal)]
    for category in list(dict.fromkeys(category_values))[:8]:
        text = text_limit(f"{category} Options", 25)
        if len(text) >= 6:
            entries.append((text, "odoo_categories"))
    entries.extend(
        [
            ("Secure Checkout", "site_baseline"),
            ("Clear Product Details", "site_baseline"),
            ("Online Ordering", "site_baseline"),
            ("Browse Health Products", "site_baseline"),
        ]
    )
    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for value, source in entries:
        value = _public_ad_text(value, 25, fallback=PUBLIC_TEXT_FALLBACKS["callout"])
        key = _normal_key(value)
        if key and key not in seen and 2 <= len(value) <= 25:
            seen.add(key)
            output.append((value, source))
        if len(output) >= ASSET_SCOPE_LIMITS["callout"]:
            break
    return output


def _callout_drafts(
    signals: list[OdooProductPageSignal],
    *,
    offer_callouts: list[str] | None = None,
    scope: dict[str, str] | None = None,
    source_prefix: str = "callout",
) -> list[GeneratedAssetDraft]:
    scope = scope or _scope_payload("account", key="account", name="Account")
    drafts: list[GeneratedAssetDraft] = []
    for value, basis in _callout_texts(signals, offer_callouts):
        skey = source_key(source_prefix, scope.get("level"), scope.get("key"), value)
        payload = {
            "callout_text": value,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source=basis,
                source_key_value=skey,
            ),
        }
        payload["payload_hash"] = payload_hash(payload)
        drafts.append(
            GeneratedAssetDraft(
                asset_type="callout",
                name=value,
                source_type=basis,
                source_key=skey,
                payload_json=payload,
                source_json={
                    "scope": scope,
                    "source_signal_ids": [signal.id for signal in signals[:50]],
                    "basis": basis,
                },
                campaign_name=scope.get("campaign_name", ""),
            )
        )
    return drafts


def _price_basis_rows(signals: list[OdooProductPageSignal], *, account: GoogleAdsAccount | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for signal in signals:
        price = _source_price(signal)
        if price is None:
            continue
        labels = _source_categories(signal)
        brand = _source_brand(signal)
        if brand:
            labels.insert(0, brand)
        if not labels:
            labels = [_clean_name(signal.product_name)]
        for label in labels[:3]:
            header = _canonical_label(label, 25)
            if not header:
                continue
            key = _normal_key(header)
            entry = grouped.setdefault(
                key,
                {
                    "header": header,
                    "price": price,
                    "currency_code": _signal_currency(signal, account),
                    "final_url": clean_url(signal.product_url),
                    "description": _public_price_description(header),
                    "score": 0.0,
                    "signal_ids": [],
                },
            )
            if price < float(entry["price"] or price):
                entry["price"] = price
                entry["final_url"] = clean_url(signal.product_url)
            entry["score"] += max(_score_signal(signal), 1.0)
            entry["signal_ids"].append(signal.id)
            entry["description"] = _public_ad_text(
                _public_price_description(entry["header"]),
                25,
                fallback=PUBLIC_TEXT_FALLBACKS["price_description"],
            )
    rows = sorted(grouped.values(), key=lambda item: (-float(item["score"] or 0), float(item["price"] or 0)))
    output: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in rows:
        url = str(row.get("final_url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        row["description"] = _public_ad_text(
            row.get("description") or _public_price_description(row.get("header")),
            25,
            fallback=PUBLIC_TEXT_FALLBACKS["price_description"],
        )
        output.append(row)
    return output


def _price_asset_drafts(
    signals: list[OdooProductPageSignal],
    *,
    account: GoogleAdsAccount | None = None,
    scope: dict[str, str] | None = None,
    source_prefix: str = "price",
) -> list[GeneratedAssetDraft]:
    scope = scope or _scope_payload("account", key="account", name="Account")
    rows = _price_basis_rows(signals, account=account)
    if len(rows) < PRICE_OFFERING_MIN_ITEMS:
        return []
    drafts: list[GeneratedAssetDraft] = []
    grouped_by_currency: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped_by_currency.setdefault(str(row.get("currency_code") or "USD").upper(), []).append(row)
    for currency_code, currency_rows in sorted(grouped_by_currency.items()):
        if len(currency_rows) < PRICE_OFFERING_MIN_ITEMS:
            continue
        for index in range(0, min(len(currency_rows), ASSET_SCOPE_LIMITS["price"] * PRICE_OFFERING_MAX_ITEMS), PRICE_OFFERING_MAX_ITEMS):
            offerings = currency_rows[index : index + PRICE_OFFERING_MAX_ITEMS]
            if len(offerings) < PRICE_OFFERING_MIN_ITEMS:
                break
            skey = source_key(source_prefix, scope.get("level"), scope.get("key"), currency_code, index, ",".join(row["header"] for row in offerings))
            payload = {
                "type": "PRODUCT_CATEGORIES",
                "price_qualifier": "FROM",
                "language_code": "en",
                "currency_code": currency_code,
                "price_offerings": [
                    {
                        "header": row["header"],
                        "description": row["description"],
                        "price": round(float(row["price"] or 0), 2),
                        "currency_code": currency_code,
                        "final_url": row["final_url"],
                        "source_signal_ids": list(dict.fromkeys(row["signal_ids"]))[:20],
                    }
                    for row in offerings
                ],
                "scope": scope,
                "mapping": _mapping_payload(
                    scope=scope,
                    dynamic_source="odoo_product_price_category_signal",
                    source_key_value=skey,
                ),
                "google_limit_note": GOOGLE_LIMIT_NOTES["price"],
            }
            payload["payload_hash"] = payload_hash(payload)
            drafts.append(
                GeneratedAssetDraft(
                    asset_type="price",
                    name=f"{scope.get('name') or 'Account'} {currency_code} price set {len(drafts) + 1}",
                    source_type="odoo_product_page_signals",
                    source_key=skey,
                    payload_json=payload,
                    source_json={
                        "scope": scope,
                        "basis": "starting category/brand price from Odoo website/pricelist currency signals; prices below threshold ignored",
                        "minimum_price_amount": MIN_PRICE_ASSET_AMOUNT,
                        "currency_code": currency_code,
                        "source_signal_ids": [sid for row in offerings for sid in list(dict.fromkeys(row["signal_ids"]))[:5]],
                    },
                    campaign_name=scope.get("campaign_name", ""),
                )
            )
    return drafts


def _business_message_draft(
    account: GoogleAdsAccount,
    preference: GoogleAdsAssetAutomationPreference,
) -> list[GeneratedAssetDraft]:
    phone = re.sub(r"[^\d+]+", "", preference.whatsapp_phone_number or "")
    if not phone:
        return []
    scope = _scope_payload("account", key="account", name="Account")
    skey = source_key("business_message", account.id, "whatsapp")
    payload = {
        "provider": "WHATSAPP",
        "country_code": preference.whatsapp_country_code or "+1",
        "phone_number": phone,
        "starter_message": text_limit(preference.whatsapp_starter_message, 140),
        "call_to_action": preference.whatsapp_call_to_action or "MESSAGE",
        "call_to_action_description": text_limit(preference.whatsapp_call_to_action_description, 30),
        "scope": scope,
        "mapping": _mapping_payload(
            scope=scope,
            dynamic_source="asset_preference_whatsapp",
            source_key_value=skey,
            delete_when_source_missing=True,
        ),
        "google_limit_note": GOOGLE_LIMIT_NOTES["business_message"],
    }
    payload["payload_hash"] = payload_hash(payload)
    return [
        GeneratedAssetDraft(
            asset_type="business_message",
            name="WhatsApp message asset",
            source_type="asset_preference_whatsapp",
            source_key=skey,
            payload_json=payload,
            source_json={"scope": scope, "basis": "per-account WhatsApp message asset preference"},
        )
    ]


def _business_name_drafts(
    account: GoogleAdsAccount,
    *,
    campaign_scopes: list[dict[str, str]] | None = None,
) -> list[GeneratedAssetDraft]:
    business_name = _business_name_for_account(account)
    if not business_name:
        return []
    scopes = [_scope_payload("account", key="account", name="Account")]
    for scope in campaign_scopes or []:
        if scope.get("level") == "campaign":
            scopes.append(scope)
    drafts: list[GeneratedAssetDraft] = []
    seen_scope_keys: set[str] = set()
    for scope in scopes:
        scope_key = f"{scope.get('level')}:{scope.get('key')}:{scope.get('campaign_name')}"
        if scope_key in seen_scope_keys:
            continue
        seen_scope_keys.add(scope_key)
        skey = source_key("business_name", account.id, scope.get("level"), scope.get("key"), business_name)
        payload = {
            "business_name": business_name,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source="google_ads_account_identity",
                source_key_value=skey,
                replace_on_change=True,
                delete_when_source_missing=False,
            ),
        }
        payload["payload_hash"] = payload_hash(payload)
        drafts.append(
            GeneratedAssetDraft(
                asset_type="business_name",
                name=business_name,
                source_type="google_ads_account_identity",
                source_key=skey,
                payload_json=payload,
                source_json={
                    "scope": scope,
                    "basis": "sanitized Google Ads account identity used for Search/PMax business-name asset",
                },
                campaign_name=scope.get("campaign_name", ""),
            )
        )
    return drafts


def _automation_asset_scope_drafts(
    signals: list[OdooProductPageSignal],
    *,
    session: Session,
    account: GoogleAdsAccount,
    include_callouts: bool,
    include_structured_snippets: bool,
    include_sitelinks: bool,
    include_prices: bool,
    include_campaign_mapping: bool,
    include_ad_group_mapping: bool,
    restricted_terms: list[str] | None = None,
) -> list[GeneratedAssetDraft]:
    if not include_campaign_mapping and not include_ad_group_mapping:
        return []
    drafts: list[GeneratedAssetDraft] = []
    campaign_scopes: list[dict[str, str]] = []
    rows = session.scalars(
        select(AdDraft)
        .where(AdDraft.account_id == account.id, AdDraft.generated_assets != {})
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        .limit(120)
    ).all()
    for draft in rows:
        assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
        automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
        source = str(automation.get("source_key") or "")
        if not source.startswith("automation:"):
            continue
        identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
        campaign_name = str(identity.get("campaign_name") or assets.get("campaign_name") or "").strip()
        campaign_code = str(identity.get("campaign_code") or assets.get("campaign_code") or source).strip()
        category = str(identity.get("category") or automation.get("category") or "").strip()
        terms = [str(item).strip() for item in (assets.get("source_terms") or []) if str(item).strip()]
        scoped_signals = _signals_matching_terms(signals, terms) or signals[:20]
        if include_campaign_mapping and campaign_name:
            scope = _scope_payload("campaign", key=campaign_code, name=category or campaign_name, campaign_name=campaign_name)
            campaign_scopes.append(scope)
            if include_callouts:
                drafts.extend(_callout_drafts(scoped_signals, scope=scope, source_prefix="campaign_callout"))
            if include_structured_snippets:
                drafts.extend(_structured_snippet_drafts(scoped_signals, scope=scope, source_prefix="campaign_structured"))
            if include_prices:
                drafts.extend(_price_asset_drafts(scoped_signals, account=account, scope=scope, source_prefix="campaign_price"))
            drafts.extend(_text_asset_mapping_drafts(assets, scope=scope, source_prefix="campaign_text", restricted_terms=restricted_terms))
        if include_ad_group_mapping:
            for cluster in assets.get("keyword_clusters") or []:
                if not isinstance(cluster, dict):
                    continue
                ad_group_name = str(cluster.get("ad_group_name") or "").strip()
                cluster_terms = [str(item).strip() for item in (cluster.get("exact_terms") or []) if str(item).strip()]
                if not ad_group_name:
                    continue
                scoped = _signals_matching_terms(signals, cluster_terms) or scoped_signals[:12]
                scope = _scope_payload(
                    "ad_group",
                    key=source_key(campaign_code, ad_group_name),
                    name=ad_group_name,
                    campaign_name=campaign_name,
                    ad_group_name=ad_group_name,
                )
                if include_callouts:
                    drafts.extend(_callout_drafts(scoped, scope=scope, source_prefix="ad_group_callout"))
                if include_structured_snippets:
                    drafts.extend(_structured_snippet_drafts(scoped, scope=scope, source_prefix="ad_group_structured"))
                drafts.extend(_text_asset_mapping_drafts(assets, scope=scope, source_prefix="ad_group_text", restricted_terms=restricted_terms))
    drafts.extend(_business_name_drafts(account, campaign_scopes=campaign_scopes))
    return drafts


def _signals_matching_terms(signals: list[OdooProductPageSignal], terms: list[str]) -> list[OdooProductPageSignal]:
    needles = [_normal_key(term) for term in terms if _normal_key(term)]
    if not needles:
        return []
    matches: list[OdooProductPageSignal] = []
    for signal in signals:
        haystack = _normal_key(" ".join([signal.product_name, signal.reason, " ".join(_source_categories(signal)), _source_brand(signal)]))
        source = signal.source_json or {}
        haystack += " " + _normal_key(" ".join(str(item or "") for item in source.get("exact_terms") or []))
        if any(needle in haystack or haystack in needle for needle in needles[:50]):
            matches.append(signal)
    return sorted(matches, key=_score_signal, reverse=True)


def _text_asset_mapping_drafts(
    assets: dict[str, Any],
    *,
    scope: dict[str, str],
    source_prefix: str,
    restricted_terms: list[str] | None = None,
) -> list[GeneratedAssetDraft]:
    drafts: list[GeneratedAssetDraft] = []
    text_specs = [
        ("headline", "headlines", 30, ASSET_SCOPE_LIMITS["headline"]),
        ("description", "descriptions", 90, ASSET_SCOPE_LIMITS["description"]),
    ]
    for asset_type, field_name, limit, max_items in text_specs:
        for index, value in enumerate(list(dict.fromkeys(str(item).strip() for item in (assets.get(field_name) or []) if str(item).strip()))[:max_items], start=1):
            text = _public_ad_text(value, limit, fallback="", title_case=False)
            if not text:
                continue
            if _contains_internal_ad_copy(text):
                continue
            if restricted_terms and restricted_title_match(text, restricted_terms):
                continue
            skey = source_key(source_prefix, asset_type, scope.get("level"), scope.get("key"), index, text)
            payload = {
                "text": text,
                "field_name": field_name,
                "scope": scope,
                "mapping": _mapping_payload(
                    scope=scope,
                    dynamic_source="automation_ad_copy",
                    source_key_value=skey,
                    delete_when_source_missing=True,
                ),
            }
            payload["payload_hash"] = payload_hash(payload)
            drafts.append(
                GeneratedAssetDraft(
                    asset_type=asset_type,
                    name=text,
                    source_type="automation_ad_copy",
                    source_key=skey,
                    payload_json=payload,
                    source_json={"scope": scope, "basis": f"{field_name} generated for automation campaign/ad group"},
                    campaign_name=scope.get("campaign_name", ""),
                )
            )
    return drafts


def build_asset_drafts_from_signals(
    signals: list[OdooProductPageSignal],
    *,
    include_sitelinks: bool = True,
    include_structured_snippets: bool = True,
    include_pmax_search_themes: bool = True,
    include_callouts: bool = True,
    include_price_assets: bool = True,
    account: GoogleAdsAccount | None = None,
    offer_callouts: list[str] | None = None,
) -> list[GeneratedAssetDraft]:
    ordered = sorted(
        signals,
        key=lambda signal: (
            {"winner": 0, "watch": 1, "fallback": 2}.get(str(signal.label or ""), 9),
            -float(signal.margin_amount or 0),
            -float(signal.google_conversion_value or 0),
            -float(signal.sales_amount or 0),
        ),
    )
    drafts: list[GeneratedAssetDraft] = []
    account_scope = _scope_payload("account", key="account", name="Account")
    if include_callouts:
        drafts.extend(_callout_drafts(ordered, offer_callouts=offer_callouts, scope=account_scope))
    if include_structured_snippets:
        drafts.extend(_structured_snippet_drafts(ordered, scope=account_scope))
    if include_price_assets:
        drafts.extend(_price_asset_drafts(ordered, account=account, scope=account_scope))
    if include_pmax_search_themes:
        drafts.extend(_pmax_search_theme_drafts(ordered))
    return drafts


def _safe_fields_get(models: Any, store: Any, uid: int, model: str) -> dict[str, Any]:
    try:
        return models.execute_kw(
            store.database,
            uid,
            store.api_key,
            model,
            "fields_get",
            [],
            {"attributes": ["string", "type"]},
        )
    except Exception:
        return {}


def _search_read_promotions(models: Any, store: Any, uid: int, model: str, fields: list[str]) -> list[dict[str, Any]]:
    try:
        return models.execute_kw(
            store.database,
            uid,
            store.api_key,
            model,
            "search_read",
            [[("active", "=", True)]],
            {"fields": fields, "limit": 100, "order": "write_date desc"},
        )
    except Exception:
        return []


def _promotion_kind(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("name", "program_type", "promo_code_usage", "trigger", "code")).lower()
    if "coupon" in text or "code" in text or row.get("code"):
        return "coupon"
    return "discount"


def _offer_callout_text(offer: dict[str, Any]) -> str:
    joined = " ".join(
        str(item or "")
        for item in [offer.get("name"), offer.get("minimum_amount_display"), *(offer.get("reward_lines") or [])]
    )
    amount_match = re.search(r"((?:CA|AU|US)?\$[\d,.]+|[A-Z]{3}\s*[\d,.]+|[\d,.]+\s*[A-Z]{3})", joined)
    if re.search(r"\bfree\b", joined, re.I) and re.search(r"\b(delivery|shipping)\b", joined, re.I):
        suffix = f" {amount_match.group(1)}+" if amount_match else ""
        return text_limit(f"Free Delivery{suffix}", 25)
    for line in offer.get("reward_lines") or []:
        text = text_limit(line, 25)
        if text:
            return text
    return text_limit(offer.get("name") or "", 25)


def _currency_code_from_m2o(value: Any) -> str:
    name = str(value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else value or "")
    token = name.strip().split(" ")[0].upper()
    return token if re.fullmatch(r"[A-Z]{3}", token) else ""


def _read_website_currency_code(models: Any, store: Any, uid: int, website: OdooWebsite | None) -> str:
    if website is None:
        return ""
    website_meta = _safe_fields_get(models, store, uid, "website")
    website_fields = [
        field_name
        for field_name in ("id", "currency_id", "pricelist_id", "default_pricelist_id")
        if field_name in website_meta
    ]
    if "id" not in website_fields:
        website_fields.insert(0, "id")
    website_rows: list[dict[str, Any]] = []
    try:
        website_rows = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "website",
            "read",
            [[int(website.website_id)]],
            {"fields": website_fields},
        )
    except Exception:
        website_rows = []
    row = website_rows[0] if website_rows else {}
    currency_code = _currency_code_from_m2o(row.get("currency_id"))
    if currency_code:
        return currency_code
    pricelist_id = _many2one_id(row.get("pricelist_id")) or _many2one_id(row.get("default_pricelist_id"))
    if not pricelist_id:
        return _currency_from_domain(website.domain)
    pricelist_meta = _safe_fields_get(models, store, uid, "product.pricelist")
    if "currency_id" not in pricelist_meta:
        return _currency_from_domain(website.domain)
    try:
        pricelists = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "product.pricelist",
            "read",
            [[int(pricelist_id)]],
            {"fields": ["id", "currency_id"]},
        )
    except Exception:
        pricelists = []
    currency_code = _currency_code_from_m2o((pricelists[0] if pricelists else {}).get("currency_id"))
    return currency_code or _currency_from_domain(website.domain)


def _cart_offer_program_drafts(
    *,
    store: Any,
    website: OdooWebsite | None,
    mapping: OdooStoreGoogleAdsMapping,
    models: Any,
    uid: int,
    website_currency_code: str = "",
) -> list[GeneratedAssetDraft]:
    if website is None:
        return []
    try:
        offers = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "website",
            "_tp_get_cart_offer_programs",
            [[int(website.website_id)]],
        )
    except Exception:
        return []
    if not isinstance(offers, list):
        return []
    drafts: list[GeneratedAssetDraft] = []
    scope = _scope_payload("account", key="account", name="Account")
    for offer in offers:
        if not isinstance(offer, dict) or not offer.get("program_id"):
            continue
        callout_text = _offer_callout_text(offer)
        codes = [str(code or "").strip() for code in (offer.get("codes") or []) if str(code or "").strip()]
        skey = source_key("promotion", "tp_cart_offer", store.id, website.website_id, offer.get("program_id"))
        payload = {
            "promotion_target": text_limit(offer.get("name") or "Website offer", 60),
            "promotion_kind": "cart_offer",
            "code": text_limit(codes[0] if codes else "", 80),
            "codes": codes[:10],
            "reward_lines": [text_limit(line, 80) for line in (offer.get("reward_lines") or []) if str(line or "").strip()][:10],
            "minimum_amount_display": text_limit(offer.get("minimum_amount_display") or "", 80),
            "minimum_qty": offer.get("minimum_qty") or 0,
            "website": website.name,
            "website_id": int(website.website_id),
            "currency_code": website_currency_code or _currency_from_domain(website.domain),
            "callout_text": callout_text,
            "scope": scope,
            "mapping": _mapping_payload(
                scope=scope,
                dynamic_source="odoo_tp_cart_offer_program",
                source_key_value=skey,
                delete_when_source_missing=True,
            ),
        }
        payload["payload_hash"] = payload_hash(payload)
        drafts.append(
            GeneratedAssetDraft(
                asset_type="promotion",
                name=text_limit(offer.get("name") or "Website offer", 255),
                source_type="odoo_tp_cart_offer_program",
                source_key=skey,
                payload_json=payload,
                source_json={
                    "store_id": store.id,
                    "store_name": store.name,
                    "website_id": int(website.website_id),
                    "website_name": website.name,
                    "website_currency_code": website_currency_code or _currency_from_domain(website.domain),
                    "odoo_model": "website._tp_get_cart_offer_programs",
                    "odoo_row": offer,
                    "scope": scope,
                },
            )
        )
    return drafts


def _promotion_drafts_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    include_discounts: bool,
    include_coupons: bool,
) -> tuple[list[GeneratedAssetDraft], list[str]]:
    if not include_discounts and not include_coupons:
        return [], ["Promotion automation is disabled for discounts and coupons."]
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping)
        .where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
        .order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)
    ).all()
    drafts: list[GeneratedAssetDraft] = []
    notes: list[str] = []
    for mapping in mappings:
        store = mapping.store
        website = None
        if mapping.website_id:
            website = session.scalar(
                select(OdooWebsite).where(
                    OdooWebsite.store_id == store.id,
                    OdooWebsite.website_id == mapping.website_id,
                    OdooWebsite.is_active.is_(True),
                )
            )
        try:
            _base_url, uid, models = _authenticate_store(store)
        except Exception as exc:  # noqa: BLE001 - keep other mapped stores usable.
            notes.append(f"{store.name}: Odoo promotion lookup failed: {str(exc)[:180]}")
            continue
        website_currency_code = _read_website_currency_code(models, store, uid, website)
        addon_drafts = _cart_offer_program_drafts(
            store=store,
            website=website,
            mapping=mapping,
            models=models,
            uid=uid,
            website_currency_code=website_currency_code,
        )
        if addon_drafts:
            drafts.extend(addon_drafts)
            continue
        found_for_store = 0
        for model in PROMOTION_MODEL_CANDIDATES:
            meta = _safe_fields_get(models, store, uid, model)
            if not meta:
                continue
            candidate_fields = [
                field_name
                for field_name in (
                    "id",
                    "name",
                    "active",
                    "program_type",
                    "promo_code_usage",
                    "trigger",
                    "applies_on",
                    "date_from",
                    "date_to",
                    "website_id",
                    "currency_id",
                    "code",
                    "write_date",
                )
                if field_name in meta
            ]
            if "id" not in candidate_fields:
                candidate_fields.insert(0, "id")
            if "name" not in candidate_fields:
                candidate_fields.append("name")
            for row in _search_read_promotions(models, store, uid, model, candidate_fields):
                row_website_id = _many2one_id(row.get("website_id"))
                if row_website_id and mapping.website_id and row_website_id != int(mapping.website_id):
                    continue
                kind = _promotion_kind(row)
                if kind == "coupon" and not include_coupons:
                    continue
                if kind == "discount" and not include_discounts:
                    continue
                name = text_limit(row.get("name") or "Odoo promotion", 255)
                target = text_limit(name, 60)
                code = text_limit(row.get("code") or "", 80)
                scope = _scope_payload("account", key="account", name="Account")
                skey = source_key("promotion", model, store.id, mapping.website_id, row.get("id"))
                payload = {
                    "promotion_target": text_limit(target, 60),
                    "promotion_kind": kind,
                    "code": code,
                    "start_date": row.get("date_from") or "",
                    "end_date": row.get("date_to") or "",
                    "website": website.name if website else "",
                    "currency_code": website_currency_code or _currency_code_from_m2o(row.get("currency_id")) or _currency_from_domain(website.domain if website else store.base_url),
                    "scope": scope,
                    "mapping": _mapping_payload(
                        scope=scope,
                        dynamic_source=model,
                        source_key_value=skey,
                        delete_when_source_missing=True,
                    ),
                }
                payload["payload_hash"] = payload_hash(payload)
                drafts.append(
                    GeneratedAssetDraft(
                        asset_type="promotion",
                        name=name,
                        source_type=model,
                        source_key=skey,
                        payload_json=payload,
                        source_json={
                            "store_id": store.id,
                            "store_name": store.name,
                            "website_id": int(mapping.website_id or 0),
                            "website_name": website.name if website else "",
                            "website_currency_code": website_currency_code,
                            "odoo_model": model,
                            "odoo_row": row,
                            "kind": kind,
                            "scope": scope,
                        },
                        status="needs_review" if not code and kind == "coupon" else "draft",
                    )
                )
                found_for_store += 1
            if found_for_store:
                break
        if not found_for_store:
            notes.append(f"{store.name}: no active Odoo discount/coupon programs found for this account mapping.")
    return drafts, notes


def get_or_create_asset_preferences(session: Session, account_id: int) -> GoogleAdsAssetAutomationPreference:
    preference = session.scalar(
        select(GoogleAdsAssetAutomationPreference).where(GoogleAdsAssetAutomationPreference.account_id == int(account_id))
    )
    if preference is not None:
        return preference
    preference = GoogleAdsAssetAutomationPreference(account_id=int(account_id))
    session.add(preference)
    session.commit()
    session.refresh(preference)
    return preference


def save_asset_preferences(
    session: Session,
    *,
    account_id: int,
    auto_discount_promotions_enabled: bool,
    auto_coupon_promotions_enabled: bool,
    auto_sitelinks_enabled: bool,
    auto_structured_snippets_enabled: bool,
    auto_pmax_search_themes_enabled: bool,
    auto_callouts_enabled: bool = True,
    auto_price_assets_enabled: bool = True,
    auto_business_messages_enabled: bool = False,
    auto_campaign_asset_mapping_enabled: bool = True,
    auto_ad_group_asset_mapping_enabled: bool = True,
    whatsapp_country_code: str = "+1",
    whatsapp_phone_number: str = "",
    whatsapp_starter_message: str = "Can I get help choosing a product?",
    whatsapp_call_to_action: str = "MESSAGE",
    whatsapp_call_to_action_description: str = "Chat with us",
) -> GoogleAdsAssetAutomationPreference:
    preference = get_or_create_asset_preferences(session, account_id)
    preference.auto_discount_promotions_enabled = bool(auto_discount_promotions_enabled)
    preference.auto_coupon_promotions_enabled = bool(auto_coupon_promotions_enabled)
    preference.auto_sitelinks_enabled = bool(auto_sitelinks_enabled)
    preference.auto_structured_snippets_enabled = bool(auto_structured_snippets_enabled)
    preference.auto_pmax_search_themes_enabled = bool(auto_pmax_search_themes_enabled)
    preference.auto_callouts_enabled = bool(auto_callouts_enabled)
    preference.auto_price_assets_enabled = bool(auto_price_assets_enabled)
    preference.auto_business_messages_enabled = bool(auto_business_messages_enabled)
    preference.auto_campaign_asset_mapping_enabled = bool(auto_campaign_asset_mapping_enabled)
    preference.auto_ad_group_asset_mapping_enabled = bool(auto_ad_group_asset_mapping_enabled)
    preference.whatsapp_country_code = text_limit(whatsapp_country_code or "+1", 8)
    preference.whatsapp_phone_number = text_limit(whatsapp_phone_number or "", 40)
    preference.whatsapp_starter_message = text_limit(whatsapp_starter_message or "Can I get help choosing a product?", 140)
    preference.whatsapp_call_to_action = text_limit(whatsapp_call_to_action or "MESSAGE", 40)
    preference.whatsapp_call_to_action_description = text_limit(whatsapp_call_to_action_description or "Chat with us", 30)
    preference.updated_at = utcnow()
    session.commit()
    session.refresh(preference)
    return preference


def _upsert_generated_asset(
    session: Session,
    *,
    account_id: int,
    created_by_id: int | None,
    draft: GeneratedAssetDraft,
    existing_by_key: dict[tuple[str, str], GoogleAdsGeneratedAsset] | None = None,
) -> None:
    now = utcnow()
    existing = (existing_by_key or {}).get((draft.asset_type, draft.source_key))
    if existing is None and existing_by_key is None:
        existing = session.scalar(
            select(GoogleAdsGeneratedAsset).where(
                GoogleAdsGeneratedAsset.account_id == int(account_id),
                GoogleAdsGeneratedAsset.asset_type == draft.asset_type,
                GoogleAdsGeneratedAsset.source_key == draft.source_key,
            )
        )
    name = text_limit(draft.name, 255)
    campaign_name = text_limit(draft.campaign_name, 255)
    if existing is None:
        row = GoogleAdsGeneratedAsset(
            account_id=account_id,
            created_by_id=created_by_id,
            asset_type=draft.asset_type,
            name=name,
            source_type=draft.source_type,
            source_key=draft.source_key,
            status=draft.status,
            campaign_id=draft.campaign_id,
            campaign_name=campaign_name,
            payload_json=draft.payload_json,
            source_json=draft.source_json,
            last_error="",
            updated_at=now,
            last_generated_at=now,
        )
        session.add(row)
        if existing_by_key is not None:
            existing_by_key[(draft.asset_type, draft.source_key)] = row
        return

    old_payload = existing.payload_json if isinstance(existing.payload_json, dict) else {}
    old_hash = str(old_payload.get("payload_hash") or "")
    new_hash = str((draft.payload_json or {}).get("payload_hash") or "")
    payload_changed = bool(old_hash and new_hash and old_hash != new_hash)
    old_resource = str(existing.google_resource_name or "").strip()
    replacement_source_json = dict(draft.source_json or {})
    replacement_payload_json = dict(draft.payload_json or {})
    for metadata_key in ("replaced_google_resource_name",):
        if metadata_key in old_payload and metadata_key not in replacement_payload_json:
            replacement_payload_json[metadata_key] = old_payload.get(metadata_key)
        if metadata_key in old_payload and metadata_key not in replacement_source_json:
            replacement_source_json[metadata_key] = old_payload.get(metadata_key)
        old_source_json = existing.source_json if isinstance(existing.source_json, dict) else {}
        if metadata_key in old_source_json and metadata_key not in replacement_source_json:
            replacement_source_json[metadata_key] = old_source_json.get(metadata_key)
    if payload_changed and old_resource and draft.asset_type in IMMUTABLE_GOOGLE_ASSET_TYPES:
        replacement_source_json["replaced_google_resource_name"] = old_resource
        replacement_payload_json["replaced_google_resource_name"] = old_resource
        existing.google_resource_name = ""
        next_status = "draft"
        next_error = "Payload changed; previous immutable Google asset will be unlinked and replaced."
    elif existing.status in {"published", "validated"} and not payload_changed and old_resource:
        next_status = existing.status
        next_error = existing.last_error or ""
    else:
        next_status = draft.status
        next_error = (existing.last_error or "") if replacement_source_json.get("replaced_google_resource_name") else ""
    next_campaign_id = draft.campaign_id
    next_campaign_name = campaign_name
    if (
        not payload_changed
        and existing.name == name
        and existing.source_type == draft.source_type
        and existing.campaign_id == next_campaign_id
        and existing.campaign_name == next_campaign_name
        and existing.payload_json == replacement_payload_json
        and existing.source_json == replacement_source_json
        and existing.status == next_status
        and (existing.last_error or "") == next_error
    ):
        return
    existing.status = next_status
    existing.name = name
    existing.source_type = draft.source_type
    existing.campaign_id = next_campaign_id
    existing.campaign_name = next_campaign_name
    existing.payload_json = replacement_payload_json
    existing.source_json = replacement_source_json
    existing.last_error = next_error
    existing.updated_at = now
    existing.last_generated_at = now


def _mark_missing_dynamic_assets(
    session: Session,
    *,
    account_id: int,
    active_source_keys: set[str],
    managed_asset_types: set[str],
    max_pending_sitelink_removals: int = CATEGORY_SITELINK_RETIRE_BATCH_SIZE,
) -> int:
    if not managed_asset_types:
        return 0
    rows = session.scalars(
        select(GoogleAdsGeneratedAsset).where(
            GoogleAdsGeneratedAsset.account_id == int(account_id),
            GoogleAdsGeneratedAsset.asset_type.in_(sorted(managed_asset_types)),
            GoogleAdsGeneratedAsset.source_key.notin_(sorted(active_source_keys) or ["__none__"]),
            GoogleAdsGeneratedAsset.status.notin_(["source_removed", "pending_remove", "removed"]),
        )
    ).all()
    changed = 0
    pending_sitelink_removals = 0
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        mapping = payload.get("mapping") if isinstance(payload.get("mapping"), dict) else {}
        if mapping and not mapping.get("delete_when_source_missing", True):
            continue
        if row.asset_type == "sitelink":
            if pending_sitelink_removals >= max(0, int(max_pending_sitelink_removals or 0)):
                deferred_message = (
                    "Legacy/product-url sitelink detected; queued for a later removal batch to protect Google Ads API quota."
                )
                if row.google_resource_name and row.last_error != deferred_message:
                    row.last_error = deferred_message
                    row.updated_at = utcnow()
                    changed += 1
                continue
            pending_sitelink_removals += 1
        next_status = "pending_remove" if row.google_resource_name else "source_removed"
        next_error = "Source no longer exists or no longer qualifies; remove the Google link/asset on next publish sync."
        next_payload = {
            **payload,
            "sync_action": "remove_from_google" if row.google_resource_name else "archive_local",
            "source_missing_at": utcnow().isoformat(),
        }
        if row.status != next_status or row.last_error != next_error or payload.get("sync_action") != next_payload["sync_action"]:
            row.status = next_status
            row.last_error = next_error
            row.payload_json = next_payload
            row.updated_at = utcnow()
            changed += 1
    return changed


def generate_account_assets(
    session: Session,
    *,
    account_id: int,
    created_by_id: int | None = None,
    include_sitelinks: bool | None = None,
    include_structured_snippets: bool | None = None,
    include_pmax_search_themes: bool | None = None,
    include_callouts: bool | None = None,
    include_price_assets: bool | None = None,
    include_business_messages: bool | None = None,
    include_campaign_mapping: bool | None = None,
    include_ad_group_mapping: bool | None = None,
    include_promotions: bool = True,
) -> dict[str, Any]:
    account = session.get(GoogleAdsAccount, int(account_id))
    if account is None:
        raise ValueError("Google Ads account not found.")
    preference = get_or_create_asset_preferences(session, account.id)
    include_sitelinks = preference.auto_sitelinks_enabled if include_sitelinks is None else bool(include_sitelinks)
    include_structured_snippets = (
        preference.auto_structured_snippets_enabled
        if include_structured_snippets is None
        else bool(include_structured_snippets)
    )
    include_pmax_search_themes = (
        preference.auto_pmax_search_themes_enabled
        if include_pmax_search_themes is None
        else bool(include_pmax_search_themes)
    )
    include_callouts = preference.auto_callouts_enabled if include_callouts is None else bool(include_callouts)
    include_price_assets = preference.auto_price_assets_enabled if include_price_assets is None else bool(include_price_assets)
    include_business_messages = (
        preference.auto_business_messages_enabled if include_business_messages is None else bool(include_business_messages)
    )
    include_campaign_mapping = (
        preference.auto_campaign_asset_mapping_enabled if include_campaign_mapping is None else bool(include_campaign_mapping)
    )
    include_ad_group_mapping = (
        preference.auto_ad_group_asset_mapping_enabled if include_ad_group_mapping is None else bool(include_ad_group_mapping)
    )
    signals = session.scalars(
        select(OdooProductPageSignal)
        .where(
            OdooProductPageSignal.account_id == account.id,
            OdooProductPageSignal.label.in_(["winner", "watch", "fallback"]),
            OdooProductPageSignal.product_url != "",
        )
        .order_by(
            OdooProductPageSignal.label,
            OdooProductPageSignal.margin_amount.desc(),
            OdooProductPageSignal.google_conversion_value.desc(),
        )
        .limit(150)
    ).all()
    restricted_terms = get_restricted_title_terms_sync(session)
    filtered_signals = [
        signal
        for signal in signals
        if not restricted_title_match(signal.product_name, restricted_terms)
    ]
    restricted_filtered_count = len(signals) - len(filtered_signals)
    signals = filtered_signals
    drafts = build_asset_drafts_from_signals(
        list(signals),
        include_sitelinks=False,
        include_structured_snippets=include_structured_snippets,
        include_pmax_search_themes=include_pmax_search_themes,
        include_callouts=include_callouts,
        include_price_assets=include_price_assets,
        account=account,
    )
    notes: list[str] = []
    if include_sitelinks:
        category_sitelinks, category_notes = _account_category_sitelink_drafts(session, account, list(signals))
        drafts.extend(category_sitelinks)
        notes.extend(category_notes)
    offer_callouts: list[str] = []
    if include_promotions:
        promotion_drafts, promotion_notes = _promotion_drafts_for_account(
            session,
            account,
            include_discounts=bool(preference.auto_discount_promotions_enabled),
            include_coupons=bool(preference.auto_coupon_promotions_enabled),
        )
        drafts.extend(promotion_drafts)
        notes.extend(promotion_notes)
        offer_callouts = [
            str(draft.payload_json.get("callout_text") or "").strip()
            for draft in promotion_drafts
            if isinstance(draft.payload_json, dict) and str(draft.payload_json.get("callout_text") or "").strip()
        ]
        if include_callouts and offer_callouts:
            drafts.extend(_callout_drafts(list(signals), offer_callouts=offer_callouts, scope=_scope_payload("account", key="account", name="Account"), source_prefix="offer_callout"))
    if include_business_messages:
        drafts.extend(_business_message_draft(account, preference))
    drafts.extend(_business_name_drafts(account))
    drafts.extend(
        _automation_asset_scope_drafts(
            list(signals),
            session=session,
            account=account,
            include_callouts=bool(include_callouts),
            include_structured_snippets=bool(include_structured_snippets),
            include_sitelinks=bool(include_sitelinks),
            include_prices=bool(include_price_assets),
            include_campaign_mapping=bool(include_campaign_mapping),
            include_ad_group_mapping=bool(include_ad_group_mapping),
            restricted_terms=restricted_terms,
        )
    )
    existing_rows = session.scalars(
        select(GoogleAdsGeneratedAsset).where(GoogleAdsGeneratedAsset.account_id == account.id)
    ).all()
    existing_by_key = {(row.asset_type, row.source_key): row for row in existing_rows}
    for index, draft in enumerate(drafts, start=1):
        _upsert_generated_asset(
            session,
            account_id=account.id,
            created_by_id=created_by_id,
            draft=draft,
            existing_by_key=existing_by_key,
        )
        if index % 10 == 0:
            session.flush()
    managed_types: set[str] = set()
    if include_sitelinks:
        managed_types.add("sitelink")
    if include_callouts:
        managed_types.add("callout")
    if include_structured_snippets:
        managed_types.add("structured_snippet")
    if include_price_assets:
        managed_types.add("price")
    if include_promotions:
        managed_types.add("promotion")
    if include_business_messages:
        managed_types.add("business_message")
    managed_types.add("business_name")
    if include_campaign_mapping or include_ad_group_mapping:
        managed_types.update({"headline", "description"})
    active_source_keys = {draft.source_key for draft in drafts}
    removed_count = _mark_missing_dynamic_assets(
        session,
        account_id=account.id,
        active_source_keys=active_source_keys,
        managed_asset_types=managed_types,
        max_pending_sitelink_removals=CATEGORY_SITELINK_RETIRE_BATCH_SIZE,
    )
    session.commit()
    counts: dict[str, int] = {}
    for draft in drafts:
        counts[draft.asset_type] = counts.get(draft.asset_type, 0) + 1
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "signal_count": len(signals),
        "restricted_filtered_count": restricted_filtered_count,
        "generated_count": len(drafts),
        "counts": counts,
        "source_removed_count": removed_count,
        "notes": notes,
        "quota_safe": True,
        "google_api_calls": 0,
        "generated_at": utcnow().isoformat(),
    }


def asset_counts_by_type(session: Session, account_id: int) -> dict[str, int]:
    rows = session.scalars(
        select(GoogleAdsGeneratedAsset).where(GoogleAdsGeneratedAsset.account_id == int(account_id))
    ).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.asset_type] = counts.get(row.asset_type, 0) + 1
    return counts
