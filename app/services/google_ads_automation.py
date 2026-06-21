from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import func, inspect as sa_inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, defer, load_only, noload

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import (
    AdDraft,
    AppSetting,
    AutoPilotEvent,
    GoogleAdsAccount,
    GoogleAdsAccountDailyMetric,
    GoogleAdsAutomationPreference,
    GoogleAdsCampaignMetric,
    GoogleAdsDataSnapshot,
    GoogleAdsKeywordCandidate,
    GoogleAdsLandingPageCandidate,
    GoogleAdsNegativeKeywordCandidate,
    GoogleAnalyticsDataSnapshot,
    GoogleAnalyticsSearchTermCandidate,
    OdooSaleOrder,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.currency_rates import convert_amount, get_latest_rate_snapshot_sync, snapshot_payload
from app.services.google_ads_api_errors import record_google_ads_api_error, record_google_ads_generic_error
from app.services.google_ads_assets import generate_account_assets
from app.services.google_ads_asset_publisher import publish_generated_assets
from app.services.google_ads_keyword_bank import sync_account_all_time_keyword_candidates, sync_account_keyword_candidates
from app.services.google_ads_landing_page_bank import (
    canonical_landing_page_url,
    sync_account_landing_page_candidates,
    sync_account_landing_page_pull,
    usable_landing_page_url,
)
from app.services.google_ads_negative_keyword_bank import sync_account_negative_keyword_candidates
from app.services.google_ads_pmax_gate import pmax_activation_gate, pmax_conversion_threshold
from app.services.google_ads_research_collector import sync_account_research_snapshots
from app.services.google_ads_snapshot_store import (
    DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
    DATASET_AUCTION_INSIGHTS_PROXY,
    DATASET_AUDIENCE_INSIGHTS,
    DATASET_CAMPAIGN_INSIGHTS,
    DATASET_LANDING_PAGES,
    DATASET_SEARCH_TERM_INSIGHTS,
    DATASET_SEARCH_TERMS,
    DATASET_TIME_SEGMENTS,
)
from app.services.google_ads_sync import build_client, enum_name, sync_account_campaign_metrics
from app.services.google_analytics import (
    DATASET_GA4_GOOGLE_ADS_SEARCH,
    DATASET_GA4_ITEM_ECOMMERCE,
    DATASET_GA4_LANDING_PAGE_ECOMMERCE,
    DATASET_GA4_TRAFFIC_ECOMMERCE,
    ensure_account_ga4_mapping,
    ga4_ads_signal_matrix,
    sync_ga4_ecommerce_snapshots,
    sync_ga4_search_term_snapshots,
)
from app.services.google_search_console import (
    GSC_DAILY_DAYS,
    GSC_DAILY_ROW_LIMIT,
    ensure_account_search_console_mapping,
    search_console_ads_signal_matrix,
    search_console_keyword_terms,
    sync_search_console_search_analytics,
)
from app.services.openai_ad_copy import generate_ad_copy
from app.services.odoo_product_feed import sync_store_product_page_feeds
from app.services.odoo_sales import sync_store_confirmed_orders, sync_store_websites
from app.services.page_feed_restrictions import (
    get_restricted_title_terms_sync,
    normalize_restricted_text,
    restricted_title_match,
)
from app.services.spend_guard import odoo_sales_budget_guard_for_account


DEFAULT_DYNAMIC_SCHEDULE_HOUR = 4
DEFAULT_DYNAMIC_SCHEDULE_MINUTE = 20
PEAK_BUDGET_STATE_PREFIX = "peak_budget_state"
CAMPAIGN_BOOTSTRAP_STATE_PREFIX = "campaign_bootstrap_state"
AUTOMATION_QUOTA_STATE_PREFIX = "automation_api_quota"
SCALE_SEARCH_THEME_MIGRATION_STATE_PREFIX = "scale_search_theme_migration"
SCALE_LANDING_PAGE_MIGRATION_STATE_PREFIX = "scale_landing_page_migration"
WASTE_RECOVERY_STATE_PREFIX = "waste_recovery"
AUDIENCE_SIGNAL_INPUTS_PREFIX = "audience_signal_inputs"
ASSET_PUBLISH_QUOTA_PREFIX = "google_ads_asset_publish_quota"
BUDGET_BOOTSTRAP_STATE_PREFIX = "automation_budget_bootstrap"
FORCE_MINIMUM_BUDGET_SETTING = "automation.force_minimum_budget_when_budget_guard_blocked"
CORE_SCALE_TARGET_ROAS = 6.67
TESTING_DISCOVERY_TARGET_ROAS = 5.0
FIX_WATCH_TARGET_ROAS = 3.5
WASTE_RECOVERY_TARGET_ROAS = 2.0
MAX_AUTOMATION_DATA_AGE_HOURS = 30
DEFAULT_TESTING_KEYWORD_LIMIT = 0
DEFAULT_TESTING_LANDING_PAGE_LIMIT = 0
DEFAULT_CRITERIA_PLANNING_WINDOW = 1000
CRITERIA_DAILY_ITEM_LIMIT_SETTING = "live_campaign_creator.criteria_daily_item_limit"
CAMPAIGN_PLANNING_REQUIRED_DATASETS = {DATASET_CAMPAIGN_INSIGHTS, DATASET_LANDING_PAGES}
CAMPAIGN_PLANNING_KEYWORD_DATASETS = {
    DATASET_SEARCH_TERMS,
    DATASET_SEARCH_TERM_INSIGHTS,
    DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS,
}
CAMPAIGN_METRIC_REFRESH_QUOTA_UNITS = 1
DAILY_INSIGHT_REFRESH_QUOTA_UNITS = 10
ALL_TIME_REFRESH_QUOTA_UNITS = 4
GA4_ECOMMERCE_REFRESH_MAX_AGE_HOURS = 20
BUDGET_GUARD_QUOTA_UNITS = 2
PEAK_BUDGET_TRANSITION_QUOTA_UNITS = 2
PEAK_TIMEZONE_FETCH_QUOTA_UNITS = 1
PMAX_SEARCH_THEME_LIMIT = 50
PMAX_ASSET_GROUP_LIMIT = 100
PMAX_SEARCH_THEME_TEXT_LIMIT = 80
PMAX_POLICY_SENSITIVE_FALLBACK_TERMS = (
    "alli",
    "orlistat",
    "melatonin",
    "yohimbe",
    "yohimbine",
    "brimonidine",
    "lumify",
    "enterosgel",
    "akkermansia",
    "dhea",
    "soursop",
    "graviola",
    "zhong gan ling",
    "lipo rush",
    "liporush",
)
SCALE_NEGATIVE_MIGRATION_HOLD_DAYS = 3
SCALE_PAGE_MIGRATION_HOLD_DAYS = 3
WASTE_RECOVERY_HOLD_DAYS = 7
WASTE_FIXED_DAILY_BUDGET_AMOUNT = 50.0
WASTE_FIXED_DAILY_BUDGET_BY_CURRENCY = {
    "INR": 4000.0,
}
MINIMUM_DAILY_BUDGET_BY_CURRENCY = {
    "INR": 1000.0,
}
BUDGET_BOOTSTRAP_DAYS = 3
WASTE_KEYWORD_PLAN_LIMIT = 1000
WASTE_LANDING_PAGE_PLAN_LIMIT = 1000
WASTE_PAGE_HIGH_CLICKS = 20
WASTE_PAGE_HIGH_COST = 30.0
WASTE_PAGE_LOW_CTR_IMPRESSIONS = 1000
WASTE_PAGE_LOW_CTR_RATE = 0.002


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def currency_minimum_daily_budget(account: GoogleAdsAccount | None, configured: Any = None) -> float:
    """Return the account-currency floor used for first-run and forced-minimum budgets."""
    try:
        configured_amount = max(float(configured if configured is not None else 0.0), 0.0)
    except (TypeError, ValueError):
        configured_amount = 0.0
    currency_code = str(getattr(account, "currency_code", "") or "").upper()
    currency_floor = MINIMUM_DAILY_BUDGET_BY_CURRENCY.get(currency_code, 10.0)
    return max(configured_amount, currency_floor)


def waste_fixed_daily_budget(account: GoogleAdsAccount | None) -> float:
    currency_code = str(getattr(account, "currency_code", "") or "").upper()
    configured = WASTE_FIXED_DAILY_BUDGET_BY_CURRENCY.get(currency_code, WASTE_FIXED_DAILY_BUDGET_AMOUNT)
    return currency_minimum_daily_budget(account, configured)


def _asset_publish_quota_retry_state(session: Session, account: GoogleAdsAccount, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or utcnow()
    rows = []
    setting = session.scalar(select(AppSetting).where(AppSetting.key == f"{ASSET_PUBLISH_QUOTA_PREFIX}.{account.customer_id}"))
    if setting is not None:
        rows.append(setting)
    if setting is None and hasattr(session, "scalars"):
        rows.extend(
            item
            for item in session.scalars(select(AppSetting).where(AppSetting.key.like(f"{ASSET_PUBLISH_QUOTA_PREFIX}.%"))).all()
            if _asset_publish_quota_setting_matches_account(item, account)
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
        "reason": quota_setting.value.get("reason") or "Google Ads API asset publish quota retry window is active.",
        "quota_key": quota_setting.key,
    }


def _account_developer_token_hash(account: GoogleAdsAccount) -> str:
    token = ""
    if account.connection is not None:
        token = str(account.connection.developer_token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def _asset_publish_quota_setting_matches_account(setting: AppSetting, account: GoogleAdsAccount) -> bool:
    if setting.key == f"{ASSET_PUBLISH_QUOTA_PREFIX}.{account.customer_id}":
        return True
    value = setting.value if isinstance(setting.value, dict) else {}
    if value.get("connection_id") and account.connection_id and int(value["connection_id"]) == int(account.connection_id):
        return True
    token_hash = _account_developer_token_hash(account)
    if token_hash and value.get("developer_token_hash") == token_hash:
        return True
    return False


def _google_ads_api_quota_retry_state(session: Session, account: GoogleAdsAccount, now: datetime | None = None) -> dict[str, Any] | None:
    retry = _asset_publish_quota_retry_state(session, account, now=now)
    if not retry:
        return None
    return {
        **retry,
        "scope": "google_ads_api",
        "reason": retry.get("reason") or "Google Ads API quota retry window is active.",
    }


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, low), high)


def numeric_value(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def schedule_time_label(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"


def safe_zoneinfo(name: str) -> Any:
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError:
        return timezone.utc


def latest_time_segments_snapshot(session: Session, account: GoogleAdsAccount) -> Optional[GoogleAdsDataSnapshot]:
    return session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == DATASET_TIME_SEGMENTS,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )


def rows_from_snapshot(snapshot: Optional[GoogleAdsDataSnapshot]) -> list[dict[str, Any]]:
    if snapshot is None or not isinstance(snapshot.payload_json, dict):
        return []
    rows = snapshot.payload_json.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def fetch_account_time_zone(session: Session, account: GoogleAdsAccount) -> str:
    values = get_sync_setting_map(session)
    client = build_client(values, account.manager_customer_id, account.connection)
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer.time_zone
        FROM customer
        LIMIT 1
    """
    for row in service.search(customer_id=account.customer_id, query=query):
        time_zone = str(row.customer.time_zone or "").strip()
        if time_zone:
            return time_zone
    return ""


def micros_to_currency(value: int) -> float:
    return int(value or 0) / 1_000_000


def peak_budget_state_key(account: GoogleAdsAccount) -> str:
    return f"{PEAK_BUDGET_STATE_PREFIX}.{account.customer_id}"


def load_peak_budget_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == peak_budget_state_key(account)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def save_peak_budget_state(session: Session, account: GoogleAdsAccount, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=peak_budget_state_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} peak budget state",
        help_text="Original and boosted budget values used to restore peak-time automation changes.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value},
    )
    session.execute(stmt)


def campaign_bootstrap_state_key(account: GoogleAdsAccount) -> str:
    return f"{CAMPAIGN_BOOTSTRAP_STATE_PREFIX}.{account.customer_id}"


def load_campaign_bootstrap_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == campaign_bootstrap_state_key(account)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def save_campaign_bootstrap_state(session: Session, account: GoogleAdsAccount, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=campaign_bootstrap_state_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} campaign bootstrap state",
        help_text="Tracks the 14-day RSA/DSA-only testing bootstrap before normal campaign categories resume.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value},
    )
    session.execute(stmt)


def scale_search_theme_migration_state_key(account: GoogleAdsAccount) -> str:
    return f"{SCALE_SEARCH_THEME_MIGRATION_STATE_PREFIX}.{account.customer_id}"


def load_scale_search_theme_migration_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == scale_search_theme_migration_state_key(account)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def save_scale_search_theme_migration_state(session: Session, account: GoogleAdsAccount, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=scale_search_theme_migration_state_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} scale search-theme migration state",
        help_text="Tracks when Testing winners were first promoted into Core / Scale PMax before they become Testing negatives.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
        },
    )
    session.execute(stmt)


def scale_landing_page_migration_state_key(account: GoogleAdsAccount) -> str:
    return f"{SCALE_LANDING_PAGE_MIGRATION_STATE_PREFIX}.{account.customer_id}"


def waste_recovery_state_key(account: GoogleAdsAccount) -> str:
    return f"{WASTE_RECOVERY_STATE_PREFIX}.{account.customer_id}"


def load_scale_landing_page_migration_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == scale_landing_page_migration_state_key(account)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def save_scale_landing_page_migration_state(session: Session, account: GoogleAdsAccount, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=scale_landing_page_migration_state_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} scale landing-page migration state",
        help_text="Tracks when Testing landing pages were first promoted into Core / Scale before they become Testing page exclusions.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
        },
    )
    session.execute(stmt)


def load_waste_recovery_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == waste_recovery_state_key(account)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def save_waste_recovery_state(session: Session, account: GoogleAdsAccount, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=waste_recovery_state_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} waste recovery state",
        help_text="Tracks quarantined waste terms/pages and the recovery hold before they can return to Testing or Core / Scale.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
        },
    )
    session.execute(stmt)


def audience_signal_inputs_key(account: GoogleAdsAccount) -> str:
    return f"{AUDIENCE_SIGNAL_INPUTS_PREFIX}.{account.customer_id}"


def load_audience_signal_inputs(session: Session, account: Optional[GoogleAdsAccount]) -> dict[str, Any]:
    if account is None:
        return {"manual_similar_urls": [], "manual_interests": []}
    row = session.scalar(select(AppSetting).where(AppSetting.key == audience_signal_inputs_key(account)))
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    return {
        "manual_similar_urls": [str(item or "").strip() for item in (value.get("manual_similar_urls") or []) if str(item or "").strip()],
        "manual_interests": [str(item or "").strip() for item in (value.get("manual_interests") or []) if str(item or "").strip()],
        "updated_at_utc": value.get("updated_at_utc"),
    }


def _split_lines(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = re.split(r"[\n,]+", values)
    elif isinstance(values, list):
        raw_values = values
    else:
        raw_values = []
    return [str(item or "").strip() for item in raw_values if str(item or "").strip()]


def save_audience_signal_inputs(
    session: Session,
    account: GoogleAdsAccount,
    *,
    manual_similar_urls: Any = None,
    manual_interests: Any = None,
) -> dict[str, Any]:
    urls: list[str] = []
    for value in _split_lines(manual_similar_urls):
        cleaned = _clean_similar_url(value)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    interests: list[str] = []
    for value in _split_lines(manual_interests):
        cleaned = _clean_pmax_search_theme(value)
        if cleaned and cleaned.lower() not in {item.lower() for item in interests}:
            interests.append(cleaned)
    state = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "manual_similar_urls": urls[:200],
        "manual_interests": interests[:200],
        "updated_at_utc": utcnow().isoformat(),
    }
    stmt = insert(AppSetting).values(
        key=audience_signal_inputs_key(account),
        value=state,
        category="Automation state",
        label=f"{account.name} audience signal manual inputs",
        help_text="Manual competitor URLs and interest terms merged into PMax custom-segment signal planning.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
        },
    )
    session.execute(stmt)
    return state


def automation_quota_state_key(account: GoogleAdsAccount, now: Optional[datetime] = None) -> str:
    day = (now or utcnow()).astimezone(timezone.utc).date().isoformat()
    return f"{AUTOMATION_QUOTA_STATE_PREFIX}.{account.customer_id}.{day}"


def load_automation_quota_state(session: Session, account: GoogleAdsAccount, now: Optional[datetime] = None) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == automation_quota_state_key(account, now)))
    if row is not None and isinstance(row.value, dict):
        return dict(row.value)
    return {}


def _postgres_advisory_lock(session: Session, key: str) -> bool:
    try:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    except Exception:  # noqa: BLE001 - tests and lightweight sessions may not expose a bind.
        dialect_name = ""
    if dialect_name != "postgresql":
        return False
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    return True


def reserve_automation_quota_units(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    units: int,
    reason: str,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or utcnow()
    account = preference.account
    budget = clamp_int(preference.api_call_budget_per_day, 750, 1, 100000)
    units = clamp_int(units, 1, 1, 100000)
    key = automation_quota_state_key(account, now)
    locked = _postgres_advisory_lock(session, key)
    state = load_automation_quota_state(session, account, now)
    used = int(state.get("used_units") or 0)
    remaining = max(budget - used, 0)
    if units > remaining:
        return {
            "allowed": False,
            "status": "quota_deferred",
            "reason": "Daily automation API budget would be exceeded; work will resume on the next schedule/day.",
            "key": key,
            "locked": locked,
            "budget_units": budget,
            "used_units": used,
            "requested_units": units,
            "remaining_units": remaining,
        }

    reservations = state.get("reservations") if isinstance(state.get("reservations"), list) else []
    reservations.append(
        {
            "reason": reason,
            "units": units,
            "reserved_at": now.astimezone(timezone.utc).isoformat(),
        }
    )
    state = {
        **state,
        "date": now.astimezone(timezone.utc).date().isoformat(),
        "account_id": account.id,
        "customer_id": account.customer_id,
        "budget_units": budget,
        "used_units": used + units,
        "remaining_units": max(budget - used - units, 0),
        "reservations": reservations[-80:],
    }
    stmt = insert(AppSetting).values(
        key=key,
        value=state,
        category="Automation state",
        label=f"{account.name} automation API quota {state['date']}",
        help_text="Conservative per-account quota ledger for Google Ads automation pull and planning blocks.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
        },
    )
    session.execute(stmt)
    session.commit()
    return {
        "allowed": True,
        "status": "reserved",
        "key": key,
        "locked": locked,
        "budget_units": budget,
        "used_units": used + units,
        "requested_units": units,
        "remaining_units": max(budget - used - units, 0),
    }


def recent_account_metric_totals(
    session: Session,
    account: GoogleAdsAccount,
    *,
    days: int = 7,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    days = clamp_int(days, 7, 1, 90)
    today = (now or utcnow()).astimezone(timezone.utc).date()
    start_date = today - timedelta(days=days - 1)
    row = session.execute(
        select(
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.impressions), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.clicks), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.all_conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions_value), 0.0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.all_conversions_value), 0.0),
            func.max(GoogleAdsAccountDailyMetric.metric_date),
            func.max(GoogleAdsAccountDailyMetric.synced_at),
            func.count(func.distinct(GoogleAdsAccountDailyMetric.metric_date)),
        ).where(
            GoogleAdsAccountDailyMetric.account_id == account.id,
            GoogleAdsAccountDailyMetric.metric_date >= start_date,
        )
    ).one()
    conversions = float(row[3] or 0) + float(row[4] or 0)
    conversion_value = float(row[5] or 0) + float(row[6] or 0)
    return {
        "days": days,
        "start_date": start_date.isoformat(),
        "end_date": today.isoformat(),
        "latest_metric_date": row[7].isoformat() if row[7] else None,
        "impressions": int(row[0] or 0),
        "clicks": int(row[1] or 0),
        "cost_micros": int(row[2] or 0),
        "conversions": conversions,
        "conversion_value": conversion_value,
        "latest_synced_at": row[8].isoformat() if row[8] else None,
        "metric_day_count": int(row[9] or 0),
    }


def _as_utc_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def automation_budget_bootstrap_state(
    session: Optional[Session],
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
    ensure: bool = False,
) -> dict[str, Any]:
    now = now or utcnow()
    account = preference.account
    days = BUDGET_BOOTSTRAP_DAYS
    key = f"{BUDGET_BOOTSTRAP_STATE_PREFIX}.{account.customer_id}"
    if session is None or not hasattr(session, "scalar"):
        return {
            "name": "automation_budget_bootstrap",
            "status": "untracked",
            "active": False,
            "reason": "No database session is available to read bootstrap state.",
            "days": days,
        }
    setting = session.scalar(select(AppSetting).where(AppSetting.key == key))
    value = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    started_at = _as_utc_datetime(value.get("started_at"))
    created = False
    if started_at is None and ensure:
        started_at = now
        created = True
        value = {
            "customer_id": account.customer_id,
            "account_id": account.id,
            "started_at": started_at.isoformat(),
            "days": days,
            "reason": "First automation launch budget window uses last-7-day Odoo sales before spend comparison guard starts.",
        }
        stmt = insert(AppSetting).values(
            key=key,
            value=value,
            category="google_ads_automation",
            label=f"{account.name} first-run budget bootstrap",
            help_text="Tracks the first live automation window where budgets are based on recent sales before spend-vs-sales guard reductions start.",
            input_type="json",
            sensitive=False,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": now},
        )
        session.execute(stmt)
        session.commit()
        setting = session.scalar(select(AppSetting).where(AppSetting.key == key))
        if setting is not None and isinstance(setting.value, dict):
            value = setting.value
            started_at = _as_utc_datetime(value.get("started_at")) or started_at
    if started_at is None:
        return {
            "name": "automation_budget_bootstrap",
            "status": "not_started",
            "active": False,
            "reason": "No first-run budget bootstrap marker exists yet.",
            "days": days,
        }
    ends_at = started_at + timedelta(days=days)
    active = now < ends_at
    remaining_hours = max((ends_at - now).total_seconds() / 3600, 0.0)
    return {
        "name": "automation_budget_bootstrap",
        "status": "active" if active else "expired",
        "active": active,
        "created": created,
        "started_at": started_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "remaining_hours": round(remaining_hours, 2),
        "days": days,
        "reason": (
            "First-run budget window is active; budgets use last-7-day Odoo sales without subtracting old campaign spend."
            if active
            else "First-run budget window has ended; normal spend-vs-sales budget guard is active."
        ),
    }


def _snapshot_status_fresh(dataset: dict[str, Any], *, now: datetime, max_age_hours: int) -> bool:
    status = str(dataset.get("status") or "")
    if status == "fetched":
        return True
    if status != "cached":
        return False
    fetched_at = _as_utc_datetime(dataset.get("fetched_at"))
    if fetched_at is None:
        return False
    return fetched_at >= now.astimezone(timezone.utc) - timedelta(hours=max_age_hours)


def ga4_ecommerce_refresh_status(
    session: Session,
    account: GoogleAdsAccount,
    *,
    now: Optional[datetime] = None,
    max_age_hours: int = GA4_ECOMMERCE_REFRESH_MAX_AGE_HOURS,
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    datasets = [
        DATASET_GA4_LANDING_PAGE_ECOMMERCE,
        DATASET_GA4_ITEM_ECOMMERCE,
        DATASET_GA4_TRAFFIC_ECOMMERCE,
        DATASET_GA4_GOOGLE_ADS_SEARCH,
    ]
    rows = session.execute(
        select(
            GoogleAnalyticsDataSnapshot.dataset_key,
            func.max(GoogleAnalyticsDataSnapshot.fetched_at),
            func.count(GoogleAnalyticsDataSnapshot.id),
        )
        .where(
            GoogleAnalyticsDataSnapshot.account_id == account.id,
            GoogleAnalyticsDataSnapshot.dataset_key.in_(datasets),
        )
        .group_by(GoogleAnalyticsDataSnapshot.dataset_key)
    ).all()
    by_dataset = {
        str(row[0]): {
            "latest_fetched_at": row[1].astimezone(timezone.utc).isoformat() if row[1] else None,
            "snapshot_count": int(row[2] or 0),
        }
        for row in rows
    }
    cutoff = now - timedelta(hours=max_age_hours)
    missing = [key for key in datasets if key not in by_dataset]
    stale = []
    for key, item in by_dataset.items():
        latest = _as_utc_datetime(item.get("latest_fetched_at"))
        if latest is None or latest < cutoff:
            stale.append(key)
    return {
        "ok": not missing and not stale,
        "status": "fresh" if not missing and not stale else "stale_or_missing",
        "account_id": account.id,
        "customer_id": account.customer_id,
        "max_age_hours": max_age_hours,
        "missing_datasets": missing,
        "stale_datasets": sorted(stale),
        "datasets": by_dataset,
        "checked_at": now.isoformat(),
    }


def maybe_refresh_ga4_ecommerce_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    source_job_id: Optional[int] = None,
    force: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    status = ga4_ecommerce_refresh_status(session, account, now=now)
    if status["ok"] and not force:
        return {
            "name": "ga4_ecommerce_intent_pull",
            "status": "cached",
            "reason": "GA4 ecommerce snapshots are fresh enough for Ads planning.",
            "freshness": status,
        }
    try:
        result = sync_ga4_ecommerce_snapshots(
            session,
            mode="recent",
            days=7,
            max_rows=5000,
            force=force,
            source_job_id=source_job_id,
            account_ids=[account.id],
        )
        return {
            "name": "ga4_ecommerce_intent_pull",
            "status": "done" if not result.get("error_count") else "partial",
            "reason": "Pulled GA4 ecommerce landing-page, item, traffic, and Google Ads search rows into Postgres.",
            "freshness_before": status,
            "result": {
                "scope_key": result.get("scope_key"),
                "target_count": result.get("target_count"),
                "dataset_count": result.get("dataset_count"),
                "error_count": result.get("error_count"),
                "landing_pages_imported": result.get("landing_pages_imported"),
                "keywords_imported": result.get("keywords_imported"),
                "search_terms_imported": result.get("search_terms_imported"),
                "datasets": result.get("datasets") or [],
                "errors": result.get("errors") or [],
            },
        }
    except Exception as exc:  # noqa: BLE001 - GA4 should not stop Ads safeguards from running.
        session.rollback()
        return {
            "name": "ga4_ecommerce_intent_pull",
            "status": "failed",
            "reason": "GA4 ecommerce pull failed; Ads planning will continue with saved GA4/Ads data only.",
            "freshness_before": status,
            "error": str(exc)[:500],
        }


def campaign_planning_data_freshness(
    session: Session,
    account: GoogleAdsAccount,
    *,
    research: Optional[dict[str, Any]],
    metrics_refresh_ok: bool,
    now: Optional[datetime] = None,
    max_age_hours: int = MAX_AUTOMATION_DATA_AGE_HOURS,
) -> dict[str, Any]:
    now = now or utcnow()
    metrics = recent_account_metric_totals(session, account, days=7, now=now)
    latest_metric_date_raw = metrics.get("latest_metric_date")
    latest_metric_date = date.fromisoformat(latest_metric_date_raw) if latest_metric_date_raw else None
    latest_synced_at = _as_utc_datetime(metrics.get("latest_synced_at"))
    metric_cutoff_date = now.astimezone(timezone.utc).date() - timedelta(days=1)
    sync_cutoff = now.astimezone(timezone.utc) - timedelta(hours=max_age_hours)
    fresh_zero_metrics = bool(metrics_refresh_ok and int(metrics.get("metric_day_count") or 0) == 0)
    metrics_fresh = bool(
        metrics_refresh_ok
        and (
            fresh_zero_metrics
            or (
                latest_metric_date is not None
                and latest_metric_date >= metric_cutoff_date
                and latest_synced_at is not None
                and latest_synced_at >= sync_cutoff
            )
        )
    )

    dataset_rows = research.get("datasets") if isinstance(research, dict) else []
    if not isinstance(dataset_rows, list):
        dataset_rows = []
    datasets = {
        str(row.get("dataset_key") or ""): row
        for row in dataset_rows
        if isinstance(row, dict) and row.get("dataset_key")
    }
    errors = research.get("errors") if isinstance(research, dict) else []
    if not isinstance(errors, list):
        errors = []
    error_keys = {
        str(item.get("dataset_key") or "")
        for item in errors
        if isinstance(item, dict) and item.get("dataset_key")
    }
    critical_error_keys = sorted(CAMPAIGN_PLANNING_REQUIRED_DATASETS.intersection(error_keys))
    missing_required = sorted(key for key in CAMPAIGN_PLANNING_REQUIRED_DATASETS if key not in datasets)
    stale_required = sorted(
        key
        for key in CAMPAIGN_PLANNING_REQUIRED_DATASETS
        if key in datasets and not _snapshot_status_fresh(datasets[key], now=now, max_age_hours=max_age_hours)
    )
    keyword_dataset_keys = sorted(key for key in CAMPAIGN_PLANNING_KEYWORD_DATASETS if key in datasets)
    keyword_dataset_fresh = any(
        _snapshot_status_fresh(datasets[key], now=now, max_age_hours=max_age_hours)
        for key in keyword_dataset_keys
    )

    problems: list[str] = []
    if not metrics_fresh:
        problems.append("7-day campaign metrics are not fresh enough for no-impression/PMax decisions.")
    if missing_required:
        problems.append("Missing current campaign or landing-page insight datasets.")
    if stale_required:
        problems.append("Current campaign or landing-page insight datasets are stale.")
    if not keyword_dataset_fresh:
        problems.append("No fresh search-term, search-term-insight, or AI Max term combination dataset is available.")
    if critical_error_keys:
        problems.append("Decision-critical Google insight datasets returned errors.")

    return {
        "ok": not problems,
        "status": "fresh" if not problems else "stale_or_incomplete",
        "reason": "Fresh enough for campaign planning." if not problems else " ".join(problems),
        "max_age_hours": max_age_hours,
        "metrics_refresh_ok": metrics_refresh_ok,
        "fresh_zero_metrics": fresh_zero_metrics,
        "metrics": metrics,
        "required_datasets": sorted(CAMPAIGN_PLANNING_REQUIRED_DATASETS),
        "keyword_datasets_present": keyword_dataset_keys,
        "missing_required_datasets": missing_required,
        "stale_required_datasets": stale_required,
        "critical_error_datasets": critical_error_keys,
    }


def mapped_website_url_for_account(session: Session, account: GoogleAdsAccount) -> str:
    row = session.execute(
        select(OdooStoreGoogleAdsMapping, OdooWebsite)
        .outerjoin(
            OdooWebsite,
            (OdooWebsite.store_id == OdooStoreGoogleAdsMapping.store_id)
            & (OdooWebsite.website_id == OdooStoreGoogleAdsMapping.website_id)
            & (OdooWebsite.is_active.is_(True)),
        )
        .where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
        .order_by(OdooStoreGoogleAdsMapping.website_id.desc(), OdooStoreGoogleAdsMapping.created_at.desc())
        .limit(1)
    ).first()
    if not row:
        return ""
    mapping, website = row
    raw_url = website.domain if website and website.domain else mapping.store.base_url
    text = str(raw_url or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    parsed = urlsplit(text)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")


def _root_domain(host: str) -> str:
    text = str(host or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlsplit(text)
    netloc = parsed.netloc or parsed.path.split("/")[0]
    netloc = netloc.split("@")[-1].split(":")[0].strip(".")
    if netloc.startswith("www."):
        netloc = netloc[4:]
    parts = [part for part in netloc.split(".") if part]
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def _clean_similar_url(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    parsed = urlsplit(text)
    host = (parsed.netloc or parsed.path.split("/")[0]).split("@")[-1].split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return ""
    return f"https://{host}"


def _append_unique_text(values: list[str], value: Any, *, limit: int) -> None:
    if len(values) >= limit:
        return
    text = _clean_pmax_search_theme(value)
    if not text:
        return
    seen = {item.lower() for item in values}
    if text.lower() not in seen:
        values.append(text)


def _append_unique_url(values: list[str], value: Any, *, limit: int) -> None:
    if len(values) >= limit:
        return
    url = _clean_similar_url(value)
    if not url:
        return
    if url not in values:
        values.append(url)


def _snapshot_rows(snapshot: Any) -> list[dict[str, Any]]:
    payload = getattr(snapshot, "payload_json", None)
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _domain_from_source_medium(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    source = text.split("/", 1)[0].strip()
    source = source.replace("organic search", "").strip()
    if not source or source in {"(direct)", "(not set)", "direct", "google", "bing", "yahoo", "duckduckgo"}:
        return ""
    if "." not in source:
        return ""
    return _clean_similar_url(source)


def _latest_ga4_snapshots_for_dataset(
    session: Session,
    account: GoogleAdsAccount,
    dataset_key: str,
    *,
    limit: int = 5,
) -> list[GoogleAnalyticsDataSnapshot]:
    return list(
        session.scalars(
            select(GoogleAnalyticsDataSnapshot)
            .where(
                GoogleAnalyticsDataSnapshot.account_id == account.id,
                GoogleAnalyticsDataSnapshot.dataset_key == dataset_key,
            )
            .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
            .limit(limit)
        ).all()
    )


def _latest_ads_snapshots_for_dataset(
    session: Session,
    account: GoogleAdsAccount,
    dataset_key: str,
    *,
    limit: int = 5,
) -> list[GoogleAdsDataSnapshot]:
    return list(
        session.scalars(
            select(GoogleAdsDataSnapshot)
            .where(
                GoogleAdsDataSnapshot.account_id == account.id,
                GoogleAdsDataSnapshot.dataset_key == dataset_key,
            )
            .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
            .limit(limit)
        ).all()
    )


def _audience_insight_score(row: dict[str, Any]) -> tuple[float, float]:
    share = numeric_value(row, "share_of_conversions") or numeric_value(row, "conversion_share")
    if share > 1:
        share = share / 100
    return (share, numeric_value(row, "index"))


def _audience_segment_resource(row: dict[str, Any]) -> str:
    for key in (
        "resource_name",
        "audience_segment_resource_name",
        "user_interest_resource_name",
        "detailed_demographic_resource_name",
        "life_event_resource_name",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    segment_id = str(row.get("segment_id") or row.get("criterion_id") or row.get("id") or "").strip()
    segment_type = str(row.get("type") or row.get("segment_type") or row.get("audience_segment_type") or "").lower()
    if not segment_id:
        return ""
    if "demographic" in segment_type:
        return f"detailedDemographics/{segment_id}"
    if "life" in segment_type:
        return f"lifeEvents/{segment_id}"
    return f"userInterests/{segment_id}"


def _audience_segment_kind(row: dict[str, Any]) -> str:
    value = str(row.get("type") or row.get("segment_type") or row.get("audience_segment_type") or "").strip().lower()
    if "demographic" in value:
        return "detailed_demographic"
    if "life" in value:
        return "life_event"
    return "user_interest"


def proven_audience_insight_segments(
    session: Session,
    account: GoogleAdsAccount,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    snapshots = _latest_ads_snapshots_for_dataset(session, account, DATASET_AUDIENCE_INSIGHTS, limit=4)
    source = "same_account_audience_insights"
    if not snapshots:
        snapshots = list(
            session.scalars(
                select(GoogleAdsDataSnapshot)
                .where(GoogleAdsDataSnapshot.dataset_key == DATASET_AUDIENCE_INSIGHTS)
                .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
                .limit(12)
            ).all()
        )
        source = "cross_account_audience_insights"
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        for row in rows_from_snapshot(snapshot):
            name = str(
                row.get("audience_segment")
                or row.get("segment_name")
                or row.get("name")
                or row.get("display_name")
                or ""
            ).strip()
            resource = _audience_segment_resource(row)
            key = (resource or name).lower()
            if not name or not key or key in seen:
                continue
            share, index = _audience_insight_score(row)
            if share <= 0 and index <= 0:
                continue
            seen.add(key)
            rows.append(
                {
                    "name": name,
                    "type": _audience_segment_kind(row),
                    "resource_name": resource,
                    "share_of_conversions": share,
                    "index": index,
                    "source": source,
                    "source_account_id": snapshot.account_id,
                    "snapshot_id": snapshot.id,
                    "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
                }
            )
    rows.sort(key=lambda item: (float(item.get("share_of_conversions") or 0), float(item.get("index") or 0)), reverse=True)
    return rows[: max(int(limit or 0), 0)]


def build_audience_signal_plan(
    session: Session,
    account: GoogleAdsAccount,
    *,
    keyword_rows: list[GoogleAdsKeywordCandidate],
    pmax_theme_plan: dict[str, Any],
    ga4_matrix: dict[str, Any],
    website_url: str,
    search_console_matrix: Optional[dict[str, Any]] = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = clamp_int(limit, 50, 5, 100)
    manual = load_audience_signal_inputs(session, account)
    term_limit = limit
    url_limit = limit
    terms: list[str] = []
    term_sources: dict[str, int] = {
        "scale_pmax_search_themes": 0,
        "google_ads_keyword_bank": 0,
        "ga4_search_terms": 0,
        "ga4_item_keywords": 0,
        "search_console_queries": 0,
        "manual_interests": 0,
    }
    for item in pmax_theme_plan.get("active_terms") or []:
        before = len(terms)
        _append_unique_text(terms, item, limit=term_limit)
        term_sources["scale_pmax_search_themes"] += int(len(terms) > before)
    for row in keyword_rows[:100]:
        before = len(terms)
        _append_unique_text(terms, getattr(row, "keyword", ""), limit=term_limit)
        term_sources["google_ads_keyword_bank"] += int(len(terms) > before)
    ga4_terms = session.scalars(
        select(GoogleAnalyticsSearchTermCandidate)
        .where(
            GoogleAnalyticsSearchTermCandidate.account_id == account.id,
            GoogleAnalyticsSearchTermCandidate.quality_label.in_(["revenue", "converting", "clicked"]),
            GoogleAnalyticsSearchTermCandidate.review_status != "rejected",
        )
        .order_by(
            GoogleAnalyticsSearchTermCandidate.score.desc(),
            GoogleAnalyticsSearchTermCandidate.purchases.desc(),
            GoogleAnalyticsSearchTermCandidate.revenue.desc(),
            GoogleAnalyticsSearchTermCandidate.engaged_sessions.desc(),
            GoogleAnalyticsSearchTermCandidate.sessions.desc(),
        )
        .limit(100)
    ).all()
    for row in ga4_terms:
        before = len(terms)
        _append_unique_text(terms, getattr(row, "search_term", ""), limit=term_limit)
        term_sources["ga4_search_terms"] += int(len(terms) > before)
    for bucket_name in ("scale_item_keywords", "testing_item_keywords"):
        for item in (ga4_matrix.get(bucket_name) or []):
            if not isinstance(item, dict):
                continue
            before = len(terms)
            _append_unique_text(terms, item.get("keyword"), limit=term_limit)
            term_sources["ga4_item_keywords"] += int(len(terms) > before)
    if isinstance(search_console_matrix, dict):
        for bucket_name in ("core_queries", "testing_queries"):
            for item in (search_console_matrix.get(bucket_name) or []):
                if not isinstance(item, dict):
                    continue
                before = len(terms)
                _append_unique_text(terms, item.get("query"), limit=term_limit)
                term_sources["search_console_queries"] += int(len(terms) > before)
    for item in manual.get("manual_interests") or []:
        before = len(terms)
        _append_unique_text(terms, item, limit=term_limit)
        term_sources["manual_interests"] += int(len(terms) > before)

    similar_urls: list[str] = []
    url_sources: dict[str, int] = {"manual": 0, "ga4_referral_sources": 0, "ads_auction_snapshots": 0}
    own_root = _root_domain(website_url)
    for item in manual.get("manual_similar_urls") or []:
        before = len(similar_urls)
        _append_unique_url(similar_urls, item, limit=url_limit)
        url_sources["manual"] += int(len(similar_urls) > before)
    for snapshot in _latest_ga4_snapshots_for_dataset(session, account, DATASET_GA4_TRAFFIC_ECOMMERCE, limit=8):
        for row in _snapshot_rows(snapshot):
            url = _domain_from_source_medium(row.get("sessionSourceMedium"))
            if not url:
                continue
            root = _root_domain(url)
            if not root or root == own_root:
                continue
            before = len(similar_urls)
            _append_unique_url(similar_urls, url, limit=url_limit)
            url_sources["ga4_referral_sources"] += int(len(similar_urls) > before)
    for snapshot in _latest_ads_snapshots_for_dataset(session, account, DATASET_AUCTION_INSIGHTS_PROXY, limit=8):
        for row in _snapshot_rows(snapshot):
            for key in ("display_url_domain", "domain", "competitor_domain", "auction_insight_domain", "website"):
                url = row.get(key)
                if not url:
                    continue
                root = _root_domain(str(url))
                if not root or root == own_root:
                    continue
                before = len(similar_urls)
                _append_unique_url(similar_urls, url, limit=url_limit)
                url_sources["ads_auction_snapshots"] += int(len(similar_urls) > before)

    google_audience_segments = proven_audience_insight_segments(session, account, limit=20)
    for segment in google_audience_segments:
        before = len(terms)
        _append_unique_text(terms, segment.get("name"), limit=term_limit)
        term_sources.setdefault("google_audience_insights", 0)
        term_sources["google_audience_insights"] += int(len(terms) > before)

    return {
        "enabled": bool(terms or similar_urls),
        "scope": "pmax_asset_group_audience_signal_plan",
        "custom_segment_terms": terms,
        "custom_segment_term_count": len(terms),
        "similar_website_urls": similar_urls,
        "similar_website_url_count": len(similar_urls),
        "google_audience_segments": google_audience_segments,
        "google_audience_segment_count": len(google_audience_segments),
        "manual_interests": manual.get("manual_interests") or [],
        "manual_similar_urls": manual.get("manual_similar_urls") or [],
        "sources": {
            "terms": term_sources,
            "similar_urls": url_sources,
            "ga4_search_terms_considered": len(ga4_terms),
            "search_console_queries_considered": int(
                ((search_console_matrix or {}).get("summary") or {}).get("candidate_count") or 0
            )
            if isinstance(search_console_matrix, dict)
            else 0,
            "manual_updated_at_utc": manual.get("updated_at_utc"),
        },
        "controls": [
            "PMax audience signals are hints for machine learning, not hard audience targeting.",
            "Manual competitor URLs are used only as custom-segment similar-website hints.",
            "Saved GA4 and Google Ads rows are reused first; scheduled collectors refresh the bank instead of making planning-time API calls.",
            "Search Console clicked queries help Core/Scale relevance; high-impression unclicked queries remain Testing evidence until sales and Ads data agree.",
        ],
        "basis": (
            "Builds PMax asset-group audience-signal inputs from Scale search themes, saved Google Ads keyword-bank terms, "
            "GA4 Google Ads search terms, GA4 ecommerce item keywords, GA4 referral/source domains, Ads auction-domain fields "
            "when present, Search Console query/page rows, and manual competitor/interest inputs."
        ),
    }


def _row_total_conversions(row: Any) -> float:
    return float(getattr(row, "conversions", 0) or 0) + float(getattr(row, "all_conversions", 0) or 0)


def _row_total_value(row: Any) -> float:
    return float(getattr(row, "conversion_value", 0) or 0) + float(getattr(row, "all_conversions_value", 0) or 0)


def _row_source_json(row: Any) -> dict[str, Any]:
    try:
        if "source_json" in sa_inspect(row).unloaded:
            return {}
    except Exception:  # noqa: BLE001 - non-SQLAlchemy rows can still expose source_json.
        pass
    value = getattr(row, "source_json", None)
    return value if isinstance(value, dict) else {}


def _criteria_planning_window(session: Session) -> int:
    try:
        value = int(
            session.scalar(
                select(AppSetting.value).where(AppSetting.key == CRITERIA_DAILY_ITEM_LIMIT_SETTING).limit(1)
            )
            or DEFAULT_CRITERIA_PLANNING_WINDOW
        )
    except Exception:  # noqa: BLE001 - planning must remain usable if settings are unavailable.
        value = DEFAULT_CRITERIA_PLANNING_WINDOW
    return max(value, 1)


def best_keyword_candidates_for_testing(
    session: Session,
    account: GoogleAdsAccount,
    *,
    limit: Optional[int] = DEFAULT_TESTING_KEYWORD_LIMIT,
    allow_cross_account_fallback: bool = True,
) -> tuple[list[GoogleAdsKeywordCandidate], str]:
    target_limit = int(limit or 0)
    if target_limit <= 0:
        target_limit = _criteria_planning_window(session)
    quality_order = ["revenue", "converting", "clicked"]
    query = (
        select(GoogleAdsKeywordCandidate)
        .options(
            defer(GoogleAdsKeywordCandidate.source_json),
            noload(GoogleAdsKeywordCandidate.account),
            noload(GoogleAdsKeywordCandidate.last_source_job),
        )
        .where(
            GoogleAdsKeywordCandidate.account_id == account.id,
            GoogleAdsKeywordCandidate.quality_label.in_(quality_order),
            GoogleAdsKeywordCandidate.review_status != "rejected",
        )
        .order_by(
            GoogleAdsKeywordCandidate.score.desc(),
            GoogleAdsKeywordCandidate.conversions.desc(),
            GoogleAdsKeywordCandidate.clicks.desc(),
        )
    )
    if target_limit:
        query = query.limit(target_limit)
    rows = session.scalars(query).all()
    source = "same_account_keyword_bank"
    # Cross-account keywords are only a bootstrap fallback for genuinely new
    # accounts. Once an account has its own usable keyword bank, keep targeting
    # country/site-specific instead of making mature accounts converge.
    if allow_cross_account_fallback and not rows:
        seen = {row.normalized_keyword for row in rows}
        fallback_query = (
            select(GoogleAdsKeywordCandidate)
            .options(
                defer(GoogleAdsKeywordCandidate.source_json),
                noload(GoogleAdsKeywordCandidate.account),
                noload(GoogleAdsKeywordCandidate.last_source_job),
            )
            .where(
                GoogleAdsKeywordCandidate.account_id != account.id,
                GoogleAdsKeywordCandidate.quality_label.in_(quality_order),
                GoogleAdsKeywordCandidate.review_status != "rejected",
            )
            .order_by(
                GoogleAdsKeywordCandidate.score.desc(),
                GoogleAdsKeywordCandidate.conversions.desc(),
                GoogleAdsKeywordCandidate.clicks.desc(),
            )
        )
        if target_limit:
            fallback_query = fallback_query.limit(target_limit * 3)
        fallback = session.scalars(fallback_query).all()
        for row in fallback:
            if row.normalized_keyword in seen:
                continue
            rows.append(row)
            seen.add(row.normalized_keyword)
            if target_limit and len(rows) >= target_limit:
                break
        if len(rows) > len(seen):
            source = "mixed_keyword_bank"
        elif fallback:
            source = "same_account_then_cross_account_keyword_bank"
    return list(rows[:target_limit] if target_limit else rows), source


def best_landing_page_candidates_for_testing(
    session: Session,
    account: GoogleAdsAccount,
    *,
    website_url: str = "",
    limit: Optional[int] = DEFAULT_TESTING_LANDING_PAGE_LIMIT,
) -> tuple[list[GoogleAdsLandingPageCandidate], str]:
    target_limit = int(limit or 0)
    if target_limit <= 0:
        target_limit = _criteria_planning_window(session)
    quality_order = ["revenue", "converting", "clicked", "watch"]
    query = (
        select(GoogleAdsLandingPageCandidate)
        .options(
            defer(GoogleAdsLandingPageCandidate.source_json),
            noload(GoogleAdsLandingPageCandidate.account),
            noload(GoogleAdsLandingPageCandidate.last_source_job),
        )
        .where(
            GoogleAdsLandingPageCandidate.account_id == account.id,
            GoogleAdsLandingPageCandidate.quality_label.in_(quality_order),
            GoogleAdsLandingPageCandidate.review_status != "rejected",
        )
        .order_by(
            GoogleAdsLandingPageCandidate.score.desc(),
            GoogleAdsLandingPageCandidate.conversions.desc(),
            GoogleAdsLandingPageCandidate.clicks.desc(),
        )
    )
    if target_limit:
        query = query.limit(target_limit)
    rows = session.scalars(query).all()
    source = "same_account_landing_page_bank"
    if website_url and not rows:
        host_root = _root_domain(urlsplit(website_url).netloc)
        seen = {row.normalized_url_hash for row in rows}
        fallback_query = (
            select(GoogleAdsLandingPageCandidate)
            .options(
                defer(GoogleAdsLandingPageCandidate.source_json),
                noload(GoogleAdsLandingPageCandidate.account),
                noload(GoogleAdsLandingPageCandidate.last_source_job),
            )
            .where(
                GoogleAdsLandingPageCandidate.account_id != account.id,
                GoogleAdsLandingPageCandidate.quality_label.in_(quality_order),
                GoogleAdsLandingPageCandidate.review_status != "rejected",
            )
            .order_by(
                GoogleAdsLandingPageCandidate.score.desc(),
                GoogleAdsLandingPageCandidate.conversions.desc(),
                GoogleAdsLandingPageCandidate.clicks.desc(),
            )
        )
        if target_limit:
            fallback_query = fallback_query.limit(target_limit * 5)
        fallback = session.scalars(fallback_query).all()
        for row in fallback:
            candidate_root = _root_domain(urlsplit(row.normalized_url or row.url or "").netloc)
            if row.normalized_url_hash in seen or not candidate_root or candidate_root != host_root:
                continue
            rows.append(row)
            seen.add(row.normalized_url_hash)
            if target_limit and len(rows) >= target_limit:
                break
        if fallback:
            source = "same_domain_cross_account_landing_page_bank"
    return list(rows[:target_limit] if target_limit else rows), source


def negative_keyword_candidates_for_waste_review(
    session: Session,
    account: GoogleAdsAccount,
    *,
    limit: int = WASTE_KEYWORD_PLAN_LIMIT,
) -> list[GoogleAdsNegativeKeywordCandidate]:
    limit = clamp_int(limit, WASTE_KEYWORD_PLAN_LIMIT, 1, 5000)
    return list(
        session.scalars(
            select(GoogleAdsNegativeKeywordCandidate)
            .where(
                GoogleAdsNegativeKeywordCandidate.account_id == account.id,
                GoogleAdsNegativeKeywordCandidate.review_status != "rejected",
                GoogleAdsNegativeKeywordCandidate.guard_status != "released",
            )
            .order_by(
                GoogleAdsNegativeKeywordCandidate.confidence.desc(),
                GoogleAdsNegativeKeywordCandidate.score.desc(),
                GoogleAdsNegativeKeywordCandidate.cost.desc(),
                GoogleAdsNegativeKeywordCandidate.clicks.desc(),
                GoogleAdsNegativeKeywordCandidate.last_seen_at.desc(),
            )
            .limit(limit)
        ).all()
    )


def keyword_candidates_for_waste_review(
    session: Session,
    account: GoogleAdsAccount,
    *,
    limit: int = WASTE_KEYWORD_PLAN_LIMIT,
) -> list[GoogleAdsKeywordCandidate]:
    limit = clamp_int(limit, WASTE_KEYWORD_PLAN_LIMIT, 1, 5000)
    return list(
        session.scalars(
            select(GoogleAdsKeywordCandidate)
            .options(
                defer(GoogleAdsKeywordCandidate.source_json),
                noload(GoogleAdsKeywordCandidate.account),
                noload(GoogleAdsKeywordCandidate.last_source_job),
            )
            .where(
                GoogleAdsKeywordCandidate.account_id == account.id,
                GoogleAdsKeywordCandidate.review_status != "rejected",
            )
            .order_by(
                GoogleAdsKeywordCandidate.updated_at.desc(),
                GoogleAdsKeywordCandidate.conversions.desc(),
                GoogleAdsKeywordCandidate.all_conversions.desc(),
                GoogleAdsKeywordCandidate.conversion_value.desc(),
                GoogleAdsKeywordCandidate.clicks.desc(),
            )
            .limit(limit)
        ).all()
    )


def landing_page_candidates_for_waste_review(
    session: Session,
    account: GoogleAdsAccount,
    *,
    limit: int = WASTE_LANDING_PAGE_PLAN_LIMIT,
) -> list[GoogleAdsLandingPageCandidate]:
    limit = clamp_int(limit, WASTE_LANDING_PAGE_PLAN_LIMIT, 1, 5000)
    return list(
        session.scalars(
            select(GoogleAdsLandingPageCandidate)
            .options(
                defer(GoogleAdsLandingPageCandidate.source_json),
                noload(GoogleAdsLandingPageCandidate.account),
                noload(GoogleAdsLandingPageCandidate.last_source_job),
            )
            .where(
                GoogleAdsLandingPageCandidate.account_id == account.id,
                GoogleAdsLandingPageCandidate.review_status != "rejected",
            )
            .order_by(
                GoogleAdsLandingPageCandidate.updated_at.desc(),
                GoogleAdsLandingPageCandidate.conversions.desc(),
                GoogleAdsLandingPageCandidate.all_conversions.desc(),
                GoogleAdsLandingPageCandidate.conversion_value.desc(),
                GoogleAdsLandingPageCandidate.cost.desc(),
                GoogleAdsLandingPageCandidate.clicks.desc(),
            )
            .limit(limit)
        ).all()
    )


def _latest_row_time(row: Any) -> Optional[datetime]:
    values = [
        _as_utc_datetime(getattr(row, "last_seen_at", None)),
        _as_utc_datetime(getattr(row, "last_pulled_at", None)),
        _as_utc_datetime(getattr(row, "updated_at", None)),
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _fresher_than(source: Any, baseline: Any) -> bool:
    source_time = _latest_row_time(source)
    baseline_time = _latest_row_time(baseline)
    if source_time is None:
        return False
    return baseline_time is None or source_time >= baseline_time


def _keyword_recovery_evidence(
    negative: GoogleAdsNegativeKeywordCandidate,
    positive: Optional[GoogleAdsKeywordCandidate],
) -> dict[str, Any]:
    if positive is None:
        return {"action": "", "fresh": False, "reason": "No positive keyword-bank evidence exists yet."}
    conversions = _row_total_conversions(positive)
    conversion_value = _row_total_value(positive)
    impressions = int(getattr(positive, "impressions", 0) or 0)
    clicks = int(getattr(positive, "clicks", 0) or 0)
    ctr = (clicks / impressions) if impressions > 0 else 0.0
    fresh = _fresher_than(positive, negative)
    base = {
        "keyword_candidate_id": getattr(positive, "id", None),
        "fresh": fresh,
        "quality_label": getattr(positive, "quality_label", ""),
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(ctr, 6),
        "conversions": conversions,
        "conversion_value": conversion_value,
        "last_seen_at": getattr(positive, "last_seen_at", None).isoformat() if getattr(positive, "last_seen_at", None) else None,
    }
    if fresh and (conversions > 0 or conversion_value > 0):
        return {
            **base,
            "action": "scale",
            "reason": "Fresh purchase/conversion value appeared after this term entered Waste.",
        }
    if (
        fresh
        and str(getattr(negative, "reason_label", "") or "") == "low_ctr_no_conversion"
        and clicks >= 10
        and ctr >= 0.005
    ):
        return {
            **base,
            "action": "testing_review",
            "reason": "Fresh click/CTR evidence improved a previous low-CTR waste term; manual Testing review is allowed.",
        }
    return {
        **base,
        "action": "",
        "reason": "Evidence is not strong enough to release this term from Waste.",
    }


def _waste_page_reason(row: GoogleAdsLandingPageCandidate) -> tuple[str, float]:
    if str(getattr(row, "review_status", "") or "").lower() == "approved":
        return "", 0.0
    if _row_total_conversions(row) > 0 or _row_total_value(row) > 0:
        return "", 0.0
    clicks = int(getattr(row, "clicks", 0) or 0)
    impressions = int(getattr(row, "impressions", 0) or 0)
    cost = float(getattr(row, "cost", 0) or 0)
    ctr = (clicks / impressions) if impressions > 0 else 0.0
    if cost >= WASTE_PAGE_HIGH_COST:
        return "zero_conversion_page_cost_waste", 0.86
    if clicks >= WASTE_PAGE_HIGH_CLICKS:
        return "zero_conversion_page_click_waste", 0.78
    if impressions >= WASTE_PAGE_LOW_CTR_IMPRESSIONS and ctr <= WASTE_PAGE_LOW_CTR_RATE:
        return "low_ctr_page_no_conversion", 0.58
    return "", 0.0


def _page_recovery_evidence(row: GoogleAdsLandingPageCandidate, state: dict[str, Any]) -> dict[str, Any]:
    conversions = _row_total_conversions(row)
    conversion_value = _row_total_value(row)
    quarantined_at = parse_iso_datetime(state.get("quarantined_at_utc")) if state else None
    row_time = _latest_row_time(row)
    fresh = bool(row_time is not None and (quarantined_at is None or row_time >= quarantined_at))
    if fresh and (conversions > 0 or conversion_value > 0):
        return {
            "action": "scale",
            "reason": "Fresh landing-page conversion evidence appeared after this URL entered Waste.",
            "landing_page_candidate_id": getattr(row, "id", None),
            "quality_label": getattr(row, "quality_label", ""),
            "fresh": fresh,
            "conversions": conversions,
            "conversion_value": conversion_value,
            "last_seen_at": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else None,
        }
    if state and fresh and not _waste_page_reason(row)[0] and int(getattr(row, "clicks", 0) or 0) >= 10:
        return {
            "action": "testing_review",
            "reason": "The URL no longer meets Waste thresholds and has fresh click evidence; manual DSA/PMax Testing review is allowed.",
            "landing_page_candidate_id": getattr(row, "id", None),
            "quality_label": getattr(row, "quality_label", ""),
            "fresh": fresh,
            "conversions": conversions,
            "conversion_value": conversion_value,
            "last_seen_at": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else None,
        }
    return {
        "action": "",
        "reason": "Landing-page evidence is not strong enough to release this URL from Waste.",
        "landing_page_candidate_id": getattr(row, "id", None),
        "quality_label": getattr(row, "quality_label", ""),
        "fresh": fresh,
        "conversions": conversions,
        "conversion_value": conversion_value,
    }


def waste_category_plan(
    negative_rows: list[GoogleAdsNegativeKeywordCandidate],
    keyword_rows: list[GoogleAdsKeywordCandidate],
    page_rows: list[GoogleAdsLandingPageCandidate],
    *,
    recovery_state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    hold_days: int = WASTE_RECOVERY_HOLD_DAYS,
    currency_code: str = "USD",
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    now_iso = now.isoformat()
    hold_days = clamp_int(hold_days, WASTE_RECOVERY_HOLD_DAYS, 1, 60)
    state = recovery_state if isinstance(recovery_state, dict) else {}
    state_terms = state.get("terms") if isinstance(state.get("terms"), dict) else {}
    state_pages = state.get("pages") if isinstance(state.get("pages"), dict) else {}
    updated_terms: dict[str, Any] = dict(state_terms)
    updated_pages: dict[str, Any] = dict(state_pages)

    positives: dict[str, GoogleAdsKeywordCandidate] = {}
    for row in keyword_rows:
        key = str(getattr(row, "normalized_keyword", "") or _theme_key(getattr(row, "keyword", ""))).strip().lower()
        if not key:
            continue
        existing = positives.get(key)
        if existing is None or (
            _row_total_conversions(row),
            _row_total_value(row),
            int(getattr(row, "clicks", 0) or 0),
            float(getattr(row, "score", 0) or 0),
        ) > (
            _row_total_conversions(existing),
            _row_total_value(existing),
            int(getattr(existing, "clicks", 0) or 0),
            float(getattr(existing, "score", 0) or 0),
        ):
            positives[key] = row

    active_negative_keywords: list[dict[str, Any]] = []
    pending_keyword_recovery: list[dict[str, Any]] = []
    scale_keyword_recovery: list[dict[str, Any]] = []
    testing_keyword_recovery: list[dict[str, Any]] = []
    for row in negative_rows:
        keyword = str(getattr(row, "keyword", "") or "").strip()
        key = str(getattr(row, "normalized_keyword", "") or _theme_key(keyword)).strip().lower()
        if not keyword or not key:
            continue
        existing = updated_terms.get(key) if isinstance(updated_terms.get(key), dict) else {}
        evidence = _keyword_recovery_evidence(row, positives.get(key))
        recovery_started_at = existing.get("recovery_started_at_utc")
        if evidence.get("action") and not recovery_started_at:
            recovery_started_at = now_iso
        quarantined_at = existing.get("quarantined_at_utc") or (
            getattr(row, "first_seen_at", None).isoformat() if getattr(row, "first_seen_at", None) else now_iso
        )
        updated_terms[key] = {
            **existing,
            "keyword": keyword,
            "theme_key": key,
            "quarantined_at_utc": quarantined_at,
            "last_seen_at_utc": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else existing.get("last_seen_at_utc"),
            "last_planned_at_utc": now_iso,
            "reason_label": getattr(row, "reason_label", ""),
            "confidence": float(getattr(row, "confidence", 0) or 0),
            "recovery_started_at_utc": recovery_started_at,
            "recovery_action": evidence.get("action") or "",
        }
        recovery_started = parse_iso_datetime(recovery_started_at)
        recovery_age_days = (now - recovery_started).total_seconds() / 86400 if recovery_started else 0.0
        base = {
            "keyword": keyword,
            "theme_key": key,
            "match_type": getattr(row, "match_type", None) or "exact",
            "source": "waste_review",
            "negative_candidate_id": getattr(row, "id", None),
            "campaign_id": int(getattr(row, "campaign_id", 0) or 0),
            "campaign_name": getattr(row, "campaign_name", "") or "",
            "reason_label": getattr(row, "reason_label", ""),
            "confidence": float(getattr(row, "confidence", 0) or 0),
            "impressions": int(getattr(row, "impressions", 0) or 0),
            "clicks": int(getattr(row, "clicks", 0) or 0),
            "cost": float(getattr(row, "cost", 0) or 0),
            "hold_days": hold_days,
            "recovery_started_at_utc": recovery_started_at,
            "recovery_age_days": round(recovery_age_days, 2),
            "recovery_evidence": evidence,
        }
        if evidence.get("action") and recovery_age_days >= hold_days:
            target = scale_keyword_recovery if evidence["action"] == "scale" else testing_keyword_recovery
            target.append(
                {
                    **base,
                    "remove_negative_before_promotion": True,
                    "reason": evidence.get("reason") or "Waste recovery guard passed.",
                }
            )
            continue
        if evidence.get("action"):
            pending_keyword_recovery.append(
                {
                    **base,
                    "reason": f"Recovery evidence exists, but the {hold_days}-day Waste recovery hold has not passed.",
                }
            )
        active_negative_keywords.append(
            {
                **base,
                "reason": (
                    "Keep quarantined as an exact negative until fresh conversion evidence or guarded review evidence "
                    "passes the Waste recovery hold."
                ),
            }
        )

    page_by_key: dict[str, GoogleAdsLandingPageCandidate] = {}
    for row in page_rows:
        url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
        key = _page_key(url)
        if key:
            page_by_key[key] = row

    active_page_exclusions: list[dict[str, Any]] = []
    pending_page_recovery: list[dict[str, Any]] = []
    scale_page_recovery: list[dict[str, Any]] = []
    testing_page_recovery: list[dict[str, Any]] = []
    active_page_keys = set(page_by_key.keys()) | {str(key) for key in updated_pages.keys()}
    for key in sorted(active_page_keys):
        row = page_by_key.get(key)
        existing = updated_pages.get(key) if isinstance(updated_pages.get(key), dict) else {}
        if row is None:
            continue
        url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
        waste_reason, confidence = _waste_page_reason(row)
        if not waste_reason and not existing:
            continue
        evidence = _page_recovery_evidence(row, existing)
        recovery_started_at = existing.get("recovery_started_at_utc")
        if evidence.get("action") and not recovery_started_at:
            recovery_started_at = now_iso
        quarantined_at = existing.get("quarantined_at_utc") or (
            getattr(row, "first_seen_at", None).isoformat() if getattr(row, "first_seen_at", None) else now_iso
        )
        updated_pages[key] = {
            **existing,
            "url": url,
            "page_key": key,
            "quarantined_at_utc": quarantined_at,
            "last_seen_at_utc": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else existing.get("last_seen_at_utc"),
            "last_planned_at_utc": now_iso,
            "reason_label": waste_reason or existing.get("reason_label", ""),
            "confidence": confidence or float(existing.get("confidence") or 0),
            "recovery_started_at_utc": recovery_started_at,
            "recovery_action": evidence.get("action") or "",
        }
        recovery_started = parse_iso_datetime(recovery_started_at)
        recovery_age_days = (now - recovery_started).total_seconds() / 86400 if recovery_started else 0.0
        base = {
            "url": url,
            "page_key": key,
            "source": "waste_review",
            "landing_page_candidate_id": getattr(row, "id", None),
            "reason_label": waste_reason or existing.get("reason_label", ""),
            "confidence": confidence or float(existing.get("confidence") or 0),
            "impressions": int(getattr(row, "impressions", 0) or 0),
            "clicks": int(getattr(row, "clicks", 0) or 0),
            "cost": float(getattr(row, "cost", 0) or 0),
            "hold_days": hold_days,
            "recovery_started_at_utc": recovery_started_at,
            "recovery_age_days": round(recovery_age_days, 2),
            "recovery_evidence": evidence,
        }
        if evidence.get("action") and recovery_age_days >= hold_days:
            target = scale_page_recovery if evidence["action"] == "scale" else testing_page_recovery
            target.append(
                {
                    **base,
                    "remove_page_exclusion_before_promotion": True,
                    "reason": evidence.get("reason") or "Waste recovery guard passed.",
                }
            )
            continue
        if evidence.get("action"):
            pending_page_recovery.append(
                {
                    **base,
                    "reason": f"Recovery evidence exists, but the {hold_days}-day Waste recovery hold has not passed.",
                }
            )
        if waste_reason:
            active_page_exclusions.append(
                {
                    **base,
                    "reason": (
                        "Keep quarantined as a DSA/PMax page exclusion until fresh conversion evidence or guarded "
                        "review evidence passes the Waste recovery hold."
                    ),
                }
            )

    new_state = {
        **state,
        "hold_days": hold_days,
        "updated_at_utc": now_iso,
        "active_negative_keyword_count": len(active_negative_keywords),
        "active_page_exclusion_count": len(active_page_exclusions),
        "pending_keyword_recovery_count": len(pending_keyword_recovery),
        "pending_page_recovery_count": len(pending_page_recovery),
        "scale_keyword_recovery_count": len(scale_keyword_recovery),
        "scale_page_recovery_count": len(scale_page_recovery),
        "terms": updated_terms,
        "pages": updated_pages,
    }
    return {
        "enabled": bool(active_negative_keywords or active_page_exclusions or pending_keyword_recovery or pending_page_recovery),
        "category": "Waste / Recovery",
        "budget": WASTE_FIXED_DAILY_BUDGET_AMOUNT,
        "fixed_daily_budget": WASTE_FIXED_DAILY_BUDGET_AMOUNT,
        "currency_code": str(currency_code or "USD").upper(),
        "budget_policy": "fixed_no_peak_boost_no_sales_guard_reduction",
        "hold_days": hold_days,
        "negative_keyword_plan": {
            "enabled": bool(active_negative_keywords),
            "applies_to": ["core_scale_rsa", "core_scale_dsa", "core_scale_pmax", "testing_rsa", "testing_dsa", "testing_pmax"],
            "match_type": "exact",
            "active_negative_keywords": active_negative_keywords,
            "active_negative_keyword_count": len(active_negative_keywords),
            "pending_recovery_keywords": pending_keyword_recovery,
            "pending_recovery_keyword_count": len(pending_keyword_recovery),
            "scale_recovery_keywords": scale_keyword_recovery,
            "scale_recovery_keyword_count": len(scale_keyword_recovery),
            "testing_recovery_keywords": testing_keyword_recovery,
            "testing_recovery_keyword_count": len(testing_keyword_recovery),
        },
        "page_exclusion_plan": {
            "enabled": bool(active_page_exclusions),
            "applies_to": ["core_scale_dsa", "core_scale_pmax", "testing_dsa", "testing_pmax"],
            "rsa_handling": "Do not use active Waste URLs as RSA final URLs.",
            "active_page_exclusions": active_page_exclusions,
            "active_page_exclusion_count": len(active_page_exclusions),
            "pending_recovery_pages": pending_page_recovery,
            "pending_recovery_page_count": len(pending_page_recovery),
            "scale_recovery_pages": scale_page_recovery,
            "scale_recovery_page_count": len(scale_page_recovery),
            "testing_recovery_pages": testing_page_recovery,
            "testing_recovery_page_count": len(testing_page_recovery),
        },
        "state": new_state,
        "basis": (
            "Waste is a fixed-budget recovery lane. It keeps high-confidence no-conversion waste terms/pages out of "
            "Testing and Core / Scale, protects its own budget from normal scaling/reduction, and can recover items "
            f"after fresh positive evidence and a {WASTE_RECOVERY_HOLD_DAYS}-day hold."
        ),
    }


def _negative_keyword_identity(item: dict[str, Any]) -> tuple[str, str]:
    return (_theme_key(item.get("keyword")), str(item.get("match_type") or "exact").lower())


def merge_negative_keywords(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group or []:
            key = _negative_keyword_identity(item)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def merge_page_exclusions(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            url = canonical_landing_page_url(str(item.get("url") or ""))
            key = str(item.get("page_key") or _page_key(url))
            if not url or not key or key in seen:
                continue
            seen.add(key)
            merged.append({**item, "url": url, "page_key": key})
    return merged


def _limited_items(items: Any, limit: int = 50) -> list[Any]:
    if not isinstance(items, list):
        return []
    return items[: max(int(limit or 0), 0)]


def compact_waste_plan_for_draft(plan: dict[str, Any], *, item_limit: int = 50) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    negative_plan = plan.get("negative_keyword_plan") if isinstance(plan.get("negative_keyword_plan"), dict) else {}
    page_plan = plan.get("page_exclusion_plan") if isinstance(plan.get("page_exclusion_plan"), dict) else {}
    state = plan.get("state") if isinstance(plan.get("state"), dict) else {}
    return {
        **{key: value for key, value in plan.items() if key not in {"negative_keyword_plan", "page_exclusion_plan", "state"}},
        "negative_keyword_plan": {
            **{key: value for key, value in negative_plan.items() if isinstance(value, (str, int, float, bool)) or value is None},
            "active_negative_keywords": _limited_items(negative_plan.get("active_negative_keywords"), item_limit),
            "pending_recovery_keywords": _limited_items(negative_plan.get("pending_recovery_keywords"), item_limit),
            "scale_recovery_keywords": _limited_items(negative_plan.get("scale_recovery_keywords"), item_limit),
            "testing_recovery_keywords": _limited_items(negative_plan.get("testing_recovery_keywords"), item_limit),
        },
        "page_exclusion_plan": {
            **{key: value for key, value in page_plan.items() if isinstance(value, (str, int, float, bool)) or value is None},
            "active_page_exclusions": _limited_items(page_plan.get("active_page_exclusions"), item_limit),
            "pending_recovery_pages": _limited_items(page_plan.get("pending_recovery_pages"), item_limit),
            "scale_recovery_pages": _limited_items(page_plan.get("scale_recovery_pages"), item_limit),
            "testing_recovery_pages": _limited_items(page_plan.get("testing_recovery_pages"), item_limit),
        },
        "state_summary": {
            "hold_days": state.get("hold_days"),
            "updated_at_utc": state.get("updated_at_utc"),
            "active_negative_keyword_count": state.get("active_negative_keyword_count"),
            "active_page_exclusion_count": state.get("active_page_exclusion_count"),
            "pending_keyword_recovery_count": state.get("pending_keyword_recovery_count"),
            "pending_page_recovery_count": state.get("pending_page_recovery_count"),
            "scale_keyword_recovery_count": state.get("scale_keyword_recovery_count"),
            "scale_page_recovery_count": state.get("scale_page_recovery_count"),
            "term_state_count": len(state.get("terms") or {}) if isinstance(state.get("terms"), dict) else 0,
            "page_state_count": len(state.get("pages") or {}) if isinstance(state.get("pages"), dict) else 0,
        },
    }


def compact_landing_page_governance_for_draft(plan: dict[str, Any], *, item_limit: int = 50) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    categories: list[dict[str, Any]] = []
    for category in plan.get("categories") or []:
        if not isinstance(category, dict):
            continue
        compact = {key: value for key, value in category.items() if not isinstance(value, list)}
        for key, value in category.items():
            if isinstance(value, list):
                compact[key] = value[:item_limit]
                compact[f"{key}_truncated_count"] = max(len(value) - item_limit, 0)
        categories.append(compact)
    return {
        **{key: value for key, value in plan.items() if key != "categories"},
        "categories": categories,
    }


def testing_daily_budget_from_sales(
    session: Session,
    preference: GoogleAdsAutomationPreference,
) -> dict[str, Any]:
    account = preference.account
    ratio = min(max(float(preference.testing_sales_budget_ratio or 0.05), 0.001), 1.0)
    window_days = clamp_int(preference.odoo_sales_guard_window_days, 7, 1, 90)
    guard = odoo_sales_budget_guard_for_account(
        session,
        account,
        window_days=window_days,
        max_spend_ratio=ratio,
    )
    sales_inr = float(guard.get("sales_inr") or 0)
    planned_daily_inr = (sales_inr * ratio / max(window_days, 1)) if sales_inr > 0 else 0.0
    daily_room_inr = float(guard.get("daily_spend_room_inr") or 0.0)
    remaining_room_inr = float(guard.get("remaining_spend_room_inr") or 0.0)
    bootstrap = automation_budget_bootstrap_state(
        session,
        preference,
        ensure=bool(preference.auto_create_campaigns_enabled and not preference.monitor_only),
    )
    bootstrap_active = bool(bootstrap.get("active"))
    daily_inr = (
        planned_daily_inr
        if bootstrap_active
        else (min(planned_daily_inr, daily_room_inr) if planned_daily_inr > 0 and daily_room_inr > 0 else 0.0)
    )
    rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    currency_code = account.currency_code or "INR"
    converted = convert_amount(daily_inr, "INR", currency_code, rates) if daily_inr > 0 else None
    minimum = currency_minimum_daily_budget(account, preference.minimum_daily_budget_amount)
    converted_amount = float(converted or 0.0)
    budget_blocked = False
    block_reason = ""
    if sales_inr <= 0:
        budget_blocked = True
        block_reason = "No synced Odoo sales are available for this account, so automated campaign spend is blocked."
    elif not bootstrap_active and (remaining_room_inr <= 0 or daily_room_inr <= 0):
        budget_blocked = True
        block_reason = "Odoo sales guard has no remaining spend room for Testing / Discovery."
    elif not bootstrap_active and converted_amount < minimum:
        budget_blocked = True
        block_reason = "Remaining Odoo spend room is below the configured minimum daily budget."
    daily_budget = 0.0 if budget_blocked else (max(converted_amount, minimum) if bootstrap_active else converted_amount)
    return {
        "ratio": ratio,
        "ratio_pct": round(ratio * 100, 2),
        "window_days": window_days,
        "sales_inr": sales_inr,
        "planned_daily_budget_inr": planned_daily_inr,
        "daily_budget": daily_budget,
        "daily_budget_inr": daily_inr,
        "daily_spend_room_inr": daily_room_inr,
        "remaining_spend_room_inr": remaining_room_inr,
        "currency_code": currency_code,
        "minimum_daily_budget": minimum,
        "budget_blocked": budget_blocked,
        "budget_block_reason": block_reason,
        "used_minimum": bool(not budget_blocked and bootstrap_active and converted_amount < minimum),
        "bootstrap": bootstrap,
        "budget_basis": "last_7_day_sales_bootstrap" if bootstrap_active else "remaining_sales_guard_room",
        "guard": guard,
    }


def _has_existing_automation_production_lanes(session: Session, account: GoogleAdsAccount) -> bool:
    """Treat already-created automation accounts as established even if daily metrics are still empty."""
    rows = session.execute(
        select(
            GoogleAdsCampaignMetric.campaign_name,
            GoogleAdsCampaignMetric.campaign_status,
            GoogleAdsCampaignMetric.channel_type,
        )
        .where(
            GoogleAdsCampaignMetric.account_id == account.id,
            GoogleAdsCampaignMetric.campaign_name.ilike("AUTO |%"),
        )
        .order_by(GoogleAdsCampaignMetric.synced_at.desc())
        .limit(50)
    ).all()
    enabled_names = [
        str(name or "").lower()
        for name, status, _channel in rows
        if str(status or "").upper() == "ENABLED"
    ]
    if any("core / scale" in name or "fix / watch" in name or "waste / recovery" in name for name in enabled_names):
        return True
    pmax_count = sum(1 for name, _status, channel in rows if str(channel or "").upper() == "PERFORMANCE_MAX" and str(name or "").startswith("AUTO |"))
    search_count = sum(1 for name, _status, channel in rows if str(channel or "").upper() == "SEARCH" and str(name or "").startswith("AUTO |"))
    return pmax_count > 0 and search_count > 0


def campaign_bootstrap_decision(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or utcnow()
    account = preference.account
    metrics = recent_account_metric_totals(session, account, days=7, now=now)
    threshold = pmax_conversion_threshold(preference)
    pmax_gate = pmax_activation_gate(session, account, preference, now=now)
    bootstrap_days = clamp_int(preference.testing_bootstrap_days, 15, 1, 60)
    state = load_campaign_bootstrap_state(session, account)
    started_at = parse_iso_datetime(state.get("started_at_utc"))
    age_days = (now.astimezone(timezone.utc) - started_at).days if started_at else None
    no_recent_impressions = int(metrics.get("impressions") or 0) <= 0
    enough_conversions = bool(pmax_gate.get("allowed"))
    existing_automation_lanes = _has_existing_automation_production_lanes(session, account)

    if no_recent_impressions and not existing_automation_lanes and started_at is None:
        started_at = now.astimezone(timezone.utc)
        state = {
            **state,
            "started_at_utc": iso_datetime(started_at),
            "reason": "no_last_7d_impressions",
        }
        save_campaign_bootstrap_state(session, account, state)
        age_days = 0

    in_bootstrap = started_at is not None and (age_days or 0) < bootstrap_days
    if enough_conversions:
        mode = "pmax_allowed"
        allowed_campaign_types = ["rsa", "dsa", "pmax"]
        reason = str(pmax_gate.get("reason") or "Automation-owned Search campaigns have enough conversion signal for PMax.")
    elif no_recent_impressions or in_bootstrap:
        mode = "testing_no_pmax"
        allowed_campaign_types = ["rsa", "dsa"]
        reason = "No recent delivery or account is inside the RSA/DSA-only testing bootstrap window. " + str(pmax_gate.get("reason") or "")
    else:
        mode = "normal_categories_no_pmax"
        allowed_campaign_types = ["rsa", "dsa"]
        reason = "Bootstrap window is complete, but PMax still waits for enough automation Search conversion signal. " + str(pmax_gate.get("reason") or "")

    if started_at is not None and not enough_conversions and (age_days or 0) >= bootstrap_days:
        state = {
            **state,
            "completed_at_utc": state.get("completed_at_utc") or iso_datetime(now),
            "completed_reason": "bootstrap_window_elapsed",
        }
        save_campaign_bootstrap_state(session, account, state)

    return {
        "mode": mode,
        "reason": reason,
        "allowed_campaign_types": allowed_campaign_types,
        "pmax_allowed": "pmax" in allowed_campaign_types,
        "no_recent_impressions": no_recent_impressions,
        "enough_auto_search_conversions": enough_conversions,
        "enough_7d_conversions": enough_conversions,
        "existing_automation_lanes": existing_automation_lanes,
        "pmax_min_7d_conversions": threshold,
        "pmax_gate": pmax_gate,
        "bootstrap_days": bootstrap_days,
        "bootstrap_started_at": started_at.isoformat() if started_at else None,
        "bootstrap_age_days": age_days,
        "metrics_7d": metrics,
    }


def _automation_draft_source_key(account: GoogleAdsAccount, kind: str) -> str:
    return f"automation:{account.customer_id}:{kind}"


def _compact_campaign_part(value: str, *, max_len: int = 48) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = text.replace("|", "/")
    return text[:max_len].strip(" -/")


def automation_campaign_code(
    account: GoogleAdsAccount,
    *,
    category: str,
    campaign_intent: str,
    website_url: str = "",
) -> str:
    customer_id = re.sub(r"\D+", "", str(account.customer_id or ""))
    root_domain = _root_domain(website_url)
    seed = "|".join(
        [
            customer_id,
            root_domain,
            str(category or "").strip().lower(),
            str(campaign_intent or "").strip().lower(),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10].upper()
    return f"AUTO-{digest}"


def automation_campaign_identity(
    account: GoogleAdsAccount,
    *,
    category: str,
    campaign_intent: str,
    channel_label: str,
    website_url: str = "",
) -> dict[str, Any]:
    code = automation_campaign_code(
        account,
        category=category,
        campaign_intent=campaign_intent,
        website_url=website_url,
    )
    category_label = _compact_campaign_part(category, max_len=44)
    channel = _compact_campaign_part(channel_label, max_len=48)
    campaign_name = f"AUTO | {category_label} | {channel} | {code}"
    return {
        "campaign_name": campaign_name[:255],
        "campaign_code": code,
        "category": category_label,
        "campaign_intent": campaign_intent,
        "channel_label": channel,
        "customer_id": re.sub(r"\D+", "", str(account.customer_id or "")),
        "root_domain": _root_domain(website_url),
        "resume_key": code,
        "lookup": {
            "name_contains": code,
            "category_contains": category_label,
            "expected_prefix": "AUTO",
        },
    }


def _latest_auto_inventory_campaign_rows(session: Session, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    cache_key = f"auto_live_inventory_campaign_rows:{account.id}"
    cached = session.info.get(cache_key)
    if isinstance(cached, list):
        return cached
    payload = session.scalar(
        select(GoogleAdsDataSnapshot.payload_json)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == "auto_live_inventory",
        )
        .order_by(GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )
    payload = payload if isinstance(payload, dict) else {}
    rows = [
        row
        for row in (((payload.get("datasets") or {}).get("campaigns") or {}).get("rows") or [])
        if isinstance(row, dict)
    ]
    session.info[cache_key] = rows
    return rows


def _campaign_match_from_inventory_row(
    row: dict[str, Any],
    *,
    matched_by: str,
    campaign_code: str,
    generated_campaign_code: str = "",
) -> dict[str, Any]:
    return {
        "campaign_id": int(row.get("campaign_id") or row.get("id") or 0),
        "campaign_name": str(row.get("campaign_name") or row.get("name") or ""),
        "campaign_status": str(row.get("campaign_status") or row.get("status") or ""),
        "channel_type": str(row.get("channel_type") or row.get("advertising_channel_type") or ""),
        "latest_metric_date": None,
        "latest_synced_at": None,
        "matched_by": matched_by,
        "campaign_code": campaign_code,
        "generated_campaign_code": generated_campaign_code,
    }


def existing_google_campaign_for_identity(
    session: Session,
    account: GoogleAdsAccount,
    identity: dict[str, Any],
) -> Optional[dict[str, Any]]:
    code = str(identity.get("campaign_code") or "").strip()
    if not code:
        return None
    for inventory_row in _latest_auto_inventory_campaign_rows(session, account):
        campaign_name = str(inventory_row.get("campaign_name") or inventory_row.get("name") or "")
        if code in campaign_name:
            return _campaign_match_from_inventory_row(
                inventory_row,
                matched_by="automation_campaign_code",
                campaign_code=code,
            )
    row = session.execute(
        select(
            GoogleAdsCampaignMetric.campaign_id,
            func.max(GoogleAdsCampaignMetric.campaign_name),
            func.max(GoogleAdsCampaignMetric.campaign_status),
            func.max(GoogleAdsCampaignMetric.channel_type),
            func.max(GoogleAdsCampaignMetric.metric_date),
            func.max(GoogleAdsCampaignMetric.synced_at),
        )
        .where(
            GoogleAdsCampaignMetric.account_id == account.id,
            GoogleAdsCampaignMetric.campaign_name.ilike(f"%{code}%"),
        )
        .group_by(GoogleAdsCampaignMetric.campaign_id)
        .order_by(func.max(GoogleAdsCampaignMetric.metric_date).desc(), func.max(GoogleAdsCampaignMetric.synced_at).desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "campaign_id": int(row[0] or 0),
        "campaign_name": str(row[1] or ""),
        "campaign_status": str(row[2] or ""),
        "channel_type": str(row[3] or ""),
        "latest_metric_date": row[4].isoformat() if row[4] else None,
        "latest_synced_at": row[5].isoformat() if row[5] else None,
        "matched_by": "automation_campaign_code",
        "campaign_code": code,
    }


def _automation_code_from_campaign_name(campaign_name: str) -> str:
    match = re.search(r"\bAUTO-[A-F0-9]{10}\b", str(campaign_name or "").upper())
    return match.group(0) if match else ""


def existing_google_campaign_for_lane(
    session: Session,
    account: GoogleAdsAccount,
    identity: dict[str, Any],
) -> Optional[dict[str, Any]]:
    category = str(identity.get("category") or "").strip()
    channel_label = str(identity.get("channel_label") or "").strip()
    if not category or not channel_label:
        return None
    lane_prefix = f"AUTO | {category} | {channel_label} | AUTO-"
    inventory_candidates: list[dict[str, Any]] = []
    for inventory_row in _latest_auto_inventory_campaign_rows(session, account):
        campaign_name = str(inventory_row.get("campaign_name") or inventory_row.get("name") or "")
        if not campaign_name.lower().startswith(lane_prefix.lower()):
            continue
        campaign_code = _automation_code_from_campaign_name(campaign_name)
        if not campaign_code:
            continue
        inventory_candidates.append(
            _campaign_match_from_inventory_row(
                inventory_row,
                matched_by="automation_lane_fallback",
                campaign_code=campaign_code,
                generated_campaign_code=str(identity.get("campaign_code") or ""),
            )
        )
    if inventory_candidates:
        inventory_candidates.sort(key=lambda item: (int(item.get("campaign_id") or 0), str(item.get("campaign_name") or "")))
        return inventory_candidates[0]
    rows = list(
        session.execute(
            select(
                GoogleAdsCampaignMetric.campaign_id,
                func.max(GoogleAdsCampaignMetric.campaign_name),
                func.max(GoogleAdsCampaignMetric.campaign_status),
                func.max(GoogleAdsCampaignMetric.channel_type),
                func.max(GoogleAdsCampaignMetric.metric_date),
                func.max(GoogleAdsCampaignMetric.synced_at),
            )
            .where(
                GoogleAdsCampaignMetric.account_id == account.id,
                GoogleAdsCampaignMetric.campaign_name.ilike(f"{lane_prefix}%"),
            )
            .group_by(GoogleAdsCampaignMetric.campaign_id)
        ).all()
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        campaign_name = str(row[1] or "")
        campaign_code = _automation_code_from_campaign_name(campaign_name)
        if not campaign_code:
            continue
        candidates.append(
            {
                "campaign_id": int(row[0] or 0),
                "campaign_name": campaign_name,
                "campaign_status": str(row[2] or ""),
                "channel_type": str(row[3] or ""),
                "latest_metric_date": row[4].isoformat() if row[4] else None,
                "latest_synced_at": row[5].isoformat() if row[5] else None,
                "matched_by": "automation_lane_fallback",
                "campaign_code": campaign_code,
                "generated_campaign_code": str(identity.get("campaign_code") or ""),
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (int(item.get("campaign_id") or 0), str(item.get("campaign_name") or "")))
    return candidates[0]


def attach_campaign_identity(
    session: Session,
    account: GoogleAdsAccount,
    *,
    automation: dict[str, Any],
    category: str,
    campaign_intent: str,
    channel_label: str,
    website_url: str,
) -> dict[str, Any]:
    identity = automation_campaign_identity(
        account,
        category=category,
        campaign_intent=campaign_intent,
        channel_label=channel_label,
        website_url=website_url,
    )
    exact_existing = existing_google_campaign_for_identity(session, account, identity)
    lane_existing = existing_google_campaign_for_lane(session, account, identity)
    existing = exact_existing
    if lane_existing and (
        exact_existing is None
        or int(lane_existing.get("campaign_id") or 0) < int(exact_existing.get("campaign_id") or 0)
    ):
        existing = lane_existing
    adopted_code = str(existing.get("campaign_code") or "") if existing else ""
    if adopted_code and adopted_code != str(identity.get("campaign_code") or ""):
        identity = {
            **identity,
            "generated_campaign_code": str(identity.get("campaign_code") or ""),
            "campaign_code": adopted_code,
            "campaign_name": str(existing.get("campaign_name") or identity.get("campaign_name") or "")[:255],
            "resume_key": adopted_code,
            "lookup": {
                **(identity.get("lookup") if isinstance(identity.get("lookup"), dict) else {}),
                "name_contains": adopted_code,
                "adopted_lane_fallback": True,
            },
        }
    return {
        **automation,
        "category": category,
        "campaign_intent": campaign_intent,
        "campaign_identity": identity,
        "existing_google_campaign": existing,
        "planned_operation": "resume_existing_campaign" if existing else "create_new_campaign_if_missing",
    }


def automation_live_creation_control(
    decision: dict[str, Any],
    *,
    category: str,
    ad_type: str,
) -> dict[str, Any]:
    if ad_type == "pmax" and not bool(decision.get("pmax_allowed")):
        gate = decision.get("pmax_gate") if isinstance(decision.get("pmax_gate"), dict) else {}
        return {
            "enabled": False,
            "phase": "pmax_search_conversion_gate",
            "reason": str(gate.get("reason") or decision.get("reason") or "PMax is held until automation-owned Search campaigns reach the conversion gate."),
            "pmax_gate": gate,
        }
    return {
        "enabled": True,
        "phase": "full_closed_loop",
        "reason": (
            "Automation owns this closed-loop campaign lane. Conversion and bootstrap signals still shape budgets, "
            "URL inclusion scope, keywords, negatives, and monitoring. PMax has its own automation Search conversion gate."
        ),
    }


def _existing_automation_draft(
    session: Session,
    *,
    account_id: int,
    ad_type: str,
    source_key: str,
) -> Optional[AdDraft]:
    return session.scalar(
        select(AdDraft)
        .options(load_only(AdDraft.id, AdDraft.status))
        .where(
            AdDraft.account_id == account_id,
            AdDraft.ad_type == ad_type,
            AdDraft.generated_assets["automation"]["source_key"].as_string() == source_key,
        )
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        .limit(1)
    )


def _max_cpc_bid_limit_for_account(account: GoogleAdsAccount) -> float:
    currency = str(account.currency_code or "").upper()
    if currency == "INR":
        return 300.0
    if currency in {"AUD", "CAD", "USD"}:
        return 2.5
    return 2.0


def _keyword_payload(rows: list[GoogleAdsKeywordCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row.id,
            "keyword": row.keyword,
            "normalized_keyword": row.normalized_keyword,
            "quality_label": row.quality_label,
            "score": float(row.score or 0),
            "clicks": int(row.clicks or 0),
            "conversions": _row_total_conversions(row),
            "conversion_value": _row_total_value(row),
            "source_account_id": row.account_id,
        }
        for row in rows
    ]


def _landing_page_engagement_metrics(row: Any) -> dict[str, float]:
    source = _row_source_json(row)
    source_rows = [item for item in (source.get("source_rows") or []) if isinstance(item, dict)]
    add_to_carts = 0.0
    checkouts = 0.0
    source_purchases = 0.0
    source_revenue = 0.0
    for source_row in source_rows:
        add_to_carts += numeric_value(source_row, "add_to_carts") or numeric_value(source_row, "addToCarts")
        checkouts += numeric_value(source_row, "checkouts") or numeric_value(source_row, "begin_checkouts")
        source_purchases += numeric_value(source_row, "purchases")
        source_revenue += numeric_value(source_row, "revenue")
    return {
        "add_to_carts": add_to_carts,
        "checkouts": checkouts,
        "purchases": source_purchases if source_rows else _row_total_conversions(row),
        "revenue": source_revenue if source_rows else _row_total_value(row),
    }


def _landing_page_payload(rows: list[GoogleAdsLandingPageCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row.id,
            "url": row.url,
            "normalized_url": row.normalized_url,
            "quality_label": row.quality_label,
            "score": float(row.score or 0),
            "clicks": int(row.clicks or 0),
            "conversions": _row_total_conversions(row),
            "conversion_value": _row_total_value(row),
            "engagement": _landing_page_engagement_metrics(row),
            "source_account_id": row.account_id,
        }
        for row in rows
    ]


def _url_inclusion_targets_from_rows(rows: list[GoogleAdsLandingPageCandidate], *, limit: Optional[int] = None) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
        if not url or not usable_landing_page_url(url) or url in seen:
            continue
        seen.add(url)
        targets.append(
            {
                "url": url,
                "page_key": _page_key(url),
                "source": "saved_landing_page_candidate",
                "candidate_id": getattr(row, "id", None),
                "score": float(getattr(row, "score", 0) or 0),
                "conversions": _row_total_conversions(row),
                "conversion_value": _row_total_value(row),
                "engagement": _landing_page_engagement_metrics(row),
            }
        )
        if limit and len(targets) >= limit:
            break
    return targets


def _url_inclusion_targets_from_page_items(
    items: list[dict[str, Any]],
    *,
    limit: Optional[int] = None,
    source: str = "landing_page_governance",
    exclude_broad_pages: bool = False,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = canonical_landing_page_url(str(item.get("url") or item.get("page_url") or ""))
        key = _page_key(url)
        if not url or not usable_landing_page_url(url) or not key or key in seen:
            continue
        if exclude_broad_pages and _is_broad_or_category_landing_page(url):
            continue
        seen.add(key)
        targets.append(
            {
                "url": url,
                "page_key": key,
                "source": str(item.get("source") or source),
                "candidate_id": item.get("candidate_id"),
                "score": float(item.get("score") or 0),
                "conversions": float(item.get("conversions") or item.get("purchases") or 0),
                "conversion_value": float(item.get("conversion_value") or item.get("revenue") or 0),
                "add_to_carts": float(item.get("add_to_carts") or 0),
                "checkouts": float(item.get("checkouts") or 0),
                "quality_label": str(item.get("quality_label") or ""),
            }
        )
        if limit and len(targets) >= limit:
            break
    return targets


def _pmax_policy_terms(session: Session) -> list[str]:
    terms = list(get_restricted_title_terms_sync(session) or [])
    terms.extend(PMAX_POLICY_SENSITIVE_FALLBACK_TERMS)
    normalized: list[str] = []
    for term in terms:
        cleaned = normalize_restricted_text(term)
        if cleaned:
            normalized.append(cleaned)
    return list(dict.fromkeys(normalized))


def _pmax_policy_match(value: Any, policy_terms: list[str] | None) -> str:
    if not policy_terms:
        return ""
    return restricted_title_match(value, policy_terms)


def _keyword_row_policy_text(row: GoogleAdsKeywordCandidate) -> str:
    parts = [
        getattr(row, "keyword", ""),
        getattr(row, "normalized_keyword", ""),
        getattr(row, "quality_label", ""),
        " ".join(str(item or "") for item in (getattr(row, "campaign_names", None) or [])),
    ]
    source_json = _row_source_json(row)
    if isinstance(source_json, dict):
        for key in ("product_name", "item_name", "page_title", "landing_page", "final_url", "raw_url"):
            parts.append(source_json.get(key) or "")
    return " ".join(str(part or "") for part in parts)


def _url_target_policy_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("url"),
        item.get("page_url"),
        item.get("normalized_url"),
        item.get("source"),
        item.get("quality_label"),
        item.get("page_title"),
        item.get("product_name"),
        item.get("keyword"),
    ]
    return " ".join(str(part or "") for part in parts)


def _pmax_safe_url_targets(
    targets: list[dict[str, Any]],
    policy_terms: list[str],
    *,
    limit: Optional[int] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in targets:
        if not isinstance(item, dict):
            continue
        match = _pmax_policy_match(_url_target_policy_text(item), policy_terms)
        if match:
            blocked.append(
                {
                    "url": item.get("url"),
                    "page_key": item.get("page_key"),
                    "matched_term": match,
                    "source": item.get("source"),
                }
            )
            continue
        safe.append(item)
        if limit and len(safe) >= limit:
            break
    return safe, {
        "mode": "pmax_policy_safe_subset",
        "restricted_term_count": len(policy_terms),
        "blocked_url_count": len(blocked),
        "blocked_urls": blocked[:100],
    }


def _primary_url_from_targets(targets: list[dict[str, Any]], fallback_url: str) -> str:
    for item in targets:
        if not isinstance(item, dict):
            continue
        url = canonical_landing_page_url(str(item.get("url") or ""))
        if url and usable_landing_page_url(url):
            return url
    return fallback_url


def _page_key(value: Any) -> str:
    return canonical_landing_page_url(str(value or "")).lower()


def _is_broad_or_category_landing_page(url: Any) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except Exception:
        return False
    path = parsed.path.strip("/").lower()
    if not path:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if path in {"shop", "products", "product"}:
        return True
    if path.startswith(("shop/category/", "category/", "collections/", "collection/", "brands/", "brand/")):
        return True
    return any(segment in {"cart", "checkout", "contact", "about", "blog", "privacy", "terms"} for segment in segments)


def _scale_landing_page_evidence(row: GoogleAdsLandingPageCandidate) -> dict[str, Any]:
    source = _row_source_json(row)
    source_rows = [item for item in (source.get("source_rows") or []) if isinstance(item, dict)]
    scale_rows = []
    for source_row in source_rows:
        if not _is_core_scale_campaign_name(source_row.get("campaign_name")):
            continue
        conversions = numeric_value(source_row, "conversions") + numeric_value(source_row, "all_conversions")
        conversion_value = numeric_value(source_row, "conversion_value") + numeric_value(source_row, "all_conversions_value")
        if conversions > 0 or conversion_value > 0:
            scale_rows.append(
                {
                    "campaign_id": source_row.get("campaign_id"),
                    "campaign_name": source_row.get("campaign_name"),
                    "url": canonical_landing_page_url(str(source_row.get("raw_url") or row.normalized_url or row.url or "")),
                    "conversions": conversions,
                    "conversion_value": conversion_value,
                }
            )
    scale_conversions = sum(float(item.get("conversions") or 0) for item in scale_rows)
    scale_conversion_value = sum(float(item.get("conversion_value") or 0) for item in scale_rows)
    scale_campaign_names = list(
        dict.fromkeys(str(item.get("campaign_name") or "").strip() for item in scale_rows if str(item.get("campaign_name") or "").strip())
    )
    campaign_names = [str(item or "").strip() for item in (getattr(row, "campaign_names", None) or []) if str(item or "").strip()]
    if not scale_rows and campaign_names and all(_is_core_scale_campaign_name(name) for name in campaign_names):
        scale_conversions = _row_total_conversions(row)
        scale_conversion_value = _row_total_value(row)
        scale_campaign_names = campaign_names
    return {
        "scale_conversions": scale_conversions,
        "scale_conversion_value": scale_conversion_value,
        "scale_campaign_names": scale_campaign_names,
        "scale_source_rows": scale_rows[:8],
        "has_scale_conversion": scale_conversions > 0 or scale_conversion_value > 0,
    }


def _matrix_scale_landing_page_items(
    *,
    ga4_matrix: Optional[dict[str, Any]] = None,
    search_console_matrix: Optional[dict[str, Any]] = None,
    seen_keys: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    seen_keys = seen_keys or set()
    items: list[dict[str, Any]] = []

    def append_item(raw: dict[str, Any], *, source: str) -> None:
        url = canonical_landing_page_url(str(raw.get("url") or raw.get("page_url") or ""))
        key = _page_key(url)
        if not url or not key or key in seen_keys:
            return
        seen_keys.add(key)
        items.append(
            {
                "url": url,
                "page_key": key,
                "source": source,
                "quality_label": str(raw.get("quality_label") or "clicked"),
                "score": float(raw.get("score") or 0),
                "clicks": int(float(raw.get("clicks") or raw.get("engaged_sessions") or 0)),
                "impressions": int(float(raw.get("impressions") or raw.get("sessions") or 0)),
                "conversions": float(raw.get("conversions") or raw.get("purchases") or 0),
                "conversion_value": float(raw.get("conversion_value") or raw.get("revenue") or 0),
                "add_to_carts": float(raw.get("add_to_carts") or 0),
                "checkouts": float(raw.get("checkouts") or 0),
                "source_account_id": raw.get("source_account_id"),
                "first_seen_at": raw.get("first_seen_at"),
                "last_seen_at": raw.get("last_seen_at"),
                "campaign_names": [str(item or "") for item in (raw.get("campaign_names") or [])],
                "scale_evidence": {
                    "scale_conversions": float(raw.get("conversions") or raw.get("purchases") or 0),
                    "scale_conversion_value": float(raw.get("conversion_value") or raw.get("revenue") or 0),
                    "scale_campaign_names": [str(item or "") for item in (raw.get("campaign_names") or [])],
                    "scale_source_rows": [],
                    "has_scale_conversion": bool(
                        float(raw.get("conversions") or raw.get("purchases") or 0)
                        or float(raw.get("conversion_value") or raw.get("revenue") or 0)
                    ),
                },
            }
        )

    if isinstance(ga4_matrix, dict):
        for bucket_name in ("scale_landing_pages", "testing_landing_pages"):
            for raw in ga4_matrix.get(bucket_name) or []:
                if isinstance(raw, dict):
                    append_item(raw, source=f"ga4_{bucket_name}")
                    if limit and len(items) >= limit:
                        return items

    if isinstance(search_console_matrix, dict):
        for raw in search_console_matrix.get("top_pages") or []:
            if not isinstance(raw, dict):
                continue
            if float(raw.get("clicks") or 0) <= 0:
                continue
            append_item(raw, source="search_console_clicked_page")
            if limit and len(items) >= limit:
                return items

    return items


def scale_landing_page_plan(
    page_rows: list[GoogleAdsLandingPageCandidate],
    *,
    ga4_matrix: Optional[dict[str, Any]] = None,
    search_console_matrix: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    def is_scale_page(row: GoogleAdsLandingPageCandidate) -> bool:
        label = str(getattr(row, "quality_label", "") or "").strip().lower()
        engagement = _landing_page_engagement_metrics(row)
        return (
            label in {"revenue", "converting", "clicked"}
            or _row_total_conversions(row) > 0
            or _row_total_value(row) > 0
            or float(engagement.get("add_to_carts") or 0) > 0
            or float(engagement.get("checkouts") or 0) > 0
            or float(engagement.get("purchases") or 0) > 0
            or float(engagement.get("revenue") or 0) > 0
        )
    ranked_rows = sorted(
        [row for row in page_rows if is_scale_page(row)],
        key=lambda row: (
            _row_total_conversions(row),
            _row_total_value(row),
            float(getattr(row, "score", 0) or 0),
            int(getattr(row, "clicks", 0) or 0),
            int(getattr(row, "impressions", 0) or 0),
        ),
        reverse=True,
    )
    for row in ranked_rows:
        url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
        key = _page_key(url)
        if not url or key in seen or _is_broad_or_category_landing_page(url):
            continue
        seen.add(key)
        pages.append(
            {
                "url": url,
                "page_key": key,
                "candidate_id": getattr(row, "id", None),
                "quality_label": getattr(row, "quality_label", ""),
                "score": float(getattr(row, "score", 0) or 0),
                "clicks": int(getattr(row, "clicks", 0) or 0),
                "impressions": int(getattr(row, "impressions", 0) or 0),
                "conversions": _row_total_conversions(row),
                "conversion_value": _row_total_value(row),
                **_landing_page_engagement_metrics(row),
                "source_account_id": getattr(row, "account_id", None),
                "first_seen_at": getattr(row, "first_seen_at", None).isoformat() if getattr(row, "first_seen_at", None) else None,
                "last_seen_at": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else None,
                "campaign_names": [str(item or "") for item in (getattr(row, "campaign_names", None) or [])],
                "scale_evidence": _scale_landing_page_evidence(row),
            }
        )
    for item in _matrix_scale_landing_page_items(
        ga4_matrix=ga4_matrix,
        search_console_matrix=search_console_matrix,
        seen_keys=seen,
    ):
        url = canonical_landing_page_url(str(item.get("url") or item.get("page_url") or ""))
        if url and not _is_broad_or_category_landing_page(url):
            pages.append(item)
    return {
        "active_pages": pages,
        "active_page_count": len(pages),
        "basis": (
            "Top saved Google Ads landing-page-bank URLs plus GA4 purchase/add-to-cart URLs and clicked Search "
            "Console pages, ranked by conversions, conversion value, score, clicks, and impressions. Testing page "
            "exclusions wait for the migration hold and Scale proof."
        ),
    }


def sync_scale_landing_page_migration_state(
    session: Session,
    account: GoogleAdsAccount,
    page_plan: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    now_iso = now.isoformat()
    state = load_scale_landing_page_migration_state(session, account)
    pages_state = state.get("pages") if isinstance(state.get("pages"), dict) else {}
    updated_pages: dict[str, Any] = dict(pages_state)
    active_keys: set[str] = set()
    for page in page_plan.get("active_pages") or []:
        if not isinstance(page, dict):
            continue
        url = canonical_landing_page_url(str(page.get("url") or ""))
        key = str(page.get("page_key") or _page_key(url))
        if not url or not key:
            continue
        active_keys.add(key)
        existing = updated_pages.get(key) if isinstance(updated_pages.get(key), dict) else {}
        scale_evidence = page.get("scale_evidence") if isinstance(page.get("scale_evidence"), dict) else {}
        scale_conversions = float(scale_evidence.get("scale_conversions") or 0)
        confirmed_at = existing.get("scale_conversion_confirmed_at_utc")
        if scale_conversions > 0 and not confirmed_at:
            confirmed_at = now_iso
        updated_pages[key] = {
            **existing,
            "url": url,
            "page_key": key,
            "promoted_at_utc": existing.get("promoted_at_utc") or now_iso,
            "last_planned_at_utc": now_iso,
            "scale_conversions": scale_conversions,
            "scale_conversion_value": float(scale_evidence.get("scale_conversion_value") or 0),
            "scale_campaign_names": scale_evidence.get("scale_campaign_names") or [],
            "scale_conversion_confirmed_at_utc": confirmed_at,
        }
    for key, existing in list(updated_pages.items()):
        if key not in active_keys and isinstance(existing, dict) and not existing.get("last_unplanned_at_utc"):
            updated_pages[key] = {**existing, "last_unplanned_at_utc": now_iso}
    state = {
        **state,
        "account_id": account.id,
        "customer_id": account.customer_id,
        "hold_days": SCALE_PAGE_MIGRATION_HOLD_DAYS,
        "updated_at_utc": now_iso,
        "active_page_count": len(active_keys),
        "pages": updated_pages,
    }
    save_scale_landing_page_migration_state(session, account, state)
    return state


def testing_scale_page_exclusion_plan(
    page_plan: dict[str, Any],
    *,
    migration_state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    hold_days: int = SCALE_PAGE_MIGRATION_HOLD_DAYS,
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    hold_days = clamp_int(hold_days, SCALE_PAGE_MIGRATION_HOLD_DAYS, 1, 60)
    state_pages = {}
    if isinstance(migration_state, dict) and isinstance(migration_state.get("pages"), dict):
        state_pages = migration_state["pages"]
    exclusions: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for page in page_plan.get("active_pages") or []:
        if not isinstance(page, dict):
            continue
        url = canonical_landing_page_url(str(page.get("url") or ""))
        key = str(page.get("page_key") or _page_key(url))
        if not url or not key:
            continue
        state = state_pages.get(key) if isinstance(state_pages.get(key), dict) else {}
        promoted_at = parse_iso_datetime(state.get("promoted_at_utc"))
        age_days = (now - promoted_at.astimezone(timezone.utc)).total_seconds() / 86400 if promoted_at else 0.0
        scale_evidence = page.get("scale_evidence") if isinstance(page.get("scale_evidence"), dict) else {}
        scale_conversions = max(float(scale_evidence.get("scale_conversions") or 0), float(state.get("scale_conversions") or 0))
        scale_conversion_value = max(float(scale_evidence.get("scale_conversion_value") or 0), float(state.get("scale_conversion_value") or 0))
        has_scale_conversion = scale_conversions > 0 or scale_conversion_value > 0 or bool(state.get("scale_conversion_confirmed_at_utc"))
        base = {
            "url": url,
            "page_key": key,
            "source": "core_scale_landing_page",
            "promoted_at_utc": state.get("promoted_at_utc"),
            "age_days": round(age_days, 2),
            "hold_days": hold_days,
            "scale_conversions": scale_conversions,
            "scale_conversion_value": scale_conversion_value,
            "scale_campaign_names": scale_evidence.get("scale_campaign_names") or state.get("scale_campaign_names") or [],
        }
        if age_days >= hold_days and has_scale_conversion:
            exclusions.append(
                {
                    **base,
                    "reason": (
                        f"Scale has conversion evidence for this landing page and the {hold_days}-day migration hold has passed; "
                        "exclude from Testing DSA/PMax page targeting to reduce internal competition."
                    ),
                }
            )
        else:
            reason = "Waiting for Scale landing-page conversion evidence."
            if has_scale_conversion and age_days < hold_days:
                reason = f"Scale has landing-page conversion evidence, but the {hold_days}-day migration hold has not passed."
            elif not has_scale_conversion and age_days < hold_days:
                reason = f"Waiting for both the {hold_days}-day migration hold and Scale landing-page conversion evidence."
            pending.append({**base, "reason": reason})
    return {
        "enabled": bool(exclusions),
        "applies_to": ["testing_dsa", "testing_pmax"],
        "rsa_handling": "Do not use excluded Scale-owned URLs as Testing RSA final URLs.",
        "hold_days": hold_days,
        "page_exclusions": exclusions,
        "page_exclusion_count": len(exclusions),
        "pending_pages": pending,
        "pending_page_count": len(pending),
        "basis": (
            f"Testing keeps newly promoted landing pages during a {hold_days}-day migration hold. A page becomes a Testing "
            "DSA/PMax exclusion only after Core / Scale has conversion evidence for it."
        ),
    }


def _landing_page_category_item(row: GoogleAdsLandingPageCandidate) -> dict[str, Any]:
    url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
    return {
        "candidate_id": getattr(row, "id", None),
        "url": url,
        "page_key": _page_key(url),
        "quality_label": getattr(row, "quality_label", ""),
        "score": float(getattr(row, "score", 0) or 0),
        "impressions": int(getattr(row, "impressions", 0) or 0),
        "clicks": int(getattr(row, "clicks", 0) or 0),
        "cost": float(getattr(row, "cost", 0) or 0),
        "conversions": _row_total_conversions(row),
        "conversion_value": _row_total_value(row),
        **_landing_page_engagement_metrics(row),
        "campaign_names": [str(item or "") for item in (getattr(row, "campaign_names", None) or [])],
        "source_dataset_keys": [str(item or "") for item in (getattr(row, "source_dataset_keys", None) or []) if str(item or "")],
    }


def _ranked_unique_landing_page_items(
    rows: list[GoogleAdsLandingPageCandidate],
    *,
    exclude_keys: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    exclude_keys = exclude_keys or set()
    ranked = sorted(
        rows,
        key=lambda row: (
            _row_total_conversions(row),
            _row_total_value(row),
            float(getattr(row, "score", 0) or 0),
            int(getattr(row, "clicks", 0) or 0),
            int(getattr(row, "impressions", 0) or 0),
        ),
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ranked:
        item = _landing_page_category_item(row)
        key = str(item.get("page_key") or "")
        if not key or key in seen or key in exclude_keys:
            continue
        seen.add(key)
        items.append(item)
        if limit and len(items) >= limit:
            break
    return items


def landing_page_category_governance_plan(
    page_rows: list[GoogleAdsLandingPageCandidate],
    *,
    website_url: str,
    scale_page_plan: dict[str, Any],
    scale_page_exclusion_plan: dict[str, Any],
    waste_plan: dict[str, Any],
    all_page_rows: Optional[list[GoogleAdsLandingPageCandidate]] = None,
    testing_limit: Optional[int] = None,
    fix_watch_limit: int = 25,
) -> dict[str, Any]:
    all_page_rows = all_page_rows or page_rows
    waste_page_plan = waste_plan.get("page_exclusion_plan") if isinstance(waste_plan.get("page_exclusion_plan"), dict) else {}
    active_waste_exclusions = merge_page_exclusions(waste_page_plan.get("active_page_exclusions") or [])
    scale_testing_exclusions = merge_page_exclusions(scale_page_exclusion_plan.get("page_exclusions") or [])
    testing_exclusions = merge_page_exclusions(scale_testing_exclusions, active_waste_exclusions)
    active_waste_keys = {str(item.get("page_key") or _page_key(item.get("url"))).strip().lower() for item in active_waste_exclusions}
    scale_exclusion_keys = {str(item.get("page_key") or _page_key(item.get("url"))).strip().lower() for item in scale_testing_exclusions}
    scale_pages = [
        item
        for item in (scale_page_plan.get("active_pages") or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    ]
    scale_keys = {str(item.get("page_key") or _page_key(item.get("url"))).strip().lower() for item in scale_pages}

    testing_candidates = _ranked_unique_landing_page_items(
        page_rows,
        exclude_keys=active_waste_keys | scale_exclusion_keys,
        limit=testing_limit,
    )

    fix_watch_rows: list[GoogleAdsLandingPageCandidate] = []
    blocked_fix_watch_keys = active_waste_keys | scale_keys | scale_exclusion_keys
    for row in all_page_rows:
        url = canonical_landing_page_url(str(getattr(row, "normalized_url", "") or getattr(row, "url", "") or ""))
        key = _page_key(url)
        if not key or key in blocked_fix_watch_keys:
            continue
        conversions = _row_total_conversions(row)
        conversion_value = _row_total_value(row)
        clicks = int(getattr(row, "clicks", 0) or 0)
        impressions = int(getattr(row, "impressions", 0) or 0)
        label = str(getattr(row, "quality_label", "") or "").lower()
        if conversions > 0 or conversion_value > 0:
            continue
        if label not in {"watch", "clicked"}:
            continue
        if impressions <= 500 or clicks >= 3:
            fix_watch_rows.append(row)
    fix_watch_candidates = _ranked_unique_landing_page_items(
        fix_watch_rows,
        exclude_keys=blocked_fix_watch_keys,
        limit=fix_watch_limit,
    )

    waste_scale_recovery_pages = [
        item
        for item in (waste_page_plan.get("scale_recovery_pages") or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    ]
    waste_testing_recovery_pages = [
        item
        for item in (waste_page_plan.get("testing_recovery_pages") or [])
        if isinstance(item, dict) and str(item.get("url") or "").strip()
    ]
    return {
        "enabled": True,
        "website_url": website_url,
        "dedupe_key": "canonical_landing_page_url",
        "controls": {
            "pmax": "Use page feeds/final URL expansion with category-specific URL exclusions.",
            "ai_max_search": "Use AI Max final URL expansion and page URL inclusions/exclusions where supported.",
            "rsa": "RSA cannot target landing pages directly; use approved final URLs only and block excluded URLs from ad drafts.",
            "legacy_dsa": "For older DSA-style drafts, use all-pages/page-feed targeting plus negative page targets.",
        },
        "categories": [
            {
                "name": "Core / Scale",
                "included_pages": scale_pages,
                "included_page_count": len(scale_pages),
                "recovery_pages": waste_scale_recovery_pages,
                "recovery_page_count": len(waste_scale_recovery_pages),
                "excluded_pages": active_waste_exclusions,
                "excluded_page_count": len(active_waste_exclusions),
                "basis": "Use conversion-backed landing pages for Scale PMax/Search AI Max page expansion; active Waste URLs stay excluded.",
            },
            {
                "name": "Testing / Discovery",
                "mode": "all_pages_with_guarded_exclusions",
                "candidate_pages": testing_candidates,
                "candidate_page_count": len(testing_candidates),
                "excluded_pages": testing_exclusions,
                "excluded_page_count": len(testing_exclusions),
                "pending_scale_pages": scale_page_exclusion_plan.get("pending_pages") or [],
                "pending_scale_page_count": int(scale_page_exclusion_plan.get("pending_page_count") or 0),
                "recovery_pages": waste_testing_recovery_pages,
                "recovery_page_count": len(waste_testing_recovery_pages),
                "basis": "Test all eligible website pages, but exclude active Waste pages and Scale-owned pages only after the migration hold plus Scale conversion proof.",
            },
            {
                "name": "Fix / Watch",
                "candidate_pages": fix_watch_candidates,
                "candidate_page_count": len(fix_watch_candidates),
                "target_roas": FIX_WATCH_TARGET_ROAS,
                "target_roas_pct": round(FIX_WATCH_TARGET_ROAS * 100),
                "budget_policy": "Up to 5% of rolling Odoo sales only when spend room remains.",
                "basis": "Use non-converting low-volume or clicked pages that are not already Scale, Testing-excluded, or Waste.",
            },
            {
                "name": "Waste / Recovery",
                "fixed_daily_budget": waste_plan.get("fixed_daily_budget", WASTE_FIXED_DAILY_BUDGET_AMOUNT),
                "active_excluded_pages": active_waste_exclusions,
                "active_excluded_page_count": len(active_waste_exclusions),
                "pending_recovery_pages": waste_page_plan.get("pending_recovery_pages") or [],
                "pending_recovery_page_count": int(waste_page_plan.get("pending_recovery_page_count") or 0),
                "scale_recovery_pages": waste_scale_recovery_pages,
                "scale_recovery_page_count": len(waste_scale_recovery_pages),
                "testing_recovery_pages": waste_testing_recovery_pages,
                "testing_recovery_page_count": len(waste_testing_recovery_pages),
                "basis": "Quarantine no-conversion page waste; recover only after fresh positive evidence and the Waste hold.",
            },
        ],
        "summary": {
            "scale_page_count": len(scale_pages),
            "testing_candidate_page_count": len(testing_candidates),
            "testing_excluded_page_count": len(testing_exclusions),
            "fix_watch_candidate_page_count": len(fix_watch_candidates),
            "waste_active_page_count": len(active_waste_exclusions),
        },
        "basis": (
            "Landing pages are assigned by canonical URL. Waste wins over every other category, Scale wins are "
            "kept in Testing during the migration hold, and page removals are represented as PMax/AI Max/DSA URL "
            "exclusions while RSA only receives final-URL allow/block guidance."
        ),
    }


def _clean_pmax_search_theme(value: Any) -> str:
    text = "".join(char if char.isalnum() or char.isspace() else " " for char in str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()[:10]
    trimmed = " ".join(words)
    if len(trimmed) <= PMAX_SEARCH_THEME_TEXT_LIMIT:
        return trimmed
    trimmed = trimmed[:PMAX_SEARCH_THEME_TEXT_LIMIT].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0].rstrip()
    return trimmed[:PMAX_SEARCH_THEME_TEXT_LIMIT].rstrip()


def _theme_key(value: Any) -> str:
    return _clean_pmax_search_theme(value).lower()


def _is_core_scale_campaign_name(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return "core scale" in normalized


def _is_waste_recovery_campaign_name(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if "waste recovery" in normalized or "waste review" in normalized:
        return True
    return "auto" in normalized and "waste" in normalized


def _scale_search_theme_evidence(row: GoogleAdsKeywordCandidate) -> dict[str, Any]:
    source = _row_source_json(row)
    source_rows = [item for item in (source.get("source_rows") or []) if isinstance(item, dict)]
    scale_rows = []
    for source_row in source_rows:
        if not _is_core_scale_campaign_name(source_row.get("campaign_name")):
            continue
        conversions = numeric_value(source_row, "conversions") + numeric_value(source_row, "all_conversions")
        conversion_value = numeric_value(source_row, "conversion_value") + numeric_value(source_row, "all_conversions_value")
        if conversions > 0 or conversion_value > 0:
            scale_rows.append(
                {
                    "campaign_id": source_row.get("campaign_id"),
                    "campaign_name": source_row.get("campaign_name"),
                    "conversions": conversions,
                    "conversion_value": conversion_value,
                }
            )
    scale_conversions = sum(float(item.get("conversions") or 0) for item in scale_rows)
    scale_conversion_value = sum(float(item.get("conversion_value") or 0) for item in scale_rows)
    scale_campaign_names = list(
        dict.fromkeys(str(item.get("campaign_name") or "").strip() for item in scale_rows if str(item.get("campaign_name") or "").strip())
    )
    campaign_names = [str(item or "").strip() for item in (getattr(row, "campaign_names", None) or []) if str(item or "").strip()]
    if not scale_rows and campaign_names and all(_is_core_scale_campaign_name(name) for name in campaign_names):
        scale_conversions = _row_total_conversions(row)
        scale_conversion_value = _row_total_value(row)
        scale_campaign_names = campaign_names
    return {
        "scale_conversions": scale_conversions,
        "scale_conversion_value": scale_conversion_value,
        "scale_campaign_names": scale_campaign_names,
        "scale_source_rows": scale_rows[:8],
        "has_scale_conversion": scale_conversions > 0 or scale_conversion_value > 0,
    }


def pmax_search_theme_plan(
    keyword_rows: list[GoogleAdsKeywordCandidate],
    *,
    limit: int = PMAX_SEARCH_THEME_LIMIT,
    asset_group_limit: int = PMAX_ASSET_GROUP_LIMIT,
    overflow_limit: Optional[int] = None,
    policy_terms: list[str] | None = None,
) -> dict[str, Any]:
    limit = clamp_int(limit, PMAX_SEARCH_THEME_LIMIT, 1, PMAX_SEARCH_THEME_LIMIT)
    asset_group_limit = clamp_int(asset_group_limit, PMAX_ASSET_GROUP_LIMIT, 1, PMAX_ASSET_GROUP_LIMIT)
    target_overflow_limit = int(overflow_limit or 0)
    ranked_rows = sorted(
        keyword_rows,
        key=lambda row: (
            _row_total_conversions(row),
            _row_total_value(row),
            float(getattr(row, "score", 0) or 0),
            int(getattr(row, "clicks", 0) or 0),
            int(getattr(row, "impressions", 0) or 0),
        ),
        reverse=True,
    )
    candidates: list[dict[str, Any]] = []
    policy_filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in ranked_rows:
        term = _clean_pmax_search_theme(getattr(row, "keyword", ""))
        key = _theme_key(term)
        if len(term) < 3 or key in seen:
            continue
        policy_match = _pmax_policy_match(_keyword_row_policy_text(row) or term, policy_terms)
        if policy_match:
            policy_filtered.append(
                {
                    "search_theme": term,
                    "theme_key": key,
                    "candidate_id": getattr(row, "id", None),
                    "matched_term": policy_match,
                    "source_account_id": getattr(row, "account_id", None),
                }
            )
            seen.add(key)
            continue
        seen.add(key)
        scale_evidence = _scale_search_theme_evidence(row)
        candidates.append(
            {
                "search_theme": term,
                "theme_key": key,
                "candidate_id": getattr(row, "id", None),
                "quality_label": getattr(row, "quality_label", ""),
                "score": float(getattr(row, "score", 0) or 0),
                "clicks": int(getattr(row, "clicks", 0) or 0),
                "impressions": int(getattr(row, "impressions", 0) or 0),
                "conversions": _row_total_conversions(row),
                "conversion_value": _row_total_value(row),
                "source_account_id": getattr(row, "account_id", None),
                "first_seen_at": getattr(row, "first_seen_at", None).isoformat() if getattr(row, "first_seen_at", None) else None,
                "last_seen_at": getattr(row, "last_seen_at", None).isoformat() if getattr(row, "last_seen_at", None) else None,
                "campaign_names": [str(item or "") for item in (getattr(row, "campaign_names", None) or [])],
                "scale_evidence": scale_evidence,
            }
        )
        if target_overflow_limit > 0 and len(candidates) >= target_overflow_limit:
            break
    asset_groups: list[dict[str, Any]] = []
    for start in range(0, len(candidates), limit):
        bucket = candidates[start : start + limit]
        if not bucket:
            continue
        asset_group_index = len(asset_groups) + 1
        asset_groups.append(
            {
                "asset_group_index": asset_group_index,
                "asset_group_code": f"AG{asset_group_index:03d}",
                "search_theme_count": len(bucket),
                "search_themes": [item["search_theme"] for item in bucket],
                "candidates": bucket,
            }
        )
    campaign_plans: list[dict[str, Any]] = []
    for start in range(0, len(asset_groups), asset_group_limit):
        group_bucket = asset_groups[start : start + asset_group_limit]
        if not group_bucket:
            continue
        campaign_index = len(campaign_plans) + 1
        campaign_plans.append(
            {
                "campaign_index": campaign_index,
                "campaign_code_suffix": f"S{campaign_index:03d}",
                "asset_group_count": len(group_bucket),
                "search_theme_count": sum(int(group.get("search_theme_count") or 0) for group in group_bucket),
                "asset_groups": group_bucket,
                "requires_new_campaign": campaign_index > 1,
            }
        )
    active = asset_groups[0]["candidates"] if asset_groups else []
    additional_asset_group_candidates = candidates[limit:]
    pending_partial_asset_group_candidates = candidates[len(asset_groups) * limit :]
    planned_capacity = limit * asset_group_limit * max(len(campaign_plans), 1)
    overflow = candidates[planned_capacity:]
    return {
        "limit": limit,
        "asset_group_limit": asset_group_limit,
        "campaign_search_theme_capacity": limit * asset_group_limit,
        "text_limit": PMAX_SEARCH_THEME_TEXT_LIMIT,
        "active_terms": [item["search_theme"] for item in active],
        "active": active,
        "additional_asset_group_term_count": len(additional_asset_group_candidates),
        "additional_asset_group_candidates": additional_asset_group_candidates[:100],
        "pending_partial_asset_group_term_count": len(pending_partial_asset_group_candidates),
        "pending_partial_asset_group_candidates": pending_partial_asset_group_candidates[:100],
        "overflow_count": len(overflow),
        "overflow_candidates": overflow[:100],
        "candidate_count": len(candidates),
        "asset_group_count": len(asset_groups),
        "campaign_count": len(campaign_plans),
        "campaign_plans": campaign_plans,
        "new_campaign_required": len(campaign_plans) > 1,
        "pmax_policy_filter": {
            "mode": "search_theme_policy_safe_subset",
            "restricted_term_count": len(policy_terms or []),
            "blocked_search_theme_count": len(policy_filtered),
            "blocked_search_themes": policy_filtered[:100],
        },
        "basis": (
            "Top saved Google Ads keyword-bank terms ranked by conversions, conversion value, "
            "score, clicks, and impressions. Terms are sharded into 50 search themes per asset group, "
            "100 asset groups per PMax campaign, then additional Core / Scale PMax campaigns."
        ),
    }


def _keyword_row_key(row: GoogleAdsKeywordCandidate) -> str:
    return str(getattr(row, "normalized_keyword", "") or _theme_key(getattr(row, "keyword", ""))).strip().lower()


def _term_key_from_text(value: Any) -> str:
    return _theme_key(str(value or ""))


def _pmax_theme_keys(plan: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for candidate in _pmax_plan_candidates(plan):
        key = str(candidate.get("theme_key") or _term_key_from_text(candidate.get("search_theme"))).strip().lower()
        if key:
            keys.add(key)
    return keys


def available_keyword_rows_for_terms(
    rows: list[GoogleAdsKeywordCandidate],
    used_keys: set[str],
) -> list[GoogleAdsKeywordCandidate]:
    available: list[GoogleAdsKeywordCandidate] = []
    seen: set[str] = set()
    for row in rows:
        key = _keyword_row_key(row)
        if not key or key in used_keys or key in seen:
            continue
        seen.add(key)
        available.append(row)
    return available


def core_owned_keyword_rows_for_scale_migration(
    rows: list[GoogleAdsKeywordCandidate],
    account: GoogleAdsAccount,
) -> list[GoogleAdsKeywordCandidate]:
    return [row for row in rows if getattr(row, "account_id", None) == account.id]


def claim_keyword_terms(
    rows: list[GoogleAdsKeywordCandidate],
    used_keys: set[str],
    *,
    limit: Optional[int] = None,
) -> tuple[list[str], list[GoogleAdsKeywordCandidate]]:
    terms: list[str] = []
    claimed_rows: list[GoogleAdsKeywordCandidate] = []
    target_limit = int(limit or 0)
    for row in rows:
        key = _keyword_row_key(row)
        term = _clean_pmax_search_theme(getattr(row, "keyword", ""))
        if not key or not term or key in used_keys:
            continue
        used_keys.add(key)
        terms.append(term)
        claimed_rows.append(row)
        if target_limit > 0 and len(terms) >= target_limit:
            break
    return terms, claimed_rows


def _pmax_plan_candidates(pmax_plan: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for campaign in pmax_plan.get("campaign_plans") or []:
        if not isinstance(campaign, dict):
            continue
        for asset_group in campaign.get("asset_groups") or []:
            if not isinstance(asset_group, dict):
                continue
            for candidate in asset_group.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                term = str(candidate.get("search_theme") or "").strip()
                key = str(candidate.get("theme_key") or _theme_key(term))
                if not term or key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
    return candidates


def sync_scale_search_theme_migration_state(
    session: Session,
    account: GoogleAdsAccount,
    pmax_plan: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    now_iso = now.isoformat()
    state = load_scale_search_theme_migration_state(session, account)
    terms_state = state.get("terms") if isinstance(state.get("terms"), dict) else {}
    updated_terms: dict[str, Any] = dict(terms_state)
    active_keys: set[str] = set()
    for candidate in _pmax_plan_candidates(pmax_plan):
        term = str(candidate.get("search_theme") or "").strip()
        key = str(candidate.get("theme_key") or _theme_key(term))
        if not term or not key:
            continue
        active_keys.add(key)
        existing = updated_terms.get(key) if isinstance(updated_terms.get(key), dict) else {}
        scale_evidence = candidate.get("scale_evidence") if isinstance(candidate.get("scale_evidence"), dict) else {}
        scale_conversions = float(scale_evidence.get("scale_conversions") or 0)
        confirmed_at = existing.get("scale_conversion_confirmed_at_utc")
        if scale_conversions > 0 and not confirmed_at:
            confirmed_at = now_iso
        updated_terms[key] = {
            **existing,
            "search_theme": term,
            "theme_key": key,
            "promoted_at_utc": existing.get("promoted_at_utc") or now_iso,
            "last_planned_at_utc": now_iso,
            "scale_conversions": scale_conversions,
            "scale_conversion_value": float(scale_evidence.get("scale_conversion_value") or 0),
            "scale_campaign_names": scale_evidence.get("scale_campaign_names") or [],
            "scale_conversion_confirmed_at_utc": confirmed_at,
        }
    for key, existing in list(updated_terms.items()):
        if key not in active_keys and isinstance(existing, dict) and not existing.get("last_unplanned_at_utc"):
            updated_terms[key] = {**existing, "last_unplanned_at_utc": now_iso}
    state = {
        **state,
        "account_id": account.id,
        "customer_id": account.customer_id,
        "hold_days": SCALE_NEGATIVE_MIGRATION_HOLD_DAYS,
        "updated_at_utc": now_iso,
        "active_term_count": len(active_keys),
        "terms": updated_terms,
    }
    save_scale_search_theme_migration_state(session, account, state)
    return state


def testing_scale_negative_keyword_plan(
    pmax_plan: dict[str, Any],
    *,
    migration_state: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    hold_days: int = SCALE_NEGATIVE_MIGRATION_HOLD_DAYS,
) -> dict[str, Any]:
    now = (now or utcnow()).astimezone(timezone.utc)
    hold_days = clamp_int(hold_days, SCALE_NEGATIVE_MIGRATION_HOLD_DAYS, 1, 60)
    state_terms = {}
    if isinstance(migration_state, dict) and isinstance(migration_state.get("terms"), dict):
        state_terms = migration_state["terms"]
    negatives = [
    ]
    pending: list[dict[str, Any]] = []
    for candidate in _pmax_plan_candidates(pmax_plan):
        term = str(candidate.get("search_theme") or "").strip()
        key = str(candidate.get("theme_key") or _theme_key(term))
        if not term or not key:
            continue
        state = state_terms.get(key) if isinstance(state_terms.get(key), dict) else {}
        promoted_at = parse_iso_datetime(state.get("promoted_at_utc"))
        age_days = (now - promoted_at.astimezone(timezone.utc)).total_seconds() / 86400 if promoted_at else 0.0
        scale_evidence = candidate.get("scale_evidence") if isinstance(candidate.get("scale_evidence"), dict) else {}
        scale_conversions = max(float(scale_evidence.get("scale_conversions") or 0), float(state.get("scale_conversions") or 0))
        scale_conversion_value = max(float(scale_evidence.get("scale_conversion_value") or 0), float(state.get("scale_conversion_value") or 0))
        has_scale_conversion = scale_conversions > 0 or scale_conversion_value > 0 or bool(state.get("scale_conversion_confirmed_at_utc"))
        base = {
            "keyword": term,
            "match_type": "exact",
            "source": "core_scale_keyword_or_search_theme",
            "theme_key": key,
            "promoted_at_utc": state.get("promoted_at_utc"),
            "age_days": round(age_days, 2),
            "hold_days": hold_days,
            "scale_conversions": scale_conversions,
            "scale_conversion_value": scale_conversion_value,
            "scale_campaign_names": scale_evidence.get("scale_campaign_names") or state.get("scale_campaign_names") or [],
        }
        if age_days >= hold_days and has_scale_conversion:
            negatives.append(
                {
                    **base,
                    "reason": (
                        f"Core / Scale has conversion evidence and the {hold_days}-day migration hold has passed; "
                        "exclude from Testing / Discovery to reduce internal competition."
                    ),
                }
            )
        else:
            reason = "Waiting for Core / Scale conversion evidence."
            if has_scale_conversion and age_days < hold_days:
                reason = f"Core / Scale has conversion evidence, but the {hold_days}-day migration hold has not passed."
            elif not has_scale_conversion and age_days < hold_days:
                reason = f"Waiting for both the {hold_days}-day migration hold and Core / Scale conversion evidence."
            pending.append({**base, "reason": reason})
    return {
        "enabled": bool(negatives),
        "applies_to": ["testing_rsa", "testing_dsa", "testing_pmax"],
        "match_type": "exact",
        "hold_days": hold_days,
        "negative_keywords": negatives,
        "negative_keyword_count": len(negatives),
        "pending_keywords": pending,
        "pending_keyword_count": len(pending),
        "basis": (
            f"Testing keeps newly promoted winners during a {hold_days}-day migration hold. A term becomes a Testing "
            "negative only after Core / Scale has conversion evidence for it."
        ),
    }


def _compact_list(value: Any, *, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[: max(int(limit or 0), 0)]


def _compact_url_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compacted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            url = canonical_landing_page_url(str(item.get("url") or item.get("final_url") or item.get("landing_page") or ""))
            source = str(item.get("source") or "")
            page_key = str(item.get("page_key") or _page_key(url) or "")
            payload = {
                "url": url,
                "page_key": page_key,
                "source": source,
                "score": float(item.get("score") or 0),
                "conversions": float(item.get("conversions") or item.get("purchases") or 0),
                "conversion_value": float(item.get("conversion_value") or item.get("revenue") or 0),
                "quality_label": str(item.get("quality_label") or ""),
            }
        else:
            url = canonical_landing_page_url(str(item or ""))
            payload = {"url": url, "page_key": _page_key(url), "source": "url_target"}
        key = _page_key(url)
        if not url or not usable_landing_page_url(url) or not key or key in seen:
            continue
        seen.add(key)
        compacted.append(payload)
    return compacted


def _compact_page_targeting(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = dict(value)
    for key in ("url_inclusions", "best_landing_pages"):
        if isinstance(compact.get(key), list):
            compact[f"{key}_count"] = len(_compact_url_targets(compact.get(key)))
            compact.pop(key, None)
    return compact


def _compact_signal_matrix(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "status": value.get("status"),
        "summary": value.get("summary") if isinstance(value.get("summary"), dict) else {},
        "basis": value.get("basis"),
        "compacted": True,
    }


def _planning_items(value: Any, limit: Optional[int] = None) -> list[Any]:
    if value is None:
        return []
    items = list(value) if isinstance(value, (list, tuple)) else []
    target_limit = int(limit or 0)
    return items[:target_limit] if target_limit > 0 else items


def _compact_generated_assets_for_storage(generated_assets: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(generated_assets, dict):
        return {}
    compact = dict(generated_assets)
    evidence_limits = {
        "landing_page_candidates": 50,
        "landing_page_candidate_rows": 50,
        "keyword_candidates": 100,
        "negative_keyword_candidates": 100,
        "blocked_final_urls": 200,
        "negative_page_targets": 200,
    }
    for key, limit in evidence_limits.items():
        if isinstance(compact.get(key), list):
            compact[key] = _compact_list(compact.get(key), limit=limit)
    for key in ("url_inclusion_targets", "page_feed_targets"):
        if isinstance(compact.get(key), list):
            compact[key] = _compact_url_targets(compact.get(key))
    if isinstance(compact.get("page_targeting"), dict):
        compact["page_targeting"] = _compact_page_targeting(compact.get("page_targeting"))
    for key in (
        "ga4_ads_signal_matrix",
        "search_console_ads_signal_matrix",
        "landing_page_governance_plan",
        "waste_management_plan",
    ):
        if isinstance(compact.get(key), dict):
            compact[key] = _compact_signal_matrix(compact.get(key))
    automation = compact.get("automation") if isinstance(compact.get("automation"), dict) else None
    if automation:
        compact_automation = dict(automation)
        if isinstance(compact_automation.get("page_targeting"), dict):
            compact_automation["page_targeting"] = _compact_page_targeting(compact_automation.get("page_targeting"))
        for key in (
            "ga4_ads_signal_matrix",
            "search_console_ads_signal_matrix",
            "landing_page_governance_plan",
            "waste_management_plan",
        ):
            value = compact_automation.get(key)
            if isinstance(value, dict):
                compact_automation[key] = _compact_signal_matrix(value)
        compact["automation"] = compact_automation
    return compact


def upsert_automation_ad_draft(
    session: Session,
    *,
    preference: GoogleAdsAutomationPreference,
    ad_type: str,
    source_key: str,
    website_url: str,
    final_url: str,
    business_name: str,
    generated_assets: dict[str, Any],
    validation_json: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    existing = _existing_automation_draft(
        session,
        account_id=preference.account_id,
        ad_type=ad_type,
        source_key=source_key,
    )
    generated_assets = _compact_generated_assets_for_storage(generated_assets)
    status = "ready_for_review" if validation_json.get("ok") else "needs_review"
    if existing is None:
        draft = AdDraft(
            account_id=preference.account_id,
            created_by_id=None,
            ad_type=ad_type,
            status=status,
            website_url=website_url,
            final_url=final_url,
            business_name=business_name,
            prompt=prompt,
            generated_assets=generated_assets,
            validation_json=validation_json,
        )
        session.add(draft)
        session.flush()
        draft_id = draft.id
        draft_status = draft.status
        created = True
    else:
        draft_id = existing.id
        session.execute(
            update(AdDraft)
            .where(AdDraft.id == draft_id)
            .values(
                status=status,
                website_url=website_url,
                final_url=final_url,
                business_name=business_name,
                prompt=prompt,
                generated_assets=generated_assets,
                validation_json=validation_json,
            )
        )
        draft_status = status
        created = False
    assets = generated_assets if isinstance(generated_assets, dict) else {}
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
    return {
        "draft_id": draft_id,
        "ad_type": ad_type,
        "status": draft_status,
        "created": created,
        "source_key": source_key,
        "campaign_name": identity.get("campaign_name") or assets.get("campaign_name") or "",
        "campaign_code": identity.get("campaign_code") or assets.get("campaign_code") or "",
        "planned_operation": automation.get("planned_operation") or assets.get("planned_operation") or "",
        "existing_google_campaign": automation.get("existing_google_campaign") or assets.get("existing_google_campaign"),
    }


def _automation_validation(base_validation: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    validation = dict(base_validation or {})
    validation.setdefault("warnings", [])
    validation["warnings"] = list(validation["warnings"]) + warnings
    validation["automation_review_required"] = True
    return validation


def block_automation_campaign_drafts_for_budget(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    budget: dict[str, Any],
) -> list[dict[str, Any]]:
    reason = str(budget.get("budget_block_reason") or "Odoo sales guard blocks automated campaign spend.")
    rows = session.scalars(
        select(AdDraft)
        .where(AdDraft.account_id == preference.account_id)
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        .limit(200)
    ).all()
    blocked: list[dict[str, Any]] = []
    for draft in rows:
        assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
        automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
        source_key = str(automation.get("source_key") or "")
        if not source_key.startswith("automation:"):
            continue
        validation = dict(draft.validation_json or {})
        errors = list(validation.get("errors") or [])
        warnings = list(validation.get("warnings") or [])
        if reason not in errors:
            errors.append(reason)
        warning = "Automation draft blocked until Odoo sales spend room is available."
        if warning not in warnings:
            warnings.append(warning)
        validation["ok"] = False
        validation["errors"] = errors
        validation["warnings"] = warnings
        validation["automation_budget_blocked"] = True
        validation["budget"] = budget
        assets = {
            **assets,
            "budget_guard_blocked": True,
            "budget_guard_block_reason": reason,
            "bidding": {
                **(assets.get("bidding") if isinstance(assets.get("bidding"), dict) else {}),
                "daily_budget": 0.0,
                "budget_blocked": True,
                "budget_block_reason": reason,
            },
        }
        draft.generated_assets = assets
        draft.validation_json = validation
        draft.status = "publish_blocked"
        blocked.append(
            {
                "draft_id": draft.id,
                "ad_type": draft.ad_type,
                "source_key": source_key,
                "campaign_name": (assets.get("campaign_identity") or {}).get("campaign_name") if isinstance(assets.get("campaign_identity"), dict) else assets.get("campaign_name", ""),
            }
        )
    if blocked:
        session.flush()
    return blocked


def block_automation_campaign_drafts_for_data_freshness(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    data_freshness: dict[str, Any],
) -> list[dict[str, Any]]:
    reason = str(data_freshness.get("reason") or "Fresh Google Ads data is required before automated campaign publishing.")
    rows = session.scalars(
        select(AdDraft)
        .where(AdDraft.account_id == preference.account_id)
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        .limit(200)
    ).all()
    blocked: list[dict[str, Any]] = []
    for draft in rows:
        assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
        automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
        source_key = str(automation.get("source_key") or "")
        if not source_key.startswith("automation:"):
            continue
        validation = dict(draft.validation_json or {})
        errors = list(validation.get("errors") or [])
        warnings = list(validation.get("warnings") or [])
        if reason not in errors:
            errors.append(reason)
        warning = "Automation draft blocked until fresh Google Ads metrics and insight datasets are available."
        if warning not in warnings:
            warnings.append(warning)
        validation["ok"] = False
        validation["errors"] = errors
        validation["warnings"] = warnings
        validation["automation_data_freshness_blocked"] = True
        validation["data_freshness"] = data_freshness
        assets = {
            **assets,
            "data_freshness_blocked": True,
            "data_freshness_block_reason": reason,
            "data_freshness": data_freshness,
        }
        draft.generated_assets = assets
        draft.validation_json = validation
        draft.status = "publish_blocked"
        blocked.append(
            {
                "draft_id": draft.id,
                "ad_type": draft.ad_type,
                "source_key": source_key,
                "campaign_name": (assets.get("campaign_identity") or {}).get("campaign_name") if isinstance(assets.get("campaign_identity"), dict) else assets.get("campaign_name", ""),
            }
        )
    if blocked:
        session.flush()
    return blocked


LEGACY_MAX_CLICKS_AUTOMATION_DRAFT_KINDS = (
    "testing_keyword_rsa_max_clicks_cpc_cap",
    "core_scale_keyword_rsa_max_clicks_cpc_cap",
)


def retire_legacy_max_clicks_automation_drafts(
    session: Session,
    preference: GoogleAdsAutomationPreference,
) -> int:
    if preference.account is None:
        return 0
    source_keys = {
        _automation_draft_source_key(preference.account, kind)
        for kind in LEGACY_MAX_CLICKS_AUTOMATION_DRAFT_KINDS
    }
    rows = session.scalars(
        select(AdDraft)
        .where(
            AdDraft.account_id == preference.account_id,
            AdDraft.ad_type == "rsa",
        )
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        .limit(500)
    ).all()
    retired = 0
    for draft in rows:
        assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
        automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
        source_key = str(automation.get("source_key") or "").strip()
        if source_key not in source_keys or str(draft.status or "").lower() == "retired":
            continue
        validation = dict(draft.validation_json or {})
        warnings = list(validation.get("warnings") or [])
        warning = "Legacy Max Clicks automation lane retired; automation now uses Target ROAS RSA/DSA only."
        if warning not in warnings:
            warnings.append(warning)
        validation["ok"] = False
        validation["warnings"] = warnings
        validation["legacy_max_clicks_retired"] = True
        draft.status = "retired"
        draft.validation_json = validation
        retired += 1
    if retired:
        session.flush()
    return retired


def run_testing_campaign_automation(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    source_job_id: Optional[int] = None,
    data_freshness: Optional[dict[str, Any]] = None,
    require_fresh_data: bool = True,
) -> dict[str, Any]:
    account = preference.account
    if not preference.testing_bootstrap_enabled:
        return {"name": "testing_campaign_automation", "status": "skipped", "reason": "Testing bootstrap is disabled."}
    if require_fresh_data and (not isinstance(data_freshness, dict) or not data_freshness.get("ok")):
        return {
            "name": "testing_campaign_automation",
            "status": "deferred_stale_data",
            "reason": (
                str(data_freshness.get("reason"))
                if isinstance(data_freshness, dict) and data_freshness.get("reason")
                else "Fresh data token is required before automated campaign planning."
            ),
            "data_freshness": data_freshness or {"ok": False, "status": "missing"},
        }

    decision = campaign_bootstrap_decision(session, preference)
    website_url = mapped_website_url_for_account(session, account)
    if not website_url:
        return {
            "name": "testing_campaign_automation",
            "status": "skipped",
            "reason": "No mapped Odoo website/domain is available for campaign drafts.",
            "decision": decision,
        }
    try:
        ga4_matrix = ga4_ads_signal_matrix(session, account.id, limit=0)
    except Exception as exc:  # noqa: BLE001 - saved Ads data remains enough to keep planning guarded.
        ga4_matrix = {
            "generated_at": utcnow().isoformat(),
            "account_id": account.id,
            "status": "failed",
            "error": str(exc)[:500],
            "summary": {},
            "protect_keyword_keys": [],
            "protect_page_keys": [],
            "basis": "GA4 matrix failed, so this plan used Google Ads data only.",
        }
    try:
        search_console_matrix = search_console_ads_signal_matrix(session, account.id, limit=0)
    except Exception as exc:  # noqa: BLE001 - Search Console is additive, not a hard planning dependency.
        search_console_matrix = {
            "generated_at": utcnow().isoformat(),
            "account_id": account.id,
            "status": "failed",
            "error": str(exc)[:500],
            "summary": {},
            "protect_keyword_keys": [],
            "protect_page_keys": [],
            "basis": "Search Console matrix failed, so this plan used Google Ads and GA4 data only.",
        }
    ga4_protected_keyword_keys = {
        str(item or "").strip().lower()
        for item in (ga4_matrix.get("protect_keyword_keys") or [])
        if str(item or "").strip()
    }
    search_console_protected_keyword_keys = {
        str(item or "").strip().lower()
        for item in (search_console_matrix.get("protect_keyword_keys") or [])
        if str(item or "").strip()
    }
    protected_keyword_keys = ga4_protected_keyword_keys | search_console_protected_keyword_keys
    ga4_protected_page_keys = {
        str(item or "").strip().lower()
        for item in (ga4_matrix.get("protect_page_keys") or [])
        if str(item or "").strip()
    }
    search_console_protected_page_keys = {
        str(item or "").strip().lower()
        for item in (search_console_matrix.get("protect_page_keys") or [])
        if str(item or "").strip()
    }
    protected_page_keys = ga4_protected_page_keys | search_console_protected_page_keys

    keyword_limit = max(int(preference.testing_keyword_limit or DEFAULT_TESTING_KEYWORD_LIMIT or 0), 0)
    keyword_fetch_limit = 0
    landing_page_limit = max(int(preference.testing_landing_page_limit or DEFAULT_TESTING_LANDING_PAGE_LIMIT or 0), 0)
    keyword_rows, keyword_source = best_keyword_candidates_for_testing(
        session,
        account,
        limit=keyword_fetch_limit,
        allow_cross_account_fallback=True,
    )
    page_rows, page_source = best_landing_page_candidates_for_testing(
        session,
        account,
        website_url=website_url,
        limit=landing_page_limit,
    )
    waste_negative_rows = negative_keyword_candidates_for_waste_review(session, account)
    waste_keyword_rows = keyword_candidates_for_waste_review(session, account)
    waste_page_rows = landing_page_candidates_for_waste_review(session, account)
    if protected_keyword_keys:
        waste_negative_rows = [
            row
            for row in waste_negative_rows
            if str(getattr(row, "normalized_keyword", "") or _theme_key(getattr(row, "keyword", ""))).strip().lower()
            not in protected_keyword_keys
        ]
    if protected_page_keys:
        waste_page_rows = [
            row
            for row in waste_page_rows
            if _page_key(getattr(row, "normalized_url", "") or getattr(row, "url", "")) not in protected_page_keys
        ]
    waste_plan = waste_category_plan(
        waste_negative_rows,
        waste_keyword_rows,
        waste_page_rows,
        recovery_state=load_waste_recovery_state(session, account),
        currency_code=account.currency_code or "USD",
    )
    waste_budget_amount = waste_fixed_daily_budget(account)
    waste_plan["budget"] = waste_budget_amount
    waste_plan["fixed_daily_budget"] = waste_budget_amount
    save_waste_recovery_state(session, account, waste_plan["state"])
    waste_negative_plan = waste_plan["negative_keyword_plan"]
    waste_page_exclusion_plan = waste_plan["page_exclusion_plan"]
    active_waste_keyword_keys = {
        str(item.get("theme_key") or _theme_key(item.get("keyword"))).strip().lower()
        for item in waste_negative_plan.get("active_negative_keywords") or []
        if str(item.get("keyword") or "").strip()
    }
    active_waste_page_keys = {
        str(item.get("page_key") or _page_key(item.get("url"))).strip().lower()
        for item in waste_page_exclusion_plan.get("active_page_exclusions") or []
        if str(item.get("url") or "").strip()
    }
    if active_waste_keyword_keys:
        keyword_rows = [
            row
            for row in keyword_rows
            if str(getattr(row, "normalized_keyword", "") or _theme_key(getattr(row, "keyword", ""))).strip().lower()
            not in active_waste_keyword_keys
        ]
    if active_waste_page_keys:
        page_rows = [
            row
            for row in page_rows
            if _page_key(getattr(row, "normalized_url", "") or getattr(row, "url", "")) not in active_waste_page_keys
        ]
    pmax_policy_terms = _pmax_policy_terms(session)
    term_ledger_keys: set[str] = set()
    same_account_keyword_rows = core_owned_keyword_rows_for_scale_migration(keyword_rows, account)
    pmax_source_rows = same_account_keyword_rows or list(keyword_rows)
    pmax_theme_plan = pmax_search_theme_plan(pmax_source_rows, policy_terms=pmax_policy_terms)
    pmax_search_themes = list(pmax_theme_plan["active_terms"])
    lane_keyword_limit = keyword_limit or (max((len(keyword_rows) + 2) // 3, 1) if keyword_rows else 0)
    core_rsa_terms, core_rsa_rows = claim_keyword_terms(keyword_rows, term_ledger_keys, limit=lane_keyword_limit)
    testing_terms, testing_rows = claim_keyword_terms(keyword_rows, term_ledger_keys, limit=lane_keyword_limit)
    fix_watch_terms, fix_watch_rows = claim_keyword_terms(keyword_rows, term_ledger_keys, limit=lane_keyword_limit)
    search_console_terms = search_console_keyword_terms(search_console_matrix, limit=0)

    def fill_with_search_console_terms(target_terms: list[str], *, limit: Optional[int] = None) -> None:
        target_limit = int(limit or 0)
        for term in search_console_terms:
            key = _term_key_from_text(term)
            if not term or not key or key in term_ledger_keys:
                continue
            term_ledger_keys.add(key)
            target_terms.append(term)
            if target_limit > 0 and len(target_terms) >= target_limit:
                return

    def distribute_search_console_terms(*targets: list[str], limit: Optional[int] = None) -> None:
        active_targets = [target for target in targets if isinstance(target, list)]
        if not active_targets:
            return
        target_limit = int(limit or 0)
        target_index = 0
        for term in search_console_terms:
            key = _term_key_from_text(term)
            if not term or not key or key in term_ledger_keys:
                continue
            attempts = 0
            selected_target: Optional[list[str]] = None
            while attempts < len(active_targets):
                candidate = active_targets[target_index % len(active_targets)]
                target_index += 1
                attempts += 1
                if target_limit <= 0 or len(candidate) < target_limit:
                    selected_target = candidate
                    break
            if selected_target is None:
                return
            term_ledger_keys.add(key)
            selected_target.append(term)

    distribute_search_console_terms(
        core_rsa_terms,
        testing_terms,
        fix_watch_terms,
        limit=keyword_limit,
    )
    testing_pmax_source_rows = available_keyword_rows_for_terms(keyword_rows, term_ledger_keys)
    testing_pmax_theme_plan = pmax_search_theme_plan(testing_pmax_source_rows, policy_terms=pmax_policy_terms)
    term_ledger_keys.update(_pmax_theme_keys(testing_pmax_theme_plan))
    waste_recovery_keyword_items = (
        list(waste_negative_plan.get("scale_recovery_keywords") or [])
        + list(waste_negative_plan.get("testing_recovery_keywords") or [])
        + list(waste_negative_plan.get("pending_recovery_keywords") or [])
    )
    waste_recovery_terms: list[str] = []
    for item in waste_recovery_keyword_items:
        if not isinstance(item, dict):
            continue
        term = _clean_pmax_search_theme(item.get("keyword"))
        key = str(item.get("theme_key") or _term_key_from_text(term)).strip().lower()
        if not term or not key or key in term_ledger_keys:
            continue
        term_ledger_keys.add(key)
        waste_recovery_terms.append(term)
        if len(waste_recovery_terms) >= keyword_limit:
            break
    keywords = testing_terms
    testing_rsa_terms = (
        testing_terms
        or [
            term
            for term in search_console_terms
            if _term_key_from_text(term) not in term_ledger_keys
        ][: lane_keyword_limit or 50]
        or [
            item
            for item in (
                f"{account.name} online",
                "shop online",
                "trusted online store",
            )
            if _term_key_from_text(item)
        ]
    )
    scale_page_plan = scale_landing_page_plan(
        page_rows,
        ga4_matrix=ga4_matrix,
        search_console_matrix=search_console_matrix,
    )
    core_owned_theme_plan = pmax_search_theme_plan(same_account_keyword_rows, policy_terms=pmax_policy_terms)
    scale_migration_state = sync_scale_search_theme_migration_state(session, account, core_owned_theme_plan)
    scale_negative_plan = testing_scale_negative_keyword_plan(
        core_owned_theme_plan,
        migration_state=scale_migration_state,
    )
    scale_page_migration_state = sync_scale_landing_page_migration_state(session, account, scale_page_plan)
    scale_page_exclusion_plan = testing_scale_page_exclusion_plan(
        scale_page_plan,
        migration_state=scale_page_migration_state,
    )
    if not decision.get("pmax_allowed"):
        scale_negative_plan = {
            **scale_negative_plan,
            "pmax_gate": decision.get("pmax_gate") or {},
            "basis": (
                scale_negative_plan.get("basis", "")
                + " PMax live publishing is still gated, but Core / Scale Search-owned terms remain tracked for Testing negatives."
            ).strip(),
        }
        scale_page_exclusion_plan = {
            **scale_page_exclusion_plan,
            "pmax_gate": decision.get("pmax_gate") or {},
            "basis": (
                scale_page_exclusion_plan.get("basis", "")
                + " PMax live publishing is still gated, but Core / Scale Search-owned URLs remain tracked for Testing URL exclusions."
            ).strip(),
        }
    testing_negative_keywords = merge_negative_keywords(
        scale_negative_plan["negative_keywords"],
        waste_negative_plan["active_negative_keywords"],
    )
    testing_page_exclusions = merge_page_exclusions(
        scale_page_exclusion_plan["page_exclusions"],
        waste_page_exclusion_plan["active_page_exclusions"],
    )
    core_scale_negative_keywords = merge_negative_keywords(waste_negative_plan["active_negative_keywords"])
    core_scale_page_exclusions = merge_page_exclusions(waste_page_exclusion_plan["active_page_exclusions"])
    landing_page_governance = landing_page_category_governance_plan(
        page_rows,
        website_url=website_url,
        scale_page_plan=scale_page_plan,
        scale_page_exclusion_plan=scale_page_exclusion_plan,
        waste_plan=waste_plan,
        all_page_rows=list(page_rows) + list(waste_page_rows),
        testing_limit=landing_page_limit,
    )
    draft_waste_plan = compact_waste_plan_for_draft(waste_plan)
    draft_landing_page_governance = compact_landing_page_governance_for_draft(landing_page_governance)
    governance_categories = {
        str(item.get("name") or ""): item
        for item in (landing_page_governance.get("categories") or [])
        if isinstance(item, dict)
    }
    core_scale_url_targets = _url_inclusion_targets_from_page_items(
        (governance_categories.get("Core / Scale") or {}).get("included_pages") or [],
        source="core_scale_governance",
        exclude_broad_pages=True,
    )
    testing_discovery_url_targets = _url_inclusion_targets_from_page_items(
        (governance_categories.get("Testing / Discovery") or {}).get("candidate_pages") or [],
        source="testing_discovery_governance",
    )
    core_scale_pmax_url_targets, core_scale_pmax_policy_filter = _pmax_safe_url_targets(
        core_scale_url_targets,
        pmax_policy_terms,
    )
    testing_pmax_url_targets, testing_pmax_policy_filter = _pmax_safe_url_targets(
        testing_discovery_url_targets,
        pmax_policy_terms,
    )
    core_scale_final_url = _primary_url_from_targets(core_scale_url_targets, website_url)
    testing_final_url = _primary_url_from_targets(testing_discovery_url_targets, website_url)
    core_scale_pmax_final_url = _primary_url_from_targets(core_scale_pmax_url_targets, website_url)
    testing_pmax_final_url = _primary_url_from_targets(testing_pmax_url_targets, website_url)
    audience_signal_plan = (
        build_audience_signal_plan(
            session,
            account,
            keyword_rows=keyword_rows,
            pmax_theme_plan=pmax_theme_plan,
            ga4_matrix=ga4_matrix,
            website_url=website_url,
            search_console_matrix=search_console_matrix,
            limit=50,
        )
        if preference.audience_signal_enabled
        else {
            "enabled": False,
            "scope": "pmax_asset_group_audience_signal_plan",
            "custom_segment_terms": [],
            "custom_segment_term_count": 0,
            "similar_website_urls": [],
            "similar_website_url_count": 0,
            "basis": "Audience signals are disabled for this account.",
        }
    )
    budget = testing_daily_budget_from_sales(session, preference)
    if budget.get("budget_blocked"):
        settings = get_sync_setting_map(session)
        if parse_bool(settings.get(FORCE_MINIMUM_BUDGET_SETTING, False)):
            minimum_budget = currency_minimum_daily_budget(account, preference.minimum_daily_budget_amount)
            budget = {
                **budget,
                "daily_budget": minimum_budget,
                "budget_blocked": False,
                "budget_forced_minimum": True,
                "budget_force_reason": budget.get("budget_block_reason"),
                "budget_block_reason": "",
                "used_minimum": True,
            }
        else:
            blocked_drafts = block_automation_campaign_drafts_for_budget(session, preference, budget)
            reason = str(budget.get("budget_block_reason") or "Odoo sales guard blocks automated campaign spend.")
            result = {
                "name": "testing_campaign_automation",
                "status": "deferred_budget_guard",
                "reason": reason,
                "decision": decision,
                "budget": budget,
                "blocked_drafts": blocked_drafts,
                "keyword_count": len(keyword_rows),
                "landing_page_count": len(page_rows),
                "audience_signal_plan": audience_signal_plan,
                "live_creation_enabled": False,
            }
            session.add(
                AutoPilotEvent(
                    account_id=account.id,
                    campaign_id=None,
                    campaign_name=None,
                    action_type="automation_campaign_plan",
                    status="blocked",
                    summary=reason,
                    evidence={
                        "decision": decision,
                        "budget": budget,
                        "blocked_drafts": blocked_drafts,
                        "source_job_id": source_job_id,
                    },
                )
            )
            session.commit()
            return result
    bidding = {
        "strategy": "maximize_conversion_value_target_roas",
        "strategy_label": "Maximize conversion value with Target ROAS",
        "currency_code": budget["currency_code"],
        "daily_budget": round(float(budget["daily_budget"] or 0), 2),
        "target_roas": TESTING_DISCOVERY_TARGET_ROAS,
        "target_roas_percent": round(TESTING_DISCOVERY_TARGET_ROAS * 100, 2),
        "max_cpc_bid_limit": _max_cpc_bid_limit_for_account(account),
        "sales_budget_ratio": budget["ratio"],
        "sales_budget_ratio_pct": budget["ratio_pct"],
    }
    drafts: list[dict[str, Any]] = []
    retired_max_clicks_drafts = retire_legacy_max_clicks_automation_drafts(session, preference)
    warnings = [
        "Automation draft only; live creation still requires monitor-only off, dry-run off, and mutation settings enabled.",
        "Testing Search campaigns use Target ROAS RSA/DSA lanes only. Legacy Max Clicks CPC-cap RSA lanes are retired and paused by campaign revisions.",
        "Campaign names include category and a stable AUTO code so reconnects can resume existing campaigns.",
    ]

    if decision["mode"] in {"testing_no_pmax", "normal_categories_no_pmax", "pmax_allowed"}:
        assets, validation, prompt = generate_ad_copy(
            session,
            ad_type="dsa",
            website_url=website_url,
            business_name=account.name,
            generic_page_feed_copy=True,
            keyword_terms=keywords,
        )
        automation = attach_campaign_identity(
            session,
            account,
            automation={
                "source_key": _automation_draft_source_key(account, "testing_all_pages_dsa"),
                "decision": decision,
                "page_targeting": {
                    "mode": "ai_max_dsa_url_inclusions",
                    "website_url": website_url,
                    "url_inclusions": testing_discovery_url_targets,
                    "url_exclusions": [item["url"] for item in testing_page_exclusions],
                    "best_landing_pages_are_evidence_only": True,
                    "best_landing_pages": _landing_page_payload(page_rows),
                    "best_landing_page_source": page_source,
                },
                "keyword_themes": _keyword_payload(_planning_items(keyword_rows, keyword_limit)),
                "keyword_source": keyword_source,
                "scale_negative_keyword_plan": scale_negative_plan,
                "scale_page_exclusion_plan": scale_page_exclusion_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "audience_signal_plan": audience_signal_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "search_console_ads_signal_matrix": search_console_matrix,
                "created_by": "automation_monitor",
                "source_job_id": source_job_id,
            },
            category="Testing / Discovery",
            campaign_intent="ai_max_dsa_testing_url_inclusions",
            channel_label="DSA AI Max Target ROAS Page Discovery",
            website_url=website_url,
        )
        campaign_identity = automation["campaign_identity"]
        assets = {
            **assets,
            "automation": automation,
            "campaign_identity": campaign_identity,
            "campaign_name": campaign_identity["campaign_name"],
            "campaign_code": campaign_identity["campaign_code"],
            "existing_google_campaign": automation["existing_google_campaign"],
            "planned_operation": automation["planned_operation"],
            "live_creation": automation_live_creation_control(decision, category="Testing / Discovery", ad_type="dsa"),
            "bidding": bidding,
            "final_url": website_url,
            "page_targeting": automation["page_targeting"],
            "page_feed_targets": testing_discovery_url_targets,
            "landing_page_candidates": _landing_page_payload(page_rows),
            "url_inclusion_targets": testing_discovery_url_targets,
            "landing_page_governance_plan": draft_landing_page_governance,
            "audience_signal_plan": audience_signal_plan,
            "scale_negative_keyword_plan": scale_negative_plan,
            "waste_management_plan": draft_waste_plan,
            "ga4_ads_signal_matrix": ga4_matrix,
            "search_console_ads_signal_matrix": search_console_matrix,
            "waste_negative_keyword_plan": waste_negative_plan,
            "negative_keywords": testing_negative_keywords,
            "scale_page_exclusion_plan": scale_page_exclusion_plan,
            "waste_page_exclusion_plan": waste_page_exclusion_plan,
            "negative_page_targets": [item["url"] for item in testing_page_exclusions],
            "source_terms": keywords[:30],
            "copy_strategy": {
                "mode": "testing_ai_max_expanded_dsa_closed_loop",
                "reason": (
                    "Testing page discovery uses an automation-owned Search campaign with AI Max enabled, "
                    "an automation-owned SEARCH_DYNAMIC_ADS ad group, Expanded Dynamic Search Ad creative, "
                    "URL inclusions from eligible landing pages, and URL exclusions from Scale/Waste governance."
                ),
            },
        }
        drafts.append(
            upsert_automation_ad_draft(
                session,
                preference=preference,
                ad_type="dsa",
                source_key=automation["source_key"],
                website_url=website_url,
                final_url=website_url,
                business_name=account.name,
                generated_assets=assets,
                validation_json=_automation_validation(validation, warnings),
                prompt=prompt,
            )
        )

    if decision["mode"] in {"testing_no_pmax", "normal_categories_no_pmax", "pmax_allowed"}:
        if testing_rsa_terms:
            testing_keyword_rows_for_payload = testing_rows or available_keyword_rows_for_terms(keyword_rows, set())
            assets, validation, prompt = generate_ad_copy(
                session,
                ad_type="rsa",
                website_url=website_url,
                business_name=account.name,
                generic_page_feed_copy=False,
                keyword_terms=testing_rsa_terms,
            )
            automation = attach_campaign_identity(
                session,
                account,
                automation={
                    "source_key": _automation_draft_source_key(account, "testing_keyword_rsa"),
                    "decision": decision,
                    "keyword_source": keyword_source,
                    "keyword_themes": _keyword_payload(_planning_items(testing_keyword_rows_for_payload, keyword_limit)),
                    "scale_negative_keyword_plan": scale_negative_plan,
                    "scale_page_exclusion_plan": scale_page_exclusion_plan,
                    "landing_page_governance_plan": draft_landing_page_governance,
                    "audience_signal_plan": audience_signal_plan,
                    "waste_management_plan": draft_waste_plan,
                    "ga4_ads_signal_matrix": ga4_matrix,
                    "search_console_ads_signal_matrix": search_console_matrix,
                    "page_targeting": {
                        "mode": "testing_discovery_final_url",
                        "website_url": website_url,
                        "final_url": testing_final_url,
                        "url_inclusions": testing_discovery_url_targets,
                        "note": "RSA uses keyword targeting, but its ad final URL is selected from Testing-only discovery URLs when available.",
                    },
                    "created_by": "automation_monitor",
                    "source_job_id": source_job_id,
                },
                category="Testing / Discovery",
                campaign_intent="maximize_clicks_rsa_best_keywords",
                channel_label="RSA Target ROAS Keywords",
                website_url=website_url,
            )
            campaign_identity = automation["campaign_identity"]
            assets = {
                **assets,
                "automation": automation,
                "campaign_identity": campaign_identity,
                "campaign_name": campaign_identity["campaign_name"],
                "campaign_code": campaign_identity["campaign_code"],
                "existing_google_campaign": automation["existing_google_campaign"],
                "planned_operation": automation["planned_operation"],
                "live_creation": automation_live_creation_control(decision, category="Testing / Discovery", ad_type="rsa"),
                "bidding": bidding,
                "final_url": testing_final_url,
                "page_targeting": automation["page_targeting"],
                "scale_negative_keyword_plan": scale_negative_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "search_console_ads_signal_matrix": search_console_matrix,
                "audience_signal_plan": audience_signal_plan,
                "waste_negative_keyword_plan": waste_negative_plan,
                "negative_keywords": testing_negative_keywords,
                "scale_page_exclusion_plan": scale_page_exclusion_plan,
                "waste_page_exclusion_plan": waste_page_exclusion_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "url_inclusion_targets": testing_discovery_url_targets,
                "blocked_final_urls": [item["url"] for item in testing_page_exclusions],
                "source_terms": _planning_items(testing_rsa_terms, keyword_limit),
                "search_console_source_terms": _planning_items(search_console_terms, keyword_limit),
                "google_keyword_plan": {
                    "source": keyword_source,
                    "terms": _planning_items(testing_rsa_terms, keyword_limit),
                    "candidate_count": len(keyword_rows),
                },
                "keyword_clusters": [
                    {
                        "ad_group_name": f"AUTO | Testing / Discovery | RSA Keywords | {campaign_identity['campaign_code']}",
                        "source": keyword_source,
                        "match_type": "exact",
                        "exact_terms": _planning_items(testing_rsa_terms, keyword_limit),
                    }
                ],
                "copy_strategy": {
                    "mode": "automation_keyword_bank_rsa",
                    "reason": "No recent account delivery, so RSA starts from best saved keyword-bank terms.",
                },
            }
            drafts.append(
                upsert_automation_ad_draft(
                    session,
                    preference=preference,
                    ad_type="rsa",
                    source_key=automation["source_key"],
                    website_url=website_url,
                    final_url=testing_final_url,
                    business_name=account.name,
                    generated_assets=assets,
                    validation_json=_automation_validation(validation, warnings),
                    prompt=prompt,
                )
            )

    if fix_watch_terms:
        fix_watch_bidding = {
            **bidding,
            "target_roas": FIX_WATCH_TARGET_ROAS,
            "target_roas_percent": round(FIX_WATCH_TARGET_ROAS * 100, 2),
            "category_strategy": "fix_watch_rescue",
        }
        assets, validation, prompt = generate_ad_copy(
            session,
            ad_type="rsa",
            website_url=website_url,
            business_name=account.name,
            generic_page_feed_copy=False,
            keyword_terms=fix_watch_terms,
        )
        automation = attach_campaign_identity(
            session,
            account,
            automation={
                "source_key": _automation_draft_source_key(account, "fix_watch_keyword_rsa"),
                "decision": decision,
                "keyword_source": keyword_source,
                "keyword_themes": _keyword_payload(_planning_items(fix_watch_rows, keyword_limit)),
                "scale_negative_keyword_plan": scale_negative_plan,
                "scale_page_exclusion_plan": scale_page_exclusion_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "audience_signal_plan": audience_signal_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "page_targeting": {
                    "mode": "website_root_repair_context",
                    "website_url": website_url,
                    "note": "Fix / Watch RSA uses unique low-risk terms not already assigned to Core / Scale PMax or other RSA lanes.",
                },
                "created_by": "automation_monitor",
                "source_job_id": source_job_id,
            },
            category="Fix / Watch",
            campaign_intent="maximize_clicks_rsa_repair_keywords",
            channel_label="RSA Target ROAS Repair Keywords",
            website_url=website_url,
        )
        campaign_identity = automation["campaign_identity"]
        assets = {
            **assets,
            "automation": automation,
            "campaign_identity": campaign_identity,
            "campaign_name": campaign_identity["campaign_name"],
            "campaign_code": campaign_identity["campaign_code"],
            "existing_google_campaign": automation["existing_google_campaign"],
            "planned_operation": automation["planned_operation"],
            "live_creation": automation_live_creation_control(decision, category="Fix / Watch", ad_type="rsa"),
            "bidding": fix_watch_bidding,
            "final_url": website_url,
            "page_targeting": automation["page_targeting"],
            "scale_negative_keyword_plan": scale_negative_plan,
            "waste_management_plan": draft_waste_plan,
            "ga4_ads_signal_matrix": ga4_matrix,
            "audience_signal_plan": audience_signal_plan,
            "waste_negative_keyword_plan": waste_negative_plan,
            "negative_keywords": testing_negative_keywords,
            "scale_page_exclusion_plan": scale_page_exclusion_plan,
            "waste_page_exclusion_plan": waste_page_exclusion_plan,
            "landing_page_governance_plan": draft_landing_page_governance,
            "url_inclusion_targets": _url_inclusion_targets_from_rows(page_rows),
            "blocked_final_urls": [item["url"] for item in testing_page_exclusions],
            "source_terms": _planning_items(fix_watch_terms, keyword_limit),
            "google_keyword_plan": {
                "source": keyword_source,
                "terms": _planning_items(fix_watch_terms, keyword_limit),
                "candidate_count": len(keyword_rows),
            },
            "keyword_clusters": [
                {
                    "ad_group_name": f"AUTO | Fix / Watch | RSA Keywords | {campaign_identity['campaign_code']}",
                    "source": keyword_source,
                    "match_type": "exact",
                    "exact_terms": _planning_items(fix_watch_terms, keyword_limit),
                }
            ],
            "copy_strategy": {
                "mode": "fix_watch_unique_keyword_rsa",
                "reason": (
                    "Fix / Watch gets its own non-duplicate keyword pool for guarded delivery repair. "
                    "Terms already assigned to Core / Scale PMax, Core RSA, or Testing RSA are skipped."
                ),
            },
        }
        drafts.append(
            upsert_automation_ad_draft(
                session,
                preference=preference,
                ad_type="rsa",
                source_key=automation["source_key"],
                website_url=website_url,
                final_url=website_url,
                business_name=account.name,
                generated_assets=assets,
                validation_json=_automation_validation(validation, warnings),
                prompt=prompt,
            )
        )

    if True:  # Always keep the automation-owned Waste / Recovery lane visible.
        waste_bidding = {
            **bidding,
            "daily_budget": waste_budget_amount,
            "fixed_daily_budget": waste_budget_amount,
            "target_roas": WASTE_RECOVERY_TARGET_ROAS,
            "target_roas_percent": round(WASTE_RECOVERY_TARGET_ROAS * 100, 2),
            "category_strategy": "waste_recovery_fixed_budget",
            "budget_policy": "fixed_no_peak_boost_no_sales_guard_reduction",
        }
        assets, validation, prompt = generate_ad_copy(
            session,
            ad_type="rsa",
            website_url=website_url,
            business_name=account.name,
            generic_page_feed_copy=True,
            keyword_terms=waste_recovery_terms or ["supplements"],
        )
        automation = attach_campaign_identity(
            session,
            account,
            automation={
                "source_key": _automation_draft_source_key(account, "waste_recovery_keyword_rsa"),
                "decision": decision,
                "keyword_source": "waste_recovery_state",
                "keyword_themes": [
                    {
                        "keyword": term,
                        "normalized_keyword": _term_key_from_text(term),
                        "source": "waste_recovery",
                    }
                    for term in waste_recovery_terms
                ],
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "page_targeting": {
                    "mode": "website_root_recovery_context",
                    "website_url": website_url,
                    "note": "Waste / Recovery RSA uses only recovery or pending-recovery terms, never active high-confidence waste terms.",
                },
                "created_by": "automation_monitor",
                "source_job_id": source_job_id,
            },
            category="Waste / Recovery",
            campaign_intent="maximize_clicks_rsa_recovery_keywords",
            channel_label="RSA Target ROAS Recovery Keywords",
            website_url=website_url,
        )
        campaign_identity = automation["campaign_identity"]
        assets = {
            **assets,
            "automation": automation,
            "campaign_identity": campaign_identity,
            "campaign_name": campaign_identity["campaign_name"],
            "campaign_code": campaign_identity["campaign_code"],
            "existing_google_campaign": automation["existing_google_campaign"],
            "planned_operation": automation["planned_operation"],
            "live_creation": automation_live_creation_control(decision, category="Waste / Recovery", ad_type="rsa"),
            "bidding": waste_bidding,
            "final_url": website_url,
            "page_targeting": automation["page_targeting"],
            "waste_management_plan": draft_waste_plan,
            "ga4_ads_signal_matrix": ga4_matrix,
            "url_inclusion_targets": _url_inclusion_targets_from_rows(page_rows),
            "negative_keywords": [],
            "source_terms": _planning_items(waste_recovery_terms, keyword_limit),
            "google_keyword_plan": {
                "source": "waste_recovery_state",
                "terms": _planning_items(waste_recovery_terms, keyword_limit),
                "candidate_count": len(waste_recovery_keyword_items),
                "pending_recovery_keyword_count": int(waste_negative_plan.get("pending_recovery_keyword_count") or 0),
                "scale_recovery_keyword_count": int(waste_negative_plan.get("scale_recovery_keyword_count") or 0),
                "testing_recovery_keyword_count": int(waste_negative_plan.get("testing_recovery_keyword_count") or 0),
            },
            "keyword_clusters": [
                {
                    "ad_group_name": f"AUTO | Waste / Recovery | RSA Keywords | {campaign_identity['campaign_code']}",
                    "source": "waste_recovery_state",
                    "match_type": "exact",
                    "exact_terms": _planning_items(waste_recovery_terms, keyword_limit),
                }
            ],
            "copy_strategy": {
                "mode": "waste_recovery_keyword_rsa",
                "reason": (
                    "Waste / Recovery is visible as a fixed-budget recovery lane. It only creates exact keywords when "
                    "recovery evidence or pending recovery state exists, so an empty recovery pool cannot spend on "
                    "invented terms. Active high-confidence waste remains excluded."
                ),
            },
        }
        drafts.append(
            upsert_automation_ad_draft(
                session,
                preference=preference,
                ad_type="rsa",
                source_key=automation["source_key"],
                website_url=website_url,
                final_url=website_url,
                business_name=account.name,
                generated_assets=assets,
                validation_json=_automation_validation(validation, warnings),
                prompt=prompt,
            )
        )

    if decision["mode"] in {"testing_no_pmax", "normal_categories_no_pmax", "pmax_allowed"}:
        pmax_roas = max(CORE_SCALE_TARGET_ROAS, 1 / max(float(preference.odoo_sales_max_spend_ratio or 0.15), 0.01))
        assets, validation, prompt = generate_ad_copy(
            session,
            ad_type="dsa",
            website_url=website_url,
            business_name=account.name,
            generic_page_feed_copy=False,
            keyword_terms=pmax_search_themes or core_rsa_terms,
        )
        automation = attach_campaign_identity(
            session,
            account,
            automation={
                "source_key": _automation_draft_source_key(account, "core_scale_ai_max_dsa"),
                "decision": decision,
                "keyword_source": keyword_source,
                "keyword_themes": _keyword_payload(_planning_items(pmax_source_rows, keyword_limit)),
                "pmax_search_theme_plan": pmax_theme_plan,
                "scale_landing_page_plan": scale_page_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "audience_signal_plan": audience_signal_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "search_console_ads_signal_matrix": search_console_matrix,
                "page_targeting": {
                    "mode": "ai_max_dsa_url_inclusions",
                    "website_url": website_url,
                    "url_inclusions": core_scale_url_targets,
                    "url_exclusions": [item["url"] for item in core_scale_page_exclusions],
                    "requires_url_inclusions": True,
                    "best_landing_pages_are_evidence_only": False,
                    "best_landing_page_source": page_source,
                },
                "created_by": "automation_monitor",
                "source_job_id": source_job_id,
            },
            category="Core / Scale",
            campaign_intent="ai_max_dsa_scale_url_inclusions",
            channel_label="DSA AI Max Target ROAS Scale Pages",
            website_url=website_url,
        )
        campaign_identity = automation["campaign_identity"]
        assets = {
            **assets,
            "automation": automation,
            "campaign_identity": campaign_identity,
            "campaign_name": campaign_identity["campaign_name"],
            "campaign_code": campaign_identity["campaign_code"],
            "existing_google_campaign": automation["existing_google_campaign"],
            "planned_operation": automation["planned_operation"],
            "live_creation": automation_live_creation_control(decision, category="Core / Scale", ad_type="dsa"),
            "bidding": {
                **bidding,
                "target_roas": pmax_roas,
                "target_roas_percent": round(pmax_roas * 100, 2),
            },
            "final_url": core_scale_final_url,
            "page_targeting": automation["page_targeting"],
            "page_feed_targets": core_scale_url_targets,
            "landing_page_candidates": _landing_page_payload(page_rows),
            "url_inclusion_targets": core_scale_url_targets,
            "scale_landing_page_plan": scale_page_plan,
            "landing_page_governance_plan": draft_landing_page_governance,
            "audience_signal_plan": audience_signal_plan,
            "waste_management_plan": draft_waste_plan,
            "ga4_ads_signal_matrix": ga4_matrix,
            "search_console_ads_signal_matrix": search_console_matrix,
            "waste_negative_keyword_plan": waste_negative_plan,
            "negative_keywords": core_scale_negative_keywords,
            "waste_page_exclusion_plan": waste_page_exclusion_plan,
            "negative_page_targets": [item["url"] for item in core_scale_page_exclusions],
            "source_terms": (pmax_search_themes or core_rsa_terms)[:30],
            "copy_strategy": {
                "mode": "core_scale_ai_max_expanded_dsa_closed_loop",
                "reason": (
                    "Core / Scale dynamic ads use an automation-owned Search campaign with AI Max enabled, "
                    "an automation-owned SEARCH_DYNAMIC_ADS ad group, Expanded Dynamic Search Ad creative, "
                    "URL inclusions from proven landing pages, and URL exclusions from Waste governance."
                ),
            },
        }
        drafts.append(
            upsert_automation_ad_draft(
                session,
                preference=preference,
                ad_type="dsa",
                source_key=automation["source_key"],
                website_url=website_url,
                final_url=core_scale_final_url,
                business_name=account.name,
                generated_assets=assets,
                validation_json=_automation_validation(validation, warnings),
                prompt=prompt,
            )
        )

        scale_rsa_terms = core_rsa_terms
        if scale_rsa_terms:
            assets, validation, prompt = generate_ad_copy(
                session,
                ad_type="rsa",
                website_url=website_url,
                business_name=account.name,
                generic_page_feed_copy=False,
                keyword_terms=scale_rsa_terms,
            )
            automation = attach_campaign_identity(
                session,
                account,
                automation={
                    "source_key": _automation_draft_source_key(account, "core_scale_keyword_rsa"),
                    "decision": decision,
                    "keyword_source": keyword_source,
                    "keyword_themes": _keyword_payload(_planning_items(core_rsa_rows, keyword_limit)),
                    "pmax_search_theme_plan": pmax_theme_plan,
                    "scale_landing_page_plan": scale_page_plan,
                    "landing_page_governance_plan": draft_landing_page_governance,
                    "audience_signal_plan": audience_signal_plan,
                    "waste_management_plan": draft_waste_plan,
                    "ga4_ads_signal_matrix": ga4_matrix,
                    "page_targeting": {
                        "mode": "core_scale_proven_final_url",
                        "website_url": website_url,
                        "final_url": core_scale_final_url,
                        "url_inclusions": core_scale_url_targets,
                        "note": "Core / Scale RSA uses proven exact keyword targets and a proven Core URL as the ad final URL.",
                    },
                    "created_by": "automation_monitor",
                    "source_job_id": source_job_id,
                },
                category="Core / Scale",
                campaign_intent="maximize_clicks_rsa_scale_keywords",
                channel_label="RSA Target ROAS Scale Keywords",
                website_url=website_url,
            )
            campaign_identity = automation["campaign_identity"]
            assets = {
                **assets,
                "automation": automation,
                "campaign_identity": campaign_identity,
                "campaign_name": campaign_identity["campaign_name"],
                "campaign_code": campaign_identity["campaign_code"],
                "existing_google_campaign": automation["existing_google_campaign"],
                "planned_operation": automation["planned_operation"],
                "live_creation": automation_live_creation_control(decision, category="Core / Scale", ad_type="rsa"),
                "bidding": {
                    **bidding,
                    "target_roas": pmax_roas,
                    "target_roas_percent": round(pmax_roas * 100, 2),
                },
                "final_url": core_scale_final_url,
                "page_targeting": automation["page_targeting"],
                "scale_landing_page_plan": scale_page_plan,
                "scale_negative_keyword_plan": scale_negative_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "audience_signal_plan": audience_signal_plan,
                "waste_negative_keyword_plan": waste_negative_plan,
                "negative_keywords": core_scale_negative_keywords,
                "waste_page_exclusion_plan": waste_page_exclusion_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "url_inclusion_targets": core_scale_url_targets,
                "blocked_final_urls": [item["url"] for item in core_scale_page_exclusions],
                "source_terms": _planning_items(scale_rsa_terms, keyword_limit),
                "google_keyword_plan": {
                    "source": keyword_source,
                    "terms": _planning_items(scale_rsa_terms, keyword_limit),
                    "candidate_count": int(pmax_theme_plan.get("candidate_count") or len(keyword_rows)),
                },
                "keyword_clusters": [
                    {
                        "ad_group_name": f"AUTO | Core / Scale | RSA Keywords | {campaign_identity['campaign_code']}",
                        "source": keyword_source,
                        "match_type": "exact",
                        "exact_terms": _planning_items(scale_rsa_terms, keyword_limit),
                    }
                ],
                "copy_strategy": {
                    "mode": "core_scale_keyword_bank_rsa",
                    "reason": "Core / Scale RSA keeps proven exact terms active alongside PMax search-theme scaling.",
                },
            }
            drafts.append(
                upsert_automation_ad_draft(
                    session,
                    preference=preference,
                    ad_type="rsa",
                    source_key=automation["source_key"],
                    website_url=website_url,
                    final_url=core_scale_final_url,
                    business_name=account.name,
                    generated_assets=assets,
                    validation_json=_automation_validation(validation, warnings),
                    prompt=prompt,
                )
            )

        campaign_plans = [item for item in (pmax_theme_plan.get("campaign_plans") or []) if isinstance(item, dict)]
        if not campaign_plans:
            campaign_plans = [
                {
                    "campaign_index": 1,
                    "campaign_code_suffix": "S001",
                    "asset_group_count": 1,
                    "search_theme_count": 0,
                    "asset_groups": [],
                    "requires_new_campaign": False,
                }
            ]
        for campaign_plan in campaign_plans:
            campaign_index = int(campaign_plan.get("campaign_index") or 1)
            campaign_terms = [
                str(term).strip()
                for asset_group in (campaign_plan.get("asset_groups") or [])
                if isinstance(asset_group, dict)
                for term in (asset_group.get("search_themes") or [])
                if str(term).strip()
            ]
            copy_terms = campaign_terms[:PMAX_SEARCH_THEME_LIMIT] or pmax_search_themes
            campaign_asset_groups = [
                {
                    **asset_group,
                    "audience_signal_plan": audience_signal_plan,
                }
                for asset_group in (campaign_plan.get("asset_groups") or [])
                if isinstance(asset_group, dict)
            ]
            assets, validation, prompt = generate_ad_copy(
                session,
                ad_type="pmax",
                website_url=website_url,
                business_name=account.name,
                generic_page_feed_copy=False,
                keyword_terms=copy_terms,
            )
            campaign_suffix = str(campaign_plan.get("campaign_code_suffix") or f"S{campaign_index:03d}")
            automation = attach_campaign_identity(
                session,
                account,
                automation={
                    "source_key": _automation_draft_source_key(account, f"pmax_scale_after_7d_conversions_{campaign_suffix.lower()}"),
                    "decision": decision,
                    "keyword_source": keyword_source,
                    "keyword_themes": _keyword_payload(_planning_items(keyword_rows, keyword_limit)),
                    "pmax_search_theme_plan": pmax_theme_plan,
                    "pmax_campaign_plan": campaign_plan,
                    "scale_landing_page_plan": scale_page_plan,
                    "landing_page_governance_plan": draft_landing_page_governance,
                    "audience_signal_plan": audience_signal_plan,
                    "waste_management_plan": draft_waste_plan,
                    "ga4_ads_signal_matrix": ga4_matrix,
                    "page_targets": core_scale_pmax_url_targets,
                    "pmax_policy_filter": {
                        "search_themes": pmax_theme_plan.get("pmax_policy_filter") or {},
                        "url_targets": core_scale_pmax_policy_filter,
                    },
                    "page_targeting": {
                        "mode": "core_scale_url_inclusions",
                        "website_url": website_url,
                        "final_url": core_scale_pmax_final_url,
                        "final_url_expansion": "restricted_to_core_scale_urls",
                        "url_expansion_opt_out": True,
                        "url_inclusions": core_scale_pmax_url_targets,
                        "url_exclusions": [item["url"] for item in core_scale_page_exclusions],
                        "best_landing_pages_are_evidence_only": False,
                        "best_landing_page_source": page_source,
                        "best_landing_pages": core_scale_pmax_url_targets,
                        "all_governance_url_inclusions": core_scale_url_targets,
                    },
                    "created_by": "automation_monitor",
                    "source_job_id": source_job_id,
                },
                category="Core / Scale",
                campaign_intent=f"pmax_after_conversion_threshold_{campaign_suffix.lower()}",
                channel_label=f"PMax Target ROAS Scale {campaign_suffix}",
                website_url=website_url,
            )
            campaign_identity = automation["campaign_identity"]
            assets = {
                **assets,
                "automation": automation,
                "campaign_identity": campaign_identity,
                "campaign_name": campaign_identity["campaign_name"],
                "campaign_code": campaign_identity["campaign_code"],
                "existing_google_campaign": automation["existing_google_campaign"],
                "planned_operation": automation["planned_operation"],
                "live_creation": automation_live_creation_control(decision, category="Core / Scale", ad_type="pmax"),
                "bidding": {
                    "strategy": "maximize_conversion_value_target_roas",
                    "strategy_label": "Maximize conversion value with Target ROAS",
                    "currency_code": budget["currency_code"],
                    "daily_budget": round(float(budget["daily_budget"] or 0), 2),
                    "target_roas": pmax_roas,
                    "target_roas_percent": round(pmax_roas * 100, 2),
                },
                "final_url": core_scale_pmax_final_url,
                "page_targeting": automation["page_targeting"],
                "page_feed_targets": core_scale_pmax_url_targets,
                "landing_page_candidates": _landing_page_payload(page_rows),
                "url_inclusion_targets": core_scale_pmax_url_targets,
                "all_governance_url_inclusions": core_scale_url_targets,
                "scale_landing_page_plan": scale_page_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "audience_signal_plan": audience_signal_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "waste_negative_keyword_plan": waste_negative_plan,
                "negative_keywords": core_scale_negative_keywords,
                "waste_page_exclusion_plan": waste_page_exclusion_plan,
                "negative_page_targets": [item["url"] for item in core_scale_page_exclusions],
                "pmax_search_theme_plan": pmax_theme_plan,
                "pmax_campaign_plan": campaign_plan,
                "pmax_asset_groups": campaign_asset_groups,
                "pmax_search_themes": copy_terms,
                "pmax_policy_filter": automation["pmax_policy_filter"],
                "source_terms": copy_terms,
                "copy_strategy": {
                    "mode": "pmax_scale_openai_copy_core_scale_url_inclusions_search_themes",
                    "reason": (
                        "Scale PMax uses proven Core / Scale URL inclusions from saved Google Ads, GA4, Search Console, "
                        "and Odoo-linked signals, OpenAI-generated text assets when configured, 50 search themes per "
                        "asset group, 100 asset groups per PMax campaign, and new Core / Scale PMax campaign shards after that."
                    ),
                },
            }
            drafts.append(
                upsert_automation_ad_draft(
                    session,
                    preference=preference,
                    ad_type="pmax",
                    source_key=automation["source_key"],
                    website_url=website_url,
                    final_url=core_scale_pmax_final_url,
                    business_name=account.name,
                    generated_assets=assets,
                    validation_json=_automation_validation(validation, warnings),
                    prompt=prompt,
                )
            )

        testing_pmax_campaign_plans = [
            item for item in (testing_pmax_theme_plan.get("campaign_plans") or []) if isinstance(item, dict)
        ]
        if not testing_pmax_campaign_plans:
            testing_pmax_campaign_plans = [
                {
                    "campaign_index": 1,
                    "campaign_code_suffix": "S001",
                    "asset_group_count": 1,
                    "search_theme_count": 0,
                    "asset_groups": [],
                    "requires_new_campaign": False,
                }
            ]
        for campaign_plan in testing_pmax_campaign_plans:
            campaign_index = int(campaign_plan.get("campaign_index") or 1)
            campaign_terms = [
                str(term).strip()
                for asset_group in (campaign_plan.get("asset_groups") or [])
                if isinstance(asset_group, dict)
                for term in (asset_group.get("search_themes") or [])
                if str(term).strip()
            ]
            copy_terms = campaign_terms[:PMAX_SEARCH_THEME_LIMIT]
            campaign_asset_groups = [
                {
                    **asset_group,
                    "audience_signal_plan": audience_signal_plan,
                }
                for asset_group in (campaign_plan.get("asset_groups") or [])
                if isinstance(asset_group, dict)
            ]
            assets, validation, prompt = generate_ad_copy(
                session,
                ad_type="pmax",
                website_url=website_url,
                business_name=account.name,
                generic_page_feed_copy=True,
                keyword_terms=copy_terms,
            )
            campaign_suffix = str(campaign_plan.get("campaign_code_suffix") or f"S{campaign_index:03d}")
            automation = attach_campaign_identity(
                session,
                account,
                automation={
                    "source_key": _automation_draft_source_key(account, f"testing_pmax_discovery_{campaign_suffix.lower()}"),
                    "decision": decision,
                    "keyword_source": keyword_source,
                    "keyword_themes": _keyword_payload(_planning_items(testing_pmax_source_rows, keyword_limit)),
                    "pmax_search_theme_plan": testing_pmax_theme_plan,
                    "pmax_campaign_plan": campaign_plan,
                    "scale_negative_keyword_plan": scale_negative_plan,
                    "scale_page_exclusion_plan": scale_page_exclusion_plan,
                    "landing_page_governance_plan": draft_landing_page_governance,
                    "audience_signal_plan": audience_signal_plan,
                    "waste_management_plan": draft_waste_plan,
                    "ga4_ads_signal_matrix": ga4_matrix,
                    "page_targets": testing_pmax_url_targets,
                    "pmax_policy_filter": {
                        "search_themes": testing_pmax_theme_plan.get("pmax_policy_filter") or {},
                        "url_targets": testing_pmax_policy_filter,
                    },
                    "page_targeting": {
                        "mode": "testing_discovery_url_inclusions",
                        "website_url": website_url,
                        "final_url": testing_pmax_final_url,
                        "final_url_expansion": "enabled",
                        "url_expansion_opt_out": False,
                        "url_inclusions": testing_pmax_url_targets,
                        "url_exclusions": [item["url"] for item in testing_page_exclusions],
                        "best_landing_pages_are_evidence_only": True,
                        "best_landing_page_source": page_source,
                        "best_landing_pages": testing_pmax_url_targets,
                        "all_governance_url_inclusions": testing_discovery_url_targets,
                    },
                    "created_by": "automation_monitor",
                    "source_job_id": source_job_id,
                },
                category="Testing / Discovery",
                campaign_intent=f"pmax_discovery_non_duplicate_{campaign_suffix.lower()}",
                channel_label=f"PMax Target ROAS Discovery {campaign_suffix}",
                website_url=website_url,
            )
            campaign_identity = automation["campaign_identity"]
            assets = {
                **assets,
                "automation": automation,
                "campaign_identity": campaign_identity,
                "campaign_name": campaign_identity["campaign_name"],
                "campaign_code": campaign_identity["campaign_code"],
                "existing_google_campaign": automation["existing_google_campaign"],
                "planned_operation": automation["planned_operation"],
                "live_creation": automation_live_creation_control(decision, category="Testing / Discovery", ad_type="pmax"),
                "bidding": {
                    "strategy": "maximize_conversion_value_target_roas",
                    "strategy_label": "Maximize conversion value with Testing target ROAS",
                    "currency_code": budget["currency_code"],
                    "daily_budget": round(float(budget["daily_budget"] or 0), 2),
                    "target_roas": TESTING_DISCOVERY_TARGET_ROAS,
                    "target_roas_percent": round(TESTING_DISCOVERY_TARGET_ROAS * 100, 2),
                },
                "final_url": testing_pmax_final_url,
                "page_targeting": automation["page_targeting"],
                "page_feed_targets": testing_pmax_url_targets,
                "landing_page_candidates": _landing_page_payload(page_rows),
                "url_inclusion_targets": testing_pmax_url_targets,
                "all_governance_url_inclusions": testing_discovery_url_targets,
                "scale_negative_keyword_plan": scale_negative_plan,
                "scale_page_exclusion_plan": scale_page_exclusion_plan,
                "landing_page_governance_plan": draft_landing_page_governance,
                "audience_signal_plan": audience_signal_plan,
                "waste_management_plan": draft_waste_plan,
                "ga4_ads_signal_matrix": ga4_matrix,
                "waste_negative_keyword_plan": waste_negative_plan,
                "negative_keywords": testing_negative_keywords,
                "waste_page_exclusion_plan": waste_page_exclusion_plan,
                "negative_page_targets": [item["url"] for item in testing_page_exclusions],
                "pmax_search_theme_plan": testing_pmax_theme_plan,
                "pmax_campaign_plan": campaign_plan,
                "pmax_asset_groups": campaign_asset_groups,
                "pmax_search_themes": copy_terms,
                "pmax_policy_filter": automation["pmax_policy_filter"],
                "source_terms": copy_terms,
                "copy_strategy": {
                    "mode": "testing_pmax_discovery_non_duplicate_search_themes",
                    "reason": (
                        "Testing / Discovery PMax uses a separate non-duplicate search-theme pool after Core / Scale "
                        "PMax, Core RSA, Testing RSA, and Fix / Watch RSA have claimed their terms."
                    ),
                },
            }
            drafts.append(
                upsert_automation_ad_draft(
                    session,
                    preference=preference,
                    ad_type="pmax",
                    source_key=automation["source_key"],
                    website_url=website_url,
                    final_url=testing_pmax_final_url,
                    business_name=account.name,
                    generated_assets=assets,
                    validation_json=_automation_validation(validation, warnings),
                    prompt=prompt,
                )
            )

    status = "planned" if drafts else "skipped"
    reason = "Campaign drafts were created or refreshed." if drafts else "No keyword or campaign draft action was eligible."
    result = {
        "name": "testing_campaign_automation",
        "status": status,
        "reason": reason,
        "decision": decision,
        "drafts": drafts,
        "keyword_count": len(keyword_rows),
        "landing_page_count": len(page_rows),
        "keyword_source": keyword_source,
        "landing_page_source": page_source,
        "term_allocation": {
            "policy": "unique_keyword_or_search_theme_per_automation_lane",
            "pmax_source_row_count": len(pmax_source_rows),
            "pmax_term_count": int(pmax_theme_plan.get("candidate_count") or 0),
            "testing_pmax_source_row_count": len(testing_pmax_source_rows),
            "testing_pmax_term_count": int(testing_pmax_theme_plan.get("candidate_count") or 0),
            "core_rsa_term_count": len(core_rsa_terms),
            "core_max_clicks_rsa_term_count": 0,
            "testing_rsa_term_count": len(testing_terms),
            "testing_max_clicks_rsa_term_count": 0,
            "fix_watch_rsa_term_count": len(fix_watch_terms),
            "waste_recovery_rsa_term_count": len(waste_recovery_terms),
            "used_term_key_count": len(term_ledger_keys),
            "retired_legacy_max_clicks_draft_count": retired_max_clicks_drafts,
        },
        "scale_landing_page_plan": scale_page_plan,
        "landing_page_governance_plan": draft_landing_page_governance,
        "audience_signal_plan": audience_signal_plan,
        "pmax_search_theme_plan": pmax_theme_plan,
        "testing_pmax_search_theme_plan": testing_pmax_theme_plan,
        "ga4_ads_signal_matrix": ga4_matrix,
        "search_console_ads_signal_matrix": search_console_matrix,
        "search_console_source_term_count": len(search_console_terms),
        "search_console_source_terms": search_console_terms,
        "scale_migration_state": {
            "hold_days": scale_migration_state.get("hold_days"),
            "active_term_count": scale_migration_state.get("active_term_count"),
            "updated_at_utc": scale_migration_state.get("updated_at_utc"),
        },
        "scale_page_migration_state": {
            "hold_days": scale_page_migration_state.get("hold_days"),
            "active_page_count": scale_page_migration_state.get("active_page_count"),
            "updated_at_utc": scale_page_migration_state.get("updated_at_utc"),
        },
        "scale_negative_keyword_plan": scale_negative_plan,
        "scale_page_exclusion_plan": scale_page_exclusion_plan,
        "waste_management_plan": {
            **draft_waste_plan,
            "state": {
                "hold_days": waste_plan["state"].get("hold_days"),
                "updated_at_utc": waste_plan["state"].get("updated_at_utc"),
                "active_negative_keyword_count": waste_plan["state"].get("active_negative_keyword_count"),
                "active_page_exclusion_count": waste_plan["state"].get("active_page_exclusion_count"),
                "pending_keyword_recovery_count": waste_plan["state"].get("pending_keyword_recovery_count"),
                "pending_page_recovery_count": waste_plan["state"].get("pending_page_recovery_count"),
                "scale_keyword_recovery_count": waste_plan["state"].get("scale_keyword_recovery_count"),
                "scale_page_recovery_count": waste_plan["state"].get("scale_page_recovery_count"),
            },
        },
        "budget": budget,
        "live_creation_enabled": bool(preference.auto_create_campaigns_enabled and not preference.monitor_only),
    }
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=None,
            campaign_name=None,
            action_type="automation_campaign_plan",
            status=status,
            summary=reason,
            evidence={
                "decision": decision,
                "keyword_count": len(keyword_rows),
                "landing_page_count": len(page_rows),
                "pmax_search_theme_count": int(pmax_theme_plan.get("candidate_count") or 0),
                "pmax_search_theme_overflow_count": int(pmax_theme_plan.get("overflow_count") or 0),
                "pmax_asset_group_count": int(pmax_theme_plan.get("asset_group_count") or 0),
                "pmax_campaign_count": int(pmax_theme_plan.get("campaign_count") or 0),
                "testing_pmax_search_theme_count": int(testing_pmax_theme_plan.get("candidate_count") or 0),
                "testing_pmax_asset_group_count": int(testing_pmax_theme_plan.get("asset_group_count") or 0),
                "testing_pmax_campaign_count": int(testing_pmax_theme_plan.get("campaign_count") or 0),
                "core_rsa_unique_keyword_count": len(core_rsa_terms),
                "core_max_clicks_rsa_unique_keyword_count": 0,
                "testing_rsa_unique_keyword_count": len(testing_terms),
                "testing_max_clicks_rsa_unique_keyword_count": 0,
                "fix_watch_rsa_unique_keyword_count": len(fix_watch_terms),
                "waste_recovery_rsa_unique_keyword_count": len(waste_recovery_terms),
                "retired_legacy_max_clicks_draft_count": retired_max_clicks_drafts,
                "audience_signal_term_count": int(audience_signal_plan.get("custom_segment_term_count") or 0),
                "audience_signal_similar_url_count": int(audience_signal_plan.get("similar_website_url_count") or 0),
                "testing_scale_negative_keyword_count": int(scale_negative_plan.get("negative_keyword_count") or 0),
                "testing_scale_pending_keyword_count": int(scale_negative_plan.get("pending_keyword_count") or 0),
                "testing_scale_page_exclusion_count": int(scale_page_exclusion_plan.get("page_exclusion_count") or 0),
                "testing_scale_pending_page_count": int(scale_page_exclusion_plan.get("pending_page_count") or 0),
                "waste_active_negative_keyword_count": int(waste_negative_plan.get("active_negative_keyword_count") or 0),
                "waste_active_page_exclusion_count": int(waste_page_exclusion_plan.get("active_page_exclusion_count") or 0),
                "waste_scale_recovery_keyword_count": int(waste_negative_plan.get("scale_recovery_keyword_count") or 0),
                "waste_testing_recovery_keyword_count": int(waste_negative_plan.get("testing_recovery_keyword_count") or 0),
                "waste_scale_recovery_page_count": int(waste_page_exclusion_plan.get("scale_recovery_page_count") or 0),
                "waste_testing_recovery_page_count": int(waste_page_exclusion_plan.get("testing_recovery_page_count") or 0),
                "fix_watch_landing_page_candidate_count": int(
                    (landing_page_governance.get("summary") or {}).get("fix_watch_candidate_page_count") or 0
                ),
                "ga4_scale_page_count": int((ga4_matrix.get("summary") or {}).get("scale_page_count") or 0),
                "ga4_testing_page_count": int((ga4_matrix.get("summary") or {}).get("testing_page_count") or 0),
                "ga4_scale_item_keyword_count": int((ga4_matrix.get("summary") or {}).get("scale_item_keyword_count") or 0),
                "ga4_testing_item_keyword_count": int((ga4_matrix.get("summary") or {}).get("testing_item_keyword_count") or 0),
                "testing_budget_ratio": budget["ratio"],
                "pmax_blocked_until_conversions": not decision["pmax_allowed"],
            },
            result_json=result,
        )
    )
    session.commit()
    return result


def record_peak_budget_event(
    session: Session,
    account: GoogleAdsAccount,
    *,
    action_type: str,
    status: str,
    summary: str,
    evidence: dict[str, Any],
    result: dict[str, Any],
    campaign_id: Optional[int] = None,
    campaign_name: Optional[str] = None,
) -> None:
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=campaign_id,
            campaign_name=campaign_name,
            action_type=action_type,
            status=status,
            summary=summary,
            evidence=evidence,
            result_json=result,
        )
    )


def peak_conversion_schedule_decision(
    session: Session,
    account: GoogleAdsAccount,
    *,
    warmup_minutes: int = 60,
    restore_delay_minutes: int = 0,
    increase_pct: float = 0.5,
    time_zone: str = "",
    require_fresh_snapshot: bool = False,
) -> dict[str, Any]:
    snapshot = latest_time_segments_snapshot(session, account)
    rows = rows_from_snapshot(snapshot)
    warmup_minutes = clamp_int(warmup_minutes, 60, 15, 240)
    restore_delay_minutes = clamp_int(restore_delay_minutes, 0, 0, 240)
    try:
        increase_pct = min(max(float(increase_pct), 0.01), 2.0)
    except (TypeError, ValueError):
        increase_pct = 0.5
    if not rows:
        return {
            "status": "fallback",
            "reason": "No saved Google Ads time_segments snapshot is available yet.",
            "peak_hour": None,
            "boost_start_hour": None,
            "restore_hour": None,
            "increase_pct": increase_pct,
            "time_zone": time_zone or "UTC",
            "hourly": [],
            "campaigns": [],
            "decided_at": utcnow().isoformat(),
        }
    if require_fresh_snapshot:
        fetched_at = _as_utc_datetime(snapshot.fetched_at if snapshot is not None else None)
        cutoff = utcnow().astimezone(timezone.utc) - timedelta(hours=MAX_AUTOMATION_DATA_AGE_HOURS)
        if fetched_at is None or fetched_at < cutoff:
            return {
                "status": "stale",
                "reason": "Saved Google Ads time_segments data is too old for a live peak-budget decision.",
                "peak_hour": None,
                "boost_start_hour": None,
                "restore_hour": None,
                "increase_pct": increase_pct,
                "time_zone": time_zone or "UTC",
                "source": f"{DATASET_TIME_SEGMENTS}:{snapshot.id}" if snapshot is not None else "",
                "snapshot_id": snapshot.id if snapshot is not None else None,
                "snapshot_fetched_at": snapshot.fetched_at.isoformat() if snapshot is not None and snapshot.fetched_at else None,
                "max_age_hours": MAX_AUTOMATION_DATA_AGE_HOURS,
                "hourly": [],
                "campaigns": [],
                "decided_at": utcnow().isoformat(),
            }

    hourly: dict[int, dict[str, Any]] = {
        hour: {
            "hour": hour,
            "impressions": 0,
            "clicks": 0,
            "cost": 0.0,
            "conversions": 0.0,
            "conversion_value": 0.0,
            "campaigns": {},
        }
        for hour in range(24)
    }
    for row in rows:
        try:
            hour = int(row.get("hour"))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour > 23:
            continue
        bucket = hourly[hour]
        impressions = int(numeric_value(row, "impressions"))
        clicks = int(numeric_value(row, "clicks"))
        cost = numeric_value(row, "cost")
        conversions = numeric_value(row, "conversions")
        conversion_value = numeric_value(row, "conversion_value")
        bucket["impressions"] += impressions
        bucket["clicks"] += clicks
        bucket["cost"] += cost
        bucket["conversions"] += conversions
        bucket["conversion_value"] += conversion_value
        campaign_id = int(row.get("campaign_id") or 0)
        if campaign_id:
            campaign = bucket["campaigns"].setdefault(
                str(campaign_id),
                {
                    "campaign_id": campaign_id,
                    "campaign_name": str(row.get("campaign_name") or ""),
                    "channel_type": str(row.get("channel_type") or ""),
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                },
            )
            campaign["impressions"] += impressions
            campaign["clicks"] += clicks
            campaign["cost"] += cost
            campaign["conversions"] += conversions
            campaign["conversion_value"] += conversion_value

    hourly_rows = []
    for bucket in hourly.values():
        campaigns = sorted(
            bucket["campaigns"].values(),
            key=lambda item: (float(item["conversions"]), float(item["conversion_value"]), int(item["clicks"]), int(item["impressions"])),
            reverse=True,
        )
        bucket = {**bucket, "campaigns": campaigns}
        hourly_rows.append(bucket)
    peak = max(
        hourly_rows,
        key=lambda item: (
            float(item["conversions"]),
            float(item["conversion_value"]),
            int(item["clicks"]),
            int(item["impressions"]),
        ),
    )
    peak_hour = int(peak["hour"])
    boost_start_hour = (peak_hour - 1) % 24
    restore_hour = (peak_hour + 1) % 24
    return {
        "status": "decided",
        "reason": "Selected the hour with the highest saved Google Ads conversions, then conversion value, clicks, and impressions as tie-breakers.",
        "peak_hour": peak_hour,
        "peak_time": schedule_time_label(peak_hour, 0),
        "boost_start_hour": boost_start_hour,
        "boost_start_time": schedule_time_label(boost_start_hour, 0),
        "restore_hour": restore_hour,
        "restore_time": schedule_time_label(restore_hour, restore_delay_minutes),
        "warmup_minutes": warmup_minutes,
        "restore_delay_minutes": restore_delay_minutes,
        "increase_pct": increase_pct,
        "time_zone": time_zone or "UTC",
        "source": f"{DATASET_TIME_SEGMENTS}:{snapshot.id}" if snapshot is not None else "",
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "snapshot_scope_key": snapshot.scope_key if snapshot is not None else "",
        "snapshot_fetched_at": snapshot.fetched_at.isoformat() if snapshot is not None and snapshot.fetched_at else None,
        "peak_hour_impressions": int(peak["impressions"] or 0),
        "peak_hour_clicks": int(peak["clicks"] or 0),
        "peak_hour_conversions": float(peak["conversions"] or 0),
        "peak_hour_conversion_value": float(peak["conversion_value"] or 0),
        "campaigns": peak["campaigns"],
        "campaign_ids": [int(item["campaign_id"]) for item in peak["campaigns"] if float(item.get("conversions") or 0) > 0],
        "hourly": sorted(hourly_rows, key=lambda item: int(item["hour"])),
        "decided_at": utcnow().isoformat(),
    }


def refresh_peak_budget_decision(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    fetch_time_zone: bool = False,
) -> dict[str, Any]:
    time_zone = str(preference.schedule_timezone or "UTC").strip() or "UTC"
    timezone_quota: Optional[dict[str, Any]] = None
    if fetch_time_zone and (not time_zone or time_zone == "UTC"):
        timezone_quota = reserve_automation_quota_units(
            session,
            preference,
            units=PEAK_TIMEZONE_FETCH_QUOTA_UNITS,
            reason="peak_budget_timezone_fetch",
        )
        if timezone_quota["allowed"]:
            try:
                fetched = fetch_account_time_zone(session, preference.account)
                if fetched:
                    time_zone = fetched
            except Exception:  # noqa: BLE001
                time_zone = time_zone or "UTC"
    decision = peak_conversion_schedule_decision(
        session,
        preference.account,
        warmup_minutes=preference.peak_budget_warmup_minutes,
        restore_delay_minutes=preference.peak_budget_restore_delay_minutes,
        increase_pct=preference.peak_budget_increase_pct,
        time_zone=time_zone,
        require_fresh_snapshot=True,
    )
    if timezone_quota is not None:
        decision["timezone_quota"] = timezone_quota
    preference.peak_budget_decision_json = decision
    preference.last_peak_budget_decision_at = utcnow()
    if time_zone:
        preference.schedule_timezone = time_zone
    session.commit()
    return decision


def local_peak_window(preference: GoogleAdsAutomationPreference, *, now: Optional[datetime] = None) -> dict[str, Any]:
    decision = preference.peak_budget_decision_json if isinstance(preference.peak_budget_decision_json, dict) else {}
    peak_hour = decision.get("peak_hour")
    if peak_hour is None:
        return {"status": "not_configured"}
    zone = safe_zoneinfo(preference.schedule_timezone)
    local_now = (now or utcnow()).astimezone(zone)
    peak_start = local_now.replace(hour=int(peak_hour), minute=0, second=0, microsecond=0)
    warmup_minutes = clamp_int(preference.peak_budget_warmup_minutes, 60, 15, 240)
    restore_delay_minutes = clamp_int(preference.peak_budget_restore_delay_minutes, 0, 0, 240)
    boost_start = peak_start - timedelta(minutes=warmup_minutes)
    restore_at = peak_start + timedelta(hours=1, minutes=restore_delay_minutes)
    if restore_at <= boost_start:
        restore_at += timedelta(days=1)
    if local_now < boost_start - timedelta(hours=12):
        boost_start -= timedelta(days=1)
        peak_start -= timedelta(days=1)
        restore_at -= timedelta(days=1)
    elif local_now >= restore_at + timedelta(hours=12):
        boost_start += timedelta(days=1)
        peak_start += timedelta(days=1)
        restore_at += timedelta(days=1)
    window_key = f"{boost_start.date().isoformat()}:{int(peak_hour):02d}:{warmup_minutes}:{restore_delay_minutes}"
    return {
        "status": "ready",
        "local_now": local_now,
        "boost_start": boost_start,
        "peak_start": peak_start,
        "restore_at": restore_at,
        "window_key": window_key,
        "in_boost_window": boost_start <= local_now < restore_at,
        "after_restore": local_now >= restore_at,
        "time_zone": preference.schedule_timezone,
    }


def iso_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def serializable_peak_window(window: dict[str, Any]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in window.items():
        if isinstance(value, datetime):
            serialized[key] = value.isoformat()
        else:
            serialized[key] = value
    return serialized


def peak_budget_due_now(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
) -> bool:
    if not preference.automation_enabled or not preference.auto_peak_budget_enabled:
        return False
    decision = preference.peak_budget_decision_json if isinstance(preference.peak_budget_decision_json, dict) else {}
    if decision.get("peak_hour") is None:
        decision = refresh_peak_budget_decision(session, preference, fetch_time_zone=False)
    if float(decision.get("peak_hour_conversions") or 0) <= 0:
        return False

    now = now or utcnow()
    state = load_peak_budget_state(session, preference.account)
    if state.get("active"):
        restore_at = parse_iso_datetime(state.get("restore_at_utc"))
        if restore_at is not None and now.astimezone(timezone.utc) >= restore_at:
            return True

    window = local_peak_window(preference, now=now)
    if window.get("status") != "ready" or not window.get("in_boost_window"):
        return False
    window_key = str(window.get("window_key") or "")
    interval_minutes = clamp_int(preference.peak_budget_check_interval_minutes, 60, 15, 240)
    last_check = preference.last_peak_budget_check_at
    if last_check is not None and last_check.astimezone(timezone.utc) > now.astimezone(timezone.utc) - timedelta(minutes=interval_minutes):
        return False
    if state.get("active") and state.get("window_key") == window_key:
        return True
    return True


def peak_budget_restore_due_now(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
) -> bool:
    if not preference.automation_enabled or not preference.auto_peak_budget_enabled:
        return False
    state = load_peak_budget_state(session, preference.account)
    if not state.get("active"):
        return False
    restore_at = parse_iso_datetime(state.get("restore_at_utc"))
    return bool(restore_at is not None and (now or utcnow()).astimezone(timezone.utc) >= restore_at)


def budget_guard_due_now(preference: GoogleAdsAutomationPreference, *, now: Optional[datetime] = None) -> bool:
    if not preference.automation_enabled or not preference.odoo_sales_guard_enabled:
        return False
    interval_hours = clamp_int(preference.budget_guard_check_interval_hours, 6, 1, 24)
    now = (now or utcnow()).astimezone(timezone.utc)
    last_run = preference.last_budget_guard_run_at
    if last_run is None:
        return True
    return last_run.astimezone(timezone.utc) <= now - timedelta(hours=interval_hours)


def peak_budget_mutation_guard(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    action_enabled: Optional[bool] = None,
) -> dict[str, Any]:
    settings = get_sync_setting_map(session)
    allow_mutations = parse_bool(settings.get("optimizer.allow_mutations", False))
    dry_run = parse_bool(settings.get("optimizer.dry_run", True))
    enabled = bool(preference.auto_peak_budget_enabled if action_enabled is None else action_enabled)
    can_mutate = bool(
        enabled
        and not preference.monitor_only
        and allow_mutations
        and not dry_run
    )
    return {
        "can_mutate": can_mutate,
        "validate_only": not can_mutate,
        "monitor_only": bool(preference.monitor_only),
        "optimizer_allow_mutations": allow_mutations,
        "optimizer_dry_run": dry_run,
        "action_enabled": enabled,
    }


def fetch_campaign_budget_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer.currency_code,
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign_budget.resource_name,
          campaign_budget.name,
          campaign_budget.amount_micros,
          campaign_budget.explicitly_shared
        FROM campaign
        WHERE campaign.status = ENABLED
    """
    rows: list[dict[str, Any]] = []
    for row in service.search(customer_id=account.customer_id, query=query):
        rows.append(
            {
                "currency_code": str(row.customer.currency_code or account.currency_code or ""),
                "campaign_id": int(row.campaign.id),
                "campaign_name": str(row.campaign.name or ""),
                "campaign_resource_name": str(row.campaign.resource_name or ""),
                "campaign_status": enum_name(row.campaign.status),
                "channel_type": enum_name(row.campaign.advertising_channel_type),
                "budget_resource_name": str(row.campaign_budget.resource_name or ""),
                "budget_name": str(row.campaign_budget.name or ""),
                "amount_micros": int(row.campaign_budget.amount_micros or 0),
                "explicitly_shared": bool(row.campaign_budget.explicitly_shared),
            }
        )
    return rows


def mutate_campaign_budgets(
    client: Any,
    account: GoogleAdsAccount,
    operations: list[dict[str, Any]],
    *,
    validate_only: bool,
) -> dict[str, Any]:
    if not operations:
        return {"resource_names": [], "operation_count": 0, "validate_only": validate_only}
    mutate_operations = []
    for item in operations:
        operation = client.get_type("CampaignBudgetOperation")
        operation.update.resource_name = str(item["budget_resource_name"])
        operation.update.amount_micros = int(item["new_amount_micros"])
        operation.update_mask.paths.append("amount_micros")
        mutate_operations.append(operation)

    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(mutate_operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=request)
    partial_failure = getattr(response, "partial_failure_error", None)
    partial_failure_message = ""
    if partial_failure is not None and getattr(partial_failure, "code", 0):
        partial_failure_message = str(getattr(partial_failure, "message", "") or partial_failure)
    return {
        "resource_names": [result.resource_name for result in response.results],
        "operation_count": len(operations),
        "validate_only": validate_only,
        "partial_failure": partial_failure_message,
    }


def _selected_peak_budget_operations(
    budget_rows: list[dict[str, Any]],
    decision: dict[str, Any],
    increase_pct: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    campaign_ids = {int(item) for item in (decision.get("campaign_ids") or []) if item}
    operations_by_budget: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for row in budget_rows:
        if campaign_ids and int(row.get("campaign_id") or 0) not in campaign_ids:
            continue
        if _is_waste_recovery_campaign_name(row.get("campaign_name")):
            skipped.append(
                {
                    "campaign_id": row.get("campaign_id"),
                    "campaign_name": row.get("campaign_name"),
                    "budget_resource_name": row.get("budget_resource_name"),
                    "reason": "fixed_waste_budget_protected",
                }
            )
            continue
        if row.get("explicitly_shared"):
            skipped.append(
                {
                    "campaign_id": row.get("campaign_id"),
                    "campaign_name": row.get("campaign_name"),
                    "budget_resource_name": row.get("budget_resource_name"),
                    "reason": "shared_budget",
                }
            )
            continue
        old_amount = int(row.get("amount_micros") or 0)
        if old_amount <= 0:
            skipped.append(
                {
                    "campaign_id": row.get("campaign_id"),
                    "campaign_name": row.get("campaign_name"),
                    "budget_resource_name": row.get("budget_resource_name"),
                    "reason": "missing_budget_amount",
                }
            )
            continue
        budget_resource_name = str(row.get("budget_resource_name") or "")
        if not budget_resource_name:
            continue
        if budget_resource_name not in operations_by_budget:
            new_amount = max(old_amount + 1, int(round(old_amount * (1 + increase_pct))))
            operations_by_budget[budget_resource_name] = {
                "budget_resource_name": budget_resource_name,
                "budget_name": row.get("budget_name"),
                "currency_code": row.get("currency_code"),
                "old_amount_micros": old_amount,
                "new_amount_micros": new_amount,
                "old_amount": micros_to_currency(old_amount),
                "new_amount": micros_to_currency(new_amount),
                "campaign_ids": [],
                "campaign_names": [],
            }
        operation = operations_by_budget[budget_resource_name]
        operation["campaign_ids"].append(int(row.get("campaign_id") or 0))
        operation["campaign_names"].append(str(row.get("campaign_name") or ""))
    return list(operations_by_budget.values()), skipped


def _amount_to_inr(amount: float, currency_code: str, rates: dict[str, Any]) -> Optional[float]:
    return convert_amount(float(amount or 0), currency_code or "UNKNOWN", "INR", rates)


def cap_budget_increase_operations_by_sales_room(
    operations: list[dict[str, Any]],
    sales_guard: Optional[dict[str, Any]],
    *,
    rates: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not operations or not sales_guard:
        return operations, []
    if str(sales_guard.get("status") or "") == "red":
        return [], [
            {
                "reason": "odoo_sales_cap_red",
                "spend_ratio": sales_guard.get("spend_ratio"),
                "max_ratio": sales_guard.get("max_ratio"),
                "sales_inr": sales_guard.get("sales_inr"),
                "ad_cost_inr": sales_guard.get("ad_cost_inr"),
            }
        ]

    daily_room_inr = float(sales_guard.get("daily_spend_room_inr") or 0)
    if daily_room_inr <= 0:
        return [], [
            {
                "reason": "no_odoo_sales_spend_room",
                "spend_ratio": sales_guard.get("spend_ratio"),
                "max_ratio": sales_guard.get("max_ratio"),
                "remaining_spend_room_inr": sales_guard.get("remaining_spend_room_inr"),
            }
        ]

    increments: list[tuple[dict[str, Any], int, float]] = []
    total_increment_inr = 0.0
    skipped: list[dict[str, Any]] = []
    for operation in operations:
        old_micros = int(operation.get("old_amount_micros") or 0)
        new_micros = int(operation.get("new_amount_micros") or 0)
        increment_micros = max(0, new_micros - old_micros)
        if increment_micros <= 0:
            skipped.append({**operation, "reason": "no_budget_increment"})
            continue
        increment_amount = micros_to_currency(increment_micros)
        increment_inr = _amount_to_inr(increment_amount, str(operation.get("currency_code") or ""), rates)
        if increment_inr is None:
            skipped.append({**operation, "reason": "currency_conversion_unavailable"})
            continue
        increments.append((operation, increment_micros, increment_inr))
        total_increment_inr += increment_inr

    if total_increment_inr <= daily_room_inr:
        return [item[0] for item in increments], skipped

    scale = max(min(daily_room_inr / total_increment_inr, 1.0), 0.0)
    capped: list[dict[str, Any]] = []
    for operation, increment_micros, increment_inr in increments:
        old_micros = int(operation.get("old_amount_micros") or 0)
        capped_increment_micros = int(round(increment_micros * scale))
        if capped_increment_micros <= 0:
            skipped.append({**operation, "reason": "odoo_sales_room_too_small"})
            continue
        new_amount_micros = old_micros + capped_increment_micros
        capped.append(
            {
                **operation,
                "new_amount_micros": new_amount_micros,
                "new_amount": micros_to_currency(new_amount_micros),
                "odoo_sales_room_limited": True,
                "requested_increment_inr": increment_inr,
                "applied_room_scale": scale,
            }
        )
    skipped.append(
        {
            "reason": "odoo_sales_room_scaled",
            "daily_spend_room_inr": daily_room_inr,
            "requested_increment_inr": total_increment_inr,
            "scale": scale,
        }
    )
    return capped, skipped


def recent_campaign_performance_rows(
    session: Session,
    account: GoogleAdsAccount,
    *,
    window_days: int,
) -> list[dict[str, Any]]:
    latest_metric_date = session.scalar(
        select(func.max(GoogleAdsCampaignMetric.metric_date)).where(
            GoogleAdsCampaignMetric.account_id == account.id,
        )
    )
    end_date = latest_metric_date or date.today()
    start_date = end_date - timedelta(days=min(max(int(window_days or 7), 1), 90) - 1)
    rows = session.execute(
        select(
            GoogleAdsCampaignMetric.campaign_id,
            func.max(GoogleAdsCampaignMetric.campaign_name),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.cost_micros), 0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.clicks), 0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.impressions), 0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.all_conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.conversions_value), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.all_conversions_value), 0.0),
        )
        .where(
            GoogleAdsCampaignMetric.account_id == account.id,
            GoogleAdsCampaignMetric.metric_date >= start_date,
        )
        .group_by(GoogleAdsCampaignMetric.campaign_id)
    ).all()
    return [
        {
            "campaign_id": int(campaign_id or 0),
            "campaign_name": campaign_name or "",
            "cost": int(cost_micros or 0) / 1_000_000,
            "clicks": int(clicks or 0),
            "impressions": int(impressions or 0),
            "conversions": float(conversions or 0),
            "all_conversions": float(all_conversions or 0),
            "conversion_value": float(conversion_value or 0),
            "all_conversions_value": float(all_conversions_value or 0),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        for campaign_id, campaign_name, cost_micros, clicks, impressions, conversions, all_conversions, conversion_value, all_conversions_value in rows
    ]


def underperforming_budget_reduction_operations(
    budget_rows: list[dict[str, Any]],
    performance_rows: list[dict[str, Any]],
    *,
    reduce_pct: float,
    minimum_daily_budget_amount: float,
    allow_conversion_efficiency_reductions: bool = False,
    required_roas: Optional[float] = None,
    force_account_cap_trim: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        reduce_pct = min(max(float(reduce_pct), 0.01), 0.90)
    except (TypeError, ValueError):
        reduce_pct = 0.20
    try:
        minimum_daily_budget_amount = max(float(minimum_daily_budget_amount), 0.0)
    except (TypeError, ValueError):
        minimum_daily_budget_amount = 1.0
    minimum_micros = int(round(minimum_daily_budget_amount * 1_000_000))
    performance_by_campaign = {int(row.get("campaign_id") or 0): row for row in performance_rows}
    operations_by_budget: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    convertible_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def make_operation(row: dict[str, Any], perf: dict[str, Any], *, reason: str) -> Optional[dict[str, Any]]:
        if row.get("explicitly_shared"):
            skipped.append({**row, "reason": "shared_budget"})
            return None
        old_amount = int(row.get("amount_micros") or 0)
        if old_amount <= minimum_micros:
            skipped.append({**row, "reason": "already_at_minimum_budget", "minimum_daily_budget_amount": minimum_daily_budget_amount})
            return None
        budget_resource_name = str(row.get("budget_resource_name") or "")
        if not budget_resource_name:
            return None
        new_amount = max(minimum_micros, int(round(old_amount * (1 - reduce_pct))))
        if new_amount >= old_amount:
            skipped.append({**row, "reason": "no_reduction_after_floor"})
            return None
        return {
            "budget_resource_name": budget_resource_name,
            "budget_name": row.get("budget_name"),
            "currency_code": row.get("currency_code"),
            "old_amount_micros": old_amount,
            "new_amount_micros": new_amount,
            "old_amount": micros_to_currency(old_amount),
            "new_amount": micros_to_currency(new_amount),
            "campaign_ids": [int(row.get("campaign_id") or 0)],
            "campaign_names": [str(row.get("campaign_name") or "")],
            "reduce_pct": reduce_pct,
            "minimum_daily_budget_amount": minimum_daily_budget_amount,
            "performance": perf,
            "reduction_reason": reason,
        }

    for row in budget_rows:
        if _is_waste_recovery_campaign_name(row.get("campaign_name")):
            skipped.append({**row, "reason": "fixed_waste_budget_protected"})
            continue
        campaign_id = int(row.get("campaign_id") or 0)
        perf = performance_by_campaign.get(campaign_id, {})
        conversions = float(perf.get("conversions") or 0) + float(perf.get("all_conversions") or 0)
        cost = float(perf.get("cost") or 0)
        clicks = int(perf.get("clicks") or 0)
        min_evidence_cost = max(minimum_daily_budget_amount * 2, 10.0)
        if conversions > 0:
            convertible_rows.append((row, perf))
            skipped.append({**row, "reason": "has_purchase_conversion_signal"})
            continue
        if cost < min_evidence_cost and clicks < 6:
            skipped.append({**row, "reason": "insufficient_underperformance_evidence", "cost": cost, "clicks": clicks})
            continue
        operation = make_operation(row, perf, reason="zero_conversion_underperformer")
        if operation:
            operations_by_budget[str(operation["budget_resource_name"])] = operation

    if not operations_by_budget and allow_conversion_efficiency_reductions:
        required_roas = float(required_roas or 0)
        ranked_rows: list[tuple[float, float, dict[str, Any], dict[str, Any]]] = []
        for row, perf in convertible_rows:
            cost = float(perf.get("cost") or 0)
            clicks = int(perf.get("clicks") or 0)
            min_evidence_cost = max(minimum_daily_budget_amount * 2, 10.0)
            if cost < min_evidence_cost and clicks < 6:
                continue
            conversion_value = float(perf.get("conversion_value") or 0) + float(perf.get("all_conversions_value") or 0)
            roas = (conversion_value / cost) if cost > 0 and conversion_value > 0 else 0.0
            if required_roas > 0 and roas >= required_roas:
                skipped.append({**row, "reason": "efficient_google_value_signal", "roas": roas, "required_roas": required_roas})
                continue
            ranked_rows.append((roas, -cost, row, perf))
        for _roas, _negative_cost, row, perf in sorted(ranked_rows, key=lambda item: (item[0], item[1])):
            operation = make_operation(row, perf, reason="weak_conversion_efficiency_against_odoo_sales_cap")
            if operation:
                operations_by_budget[str(operation["budget_resource_name"])] = operation
        if not operations_by_budget and force_account_cap_trim:
            fallback_rows = []
            for row, perf in convertible_rows:
                cost = float(perf.get("cost") or 0)
                clicks = int(perf.get("clicks") or 0)
                min_evidence_cost = max(minimum_daily_budget_amount * 2, 10.0)
                if cost < min_evidence_cost and clicks < 6:
                    continue
                conversion_value = float(perf.get("conversion_value") or 0) + float(perf.get("all_conversions_value") or 0)
                roas = (conversion_value / cost) if cost > 0 and conversion_value > 0 else 0.0
                fallback_rows.append((roas, -cost, row, perf))
            for _roas, _negative_cost, row, perf in sorted(fallback_rows, key=lambda item: (item[0], item[1])):
                operation = make_operation(row, perf, reason="account_over_odoo_sales_cap_proportional_trim")
                if operation:
                    operations_by_budget[str(operation["budget_resource_name"])] = operation
    return list(operations_by_budget.values()), skipped


def recently_reduced_budget_resources(
    session: Session,
    account: GoogleAdsAccount,
    *,
    interval_hours: int,
    now: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    now = now or utcnow()
    since = now.astimezone(timezone.utc) - timedelta(hours=clamp_int(interval_hours, 6, 1, 72))
    rows = session.scalars(
        select(AutoPilotEvent)
        .where(
            AutoPilotEvent.account_id == account.id,
            AutoPilotEvent.action_type == "odoo_sales_budget_guard",
            AutoPilotEvent.status == "applied",
            AutoPilotEvent.created_at >= since,
        )
        .order_by(AutoPilotEvent.created_at.desc(), AutoPilotEvent.id.desc())
    ).all()
    reduced: dict[str, dict[str, Any]] = {}
    for event in rows:
        result = event.result_json if isinstance(event.result_json, dict) else {}
        for budget in result.get("budgets") or []:
            if not isinstance(budget, dict):
                continue
            resource_name = str(budget.get("budget_resource_name") or "")
            if resource_name and resource_name not in reduced:
                reduced[resource_name] = {
                    "event_id": event.id,
                    "reduced_at": event.created_at.isoformat() if event.created_at else None,
                    "old_amount": budget.get("old_amount"),
                    "new_amount": budget.get("new_amount"),
                    "campaign_ids": budget.get("campaign_ids") or [],
                    "campaign_names": budget.get("campaign_names") or [],
                }
    return reduced


def filter_recent_budget_reductions(
    operations: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    recent_reductions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not operations or not recent_reductions:
        return operations, skipped
    filtered: list[dict[str, Any]] = []
    for operation in operations:
        resource_name = str(operation.get("budget_resource_name") or "")
        recent = recent_reductions.get(resource_name)
        if recent:
            skipped.append(
                {
                    **operation,
                    "reason": "recent_budget_guard_reduction_cooldown",
                    "recent_reduction": recent,
                }
            )
            continue
        filtered.append(operation)
    return filtered, skipped


def run_odoo_sales_budget_guard(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    account = preference.account
    if not preference.odoo_sales_guard_enabled:
        return {"name": "odoo_sales_budget_guard", "status": "skipped", "reason": "Odoo sales spend guard is disabled."}
    guard = odoo_sales_budget_guard_for_account(
        session,
        account,
        window_days=preference.odoo_sales_guard_window_days,
        max_spend_ratio=preference.odoo_sales_max_spend_ratio,
    )
    evidence = {
        "source_job_id": source_job_id,
        "guard": guard,
        "max_spend_ratio": preference.odoo_sales_max_spend_ratio,
        "window_days": preference.odoo_sales_guard_window_days,
        "minimum_daily_budget_amount": preference.minimum_daily_budget_amount,
        "underperforming_budget_reduce_pct": preference.underperforming_budget_reduce_pct,
    }
    if guard.get("status") != "red":
        session.commit()
        return {
            "name": "odoo_sales_budget_guard",
            "status": guard.get("status"),
            "guard": guard,
            "action": "monitor",
            "reason": "Spend is within the Odoo sales cap.",
        }

    api_quota = reserve_automation_quota_units(
        session,
        preference,
        units=BUDGET_GUARD_QUOTA_UNITS,
        reason="odoo_sales_budget_guard_budget_check",
    )
    if not api_quota["allowed"]:
        result = {"guard": guard, "quota": api_quota}
        record_peak_budget_event(
            session,
            account,
            action_type="odoo_sales_budget_guard",
            status="deferred_quota",
            summary=api_quota["reason"],
            evidence=evidence,
            result=result,
        )
        session.commit()
        return {
            "name": "odoo_sales_budget_guard",
            "status": "deferred_quota",
            "action": "defer_budget_reduction",
            "reason": api_quota["reason"],
            "guard": guard,
            "quota": api_quota,
        }

    mutation_guard = peak_budget_mutation_guard(
        session,
        preference,
        action_enabled=preference.odoo_sales_guard_enabled,
    )
    validate_only = bool(mutation_guard["validate_only"])
    client = build_client(get_sync_setting_map(session), account.manager_customer_id, account.connection)
    budget_rows = fetch_campaign_budget_rows(client, account)
    performance_rows = recent_campaign_performance_rows(
        session,
        account,
        window_days=preference.odoo_sales_guard_window_days,
    )
    operations, skipped = underperforming_budget_reduction_operations(
        budget_rows,
        performance_rows,
        reduce_pct=preference.underperforming_budget_reduce_pct,
        minimum_daily_budget_amount=preference.minimum_daily_budget_amount,
        allow_conversion_efficiency_reductions=True,
        required_roas=(1 / float(preference.odoo_sales_max_spend_ratio or 0.15)),
        force_account_cap_trim=True,
    )
    recent_reductions = recently_reduced_budget_resources(
        session,
        account,
        interval_hours=preference.budget_guard_check_interval_hours,
    )
    operations, skipped = filter_recent_budget_reductions(operations, skipped, recent_reductions)
    if not operations:
        result = {"guard": guard, "mutation_guard": mutation_guard, "skipped": skipped, "recent_reductions": recent_reductions}
        record_peak_budget_event(
            session,
            account,
            action_type="odoo_sales_budget_guard",
            status="skipped",
            summary=(
                "Odoo sales spend cap is red, but recent budget-guard reductions are still cooling down."
                if recent_reductions
                else "Odoo sales spend cap is red, but no eligible underperforming non-shared budgets could be reduced."
            ),
            evidence=evidence,
            result=result,
        )
        session.commit()
        return {
            "name": "odoo_sales_budget_guard",
            "status": "red",
            "action": "no_eligible_reductions",
            "guard": guard,
            "skipped_count": len(skipped),
        }

    result = mutate_campaign_budgets(client, account, operations, validate_only=validate_only)
    result["guard"] = guard
    result["mutation_guard"] = mutation_guard
    result["budgets"] = operations
    result["skipped"] = skipped
    result["recent_reductions"] = recent_reductions
    status = "validated" if validate_only else "applied"
    record_peak_budget_event(
        session,
        account,
        action_type="odoo_sales_budget_guard",
        status=status,
        summary=f"Reduce {len(operations)} underperforming campaign budget(s) because Google spend is above {round(float(preference.odoo_sales_max_spend_ratio or 0.15) * 100, 2)}% of actual Odoo sales.",
        evidence=evidence,
        result=result,
    )
    session.commit()
    return {
        "name": "odoo_sales_budget_guard",
        "status": status,
        "action": "reduce_underperforming_budgets",
        "guard": guard,
        "budget_count": len(operations),
        "validate_only": validate_only,
    }


def run_peak_budget_transition(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    source_job_id: Optional[int] = None,
    now: Optional[datetime] = None,
    sales_guard: Optional[dict[str, Any]] = None,
    fetch_time_zone: bool = True,
) -> dict[str, Any]:
    account = preference.account
    now = now or utcnow()
    if not preference.auto_peak_budget_enabled:
        return {"name": "peak_budget", "status": "skipped", "reason": "Peak budget automation is disabled."}

    decision = refresh_peak_budget_decision(session, preference, fetch_time_zone=fetch_time_zone)
    if decision.get("peak_hour") is None or float(decision.get("peak_hour_conversions") or 0) <= 0:
        return {
            "name": "peak_budget",
            "status": "skipped",
            "reason": "No conversion peak is available in saved time insights.",
            "decision": {
                "status": decision.get("status"),
                "peak_hour": decision.get("peak_hour"),
                "peak_hour_conversions": decision.get("peak_hour_conversions"),
            },
        }

    window = local_peak_window(preference, now=now)
    if window.get("status") != "ready":
        return {"name": "peak_budget", "status": "skipped", "reason": "Peak budget window is not configured."}

    state = load_peak_budget_state(session, account)
    guard = peak_budget_mutation_guard(session, preference)
    validate_only = bool(guard["validate_only"])
    evidence = {
        "source_job_id": source_job_id,
        "guard": guard,
        "decision": {
            "source": decision.get("source"),
            "peak_time": decision.get("peak_time"),
            "boost_start_time": decision.get("boost_start_time"),
            "restore_time": decision.get("restore_time"),
            "time_zone": decision.get("time_zone"),
            "peak_hour_conversions": decision.get("peak_hour_conversions"),
            "peak_hour_conversion_value": decision.get("peak_hour_conversion_value"),
            "campaign_ids": decision.get("campaign_ids") or [],
        },
        "window": serializable_peak_window(window),
    }
    window_key = str(window.get("window_key") or "")

    active_restore_at = parse_iso_datetime(state.get("restore_at_utc")) if state.get("active") else None
    restore_due = bool(state.get("active") and active_restore_at is not None and now.astimezone(timezone.utc) >= active_restore_at)
    if restore_due:
        budgets = [item for item in state.get("budgets", []) if isinstance(item, dict)]
        operations = [
            {
                "budget_resource_name": item.get("budget_resource_name"),
                "new_amount_micros": int(item.get("old_amount_micros") or 0),
                "old_amount_micros": int(item.get("boost_amount_micros") or item.get("new_amount_micros") or 0),
                "campaign_ids": item.get("campaign_ids") or [],
                "campaign_names": item.get("campaign_names") or [],
            }
            for item in budgets
            if item.get("budget_resource_name") and int(item.get("old_amount_micros") or 0) > 0
        ]
        if not operations:
            state["active"] = False
            state["restored_at_utc"] = iso_datetime(now)
            state["restore_result"] = {"mode": "skipped", "reason": "No original budget values were stored."}
            save_peak_budget_state(session, account, state)
            record_peak_budget_event(
                session,
                account,
                action_type="peak_budget_restore",
                status="skipped",
                summary="Peak budget restore skipped because no original budget values were stored.",
                evidence=evidence,
                result=state["restore_result"],
            )
            session.commit()
            return {"name": "peak_budget", "status": "skipped", "action": "restore", "reason": "No stored budgets."}

        api_quota = reserve_automation_quota_units(
            session,
            preference,
            units=PEAK_BUDGET_TRANSITION_QUOTA_UNITS,
            reason="peak_budget_restore",
        )
        if not api_quota["allowed"]:
            result = {"reason": api_quota["reason"], "quota": api_quota, "budgets": operations}
            record_peak_budget_event(
                session,
                account,
                action_type="peak_budget_restore",
                status="deferred_quota",
                summary=api_quota["reason"],
                evidence=evidence,
                result=result,
            )
            session.commit()
            return {
                "name": "peak_budget",
                "status": "deferred_quota",
                "action": "restore",
                "reason": api_quota["reason"],
                "quota": api_quota,
            }

        client = build_client(get_sync_setting_map(session), account.manager_customer_id, account.connection)
        result = mutate_campaign_budgets(client, account, operations, validate_only=validate_only)
        result["guard"] = guard
        result["quota"] = api_quota
        result["budgets"] = operations
        status = "validated" if validate_only else "applied"
        if not validate_only:
            state["active"] = False
            state["restored_at_utc"] = iso_datetime(now)
            state["restore_result"] = result
        else:
            state["last_restore_validation_at_utc"] = iso_datetime(now)
            state["last_restore_validation_result"] = result
        save_peak_budget_state(session, account, state)
        record_peak_budget_event(
            session,
            account,
            action_type="peak_budget_restore",
            status=status,
            summary=f"Restore {len(operations)} peak budget(s) to their original amount after the conversion peak window.",
            evidence=evidence,
            result=result,
        )
        session.commit()
        return {
            "name": "peak_budget",
            "status": status,
            "action": "restore",
            "budget_count": len(operations),
            "validate_only": validate_only,
        }

    if not window.get("in_boost_window"):
        return {
            "name": "peak_budget",
            "status": "waiting",
            "peak_time": decision.get("peak_time"),
            "boost_start": window["boost_start"].isoformat(),
            "restore_at": window["restore_at"].isoformat(),
            "time_zone": window.get("time_zone"),
        }
    if state.get("active") and state.get("window_key") == window_key:
        return {"name": "peak_budget", "status": "waiting", "reason": "Peak budget boost is already active."}
    if state.get("last_boost_transition_window_key") == window_key:
        return {"name": "peak_budget", "status": "waiting", "reason": "Peak budget boost was already handled for this window."}

    api_quota = reserve_automation_quota_units(
        session,
        preference,
        units=PEAK_BUDGET_TRANSITION_QUOTA_UNITS,
        reason="peak_budget_boost",
    )
    if not api_quota["allowed"]:
        result = {"reason": api_quota["reason"], "quota": api_quota}
        record_peak_budget_event(
            session,
            account,
            action_type="peak_budget_boost",
            status="deferred_quota",
            summary=api_quota["reason"],
            evidence=evidence,
            result=result,
        )
        session.commit()
        return {
            "name": "peak_budget",
            "status": "deferred_quota",
            "action": "boost",
            "reason": api_quota["reason"],
            "quota": api_quota,
        }

    values = get_sync_setting_map(session)
    client = build_client(values, account.manager_customer_id, account.connection)
    budget_rows = fetch_campaign_budget_rows(client, account)
    increase_pct = min(max(float(decision.get("increase_pct") or preference.peak_budget_increase_pct or 0.5), 0.01), 2.0)
    operations, skipped = _selected_peak_budget_operations(budget_rows, decision, increase_pct)
    if preference.odoo_sales_guard_enabled:
        peak_max_ratio = min(
            max(float(preference.odoo_sales_max_spend_ratio or 0.15) + float(preference.peak_budget_extra_spend_ratio or 0.05), 0.01),
            1.0,
        )
        sales_guard = odoo_sales_budget_guard_for_account(
            session,
            account,
            window_days=preference.odoo_sales_guard_window_days,
            max_spend_ratio=peak_max_ratio,
        )
        sales_guard["base_max_spend_ratio"] = float(preference.odoo_sales_max_spend_ratio or 0.15)
        sales_guard["peak_extra_spend_ratio"] = float(preference.peak_budget_extra_spend_ratio or 0.05)
        evidence["odoo_sales_guard"] = sales_guard
        rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
        capped_operations, sales_guard_skipped = cap_budget_increase_operations_by_sales_room(
            operations,
            sales_guard,
            rates=rates,
        )
        if sales_guard_skipped:
            skipped.extend(sales_guard_skipped)
        operations = capped_operations
    if not operations:
        state["last_boost_transition_window_key"] = window_key
        state["last_boost_validation_at_utc"] = iso_datetime(now)
        save_peak_budget_state(session, account, state)
        sales_cap_reasons = {"odoo_sales_cap_red", "no_odoo_sales_spend_room", "odoo_sales_room_too_small"}
        skip_reasons = {str(item.get("reason") or "") for item in skipped if isinstance(item, dict)}
        reason = (
            "Peak budget boost blocked by the Odoo sales spend cap."
            if skip_reasons.intersection(sales_cap_reasons)
            else "No eligible non-shared campaign budgets matched the conversion peak campaigns."
        )
        result = {"reason": reason, "skipped": skipped, "odoo_sales_guard": sales_guard}
        record_peak_budget_event(
            session,
            account,
            action_type="peak_budget_boost",
            status="skipped",
            summary=reason,
            evidence=evidence,
            result=result,
        )
        session.commit()
        return {"name": "peak_budget", "status": "skipped", "action": "boost", "reason": result["reason"]}

    result = mutate_campaign_budgets(client, account, operations, validate_only=validate_only)
    result["guard"] = guard
    result["quota"] = api_quota
    result["odoo_sales_guard"] = sales_guard
    result["increase_pct"] = increase_pct
    result["skipped"] = skipped
    result["budgets"] = operations
    status = "validated" if validate_only else "applied"
    state_update = {
        "active": not validate_only,
        "window_key": window_key,
        "last_boost_transition_window_key": window_key,
        "boosted_at_utc": iso_datetime(now),
        "boost_start_utc": iso_datetime(window["boost_start"]),
        "peak_start_utc": iso_datetime(window["peak_start"]),
        "restore_at_utc": iso_datetime(window["restore_at"]),
        "time_zone": window.get("time_zone"),
        "increase_pct": increase_pct,
        "validate_only": validate_only,
        "decision_source": decision.get("source"),
        "budgets": [
            {
                **operation,
                "boost_amount_micros": operation["new_amount_micros"],
            }
            for operation in operations
        ],
        "boost_result": result,
    }
    if not validate_only:
        state.clear()
    state.update(state_update)
    save_peak_budget_state(session, account, state)
    record_peak_budget_event(
        session,
        account,
        action_type="peak_budget_boost",
        status=status,
        summary=f"Increase {len(operations)} peak campaign budget(s) by {round(increase_pct * 100, 2)}% before the conversion peak.",
        evidence=evidence,
        result=result,
    )
    session.commit()
    return {
        "name": "peak_budget",
        "status": status,
        "action": "boost",
        "budget_count": len(operations),
        "validate_only": validate_only,
        "peak_time": decision.get("peak_time"),
        "boost_start": window["boost_start"].isoformat(),
        "restore_at": window["restore_at"].isoformat(),
        "time_zone": window.get("time_zone"),
    }


def low_traffic_schedule_decision(
    session: Session,
    account: GoogleAdsAccount,
    *,
    fallback_hour: int = DEFAULT_DYNAMIC_SCHEDULE_HOUR,
    fallback_minute: int = DEFAULT_DYNAMIC_SCHEDULE_MINUTE,
    time_zone: str = "",
) -> dict[str, Any]:
    snapshot = latest_time_segments_snapshot(session, account)
    rows = rows_from_snapshot(snapshot)
    fallback_hour = clamp_int(fallback_hour, DEFAULT_DYNAMIC_SCHEDULE_HOUR, 0, 23)
    fallback_minute = clamp_int(fallback_minute, DEFAULT_DYNAMIC_SCHEDULE_MINUTE, 0, 59)
    if not rows:
        return {
            "status": "fallback",
            "reason": "No saved Google Ads time_segments snapshot is available yet.",
            "recommended_hour": fallback_hour,
            "recommended_minute": fallback_minute,
            "recommended_time": schedule_time_label(fallback_hour, fallback_minute),
            "time_zone": time_zone or "UTC",
            "source": "",
            "hourly": [],
            "peak_hour": None,
            "low_hour": fallback_hour,
            "decided_at": utcnow().isoformat(),
        }

    hourly: dict[int, dict[str, Any]] = {
        hour: {
            "hour": hour,
            "impressions": 0,
            "clicks": 0,
            "cost": 0.0,
            "conversions": 0.0,
            "conversion_value": 0.0,
            "row_count": 0,
        }
        for hour in range(24)
    }
    for row in rows:
        try:
            hour = int(row.get("hour"))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour > 23:
            continue
        bucket = hourly[hour]
        bucket["impressions"] += int(numeric_value(row, "impressions"))
        bucket["clicks"] += int(numeric_value(row, "clicks"))
        bucket["cost"] += numeric_value(row, "cost")
        bucket["conversions"] += numeric_value(row, "conversions")
        bucket["conversion_value"] += numeric_value(row, "conversion_value")
        bucket["row_count"] += 1

    hourly_rows = list(hourly.values())
    peak = max(hourly_rows, key=lambda item: (item["impressions"], item["clicks"], item["cost"]))
    low = min(hourly_rows, key=lambda item: (item["impressions"], item["clicks"], item["conversions"], item["cost"]))
    recommended_hour = int(low["hour"])
    source = f"{DATASET_TIME_SEGMENTS}:{snapshot.id}" if snapshot is not None else ""
    total_impressions = sum(int(item["impressions"] or 0) for item in hourly_rows)
    peak_impressions = int(peak["impressions"] or 0)
    low_impressions = int(low["impressions"] or 0)
    return {
        "status": "decided",
        "reason": "Selected the hour with the lowest saved Google Ads impressions, then used clicks, conversions, and cost as tie-breakers.",
        "recommended_hour": recommended_hour,
        "recommended_minute": fallback_minute,
        "recommended_time": schedule_time_label(recommended_hour, fallback_minute),
        "time_zone": time_zone or "UTC",
        "source": source,
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "snapshot_scope_key": snapshot.scope_key if snapshot is not None else "",
        "snapshot_fetched_at": snapshot.fetched_at.isoformat() if snapshot is not None and snapshot.fetched_at else None,
        "row_count": len(rows),
        "total_impressions": total_impressions,
        "low_hour": recommended_hour,
        "low_hour_impressions": low_impressions,
        "peak_hour": int(peak["hour"]),
        "peak_hour_impressions": peak_impressions,
        "peak_to_low_ratio": (peak_impressions / low_impressions) if low_impressions else None,
        "hourly": sorted(hourly_rows, key=lambda item: int(item["hour"])),
        "decided_at": utcnow().isoformat(),
    }


def refresh_low_traffic_schedule(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    fetch_time_zone: bool = False,
) -> dict[str, Any]:
    time_zone = str(preference.schedule_timezone or "").strip()
    if fetch_time_zone and (not time_zone or time_zone == "UTC"):
        try:
            fetched = fetch_account_time_zone(session, preference.account)
            if fetched:
                time_zone = fetched
        except Exception:  # noqa: BLE001 - schedule can still be decided from saved time segments.
            time_zone = time_zone or "UTC"
    decision = low_traffic_schedule_decision(
        session,
        preference.account,
        fallback_hour=preference.scheduled_hour,
        fallback_minute=preference.scheduled_minute,
        time_zone=time_zone or "UTC",
    )
    preference.schedule_mode = "dynamic_low_traffic"
    preference.scheduled_hour = int(decision["recommended_hour"])
    preference.scheduled_minute = int(decision["recommended_minute"])
    preference.schedule_timezone = str(decision.get("time_zone") or "UTC")
    preference.schedule_source = str(decision.get("source") or "")
    preference.schedule_decision_json = decision
    preference.last_schedule_decision_at = utcnow()
    session.commit()
    return decision


def preference_due_now(preference: GoogleAdsAutomationPreference, *, now: Optional[datetime] = None) -> bool:
    if not preference.automation_enabled:
        return False
    if preference.schedule_mode != "dynamic_low_traffic":
        return True
    zone = safe_zoneinfo(preference.schedule_timezone)
    local_now = (now or utcnow()).astimezone(zone)
    scheduled_today = local_now.replace(
        hour=int(preference.scheduled_hour),
        minute=int(preference.scheduled_minute),
        second=0,
        microsecond=0,
    )
    if preference.last_run_at is not None:
        last_run_local = preference.last_run_at.astimezone(zone)
        if last_run_local >= scheduled_today:
            return False
    return scheduled_today <= local_now < scheduled_today + timedelta(hours=1)


def automation_strategy_summary(preference: Optional[GoogleAdsAutomationPreference]) -> dict[str, Any]:
    if preference is None:
        enabled = False
        monitor_only = True
        lookback_days = 60
        all_time_days = 7
        max_rows = 10000
        api_budget = 750
        cooldown_days = 3
        schedule_time = schedule_time_label(DEFAULT_DYNAMIC_SCHEDULE_HOUR, DEFAULT_DYNAMIC_SCHEDULE_MINUTE)
        schedule_timezone = "UTC"
        peak_budget_enabled = False
        peak_time = "not decided"
        peak_warmup_minutes = 60
        peak_increase_pct = 50
        sales_guard_enabled = True
        sales_guard_ratio = 15
        sales_guard_days = 7
        budget_guard_interval_hours = 6
        min_daily_budget = 1.0
        budget_reduce_pct = 20
        peak_extra_pct = 5
        peak_check_minutes = 60
        testing_bootstrap_enabled = True
        testing_bootstrap_days = 15
        pmax_min_7d_conversions = 15.0
        testing_sales_budget_pct = 5
        testing_keyword_limit = DEFAULT_TESTING_KEYWORD_LIMIT
        testing_landing_page_limit = DEFAULT_TESTING_LANDING_PAGE_LIMIT
        waste_currency_code = "USD"
    else:
        enabled = bool(preference.automation_enabled)
        monitor_only = bool(preference.monitor_only)
        lookback_days = preference.daily_keyword_lookback_days or 120
        all_time_days = preference.all_time_refresh_interval_days or 7
        max_rows = preference.max_daily_api_rows or 10000
        api_budget = preference.api_call_budget_per_day or 750
        cooldown_days = preference.mutation_cooldown_days or 3
        schedule_time = schedule_time_label(preference.scheduled_hour or DEFAULT_DYNAMIC_SCHEDULE_HOUR, preference.scheduled_minute or DEFAULT_DYNAMIC_SCHEDULE_MINUTE)
        schedule_timezone = preference.schedule_timezone or "UTC"
        peak_decision = preference.peak_budget_decision_json if isinstance(preference.peak_budget_decision_json, dict) else {}
        peak_budget_enabled = bool(preference.auto_peak_budget_enabled)
        peak_time = str(peak_decision.get("peak_time") or "not decided")
        peak_warmup_minutes = preference.peak_budget_warmup_minutes or 60
        peak_increase_pct = round(float(preference.peak_budget_increase_pct or 0.5) * 100, 2)
        sales_guard_enabled = bool(preference.odoo_sales_guard_enabled)
        sales_guard_ratio = round(float(preference.odoo_sales_max_spend_ratio or 0.15) * 100, 2)
        sales_guard_days = preference.odoo_sales_guard_window_days or 7
        budget_guard_interval_hours = preference.budget_guard_check_interval_hours or 6
        min_daily_budget = currency_minimum_daily_budget(preference.account, preference.minimum_daily_budget_amount)
        budget_reduce_pct = round(float(preference.underperforming_budget_reduce_pct or 0.2) * 100, 2)
        peak_extra_pct = round(float(preference.peak_budget_extra_spend_ratio or 0.05) * 100, 2)
        peak_check_minutes = preference.peak_budget_check_interval_minutes or 60
        testing_bootstrap_enabled = bool(preference.testing_bootstrap_enabled)
        testing_bootstrap_days = preference.testing_bootstrap_days or 15
        pmax_min_7d_conversions = pmax_conversion_threshold(preference)
        testing_sales_budget_pct = round(float(preference.testing_sales_budget_ratio or 0.05) * 100, 2)
        testing_keyword_limit = max(preference.testing_keyword_limit or DEFAULT_TESTING_KEYWORD_LIMIT, DEFAULT_TESTING_KEYWORD_LIMIT)
        testing_landing_page_limit = max(int(preference.testing_landing_page_limit or DEFAULT_TESTING_LANDING_PAGE_LIMIT or 0), 0)
        waste_currency_code = str((preference.account.currency_code if preference.account else "") or "USD").upper()
    waste_fixed_amount = waste_fixed_daily_budget(preference.account if preference else None)
    core_scale_roas = round(1 / max(float(sales_guard_ratio or 15) / 100, 0.01), 2)
    if core_scale_roas < CORE_SCALE_TARGET_ROAS:
        core_scale_roas = CORE_SCALE_TARGET_ROAS
    campaign_categories = [
        {
            "name": "Core / Scale",
            "budget": "80-85% of allowed spend inside the rolling Odoo sales cap",
            "target_roas": core_scale_roas,
            "target_roas_pct": round(core_scale_roas * 100),
            "campaign_types": "PMax for product/category scale, Target-ROAS RSA for proven exact/phrase terms, and AI Max DSA/page-feed URL inclusion for proven landing-page targets. Legacy Max Clicks lanes are retired.",
            "rule": "Add compatible winning keywords and page targets to existing campaigns first; create a new campaign only for a proven separate cluster that needs its own budget.",
        },
        {
            "name": "Testing / Discovery",
            "budget": "10-15% of allowed spend inside the rolling Odoo sales cap",
            "target_roas": TESTING_DISCOVERY_TARGET_ROAS,
            "target_roas_pct": round(TESTING_DISCOVERY_TARGET_ROAS * 100),
            "campaign_types": "Target-ROAS RSA from the keyword bank plus AI Max DSA page discovery. PMax drafts are prepared but held from live until automation-owned Search campaigns have enough conversion evidence. Legacy Max Clicks lanes are retired.",
            "rule": f"Use RSA/page discovery only for the first {testing_bootstrap_days} day(s) when delivery is missing or conversion evidence is thin; promote to Core / Scale only after purchase evidence, healthy spend efficiency, and no duplicate keyword or landing-page conflict.",
        },
        {
            "name": "Fix / Watch",
            "budget": "Up to 5% of Odoo sales only when room remains inside the total account cap",
            "target_roas": FIX_WATCH_TARGET_ROAS,
            "target_roas_pct": round(FIX_WATCH_TARGET_ROAS * 100),
            "campaign_types": "Low-impression rescue, delivery repair, and guarded landing-page rescue tests using non-Scale/non-Waste pages.",
            "rule": "Use 350% ROAS as the repair threshold; if purchase evidence does not appear after enough spend/clicks, reduce or pause instead of scaling.",
        },
        {
            "name": "Waste / Recovery",
            "budget": f"Fixed {waste_fixed_amount:,.2f} {waste_currency_code} daily budget; never peak-boosted and never reduced by the sales guard",
            "target_roas": WASTE_RECOVERY_TARGET_ROAS,
            "target_roas_pct": round(WASTE_RECOVERY_TARGET_ROAS * 100),
            "badge": f"Fixed {waste_fixed_amount:,.0f} {waste_currency_code}",
            "campaign_types": "Target-ROAS RSA recovery lane for released waste terms plus campaign-scoped exact negatives and AI Max/PMax/legacy DSA URL exclusions for active waste terms/pages.",
            "rule": f"Keep no-conversion waste blocked from Testing and Core / Scale. Release only after fresh positive evidence and a {WASTE_RECOVERY_HOLD_DAYS}-day recovery hold, then move conversion-backed items to Core / Scale or low-risk items to Testing review.",
        },
    ]
    return {
        "enabled": enabled,
        "mode": "Monitor only" if monitor_only else "Live-ready with mutation guards",
        "intervals": [
            {
                "name": "Daily insight refresh",
                "cadence": f"Daily at {schedule_time} {schedule_timezone}",
                "detail": f"Runtime is chosen from the account's lowest-impression Google Ads hour, then pulls the last {lookback_days} days of search terms, AI Max search term/ad combinations, search-term insights, landing pages, time, geo, and auction-pressure proxy data.",
            },
            {
                "name": "All-time keyword refresh",
                "cadence": f"Every {all_time_days} day(s)",
                "detail": "Refreshes the full available Google Ads search-term history plus GA4 Google Ads search terms, then dedupes into Postgres banks by normalized text.",
            },
            {
                "name": "Audience signal planning",
                "cadence": "After each daily insight refresh",
                "detail": "Builds PMax asset-group custom segment inputs from Scale search themes, GA4 search terms, GA4 ecommerce item intent, manually entered interest terms, GA4 referral/source domains, and any saved auction-domain fields.",
            },
            {
                "name": "Negative keyword review",
                "cadence": "After each search-term refresh",
                "detail": "Scores campaign-scoped exact negative candidates from saved search terms and AI Max term combinations, skipping any term with conversions, revenue, brand intent, or a saved positive keyword match.",
            },
            {
                "name": "Landing-page governance",
                "cadence": "After each landing-page and AI Max refresh",
                "detail": f"Assigns canonical URLs to Core / Scale, Testing / Discovery, Fix / Watch, or Waste / Recovery. Scale pages are removed from Testing only after the {SCALE_PAGE_MIGRATION_HOLD_DAYS}-day hold and Scale conversion proof; Waste pages are excluded everywhere until guarded recovery.",
            },
            {
                "name": "Waste quarantine and recovery",
                "cadence": "After each daily insight refresh",
                "detail": f"Builds a fixed {waste_fixed_amount:,.2f} {waste_currency_code} Waste / Recovery lane from negative keyword candidates and no-conversion landing-page waste. Its budget is protected from peak boosts and Odoo sales-guard reductions; recovery requires fresh positive evidence and a {WASTE_RECOVERY_HOLD_DAYS}-day hold before promotion.",
            },
            {
                "name": "Peak conversion budget",
                "cadence": (
                    f"Every {peak_check_minutes} minutes during the peak window; starts {peak_warmup_minutes} minutes before {peak_time} {schedule_timezone}"
                    if peak_budget_enabled
                    else "Disabled"
                ),
                "detail": f"Uses saved time insight conversions to raise eligible non-shared campaign budgets by {peak_increase_pct}% before the peak hour. The account cap is temporarily {sales_guard_ratio + peak_extra_pct}% of rolling Odoo sales during peak, then the exact original budgets are restored and the normal {sales_guard_ratio}% cap is enforced again.",
            },
            {
                "name": "Odoo sales guard",
                "cadence": f"Every {budget_guard_interval_hours} hour(s) over last {sales_guard_days} day(s)" if sales_guard_enabled else "Disabled",
                "detail": f"Caps Google spend at {sales_guard_ratio}% of actual synced Odoo website sales using a rolling {sales_guard_days}-day window, not a midnight reset. It blocks peak budget increases when no room is left and reduces underperforming non-shared budgets by {budget_reduce_pct}% down to a minimum daily budget of {min_daily_budget:,.2f}.",
            },
            {
                "name": "Testing campaign bootstrap",
                "cadence": "After each daily insight refresh" if testing_bootstrap_enabled else "Disabled",
                "detail": f"If an account has no last-7-day impressions, or is inside the {testing_bootstrap_days}-day bootstrap, automation publishes Testing / Discovery Search using Target ROAS RSA plus AI Max DSA page-discovery URL inclusions from the best keyword and URL banks. Core / Scale, Fix / Watch, and Waste / Recovery can stay in the closed loop, but PMax is drafted/held from live until automation-owned Search campaigns reach at least {pmax_min_7d_conversions:g} conversions. The testing lane uses {testing_sales_budget_pct:g}% of rolling Odoo sales, capped by the account guard.",
            },
            {
                "name": "Mutation cooldown",
                "cadence": f"{cooldown_days} day(s)",
                "detail": "Prevents repeated keyword, negative-keyword, campaign creation, or pause actions from firing too aggressively.",
            },
        ],
        "quota": {
            "api_call_budget_per_day": api_budget,
            "max_daily_api_rows": max_rows,
            "reuse_saved_snapshots": True,
            "odoo_sales_max_spend_ratio": sales_guard_ratio,
            "minimum_daily_budget": min_daily_budget,
            "budget_guard_check_interval_hours": budget_guard_interval_hours,
            "peak_budget_extra_spend_ratio": peak_extra_pct,
            "peak_budget_check_interval_minutes": peak_check_minutes,
            "sales_guard_window_type": "rolling",
            "fix_watch_target_roas": FIX_WATCH_TARGET_ROAS,
            "testing_bootstrap_enabled": testing_bootstrap_enabled,
            "testing_bootstrap_days": testing_bootstrap_days,
            "pmax_min_7d_conversions": pmax_min_7d_conversions,
            "testing_sales_budget_ratio": testing_sales_budget_pct,
            "testing_keyword_limit": testing_keyword_limit,
            "testing_landing_page_limit": testing_landing_page_limit,
            "waste_recovery_hold_days": WASTE_RECOVERY_HOLD_DAYS,
            "waste_fixed_daily_budget": waste_fixed_amount,
            "waste_currency_code": waste_currency_code,
        },
        "campaign_categories": campaign_categories,
        "guards": [
            "Postgres is the source of truth for account settings, jobs, snapshots, and candidate keywords.",
            "Fresh Google snapshots are reused for 24 hours unless Force refresh is explicitly requested.",
            "Keyword candidates are upserted by account and normalized keyword, so duplicates are not created.",
            "AI Max search-term/ad combinations are stored as their own snapshot dataset and feed both keyword and landing-page banks without duplicate keywords or URLs.",
            "GA4 Google Ads search terms are stored in their own deduped Postgres bank and reused by campaign planning, research, and PMax audience signals.",
            "PMax audience signal plans are attached at asset-group scope with search/interest terms and similar-website URLs; manual competitor URLs fill the gap when Auction Insights domains are not available through API data.",
            "Negative candidates are upserted by account, campaign scope, normalized keyword, and match type, so the same campaign does not receive duplicate exclusions.",
            "Landing-page proposals are deduped by canonical URL and governed by category: Waste excludes first, Scale reserve rules wait for proof, and RSA only receives final-URL guidance.",
            f"Waste / Recovery has a fixed {waste_fixed_amount:,.2f} {waste_currency_code} daily budget and is excluded from peak budget boosts and Odoo sales-guard reductions.",
            f"Fix / Watch campaigns use {round(FIX_WATCH_TARGET_ROAS * 100)}% ROAS as the repair threshold before they are allowed to leave watch mode.",
            "PMax, Core / Scale, Fix / Watch, and Waste / Recovery drafts are not live-created for no-impression or thin-signal accounts; bootstrap publishes Testing / Discovery Search only.",
            "Automation campaign names include the category and a stable AUTO code, so reconnecting the same Google Ads customer can resume the existing campaign instead of creating a duplicate.",
            "The Odoo sales guard uses a rolling sales window, so next-morning budgets do not collapse just because the calendar day changed.",
            "Live add/remove/pause/create actions remain blocked while Monitor only is on.",
        ],
    }


def all_time_refresh_due(preference: GoogleAdsAutomationPreference, now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    interval_days = clamp_int(preference.all_time_refresh_interval_days, 7, 1, 90)
    if preference.last_all_time_pull_at is None:
        return True
    return preference.last_all_time_pull_at <= now - timedelta(days=interval_days)


def daily_insight_api_window_days(
    preference: GoogleAdsAutomationPreference,
    now: Optional[datetime] = None,
    *,
    force: bool = False,
) -> tuple[int, str]:
    configured_days = clamp_int(preference.daily_keyword_lookback_days, 120, 1, 365)
    if force:
        return configured_days, "forced_full_configured_recent_window"
    if preference.last_all_time_pull_at is None:
        return configured_days, "baseline_recent_window_before_all_time_import"
    now = now or utcnow()
    if preference.last_keyword_pull_at is None:
        return configured_days, "activation_recent_window_after_existing_all_time_import"
    gap_days = max((now.date() - preference.last_keyword_pull_at.date()).days + 1, 1)
    incremental_days = min(configured_days, max(3, min(gap_days, 7)))
    return incremental_days, "incremental_last_3d_minimum_since_last_daily_pull"


def _odoo_preflight_sync_for_account(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh mapped Odoo data before budget/assets planning without spending Google Ads quota."""
    account = preference.account
    now = now or utcnow()
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping)
        .where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
        .order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)
    ).all()
    step: dict[str, Any] = {
        "name": "odoo_preflight_sync",
        "status": "skipped",
        "reason": "No active Odoo store mapping is attached to this Google Ads account.",
        "store_count": 0,
        "synced_store_count": 0,
        "errors": [],
        "stores": [],
    }
    if not mappings:
        return step

    stale_after = now - timedelta(hours=6)
    window_days = max(int(preference.odoo_sales_guard_window_days or 7), 7)
    seen_store_ids: set[int] = set()
    stores: list[OdooStore] = []
    website_ids_by_store: dict[int, set[int]] = {}
    for mapping in mappings:
        if mapping.store_id not in seen_store_ids:
            seen_store_ids.add(mapping.store_id)
            if mapping.store is not None and mapping.store.is_active:
                stores.append(mapping.store)
        if mapping.website_id:
            website_ids_by_store.setdefault(mapping.store_id, set()).add(int(mapping.website_id))

    step["store_count"] = len(stores)
    step["reason"] = ""
    if not stores:
        step["status"] = "skipped"
        step["reason"] = "Odoo mappings exist, but none point to an active store."
        return step

    synced_count = 0
    skipped_count = 0
    for store in stores:
        store_entry: dict[str, Any] = {
            "store_id": store.id,
            "store_name": store.name,
            "last_sync_at": store.last_sync_at.isoformat() if store.last_sync_at else None,
        }
        try:
            existing_order_count = int(
                session.scalar(
                    select(func.count(OdooSaleOrder.id)).where(OdooSaleOrder.store_id == store.id)
                )
                or 0
            )
            latest_order_at = session.scalar(
                select(func.max(OdooSaleOrder.order_datetime)).where(OdooSaleOrder.store_id == store.id)
            )
            should_sync = force or existing_order_count == 0 or store.last_sync_at is None or store.last_sync_at <= stale_after
            store_entry["existing_order_count"] = existing_order_count
            store_entry["latest_order_at"] = latest_order_at.isoformat() if latest_order_at else None
            store_entry["stale"] = bool(should_sync)
            if not should_sync:
                skipped_count += 1
                store_entry["status"] = "fresh"
                step["stores"].append(store_entry)
                continue

            baseline_days = 120 if existing_order_count == 0 or force else max(window_days + 2, 14)
            hours = baseline_days * 24
            websites_saved = sync_store_websites(session, store)
            selected_website_ids = sorted(website_ids_by_store.get(store.id) or [])
            orders_saved = sync_store_confirmed_orders(
                session,
                store,
                hours=hours,
                website_ids=selected_website_ids or None,
            )
            product_result = sync_store_product_page_feeds(session, store, days=max(baseline_days, 30))
            synced_count += 1
            store_entry.update(
                {
                    "status": "synced",
                    "hours": hours,
                    "website_ids": selected_website_ids,
                    "websites_saved": websites_saved,
                    "orders_saved": orders_saved,
                    "product_page_signals": product_result.get("saved") if isinstance(product_result, dict) else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep Ads automation moving; budget step will fall back safely.
            session.rollback()
            store_entry["status"] = "failed"
            store_entry["error"] = str(exc)[:500]
            step["errors"].append(
                {
                    "store_id": store.id,
                    "store_name": store.name,
                    "error": str(exc)[:500],
                }
            )
        step["stores"].append(store_entry)

    step["synced_store_count"] = synced_count
    step["skipped_store_count"] = skipped_count
    step["error_count"] = len(step["errors"])
    if synced_count:
        step["status"] = "done" if not step["errors"] else "partial"
    elif step["errors"]:
        step["status"] = "failed"
    else:
        step["status"] = "fresh"
    return step


def run_account_automation_monitor(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    source_job_id: Optional[int] = None,
    force: bool = False,
    budget_only: bool = False,
    progress_callback: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    account = preference.account
    now = utcnow()
    summary: dict[str, Any] = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "account_name": account.name,
        "mode": "monitor_only" if preference.monitor_only else "live_ready_guarded",
        "started_at": now.isoformat(),
        "steps": [],
        "planned_live_actions": [],
        "errors": [],
    }

    def checkpoint(name: str, status: str = "started", **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                name,
                {
                    "name": name,
                    "status": status,
                    "account_id": account.id,
                    "customer_id": account.customer_id,
                    "account_name": account.name,
                    "at": utcnow().isoformat(),
                    **extra,
                },
            )
        except Exception:
            pass

    if not preference.automation_enabled:
        summary["steps"].append({"name": "automation", "status": "skipped", "reason": "Account automation is disabled."})
        preference.last_run_at = now
        preference.last_analysis_at = now
        preference.strategy_summary_json = summary
        preference.last_error = None
        session.commit()
        return summary

    google_quota_retry = _google_ads_api_quota_retry_state(session, account, now=now)
    google_ads_api_blocked = bool(google_quota_retry)
    if google_quota_retry:
        summary["status"] = "blocked_by_google_quota"
        summary["google_ads_api_blocked"] = True
        summary["steps"].append(
            {
                "name": "google_ads_api_quota",
                "status": "blocked_by_google_quota",
                **google_quota_retry,
            }
        )

    if budget_only:
        schedule_decision = preference.schedule_decision_json if isinstance(preference.schedule_decision_json, dict) else {}
        if not schedule_decision:
            checkpoint("low_traffic_schedule")
            schedule_decision = refresh_low_traffic_schedule(session, preference, fetch_time_zone=False)
    else:
        checkpoint("low_traffic_schedule")
        schedule_decision = refresh_low_traffic_schedule(session, preference, fetch_time_zone=not google_ads_api_blocked)
    checkpoint("low_traffic_schedule", schedule_decision.get("status") or "done")
    summary["schedule_decision"] = {
        "recommended_time": schedule_decision.get("recommended_time"),
        "time_zone": schedule_decision.get("time_zone"),
        "low_hour_impressions": schedule_decision.get("low_hour_impressions"),
        "peak_hour": schedule_decision.get("peak_hour"),
        "peak_hour_impressions": schedule_decision.get("peak_hour_impressions"),
        "source": schedule_decision.get("source"),
    }
    summary["steps"].append(
        {
            "name": "low_traffic_schedule",
            "status": schedule_decision.get("status"),
            "recommended_time": schedule_decision.get("recommended_time"),
            "time_zone": schedule_decision.get("time_zone"),
            "low_hour_impressions": schedule_decision.get("low_hour_impressions"),
            "peak_hour": schedule_decision.get("peak_hour"),
            "peak_hour_impressions": schedule_decision.get("peak_hour_impressions"),
        }
    )
    checkpoint("odoo_preflight_sync")
    odoo_preflight_step = _odoo_preflight_sync_for_account(
        session,
        preference,
        now=now,
        force=force and preference.last_run_at is None,
    )
    checkpoint(
        "odoo_preflight_sync",
        odoo_preflight_step.get("status") or "done",
        store_count=odoo_preflight_step.get("store_count"),
        synced_store_count=odoo_preflight_step.get("synced_store_count"),
        error_count=odoo_preflight_step.get("error_count"),
    )
    summary["steps"].append(odoo_preflight_step)
    checkpoint("automation_budget_bootstrap")
    budget_bootstrap = automation_budget_bootstrap_state(
        session,
        preference,
        now=now,
        ensure=bool(preference.auto_create_campaigns_enabled and not preference.monitor_only),
    )
    checkpoint("automation_budget_bootstrap", budget_bootstrap.get("status") or "done")
    summary["budget_bootstrap"] = budget_bootstrap
    summary["steps"].append(budget_bootstrap)

    def run_sales_guard_step() -> dict[str, Any]:
        nonlocal preference
        if budget_bootstrap.get("active"):
            step = {
                "name": "odoo_sales_guard",
                "status": "bootstrap_deferred",
                "reason": "First-run budget bootstrap is active; spend-vs-sales reductions start after the 3-day opening window.",
                "bootstrap": budget_bootstrap,
                "guard": None,
            }
            summary["steps"].append(step)
            checkpoint("odoo_sales_guard", step.get("status") or "done")
            return step
        checkpoint("odoo_sales_guard")
        step = run_odoo_sales_budget_guard(
            session,
            preference,
            source_job_id=source_job_id,
        )
        preference = session.get(GoogleAdsAutomationPreference, preference.id) or preference
        preference.last_budget_guard_run_at = utcnow()
        session.commit()
        summary["steps"].append(step)
        checkpoint("odoo_sales_guard", step.get("status") or "done")
        return step

    def run_peak_budget_step(sales_step: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        nonlocal preference
        checkpoint("peak_budget_transition")
        step = run_peak_budget_transition(
            session,
            preference,
            source_job_id=source_job_id,
            now=now,
            sales_guard=sales_step.get("guard") if isinstance(sales_step, dict) else None,
            fetch_time_zone=not budget_only,
        )
        preference = session.get(GoogleAdsAutomationPreference, preference.id) or preference
        peak_waiting_reason = str(step.get("reason") or "") if isinstance(step, dict) else ""
        if (
            isinstance(step, dict)
            and (
                step.get("action") in {"boost", "restore"}
                or peak_waiting_reason
                in {
                    "Peak budget boost is already active.",
                    "Peak budget boost was already handled for this window.",
                }
            )
        ):
            preference.last_peak_budget_check_at = utcnow()
            session.commit()
        summary["steps"].append(step)
        checkpoint("peak_budget_transition", step.get("status") or "done")
        return step

    if google_ads_api_blocked:
        sales_budget_step = {
            "name": "odoo_sales_guard",
            "status": "blocked_by_google_quota",
            "reason": "Skipped Google Ads budget fetch/mutation while the Ads API quota retry window is active.",
            "quota": google_quota_retry,
        }
        peak_budget_step = {
            "name": "peak_budget_transition",
            "status": "blocked_by_google_quota",
            "reason": "Skipped Google Ads peak budget fetch/mutation while the Ads API quota retry window is active.",
            "quota": google_quota_retry,
        }
        summary["steps"].append(sales_budget_step)
        summary["steps"].append(peak_budget_step)
    elif peak_budget_restore_due_now(session, preference, now=now):
        run_peak_budget_step()
        run_sales_guard_step()
    else:
        sales_budget_step = run_sales_guard_step()
        run_peak_budget_step(sales_budget_step)

    if budget_only:
        summary["finished_at"] = utcnow().isoformat()
        preference.last_analysis_at = utcnow()
        preference.strategy_summary_json = summary
        preference.last_error = None
        session.commit()
        return summary

    metrics_refresh_ok = False
    if google_ads_api_blocked:
        metric_quota = google_quota_retry
        summary["steps"].append(
            {
                "name": "campaign_metric_refresh_7d",
                "status": "blocked_by_google_quota",
                "reason": "Skipped Google Ads metric refresh while the Ads API quota retry window is active.",
                "quota": metric_quota,
            }
        )
    else:
        metric_quota = reserve_automation_quota_units(
            session,
            preference,
            units=CAMPAIGN_METRIC_REFRESH_QUOTA_UNITS,
            reason="campaign_metric_refresh_7d",
            now=now,
        )
    if not google_ads_api_blocked and metric_quota["allowed"]:
        try:
            checkpoint("campaign_metric_refresh_7d")
            metric_rows_saved = sync_account_campaign_metrics(
                session,
                account,
                days=7,
                source_job_id=source_job_id,
            )
            checkpoint("campaign_metric_refresh_7d", "done", saved=metric_rows_saved)
            metrics_refresh_ok = True
            summary["steps"].append(
                {
                    "name": "campaign_metric_refresh_7d",
                    "status": "done",
                    "saved": metric_rows_saved,
                    "freshness_window_hours": MAX_AUTOMATION_DATA_AGE_HOURS,
                    "quota": metric_quota,
                }
            )
        except GoogleAdsException as exc:
            record_google_ads_api_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="automation_campaign_metric_refresh",
                severity="manual_action_required",
                extra={"days": 7},
            )
            preference = session.get(GoogleAdsAutomationPreference, preference.id) or preference
            account = preference.account
            summary["errors"].append({"step": "campaign_metric_refresh_7d", "error": str(exc)[:500]})
            summary["steps"].append(
                {
                    "name": "campaign_metric_refresh_7d",
                    "status": "failed",
                    "reason": "Google Ads metric refresh failed, so campaign planning will be deferred.",
                    "quota": metric_quota,
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep the monitor alive and defer planning.
            record_google_ads_generic_error(
                session,
                exc,
                account=account,
                job_id=source_job_id,
                context="automation_campaign_metric_refresh",
                severity="manual_action_required",
                extra={"days": 7},
            )
            preference = session.get(GoogleAdsAutomationPreference, preference.id) or preference
            account = preference.account
            summary["errors"].append({"step": "campaign_metric_refresh_7d", "error": str(exc)[:500]})
            summary["steps"].append(
                {
                    "name": "campaign_metric_refresh_7d",
                    "status": "failed",
                    "reason": "Google Ads metric refresh failed, so campaign planning will be deferred.",
                    "quota": metric_quota,
                }
            )
    elif not google_ads_api_blocked:
        summary["steps"].append(
            {
                "name": "campaign_metric_refresh_7d",
                "status": "deferred_quota",
                "reason": metric_quota["reason"],
                "quota": metric_quota,
            }
        )

    rows = clamp_int(preference.max_daily_api_rows, 10000, 50, 250000)
    daily_rows = rows
    days, daily_window_mode = daily_insight_api_window_days(preference, now=now, force=force)
    research: Optional[dict[str, Any]] = None
    if preference.keyword_discovery_enabled and google_ads_api_blocked:
        summary["steps"].append(
            {
                "name": "daily_insight_keyword_pull",
                "status": "blocked_by_google_quota",
                "reason": "Skipped Google Ads insight pull while the Ads API quota retry window is active.",
                "quota": google_quota_retry,
            }
        )
    elif preference.keyword_discovery_enabled and metrics_refresh_ok:
        daily_quota = reserve_automation_quota_units(
            session,
            preference,
            units=DAILY_INSIGHT_REFRESH_QUOTA_UNITS,
            reason="daily_insight_keyword_pull",
            now=now,
        )
        if daily_quota["allowed"]:
            checkpoint("daily_insight_keyword_pull", days=days, max_rows=daily_rows)
            research = sync_account_research_snapshots(
                session,
                account,
                days=days,
                max_rows=daily_rows,
                force=force,
                source_job_id=source_job_id,
            )
            checkpoint("daily_insight_google_snapshots", "done", datasets=research.get("datasets") or [])
            keyword_result = sync_account_keyword_candidates(
                session,
                account,
                scope_key=str(research.get("scope_key") or ""),
                source_job_id=source_job_id,
                force=force,
            )
            landing_page_result = sync_account_landing_page_candidates(
                session,
                account,
                scope_key=str(research.get("scope_key") or ""),
                source_job_id=source_job_id,
            )
            negative_result = (
                sync_account_negative_keyword_candidates(
                    session,
                    account,
                    scope_key=str(research.get("scope_key") or ""),
                    source_job_id=source_job_id,
                )
                if preference.negative_keyword_enabled
                else {"saved": 0, "candidate_count": 0, "skipped": {}, "status": "skipped"}
            )
            preference.last_keyword_pull_at = utcnow()
            checkpoint(
                "daily_insight_keyword_pull",
                "done",
                keywords_saved=keyword_result.get("saved"),
                landing_pages_saved=landing_page_result.get("saved"),
                negative_keywords_saved=negative_result.get("saved"),
            )
            summary["steps"].append(
                {
                    "name": "daily_insight_keyword_pull",
                    "status": "done",
                    "days": days,
                    "api_window_mode": daily_window_mode,
                    "max_rows": daily_rows,
                    "datasets": research.get("datasets") or [],
                    "errors": research.get("errors") or [],
                    "keywords_saved": keyword_result.get("saved"),
                    "candidate_count": keyword_result.get("candidate_count"),
                    "landing_pages_saved": landing_page_result.get("saved"),
                    "landing_page_count": landing_page_result.get("candidate_count"),
                    "negative_keywords_saved": negative_result.get("saved"),
                    "negative_keyword_count": negative_result.get("candidate_count"),
                    "negative_keyword_skipped": negative_result.get("skipped") or {},
                    "quota": daily_quota,
                }
            )
        else:
            summary["steps"].append(
                {
                    "name": "daily_insight_keyword_pull",
                    "status": "deferred_quota",
                    "reason": daily_quota["reason"],
                    "quota": daily_quota,
                }
            )
    elif preference.keyword_discovery_enabled:
        summary["steps"].append(
            {
                "name": "daily_insight_keyword_pull",
                "status": "deferred",
                "reason": "Skipped because the 7-day metric refresh failed; avoids making more Google Ads calls after a quota/API failure.",
            }
        )
    else:
        summary["steps"].append({"name": "daily_insight_keyword_pull", "status": "skipped"})

    checkpoint("ga4_mapping")
    ga4_connect_step = ensure_account_ga4_mapping(session, account)
    summary["steps"].append(ga4_connect_step)
    checkpoint("ga4_mapping", ga4_connect_step.get("status") or "done")
    checkpoint("ga4_ecommerce_refresh")
    ga4_step = maybe_refresh_ga4_ecommerce_for_account(
        session,
        account,
        source_job_id=source_job_id,
        force=force,
        now=utcnow(),
    )
    summary["steps"].append(ga4_step)
    checkpoint("ga4_ecommerce_refresh", ga4_step.get("status") or "done")
    try:
        ga4_matrix = ga4_ads_signal_matrix(session, account.id, limit=50)
        summary["ga4_ads_signal_matrix"] = {
            "generated_at": ga4_matrix.get("generated_at"),
            "summary": ga4_matrix.get("summary") or {},
            "snapshot_ids": ga4_matrix.get("snapshot_ids") or [],
            "basis": ga4_matrix.get("basis"),
        }
    except Exception as exc:  # noqa: BLE001 - keep monitor decisions moving on Ads-only data.
        summary["errors"].append({"step": "ga4_ads_signal_matrix", "error": str(exc)[:500]})
        summary["ga4_ads_signal_matrix"] = {"status": "failed", "error": str(exc)[:500]}

    checkpoint("search_console_mapping")
    search_console_connect_step = ensure_account_search_console_mapping(session, account)
    summary["steps"].append(search_console_connect_step)
    checkpoint("search_console_mapping", search_console_connect_step.get("status") or "done")
    try:
        checkpoint("search_console_query_page_pull")
        search_console_refresh = sync_search_console_search_analytics(
            session,
            mode="daily",
            days=GSC_DAILY_DAYS,
            max_rows=min(rows, GSC_DAILY_ROW_LIMIT),
            force=force,
            source_job_id=source_job_id,
            account_ids=[account.id],
        )
        checkpoint(
            "search_console_query_page_pull",
            "done" if not search_console_refresh.get("error_count") else "partial",
            snapshot_count=search_console_refresh.get("snapshot_count"),
            query_candidates_imported=search_console_refresh.get("query_candidates_imported"),
        )
        summary["steps"].append(
            {
                "name": "search_console_query_page_pull",
                "status": "done" if not search_console_refresh.get("error_count") else "partial",
                "scope_key": search_console_refresh.get("scope_key"),
                "target_count": search_console_refresh.get("target_count"),
                "dataset_count": search_console_refresh.get("dataset_count"),
                "snapshot_count": search_console_refresh.get("snapshot_count"),
                "query_candidates_imported": search_console_refresh.get("query_candidates_imported"),
                "errors": search_console_refresh.get("errors") or [],
            }
        )
        search_console_matrix = search_console_ads_signal_matrix(session, account.id, limit=50)
        summary["search_console_ads_signal_matrix"] = {
            "generated_at": search_console_matrix.get("generated_at"),
            "summary": search_console_matrix.get("summary") or {},
            "snapshot_ids": search_console_matrix.get("snapshot_ids") or [],
            "basis": search_console_matrix.get("basis"),
        }
    except Exception as exc:  # noqa: BLE001 - keep monitor decisions moving on Ads and GA4 data.
        summary["errors"].append({"step": "search_console_query_page_pull", "error": str(exc)[:500]})
        summary["steps"].append(
            {
                "name": "search_console_query_page_pull",
                "status": "failed",
                "reason": "Search Console refresh failed; campaign planning continues with Google Ads and GA4 data.",
                "error": str(exc)[:500],
            }
        )
        summary["search_console_ads_signal_matrix"] = {"status": "failed", "error": str(exc)[:500]}

    data_freshness = campaign_planning_data_freshness(
        session,
        account,
        research=research,
        metrics_refresh_ok=metrics_refresh_ok,
        now=utcnow(),
    )
    summary["data_freshness"] = data_freshness

    should_refresh_all_time = bool(force or all_time_refresh_due(preference))
    if preference.keyword_discovery_enabled and should_refresh_all_time:
        if google_ads_api_blocked:
            summary["steps"].append(
                {
                    "name": "all_time_keyword_pull",
                    "status": "blocked_by_google_quota",
                    "reason": "Skipped all-time Google Ads insight pull while the Ads API quota retry window is active.",
                    "quota": google_quota_retry,
                }
            )
        elif data_freshness["ok"]:
            all_time_quota = reserve_automation_quota_units(
                session,
                preference,
                units=ALL_TIME_REFRESH_QUOTA_UNITS,
                reason="all_time_keyword_pull",
                now=now,
            )
            if all_time_quota["allowed"]:
                checkpoint("all_time_keyword_pull", max_rows=rows)
                all_time = sync_account_all_time_keyword_candidates(
                    session,
                    account,
                    max_rows=rows,
                    force=force,
                    source_job_id=source_job_id,
                )
                all_time_pages = sync_account_landing_page_pull(
                    session,
                    account,
                    max_rows=rows,
                    mode="all_time",
                    force=force,
                    source_job_id=source_job_id,
                )
                ga4_all_time_search_terms = sync_ga4_search_term_snapshots(
                    session,
                    mode="all_time",
                    max_rows=rows,
                    force=force,
                    source_job_id=source_job_id,
                    account_ids=[account.id],
                )
                all_time_negatives = (
                    sync_account_negative_keyword_candidates(
                        session,
                        account,
                        source_job_id=source_job_id,
                    )
                    if preference.negative_keyword_enabled
                    else {"saved": 0, "candidate_count": 0, "skipped": {}, "status": "skipped"}
                )
                preference.last_all_time_pull_at = utcnow()
                checkpoint(
                    "all_time_keyword_pull",
                    "done",
                    keyword_result=(all_time.get("keyword_result") or {}).get("saved"),
                    landing_page_result=(all_time_pages.get("landing_page_result") or {}).get("saved"),
                )
                summary["steps"].append(
                    {
                        "name": "all_time_keyword_pull",
                        "status": "done",
                        "max_rows": rows,
                        "datasets": all_time.get("datasets") or [],
                        "keyword_result": all_time.get("keyword_result") or {},
                        "landing_page_result": all_time_pages.get("landing_page_result") or {},
                        "ga4_search_term_result": {
                            "target_count": ga4_all_time_search_terms.get("target_count"),
                            "dataset_count": ga4_all_time_search_terms.get("dataset_count"),
                            "error_count": ga4_all_time_search_terms.get("error_count"),
                            "search_terms_imported": ga4_all_time_search_terms.get("search_terms_imported"),
                        },
                        "negative_keyword_result": all_time_negatives,
                        "quota": all_time_quota,
                    }
                )
            else:
                summary["steps"].append(
                    {
                        "name": "all_time_keyword_pull",
                        "status": "deferred_quota",
                        "reason": all_time_quota["reason"],
                        "quota": all_time_quota,
                    }
                )
        else:
            summary["steps"].append(
                {
                    "name": "all_time_keyword_pull",
                    "status": "deferred",
                    "reason": "Skipped until current daily metrics and insight datasets are fresh enough for decisions.",
                    "data_freshness": data_freshness,
                }
            )
    else:
        summary["steps"].append(
            {
                "name": "all_time_keyword_pull",
                "status": "skipped",
                "reason": "Not due yet" if preference.keyword_discovery_enabled else "Keyword discovery is disabled.",
            }
        )

    if data_freshness["ok"]:
        checkpoint("testing_campaign_automation")
        campaign_plan_step = run_testing_campaign_automation(
            session,
            preference,
            source_job_id=source_job_id,
            data_freshness=data_freshness,
        )
        checkpoint("testing_campaign_automation", campaign_plan_step.get("status") or "done")
    else:
        blocked_drafts = block_automation_campaign_drafts_for_data_freshness(session, preference, data_freshness)
        campaign_plan_step = {
            "name": "testing_campaign_automation",
            "status": "deferred_stale_data",
            "reason": data_freshness["reason"],
            "data_freshness": data_freshness,
            "blocked_drafts": blocked_drafts,
        }
    summary["steps"].append(campaign_plan_step)

    live_campaign_creation_step: Optional[dict[str, Any]] = None
    def step_has_suspended_account_error(step: Optional[dict[str, Any]]) -> bool:
        if not isinstance(step, dict):
            return False
        try:
            payload = json.dumps(step, default=str)
        except Exception:
            payload = str(step)
        return "ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT" in payload or "account is suspended" in payload.lower()

    if preference.auto_create_campaigns_enabled:
        if google_ads_api_blocked:
            live_campaign_creation_step = {
                "name": "live_campaign_creation",
                "status": "blocked_by_google_quota",
                "reason": "Skipped live Google Ads mutations while the Ads API quota retry window is active.",
                "quota": google_quota_retry,
            }
        elif preference.monitor_only:
            live_campaign_creation_step = {
                "name": "live_campaign_creation",
                "status": "blocked_by_monitor_only",
                "reason": "Turn monitor-only off before live campaign creation.",
            }
        else:
            from app.services.google_ads_live_campaign_creator import enforce_automation_campaign_revisions, publish_automation_campaigns

            # The live publisher performs several Google API calls. End the
            # current transaction first so remote Postgres does not close an
            # idle-in-transaction connection while we wait on Google.
            session.commit()
            checkpoint("live_campaign_creation")
            live_campaign_creation_step = publish_automation_campaigns(
                session,
                preference,
                validate_only=False,
                progress_callback=checkpoint,
            )
            session.commit()
            checkpoint("live_campaign_revisions")
            campaign_revision_step = enforce_automation_campaign_revisions(
                session,
                preference,
                validate_only=False,
            )
            session.commit()
            checkpoint(
                "live_campaign_creation",
                live_campaign_creation_step.get("status") or "done",
                revision_status=campaign_revision_step.get("status") if isinstance(campaign_revision_step, dict) else None,
            )
            live_campaign_creation_step = {
                **live_campaign_creation_step,
                "revision": campaign_revision_step,
            }
        summary["steps"].append(live_campaign_creation_step)

    asset_publication_step: Optional[dict[str, Any]] = None
    if preference.auto_create_campaigns_enabled:
        try:
            settings_map = get_sync_setting_map(session)
            allow_mutations = parse_bool(settings_map.get("optimizer.allow_mutations", False))
            dry_run = parse_bool(settings_map.get("optimizer.dry_run", True))
            if step_has_suspended_account_error(live_campaign_creation_step):
                asset_publication_step = {
                    "name": "live_asset_publication",
                    "status": "blocked_by_suspended_account",
                    "reason": "Skipped asset mutations because Google Ads rejected campaign creation with ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT.",
                }
            elif google_ads_api_blocked:
                asset_publication_step = {
                    "name": "live_asset_publication",
                    "status": "blocked_by_google_quota",
                    "reason": "Skipped live Google Ads asset mutations while the Ads API quota retry window is active.",
                    "quota": google_quota_retry,
                }
            elif preference.monitor_only:
                asset_publication_step = {
                    "name": "live_asset_publication",
                    "status": "blocked_by_monitor_only",
                    "reason": "Turn monitor-only off before live asset publication.",
                }
            elif not allow_mutations or dry_run:
                asset_publication_step = {
                    "name": "live_asset_publication",
                    "status": "blocked_by_mutation_guard",
                    "reason": "Global mutation guards block live asset publication until Allow mutations is on and Dry run is off.",
                    "guard": {
                        "optimizer_allow_mutations": allow_mutations,
                        "optimizer_dry_run": dry_run,
                        "monitor_only": bool(preference.monitor_only),
                    },
                }
            else:
                quota_retry = _asset_publish_quota_retry_state(session, account)
                if quota_retry:
                    asset_publication_step = {
                        "name": "live_asset_publication",
                        "status": "blocked_by_google_quota",
                        **quota_retry,
                    }
                else:
                    # Asset generation/publishing may call Google and Odoo.
                    # Keep each external section out of an open DB transaction.
                    session.commit()
                    checkpoint("live_asset_generation")
                    generated_asset_result = generate_account_assets(
                        session,
                        account_id=account.id,
                        include_promotions=True,
                    )
                    session.commit()
                    checkpoint(
                        "live_asset_generation",
                        "done",
                        generated_count=generated_asset_result.get("generated_count"),
                        signal_count=generated_asset_result.get("signal_count"),
                    )
                    checkpoint("live_asset_publication")
                    client = build_client(settings_map, account.manager_customer_id, account.connection)
                    published_asset_result = publish_generated_assets(
                        session,
                        client,
                        account,
                        validate_only=False,
                        max_assets=100,
                    )
                    session.commit()
                    checkpoint(
                        "live_asset_publication",
                        "done" if not published_asset_result.get("failed") else "partial",
                        created=published_asset_result.get("created"),
                        linked=published_asset_result.get("linked"),
                        failed=published_asset_result.get("failed"),
                        quota_exhausted=published_asset_result.get("quota_exhausted"),
                    )
                    if published_asset_result.get("quota_exhausted"):
                        retry_after_seconds = int(published_asset_result.get("quota_retry_after_seconds") or 0)
                        if retry_after_seconds > 0:
                            quota_value = {
                                "customer_id": account.customer_id,
                                "account_id": account.id,
                                "reason": "Google Ads API basic-access operations quota exhausted during live asset publication",
                                "retry_after_seconds": retry_after_seconds,
                                "retry_not_before": (utcnow() + timedelta(seconds=retry_after_seconds)).isoformat(),
                                "recorded_at": utcnow().isoformat(),
                            }
                            stmt = insert(AppSetting).values(
                                key=f"{ASSET_PUBLISH_QUOTA_PREFIX}.{account.customer_id}",
                                value=quota_value,
                                category="google_ads_automation",
                                label=f"{account.name} asset publish quota retry",
                                help_text="Recorded when Google Ads API returns too many requests during live asset publication.",
                                input_type="json",
                                sensitive=False,
                                updated_at=utcnow(),
                            ).on_conflict_do_update(
                                index_elements=[AppSetting.key],
                                set_={"value": quota_value, "updated_at": utcnow()},
                            )
                            session.execute(stmt)
                            session.commit()
                    asset_publication_step = {
                        "name": "live_asset_publication",
                        "status": "done" if not published_asset_result.get("failed") else "partial",
                        "generated": generated_asset_result,
                        "published": published_asset_result,
                    }
        except Exception as exc:  # noqa: BLE001 - assets should not hide campaign-monitor results.
            session.rollback()
            asset_publication_step = {
                "name": "live_asset_publication",
                "status": "failed",
                "error": str(exc)[:1000],
            }
        summary["steps"].append(asset_publication_step)

    enabled_live_switches = [
        name
        for name, is_enabled in [
            ("auto_apply_keywords", preference.auto_apply_keywords_enabled),
            ("auto_apply_negatives", preference.auto_apply_negatives_enabled),
            ("auto_create_campaigns", preference.auto_create_campaigns_enabled),
            ("auto_pause_campaigns", preference.auto_pause_campaigns_enabled),
        ]
        if is_enabled
    ]

    def live_switch_status(switch_name: str) -> dict[str, Any]:
        if google_ads_api_blocked:
            return {
                "switch": switch_name,
                "status": "blocked_by_google_quota",
                "reason": "Google Ads API quota retry window is active.",
            }
        if preference.monitor_only:
            return {
                "switch": switch_name,
                "status": "blocked_by_monitor_only",
                "reason": "Monitor-only is on.",
            }
        if switch_name == "auto_create_campaigns":
            return {
                "switch": switch_name,
                "status": live_campaign_creation_step.get("status")
                if isinstance(live_campaign_creation_step, dict)
                else "skipped",
            }
        if switch_name in {"auto_apply_keywords", "auto_apply_negatives"}:
            if not preference.auto_create_campaigns_enabled:
                return {
                    "switch": switch_name,
                    "status": "requires_auto_create_campaigns",
                    "reason": "Keyword and negative mutations are applied while publishing or reconciling automation-owned campaigns.",
                }
            return {
                "switch": switch_name,
                "status": live_campaign_creation_step.get("status")
                if isinstance(live_campaign_creation_step, dict)
                else "skipped",
                "reason": "Applied inside the automation-owned campaign publish/reconcile path.",
            }
        if switch_name == "auto_pause_campaigns":
            if not preference.auto_create_campaigns_enabled:
                return {
                    "switch": switch_name,
                    "status": "requires_auto_create_campaigns",
                    "reason": "Campaign pause/revision actions are applied by the automation campaign revision engine.",
                }
            revision = (
                live_campaign_creation_step.get("revision")
                if isinstance(live_campaign_creation_step, dict)
                and isinstance(live_campaign_creation_step.get("revision"), dict)
                else None
            )
            return {
                "switch": switch_name,
                "status": revision.get("status") if isinstance(revision, dict) else "skipped",
                "reason": "Handled by the automation campaign revision engine for duplicate or outdated automation campaigns.",
            }
        return {
            "switch": switch_name,
            "status": "unknown",
        }

    summary["planned_live_actions"] = [
        live_switch_status(switch_name)
        for switch_name in enabled_live_switches
    ]
    suspended_account_block = step_has_suspended_account_error(live_campaign_creation_step) or step_has_suspended_account_error(asset_publication_step)
    if suspended_account_block:
        summary["status"] = "blocked_by_suspended_account"
        summary["google_ads_account_suspended"] = True
        preference.monitor_only = True
        preference.auto_create_campaigns_enabled = False
        preference.auto_apply_keywords_enabled = False
        preference.auto_apply_negatives_enabled = False
        preference.auto_pause_campaigns_enabled = False
        preference.last_error = (
            "Google Ads rejected live automation because this account is suspended "
            "(ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT). Automation was switched to monitor-only for this account."
        )
    else:
        preference.last_error = None
    summary["finished_at"] = utcnow().isoformat()
    preference.last_run_at = utcnow()
    preference.last_analysis_at = preference.last_run_at
    preference.strategy_summary_json = summary
    session.commit()
    return summary


def enabled_automation_preferences(
    session: Session,
    *,
    account_ids: Optional[list[int]] = None,
) -> list[GoogleAdsAutomationPreference]:
    query = (
        select(GoogleAdsAutomationPreference)
        .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAutomationPreference.account_id)
        .where(
            GoogleAdsAutomationPreference.automation_enabled.is_(True),
            GoogleAdsAccount.is_active.is_(True),
        )
        .order_by(GoogleAdsAccount.name)
    )
    selected_ids = [int(item) for item in (account_ids or []) if item]
    if selected_ids:
        query = query.where(GoogleAdsAutomationPreference.account_id.in_(selected_ids))
    return list(session.scalars(query).all())
