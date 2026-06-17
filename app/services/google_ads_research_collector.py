from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsDataSnapshot
from app.services.google_ads_api_errors import record_google_ads_api_error, record_google_ads_generic_error
from app.services.google_ads_snapshot_store import (
    DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
    DATASET_AUCTION_INSIGHTS_PROXY,
    DATASET_CAMPAIGN_INSIGHTS,
    DATASET_GEO_SEGMENTS,
    DATASET_LANDING_PAGES,
    DATASET_SEARCH_TERM_INSIGHTS,
    DATASET_SEARCH_TERMS,
    DATASET_TIME_SEGMENTS,
    get_fresh_snapshot,
    upsert_snapshot,
)
from app.services.google_ads_sync import build_client, enum_name, optional_float


DEFAULT_RESEARCH_DAYS = 60
DEFAULT_RESEARCH_MAX_ROWS = 5000
RESEARCH_SNAPSHOT_TTL_HOURS = 24


def _is_google_ads_quota_exception(exc: Exception) -> bool:
    haystack = " ".join([exc.__class__.__name__, str(exc)]).lower()
    return any(token in haystack for token in ["quota", "resource exhausted", "too many requests", "basic access"])


@dataclass(frozen=True)
class ResearchDatasetSpec:
    dataset_key: str
    title: str
    query_builder: Callable[[date, date, int], str]
    row_parser: Callable[[Any], dict[str, Any]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def research_date_window(days: int) -> tuple[date, date]:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(int(days or DEFAULT_RESEARCH_DAYS), 1) - 1)
    return start_date, end_date


def research_scope(days: int, start_date: date, end_date: date) -> str:
    return f"last_{int(days)}d:{start_date.isoformat()}:{end_date.isoformat()}"


def snapshot_expires_at(end_date: date) -> datetime | None:
    if end_date >= date.today():
        return utcnow() + timedelta(hours=RESEARCH_SNAPSHOT_TTL_HOURS)
    return None


def micros_to_units(value: Any) -> float:
    return float(value or 0) / 1_000_000


def metric_payload(metrics: Any) -> dict[str, Any]:
    cost_micros = int(getattr(metrics, "cost_micros", 0) or 0)
    clicks = int(getattr(metrics, "clicks", 0) or 0)
    impressions = int(getattr(metrics, "impressions", 0) or 0)
    conversions = float(getattr(metrics, "conversions", 0) or 0)
    conversion_value = float(getattr(metrics, "conversions_value", 0) or 0)
    return {
        "cost_micros": cost_micros,
        "cost": cost_micros / 1_000_000,
        "impressions": impressions,
        "clicks": clicks,
        "conversions": conversions,
        "conversion_value": conversion_value,
        "all_conversions": float(getattr(metrics, "all_conversions", 0) or 0),
        "all_conversions_value": float(getattr(metrics, "all_conversions_value", 0) or 0),
        "ctr": (clicks / impressions) if impressions else 0.0,
        "cpc": (cost_micros / 1_000_000 / clicks) if clicks else 0.0,
        "cost_per_conversion": (cost_micros / 1_000_000 / conversions) if conversions else None,
        "roas": (conversion_value / (cost_micros / 1_000_000)) if cost_micros else None,
    }


def campaign_base(row: Any) -> dict[str, Any]:
    return {
        "campaign_id": int(row.campaign.id),
        "campaign_name": str(row.campaign.name or row.campaign.id),
        "campaign_resource_name": row.campaign.resource_name,
        "campaign_status": enum_name(row.campaign.status),
        "channel_type": enum_name(row.campaign.advertising_channel_type),
    }


def query_campaign_insights(start_date: date, end_date: date, max_rows: int) -> str:
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
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_campaign_insight(row: Any) -> dict[str, Any]:
    metrics = metric_payload(row.metrics)
    return {
        **campaign_base(row),
        "currency_code": str(row.customer.currency_code or ""),
        "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
        "target_roas": optional_float(row.campaign.maximize_conversion_value.target_roas),
        "target_cpa": micros_to_units(row.campaign.maximize_conversions.target_cpa_micros),
        "budget_amount": micros_to_units(row.campaign_budget.amount_micros),
        "budget_explicitly_shared": bool(row.campaign_budget.explicitly_shared),
        "search_impression_share": float(row.metrics.search_impression_share or 0),
        "search_budget_lost_impression_share": float(row.metrics.search_budget_lost_impression_share or 0),
        "search_rank_lost_impression_share": float(row.metrics.search_rank_lost_impression_share or 0),
        **metrics,
    }


