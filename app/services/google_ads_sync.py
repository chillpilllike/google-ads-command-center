from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsAccountDailyMetric, GoogleAdsCampaignMetric, GoogleAdsConnection
from app.services.google_ads_snapshot_store import (
    DATASET_CAMPAIGN_DAILY,
    get_fresh_snapshot,
    upsert_snapshot,
)


PERFORMANCE_SNAPSHOT_TTL_HOURS = 6


def enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return name
    return str(value).split(".")[-1]


def optional_float(value: Any) -> float | None:
    parsed = float(value or 0)
    return parsed or None


def optional_int(value: Any) -> int | None:
    parsed = int(value or 0)
    return parsed or None


def campaign_daily_scope(start_date: date, end_date: date) -> str:
    return f"{start_date.isoformat()}:{end_date.isoformat()}"


def campaign_daily_query(start_date: date, end_date: date) -> str:
    return f"""
        SELECT
          customer.currency_code,
          segments.date,
          campaign.id,
          campaign.resource_name,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversion_value.target_roas,
          campaign.maximize_conversions.target_cpa_micros,
          campaign_budget.resource_name,
          campaign_budget.name,
          campaign_budget.amount_micros,
          campaign_budget.explicitly_shared,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
    """


def snapshot_expiry_for_date_range(end_date: date) -> datetime | None:
    if end_date >= date.today():
        return datetime.now(timezone.utc) + timedelta(hours=PERFORMANCE_SNAPSHOT_TTL_HOURS)
    return None


def campaign_daily_rows_from_google(
    client: GoogleAdsClient,
    account: GoogleAdsAccount,
    *,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    ga_service = client.get_service("GoogleAdsService")
    query = campaign_daily_query(start_date, end_date)
    rows: list[dict[str, Any]] = []
    for batch in ga_service.search_stream(customer_id=account.customer_id, query=query):
        for row in batch.results:
            rows.append(
                {
                    "customer_currency_code": row.customer.currency_code or "",
                    "metric_date": str(row.segments.date),
                    "campaign_id": int(row.campaign.id),
                    "campaign_resource_name": row.campaign.resource_name,
                    "campaign_name": row.campaign.name,
                    "campaign_status": enum_name(row.campaign.status),
                    "channel_type": enum_name(row.campaign.advertising_channel_type),
                    "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
                    "target_roas": optional_float(row.campaign.maximize_conversion_value.target_roas),
                    "target_cpa_micros": optional_int(row.campaign.maximize_conversions.target_cpa_micros),
                    "budget_resource_name": row.campaign_budget.resource_name,
                    "budget_name": row.campaign_budget.name,
                    "budget_amount_micros": int(row.campaign_budget.amount_micros or 0),
                    "budget_explicitly_shared": bool(row.campaign_budget.explicitly_shared),
                    "cost_micros": int(row.metrics.cost_micros or 0),
                    "impressions": int(row.metrics.impressions or 0),
                    "clicks": int(row.metrics.clicks or 0),
                    "conversions": float(row.metrics.conversions or 0),
                    "conversions_value": float(row.metrics.conversions_value or 0),
                    "all_conversions": float(row.metrics.all_conversions or 0),
                    "all_conversions_value": float(row.metrics.all_conversions_value or 0),
                }
            )
    return rows


def _credential_value(connection: GoogleAdsConnection | None, attr: str, fallback: Any) -> Any:
    if connection is None:
        return fallback
    value = getattr(connection, attr, None)
    return value if value not in {None, ""} else fallback


def build_client(
    values: dict[str, Any],
    login_customer_id: str,
    connection: GoogleAdsConnection | None = None,
) -> GoogleAdsClient:
    config = {
        "developer_token": _credential_value(connection, "developer_token", values.get("google_ads.developer_token")),
        "client_id": _credential_value(connection, "client_id", values.get("google_ads.client_id")),
        "client_secret": _credential_value(connection, "client_secret", values.get("google_ads.client_secret")),
        "refresh_token": _credential_value(connection, "refresh_token", values.get("google_ads.refresh_token")),
        "login_customer_id": login_customer_id,
        "use_proto_plus": True,
    }
    missing = [key for key, value in config.items() if key != "use_proto_plus" and not value]
    if missing:
        raise RuntimeError(f"Missing Google Ads settings: {', '.join(missing)}")
    api_version = _credential_value(connection, "api_version", values.get("google_ads.api_version"))
    if api_version:
        return GoogleAdsClient.load_from_dict(config, version=str(api_version))
    return GoogleAdsClient.load_from_dict(config)


def refresh_account_currency(
    session: Session,
    account: GoogleAdsAccount,
    client: GoogleAdsClient | None = None,
) -> str:
    values = get_sync_setting_map(session)
    client = client or build_client(values, account.manager_customer_id, account.connection)
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer.id,
          customer.descriptive_name,
          customer.currency_code
        FROM customer
        LIMIT 1
    """
    for row in ga_service.search(customer_id=account.customer_id, query=query):
        currency_code = row.customer.currency_code or ""
        if currency_code and account.currency_code != currency_code:
            account.currency_code = currency_code
        return currency_code
    return account.currency_code or ""


def sync_account_daily_metrics(
    session: Session,
    account: GoogleAdsAccount,
    days: int,
    client: GoogleAdsClient | None = None,
) -> int:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    snapshot = get_fresh_snapshot(
        session,
        dataset_key=DATASET_CAMPAIGN_DAILY,
        account=account,
        scope_key=campaign_daily_scope(start_date, end_date),
        query=campaign_daily_query(start_date, end_date),
    )
    if snapshot is not None:
        rows = (snapshot.payload_json or {}).get("rows") or []
        if isinstance(rows, list):
            saved = upsert_account_daily_metrics_from_campaign_rows(session, account, rows)
            session.commit()
            return saved

    values = get_sync_setting_map(session)
    client = client or build_client(values, account.manager_customer_id, account.connection)
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          customer.currency_code,
          segments.date,
          metrics.cost_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM customer
        WHERE segments.date BETWEEN '{start_date.isoformat()}' AND '{end_date.isoformat()}'
    """
    saved = 0
    account_currency_code = account.currency_code or ""
    for row in ga_service.search(customer_id=account.customer_id, query=query):
        if row.customer.currency_code:
            account_currency_code = row.customer.currency_code
        metric_date = date.fromisoformat(str(row.segments.date))
        stmt = insert(GoogleAdsAccountDailyMetric).values(
            account_id=account.id,
            metric_date=metric_date,
            currency_code=account_currency_code,
            cost_micros=int(row.metrics.cost_micros),
            impressions=int(row.metrics.impressions),
            clicks=int(row.metrics.clicks),
            conversions=float(row.metrics.conversions),
            conversions_value=float(row.metrics.conversions_value),
            all_conversions=float(row.metrics.all_conversions),
            all_conversions_value=float(row.metrics.all_conversions_value),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsAccountDailyMetric.account_id,
                GoogleAdsAccountDailyMetric.metric_date,
            ],
            set_={
                "currency_code": stmt.excluded.currency_code,
                "cost_micros": stmt.excluded.cost_micros,
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "conversions": stmt.excluded.conversions,
                "conversions_value": stmt.excluded.conversions_value,
                "all_conversions": stmt.excluded.all_conversions,
                "all_conversions_value": stmt.excluded.all_conversions_value,
                "synced_at": func.now(),
            },
        )
        session.execute(stmt)
        saved += 1
    if account_currency_code and account.currency_code != account_currency_code:
        account.currency_code = account_currency_code
    session.commit()
    return saved


