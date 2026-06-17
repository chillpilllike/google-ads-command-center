from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import (
    CampaignOptimizationRun,
    GoogleAdsAccount,
    GoogleAdsDataSnapshot,
    OdooProductPageSignal,
)
from app.services.currency_rates import convert_amount, get_latest_rate_snapshot_sync, snapshot_payload
from app.services.google_ads_api_errors import record_google_ads_api_error, record_google_ads_generic_error
from app.services.google_ads_research_collector import (
    parse_campaign_insight,
    parse_geo_segment,
    parse_landing_page,
    parse_search_term,
    parse_search_term_insight,
    parse_time_segment,
    query_search_term_insights_for_campaign,
    rows_by_campaign,
)
from app.services.google_ads_snapshot_store import (
    DATASET_AUCTION_INSIGHTS_PROXY,
    DATASET_CAMPAIGN_INSIGHTS,
    DATASET_GEO_SEGMENTS,
    DATASET_LANDING_PAGES,
    DATASET_SEARCH_TERM_INSIGHTS,
    DATASET_SEARCH_TERMS,
    DATASET_TIME_SEGMENTS,
    get_fresh_snapshot,
    query_hash,
    upsert_snapshot,
)
from app.services.google_ads_sync import build_client
from app.services.spend_guard import (
    clean_customer_id,
    priority_customer_ids,
    sales_and_margin_inr_for_account,
)


OPTIMIZER_DATASET_TTL_HOURS = 24
DEFAULT_OPTIMIZER_DAYS = 30
DEFAULT_OPTIMIZER_MAX_ROWS = 3000
KNOWN_CAMPAIGN_TYPES = {"all", "pmax", "dsa", "search"}
GOOGLE_MUTATION_ACTIONS = {
    "reduce_budget",
    "increase_budget",
    "raise_target_roas",
    "lower_target_roas",
    "add_negative_exact_terms",
    "add_search_exact_winners",
    "add_pmax_search_themes",
    "schedule_focus",
    "device_reduce",
    "geo_reduce",
}
LOCAL_FEED_ACTIONS = {"promote_page_feed_urls", "exclude_page_feed_urls"}
KNOWN_ACTION_TYPES = GOOGLE_MUTATION_ACTIONS | LOCAL_FEED_ACTIONS | {
    "fix_conversion_value_tracking",
    "split_best_products_campaign",
    "hold_collect_more_data",
    "manual_review",
}


