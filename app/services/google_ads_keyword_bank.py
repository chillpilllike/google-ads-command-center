from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsDataSnapshot, GoogleAdsKeywordCandidate
from app.services.google_ads_keyword_plan import (
    KEYWORD_DATASETS,
    clean_keyword,
    keyword_row_score,
    keyword_text_for_row,
    metric_value,
    normalized_keyword,
    usable_keyword,
)
from app.app_settings import get_sync_setting_map
from app.services.google_ads_research_collector import (
    DEFAULT_RESEARCH_MAX_ROWS,
    campaign_base,
    metric_payload,
    parse_search_term,
    query_search_term_insights_for_campaign,
    rows_by_campaign,
    search_term_insight_loop_query,
)
from app.services.google_ads_snapshot_store import DATASET_SEARCH_TERM_INSIGHTS, DATASET_SEARCH_TERMS, get_fresh_snapshot, upsert_snapshot
from app.services.google_ads_sync import build_client, enum_name


ALL_TIME_START_DATE = date(2000, 1, 1)
ALL_TIME_SNAPSHOT_TTL_HOURS = 24
ALL_TIME_INSIGHT_CAMPAIGN_LIMIT = 100
ALL_TIME_INSIGHT_PER_CAMPAIGN_LIMIT = 500
GOOGLE_ADS_SEARCH_STREAM_TIMEOUT_SECONDS = 45


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


def _append_unique(target: list[Any], value: Any, *, limit: int = 20) -> None:
    if value in {None, ""}:
        return
    if value not in target and len(target) < limit:
        target.append(value)


def _quality_label(entry: dict[str, Any]) -> str:
    conversions = float(entry.get("conversions") or 0) + float(entry.get("all_conversions") or 0)
    conversion_value = float(entry.get("conversion_value") or 0) + float(entry.get("all_conversions_value") or 0)
    clicks = int(entry.get("clicks") or 0)
    if conversions > 0 and conversion_value > 0:
        return "revenue"
    if conversions > 0:
        return "converting"
    if clicks >= 5:
        return "clicked"
    return "watch"


