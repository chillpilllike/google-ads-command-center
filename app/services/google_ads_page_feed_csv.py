from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    GoogleAdsPageFeedAsset,
    GoogleAdsPageFeedPublication,
    OdooProductPageSignal,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.page_feed_restrictions import get_restricted_title_terms, restricted_title_match


CSV_HEADER = ["Page URL", "Custom label"]
DEFAULT_MAX_URLS = 5000
MAX_GOOGLE_PAGE_FEED_URLS = 5_000_000
GOOGLE_CUSTOM_LABEL_LIMIT = 20
PAGE_FEED_KINDS = {
    "best": "Best performing products",
    "good": "Good/watch products",
    "new": "New/cross-site learned products",
    "mixed": "Mixed products",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_public_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return "https://" + text.lstrip("/")


def normalize_max_urls(value: Any, default: int = DEFAULT_MAX_URLS) -> int:
    try:
        count = int(float(value or default))
    except (TypeError, ValueError):
        count = default
    return min(max(count, 1), MAX_GOOGLE_PAGE_FEED_URLS)


def normalize_feed_kind(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if text in PAGE_FEED_KINDS:
        return text
    if text.startswith("best"):
        return "best"
    if text.startswith("good") or text.startswith("watch"):
        return "good"
    if text.startswith("new") or text.startswith("fallback") or text.startswith("discovery"):
        return "new"
    if text.startswith("mixed"):
        return "mixed"
    return "best"


def feed_kind_label(feed_kind: Any) -> str:
    return PAGE_FEED_KINDS.get(normalize_feed_kind(feed_kind), PAGE_FEED_KINDS["best"])


def feed_kind_slug(feed_kind: Any) -> str:
    feed_kind = normalize_feed_kind(feed_kind)
    return {
        "best": "best-products",
        "good": "good-products",
        "new": "new-products",
        "mixed": "mixed-products",
    }.get(feed_kind, "best-products")


def google_label(value: Any, *, prefix: str = "", max_length: int = 80) -> str:
    text = re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper()).strip("_")
    if prefix:
        text = f"{prefix}_{text}" if text else prefix
    return text[:max_length].strip("_") or (prefix or "ODOO")


def feed_slug(*parts: Any, max_length: int = 120) -> str:
    text = "-".join(
        re.sub(r"[^a-z0-9]+", "-", str(part or "").lower()).strip("-")
        for part in parts
        if str(part or "").strip()
    )
    text = re.sub(r"-+", "-", text).strip("-")
    return (text[:max_length].strip("-") or "odoo-page-feed")


def margin_label(margin_percent: float | None) -> str:
    if margin_percent is None:
        return "MARGIN_UNKNOWN"
    if margin_percent >= 0.30:
        return "MARGIN_30_PLUS"
    if margin_percent >= 0.20:
        return "MARGIN_20_PLUS"
    if margin_percent >= 0.10:
        return "MARGIN_10_PLUS"
    return "MARGIN_LOW"


def _public_feed_token(publication: GoogleAdsPageFeedPublication, *, include_feed_kind: bool) -> str:
    settings = get_settings()
    token_parts = [
        str(publication.id or 0),
        str(publication.account_id or 0),
        str(publication.store_id or 0),
        str(publication.website_id or 0),
    ]
    if include_feed_kind:
        token_parts.append(str(getattr(publication, "feed_kind", "best") or "best"))
    token_parts.append(settings.secret_key)
    token_source = ":".join(token_parts)
    return hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:24]


def public_feed_token(publication: GoogleAdsPageFeedPublication) -> str:
    return _public_feed_token(publication, include_feed_kind=False)


def accepted_public_feed_tokens(publication: GoogleAdsPageFeedPublication) -> set[str]:
    return {
        public_feed_token(publication),
        _public_feed_token(publication, include_feed_kind=True),
    }


def public_feed_name(publication: GoogleAdsPageFeedPublication) -> str:
    stored_name = str(publication.asset_set_name or "").strip()
    if stored_name:
        return feed_slug(stored_name)
    result = publication.last_publish_json if isinstance(publication.last_publish_json, dict) else {}
    customer_id = str(result.get("customer_id") or "").strip()
    if not customer_id:
        account = getattr(publication, "account", None)
        customer_id = str(getattr(account, "customer_id", "") or publication.account_id)
    return feed_slug("odoo", publication.website_name or "website", feed_kind_slug(getattr(publication, "feed_kind", "best")), "page-feed", customer_id)


def public_feed_filename(publication: GoogleAdsPageFeedPublication) -> str:
    return f"{public_feed_name(publication)}.csv"


def public_feed_url(publication: GoogleAdsPageFeedPublication, public_base_url: str) -> str:
    base_url = normalize_public_base_url(public_base_url)
    if not base_url:
        return ""
    return (
        f"{base_url}/feeds/google/page-feed/"
        f"{publication.id}/{public_feed_token(publication)}/{public_feed_filename(publication)}"
    )