def query_search_terms(start_date: date, end_date: date, max_rows: int) -> str:
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
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_search_term(row: Any) -> dict[str, Any]:
    return {
        **campaign_base(row),
        "ad_group_id": int(row.ad_group.id or 0),
        "ad_group_name": str(row.ad_group.name or ""),
        "search_term": str(row.search_term_view.search_term or ""),
        "search_term_status": enum_name(row.search_term_view.status),
        **metric_payload(row.metrics),
    }


def query_ai_max_search_term_combinations(start_date: date, end_date: date, max_rows: int) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          ad_group.id,
          ad_group.name,
          ai_max_search_term_ad_combination_view.resource_name,
          ai_max_search_term_ad_combination_view.search_term,
          ai_max_search_term_ad_combination_view.landing_page,
          ai_max_search_term_ad_combination_view.headline,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM ai_max_search_term_ad_combination_view
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_ai_max_search_term_combination(row: Any) -> dict[str, Any]:
    view = row.ai_max_search_term_ad_combination_view
    landing_page = str(getattr(view, "landing_page", "") or "")
    search_term = str(getattr(view, "search_term", "") or "")
    headline = str(getattr(view, "headline", "") or "")
    return {
        **campaign_base(row),
        "ad_group_id": int(row.ad_group.id or 0),
        "ad_group_name": str(row.ad_group.name or ""),
        "resource_name": str(getattr(view, "resource_name", "") or ""),
        "search_term": search_term,
        "landing_page": landing_page,
        "url": landing_page,
        "expanded_final_url": landing_page,
        "headline": headline,
        "source": "ai_max",
        "match_source": "ai_max_search_term_ad_combination_view",
        "search_term_status": "AI_MAX_DISCOVERY",
        **metric_payload(row.metrics),
    }


def query_search_term_insights(start_date: date, end_date: date, max_rows: int) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign_search_term_insight.id,
          campaign_search_term_insight.category_label,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign_search_term_insight
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign.status != REMOVED
        ORDER BY metrics.impressions DESC
        LIMIT {int(max_rows)}
    """


def query_search_term_insights_for_campaign(start_date: date, end_date: date, max_rows: int, campaign_id: int) -> str:
    return f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign_search_term_insight.id,
          campaign_search_term_insight.category_label,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign_search_term_insight
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
          AND campaign_search_term_insight.campaign_id = {int(campaign_id)}
        ORDER BY metrics.impressions DESC
        LIMIT {int(max_rows)}
    """


def search_term_insight_loop_query(start_date: date, end_date: date, max_rows: int) -> str:
    return f"campaign_search_term_insight_loop:{start_date.isoformat()}:{end_date.isoformat()}:max_rows:{int(max_rows)}"


def parse_search_term_insight(row: Any) -> dict[str, Any]:
    return {
        **campaign_base(row),
        "insight_id": int(row.campaign_search_term_insight.id or 0),
        "category_label": str(row.campaign_search_term_insight.category_label or ""),
        **metric_payload(row.metrics),
    }


def query_landing_pages(start_date: date, end_date: date, max_rows: int) -> str:
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
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_landing_page(row: Any) -> dict[str, Any]:
    url = str(row.expanded_landing_page_view.expanded_final_url or "")
    return {
        **campaign_base(row),
        "url": url,
        "expanded_final_url": url,
        **metric_payload(row.metrics),
    }


def query_time_segments(start_date: date, end_date: date, max_rows: int) -> str:
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
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_time_segment(row: Any) -> dict[str, Any]:
    return {
        **campaign_base(row),
        "day_of_week": enum_name(row.segments.day_of_week),
        "hour": int(row.segments.hour),
        "device": enum_name(row.segments.device),
        "ad_network_type": enum_name(row.segments.ad_network_type),
        **metric_payload(row.metrics),
    }