def upsert_account_daily_metrics_from_campaign_rows(
    session: Session,
    account: GoogleAdsAccount,
    rows: list[dict[str, Any]],
) -> int:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    account_currency_code = account.currency_code or ""
    for row in rows:
        currency_code = str(row.get("customer_currency_code") or account.currency_code or "")
        if currency_code:
            account_currency_code = currency_code
        metric_date_text = str(row.get("metric_date") or "")
        if not metric_date_text:
            continue
        key = (metric_date_text, currency_code)
        current = grouped.setdefault(
            key,
            {
                "cost_micros": 0,
                "impressions": 0,
                "clicks": 0,
                "conversions": 0.0,
                "conversions_value": 0.0,
                "all_conversions": 0.0,
                "all_conversions_value": 0.0,
            },
        )
        current["cost_micros"] += int(row.get("cost_micros") or 0)
        current["impressions"] += int(row.get("impressions") or 0)
        current["clicks"] += int(row.get("clicks") or 0)
        current["conversions"] += float(row.get("conversions") or 0)
        current["conversions_value"] += float(row.get("conversions_value") or 0)
        current["all_conversions"] += float(row.get("all_conversions") or 0)
        current["all_conversions_value"] += float(row.get("all_conversions_value") or 0)

    saved = 0
    for (metric_date_text, currency_code), metrics in grouped.items():
        stmt = insert(GoogleAdsAccountDailyMetric).values(
            account_id=account.id,
            metric_date=date.fromisoformat(metric_date_text),
            currency_code=currency_code or account_currency_code,
            cost_micros=metrics["cost_micros"],
            impressions=metrics["impressions"],
            clicks=metrics["clicks"],
            conversions=metrics["conversions"],
            conversions_value=metrics["conversions_value"],
            all_conversions=metrics["all_conversions"],
            all_conversions_value=metrics["all_conversions_value"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsAccountDailyMetric.account_id,
                GoogleAdsAccountDailyMetric.metric_date,
            ],
            set_={
                "currency_code": stmt.excluded.currency_code,
                "cost_micros": stmt.excluded.cost_micros,
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "conversions": stmt.excluded.conversions,
                "conversions_value": stmt.excluded.conversions_value,
                "all_conversions": stmt.excluded.all_conversions,
                "all_conversions_value": stmt.excluded.all_conversions_value,
                "synced_at": func.now(),
            },
        )
        session.execute(stmt)
        saved += 1
    if account_currency_code and account.currency_code != account_currency_code:
        account.currency_code = account_currency_code
    return saved


