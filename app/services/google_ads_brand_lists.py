from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import AppSetting, GoogleAdsAccount, GoogleAdsBrandListCandidate, OdooProductPageSignal
from app.services.google_ads_sync import build_client, enum_name
from app.services.page_feed_restrictions import normalize_restricted_text


AUTO_ACCEPT_CONFIDENCE = 0.86
BRAND_LIST_NAME = "AUTO | Odoo Order Brands"
GOOGLE_ADS_API_QUOTA_PREFIX = "google_ads_asset_publish_quota"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def root_domain(value: str) -> str:
    parsed = urlsplit(value if "://" in str(value or "") else f"https://{value or ''}")
    host = (parsed.netloc or parsed.path).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def country_code_from_domain(domain: str) -> str:
    host = root_domain(domain)
    if host.endswith(".com.au") or host.endswith(".au"):
        return "AU"
    if host.endswith(".co.uk") or host.endswith(".uk"):
        return "GB"
    if host.endswith(".co.nz") or host.endswith(".nz"):
        return "NZ"
    if host.endswith(".ca"):
        return "CA"
    if host.endswith(".in"):
        return "IN"
    if host.endswith(".ie"):
        return "IE"
    if host.endswith(".com"):
        return "US"
    return ""


def country_code_for_account(account: GoogleAdsAccount, domain: str = "") -> str:
    domain_country = country_code_from_domain(domain)
    if domain_country:
        return domain_country
    text = f"{account.name or ''} {account.currency_code or ''}".lower()
    if "canada" in text or re.search(r"\bca\b", text):
        return "CA"
    if "australia" in text or re.search(r"\baud\b|\bau\b", text):
        return "AU"
    if "uk" in text or "united kingdom" in text or re.search(r"\bgbp\b", text):
        return "GB"
    if "india" in text or re.search(r"\binr\b", text):
        return "IN"
    return ""


def _google_ads_quota_retry_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any] | None:
    now = utcnow()
    rows = []
    setting = session.scalar(select(AppSetting).where(AppSetting.key == f"{GOOGLE_ADS_API_QUOTA_PREFIX}.{account.customer_id}"))
    if setting is not None:
        rows.append(setting)
    if setting is None:
        rows.extend(
            item
            for item in session.scalars(select(AppSetting).where(AppSetting.key.like(f"{GOOGLE_ADS_API_QUOTA_PREFIX}.%"))).all()
            if _google_ads_quota_setting_matches_account(item, account)
        )
    active: list[tuple[datetime, AppSetting]] = []
    for item in rows:
        if item is None or not isinstance(item.value, dict):
            continue
        retry_not_before_raw = str(item.value.get("retry_not_before") or "")
        try:
            retry_not_before = datetime.fromisoformat(retry_not_before_raw)
        except ValueError:
            continue
        if retry_not_before.tzinfo is None:
            retry_not_before = retry_not_before.replace(tzinfo=timezone.utc)
        if retry_not_before > now:
            active.append((retry_not_before, item))
    if not active:
        return None
    retry_not_before, quota_setting = max(active, key=lambda pair: pair[0])
    return {
        "retry_not_before": retry_not_before.isoformat(),
        "retry_after_seconds": max(0, int((retry_not_before - now).total_seconds())),
        "reason": quota_setting.value.get("reason") or "Google Ads API quota retry window is active.",
        "quota_key": quota_setting.key,
    }


def _account_developer_token_hash(account: GoogleAdsAccount) -> str:
    token = ""
    if account.connection is not None:
        token = str(account.connection.developer_token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def _google_ads_quota_setting_matches_account(setting: AppSetting, account: GoogleAdsAccount) -> bool:
    if setting.key == f"{GOOGLE_ADS_API_QUOTA_PREFIX}.{account.customer_id}":
        return True
    value = setting.value if isinstance(setting.value, dict) else {}
    if value.get("connection_id") and account.connection_id and int(value["connection_id"]) == int(account.connection_id):
        return True
    token_hash = _account_developer_token_hash(account)
    if token_hash and value.get("developer_token_hash") == token_hash:
        return True
    return False


def _brand_from_signal(signal: OdooProductPageSignal) -> str:
    source = signal.source_json if isinstance(signal.source_json, dict) else {}
    for key in ("brand", "brand_name", "product_brand"):
        value = str(source.get(key) or "").strip()
        if value and normalize_restricted_text(value) not in {"brand", "none", "false"}:
            return value
    return ""


def _brand_groups(session: Session, account: GoogleAdsAccount, *, limit: int = 250) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(OdooProductPageSignal)
        .where(OdooProductPageSignal.account_id == account.id)
        .order_by(OdooProductPageSignal.sales_amount.desc(), OdooProductPageSignal.order_count.desc())
        .limit(max(limit * 10, limit))
    ).all()
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for signal in rows:
        brand = _brand_from_signal(signal)
        normalized = normalize_restricted_text(brand)
        if len(normalized) < 2:
            continue
        key = (int(signal.website_id or 0), normalized)
        entry = grouped.setdefault(
            key,
            {
                "account_id": account.id,
                "store_id": int(signal.store_id or 0) or None,
                "website_id": int(signal.website_id or 0),
                "website_name": signal.website_name or "",
                "website_domain": root_domain(signal.domain or signal.product_url or ""),
                "country_code": country_code_for_account(account, signal.domain or signal.product_url or ""),
                "brand_name": brand[:255],
                "normalized_brand": normalized[:160],
                "order_count": 0,
                "sales_amount": 0.0,
                "signals": [],
            },
        )
        entry["order_count"] += int(signal.order_count or 0)
        entry["sales_amount"] += float(signal.sales_amount or 0)
        if len(entry["signals"]) < 12:
            entry["signals"].append(
                {
                    "product": signal.product_name,
                    "url": signal.product_url,
                    "orders": int(signal.order_count or 0),
                    "sales": float(signal.sales_amount or 0),
                }
            )
    return sorted(grouped.values(), key=lambda item: (item["sales_amount"], item["order_count"]), reverse=True)[:limit]