def keyword_candidates_from_snapshots(
    account: GoogleAdsAccount,
    snapshots: list[GoogleAdsDataSnapshot],
    *,
    pulled_at: Optional[datetime] = None,
    source_job_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    pulled_at = pulled_at or utcnow()
    terms: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.dataset_key not in KEYWORD_DATASETS:
            continue
        date_range = _payload_date_range(snapshot)
        for row in _rows_from_snapshot(snapshot):
            if not isinstance(row, dict):
                continue
            keyword = clean_keyword(keyword_text_for_row(snapshot.dataset_key, row))
            if not usable_keyword(keyword):
                continue
            clicks = int(metric_value(row, "clicks"))
            conversions = metric_value(row, "conversions")
            all_conversions = metric_value(row, "all_conversions")
            impressions = int(metric_value(row, "impressions"))
            if clicks <= 0 and conversions <= 0 and all_conversions <= 0 and impressions <= 0:
                continue
            key = normalized_keyword(keyword)
            entry = terms.setdefault(
                key,
                {
                    "account_id": account.id,
                    "keyword": keyword,
                    "normalized_keyword": key,
                    "source_dataset_keys": [],
                    "source_scope_keys": [],
                    "source_snapshot_ids": [],
                    "campaign_ids": [],
                    "campaign_names": [],
                    "ad_group_names": [],
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
            if keyword != key and len(keyword) > len(str(entry.get("keyword") or "")):
                entry["keyword"] = keyword
            _append_unique(entry["source_dataset_keys"], snapshot.dataset_key)
            _append_unique(entry["source_scope_keys"], snapshot.scope_key)
            _append_unique(entry["source_snapshot_ids"], snapshot.id)
            _append_unique(entry["campaign_ids"], int(row.get("campaign_id") or 0) or None)
            _append_unique(entry["campaign_names"], str(row.get("campaign_name") or "").strip())
            _append_unique(entry["ad_group_names"], str(row.get("ad_group_name") or "").strip())
            if date_range and date_range not in entry["date_ranges"]:
                entry["date_ranges"].append(date_range)
            entry["impressions"] += impressions
            entry["clicks"] += clicks
            entry["cost"] += metric_value(row, "cost")
            entry["conversions"] += conversions
            entry["conversion_value"] += metric_value(row, "conversion_value")
            entry["all_conversions"] += all_conversions
            entry["all_conversions_value"] += metric_value(row, "all_conversions_value")
            entry["score"] += keyword_row_score(row)
            if len(entry["source_rows"]) < 12:
                entry["source_rows"].append(
                    {
                        "dataset_key": snapshot.dataset_key,
                        "scope_key": snapshot.scope_key,
                        "campaign_id": row.get("campaign_id"),
                        "campaign_name": row.get("campaign_name"),
                        "ad_group_id": row.get("ad_group_id"),
                        "ad_group_name": row.get("ad_group_name"),
                        "search_term": row.get("search_term") or row.get("category_label") or keyword,
                        "headline": row.get("headline"),
                        "landing_page": row.get("landing_page") or row.get("url") or row.get("expanded_final_url"),
                        "match_source": row.get("match_source") or row.get("source"),
                        "impressions": impressions,
                        "clicks": clicks,
                        "cost": metric_value(row, "cost"),
                        "conversions": conversions,
                        "conversion_value": metric_value(row, "conversion_value"),
                    }
                )

    candidates: list[dict[str, Any]] = []
    for entry in terms.values():
        entry["quality_label"] = _quality_label(entry)
        entry["match_type"] = "exact"
        entry["source_json"] = {
            "source": "google_ads_insight_snapshots",
            "source_job_id": source_job_id,
            "pulled_at": pulled_at.isoformat(),
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


def latest_keyword_snapshots(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
) -> list[GoogleAdsDataSnapshot]:
    query = (
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key.in_(list(KEYWORD_DATASETS)),
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
    latest_by_dataset: dict[str, GoogleAdsDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_dataset.setdefault(snapshot.dataset_key, snapshot)
    return list(latest_by_dataset.values())


def upsert_keyword_candidates(
    session: Session,
    candidates: list[dict[str, Any]],
) -> int:
    if not candidates:
        return 0
    stmt = insert(GoogleAdsKeywordCandidate).values(candidates)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsKeywordCandidate.account_id,
            GoogleAdsKeywordCandidate.normalized_keyword,
        ],
        set_={
            "keyword": excluded.keyword,
            "quality_label": excluded.quality_label,
            "source_dataset_keys": excluded.source_dataset_keys,
            "source_scope_keys": excluded.source_scope_keys,
            "source_snapshot_ids": excluded.source_snapshot_ids,
            "campaign_ids": excluded.campaign_ids,
            "campaign_names": excluded.campaign_names,
            "ad_group_names": excluded.ad_group_names,
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


def sync_account_keyword_candidates(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    pulled_at = utcnow()
    fresh_cutoff = pulled_at - timedelta(hours=24)
    existing_count, existing_last_pulled_at = session.execute(
        select(
            func.count(GoogleAdsKeywordCandidate.id),
            func.max(GoogleAdsKeywordCandidate.last_pulled_at),
        ).where(GoogleAdsKeywordCandidate.account_id == account.id)
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
    snapshots = latest_keyword_snapshots(session, account, scope_key=scope_key)
    candidates = keyword_candidates_from_snapshots(
        account,
        snapshots,
        pulled_at=pulled_at,
        source_job_id=source_job_id,
    )
    snapshot_ids = [snapshot.id for snapshot in snapshots]
    saved = upsert_keyword_candidates(session, candidates)
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


def all_time_scope(start_date: date, end_date: date) -> str:
    return f"all_time:{start_date.isoformat()}:{end_date.isoformat()}"


def all_time_query_search_terms(start_date: date, end_date: date, max_rows: int) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          ad_group.id,
          ad_group.name,
          search_term_view.search_term,
          search_term_view.status,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM search_term_view
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
        ORDER BY metrics.conversions_value DESC, metrics.conversions DESC, metrics.clicks DESC, metrics.impressions DESC
        LIMIT {int(max_rows)}
    """


def _query_campaigns_for_insights(max_rows: int) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type
        FROM campaign
        WHERE campaign.status != REMOVED
        ORDER BY campaign.id
        LIMIT {int(max_rows)}
    """


def _fetch_campaigns_for_insights(client: Any, account: GoogleAdsAccount, *, max_rows: int) -> list[dict[str, Any]]:
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for batch in service.search_stream(
        customer_id=account.customer_id,
        query=_query_campaigns_for_insights(max_rows),
        timeout=GOOGLE_ADS_SEARCH_STREAM_TIMEOUT_SECONDS,
    ):
        for row in batch.results:
            rows.append(
                {
                    "campaign_id": int(row.campaign.id),
                    "campaign_name": str(row.campaign.name or row.campaign.id),
                    "campaign_resource_name": row.campaign.resource_name,
                    "campaign_status": enum_name(row.campaign.status),
                    "channel_type": enum_name(row.campaign.advertising_channel_type),
                }
            )
            if len(rows) >= max_rows:
                return rows
    return rows


def _parse_all_time_search_term_insight(row: Any) -> dict[str, Any]:
    return {
        **campaign_base(row),
        "insight_id": int(row.campaign_search_term_insight.id or 0),
        "category_label": str(row.campaign_search_term_insight.category_label or ""),
        **metric_payload(row.metrics),
    }


def _fetch_search_term_rows(client: Any, account: GoogleAdsAccount, *, start_date: date, end_date: date, max_rows: int) -> tuple[str, list[dict[str, Any]]]:
    query = all_time_query_search_terms(start_date, end_date, max_rows)
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for batch in service.search_stream(
        customer_id=account.customer_id,
        query=query,
        timeout=GOOGLE_ADS_SEARCH_STREAM_TIMEOUT_SECONDS,
    ):
        for row in batch.results:
            rows.append(parse_search_term(row))
            if len(rows) >= max_rows:
                return query, rows
    return query, rows


def _fetch_search_term_insight_rows(
    client: Any,
    account: GoogleAdsAccount,
    *,
    start_date: date,
    end_date: date,
    max_rows: int,
) -> tuple[str, list[dict[str, Any]], int]:
    campaigns = _fetch_campaigns_for_insights(client, account, max_rows=ALL_TIME_INSIGHT_CAMPAIGN_LIMIT)
    query_marker = search_term_insight_loop_query(start_date, end_date, max_rows)
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    per_campaign_limit = min(
        max(int(max_rows or DEFAULT_RESEARCH_MAX_ROWS), 50),
        ALL_TIME_INSIGHT_PER_CAMPAIGN_LIMIT,
    )
    for campaign in campaigns:
        query = query_search_term_insights_for_campaign(
            start_date,
            end_date,
            per_campaign_limit,
            int(campaign["campaign_id"]),
        )
        try:
            stream = service.search_stream(
                customer_id=account.customer_id,
                query=query,
                timeout=GOOGLE_ADS_SEARCH_STREAM_TIMEOUT_SECONDS,
            )
            for batch in stream:
                for row in batch.results:
                    rows.append(_parse_all_time_search_term_insight(row))
                    if len(rows) >= max_rows:
                        return query_marker, rows, len(campaigns)
        except Exception:
            continue
    return query_marker, rows, len(campaigns)


def _snapshot_payload(
    account: GoogleAdsAccount,
    *,
    dataset_key: str,
    title: str,
    rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    max_rows: int,
    notes: list[str],
) -> dict[str, Any]:
    return {
        "account": {
            "id": account.id,
            "name": account.name,
            "customer_id": account.customer_id,
            "currency_code": account.currency_code,
        },
        "dataset_key": dataset_key,
        "title": title,
        "date_range": {
            "mode": "all_time",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "rows": rows,
        "campaigns": rows_by_campaign(rows),
        "row_limit": max_rows,
        "notes": notes,
        "fetched_at": utcnow().isoformat(),
    }


def sync_account_all_time_keyword_candidates(
    session: Session,
    account: GoogleAdsAccount,
    *,
    start_date: date = ALL_TIME_START_DATE,
    end_date: Optional[date] = None,
    max_rows: int = 100_000,
    force: bool = False,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    end_date = end_date or date.today()
    max_rows = min(max(int(max_rows or 100_000), 50), 250_000)
    scope_key = all_time_scope(start_date, end_date)
    expires_at = utcnow() + timedelta(hours=ALL_TIME_SNAPSHOT_TTL_HOURS)
    values = get_sync_setting_map(session)
    client = build_client(values, account.manager_customer_id, account.connection)
    datasets: list[dict[str, Any]] = []

    search_query = all_time_query_search_terms(start_date, end_date, max_rows)
    search_snapshot = None if force else get_fresh_snapshot(
        session,
        dataset_key=DATASET_SEARCH_TERMS,
        account=account,
        scope_key=scope_key,
        query=search_query,
    )
    if search_snapshot is not None:
        datasets.append(
            {
                "dataset_key": DATASET_SEARCH_TERMS,
                "status": "cached",
                "rows": search_snapshot.row_count,
                "snapshot_id": search_snapshot.id,
            }
        )
    else:
        search_query, search_rows = _fetch_search_term_rows(
            client,
            account,
            start_date=start_date,
            end_date=end_date,
            max_rows=max_rows,
        )
        upsert_snapshot(
            session,
            dataset_key=DATASET_SEARCH_TERMS,
            payload=_snapshot_payload(
                account,
                dataset_key=DATASET_SEARCH_TERMS,
                title="All-time search terms",
                rows=search_rows,
                start_date=start_date,
                end_date=end_date,
                max_rows=max_rows,
                notes=[
                    "All available search-term rows from Google Ads API for the requested custom all-time date range.",
                    "Rows are stored in Postgres and deduped into the keyword bank by account and normalized keyword.",
                ],
            ),
            scope_key=scope_key,
            query=search_query,
            account=account,
            expires_at=expires_at,
            source_job_id=source_job_id,
            row_count=len(search_rows),
        )
        session.commit()
        datasets.append({"dataset_key": DATASET_SEARCH_TERMS, "status": "fetched", "rows": len(search_rows)})

    insight_query = search_term_insight_loop_query(start_date, end_date, max_rows)
    insight_snapshot = None if force else get_fresh_snapshot(
        session,
        dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
        account=account,
        scope_key=scope_key,
        query=insight_query,
    )
    if insight_snapshot is not None:
        datasets.append(
            {
                "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                "status": "cached",
                "rows": insight_snapshot.row_count,
                "snapshot_id": insight_snapshot.id,
            }
        )
    else:
        insight_query, insight_rows, campaign_count = _fetch_search_term_insight_rows(
            client,
            account,
            start_date=start_date,
            end_date=end_date,
            max_rows=max_rows,
        )
        upsert_snapshot(
            session,
            dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
            payload=_snapshot_payload(
                account,
                dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
                title="All-time search term insights",
                rows=insight_rows,
                start_date=start_date,
                end_date=end_date,
                max_rows=max_rows,
                notes=[
                    f"Fetched by looping {campaign_count} non-removed campaigns to avoid Google Ads API per-campaign insight gaps.",
                    "Google's 2025 search-term-insight API update removes subcategory detail; category labels are stored when exposed.",
                ],
            ),
            scope_key=scope_key,
            query=insight_query,
            account=account,
            expires_at=expires_at,
            source_job_id=source_job_id,
            row_count=len(insight_rows),
        )
        session.commit()
        datasets.append({"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "status": "fetched", "rows": len(insight_rows)})

    keyword_result = sync_account_keyword_candidates(
        session,
        account,
        scope_key=scope_key,
        source_job_id=source_job_id,
    )
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "scope_key": scope_key,
        "datasets": datasets,
        "keyword_result": keyword_result,
    }
