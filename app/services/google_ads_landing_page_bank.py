from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsDataSnapshot, GoogleAdsLandingPageCandidate
from app.services.google_ads_research_collector import ResearchDatasetSpec, fetch_rows, query_landing_pages, parse_landing_page
from app.services.google_ads_snapshot_store import (
    DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
    DATASET_LANDING_PAGES,
    get_fresh_snapshot,
    upsert_snapshot,
)
from app.services.google_ads_sync import build_client


ALL_TIME_START_DATE = date(2000, 1, 1)
LANDING_PAGE_SNAPSHOT_TTL_HOURS = 24
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "fbclid",
    "gad_source",
    "gad_campaignid",
    "gad_adgroupid",
    "gad_keyword",
}
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "auth_token",
    "checkout_token",
    "hash",
    "key",
    "pid",
    "signature",
    "token",
}
PRIVATE_PATH_PREFIXES = (
    "/account",
    "/cart",
    "/checkout",
    "/my",
    "/order",
    "/orders",
    "/payment",
    "/shop/cart",
    "/shop/checkout",
    "/web",
    "/website/payment",
)
LANDING_PAGE_DATASETS = {DATASET_LANDING_PAGES, DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rows_from_snapshot(snapshot: GoogleAdsDataSnapshot) -> list[dict[str, Any]]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def _payload_date_range(snapshot: GoogleAdsDataSnapshot) -> dict[str, Any]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    value = payload.get("date_range")
    return value if isinstance(value, dict) else {}


def _append_unique(target: list[Any], value: Any, *, limit: int = 30) -> None:
    if value in {None, ""}:
        return
    if value not in target and len(target) < limit:
        target.append(value)


def canonical_landing_page_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme and not parsed.netloc:
        text = "https://" + text.lstrip("/")
        parsed = urlsplit(text)
    scheme = (parsed.scheme or "https").lower()
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/":
        path = path.rstrip("/")
    kept_query = []
    for key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_QUERY_KEYS or any(lowered.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        kept_query.append((key, raw_value))
    query = urlencode(kept_query, doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def landing_page_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def usable_landing_page_url(value: str) -> bool:
    normalized = canonical_landing_page_url(value)
    if not normalized:
        return False
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or "." not in parsed.netloc:
        return False
    path = (parsed.path or "/").lower()
    if any(path == prefix or path.startswith(prefix + "/") for prefix in PRIVATE_PATH_PREFIXES):
        return False
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys.intersection(SENSITIVE_QUERY_KEYS):
        return False
    return True


def metric_value(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def landing_page_row_score(row: dict[str, Any]) -> float:
    conversions = metric_value(row, "conversions") + metric_value(row, "all_conversions")
    conversion_value = metric_value(row, "conversion_value") + metric_value(row, "all_conversions_value")
    clicks = metric_value(row, "clicks")
    impressions = metric_value(row, "impressions")
    cost = metric_value(row, "cost")
    zero_conversion_penalty = cost * 2 if conversions <= 0 else 0
    return (conversions * 120000) + (conversion_value * 1000) + (clicks * 100) + impressions - zero_conversion_penalty


def landing_page_quality_label(entry: dict[str, Any]) -> str:
    conversions = float(entry.get("conversions") or 0) + float(entry.get("all_conversions") or 0)
    conversion_value = float(entry.get("conversion_value") or 0) + float(entry.get("all_conversions_value") or 0)
    clicks = int(entry.get("clicks") or 0)
    if conversions > 0 and conversion_value > 0:
        return "revenue"
    if conversions > 0:
        return "converting"
    if clicks >= 10:
        return "clicked"
    return "watch"


def latest_landing_page_snapshots(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
) -> list[GoogleAdsDataSnapshot]:
    query = (
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key.in_(list(LANDING_PAGE_DATASETS)),
        )
        .order_by(
            GoogleAdsDataSnapshot.dataset_key,
            GoogleAdsDataSnapshot.fetched_at.desc(),
            GoogleAdsDataSnapshot.id.desc(),
        )
    )
    if scope_key:
        query = query.where(GoogleAdsDataSnapshot.scope_key == scope_key)
    snapshots = session.scalars(query).all()
    if scope_key:
        return list(snapshots)
    latest_by_dataset: dict[str, GoogleAdsDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_dataset.setdefault(snapshot.dataset_key, snapshot)
    return list(latest_by_dataset.values())


def landing_page_candidates_from_snapshots(
    account: GoogleAdsAccount,
    snapshots: list[GoogleAdsDataSnapshot],
    *,
    pulled_at: Optional[datetime] = None,
    source_job_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    pulled_at = pulled_at or utcnow()
    pages: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.dataset_key not in LANDING_PAGE_DATASETS:
            continue
        date_range = _payload_date_range(snapshot)
        for row in _rows_from_snapshot(snapshot):
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or row.get("expanded_final_url") or row.get("landing_page") or "").strip()
            if not usable_landing_page_url(url):
                continue
            normalized_url = canonical_landing_page_url(url)
            normalized_url_hash = landing_page_hash(normalized_url)
            clicks = int(metric_value(row, "clicks"))
            conversions = metric_value(row, "conversions")
            all_conversions = metric_value(row, "all_conversions")
            impressions = int(metric_value(row, "impressions"))
            if clicks <= 0 and conversions <= 0 and all_conversions <= 0 and impressions <= 0:
                continue
            entry = pages.setdefault(
                normalized_url_hash,
                {
                    "account_id": account.id,
                    "url": normalized_url,
                    "normalized_url": normalized_url,
                    "normalized_url_hash": normalized_url_hash,
                    "source_dataset_keys": [],
                    "source_scope_keys": [],
                    "source_snapshot_ids": [],
                    "campaign_ids": [],
                    "campaign_names": [],
                    "channel_types": [],
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                    "all_conversions": 0.0,
                    "all_conversions_value": 0.0,
                    "score": 0.0,
                    "source_rows": [],
                    "date_ranges": [],
                },
            )
            entry["url"] = normalized_url
            _append_unique(entry["source_dataset_keys"], snapshot.dataset_key)
            _append_unique(entry["source_scope_keys"], snapshot.scope_key)
            _append_unique(entry["source_snapshot_ids"], snapshot.id)
            _append_unique(entry["campaign_ids"], int(row.get("campaign_id") or 0) or None)
            _append_unique(entry["campaign_names"], str(row.get("campaign_name") or "").strip())
            _append_unique(entry["channel_types"], str(row.get("channel_type") or "").strip())
            if date_range and date_range not in entry["date_ranges"]:
                entry["date_ranges"].append(date_range)
            entry["impressions"] += impressions
            entry["clicks"] += clicks
            entry["cost"] += metric_value(row, "cost")
            entry["conversions"] += conversions
            entry["conversion_value"] += metric_value(row, "conversion_value")
            entry["all_conversions"] += all_conversions
            entry["all_conversions_value"] += metric_value(row, "all_conversions_value")
            entry["score"] += landing_page_row_score(row)
            if len(entry["source_rows"]) < 12:
                entry["source_rows"].append(
                    {
                        "dataset_key": snapshot.dataset_key,
                        "scope_key": snapshot.scope_key,
                        "campaign_id": row.get("campaign_id"),
                        "campaign_name": row.get("campaign_name"),
                        "channel_type": row.get("channel_type"),
                        "raw_url": url,
                        "search_term": row.get("search_term"),
                        "headline": row.get("headline"),
                        "match_source": row.get("match_source") or row.get("source"),
                        "impressions": impressions,
                        "clicks": clicks,
                        "cost": metric_value(row, "cost"),
                        "conversions": conversions,
                        "conversion_value": metric_value(row, "conversion_value"),
                    }
                )

    candidates: list[dict[str, Any]] = []
    for entry in pages.values():
        entry["quality_label"] = landing_page_quality_label(entry)
        entry["source_json"] = {
            "source": "google_ads_landing_page_snapshots",
            "source_job_id": source_job_id,
            "pulled_at": pulled_at.isoformat(),
            "dedupe_key": "account_id+normalized_url_hash",
            "date_ranges": entry.pop("date_ranges"),
            "source_rows": entry.pop("source_rows"),
        }
        entry["last_seen_at"] = pulled_at
        entry["last_pulled_at"] = pulled_at
        entry["last_source_job_id"] = source_job_id
        entry["updated_at"] = pulled_at
        candidates.append(entry)
    return sorted(
        candidates,
        key=lambda item: (
            float(item.get("score") or 0),
            float(item.get("conversions") or 0) + float(item.get("all_conversions") or 0),
            int(item.get("clicks") or 0),
        ),
        reverse=True,
    )


def upsert_landing_page_candidates(session: Session, candidates: list[dict[str, Any]]) -> int:
    if not candidates:
        return 0
    stmt = insert(GoogleAdsLandingPageCandidate).values(candidates)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsLandingPageCandidate.account_id,
            GoogleAdsLandingPageCandidate.normalized_url_hash,
        ],
        set_={
            "url": excluded.url,
            "normalized_url": excluded.normalized_url,
            "quality_label": excluded.quality_label,
            "source_dataset_keys": excluded.source_dataset_keys,
            "source_scope_keys": excluded.source_scope_keys,
            "source_snapshot_ids": excluded.source_snapshot_ids,
            "campaign_ids": excluded.campaign_ids,
            "campaign_names": excluded.campaign_names,
            "channel_types": excluded.channel_types,
            "impressions": excluded.impressions,
            "clicks": excluded.clicks,
            "cost": excluded.cost,
            "conversions": excluded.conversions,
            "conversion_value": excluded.conversion_value,
            "all_conversions": excluded.all_conversions,
            "all_conversions_value": excluded.all_conversions_value,
            "score": excluded.score,
            "source_json": excluded.source_json,
            "last_seen_at": excluded.last_seen_at,
            "last_pulled_at": excluded.last_pulled_at,
            "last_source_job_id": excluded.last_source_job_id,
            "updated_at": excluded.updated_at,
        },
    )
    session.execute(stmt)
    return len(candidates)


def sync_account_landing_page_candidates(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    pulled_at = utcnow()
    fresh_cutoff = pulled_at - timedelta(hours=LANDING_PAGE_SNAPSHOT_TTL_HOURS)
    existing_count, existing_last_pulled_at = session.execute(
        select(
            func.count(GoogleAdsLandingPageCandidate.id),
            func.max(GoogleAdsLandingPageCandidate.last_pulled_at),
        ).where(GoogleAdsLandingPageCandidate.account_id == account.id)
    ).one()
    if existing_count and existing_last_pulled_at and existing_last_pulled_at >= fresh_cutoff:
        return {
            "account_id": account.id,
            "customer_id": account.customer_id,
            "scope_key": scope_key,
            "snapshot_ids": [],
            "candidate_count": int(existing_count or 0),
            "saved": 0,
            "cached": True,
            "last_pulled_at": existing_last_pulled_at.isoformat(),
            "pulled_at": pulled_at.isoformat(),
        }
    snapshots = latest_landing_page_snapshots(session, account, scope_key=scope_key)
    candidates = landing_page_candidates_from_snapshots(
        account,
        snapshots,
        pulled_at=pulled_at,
        source_job_id=source_job_id,
    )
    snapshot_ids = [snapshot.id for snapshot in snapshots]
    saved = upsert_landing_page_candidates(session, candidates)
    session.commit()
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "scope_key": scope_key,
        "snapshot_ids": snapshot_ids,
        "candidate_count": len(candidates),
        "saved": saved,
        "pulled_at": pulled_at.isoformat(),
    }


def landing_page_scope(mode: str, start_date: date, end_date: date, days: Optional[int] = None) -> str:
    if mode == "all_time":
        return f"all_time:{start_date.isoformat()}:{end_date.isoformat()}"
    return f"last_{int(days or 60)}d:{start_date.isoformat()}:{end_date.isoformat()}"


def _snapshot_payload(
    account: GoogleAdsAccount,
    *,
    rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    max_rows: int,
    mode: str,
) -> dict[str, Any]:
    return {
        "account": {
            "id": account.id,
            "name": account.name,
            "customer_id": account.customer_id,
            "currency_code": account.currency_code,
        },
        "dataset_key": DATASET_LANDING_PAGES,
        "title": "Landing pages",
        "date_range": {
            "mode": mode,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "rows": rows,
        "row_limit": max_rows,
        "notes": [
            "Stored in Postgres for landing-page reuse, page-feed planning, and campaign factory duplicate checks.",
            "Landing pages are deduped into the landing-page bank by account and normalized URL hash.",
        ],
        "fetched_at": utcnow().isoformat(),
    }


def sync_account_landing_page_pull(
    session: Session,
    account: GoogleAdsAccount,
    *,
    days: int = 60,
    max_rows: int = 5000,
    mode: str = "recent",
    force: bool = False,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    mode = "all_time" if str(mode or "").strip().lower() == "all_time" else "recent"
    end_date = date.today()
    if mode == "all_time":
        start_date = ALL_TIME_START_DATE
        scope_key = landing_page_scope(mode, start_date, end_date)
        max_rows = min(max(int(max_rows or 100_000), 50), 250_000)
    else:
        days = min(max(int(days or 60), 1), 365)
        start_date = end_date - timedelta(days=days - 1)
        scope_key = landing_page_scope(mode, start_date, end_date, days)
        max_rows = min(max(int(max_rows or 5000), 50), 50_000)
    query = query_landing_pages(start_date, end_date, max_rows)
    expires_at = utcnow() + timedelta(hours=LANDING_PAGE_SNAPSHOT_TTL_HOURS)
    snapshot = None if force else get_fresh_snapshot(
        session,
        dataset_key=DATASET_LANDING_PAGES,
        account=account,
        scope_key=scope_key,
        query=query,
    )
    dataset_result: dict[str, Any]
    if snapshot is not None:
        dataset_result = {
            "dataset_key": DATASET_LANDING_PAGES,
            "status": "cached",
            "rows": snapshot.row_count,
            "snapshot_id": snapshot.id,
        }
    else:
        values = get_sync_setting_map(session)
        client = build_client(values, account.manager_customer_id, account.connection)
        spec = ResearchDatasetSpec(DATASET_LANDING_PAGES, "Landing pages", query_landing_pages, parse_landing_page)
        query, rows = fetch_rows(client, account, spec, start_date, end_date, max_rows)
        upsert_snapshot(
            session,
            dataset_key=DATASET_LANDING_PAGES,
            payload=_snapshot_payload(
                account,
                rows=rows,
                start_date=start_date,
                end_date=end_date,
                max_rows=max_rows,
                mode=mode,
            ),
            scope_key=scope_key,
            query=query,
            account=account,
            expires_at=expires_at,
            source_job_id=source_job_id,
            row_count=len(rows),
        )
        session.commit()
        snapshot = get_fresh_snapshot(
            session,
            dataset_key=DATASET_LANDING_PAGES,
            account=account,
            scope_key=scope_key,
            query=query,
        )
        dataset_result = {
            "dataset_key": DATASET_LANDING_PAGES,
            "status": "fetched",
            "rows": len(rows),
            "snapshot_id": snapshot.id if snapshot is not None else None,
        }

    landing_page_result = sync_account_landing_page_candidates(
        session,
        account,
        scope_key=scope_key,
        source_job_id=source_job_id,
    )
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "scope_key": scope_key,
        "dataset": dataset_result,
        "landing_page_result": landing_page_result,
    }