def _suggested_brand_dict(brand: Any) -> dict[str, Any]:
    urls = [str(url or "").strip() for url in (getattr(brand, "urls", []) or []) if str(url or "").strip()]
    return {
        "id": str(getattr(brand, "id", "") or ""),
        "name": str(getattr(brand, "name", "") or ""),
        "urls": urls,
        "primary_url": urls[0] if urls else "",
        "state": enum_name(getattr(brand, "state", "")),
    }


def suggest_google_brands(client: Any, account: GoogleAdsAccount, brand_name: str) -> list[dict[str, Any]]:
    service = client.get_service("BrandSuggestionService")
    request = client.get_type("SuggestBrandsRequest")
    request.customer_id = account.customer_id
    request.brand_prefix = str(brand_name or "")[:80]
    response = service.suggest_brands(request=request, timeout=30)
    return [_suggested_brand_dict(item) for item in (getattr(response, "brands", []) or [])]


def _domain_country_match_score(urls: list[str], country_code: str) -> float:
    if not country_code:
        return 0.0
    country = country_code.upper()
    country_tlds = {
        "AU": (".com.au", ".au"),
        "CA": (".ca",),
        "GB": (".co.uk", ".uk"),
        "IN": (".in",),
        "NZ": (".co.nz", ".nz"),
        "IE": (".ie",),
        "US": (".com",),
    }.get(country, ())
    if not country_tlds:
        return 0.0
    domains = [root_domain(url) for url in urls if url]
    if any(any(domain.endswith(tld) for tld in country_tlds) for domain in domains):
        return 0.32
    return 0.0


def _second_level_domain_key(url: str) -> str:
    domain = root_domain(url)
    if domain.endswith(".com.au") or domain.endswith(".co.uk") or domain.endswith(".co.nz"):
        domain = ".".join(domain.split(".")[:-2])
    else:
        domain = ".".join(domain.split(".")[:-1])
    label = domain.split(".")[-1] if domain else ""
    return normalize_restricted_text(label)


def score_brand_suggestion(source_brand: str, suggestion: dict[str, Any], country_code: str) -> float:
    source_key = normalize_restricted_text(source_brand)
    name_key = normalize_restricted_text(suggestion.get("name"))
    if not source_key or not name_key:
        return 0.0
    score = 0.0
    if source_key == name_key:
        score += 0.72
    elif source_key in name_key or name_key in source_key:
        score += 0.52
    source_words = set(source_key.split())
    name_words = set(name_key.split())
    unrelated_extra_words = set()
    if source_words and name_words:
        score += min(len(source_words & name_words) / len(source_words | name_words), 1.0) * 0.18
        country_words = {"canada", "ca", "australia", "au", "uk", "united", "kingdom", "usa", "us"}
        legal_words = {"inc", "llc", "ltd", "limited", "corp", "corporation", "company", "co"}
        unrelated_extra_words = name_words - source_words - country_words - legal_words
    score += _domain_country_match_score(list(suggestion.get("urls") or []), country_code)
    if str(suggestion.get("state") or "").upper() == "ENABLED":
        score += 0.04
    if source_key != name_key and unrelated_extra_words:
        score = min(score - 0.25, 0.82)
    if source_key == name_key and len(source_words) == 1:
        source_compact = source_key.replace(" ", "")
        domain_keys = [_second_level_domain_key(url).replace(" ", "") for url in (suggestion.get("urls") or [])]
        if domain_keys and source_compact not in domain_keys:
            score = min(score, 0.82)
    return min(score, 1.0)