def upsert_campaign_metrics_from_rows(
    session: Session,
    account: GoogleAdsAccount,
    rows: list[dict[str, Any]],
) -> int:
    saved = 0
    account_currency_code = account.currency_code or ""
    for row in rows:
        if row.get("customer_currency_code"):
            account_currency_code = str(row["customer_currency_code"])
        metric_date_text = str(row.get("metric_date") or "")
        if not metric_date_text:
            continue
        stmt = insert(GoogleAdsCampaignMetric).values(
            account_id=account.id,
            metric_date=date.fromisoformat(metric_date_text),
            campaign_id=int(row.get("campaign_id") or 0),
            campaign_name=str(row.get("campaign_name") or ""),
            campaign_status=str(row.get("campaign_status") or "UNKNOWN"),
            channel_type=str(row.get("channel_type") or "UNKNOWN"),
            bidding_strategy_type=str(row.get("bidding_strategy_type") or "UNKNOWN"),
            target_roas=row.get("target_roas"),
            budget_amount_micros=int(row.get("budget_amount_micros") or 0),
            cost_micros=int(row.get("cost_micros") or 0),
            impressions=int(row.get("impressions") or 0),
            clicks=int(row.get("clicks") or 0),
            conversions=float(row.get("conversions") or 0),
            conversions_value=float(row.get("conversions_value") or 0),
            all_conversions=float(row.get("all_conversions") or 0),
            all_conversions_value=float(row.get("all_conversions_value") or 0),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsCampaignMetric.account_id,
                GoogleAdsCampaignMetric.metric_date,
                GoogleAdsCampaignMetric.campaign_id,
            ],
            set_={
                "campaign_name": stmt.excluded.campaign_name,
                "campaign_status": stmt.excluded.campaign_status,
                "channel_type": stmt.excluded.channel_type,
                "bidding_strategy_type": stmt.excluded.bidding_strategy_type,
                "target_roas": stmt.excluded.target_roas,
                "budget_amount_micros": stmt.excluded.budget_amount_micros,
                "cost_micros": stmt.excluded.cost_micros,
                "impressions": stmt.excluded.impressions,
                "clicks": stmt.excluded.clicks,
                "conversions": stmt.excluded.conversions,
                "conversions_value": stmt.excluded.conversions_value,
                "all_conversions": stmt.excluded.all_conversions,
                "all_conversions_value": stmt.excluded.all_conversions_value,
            },
        )
        session.execute(stmt)
        saved += 1
    if account_currency_code and account.currency_code != account_currency_code:
        account.currency_code = account_currency_code
    return saved


def sync_account_campaign_metrics(
    session: Session,
    account: GoogleAdsAccount,
    days: int,
    source_job_id: int | None = None,
) -> int:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    scope_key = campaign_daily_scope(start_date, end_date)
    query = campaign_daily_query(start_date, end_date)
    snapshot = get_fresh_snapshot(
        session,
        dataset_key=DATASET_CAMPAIGN_DAILY,
        account=account,
        scope_key=scope_key,
        query=query,
    )
    if snapshot is None:
        values = get_sync_setting_map(session)
        client = build_client(values, account.manager_customer_id, account.connection)
        rows = campaign_daily_rows_from_google(client, account, start_date=start_date, end_date=end_date)
        upsert_snapshot(
            session,
            dataset_key=DATASET_CAMPAIGN_DAILY,
            account=account,
            scope_key=scope_key,
            query=query,
            payload={
                "customer_id": account.customer_id,
                "manager_customer_id": account.manager_customer_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "rows": rows,
            },
            expires_at=snapshot_expiry_for_date_range(end_date),
            source_job_id=source_job_id,
            row_count=len(rows),
        )
    else:
        rows = (snapshot.payload_json or {}).get("rows") or []
        if not isinstance(rows, list):
            rows = []
    saved = upsert_campaign_metrics_from_rows(session, account, rows)
    daily_saved = upsert_account_daily_metrics_from_campaign_rows(session, account, rows)
    if not rows and not (account.currency_code or ""):
        values = get_sync_setting_map(session)
        client = build_client(values, account.manager_customer_id, account.connection)
        refresh_account_currency(session, account, client)
    session.commit()
    return max(saved, daily_saved)