def csv_labels_for_signal(signal: OdooProductPageSignal, feed_kind: Any = "best") -> list[str]:
    feed_kind = normalize_feed_kind(feed_kind)
    labels: list[str] = ["SINGLE_PRODUCT", google_label(feed_kind_slug(feed_kind), prefix="FEED", max_length=80)]
    if signal.label == "winner":
        labels.extend(["ODOO_WINNER", "TOP_SELLER"])
    elif signal.label == "watch":
        labels.extend(["ODOO_WATCH", "GOOD_PRODUCT"])
    elif signal.label == "fallback":
        labels.extend(["ODOO_FALLBACK", "NEW_PRODUCT"])
    else:
        labels.append(google_label(signal.label or "WATCH", prefix="ODOO"))

    if signal.website_name:
        labels.append(google_label(signal.website_name, prefix="SITE", max_length=80))
    if signal.product_code and not str(signal.product_code).startswith("fallback:"):
        labels.append(google_label(signal.product_code, prefix="SKU", max_length=80))
    labels.append(margin_label(signal.margin_percent))

    if float(signal.google_conversions or 0) > 0:
        labels.append("GOOGLE_CONVERTER")
    elif float(signal.google_cost or 0) > 0:
        labels.append("GOOGLE_TESTED")
    if int(signal.order_count or 0) <= 1 and float(signal.google_conversions or 0) <= 0:
        labels.append("LOW_DATA")

    source_json = signal.source_json if isinstance(signal.source_json, dict) else {}
    source = str(source_json.get("source") or "")
    if source.startswith("global_odoo_product_signal_fallback"):
        labels.append("CROSS_SITE_LEARNED")
    elif source.startswith("google_converting_landing_page_fallback"):
        labels.append("GOOGLE_LANDING_FALLBACK")

    deduped = list(dict.fromkeys(label for label in labels if label))
    return deduped[:GOOGLE_CUSTOM_LABEL_LIMIT]


def is_broad_or_excluded_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    path = parsed.path.strip("/").lower()
    if not path:
        return True
    broad_markers = (
        "shop/category",
        "category/",
        "collections/",
        "cart",
        "checkout",
        "contact",
        "about",
        "blog",
        "privacy",
        "terms",
    )
    return any(marker in path for marker in broad_markers)


async def select_public_feed_signals(
    session: AsyncSession,
    publication: GoogleAdsPageFeedPublication,
    *,
    max_urls: int = DEFAULT_MAX_URLS,
) -> list[OdooProductPageSignal]:
    max_urls = normalize_max_urls(max_urls)
    feed_kind = normalize_feed_kind(getattr(publication, "feed_kind", "best"))
    if feed_kind == "best":
        labels_for_feed = ["winner"]
    elif feed_kind == "good":
        labels_for_feed = ["watch"]
    elif feed_kind == "new":
        labels_for_feed = ["fallback"]
    else:
        labels_for_feed = ["winner", "watch", "fallback"]
    base_filters = [
        OdooProductPageSignal.account_id == publication.account_id,
        OdooProductPageSignal.store_id == publication.store_id,
        OdooProductPageSignal.product_url != "",
    ]
    exclusion_filters = list(base_filters)
    if publication.website_id:
        base_filters.append(OdooProductPageSignal.website_id == publication.website_id)
        exclusion_filters.append(OdooProductPageSignal.website_id == publication.website_id)

    excluded_urls = {
        str(url or "").strip()
        for url in (
            await session.scalars(
                select(OdooProductPageSignal.product_url).where(
                    *exclusion_filters,
                    OdooProductPageSignal.label == "exclude",
                )
            )
        ).all()
    }
    restricted_terms = await get_restricted_title_terms(session)
    priority = case(
        (OdooProductPageSignal.label == "winner", 0),
        (OdooProductPageSignal.label == "watch", 1),
        (OdooProductPageSignal.label == "fallback", 2),
        else_=3,
    )
    rows = (
        await session.scalars(
            select(OdooProductPageSignal)
            .where(
                *base_filters,
                OdooProductPageSignal.label.in_(labels_for_feed),
            )
            .order_by(
                priority,
                OdooProductPageSignal.margin_amount.desc(),
                OdooProductPageSignal.sales_amount.desc(),
                OdooProductPageSignal.google_conversion_value.desc(),
            )
            .limit(min(max_urls * 3, max_urls + 1000))
        )
    ).all()

    by_url: dict[str, OdooProductPageSignal] = {}
    for signal in rows:
        url = str(signal.product_url or "").strip()
        if not url or url in excluded_urls or is_broad_or_excluded_url(url):
            continue
        if restricted_title_match(signal.product_name, restricted_terms):
            continue
        current = by_url.get(url)
        if current is None or (signal.label == "winner" and current.label != "winner"):
            by_url[url] = signal
        if len(by_url) >= max_urls:
            break
    return list(by_url.values())[:max_urls]


def page_feed_csv_text(signals: list[OdooProductPageSignal]) -> str:
    return page_feed_csv_text_for_kind(signals, "best")