@dataclass(frozen=True)
class OptimizerDatasetSpec:
    dataset_key: str
    title: str
    query_builder: Callable[[date, date, int, str], str]
    row_parser: Callable[[Any], dict[str, Any]]
    needs_explicit_campaign_for_pmax: bool = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _num(values: dict[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _date_window(days: int) -> tuple[date, date]:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(int(days or DEFAULT_OPTIMIZER_DAYS), 1) - 1)
    return start_date, end_date


def _scope_key(days: int, start_date: date, end_date: date, campaign_type: str, campaign_id: Optional[int]) -> str:
    campaign_part = str(int(campaign_id)) if campaign_id else "all"
    return f"optimizer:last_{int(days)}d:{start_date.isoformat()}:{end_date.isoformat()}:type:{campaign_type}:campaign:{campaign_part}"


def _expires_at(end_date: date) -> datetime | None:
    if end_date >= date.today():
        return utcnow() + timedelta(hours=OPTIMIZER_DATASET_TTL_HOURS)
    return None


def normalize_campaign_type(value: str) -> str:
    value = str(value or "all").strip().lower()
    if value in {"performance_max", "performance max", "p-max"}:
        return "pmax"
    if value in {"dynamic", "dynamic_search", "dynamic search", "dynamic search ads"}:
        return "dsa"
    return value if value in KNOWN_CAMPAIGN_TYPES else "all"


def _campaign_filter(campaign_type: str, campaign_id: Optional[int]) -> str:
    if campaign_id:
        return f"AND campaign.id = {int(campaign_id)}"
    if campaign_type == "pmax":
        return "AND campaign.advertising_channel_type = PERFORMANCE_MAX"
    if campaign_type in {"dsa", "search"}:
        return "AND campaign.advertising_channel_type = SEARCH"
    return ""


def _campaign_insights_query(start_date: date, end_date: date, max_rows: int, campaign_filter: str) -> str:
    return f"""
        SELECT
          customer.currency_code,
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversion_value.target_roas,
          campaign.maximize_conversions.target_cpa_micros,
          campaign_budget.amount_micros,
          campaign_budget.explicitly_shared,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value,
          metrics.search_impression_share,
          metrics.search_budget_lost_impression_share,
          metrics.search_rank_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def _search_terms_query(start_date: date, end_date: date, max_rows: int, campaign_filter: str) -> str:
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
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def _landing_pages_query(start_date: date, end_date: date, max_rows: int, campaign_filter: str) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          expanded_landing_page_view.expanded_final_url,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM expanded_landing_page_view
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def _time_segments_query(start_date: date, end_date: date, max_rows: int, campaign_filter: str) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          segments.day_of_week,
          segments.hour,
          segments.device,
          segments.ad_network_type,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def _geo_segments_query(start_date: date, end_date: date, max_rows: int, campaign_filter: str) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          geographic_view.country_criterion_id,
          geographic_view.location_type,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM geographic_view
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
          {campaign_filter}
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


OPTIMIZER_DATASETS = [
    OptimizerDatasetSpec(DATASET_CAMPAIGN_INSIGHTS, "Campaign insights", _campaign_insights_query, parse_campaign_insight),
    OptimizerDatasetSpec(DATASET_SEARCH_TERMS, "Search terms", _search_terms_query, parse_search_term),
    OptimizerDatasetSpec(DATASET_LANDING_PAGES, "Landing pages", _landing_pages_query, parse_landing_page),
    OptimizerDatasetSpec(DATASET_TIME_SEGMENTS, "When ads showed", _time_segments_query, parse_time_segment),
    OptimizerDatasetSpec(DATASET_GEO_SEGMENTS, "Where ads showed", _geo_segments_query, parse_geo_segment),
]


def _fetch_query_rows(client: GoogleAdsClient, account: GoogleAdsAccount, query: str, parser: Callable[[Any], dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for batch in service.search_stream(customer_id=account.customer_id, query=query):
        for row in batch.results:
            rows.append(parser(row))
            if len(rows) >= max_rows:
                return rows
    return rows


def _latest_exact_snapshot(
    session: Session,
    account: GoogleAdsAccount,
    dataset_key: str,
    scope_key: str,
    query: str,
) -> GoogleAdsDataSnapshot | None:
    return session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == dataset_key,
            GoogleAdsDataSnapshot.scope_key == scope_key,
            GoogleAdsDataSnapshot.query_hash == query_hash(query),
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )


def _campaign_type_matches(row: dict[str, Any], campaign_type: str) -> bool:
    if campaign_type == "all":
        return True
    channel = str(row.get("channel_type") or "").upper()
    name = str(row.get("campaign_name") or "").lower()
    if campaign_type == "pmax":
        return channel == "PERFORMANCE_MAX"
    if campaign_type == "search":
        return channel == "SEARCH"
    if campaign_type == "dsa":
        return channel == "SEARCH" and ("dynamic" in name or "dsa" in name)
    return True


def _filter_rows(rows: list[dict[str, Any]], campaign_type: str, campaign_id: Optional[int]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if campaign_id and int(row.get("campaign_id") or 0) != int(campaign_id):
            continue
        if not campaign_id and not _campaign_type_matches(row, campaign_type):
            continue
        filtered.append(row)
    return filtered


def _fallback_snapshot(
    session: Session,
    account: GoogleAdsAccount,
    dataset_key: str,
    campaign_type: str,
    campaign_id: Optional[int],
) -> tuple[GoogleAdsDataSnapshot | None, dict[str, Any] | None]:
    snapshots = session.scalars(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == dataset_key,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(10)
    ).all()
    for snapshot in snapshots:
        payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            continue
        filtered = _filter_rows([row for row in rows if isinstance(row, dict)], campaign_type, campaign_id)
        if not filtered:
            continue
        fallback_payload = dict(payload)
        fallback_payload["rows"] = filtered
        fallback_payload["campaigns"] = rows_by_campaign(filtered)
        fallback_payload["fallback_source"] = {
            "snapshot_id": snapshot.id,
            "scope_key": snapshot.scope_key,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
        }
        return snapshot, fallback_payload
    return None, None


def _snapshot_payload(
    *,
    account: GoogleAdsAccount,
    dataset_key: str,
    title: str,
    days: int,
    start_date: date,
    end_date: date,
    rows: list[dict[str, Any]],
    max_rows: int,
    notes: Optional[list[str]] = None,
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
            "days": days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "rows": rows,
        "campaigns": rows_by_campaign(rows),
        "row_limit": max_rows,
        "notes": notes or [
            "Campaign-scoped optimizer snapshot stored in Postgres JSON for reuse without repeat Google Ads calls.",
        ],
        "fetched_at": utcnow().isoformat(),
    }


def load_or_fetch_optimizer_snapshots(
    session: Session,
    account: GoogleAdsAccount,
    *,
    campaign_type: str,
    campaign_id: Optional[int],
    days: int,
    max_rows: int,
    force: bool = False,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    campaign_type = normalize_campaign_type(campaign_type)
    days = min(max(int(days or DEFAULT_OPTIMIZER_DAYS), 1), 90)
    max_rows = min(max(int(max_rows or DEFAULT_OPTIMIZER_MAX_ROWS), 50), 20_000)
    start_date, end_date = _date_window(days)
    scope_key = _scope_key(days, start_date, end_date, campaign_type, campaign_id)
    campaign_filter = _campaign_filter(campaign_type, campaign_id)
    expires_at = _expires_at(end_date)
    settings = get_sync_setting_map(session)
    client: GoogleAdsClient | None = None
    payloads: dict[str, dict[str, Any]] = {}
    snapshots: list[int] = []
    statuses: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    api_queries_attempted = 0

    for spec in OPTIMIZER_DATASETS:
        query = spec.query_builder(start_date, end_date, max_rows, campaign_filter)
        cached = None if force else get_fresh_snapshot(
            session,
            dataset_key=spec.dataset_key,
            account=account,
            scope_key=scope_key,
            query=query,
        )
        if cached is not None and isinstance(cached.payload_json, dict):
            payloads[spec.dataset_key] = cached.payload_json
            snapshots.append(cached.id)
            statuses.append(
                {
                    "dataset_key": spec.dataset_key,
                    "title": spec.title,
                    "status": "cached",
                    "rows": cached.row_count,
                    "snapshot_id": cached.id,
                }
            )
            continue
        try:
            client = client or build_client(settings, account.manager_customer_id, account.connection)
            api_queries_attempted += 1
            rows = _fetch_query_rows(client, account, query, spec.row_parser, max_rows)
            payload = _snapshot_payload(
                account=account,
                dataset_key=spec.dataset_key,
                title=spec.title,
                days=days,
                start_date=start_date,
                end_date=end_date,
                rows=rows,
                max_rows=max_rows,
            )
            upsert_snapshot(
                session,
                dataset_key=spec.dataset_key,
                payload=payload,
                scope_key=scope_key,
                query=query,
                account=account,
                expires_at=expires_at,
                source_job_id=source_job_id,
                row_count=len(rows),
            )
            session.commit()
            saved = _latest_exact_snapshot(session, account, spec.dataset_key, scope_key, query)
            if saved is not None:
                snapshots.append(saved.id)
            payloads[spec.dataset_key] = payload
            statuses.append(
                {
                    "dataset_key": spec.dataset_key,
                    "title": spec.title,
                    "status": "fetched",
                    "rows": len(rows),
                    "snapshot_id": saved.id if saved else None,
                }
            )
        except GoogleAdsException as exc:
            session.rollback()
            record_google_ads_api_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="campaign_optimization_sync",
                severity="manual_action_required",
                extra={
                    "dataset_key": spec.dataset_key,
                    "campaign_type": campaign_type,
                    "campaign_id": campaign_id,
                    "days": days,
                    "manual_action": "Use saved optimizer output or make the recommended Google Ads changes manually if quota blocks the API.",
                },
            )
            fallback, fallback_payload = _fallback_snapshot(session, account, spec.dataset_key, campaign_type, campaign_id)
            if fallback_payload:
                payloads[spec.dataset_key] = fallback_payload
                snapshots.append(fallback.id)
                statuses.append(
                    {
                        "dataset_key": spec.dataset_key,
                        "title": spec.title,
                        "status": "stale_fallback",
                        "rows": len(fallback_payload.get("rows") or []),
                        "snapshot_id": fallback.id,
                    }
                )
            else:
                statuses.append({"dataset_key": spec.dataset_key, "title": spec.title, "status": "failed", "rows": 0})
            errors.append({"dataset_key": spec.dataset_key, "error": str(exc)[:500]})
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            record_google_ads_generic_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="campaign_optimization_sync",
                severity="manual_action_required",
                extra={
                    "dataset_key": spec.dataset_key,
                    "campaign_type": campaign_type,
                    "campaign_id": campaign_id,
                    "days": days,
                    "manual_action": "Use saved optimizer output or make the recommended Google Ads changes manually if quota blocks the API.",
                },
            )
            fallback, fallback_payload = _fallback_snapshot(session, account, spec.dataset_key, campaign_type, campaign_id)
            if fallback_payload:
                payloads[spec.dataset_key] = fallback_payload
                snapshots.append(fallback.id)
                statuses.append(
                    {
                        "dataset_key": spec.dataset_key,
                        "title": spec.title,
                        "status": "stale_fallback",
                        "rows": len(fallback_payload.get("rows") or []),
                        "snapshot_id": fallback.id,
                    }
                )
            else:
                statuses.append({"dataset_key": spec.dataset_key, "title": spec.title, "status": "failed", "rows": 0})
            errors.append({"dataset_key": spec.dataset_key, "error": str(exc)[:500]})

    campaign_rows = payloads.get(DATASET_CAMPAIGN_INSIGHTS, {}).get("rows") or []
    explicit_insight_campaign_id = campaign_id
    if not explicit_insight_campaign_id and len(campaign_rows) == 1:
        explicit_insight_campaign_id = int(campaign_rows[0].get("campaign_id") or 0) or None
    if explicit_insight_campaign_id:
        query = query_search_term_insights_for_campaign(start_date, end_date, max_rows, explicit_insight_campaign_id)
        cached = None if force else get_fresh_snapshot(
            session,
            dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
            account=account,
            scope_key=scope_key,
            query=query,
        )
        if cached is not None and isinstance(cached.payload_json, dict):
            payloads[DATASET_SEARCH_TERM_INSIGHTS] = cached.payload_json
            snapshots.append(cached.id)
            statuses.append(
                {
                    "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                    "title": "Search term insights",
                    "status": "cached",
                    "rows": cached.row_count,
                    "snapshot_id": cached.id,
                }
            )
        else:
            try:
                client = client or build_client(settings, account.manager_customer_id, account.connection)
                api_queries_attempted += 1
                rows = _fetch_query_rows(client, account, query, parse_search_term_insight, max_rows)
                payload = _snapshot_payload(
                    account=account,
                    dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
                    title="Search term insights",
                    days=days,
                    start_date=start_date,
                    end_date=end_date,
                    rows=rows,
                    max_rows=max_rows,
                )
                upsert_snapshot(
                    session,
                    dataset_key=DATASET_SEARCH_TERM_INSIGHTS,
                    payload=payload,
                    scope_key=scope_key,
                    query=query,
                    account=account,
                    expires_at=expires_at,
                    source_job_id=source_job_id,
                    row_count=len(rows),
                )
                session.commit()
                saved = _latest_exact_snapshot(session, account, DATASET_SEARCH_TERM_INSIGHTS, scope_key, query)
                if saved is not None:
                    snapshots.append(saved.id)
                payloads[DATASET_SEARCH_TERM_INSIGHTS] = payload
                statuses.append(
                    {
                        "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                        "title": "Search term insights",
                        "status": "fetched",
                        "rows": len(rows),
                        "snapshot_id": saved.id if saved else None,
                    }
                )
            except GoogleAdsException as exc:
                session.rollback()
                record_google_ads_api_error(
                    session,
                    exc,
                    account=account,
                    job_id=source_job_id,
                    context="campaign_optimization_sync",
                    severity="manual_action_required",
                    extra={"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "campaign_id": explicit_insight_campaign_id},
                )
                fallback, fallback_payload = _fallback_snapshot(
                    session,
                    account,
                    DATASET_SEARCH_TERM_INSIGHTS,
                    campaign_type,
                    explicit_insight_campaign_id,
                )
                if fallback_payload:
                    payloads[DATASET_SEARCH_TERM_INSIGHTS] = fallback_payload
                    snapshots.append(fallback.id)
                    statuses.append(
                        {
                            "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                            "title": "Search term insights",
                            "status": "stale_fallback",
                            "rows": len(fallback_payload.get("rows") or []),
                            "snapshot_id": fallback.id,
                        }
                    )
                else:
                    statuses.append({"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "title": "Search term insights", "status": "failed", "rows": 0})
                errors.append({"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "error": str(exc)[:500]})
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                record_google_ads_generic_error(
                    session,
                    exc,
                    account=account,
                    job_id=source_job_id,
                    context="campaign_optimization_sync",
                    severity="manual_action_required",
                    extra={"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "campaign_id": explicit_insight_campaign_id},
                )
                fallback, fallback_payload = _fallback_snapshot(
                    session,
                    account,
                    DATASET_SEARCH_TERM_INSIGHTS,
                    campaign_type,
                    explicit_insight_campaign_id,
                )
                if fallback_payload:
                    payloads[DATASET_SEARCH_TERM_INSIGHTS] = fallback_payload
                    snapshots.append(fallback.id)
                    statuses.append(
                        {
                            "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                            "title": "Search term insights",
                            "status": "stale_fallback",
                            "rows": len(fallback_payload.get("rows") or []),
                            "snapshot_id": fallback.id,
                        }
                    )
                else:
                    statuses.append({"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "title": "Search term insights", "status": "failed", "rows": 0})
                errors.append({"dataset_key": DATASET_SEARCH_TERM_INSIGHTS, "error": str(exc)[:500]})
    else:
        statuses.append(
            {
                "dataset_key": DATASET_SEARCH_TERM_INSIGHTS,
                "title": "Search term insights",
                "status": "skipped",
                "rows": 0,
                "reason": "Skipped broad search-term-insight loop to protect API quota. Enter a campaign ID for PMax search categories.",
            }
        )

    auction_rows = [
        {
            "note": "Derived from campaign insights; no extra Google Ads API query was used.",
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "channel_type": row.get("channel_type"),
            "cost": row.get("cost"),
            "impressions": row.get("impressions"),
            "clicks": row.get("clicks"),
            "conversions": row.get("conversions"),
            "conversion_value": row.get("conversion_value"),
            "search_impression_share": row.get("search_impression_share"),
            "search_budget_lost_impression_share": row.get("search_budget_lost_impression_share"),
            "search_rank_lost_impression_share": row.get("search_rank_lost_impression_share"),
        }
        for row in campaign_rows
        if isinstance(row, dict)
    ]
    payloads[DATASET_AUCTION_INSIGHTS_PROXY] = _snapshot_payload(
        account=account,
        dataset_key=DATASET_AUCTION_INSIGHTS_PROXY,
        title="Auction insights proxy",
        days=days,
        start_date=start_date,
        end_date=end_date,
        rows=auction_rows,
        max_rows=max_rows,
        notes=["Derived from campaign insights to avoid a duplicate API query."],
    )
    statuses.append(
        {
            "dataset_key": DATASET_AUCTION_INSIGHTS_PROXY,
            "title": "Auction insights proxy",
            "status": "derived",
            "rows": len(auction_rows),
        }
    )

    return {
        "scope_key": scope_key,
        "date_range": {
            "days": days,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        "datasets": payloads,
        "statuses": statuses,
        "errors": errors,
        "source_snapshot_ids": list(dict.fromkeys(snapshots)),
        "api_queries_attempted": api_queries_attempted,
    }


def _metric_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = {
        "cost": 0.0,
        "impressions": 0,
        "clicks": 0,
        "conversions": 0.0,
        "conversion_value": 0.0,
    }
    for row in rows:
        total["cost"] += float(row.get("cost") or 0)
        total["impressions"] += int(row.get("impressions") or 0)
        total["clicks"] += int(row.get("clicks") or 0)
        total["conversions"] += float(row.get("conversions") or 0)
        total["conversion_value"] += float(row.get("conversion_value") or 0)
    cost = float(total["cost"] or 0)
    conversions = float(total["conversions"] or 0)
    total["roas"] = (float(total["conversion_value"] or 0) / cost) if cost else None
    total["cost_per_conversion"] = (cost / conversions) if conversions else None
    total["ctr"] = (int(total["clicks"] or 0) / int(total["impressions"] or 0)) if int(total["impressions"] or 0) else 0.0
    return total


def _sort_metric_rows(rows: list[dict[str, Any]], *, reverse: bool = True) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (float(row.get("cost") or 0), float(row.get("conversion_value") or 0)), reverse=reverse)


def _is_homepage(url: str) -> bool:
    url = str(url or "").strip().lower().split("?", 1)[0].rstrip("/")
    if not url:
        return False
    match = re.match(r"^https?://[^/]+/?$", url)
    return bool(match)


def _action(
    *,
    action_type: str,
    priority: str,
    title: str,
    summary: str,
    campaign: dict[str, Any] | None = None,
    apply_mode: str = "manual_review",
    confidence: float = 0.7,
    field: str = "",
    old_value: Any = None,
    new_value: Any = None,
    change_pct: Optional[float] = None,
    items: Optional[list[Any]] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    campaign = campaign or {}
    return {
        "action_type": action_type,
        "priority": priority,
        "title": title,
        "summary": summary,
        "campaign_id": campaign.get("campaign_id"),
        "campaign_name": campaign.get("campaign_name"),
        "channel_type": campaign.get("channel_type"),
        "apply_mode": apply_mode,
        "google_mutation_required": action_type in GOOGLE_MUTATION_ACTIONS,
        "local_only": action_type in LOCAL_FEED_ACTIONS,
        "confidence": round(max(min(float(confidence or 0), 1.0), 0.0), 2),
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "change_pct": change_pct,
        "items": items or [],
        "evidence": evidence or {},
    }


def margin_guard_for_optimizer(
    session: Session,
    account: GoogleAdsAccount,
    settings: dict[str, Any],
    *,
    cost_by_currency: dict[str, float],
    days: int,
) -> dict[str, Any]:
    rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    sales_inr, margin_inr, sales_details = sales_and_margin_inr_for_account(
        session,
        account,
        hours=max(int(days or DEFAULT_OPTIMIZER_DAYS), 1) * 24,
        rates=rates,
    )
    ad_cost_inr = 0.0
    cost_details: list[dict[str, Any]] = []
    for currency_code, amount in cost_by_currency.items():
        converted = convert_amount(float(amount or 0), currency_code or "UNKNOWN", "INR", rates)
        if converted is None:
            continue
        ad_cost_inr += converted
        cost_details.append({"currency": currency_code, "raw_cost": float(amount or 0), "cost_inr": converted})
    margin_data_present = any(int(detail.get("margin_order_count") or 0) > 0 for detail in sales_details)
    hard_cap = _num(settings, "spend_guard.margin_hard_cap_ratio", 0.20)
    priority_extra = max(_num(settings, "spend_guard.priority_margin_extra_ratio", 0.10), 0.0)
    priority = clean_customer_id(account.customer_id) in priority_customer_ids(settings)
    basis = margin_inr if margin_data_present else sales_inr
    max_ratio = hard_cap + priority_extra if margin_data_present and priority and basis > 0 else hard_cap
    target_ratio = min(_num(settings, "spend_guard.margin_warning_ratio", 0.16), max_ratio)
    ratio = (ad_cost_inr / basis) if basis > 0 else (max_ratio + 1.0 if ad_cost_inr > 0 else 0.0)
    if ratio <= target_ratio:
        status = "green"
    elif ratio <= max_ratio:
        status = "amber"
    else:
        status = "red"
    return {
        "status": status,
        "basis_mode": "odoo_margin" if margin_data_present else "gross_sales_fallback",
        "basis_inr": basis,
        "sales_inr": sales_inr,
        "margin_inr": margin_inr,
        "ad_cost_inr": ad_cost_inr,
        "spend_ratio": ratio,
        "target_ratio": target_ratio,
        "max_ratio": max_ratio,
        "normal_margin_cap_ratio": hard_cap,
        "priority_margin_extra_ratio": priority_extra,
        "priority_account": priority,
        "priority_extra_applied": bool(priority and margin_data_present and basis > 0),
        "allowed_spend_inr": basis * max_ratio if basis > 0 else 0.0,
        "remaining_spend_room_inr": max((basis * max_ratio) - ad_cost_inr, 0.0) if basis > 0 else 0.0,
        "sales_details": sales_details,
        "cost_details": cost_details,
        "window_days": days,
    }


def _product_signals(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    rows = session.scalars(
        select(OdooProductPageSignal)
        .where(OdooProductPageSignal.account_id == account.id)
        .order_by(OdooProductPageSignal.label, OdooProductPageSignal.margin_amount.desc())
        .limit(500)
    ).all()
    by_url = {str(row.product_url or "").split("?", 1)[0].rstrip("/"): row for row in rows if row.product_url}
    winners = [row for row in rows if row.label == "winner"]
    watch = [row for row in rows if row.label == "watch"]
    excluded = [row for row in rows if row.label == "exclude"]
    return {
        "rows": rows,
        "by_url": by_url,
        "summary": {
            "winner": len(winners),
            "watch": len(watch),
            "exclude": len(excluded),
        },
        "top_winners": [
            {
                "product_name": row.product_name,
                "product_code": row.product_code,
                "product_url": row.product_url,
                "sales_amount": row.sales_amount,
                "margin_amount": row.margin_amount,
                "margin_percent": row.margin_percent,
                "order_count": row.order_count,
            }
            for row in winners[:20]
        ],
    }


def build_rule_plan(
    *,
    account: GoogleAdsAccount,
    datasets: dict[str, dict[str, Any]],
    settings: dict[str, Any],
    product_signals: dict[str, Any],
    margin_guard: dict[str, Any],
) -> dict[str, Any]:
    campaign_rows = [row for row in datasets.get(DATASET_CAMPAIGN_INSIGHTS, {}).get("rows", []) if isinstance(row, dict)]
    search_rows = [row for row in datasets.get(DATASET_SEARCH_TERMS, {}).get("rows", []) if isinstance(row, dict)]
    landing_rows = [row for row in datasets.get(DATASET_LANDING_PAGES, {}).get("rows", []) if isinstance(row, dict)]
    time_rows = [row for row in datasets.get(DATASET_TIME_SEGMENTS, {}).get("rows", []) if isinstance(row, dict)]
    geo_rows = [row for row in datasets.get(DATASET_GEO_SEGMENTS, {}).get("rows", []) if isinstance(row, dict)]
    insight_rows = [row for row in datasets.get(DATASET_SEARCH_TERM_INSIGHTS, {}).get("rows", []) if isinstance(row, dict)]
    min_cost = _num(settings, "optimizer.min_cost_for_action", 25)
    min_clicks = int(_num(settings, "optimizer.zero_conversion_min_clicks", 10))
    min_conversions = _num(settings, "optimizer.min_conversions_for_bid_change", 5)
    value_ready_conversions = _num(settings, "optimizer.min_conversions_for_value_bidding", 15)
    max_budget_change_pct = min(max(_num(settings, "optimizer.max_budget_change_pct", 0.20), 0.01), 0.50)
    max_roas_change_pct = min(max(_num(settings, "optimizer.max_target_roas_change_pct", 0.10), 0.01), 0.30)
    min_daily_budget = _num(settings, "optimizer.min_daily_budget", 1)
    allow_total_increase = parse_bool(settings.get("optimizer.allow_total_budget_increase", False))
    actions: list[dict[str, Any]] = []
    manual_notes: list[str] = []
    by_url = product_signals.get("by_url") or {}

    total_campaign_metrics = _metric_totals(campaign_rows)
    if margin_guard["status"] == "red":
        manual_notes.append("Margin guard is red. Budget increases are blocked until Odoo margin supports more spend.")
    elif margin_guard["status"] == "amber":
        manual_notes.append("Margin guard is amber. Only proven winner reallocation should be considered.")

    for campaign in campaign_rows:
        cost = float(campaign.get("cost") or 0)
        conversions = float(campaign.get("conversions") or 0)
        conversion_value = float(campaign.get("conversion_value") or 0)
        clicks = int(campaign.get("clicks") or 0)
        roas = (conversion_value / cost) if cost else None
        budget = float(campaign.get("budget_amount") or 0)
        target_roas = campaign.get("target_roas")
        lost_budget = float(campaign.get("search_budget_lost_impression_share") or 0)
        lost_rank = float(campaign.get("search_rank_lost_impression_share") or 0)
        channel = str(campaign.get("channel_type") or "")
        evidence = {
            "cost": cost,
            "clicks": clicks,
            "conversions": conversions,
            "conversion_value": conversion_value,
            "roas": roas,
            "budget": budget,
            "target_roas": target_roas,
            "lost_budget_share": lost_budget,
            "lost_rank_share": lost_rank,
            "margin_guard": margin_guard,
        }
        if conversions > 0 and conversion_value <= 0:
            actions.append(
                _action(
                    action_type="fix_conversion_value_tracking",
                    priority="critical",
                    title="Fix missing conversion value",
                    summary="Campaign has conversions but no primary conversion value. Do not let ChatGPT or bid rules change Target ROAS until purchase value is trustworthy.",
                    campaign=campaign,
                    apply_mode="manual_review",
                    confidence=0.95,
                    evidence=evidence,
                )
            )
            continue
        if margin_guard["status"] == "red" and budget > min_daily_budget:
            new_budget = max(min_daily_budget, budget * (1 - max_budget_change_pct))
            actions.append(
                _action(
                    action_type="reduce_budget",
                    priority="critical",
                    title="Reduce budget under margin cap",
                    summary=f"Ad cost is above the Odoo margin guard. Reduce budget by {max_budget_change_pct * 100:,.0f}% until sales margin catches up.",
                    campaign=campaign,
                    apply_mode="manual_review",
                    field="campaign_budget.amount",
                    old_value=budget,
                    new_value=round(new_budget, 2),
                    change_pct=-max_budget_change_pct,
                    confidence=0.9,
                    evidence=evidence,
                )
            )
        if cost >= min_cost and conversions <= 0 and clicks >= min_clicks and budget > min_daily_budget:
            new_budget = max(min_daily_budget, budget * (1 - max_budget_change_pct))
            actions.append(
                _action(
                    action_type="reduce_budget",
                    priority="high",
                    title="Cut spend with no conversions",
                    summary="Campaign spent enough to act, received clicks, and produced zero conversions in the selected window.",
                    campaign=campaign,
                    apply_mode="manual_review",
                    field="campaign_budget.amount",
                    old_value=budget,
                    new_value=round(new_budget, 2),
                    change_pct=-max_budget_change_pct,
                    confidence=0.85,
                    evidence=evidence,
                )
            )
        if target_roas and conversions >= min_conversions and roas is not None:
            target_roas_float = float(target_roas or 0)
            if roas < 1.5 and cost >= min_cost:
                new_target = target_roas_float * (1 + max_roas_change_pct)
                actions.append(
                    _action(
                        action_type="raise_target_roas",
                        priority="high",
                        title="Tighten Target ROAS",
                        summary="ROAS is weak with enough spend. Raise Target ROAS gradually to slow low-quality traffic.",
                        campaign=campaign,
                        apply_mode="manual_review",
                        field="campaign.maximize_conversion_value.target_roas",
                        old_value=target_roas_float,
                        new_value=round(new_target, 4),
                        change_pct=max_roas_change_pct,
                        confidence=0.78,
                        evidence=evidence,
                    )
                )
            elif roas >= 3.0 and conversions >= value_ready_conversions and margin_guard["status"] == "green":
                new_target = target_roas_float * (1 - (max_roas_change_pct / 2))
                actions.append(
                    _action(
                        action_type="lower_target_roas",
                        priority="medium",
                        title="Loosen Target ROAS for proven winner",
                        summary="Campaign has strong ROAS and conversion volume. Lower Target ROAS slightly to gain more eligible conversions without a large budget jump.",
                        campaign=campaign,
                        apply_mode="manual_review",
                        field="campaign.maximize_conversion_value.target_roas",
                        old_value=target_roas_float,
                        new_value=round(new_target, 4),
                        change_pct=-(max_roas_change_pct / 2),
                        confidence=0.7,
                        evidence=evidence,
                    )
                )
        if roas is not None and roas >= 3.0 and conversions >= value_ready_conversions and lost_budget > 0.10:
            if margin_guard["status"] == "green" and (allow_total_increase or margin_guard["remaining_spend_room_inr"] > 0):
                actions.append(
                    _action(
                        action_type="increase_budget",
                        priority="medium",
                        title="Scale proven winner carefully",
                        summary=f"Strong ROAS with budget-lost impression share. Increase by no more than {max_budget_change_pct * 100:,.0f}% or reallocate from loser campaigns.",
                        campaign=campaign,
                        apply_mode="manual_review",
                        field="campaign_budget.amount",
                        old_value=budget,
                        new_value=round(budget * (1 + max_budget_change_pct), 2) if budget else None,
                        change_pct=max_budget_change_pct,
                        confidence=0.72,
                        evidence=evidence,
                    )
                )
            else:
                actions.append(
                    _action(
                        action_type="manual_review",
                        priority="medium",
                        title="Winner needs budget room",
                        summary="Campaign looks scalable, but margin guard or total-budget settings block automatic budget increase. Reallocate from waste first.",
                        campaign=campaign,
                        apply_mode="manual_review",
                        confidence=0.75,
                        evidence=evidence,
                    )
                )
        if channel == "PERFORMANCE_MAX" and product_signals["summary"].get("winner", 0):
            actions.append(
                _action(
                    action_type="split_best_products_campaign",
                    priority="medium",
                    title="Use best-product page feed",
                    summary="Create or keep a separate PMax/DSA campaign constrained to Odoo winner product URLs and high-margin labels.",
                    campaign=campaign,
                    apply_mode="manual_review",
                    items=[item["product_url"] for item in product_signals.get("top_winners", [])[:10]],
                    confidence=0.8,
                    evidence={"product_signal_summary": product_signals["summary"], "margin_guard": margin_guard},
                )
            )

    waste_terms = [
        row
        for row in search_rows
        if float(row.get("cost") or 0) >= max(min_cost / 2, 1)
        and float(row.get("conversions") or 0) <= 0
        and int(row.get("clicks") or 0) >= max(2, min_clicks // 2)
        and str(row.get("search_term") or "").strip()
    ]
    if waste_terms:
        terms = []
        for row in _sort_metric_rows(waste_terms)[:25]:
            term = str(row.get("search_term") or "").strip()
            if term and term.lower() not in {item.lower() for item in terms}:
                terms.append(term)
        campaign = waste_terms[0] if waste_terms else {}
        actions.append(
            _action(
                action_type="add_negative_exact_terms",
                priority="high",
                title="Add exact negatives for waste terms",
                summary="These search terms consumed spend with no conversion in the selected window.",
                campaign=campaign,
                apply_mode="manual_review",
                items=terms[:20],
                confidence=0.82,
                evidence={"top_waste_terms": waste_terms[:20], "threshold_cost": max(min_cost / 2, 1)},
            )
        )

    winner_terms = [
        row
        for row in search_rows
        if str(row.get("channel_type") or "").upper() != "PERFORMANCE_MAX"
        if float(row.get("conversions") or 0) >= 2
        and float(row.get("cost") or 0) > 0
        and (float(row.get("conversion_value") or 0) / float(row.get("cost") or 1)) >= 3
        and str(row.get("search_term") or "").strip()
    ]
    if winner_terms:
        terms = []
        for row in sorted(winner_terms, key=lambda item: float(item.get("conversions") or 0), reverse=True)[:25]:
            term = str(row.get("search_term") or "").strip()
            if term and term.lower() not in {item.lower() for item in terms}:
                terms.append(term)
        actions.append(
            _action(
                action_type="add_search_exact_winners",
                priority="medium",
                title="Build Search/RSA exact clusters",
                summary="Move proven converting search terms into tight Search/RSA exact-match ad groups tied to matching Odoo winner pages. PMax uses search themes instead.",
                campaign=winner_terms[0],
                apply_mode="manual_review",
                items=terms[:20],
                confidence=0.84,
                evidence={"top_winner_terms": winner_terms[:20]},
            )
        )

    waste_urls = []
    winner_urls = []
    for row in landing_rows:
        cost = float(row.get("cost") or 0)
        conversions = float(row.get("conversions") or 0)
        value = float(row.get("conversion_value") or 0)
        url = str(row.get("url") or row.get("expanded_final_url") or "").split("?", 1)[0].rstrip("/")
        if not url:
            continue
        if cost >= max(min_cost / 2, 1) and conversions <= 0:
            waste_urls.append({**row, "clean_url": url, "is_homepage": _is_homepage(url)})
        if conversions >= 2 and cost > 0 and value / cost >= 3:
            winner_urls.append({**row, "clean_url": url, "odoo_label": getattr(by_url.get(url), "label", "")})
    if waste_urls:
        actions.append(
            _action(
                action_type="exclude_page_feed_urls",
                priority="high",
                title="Exclude waste landing pages from feeds",
                summary="These URLs spent with no conversions. Exclude matched product URLs locally and avoid homepage/category pages in DSA/PMax feeds.",
                campaign=waste_urls[0],
                apply_mode="local_only",
                items=[row["clean_url"] for row in _sort_metric_rows(waste_urls)[:25]],
                confidence=0.8,
                evidence={"top_waste_urls": waste_urls[:20]},
            )
        )
    if winner_urls:
        actions.append(
            _action(
                action_type="promote_page_feed_urls",
                priority="medium",
                title="Promote converting landing pages",
                summary="These product URLs produced conversion value efficiently. Promote matching Odoo page-feed rows to winner labels.",
                campaign=winner_urls[0],
                apply_mode="local_only",
                items=[row["clean_url"] for row in sorted(winner_urls, key=lambda item: float(item.get("conversions") or 0), reverse=True)[:25]],
                confidence=0.82,
                evidence={"top_winner_urls": winner_urls[:20]},
            )
        )

    hour_rows: dict[str, list[dict[str, Any]]] = {}
    device_rows: dict[str, list[dict[str, Any]]] = {}
    for row in time_rows:
        hour_rows.setdefault(str(row.get("hour")), []).append(row)
        device_rows.setdefault(str(row.get("device") or "UNKNOWN"), []).append(row)
    best_hours = []
    weak_hours = []
    for hour, rows in hour_rows.items():
        metrics = _metric_totals(rows)
        if metrics["cost"] >= max(min_cost / 3, 1) and metrics["conversions"] <= 0:
            weak_hours.append({"hour": hour, **metrics})
        if metrics["cost"] > 0 and metrics["conversions"] >= 1 and (metrics["roas"] or 0) >= 3:
            best_hours.append({"hour": hour, **metrics})
    if best_hours or weak_hours:
        actions.append(
            _action(
                action_type="schedule_focus",
                priority="low",
                title="Focus schedule around proven hours",
                summary="Use best hours for scale and reduce spend in repeated zero-conversion hours after review.",
                campaign=campaign_rows[0] if campaign_rows else {},
                apply_mode="manual_review",
                items={
                    "best_hours": sorted(best_hours, key=lambda item: item.get("roas") or 0, reverse=True)[:8],
                    "weak_hours": sorted(weak_hours, key=lambda item: item.get("cost") or 0, reverse=True)[:8],
                },
                confidence=0.62,
            )
        )
    weak_devices = []
    for device, rows in device_rows.items():
        metrics = _metric_totals(rows)
        if metrics["cost"] >= min_cost and metrics["conversions"] <= 0:
            weak_devices.append({"device": device, **metrics})
    if weak_devices:
        actions.append(
            _action(
                action_type="device_reduce",
                priority="medium",
                title="Reduce weak device traffic",
                summary="These devices spent without conversion. Review campaign type support before applying bid adjustments.",
                campaign=campaign_rows[0] if campaign_rows else {},
                apply_mode="manual_review",
                items=weak_devices[:8],
                confidence=0.66,
            )
        )
    weak_geos = []
    for row in geo_rows:
        if float(row.get("cost") or 0) >= min_cost and float(row.get("conversions") or 0) <= 0:
            weak_geos.append(row)
    if weak_geos:
        actions.append(
            _action(
                action_type="geo_reduce",
                priority="medium",
                title="Review weak location segments",
                summary="These location segments spent without conversion. Exclude or reduce only after confirming shipping/sales coverage.",
                campaign=weak_geos[0],
                apply_mode="manual_review",
                items=weak_geos[:10],
                confidence=0.6,
            )
        )

    pmax_waste_categories = [
        row
        for row in insight_rows
        if int(row.get("clicks") or 0) >= min_clicks and float(row.get("conversions") or 0) <= 0 and str(row.get("category_label") or "").strip()
    ]
    pmax_winner_categories = [
        row
        for row in insight_rows
        if float(row.get("conversions") or 0) > 0 and str(row.get("category_label") or "").strip()
    ]
    if pmax_winner_categories:
        categories = []
        for row in sorted(pmax_winner_categories, key=lambda item: float(item.get("conversions") or 0), reverse=True)[:25]:
            category = str(row.get("category_label") or "").strip()
            if category and category.lower() not in {item.lower() for item in categories}:
                categories.append(category)
        actions.append(
            _action(
                action_type="add_pmax_search_themes",
                priority="medium",
                title="Add PMax search themes from winners",
                summary="Use converting PMax search-term insight categories as search-theme candidates. These are not keywords.",
                campaign=pmax_winner_categories[0],
                apply_mode="manual_review",
                items=categories[:20],
                confidence=0.72,
                evidence={"search_term_insights": pmax_winner_categories[:20]},
            )
        )
    if pmax_waste_categories:
        actions.append(
            _action(
                action_type="manual_review",
                priority="medium",
                title="Review weak PMax search themes",
                summary="PMax search-term insight categories show clicks without conversions. Use these for asset/theme cleanup and account-level negative review.",
                campaign=pmax_waste_categories[0],
                apply_mode="manual_review",
                items=[row.get("category_label") for row in pmax_waste_categories[:15]],
                confidence=0.65,
                evidence={"search_term_insights": pmax_waste_categories[:20]},
            )
        )

    if not actions:
        actions.append(
            _action(
                action_type="hold_collect_more_data",
                priority="low",
                title="Hold and collect more data",
                summary="No strong optimization action crossed the safety thresholds. Keep collecting data and refresh after more conversions or spend.",
                campaign=campaign_rows[0] if campaign_rows else {},
                apply_mode="manual_review",
                confidence=0.75,
                evidence={"campaign_metrics": total_campaign_metrics, "margin_guard": margin_guard},
            )
        )

    return {
        "summary": {
            "account_id": account.id,
            "customer_id": account.customer_id,
            "campaign_count": len(campaign_rows),
            "total_metrics": total_campaign_metrics,
            "margin_guard": margin_guard,
            "product_signal_summary": product_signals.get("summary") or {},
            "manual_notes": manual_notes,
        },
        "actions": actions,
        "source": "rule_engine_v1",
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _compact_rows(rows: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    compact = []
    for row in rows[:limit]:
        compact.append(
            {
                key: row.get(key)
                for key in [
                    "campaign_id",
                    "campaign_name",
                    "channel_type",
                    "search_term",
                    "category_label",
                    "url",
                    "expanded_final_url",
                    "cost",
                    "clicks",
                    "impressions",
                    "conversions",
                    "conversion_value",
                    "roas",
                    "hour",
                    "device",
                    "country_criterion_id",
                    "location_type",
                ]
                if key in row
            }
        )
    return compact


def build_openai_prompt(context: dict[str, Any]) -> str:
    compact_context = {
        "account": context.get("account"),
        "date_range": context.get("date_range"),
        "campaign_type": context.get("campaign_type"),
        "campaign_id": context.get("campaign_id"),
        "margin_guard": context.get("rule_plan", {}).get("summary", {}).get("margin_guard"),
        "product_signal_summary": context.get("rule_plan", {}).get("summary", {}).get("product_signal_summary"),
        "campaigns": _compact_rows(context.get("datasets", {}).get(DATASET_CAMPAIGN_INSIGHTS, {}).get("rows", []), 12),
        "top_search_terms": _compact_rows(_sort_metric_rows(context.get("datasets", {}).get(DATASET_SEARCH_TERMS, {}).get("rows", [])), 20),
        "top_landing_pages": _compact_rows(_sort_metric_rows(context.get("datasets", {}).get(DATASET_LANDING_PAGES, {}).get("rows", [])), 20),
        "top_time_segments": _compact_rows(_sort_metric_rows(context.get("datasets", {}).get(DATASET_TIME_SEGMENTS, {}).get("rows", [])), 20),
        "top_geo_segments": _compact_rows(_sort_metric_rows(context.get("datasets", {}).get(DATASET_GEO_SEGMENTS, {}).get("rows", [])), 15),
        "search_term_insights": _compact_rows(context.get("datasets", {}).get(DATASET_SEARCH_TERM_INSIGHTS, {}).get("rows", []), 20),
        "rule_actions": context.get("rule_plan", {}).get("actions", [])[:40],
        "allowed_action_types": sorted(
            action_type
            for action_type in KNOWN_ACTION_TYPES
            if context.get("allow_local_feed_labels") or action_type not in LOCAL_FEED_ACTIONS
        ),
        "hard_rules": [
            "Never recommend ad spend above the Odoo margin cap.",
            "Normal cap is 20% of Odoo margin. Priority proven accounts may use an extra 10% only when the margin guard says it is available.",
            "Budget or Target ROAS changes must be gradual and must not exceed configured max change percentages.",
            "If Google API quota fails, return manual actions instead of pretending changes were applied.",
            "Keep page-feed additions separate from existing-campaign optimization unless a local feed-label action is explicitly supplied.",
            "Do not add keywords to PMax. For PMax use search theme recommendations only.",
        ],
    }
    return (
        "You are reviewing a Google Ads campaign optimization plan for an operator app. "
        "Return strict JSON only. Do not include markdown. "
        "Use the rule actions and evidence, but improve prioritization, merge duplicates, and add manual cautions. "
        "Do not invent metrics. Do not exceed guardrails. "
        "JSON schema: {summary:string, decision:string, confidence:number, actions:[{action_type:string, priority:string, title:string, summary:string, campaign_id:number|null, campaign_name:string|null, apply_mode:string, change_pct:number|null, items:array, confidence:number}], manual_actions:[string], risks:[string]}. "
        f"Context JSON: {json.dumps(compact_context, sort_keys=True, default=str)[:26000]}"
    )


def _openai_review(settings: dict[str, Any], prompt: str) -> tuple[dict[str, Any], str]:
    if not parse_bool(settings.get("optimizer.ai_enabled", True)):
        return {"status": "disabled", "actions": []}, prompt
    api_key = str(settings.get("openai.api_key") or "").strip()
    if not api_key:
        return {"status": "missing_api_key", "actions": []}, prompt
    model = str(settings.get("openai.model") or "gpt-5.2").strip() or "gpt-5.2"
    payload = {
        "model": model,
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("output_text") or ""
        if not text:
            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        text += content.get("text", "")
        parsed = _extract_json(text)
        parsed["status"] = "reviewed"
        parsed["model"] = model
        return parsed, prompt
    except Exception as exc:  # noqa: BLE001 - deterministic rules are the fallback.
        return {"status": "failed", "error": str(exc)[:500], "actions": []}, prompt


def _sanitize_ai_actions(
    ai_result: dict[str, Any],
    *,
    settings: dict[str, Any],
    margin_guard: dict[str, Any],
) -> list[dict[str, Any]]:
    max_budget_change_pct = min(max(_num(settings, "optimizer.max_budget_change_pct", 0.20), 0.01), 0.50)
    max_roas_change_pct = min(max(_num(settings, "optimizer.max_target_roas_change_pct", 0.10), 0.01), 0.30)
    sanitized: list[dict[str, Any]] = []
    actions = ai_result.get("actions") or []
    if not isinstance(actions, list):
        return []
    for raw in actions:
        if not isinstance(raw, dict):
            continue
        action_type = str(raw.get("action_type") or "").strip()
        if action_type not in KNOWN_ACTION_TYPES:
            continue
        change_pct = raw.get("change_pct")
        try:
            change_pct = float(change_pct) if change_pct is not None else None
        except (TypeError, ValueError):
            change_pct = None
        if action_type in {"increase_budget", "reduce_budget"} and change_pct is not None:
            change_pct = max(min(change_pct, max_budget_change_pct), -max_budget_change_pct)
        if action_type in {"raise_target_roas", "lower_target_roas"} and change_pct is not None:
            change_pct = max(min(change_pct, max_roas_change_pct), -max_roas_change_pct)
        if action_type == "increase_budget" and margin_guard.get("status") != "green":
            raw["apply_mode"] = "manual_review"
            raw["summary"] = (str(raw.get("summary") or "") + " Margin guard is not green, so this cannot be automatic.").strip()
        sanitized.append(
            {
                "action_type": action_type,
                "priority": str(raw.get("priority") or "medium"),
                "title": str(raw.get("title") or action_type.replace("_", " ").title())[:255],
                "summary": str(raw.get("summary") or "")[:1200],
                "campaign_id": raw.get("campaign_id"),
                "campaign_name": raw.get("campaign_name"),
                "apply_mode": str(raw.get("apply_mode") or "manual_review"),
                "google_mutation_required": action_type in GOOGLE_MUTATION_ACTIONS,
                "local_only": action_type in LOCAL_FEED_ACTIONS,
                "change_pct": change_pct,
                "items": raw.get("items") if isinstance(raw.get("items"), list) else [],
                "confidence": round(max(min(float(raw.get("confidence") or 0.6), 1.0), 0.0), 2),
                "source": "openai_review",
            }
        )
    return sanitized


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for action in sorted(actions, key=lambda item: priority_rank.get(str(item.get("priority") or "medium"), 2)):
        items = action.get("items")
        if isinstance(items, list):
            item_key = ",".join(str(item) for item in items[:8])
        else:
            item_key = str(items or "")
        key = f"{action.get('campaign_id')}|{action.get('action_type')}|{action.get('field')}|{item_key}"
        if key in seen:
            continue
        seen.add(key)
        output.append(action)
    return output


def _apply_local_feed_labels(
    session: Session,
    account: GoogleAdsAccount,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    promoted = 0
    excluded = 0
    changed_urls: list[dict[str, Any]] = []
    by_url = {
        str(row.product_url or "").split("?", 1)[0].rstrip("/"): row
        for row in session.scalars(
            select(OdooProductPageSignal).where(OdooProductPageSignal.account_id == account.id)
        ).all()
        if row.product_url
    }
    for action in actions:
        if action.get("action_type") not in LOCAL_FEED_ACTIONS or action.get("apply_mode") != "local_only":
            continue
        target_label = "winner" if action.get("action_type") == "promote_page_feed_urls" else "exclude"
        for raw_url in action.get("items") or []:
            url = str(raw_url or "").split("?", 1)[0].rstrip("/")
            signal = by_url.get(url)
            if signal is None or signal.label == target_label:
                continue
            old_label = signal.label
            if target_label == "exclude" and float(signal.order_count or 0) > 0 and float(signal.margin_amount or 0) > 0:
                continue
            signal.label = target_label
            source = dict(signal.source_json or {})
            source["optimizer_label_update"] = {
                "old_label": old_label,
                "new_label": target_label,
                "updated_at": utcnow().isoformat(),
                "reason": action.get("title") or action.get("action_type"),
            }
            signal.source_json = source
            changed_urls.append({"url": url, "old_label": old_label, "new_label": target_label})
            if target_label == "winner":
                promoted += 1
            else:
                excluded += 1
    if changed_urls:
        session.commit()
    return {
        "promoted": promoted,
        "excluded": excluded,
        "changed_urls": changed_urls[:100],
    }


def run_campaign_optimization(
    session: Session,
    run: CampaignOptimizationRun,
    *,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    settings = get_sync_setting_map(session)
    account = session.get(GoogleAdsAccount, run.account_id)
    if account is None:
        raise ValueError("Google Ads account not found for optimization run.")
    run.status = "running"
    run.started_at = utcnow()
    session.commit()
    campaign_type = normalize_campaign_type(run.campaign_type)
    days = min(max(int(run.days or _num(settings, "optimizer.default_optimization_days", DEFAULT_OPTIMIZER_DAYS)), 1), 90)
    max_rows = min(max(int(run.max_rows or _num(settings, "optimizer.optimization_max_rows", DEFAULT_OPTIMIZER_MAX_ROWS)), 50), 20_000)
    snapshot_result = load_or_fetch_optimizer_snapshots(
        session,
        account,
        campaign_type=campaign_type,
        campaign_id=run.campaign_id,
        days=days,
        max_rows=max_rows,
        force=bool(run.force_refresh),
        source_job_id=source_job_id,
    )
    datasets = snapshot_result["datasets"]
    campaign_rows = [row for row in datasets.get(DATASET_CAMPAIGN_INSIGHTS, {}).get("rows", []) if isinstance(row, dict)]
    if run.campaign_id and campaign_rows:
        run.campaign_name = str(campaign_rows[0].get("campaign_name") or run.campaign_name or "")
    cost_by_currency: dict[str, float] = {}
    for row in campaign_rows:
        currency = str(row.get("currency_code") or account.currency_code or "UNKNOWN")
        cost_by_currency[currency] = cost_by_currency.get(currency, 0.0) + float(row.get("cost") or 0)
    margin_guard = margin_guard_for_optimizer(
        session,
        account,
        settings,
        cost_by_currency=cost_by_currency,
        days=days,
    )
    product_signals = _product_signals(session, account)
    rule_plan = build_rule_plan(
        account=account,
        datasets=datasets,
        settings=settings,
        product_signals=product_signals,
        margin_guard=margin_guard,
    )
    if not bool(run.apply_local_feed_labels):
        rule_plan["actions"] = [
            action
            for action in rule_plan.get("actions", [])
            if action.get("action_type") not in LOCAL_FEED_ACTIONS
        ]
    context = {
        "account": {"id": account.id, "name": account.name, "customer_id": account.customer_id, "currency_code": account.currency_code},
        "campaign_type": campaign_type,
        "campaign_id": run.campaign_id,
        "date_range": snapshot_result["date_range"],
        "datasets": datasets,
        "dataset_statuses": snapshot_result["statuses"],
        "rule_plan": rule_plan,
        "allow_local_feed_labels": bool(run.apply_local_feed_labels),
    }
    prompt = build_openai_prompt(context)
    ai_result = {"status": "skipped", "actions": []}
    ai_actions: list[dict[str, Any]] = []
    if bool(run.use_openai):
        ai_result, prompt = _openai_review(settings, prompt)
        ai_actions = _sanitize_ai_actions(ai_result, settings=settings, margin_guard=margin_guard)
    if not bool(run.apply_local_feed_labels):
        ai_actions = [
            action
            for action in ai_actions
            if action.get("action_type") not in LOCAL_FEED_ACTIONS
        ]
    hard_rule_actions = [
        action
        for action in rule_plan["actions"]
        if action.get("priority") in {"critical", "high"} or action.get("action_type") in LOCAL_FEED_ACTIONS
    ]
    softer_rule_actions = [action for action in rule_plan["actions"] if action not in hard_rule_actions]
    final_actions = _dedupe_actions(hard_rule_actions + ai_actions + softer_rule_actions)
    local_label_result = {"promoted": 0, "excluded": 0, "changed_urls": []}
    apply_local = bool(run.apply_local_feed_labels) and parse_bool(settings.get("optimizer.apply_local_page_feed_labels", True))
    if apply_local:
        local_label_result = _apply_local_feed_labels(session, account, final_actions)
    live_mutation_ready = parse_bool(settings.get("optimizer.allow_mutations", False)) and not parse_bool(settings.get("optimizer.dry_run", True))
    summary = {
        "account": {"id": account.id, "name": account.name, "customer_id": account.customer_id},
        "campaign_type": campaign_type,
        "campaign_id": run.campaign_id,
        "campaign_name": run.campaign_name,
        "date_range": snapshot_result["date_range"],
        "dataset_statuses": snapshot_result["statuses"],
        "api_queries_attempted": snapshot_result["api_queries_attempted"],
        "errors": snapshot_result["errors"],
        "action_count": len(final_actions),
        "google_mutation_action_count": len([action for action in final_actions if action.get("google_mutation_required")]),
        "local_feed_updates": local_label_result,
        "live_mutation_ready": live_mutation_ready,
        "live_mutation_status": "not_sent",
        "live_mutation_reason": (
            "This optimizer run records decisions and local feed-label changes. Google budget, bid, negative-keyword, schedule, device, and geo mutations are not sent by this button yet."
        ),
        "margin_guard": margin_guard,
        "openai_status": ai_result.get("status"),
        "openai_decision": ai_result.get("decision"),
        "openai_confidence": ai_result.get("confidence"),
    }
    run.summary_json = summary
    run.actions_json = {
        "actions": final_actions,
        "rule_actions": rule_plan["actions"],
        "openai_actions": ai_actions,
        "manual_actions": ai_result.get("manual_actions") or [],
        "risks": ai_result.get("risks") or [],
    }
    run.source_snapshot_ids = snapshot_result["source_snapshot_ids"]
    run.prompt = prompt
    run.openai_response_json = ai_result
    run.status = "succeeded" if campaign_rows or final_actions else "needs_data"
    run.finished_at = utcnow()
    run.error = "\n".join(item.get("error", "") for item in snapshot_result["errors"][:5]) or None
    session.commit()
    return summary