def query_geo_segments(start_date: date, end_date: date, max_rows: int) -> str:
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
        ORDER BY metrics.cost_micros DESC
        LIMIT {int(max_rows)}
    """


def parse_geo_segment(row: Any) -> dict[str, Any]:
    return {
        **campaign_base(row),
        "country_criterion_id": int(row.geographic_view.country_criterion_id or 0),
        "location_type": enum_name(row.geographic_view.location_type),
        **metric_payload(row.metrics),
    }


def auction_proxy_query(start_date: date, end_date: date, max_rows: int) -> str:
    return query_campaign_insights(start_date, end_date, max_rows)


def parse_auction_proxy(row: Any) -> dict[str, Any]:
    parsed = parse_campaign_insight(row)
    return {
        "note": "True competitor Auction Insights are not generally exposed by the Google Ads API. This row stores available auction-pressure proxies.",
        "campaign_id": parsed["campaign_id"],
        "campaign_name": parsed["campaign_name"],
        "channel_type": parsed["channel_type"],
        "cost": parsed["cost"],
        "impressions": parsed["impressions"],
        "clicks": parsed["clicks"],
        "conversions": parsed["conversions"],
        "conversion_value": parsed["conversion_value"],
        "search_impression_share": parsed["search_impression_share"],
        "search_budget_lost_impression_share": parsed["search_budget_lost_impression_share"],
        "search_rank_lost_impression_share": parsed["search_rank_lost_impression_share"],
    }


RESEARCH_DATASETS = [
    ResearchDatasetSpec(DATASET_CAMPAIGN_INSIGHTS, "Campaign insights", query_campaign_insights, parse_campaign_insight),
    ResearchDatasetSpec(DATASET_SEARCH_TERMS, "Search terms", query_search_terms, parse_search_term),
    ResearchDatasetSpec(
        DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
        "AI Max search term/ad combinations",
        query_ai_max_search_term_combinations,
        parse_ai_max_search_term_combination,
    ),
    ResearchDatasetSpec(DATASET_SEARCH_TERM_INSIGHTS, "Search term insights", query_search_term_insights, parse_search_term_insight),
    ResearchDatasetSpec(DATASET_LANDING_PAGES, "Landing pages", query_landing_pages, parse_landing_page),
    ResearchDatasetSpec(DATASET_TIME_SEGMENTS, "When ads showed", query_time_segments, parse_time_segment),
    ResearchDatasetSpec(DATASET_GEO_SEGMENTS, "Where ads showed", query_geo_segments, parse_geo_segment),
    ResearchDatasetSpec(DATASET_AUCTION_INSIGHTS_PROXY, "Auction insights proxy", auction_proxy_query, parse_auction_proxy),
]


def rows_by_campaign(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    campaigns: dict[str, dict[str, Any]] = {}
    for row in rows:
        campaign_id = str(row.get("campaign_id") or "0")
        campaign = campaigns.setdefault(
            campaign_id,
            {
                "campaign_id": row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "channel_type": row.get("channel_type"),
                "rows": [],
                "summary": {
                    "cost": 0.0,
                    "impressions": 0,
                    "clicks": 0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                },
            },
        )
        campaign["rows"].append(row)
        summary = campaign["summary"]
        summary["cost"] += float(row.get("cost") or 0)
        summary["impressions"] += int(row.get("impressions") or 0)
        summary["clicks"] += int(row.get("clicks") or 0)
        summary["conversions"] += float(row.get("conversions") or 0)
        summary["conversion_value"] += float(row.get("conversion_value") or 0)
    for campaign in campaigns.values():
        summary = campaign["summary"]
        cost = float(summary["cost"] or 0)
        conversions = float(summary["conversions"] or 0)
        summary["cost_per_conversion"] = (cost / conversions) if conversions else None
        summary["roas"] = (float(summary["conversion_value"] or 0) / cost) if cost else None
    return campaigns


def fetch_rows(client: GoogleAdsClient, account: GoogleAdsAccount, spec: ResearchDatasetSpec, start_date: date, end_date: date, max_rows: int) -> tuple[str, list[dict[str, Any]]]:
    query = spec.query_builder(start_date, end_date, max_rows)
    ga_service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for batch in ga_service.search_stream(customer_id=account.customer_id, query=query):
        for row in batch.results:
            rows.append(spec.row_parser(row))
            if len(rows) >= max_rows:
                return query, rows
    return query, rows


def campaign_ids_for_search_term_insights(session: Session, account: GoogleAdsAccount, scope_key: str) -> list[int]:
    snapshot = session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == DATASET_CAMPAIGN_INSIGHTS,
            GoogleAdsDataSnapshot.scope_key == scope_key,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )
    rows = []
    if snapshot is not None and isinstance(snapshot.payload_json, dict):
        rows = snapshot.payload_json.get("rows") or []
    ids = [int(row.get("campaign_id") or 0) for row in rows if isinstance(row, dict) and row.get("campaign_id")]
    return list(dict.fromkeys(item for item in ids if item))


def fetch_search_term_insight_rows_by_campaign(
    client: GoogleAdsClient,
    account: GoogleAdsAccount,
    *,
    session: Session,
    scope_key: str,
    start_date: date,
    end_date: date,
    max_rows: int,
) -> tuple[str, list[dict[str, Any]]]:
    query_marker = search_term_insight_loop_query(start_date, end_date, max_rows)
    campaign_ids = campaign_ids_for_search_term_insights(session, account, scope_key)
    if not campaign_ids:
        return query_marker, []
    ga_service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    per_campaign_limit = min(max(int(max_rows or DEFAULT_RESEARCH_MAX_ROWS), 50), 5000)
    for campaign_id in campaign_ids:
        query = query_search_term_insights_for_campaign(start_date, end_date, per_campaign_limit, campaign_id)
        for batch in ga_service.search_stream(customer_id=account.customer_id, query=query):
            for row in batch.results:
                rows.append(parse_search_term_insight(row))
    return query_marker, rows


def latest_research_summary(session: Session, account: GoogleAdsAccount, *, days: int = DEFAULT_RESEARCH_DAYS) -> dict[str, Any]:
    start_date, end_date = research_date_window(days)
    scope_key = research_scope(days, start_date, end_date)
    snapshots = session.scalars(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.scope_key == scope_key,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc())
    ).all()
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "scope_key": scope_key,
        "datasets": {
            snapshot.dataset_key: {
                "row_count": snapshot.row_count,
                "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
            }
            for snapshot in snapshots
        },
    }


def sync_account_research_snapshots(
    session: Session,
    account: GoogleAdsAccount,
    *,
    days: int = DEFAULT_RESEARCH_DAYS,
    max_rows: int = DEFAULT_RESEARCH_MAX_ROWS,
    force: bool = False,
    source_job_id: int | None = None,
) -> dict[str, Any]:
    days = min(max(int(days or DEFAULT_RESEARCH_DAYS), 1), 365)
    max_rows = min(max(int(max_rows or DEFAULT_RESEARCH_MAX_ROWS), 50), 50_000)
    start_date, end_date = research_date_window(days)
    scope_key = research_scope(days, start_date, end_date)
    expires_at = snapshot_expires_at(end_date)
    values = get_sync_setting_map(session)
    client: GoogleAdsClient | None = None
    datasets: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for spec in RESEARCH_DATASETS:
        query = (
            search_term_insight_loop_query(start_date, end_date, max_rows)
            if spec.dataset_key == DATASET_SEARCH_TERM_INSIGHTS
            else spec.query_builder(start_date, end_date, max_rows)
        )
        cached = None if force else get_fresh_snapshot(
            session,
            dataset_key=spec.dataset_key,
            account=account,
            scope_key=scope_key,
            query=query,
        )
        if cached is not None:
            datasets.append(
                {
                    "dataset_key": spec.dataset_key,
                    "title": spec.title,
                    "status": "cached",
                    "rows": cached.row_count,
                    "fetched_at": cached.fetched_at.isoformat() if cached.fetched_at else None,
                }
            )
            continue
        try:
            client = client or build_client(values, account.manager_customer_id, account.connection)
            if spec.dataset_key == DATASET_SEARCH_TERM_INSIGHTS:
                query, rows = fetch_search_term_insight_rows_by_campaign(
                    client,
                    account,
                    session=session,
                    scope_key=scope_key,
                    start_date=start_date,
                    end_date=end_date,
                    max_rows=max_rows,
                )
            else:
                query, rows = fetch_rows(client, account, spec, start_date, end_date, max_rows)
            payload = {
                "account": {
                    "id": account.id,
                    "name": account.name,
                    "customer_id": account.customer_id,
                    "currency_code": account.currency_code,
                },
                "dataset_key": spec.dataset_key,
                "title": spec.title,
                "date_range": {
                    "days": days,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
                "rows": rows,
                "campaigns": rows_by_campaign(rows),
                "row_limit": max_rows,
                "notes": [
                    "Stored in Postgres JSON for reuse by analysis, page-feed generation, campaign planning, and future daily enhancement.",
                ],
                "fetched_at": utcnow().isoformat(),
            }
            if spec.dataset_key == DATASET_AUCTION_INSIGHTS_PROXY:
                payload["notes"].append(
                    "True competitor Auction Insights are not generally available through the Google Ads API; this dataset stores available impression-share pressure proxies."
                )
            if spec.dataset_key == DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS:
                payload["notes"].append(
                    "AI Max rows preserve the search term, generated headline, landing page, and performance in one combination so automation can classify AI Max discoveries more accurately."
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
            datasets.append(
                {
                    "dataset_key": spec.dataset_key,
                    "title": spec.title,
                    "status": "fetched",
                    "rows": len(rows),
                }
            )
        except GoogleAdsException as exc:
            session.rollback()
            record_google_ads_api_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="research_sync",
                severity="manual_action_required",
                extra={"dataset_key": spec.dataset_key, "days": days, "max_rows": max_rows},
            )
            errors.append({"dataset_key": spec.dataset_key, "error": str(exc)[:500]})
            if _is_google_ads_quota_exception(exc):
                raise
        except Exception as exc:  # noqa: BLE001 - continue other datasets.
            session.rollback()
            record_google_ads_generic_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="research_sync",
                severity="manual_action_required",
                extra={"dataset_key": spec.dataset_key, "days": days, "max_rows": max_rows},
            )
            errors.append({"dataset_key": spec.dataset_key, "error": str(exc)[:500]})
            if _is_google_ads_quota_exception(exc):
                raise

    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "days": days,
        "scope_key": scope_key,
        "datasets": datasets,
        "errors": errors,
    }