def page_feed_csv_text_for_kind(signals: list[OdooProductPageSignal], feed_kind: Any = "best") -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for signal in signals:
        writer.writerow([str(signal.product_url or "").strip(), ";".join(csv_labels_for_signal(signal, feed_kind))])
    return output.getvalue()


async def upsert_public_feed_asset_rows(
    session: AsyncSession,
    publication: GoogleAdsPageFeedPublication,
    signals: list[OdooProductPageSignal],
) -> None:
    feed_kind = normalize_feed_kind(getattr(publication, "feed_kind", "best"))
    for signal in signals:
        page_url = str(signal.product_url or "").strip()
        if not page_url:
            continue
        page_url_hash = hashlib.md5(page_url.encode("utf-8")).hexdigest()
        stmt = insert(GoogleAdsPageFeedAsset).values(
            publication_id=publication.id,
            signal_id=signal.id,
            page_url=page_url,
            page_url_hash=page_url_hash,
            labels=csv_labels_for_signal(signal, feed_kind),
            status="feed_url_ready",
            source_json={
                "source": "hosted_csv",
                "feed_kind": feed_kind,
                "signal_label": signal.label,
                "product_code": signal.product_code,
                "product_name": signal.product_name,
                "sales_amount": signal.sales_amount,
                "margin_amount": signal.margin_amount,
                "margin_percent": signal.margin_percent,
                "google_conversions": signal.google_conversions,
            },
            synced_at=utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[GoogleAdsPageFeedAsset.publication_id, GoogleAdsPageFeedAsset.page_url_hash],
            set_={
                "signal_id": stmt.excluded.signal_id,
                "page_url": stmt.excluded.page_url,
                "labels": stmt.excluded.labels,
                "status": stmt.excluded.status,
                "source_json": stmt.excluded.source_json,
                "synced_at": stmt.excluded.synced_at,
            },
        )
        await session.execute(stmt)


async def prepare_public_feed_for_mapping(
    session: AsyncSession,
    mapping: OdooStoreGoogleAdsMapping,
    *,
    public_base_url: str,
    max_urls: int = DEFAULT_MAX_URLS,
    feed_kind: str = "best",
) -> GoogleAdsPageFeedPublication:
    max_urls = normalize_max_urls(max_urls)
    feed_kind = normalize_feed_kind(feed_kind)
    website = None
    if mapping.website_id:
        website = await session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.store_id == mapping.store_id,
                OdooWebsite.website_id == mapping.website_id,
                OdooWebsite.is_active.is_(True),
            )
        )
    website_id = int(website.website_id if website else mapping.website_id or 0)
    website_name = website.name if website else "All websites"
    feed_name = feed_slug("odoo", website_name, feed_kind_slug(feed_kind), "page-feed", mapping.account.customer_id)

    stmt = insert(GoogleAdsPageFeedPublication).values(
        account_id=mapping.account_id,
        store_id=mapping.store_id,
        website_id=website_id,
        website_name=website_name,
        feed_kind=feed_kind,
        asset_set_name=feed_name,
        status="feed_url_ready",
        last_publish_json={
            "mode": "hosted_csv",
            "max_urls": max_urls,
            "api_calls_used": 0,
        },
        updated_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsPageFeedPublication.account_id,
            GoogleAdsPageFeedPublication.store_id,
            GoogleAdsPageFeedPublication.website_id,
            GoogleAdsPageFeedPublication.feed_kind,
        ],
        set_={
            "website_name": stmt.excluded.website_name,
            "asset_set_name": stmt.excluded.asset_set_name,
            "status": stmt.excluded.status,
            "last_error": "",
            "last_publish_json": stmt.excluded.last_publish_json,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)
    await session.flush()
    publication = await session.scalar(
        select(GoogleAdsPageFeedPublication).where(
            GoogleAdsPageFeedPublication.account_id == mapping.account_id,
            GoogleAdsPageFeedPublication.store_id == mapping.store_id,
            GoogleAdsPageFeedPublication.website_id == website_id,
            GoogleAdsPageFeedPublication.feed_kind == feed_kind,
        )
    )
    if publication is None:
        raise RuntimeError("Could not prepare page feed publication.")

    signals = await select_public_feed_signals(session, publication, max_urls=max_urls)
    await upsert_public_feed_asset_rows(session, publication, signals)
    feed_url = public_feed_url(publication, public_base_url)
    publication.status = "feed_url_ready"
    publication.last_error = ""
    publication.last_publish_json = {
        "mode": "hosted_csv",
        "feed_name": public_feed_name(publication),
        "filename": public_feed_filename(publication),
        "feed_url": feed_url,
        "feed_kind": feed_kind,
        "feed_kind_label": feed_kind_label(feed_kind),
        "customer_id": mapping.account.customer_id,
        "selected_urls": len(signals),
        "max_urls": max_urls,
        "api_calls_used": 0,
        "custom_labels": sorted({label for signal in signals for label in csv_labels_for_signal(signal, feed_kind)}),
        "updated_at": utcnow().isoformat(),
    }
    return publication