def best_brand_suggestion(source_brand: str, suggestions: list[dict[str, Any]], country_code: str) -> tuple[Optional[dict[str, Any]], float]:
    if not suggestions:
        return None, 0.0
    ranked = sorted(
        ((item, score_brand_suggestion(source_brand, item, country_code)) for item in suggestions),
        key=lambda item: item[1],
        reverse=True,
    )
    return ranked[0]


def upsert_brand_list_candidates(session: Session, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(GoogleAdsBrandListCandidate).values(rows)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsBrandListCandidate.account_id,
            GoogleAdsBrandListCandidate.website_id,
            GoogleAdsBrandListCandidate.normalized_brand,
        ],
        set_={
            "store_id": excluded.store_id,
            "website_name": excluded.website_name,
            "website_domain": excluded.website_domain,
            "country_code": excluded.country_code,
            "brand_name": excluded.brand_name,
            "order_count": excluded.order_count,
            "sales_amount": excluded.sales_amount,
            "suggested_brand_id": excluded.suggested_brand_id,
            "suggested_brand_name": excluded.suggested_brand_name,
            "suggested_primary_url": excluded.suggested_primary_url,
            "suggested_urls": excluded.suggested_urls,
            "google_brand_state": excluded.google_brand_state,
            "match_confidence": excluded.match_confidence,
            "match_status": excluded.match_status,
            "source_json": excluded.source_json,
            "candidate_json": excluded.candidate_json,
            "last_seen_at": excluded.last_seen_at,
            "last_synced_at": excluded.last_synced_at,
            "updated_at": excluded.updated_at,
        },
    )
    session.execute(stmt)
    return len(rows)


def sync_account_brand_list_candidates(
    session: Session,
    account: GoogleAdsAccount,
    *,
    client: Any | None = None,
    source_job_id: Optional[int] = None,
    limit: int = 250,
) -> dict[str, Any]:
    now = utcnow()
    quota_retry = _google_ads_quota_retry_state(session, account)
    if quota_retry:
        return {
            "account_id": account.id,
            "customer_id": account.customer_id,
            "status": "blocked_by_google_quota",
            **quota_retry,
        }
    client = client or build_client(get_sync_setting_map(session), account.manager_customer_id, account.connection)
    groups = _brand_groups(session, account, limit=limit)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for group in groups:
        suggestions: list[dict[str, Any]] = []
        try:
            suggestions = suggest_google_brands(client, account, group["brand_name"])
        except Exception as exc:  # noqa: BLE001 - keep remaining brands flowing.
            errors.append({"brand": group["brand_name"], "error": str(exc)[:240]})
        best, confidence = best_brand_suggestion(group["brand_name"], suggestions, group["country_code"])
        best = best or {}
        rows.append(
            {
                "account_id": account.id,
                "store_id": group["store_id"],
                "website_id": group["website_id"],
                "website_name": group["website_name"],
                "website_domain": group["website_domain"],
                "country_code": group["country_code"],
                "brand_name": group["brand_name"],
                "normalized_brand": group["normalized_brand"],
                "order_count": group["order_count"],
                "sales_amount": group["sales_amount"],
                "suggested_brand_id": str(best.get("id") or ""),
                "suggested_brand_name": str(best.get("name") or ""),
                "suggested_primary_url": str(best.get("primary_url") or ""),
                "suggested_urls": list(best.get("urls") or []),
                "google_brand_state": str(best.get("state") or ""),
                "match_confidence": confidence,
                "match_status": "auto_selected" if confidence >= AUTO_ACCEPT_CONFIDENCE else ("request_needed" if not suggestions else "needs_review"),
                "source_json": {
                    "source": "odoo_order_line_brand_attributes",
                    "source_job_id": source_job_id,
                    "signals": group["signals"],
                    "brand_list_name": BRAND_LIST_NAME,
                },
                "candidate_json": {
                    "suggestions": suggestions[:12],
                    "scoring": {
                        "auto_accept_confidence": AUTO_ACCEPT_CONFIDENCE,
                        "country_code": group["country_code"],
                        "preferred_country_domain": True,
                    },
                },
                "last_seen_at": now,
                "last_synced_at": now,
                "updated_at": now,
            }
        )
    saved = upsert_brand_list_candidates(session, rows)
    session.commit()
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "source_brand_count": len(groups),
        "saved": saved,
        "auto_selected": sum(1 for row in rows if row["match_status"] == "auto_selected"),
        "needs_review": sum(1 for row in rows if row["match_status"] == "needs_review"),
        "request_needed": sum(1 for row in rows if row["match_status"] == "request_needed"),
        "errors": errors,
    }
