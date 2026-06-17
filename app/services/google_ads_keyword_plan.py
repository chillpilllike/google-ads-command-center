from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsDataSnapshot
from app.services.google_ads_snapshot_store import (
    DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
    DATASET_SEARCH_TERM_INSIGHTS,
    DATASET_SEARCH_TERMS,
)


KEYWORD_LOOKBACK_OPTIONS = {
    "30": "Last 30 days",
    "60": "Last 60 days",
    "90": "Last 90 days",
    "all_time": "All saved time",
}

KEYWORD_DATASETS = {DATASET_SEARCH_TERMS, DATASET_SEARCH_TERM_INSIGHTS, DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_keyword_lookback(value: str | None) -> str:
    value = str(value or "60").strip().lower()
    if value in KEYWORD_LOOKBACK_OPTIONS:
        return value
    if value in {"all", "alltime", "all-time"}:
        return "all_time"
    return "60"


def _rows_from_snapshot(snapshot: GoogleAdsDataSnapshot) -> list[dict[str, Any]]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def keyword_text_for_row(dataset_key: str, row: dict[str, Any]) -> str:
    if dataset_key == DATASET_SEARCH_TERMS:
        return str(row.get("search_term") or "").strip()
    if dataset_key == DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS:
        return str(row.get("search_term") or "").strip()
    if dataset_key == DATASET_SEARCH_TERM_INSIGHTS:
        return str(row.get("category_label") or "").strip()
    return ""


def clean_keyword(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = value.strip("\"'`[]")
    return value[:80].strip(" ,.;:")


def normalized_keyword(value: str) -> str:
    return clean_keyword(value).lower()


def usable_keyword(value: str) -> bool:
    value = clean_keyword(value)
    if len(value) < 3:
        return False
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "www.")):
        return False
    if lowered in {"not set", "(not set)", "unknown", "other"}:
        return False
    if re.fullmatch(r"[\W_]+", lowered):
        return False
    return True


def metric_value(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def keyword_row_score(row: dict[str, Any]) -> float:
    conversions = metric_value(row, "conversions") + metric_value(row, "all_conversions")
    conversion_value = metric_value(row, "conversion_value") + metric_value(row, "all_conversions_value")
    clicks = metric_value(row, "clicks")
    impressions = metric_value(row, "impressions")
    cost = metric_value(row, "cost")
    zero_conversion_penalty = cost * 2 if conversions <= 0 else 0
    return (conversions * 100000) + (conversion_value * 1000) + (clicks * 100) + impressions - zero_conversion_penalty


def _keyword_text(dataset_key: str, row: dict[str, Any]) -> str:
    return keyword_text_for_row(dataset_key, row)


def _clean_keyword(value: str) -> str:
    return clean_keyword(value)


def _usable_keyword(value: str) -> bool:
    return usable_keyword(value)


def _metric(row: dict[str, Any], key: str) -> float:
    return metric_value(row, key)


def _row_score(row: dict[str, Any]) -> float:
    return keyword_row_score(row)


def _snapshot_matches_lookback(snapshot: GoogleAdsDataSnapshot, lookback: str) -> bool:
    if lookback == "all_time":
        return True
    return str(snapshot.scope_key or "").startswith(f"last_{lookback}d:")


def _snapshots_for_keyword_plan(
    session: Session,
    account: GoogleAdsAccount,
    lookback: str,
) -> list[GoogleAdsDataSnapshot]:
    snapshots = session.scalars(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key.in_(list(KEYWORD_DATASETS)),
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
    ).all()
    snapshots = [snapshot for snapshot in snapshots if _snapshot_matches_lookback(snapshot, lookback)]
    if lookback == "all_time":
        return snapshots
    latest_by_dataset: dict[str, GoogleAdsDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_dataset.setdefault(snapshot.dataset_key, snapshot)
    return list(latest_by_dataset.values())


def google_keyword_plan_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    lookback: str = "60",
    limit: int = 30,
) -> dict[str, Any]:
    lookback = normalize_keyword_lookback(lookback)
    limit = min(max(int(limit or 30), 1), 100)
    snapshots = _snapshots_for_keyword_plan(session, account, lookback)
    terms: dict[str, dict[str, Any]] = {}
    datasets: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        rows = _rows_from_snapshot(snapshot)
        datasets[snapshot.dataset_key] = {
            "scope_key": snapshot.scope_key,
            "row_count": snapshot.row_count,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
        }
        for row in rows:
            if not isinstance(row, dict):
                continue
            keyword = _clean_keyword(_keyword_text(snapshot.dataset_key, row))
            if not _usable_keyword(keyword):
                continue
            clicks = _metric(row, "clicks")
            conversions = _metric(row, "conversions") + _metric(row, "all_conversions")
            if clicks <= 0 and conversions <= 0:
                continue
            key = keyword.lower()
            entry = terms.setdefault(
                key,
                {
                    "keyword": keyword,
                    "impressions": 0.0,
                    "clicks": 0.0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                    "score": 0.0,
                    "datasets": [],
                    "campaigns": [],
                },
            )
            entry["impressions"] += _metric(row, "impressions")
            entry["clicks"] += clicks
            entry["cost"] += _metric(row, "cost")
            entry["conversions"] += conversions
            entry["conversion_value"] += _metric(row, "conversion_value") + _metric(row, "all_conversions_value")
            entry["score"] += _row_score(row)
            if snapshot.dataset_key not in entry["datasets"]:
                entry["datasets"].append(snapshot.dataset_key)
            campaign_name = str(row.get("campaign_name") or "").strip()
            if campaign_name and campaign_name not in entry["campaigns"]:
                entry["campaigns"].append(campaign_name)
    rows = sorted(terms.values(), key=lambda item: (item["score"], item["conversions"], item["clicks"]), reverse=True)
    rows = rows[:limit]
    exact_terms = [row["keyword"] for row in rows]
    return {
        "source": "saved_google_ads_snapshots",
        "lookback": lookback,
        "lookback_label": KEYWORD_LOOKBACK_OPTIONS[lookback],
        "datasets": datasets,
        "snapshot_ids": [snapshot.id for snapshot in snapshots],
        "terms": exact_terms,
        "exact_terms": exact_terms,
        "rows": rows,
        "row_count": len(rows),
        "generated_at": _utcnow().isoformat(),
        "criteria": [
            "Uses saved Google Ads search_terms, search_term_insights, and AI Max search term/ad combination snapshots only.",
            "Keeps terms with clicks or conversions, then ranks by conversions, value, clicks, impressions, and wasted spend.",
            "Does not call Google Ads while generating the draft.",
        ],
    }
