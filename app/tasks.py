from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import dramatiq
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from google.ads.googleads.errors import GoogleAdsException

from app.app_settings import get_sync_setting_map, optimizer_env_from_settings, parse_bool
from app.broker import configure_broker
from app.config import ROOT_DIR, get_settings
from app.models import (
    AccountStatus,
    AppSetting,
    BackgroundJob,
    BackgroundJobStatus,
    CampaignOptimizationRun,
    GoogleAdsAccount,
    GoogleAdsAutomationPreference,
    GoogleAdsDataSnapshot,
    GoogleAdsCustomerMatchPublication,
    GoogleAnalyticsConnection,
    GoogleSearchConsoleConnection,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    RunStatus,
    Strategy,
    StrategyRun,
    StrategyRunAccount,
)
from app.runtime_role import primary_instance_required_result
from app.services.cost_dashboard_snapshot import refresh_standard_cost_dashboard_snapshots
from app.services.google_ads_account_discovery import discover_accounts_for_connection
from app.services.google_ads_api_errors import (
    classify_google_ads_error,
    record_google_ads_api_error,
    record_google_ads_generic_error,
    summarize_google_ads_exception,
)
from app.services.google_ads_assets import generate_account_assets
from app.services.google_ads_automation import enabled_automation_preferences, run_account_automation_monitor
from app.services.google_ads_autopilot import run_account_autopilot
from app.services.google_ads_brand_lists import sync_account_brand_list_candidates
from app.services.campaign_optimizer import run_campaign_optimization
from app.services.google_ads_goals import sync_account_conversion_goals
from app.services.google_ads_page_feed_publisher import publish_page_feeds_for_mappings
from app.services.google_ads_keyword_bank import sync_account_all_time_keyword_candidates, sync_account_keyword_candidates
from app.services.google_ads_landing_page_bank import sync_account_landing_page_candidates, sync_account_landing_page_pull
from app.services.google_ads_negative_keyword_bank import sync_account_negative_keyword_candidates
from app.services.google_ads_policy_disapprovals import sync_account_policy_disapproval_terms
from app.services.google_ads_research_collector import sync_account_research_snapshots
from app.services.google_ads_snapshot_store import DATASET_CAMPAIGN_DAILY
from app.services.google_ads_sync import sync_account_campaign_metrics
from app.services.google_analytics import discover_analytics_connection, sync_ga4_ecommerce_snapshots, sync_ga4_search_term_snapshots
from app.services.google_search_console import (
    discover_search_console_connection,
    sync_search_console_search_analytics,
)
from app.services.customer_match import push_customer_match_outbox_for_publication, sync_customer_match_members_for_publication
from app.services.odoo_sales import sync_store_confirmed_orders, sync_store_websites
from app.services.odoo_product_feed import sync_mapped_product_page_feeds, sync_store_product_page_feeds
from app.services.razorpay_sync import sync_razorpay_daily_receipts
from app.services.strategy_engine import generate_value_strategy_recommendations


configure_broker()
settings = get_settings()
sync_engine = create_engine(
    settings.sqlalchemy_sync_url,
    connect_args={"sslmode": "disable", "connect_timeout": 20},
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=900,
    pool_timeout=30,
)
SessionLocal = sessionmaker(sync_engine, expire_on_commit=False)
AUTOMATION_RUN_LEASE_TTL = timedelta(hours=3)
GOOGLE_ADS_API_ACCOUNT_LEASE_TTL = timedelta(hours=3)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _automation_run_lease_key(account: GoogleAdsAccount) -> str:
    return f"automation_run_lease.{account.customer_id}"


def _google_ads_api_account_lease_key(account: GoogleAdsAccount) -> str:
    return f"google_ads_api_account_lease.{account.customer_id}"


def _automation_lease_time(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _lock_app_setting_key(session: Session, key: str) -> None:
    try:
        bind = session.get_bind()
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    except Exception:  # noqa: BLE001 - lightweight test sessions may not expose a bind.
        dialect_name = ""
    if dialect_name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def acquire_automation_run_lease(
    session: Session,
    account: GoogleAdsAccount,
    *,
    job_id: Optional[int],
) -> dict[str, Any]:
    now = utcnow()
    key = _automation_run_lease_key(account)
    _lock_app_setting_key(session, key)
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    state = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    expires_at = _automation_lease_time(state.get("expires_at"))
    active_job_id = state.get("job_id")
    if state.get("active") and active_job_id != job_id and expires_at and expires_at > now:
        return {
            "acquired": False,
            "key": key,
            "active_job_id": active_job_id,
            "expires_at": expires_at.isoformat(),
            "reason": "Another automation monitor is already running for this account.",
        }
    lease = {
        "active": True,
        "job_id": job_id,
        "account_id": account.id,
        "customer_id": account.customer_id,
        "acquired_at": now.isoformat(),
        "expires_at": (now + AUTOMATION_RUN_LEASE_TTL).isoformat(),
    }
    stmt = insert(AppSetting).values(
        key=key,
        value=lease,
        category="Automation state",
        label=f"{account.name} automation run lease",
        help_text="Prevents overlapping Google Ads automation monitor runs for the same account.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": lease,
            "category": "Automation state",
            "label": f"{account.name} automation run lease",
            "help_text": "Prevents overlapping Google Ads automation monitor runs for the same account.",
            "input_type": "json",
            "sensitive": False,
        },
    )
    session.execute(stmt)
    session.commit()
    return {"acquired": True, "key": key, "expires_at": lease["expires_at"]}


def release_automation_run_lease(
    session: Session,
    account: GoogleAdsAccount,
    *,
    job_id: Optional[int],
) -> None:
    key = _automation_run_lease_key(account)
    _lock_app_setting_key(session, key)
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None or not isinstance(row.value, dict) or row.value.get("job_id") != job_id:
        session.commit()
        return
    state = dict(row.value)
    now = utcnow()
    state.update({"active": False, "released_at": now.isoformat(), "expires_at": now.isoformat()})
    row.value = state
    session.commit()


def acquire_google_ads_api_account_lease(
    session: Session,
    account: GoogleAdsAccount,
    *,
    job_id: Optional[int],
    purpose: str,
) -> dict[str, Any]:
    quota_retry = google_ads_api_quota_retry_state(session, account)
    if quota_retry:
        return {"acquired": False, "status": "blocked_by_google_quota", **quota_retry}
    now = utcnow()
    key = _google_ads_api_account_lease_key(account)
    _lock_app_setting_key(session, key)
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    state = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    expires_at = _automation_lease_time(state.get("expires_at"))
    active_job_id = state.get("job_id")
    if state.get("active") and active_job_id != job_id and expires_at and expires_at > now:
        return {
            "acquired": False,
            "status": "busy",
            "key": key,
            "active_job_id": active_job_id,
            "active_purpose": state.get("purpose"),
            "expires_at": expires_at.isoformat(),
            "reason": "Another Google Ads API job is already running for this account.",
        }
    lease = {
        "active": True,
        "job_id": job_id,
        "purpose": str(purpose or "google_ads_api"),
        "account_id": account.id,
        "customer_id": account.customer_id,
        "acquired_at": now.isoformat(),
        "expires_at": (now + GOOGLE_ADS_API_ACCOUNT_LEASE_TTL).isoformat(),
    }
    stmt = insert(AppSetting).values(
        key=key,
        value=lease,
        category="Automation state",
        label=f"{account.name} Google Ads API account lease",
        help_text="Prevents overlapping Google Ads API pulls or mutations for the same account across workers and manual jobs.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": lease,
            "category": "Automation state",
            "label": f"{account.name} Google Ads API account lease",
            "help_text": "Prevents overlapping Google Ads API pulls or mutations for the same account across workers and manual jobs.",
            "input_type": "json",
            "sensitive": False,
        },
    )
    session.execute(stmt)
    session.commit()
    return {"acquired": True, "key": key, "expires_at": lease["expires_at"]}


def release_google_ads_api_account_lease(
    session: Session,
    account: GoogleAdsAccount,
    *,
    job_id: Optional[int],
) -> None:
    key = _google_ads_api_account_lease_key(account)
    _lock_app_setting_key(session, key)
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None or not isinstance(row.value, dict) or row.value.get("job_id") != job_id:
        session.commit()
        return
    state = dict(row.value)
    now = utcnow()
    state.update({"active": False, "released_at": now.isoformat(), "expires_at": now.isoformat()})
    row.value = state
    session.commit()


def google_ads_api_quota_retry_state(session: Session, account: GoogleAdsAccount) -> dict[str, Any] | None:
    now = utcnow()
    rows = []
    row = session.scalar(select(AppSetting).where(AppSetting.key == f"google_ads_asset_publish_quota.{account.customer_id}"))
    if row is not None:
        rows.append(row)
    if row is None:
        rows.extend(
            item
            for item in session.scalars(select(AppSetting).where(AppSetting.key.like("google_ads_asset_publish_quota.%"))).all()
            if google_ads_quota_setting_matches_account(item, account)
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
    retry_not_before, quota_row = max(active, key=lambda pair: pair[0])
    return {
        "retry_not_before": retry_not_before.isoformat(),
        "retry_after_seconds": max(0, int((retry_not_before - now).total_seconds())),
        "reason": quota_row.value.get("reason") or "Google Ads API quota retry window is active.",
        "scope": "google_ads_api",
        "quota_key": quota_row.key,
    }


def google_ads_connection_token_hash(account: GoogleAdsAccount) -> str:
    token = ""
    if account.connection is not None:
        token = str(account.connection.developer_token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def google_ads_quota_setting_matches_account(setting: AppSetting, account: GoogleAdsAccount) -> bool:
    if setting.key == f"google_ads_asset_publish_quota.{account.customer_id}":
        return True
    value = setting.value if isinstance(setting.value, dict) else {}
    if value.get("connection_id") and account.connection_id and int(value["connection_id"]) == int(account.connection_id):
        return True
    token_hash = google_ads_connection_token_hash(account)
    if token_hash and value.get("developer_token_hash") == token_hash:
        return True
    return False


def google_ads_quota_blocked_result(account: GoogleAdsAccount, quota_retry: dict[str, Any]) -> dict[str, Any]:
    reason = quota_retry.get("reason") or "Google Ads API quota retry window is active."
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "account_name": account.name,
        "mode": "live_ready_guarded",
        "status": "blocked_by_google_quota",
        "steps": [
            {
                "name": "google_ads_api_quota",
                "status": "blocked_by_google_quota",
                **quota_retry,
            },
            {
                "name": "policy_disapproved_unapproved_substances",
                "status": "blocked_by_google_quota",
                "reason": reason,
            },
            {
                "name": "odoo_order_brand_list_candidates",
                "status": "blocked_by_google_quota",
                "reason": reason,
            },
        ],
        "errors": [],
    }


def mark_preference_google_quota_blocked(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    result: dict[str, Any],
) -> None:
    now = utcnow()
    preference.last_run_at = now
    preference.last_analysis_at = now
    preference.last_error = None
    preference.strategy_summary_json = result
    session.commit()


def google_ads_exception_quota_retry_state(
    session: Session,
    account: GoogleAdsAccount,
    exc: GoogleAdsException,
) -> dict[str, Any] | None:
    summary = summarize_google_ads_exception(exc)
    error_code = classify_google_ads_error(summary)
    haystack = " ".join([str(error_code), str(summary.get("message") or ""), str(exc)]).lower()
    return google_ads_quota_retry_state_from_haystack(session, account, haystack)


def google_ads_generic_exception_quota_retry_state(
    session: Session,
    account: GoogleAdsAccount,
    exc: Exception,
) -> dict[str, Any] | None:
    haystack = " ".join([exc.__class__.__name__, str(exc)]).lower()
    return google_ads_quota_retry_state_from_haystack(session, account, haystack)


def google_ads_quota_retry_state_from_haystack(
    session: Session,
    account: GoogleAdsAccount,
    haystack: str,
) -> dict[str, Any] | None:
    if not any(token in haystack for token in ["quota", "resource exhausted", "too many requests", "basic access"]):
        return None
    retry_after_seconds = 3600
    match = re.search(r"retry\s+in\s+(\d+)\s+seconds?", haystack)
    if match:
        retry_after_seconds = max(60, int(match.group(1)))
    now = utcnow()
    retry_not_before = now + timedelta(seconds=retry_after_seconds)
    value = {
        "reason": "Google Ads API basic-access operations quota exhausted during live automation",
        "account_id": account.id,
        "customer_id": account.customer_id,
        "connection_id": account.connection_id,
        "developer_token_hash": google_ads_connection_token_hash(account),
        "recorded_at": now.isoformat(),
        "retry_not_before": retry_not_before.isoformat(),
        "retry_after_seconds": retry_after_seconds,
    }
    stmt = insert(AppSetting).values(
        key=f"google_ads_asset_publish_quota.{account.customer_id}",
        value=value,
        category="Google Ads automation",
        label=f"{account.name} Google Ads API quota retry",
        help_text="Temporarily blocks live Google Ads automation calls after the API reports quota exhaustion.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": value, "updated_at": now},
    )
    session.execute(stmt)
    session.commit()
    return google_ads_api_quota_retry_state(session, account) or {
        "retry_not_before": retry_not_before.isoformat(),
        "retry_after_seconds": retry_after_seconds,
        "reason": value["reason"],
        "scope": "google_ads_api",
    }


def mark_run(session: Session, run: StrategyRun, status: RunStatus, error: Optional[str] = None) -> None:
    run.status = status
    run.error = error
    if status == RunStatus.running:
        run.started_at = utcnow()
    if status in {RunStatus.succeeded, RunStatus.failed}:
        run.finished_at = utcnow()
    session.commit()


def _job(session: Session, job_id: Optional[int]) -> BackgroundJob | None:
    if not job_id:
        return None
    return session.get(BackgroundJob, job_id)


def mark_job_started(session: Session, job_id: Optional[int], *, total: Optional[int] = None) -> bool:
    job = _job(session, job_id)
    if job is None:
        return True
    if job.status in {BackgroundJobStatus.succeeded, BackgroundJobStatus.failed, BackgroundJobStatus.canceled}:
        return False
    if job.cancel_requested or job.status in {BackgroundJobStatus.cancel_requested, BackgroundJobStatus.canceled}:
        job.status = BackgroundJobStatus.canceled
        job.finished_at = utcnow()
        session.commit()
        return False
    job.status = BackgroundJobStatus.running
    job.started_at = job.started_at or utcnow()
    if total is not None:
        job.progress_total = int(total)
    session.commit()
    return True


def job_cancel_requested(session: Session, job_id: Optional[int]) -> bool:
    job = _job(session, job_id)
    return bool(job and (job.cancel_requested or job.status in {BackgroundJobStatus.cancel_requested, BackgroundJobStatus.canceled}))


def update_job_progress(
    session: Session,
    job_id: Optional[int],
    *,
    current: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    job = _job(session, job_id)
    if job is None:
        return
    if current is not None:
        job.progress_current = int(current)
    if total is not None:
        job.progress_total = int(total)
    session.commit()


def mark_job_finished(
    session: Session,
    job_id: Optional[int],
    status: BackgroundJobStatus,
    *,
    error: Optional[str] = None,
    current: Optional[int] = None,
) -> None:
    job = _job(session, job_id)
    if job is None:
        return
    job.status = status
    job.error = error
    if current is not None:
        job.progress_current = int(current)
    job.finished_at = utcnow()
    session.commit()


def account_config_for(account: GoogleAdsAccount, path: Path) -> None:
    data = {
        "manager_groups": [
            {
                "manager": {
                    "name": account.manager_name,
                    "customer_id": account.manager_customer_id,
                },
                "sub_accounts": [
                    {
                        "name": account.name,
                        "customer_id": account.customer_id,
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def state_setting_key(account: GoogleAdsAccount) -> str:
    return f"optimizer_state.{account.customer_id}"


def load_account_state(session: Session, account: GoogleAdsAccount) -> dict:
    row = session.scalar(select(AppSetting).where(AppSetting.key == state_setting_key(account)))
    if row is None or not isinstance(row.value, dict):
        return {"actions": {}}
    return row.value


def save_account_state(session: Session, account: GoogleAdsAccount, state: dict) -> None:
    stmt = insert(AppSetting).values(
        key=state_setting_key(account),
        value=state,
        category="Optimizer state",
        label=f"{account.name} optimizer cooldown state",
        help_text="Machine state persisted by optimizer cooldown tracking.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value},
    )
    session.execute(stmt)
    session.commit()


def latest_json_file(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    files = sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None


def latest_campaign_snapshot_payload(session: Session, account: GoogleAdsAccount) -> Optional[dict]:
    snapshot = session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == DATASET_CAMPAIGN_DAILY,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None or not isinstance(snapshot.payload_json, dict):
        return None
    rows = snapshot.payload_json.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    return snapshot.payload_json


def run_optimizer(
    session: Session,
    account: GoogleAdsAccount,
    strategy: Strategy,
) -> tuple[subprocess.CompletedProcess[str], Optional[dict], Optional[dict]]:
    db_settings = get_sync_setting_map(session)
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GOOGLE_ADS_") and not key.startswith("GOFINCH_GOOGLE_ADS_")
    }
    base_env.update(optimizer_env_from_settings(db_settings, account.connection))
    base_env["GOOGLE_ADS_CONFIG_PY"] = ""

    with tempfile.TemporaryDirectory(prefix="google-ads-worker-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        report_root = temp_dir / "reports"
        state_root = temp_dir / "state"
        account_state_dir = state_root / account.customer_id
        account_state_dir.mkdir(parents=True, exist_ok=True)
        state_path = account_state_dir / "google_ads_optimizer_state.json"
        state_path.write_text(json.dumps(load_account_state(session, account), indent=2, sort_keys=True) + "\n")

        config_path = temp_dir / "accounts.json"
        env_path = temp_dir / "empty.env"
        env_path.write_text("")
        account_config_for(account, config_path)
        campaign_snapshot_payload = latest_campaign_snapshot_payload(session, account)
        if campaign_snapshot_payload is not None:
            campaign_snapshot_path = temp_dir / "campaign_snapshot.json"
            campaign_snapshot_path.write_text(json.dumps(campaign_snapshot_payload, indent=2, sort_keys=True) + "\n")
            base_env["GOOGLE_ADS_CAMPAIGN_SNAPSHOT_JSON"] = str(campaign_snapshot_path)

        base_env["GOFINCH_GOOGLE_ADS_REPORT_DIR"] = str(report_root)
        base_env["GOFINCH_GOOGLE_ADS_STATE_DIR"] = str(state_root)
        base_env["GOOGLE_ADS_REPORT_DIR"] = str(report_root)
        base_env["GOOGLE_ADS_STATE_DIR"] = str(state_root)

        cmd = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "gofinch_google_ads_optimizer.py"),
            strategy.mode,
            "--account",
            account.customer_id,
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--stop-on-error",
        ]
        result = subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            text=True,
            capture_output=True,
            timeout=settings.worker_timeout_seconds,
            env=base_env,
        )

        report_json = None
        if parse_bool(db_settings.get("storage.persist_optimizer_reports", True)):
            report_path = latest_json_file(report_root / account.customer_id)
            if report_path is not None:
                report_json = json.loads(report_path.read_text())

        state_json = None
        if state_path.exists():
            state_json = json.loads(state_path.read_text())
            save_account_state(session, account, state_json)

        return result, report_json, state_json


def old_run_optimizer(account: GoogleAdsAccount, strategy: Strategy) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "gofinch_google_ads_optimizer.py"),
        strategy.mode,
        "--account",
        account.customer_id,
        "--stop-on-error",
    ]
    return subprocess.run(
        cmd,
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        timeout=settings.worker_timeout_seconds,
    )


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def execute_strategy_run(run_id: int, job_id: Optional[int] = None) -> None:
    with SessionLocal() as session:
        run = session.get(StrategyRun, run_id)
        if run is None:
            return
        total_work = len(run.account_ids or []) * len(run.strategy_keys or [])
        if not mark_job_started(session, job_id, total=total_work):
            return

        mark_run(session, run, RunStatus.running)
        failed = False
        canceled = False
        processed = 0

        accounts = session.scalars(
            select(GoogleAdsAccount).where(GoogleAdsAccount.id.in_(run.account_ids), GoogleAdsAccount.is_active.is_(True))
        ).all()
        strategies = session.scalars(
            select(Strategy).where(Strategy.key.in_(run.strategy_keys), Strategy.is_active.is_(True))
        ).all()
        account_by_id = {account.id: account for account in accounts}
        strategy_by_key = {strategy.key: strategy for strategy in strategies}

        for account_id in run.account_ids:
            if job_cancel_requested(session, job_id):
                canceled = True
                break
            account = account_by_id.get(account_id)
            if account is None:
                continue
            for strategy_key in run.strategy_keys:
                if job_cancel_requested(session, job_id):
                    canceled = True
                    break
                strategy = strategy_by_key.get(strategy_key)
                if strategy is None:
                    continue

                row = StrategyRunAccount(
                    run_id=run.id,
                    account_id=account.id,
                    strategy_key=strategy.key,
                    status=AccountStatus.running,
                    started_at=utcnow(),
                )
                session.add(row)
                session.commit()

                api_lease = None
                try:
                    api_lease = acquire_google_ads_api_account_lease(
                        session,
                        account,
                        job_id=job_id,
                        purpose=f"strategy_run:{strategy.key}",
                    )
                    if not api_lease.get("acquired"):
                        failed = True
                        row.status = AccountStatus.failed
                        row.error = api_lease.get("reason") or api_lease.get("status") or "Google Ads API account lease was not acquired."
                        continue
                    if strategy.mode == "autopilot":
                        report_json = run_account_autopilot(session, account, source_job_id=job_id)
                        row.output = json.dumps(report_json, indent=2, sort_keys=True)[-20000:]
                        row.report_json = report_json
                        row.optimizer_state_json = None
                        row.status = AccountStatus.failed if report_json.get("mode") == "failed" else AccountStatus.succeeded
                        if row.status == AccountStatus.failed:
                            failed = True
                            row.error = str(report_json.get("error") or "Autopilot failed.")
                    else:
                        result, report_json, state_json = run_optimizer(session, account, strategy)
                        output_text = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()[-20000:]
                        row.output = output_text
                        row.report_json = report_json
                        row.optimizer_state_json = state_json
                        row.status = AccountStatus.succeeded if result.returncode == 0 else AccountStatus.failed
                        if result.returncode != 0:
                            failed = True
                            record_google_ads_generic_error(
                                session,
                                RuntimeError(output_text or f"Optimizer exited with code {result.returncode}."),
                                account=account,
                                job_id=job_id,
                                context="optimizer_script",
                                extra={"strategy_key": strategy.key, "run_id": run.id, "returncode": result.returncode},
                            )
                            row.output = output_text
                            row.report_json = report_json
                            row.optimizer_state_json = state_json
                            row.status = AccountStatus.failed
                            row.error = f"Optimizer exited with code {result.returncode}."
                except Exception as exc:  # noqa: BLE001 - persist worker failures for operators.
                    failed = True
                    record_google_ads_generic_error(
                        session,
                        exc,
                        account=account,
                        job_id=job_id,
                        context="strategy_run",
                        extra={"strategy_key": strategy.key, "run_id": run.id},
                    )
                    row.status = AccountStatus.failed
                    row.error = str(exc)
                finally:
                    if api_lease and api_lease.get("acquired"):
                        release_google_ads_api_account_lease(session, account, job_id=job_id)
                    row.finished_at = utcnow()
                    session.commit()
                    processed += 1
                    update_job_progress(session, job_id, current=processed)
            if canceled:
                break

        run = session.get(StrategyRun, run_id)
        if run is not None:
            if canceled:
                mark_run(session, run, RunStatus.failed, error="Canceled from background jobs.")
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
            else:
                final_status = RunStatus.failed if failed else RunStatus.succeeded
                mark_run(session, run, final_status)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.failed if failed else BackgroundJobStatus.succeeded,
                    current=processed,
                    error="One or more strategy items failed." if failed else None,
                )


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_performance(
    days: int = 30,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_metric_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        sync_account_campaign_metrics(session, account, days, source_job_id=job_id)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="google_ads_sync",
                            extra={"days": days},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep bulk sync moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="google_ads_sync",
                            extra={"days": days},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                    return
                generate_value_strategy_recommendations(session)
                try:
                    refresh_standard_cost_dashboard_snapshots(session)
                except Exception:  # noqa: BLE001 - snapshots are a cache, sync data is already persisted.
                    errors += 1
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded,
                    current=processed,
                    error=f"{errors} account/snapshot errors were skipped." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001 - keep operator-facing job state accurate.
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_research(
    days: int = 60,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 5000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            keyword_saved = 0
            pages_saved = 0
            negative_saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_research_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        result = sync_account_research_snapshots(
                            session,
                            account,
                            days=days,
                            max_rows=max_rows,
                            force=force,
                            source_job_id=job_id,
                        )
                        if result.get("errors"):
                            errors += len(result["errors"])
                        keyword_result = sync_account_keyword_candidates(
                            session,
                            account,
                            scope_key=str(result.get("scope_key") or ""),
                            source_job_id=job_id,
                        )
                        landing_page_result = sync_account_landing_page_candidates(
                            session,
                            account,
                            scope_key=str(result.get("scope_key") or ""),
                            source_job_id=job_id,
                        )
                        keyword_saved += int(keyword_result.get("saved") or 0)
                        pages_saved += int(landing_page_result.get("saved") or 0)
                    except Exception as exc:  # noqa: BLE001 - keyword bank is reusable cache, keep the sync moving.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="insight_bank_sync",
                            severity="manual_action_required",
                            extra={"days": days, "max_rows": max_rows},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} dataset/bank errors were recorded as dashboard alerts. {keyword_saved} keyword rows and {pages_saved} landing-page rows refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_daily_keywords(
    days: int = 60,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 5000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            keyword_saved = 0
            pages_saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_daily_keyword_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        result = sync_account_research_snapshots(
                            session,
                            account,
                            days=days,
                            max_rows=max_rows,
                            force=force,
                            source_job_id=job_id,
                        )
                        if result.get("errors"):
                            errors += len(result["errors"])
                        keyword_result = sync_account_keyword_candidates(
                            session,
                            account,
                            scope_key=str(result.get("scope_key") or ""),
                            source_job_id=job_id,
                        )
                        landing_page_result = sync_account_landing_page_candidates(
                            session,
                            account,
                            scope_key=str(result.get("scope_key") or ""),
                            source_job_id=job_id,
                        )
                        negative_result = sync_account_negative_keyword_candidates(
                            session,
                            account,
                            scope_key=str(result.get("scope_key") or ""),
                            source_job_id=job_id,
                        )
                        keyword_saved += int(keyword_result.get("saved") or 0)
                        pages_saved += int(landing_page_result.get("saved") or 0)
                        negative_saved += int(negative_result.get("saved") or 0)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="daily_keyword_sync",
                            severity="manual_action_required",
                            extra={"days": days, "max_rows": max_rows},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep daily keyword pull moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="daily_keyword_sync",
                            severity="manual_action_required",
                            extra={"days": days, "max_rows": max_rows},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account/dataset errors were recorded as dashboard alerts. {keyword_saved} keyword rows, {pages_saved} landing-page rows, and {negative_saved} negative-keyword rows refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=2 * 60 * 60 * 1000)
def sync_google_ads_keyword_pull(
    mode: str = "recent",
    days: int = 60,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 5000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            mode = "all_time" if str(mode or "").strip().lower() == "all_time" else "recent"
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            keyword_saved = 0
            pages_saved = 0
            negative_saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_keyword_pull",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        if mode == "all_time":
                            result = sync_account_all_time_keyword_candidates(
                                session,
                                account,
                                max_rows=max_rows,
                                force=force,
                                source_job_id=job_id,
                            )
                            keyword_result = result.get("keyword_result") or {}
                            landing_page_pull = sync_account_landing_page_pull(
                                session,
                                account,
                                max_rows=max_rows,
                                mode="all_time",
                                force=force,
                                source_job_id=job_id,
                            )
                            landing_page_result = landing_page_pull.get("landing_page_result") or {}
                            negative_result = sync_account_negative_keyword_candidates(
                                session,
                                account,
                                source_job_id=job_id,
                            )
                        else:
                            result = sync_account_research_snapshots(
                                session,
                                account,
                                days=days,
                                max_rows=max_rows,
                                force=force,
                                source_job_id=job_id,
                            )
                            if result.get("errors"):
                                errors += len(result["errors"])
                            keyword_result = sync_account_keyword_candidates(
                                session,
                                account,
                                scope_key=str(result.get("scope_key") or ""),
                                source_job_id=job_id,
                            )
                            landing_page_result = sync_account_landing_page_candidates(
                                session,
                                account,
                                scope_key=str(result.get("scope_key") or ""),
                                source_job_id=job_id,
                            )
                            negative_result = sync_account_negative_keyword_candidates(
                                session,
                                account,
                                scope_key=str(result.get("scope_key") or ""),
                                source_job_id=job_id,
                            )
                        keyword_saved += int(keyword_result.get("saved") or 0)
                        pages_saved += int(landing_page_result.get("saved") or 0)
                        negative_saved += int(negative_result.get("saved") or 0)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="keyword_manual_pull",
                            severity="manual_action_required",
                            extra={"mode": mode, "days": days, "max_rows": max_rows},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep keyword pull moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="keyword_manual_pull",
                            severity="manual_action_required",
                            extra={"mode": mode, "days": days, "max_rows": max_rows},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account/dataset errors were recorded as dashboard alerts. {keyword_saved} keyword rows, {pages_saved} landing-page rows, and {negative_saved} negative-keyword rows refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=2 * 60 * 60 * 1000)
def sync_google_ads_landing_page_pull(
    mode: str = "recent",
    days: int = 60,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 5000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            mode = "all_time" if str(mode or "").strip().lower() == "all_time" else "recent"
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            pages_saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_landing_page_pull",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        result = sync_account_landing_page_pull(
                            session,
                            account,
                            days=days,
                            max_rows=max_rows,
                            mode=mode,
                            force=force,
                            source_job_id=job_id,
                        )
                        landing_page_result = result.get("landing_page_result") or {}
                        pages_saved += int(landing_page_result.get("saved") or 0)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="landing_page_manual_pull",
                            severity="manual_action_required",
                            extra={"mode": mode, "days": days, "max_rows": max_rows},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep landing-page pull moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="landing_page_manual_pull",
                            severity="manual_action_required",
                            extra={"mode": mode, "days": days, "max_rows": max_rows},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account/dataset errors were recorded as dashboard alerts. {pages_saved} landing-page rows refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_negative_keyword_candidates(
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            negative_saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    try:
                        result = sync_account_negative_keyword_candidates(
                            session,
                            account,
                            source_job_id=job_id,
                        )
                        negative_saved += int(result.get("saved") or 0)
                    except Exception as exc:  # noqa: BLE001 - keep negative review moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="negative_keyword_candidate_sync",
                            severity="manual_action_required",
                            extra={"source": "saved_search_terms_snapshots"},
                        )
                    finally:
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account errors were recorded as dashboard alerts. {negative_saved} negative-keyword rows refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_policy_disapproval_terms(
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            term_count = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_policy_disapproval_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        result = sync_account_policy_disapproval_terms(session, account, source_job_id=job_id)
                        term_count += int(result.get("term_count") or 0)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="policy_disapproval_term_sync",
                            severity="manual_action_required",
                            extra={"source": "google_ads_policy_summary"},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep remaining accounts moving.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="policy_disapproval_term_sync",
                            severity="manual_action_required",
                            extra={"source": "google_ads_policy_summary"},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account policy sync errors were recorded. {term_count} disapproved-substance terms refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_google_ads_brand_list_candidates(
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    limit: int = 250,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            query = select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            selected_ids = [int(item) for item in (account_ids or []) if item]
            if selected_ids:
                query = query.where(GoogleAdsAccount.id.in_(selected_ids))
            accounts = session.scalars(query.order_by(GoogleAdsAccount.name)).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            saved = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_brand_list_candidate_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        result = sync_account_brand_list_candidates(
                            session,
                            account,
                            source_job_id=job_id,
                            limit=limit,
                        )
                        saved += int(result.get("saved") or 0)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="brand_list_candidate_sync",
                            severity="manual_action_required",
                            extra={"source": "odoo_order_line_brand_attributes"},
                        )
                    except Exception as exc:  # noqa: BLE001 - keep remaining accounts moving.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="brand_list_candidate_sync",
                            severity="manual_action_required",
                            extra={"source": "odoo_order_line_brand_attributes"},
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                    current=processed,
                    error=f"{errors} account brand-list sync errors were recorded. {saved} brand candidates refreshed." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=3 * 60 * 60 * 1000)
def run_google_ads_automation_monitor(
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    force: bool = False,
    budget_only: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            runtime_block = primary_instance_required_result()
            if runtime_block is not None:
                mark_job_started(session, job_id, total=0)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded,
                    current=0,
                    error=str(runtime_block.get("message")),
                )
                return
            preferences = enabled_automation_preferences(session, account_ids=account_ids)
            if not mark_job_started(session, job_id, total=len(preferences)):
                return
            processed = 0
            errors = 0
            ga4_refresh: dict | None = None
            ga4_refresh_error: str | None = None
            results: list[dict] = []
            leased_preference_ids: set[int] = set()
            leased_api_preference_ids: set[int] = set()
            def release_monitor_leases(preferences_to_release: list[GoogleAdsAutomationPreference]) -> None:
                for leased_preference in preferences_to_release:
                    leased_account = leased_preference.account
                    if leased_preference.id in leased_preference_ids:
                        try:
                            release_automation_run_lease(session, leased_account, job_id=job_id)
                        except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                            session.rollback()
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=leased_account,
                                job_id=job_id,
                                context="automation_monitor_lease_release",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                    if leased_preference.id in leased_api_preference_ids:
                        try:
                            release_google_ads_api_account_lease(session, leased_account, job_id=job_id)
                        except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                            session.rollback()
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=leased_account,
                                job_id=job_id,
                                context="automation_monitor_api_lease_release",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
            try:
                runnable_preferences = []
                for preference in preferences:
                    if job_cancel_requested(session, job_id):
                        release_monitor_leases(preferences)
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    account = preference.account
                    quota_retry = google_ads_api_quota_retry_state(session, account)
                    if quota_retry:
                        result = google_ads_quota_blocked_result(account, quota_retry)
                        mark_preference_google_quota_blocked(session, preference, result)
                        results.append(result)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_automation_monitor",
                        )
                    except Exception as exc:  # noqa: BLE001 - account will be retried on the next scheduler pass.
                        session.rollback()
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="automation_monitor_api_lease_acquire",
                            severity="manual_action_required",
                            extra={"force": bool(force), "budget_only": bool(budget_only)},
                        )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    if not api_lease.get("acquired"):
                        results.append(
                            {
                                "account_id": account.id,
                                "customer_id": account.customer_id,
                                "account_name": account.name,
                                "mode": "skipped",
                                "steps": [
                                    {
                                        "name": "google_ads_api_account_lease",
                                        "status": api_lease.get("status") or "skipped",
                                        "reason": api_lease.get("reason"),
                                        "active_job_id": api_lease.get("active_job_id"),
                                        "active_purpose": api_lease.get("active_purpose"),
                                        "expires_at": api_lease.get("expires_at"),
                                        "retry_not_before": api_lease.get("retry_not_before"),
                                    }
                                ],
                            }
                        )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    try:
                        lease = acquire_automation_run_lease(session, account, job_id=job_id)
                    except Exception as exc:  # noqa: BLE001 - account will be retried on the next scheduler pass.
                        session.rollback()
                        errors += 1
                        try:
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        except Exception:  # noqa: BLE001 - stale leases expire and the original error is recorded below.
                            session.rollback()
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="automation_monitor_lease_acquire",
                            severity="manual_action_required",
                            extra={"force": bool(force), "budget_only": bool(budget_only)},
                        )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    if not lease.get("acquired"):
                        try:
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                            session.rollback()
                            errors += 1
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=account,
                                job_id=job_id,
                                context="automation_monitor_api_lease_release",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                        results.append(
                            {
                                "account_id": account.id,
                                "customer_id": account.customer_id,
                                "account_name": account.name,
                                "mode": "skipped",
                                "steps": [
                                    {
                                        "name": "automation_run_lease",
                                        "status": "skipped",
                                        "reason": lease.get("reason"),
                                        "active_job_id": lease.get("active_job_id"),
                                        "expires_at": lease.get("expires_at"),
                                    }
                                ],
                            }
                        )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    quota_retry = google_ads_api_quota_retry_state(session, account)
                    if quota_retry:
                        result = google_ads_quota_blocked_result(account, quota_retry)
                        mark_preference_google_quota_blocked(session, preference, result)
                        results.append(result)
                        try:
                            release_automation_run_lease(session, account, job_id=job_id)
                        except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                            session.rollback()
                            errors += 1
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=account,
                                job_id=job_id,
                                context="automation_monitor_lease_release",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                        try:
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                            session.rollback()
                            errors += 1
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=account,
                                job_id=job_id,
                                context="automation_monitor_api_lease_release",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                        continue
                    leased_preference_ids.add(preference.id)
                    leased_api_preference_ids.add(preference.id)
                    runnable_preferences.append(preference)
                preferences = runnable_preferences
                if preferences and not budget_only:
                    settings_map = get_sync_setting_map(session)
                    ga4_pre_refresh_enabled = parse_bool(settings_map.get("automation.ga4_pre_refresh_enabled", False))
                    if ga4_pre_refresh_enabled:
                        try:
                            ga4_days = max(7, min(max(int(item.daily_keyword_lookback_days or 60) for item in preferences), 365))
                            ga4_max_rows = max(1000, min(max(int(item.max_daily_api_rows or 10_000) for item in preferences), 5_000))
                            ga4_account_ids = [item.account_id for item in preferences]
                            ga4_result = sync_ga4_ecommerce_snapshots(
                                session,
                                mode="recent",
                                days=ga4_days,
                                max_rows=ga4_max_rows,
                                force=False,
                                source_job_id=job_id,
                                account_ids=ga4_account_ids,
                            )
                            ga4_refresh = {
                                "mode": ga4_result.get("mode"),
                                "scope_key": ga4_result.get("scope_key"),
                                "target_count": ga4_result.get("target_count"),
                                "dataset_count": ga4_result.get("dataset_count"),
                                "error_count": ga4_result.get("error_count"),
                                "landing_pages_imported": ga4_result.get("landing_pages_imported"),
                            }
                            if ga4_result.get("errors"):
                                ga4_refresh["errors"] = ga4_result.get("errors")[:20]
                        except Exception as exc:  # noqa: BLE001 - GA4 improves decisions, but should not block Ads monitoring.
                            session.rollback()
                            ga4_refresh_error = str(exc)[:2000]
                            record_google_ads_generic_error(
                                session,
                                exc,
                                job_id=job_id,
                                context="google_analytics_daily_collector",
                                severity="manual_action_required",
                                extra={"account_ids": account_ids or [], "force": bool(force)},
                            )
                    else:
                        ga4_refresh = {
                            "mode": "skipped",
                            "reason": "GA4 pre-refresh is disabled for the automation monitor; saved GA4 snapshots are reused by account planning.",
                        }
                for preference in preferences:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    account = preference.account
                    lease_acquired = preference.id in leased_preference_ids
                    api_lease_acquired = preference.id in leased_api_preference_ids
                    try:
                        def progress_checkpoint(step_name: str, payload: dict[str, Any]) -> None:
                            if job_id is None:
                                return
                            job = session.get(BackgroundJob, job_id)
                            if job is None:
                                return
                            job_payload = dict(job.payload or {})
                            checkpoints = list(job_payload.get("live_checkpoints") or [])
                            checkpoint = {**payload, "step": step_name}
                            checkpoints.append(checkpoint)
                            job_payload["live_checkpoint"] = checkpoint
                            job_payload["live_checkpoints"] = checkpoints[-40:]
                            job.payload = job_payload
                            session.commit()

                        result = run_account_automation_monitor(
                            session,
                            preference,
                            source_job_id=job_id,
                            force=force,
                            budget_only=budget_only,
                            progress_callback=progress_checkpoint,
                        )
                        if not budget_only:
                            steps = result.setdefault("steps", [])
                            if result.get("status") == "blocked_by_google_quota":
                                steps.append(
                                    {
                                        "name": "policy_disapproved_unapproved_substances",
                                        "status": "blocked_by_google_quota",
                                        "reason": result.get("steps", [{}])[0].get("reason") if result.get("steps") else "Google Ads API quota retry window is active.",
                                    }
                                )
                            else:
                                try:
                                    session.commit()
                                    policy_result = sync_account_policy_disapproval_terms(
                                        session,
                                        account,
                                        source_job_id=job_id,
                                    )
                                    session.commit()
                                    steps.append(
                                        {
                                            "name": "policy_disapproved_unapproved_substances",
                                            "status": "updated",
                                            "term_count": policy_result.get("term_count"),
                                            "negative_saved": policy_result.get("negative_saved"),
                                            "policy_saved": policy_result.get("policy_saved"),
                                        }
                                    )
                                except Exception as exc:  # noqa: BLE001 - policy sync should not block budget/campaign monitoring.
                                    session.rollback()
                                    steps.append(
                                        {
                                            "name": "policy_disapproved_unapproved_substances",
                                            "status": "failed",
                                            "error": str(exc)[:500],
                                        }
                                    )
                            if result.get("status") == "blocked_by_google_quota":
                                steps.append(
                                    {
                                        "name": "odoo_order_brand_list_candidates",
                                        "status": "blocked_by_google_quota",
                                        "reason": result.get("steps", [{}])[0].get("reason") if result.get("steps") else "Google Ads API quota retry window is active.",
                                    }
                                )
                            else:
                                try:
                                    session.commit()
                                    brand_result = sync_account_brand_list_candidates(
                                        session,
                                        account,
                                        source_job_id=job_id,
                                    )
                                    session.commit()
                                    steps.append(
                                        {
                                            "name": "odoo_order_brand_list_candidates",
                                            "status": brand_result.get("status") or "updated",
                                            "source_brand_count": brand_result.get("source_brand_count"),
                                            "saved": brand_result.get("saved"),
                                            "auto_selected": brand_result.get("auto_selected"),
                                            "needs_review": brand_result.get("needs_review"),
                                            "request_needed": brand_result.get("request_needed"),
                                            "retry_not_before": brand_result.get("retry_not_before"),
                                            "reason": brand_result.get("reason"),
                                        }
                                    )
                                except Exception as exc:  # noqa: BLE001 - brand candidate sync should not block monitoring.
                                    session.rollback()
                                    steps.append(
                                        {
                                            "name": "odoo_order_brand_list_candidates",
                                            "status": "failed",
                                            "error": str(exc)[:500],
                                        }
                                    )
                        results.append(result)
                    except GoogleAdsException as exc:
                        session.rollback()
                        preference = session.get(GoogleAdsAutomationPreference, preference.id)
                        quota_retry = google_ads_exception_quota_retry_state(session, account, exc)
                        if quota_retry and preference is not None:
                            result = google_ads_quota_blocked_result(account, quota_retry)
                            mark_preference_google_quota_blocked(session, preference, result)
                            results.append(result)
                        else:
                            errors += 1
                            if preference is not None:
                                preference.last_error = str(exc)[:2000]
                                preference.last_run_at = utcnow()
                                session.commit()
                            record_google_ads_api_error(
                                session,
                                exc,
                                account=account,
                                job_id=job_id,
                                context="automation_monitor",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                    except Exception as exc:  # noqa: BLE001 - keep other accounts moving.
                        session.rollback()
                        preference = session.get(GoogleAdsAutomationPreference, preference.id)
                        quota_retry = google_ads_generic_exception_quota_retry_state(session, account, exc)
                        if quota_retry and preference is not None:
                            result = google_ads_quota_blocked_result(account, quota_retry)
                            mark_preference_google_quota_blocked(session, preference, result)
                            results.append(result)
                        else:
                            errors += 1
                            if preference is not None:
                                preference.last_error = str(exc)[:2000]
                                preference.last_run_at = utcnow()
                                session.commit()
                            record_google_ads_generic_error(
                                session,
                                exc,
                                account=account,
                                job_id=job_id,
                                context="automation_monitor",
                                severity="manual_action_required",
                                extra={"force": bool(force), "budget_only": bool(budget_only)},
                            )
                    finally:
                        if lease_acquired:
                            try:
                                release_automation_run_lease(session, account, job_id=job_id)
                            except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                                session.rollback()
                                record_google_ads_generic_error(
                                    session,
                                    exc,
                                    account=account,
                                    job_id=job_id,
                                    context="automation_monitor_lease_release",
                                    severity="manual_action_required",
                                    extra={"force": bool(force), "budget_only": bool(budget_only)},
                                )
                        if api_lease_acquired:
                            try:
                                release_google_ads_api_account_lease(session, account, job_id=job_id)
                            except Exception as exc:  # noqa: BLE001 - stale leases expire, but record the cleanup failure.
                                session.rollback()
                                record_google_ads_generic_error(
                                    session,
                                    exc,
                                    account=account,
                                    job_id=job_id,
                                    context="automation_monitor_api_lease_release",
                                    severity="manual_action_required",
                                    extra={"force": bool(force), "budget_only": bool(budget_only)},
                                )
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                finish_payload = {
                    "processed": processed,
                    "errors": errors,
                    "ga4_ecommerce_refresh": ga4_refresh,
                    "ga4_ecommerce_error": ga4_refresh_error,
                    "accounts": [
                        {
                            "account_id": item.get("account_id"),
                            "customer_id": item.get("customer_id"),
                            "account_name": item.get("account_name"),
                            "mode": item.get("mode"),
                            "status": item.get("status"),
                            "steps": item.get("steps"),
                            "errors": item.get("errors"),
                        }
                        for item in results
                    ],
                }
                session.commit()
                session.close()
                with SessionLocal() as finish_session:
                    job = _job(finish_session, job_id)
                    if job is not None:
                        payload = dict(job.payload or {})
                        payload["result"] = finish_payload
                        job.payload = payload
                        finish_session.commit()
                    mark_job_finished(
                        finish_session,
                        job_id,
                        BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                        current=processed,
                        error=f"{errors} account automation monitor errors were recorded as dashboard alerts." if errors else None,
                    )
            except Exception as exc:  # noqa: BLE001
                release_monitor_leases(preferences)
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def optimize_google_ads_campaign(run_id: int, job_id: Optional[int] = None) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            run = session.get(CampaignOptimizationRun, run_id)
            if run is None:
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error="Optimization run not found.")
                return
            account = session.get(GoogleAdsAccount, run.account_id)
            if account is None:
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error="Optimization account not found.")
                return
            api_lease = None
            try:
                if job_cancel_requested(session, job_id):
                    run.status = "canceled"
                    run.finished_at = utcnow()
                    session.commit()
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                api_lease = acquire_google_ads_api_account_lease(
                    session,
                    account,
                    job_id=job_id,
                    purpose="google_ads_campaign_optimization",
                )
                if not api_lease.get("acquired"):
                    mark_job_finished(
                        session,
                        job_id,
                        BackgroundJobStatus.canceled,
                        current=0,
                        error=api_lease.get("reason") or api_lease.get("status") or "Google Ads API account lease was not acquired.",
                    )
                    return
                summary = run_campaign_optimization(session, run, source_job_id=job_id)
                update_job_progress(session, job_id, current=1, total=1)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not summary.get("errors") else BackgroundJobStatus.failed,
                    current=1,
                    error=f"{len(summary.get('errors') or [])} dataset errors were recorded as dashboard alerts." if summary.get("errors") else None,
                )
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                run = session.get(CampaignOptimizationRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(exc)
                    run.finished_at = utcnow()
                    session.commit()
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error=str(exc))
                raise
            finally:
                if api_lease and api_lease.get("acquired"):
                    release_google_ads_api_account_lease(session, account, job_id=job_id)


@dramatiq.actor(max_retries=0, time_limit=30 * 60 * 1000)
def generate_google_ads_assets(
    account_id: int,
    requested_by_id: Optional[int] = None,
    job_id: Optional[int] = None,
    include_sitelinks: bool = True,
    include_structured_snippets: bool = True,
    include_pmax_search_themes: bool = True,
    include_promotions: bool = True,
) -> None:
    with SessionLocal() as session:
        if not mark_job_started(session, job_id, total=1):
            return
        try:
            if job_cancel_requested(session, job_id):
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                return
            result = generate_account_assets(
                session,
                account_id=int(account_id),
                created_by_id=requested_by_id,
                include_sitelinks=bool(include_sitelinks),
                include_structured_snippets=bool(include_structured_snippets),
                include_pmax_search_themes=bool(include_pmax_search_themes),
                include_promotions=bool(include_promotions),
            )
            job = _job(session, job_id)
            if job is not None:
                payload = dict(job.payload or {})
                payload["result"] = result
                job.payload = payload
                session.commit()
            update_job_progress(session, job_id, current=1, total=1)
            mark_job_finished(session, job_id, BackgroundJobStatus.succeeded, current=1)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            account = session.get(GoogleAdsAccount, int(account_id))
            record_google_ads_generic_error(
                session,
                exc,
                account=account,
                job_id=job_id,
                context="asset_generation",
                severity="manual_action_required",
                extra={"account_id": int(account_id)},
            )
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_odoo_store_sales(
    store_ids: Optional[list[int]] = None,
    hours: int = 12,
    job_id: Optional[int] = None,
) -> None:
    with SessionLocal() as session:
        query = select(OdooStore).where(OdooStore.is_active.is_(True))
        selected_ids = [int(item) for item in (store_ids or []) if item]
        if selected_ids:
            query = query.where(OdooStore.id.in_(selected_ids))
        stores = session.scalars(query.order_by(OdooStore.name)).all()
        if not mark_job_started(session, job_id, total=len(stores)):
            return
        processed = 0
        errors = 0
        try:
            for store in stores:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                    return
                try:
                    sync_store_websites(session, store)
                    sync_store_confirmed_orders(session, store, hours=hours)
                    sync_store_product_page_feeds(session, store, days=max(7, int(hours or 12) // 24 or 1))
                    publications = session.scalars(
                        select(GoogleAdsCustomerMatchPublication).where(
                            GoogleAdsCustomerMatchPublication.store_id == store.id,
                            GoogleAdsCustomerMatchPublication.website_id != 0,
                            GoogleAdsCustomerMatchPublication.customer_match_policy_accepted.is_(True),
                        )
                    ).all()
                    for publication in publications:
                        sync_customer_match_members_for_publication(session, publication, lookback_days=540)
                        push_customer_match_outbox_for_publication(session, publication, max_batches=1)
                except Exception as exc:  # noqa: BLE001 - keep other stores syncing.
                    errors += 1
                    store.last_sync_error = str(exc)
                    session.commit()
                finally:
                    processed += 1
                    update_job_progress(session, job_id, current=processed)
            try:
                refresh_standard_cost_dashboard_snapshots(session)
            except Exception:  # noqa: BLE001 - Odoo data is already persisted.
                errors += 1
            mark_job_finished(
                session,
                job_id,
                BackgroundJobStatus.succeeded,
                current=processed,
                error=f"{errors} store/snapshot errors were skipped." if errors else None,
            )
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_customer_match_publication(
    publication_id: int,
    lookback_days: int = 540,
    job_id: Optional[int] = None,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                publication = session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
                if publication is None:
                    mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error="Publication not found.")
                    return
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = sync_customer_match_members_for_publication(
                    session,
                    publication,
                    lookback_days=min(max(int(lookback_days or 540), 1), 540),
                )
                api_result = {}
                if result.get("status") == "done":
                    api_result = push_customer_match_outbox_for_publication(session, publication, max_batches=1)
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = result
                    payload["api_result"] = api_result
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1, total=1)
                succeeded = result.get("status") == "done" and api_result.get("status") in {
                    "done",
                    "partial",
                    "disabled",
                    "quota_deferred",
                }
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if succeeded else BackgroundJobStatus.failed,
                    current=1,
                    error=None
                    if succeeded
                    else str(api_result.get("reason") or result.get("reason") or api_result.get("status") or result.get("status")),
                )
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                publication = session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
                if publication is not None:
                    publication.last_error = str(exc)
                    session.commit()
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=0, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_conversion_goal_snapshots(job_id: Optional[int] = None) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            accounts = session.scalars(
                select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))
            ).all()
            if not mark_job_started(session, job_id, total=len(accounts)):
                return
            processed = 0
            errors = 0
            try:
                for account in accounts:
                    if job_cancel_requested(session, job_id):
                        mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                        return
                    api_lease = None
                    try:
                        api_lease = acquire_google_ads_api_account_lease(
                            session,
                            account,
                            job_id=job_id,
                            purpose="google_ads_conversion_goal_sync",
                        )
                        if not api_lease.get("acquired"):
                            continue
                        sync_account_conversion_goals(session, account, source_job_id=job_id)
                    except GoogleAdsException as exc:
                        errors += 1
                        record_google_ads_api_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="conversion_goal_sync",
                        )
                    except Exception as exc:  # noqa: BLE001 - keep bulk sync moving account by account.
                        errors += 1
                        record_google_ads_generic_error(
                            session,
                            exc,
                            account=account,
                            job_id=job_id,
                            context="conversion_goal_sync",
                        )
                    finally:
                        if api_lease and api_lease.get("acquired"):
                            release_google_ads_api_account_lease(session, account, job_id=job_id)
                        processed += 1
                        update_job_progress(session, job_id, current=processed)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded,
                    current=processed,
                    error=f"{errors} account errors were skipped." if errors else None,
                )
            except Exception as exc:  # noqa: BLE001
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def sync_odoo_product_page_feeds(
    days: int = 7,
    job_id: Optional[int] = None,
    store_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
    website_ids: Optional[list[int]] = None,
) -> None:
    with SessionLocal() as session:
        mapping_count = len(
            session.scalars(
                select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
            ).all()
        )
        if not mark_job_started(session, job_id, total=mapping_count):
            return
        try:
            if job_cancel_requested(session, job_id):
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                return
            result = sync_mapped_product_page_feeds(
                session,
                days=max(int(days or 7), 1),
                store_ids=store_ids,
                account_ids=account_ids,
                website_ids=website_ids,
            )
            processed = int(result.get("mappings") or 0)
            update_job_progress(session, job_id, current=processed, total=processed)
            errors = result.get("errors") or []
            mark_job_finished(
                session,
                job_id,
                BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                current=processed,
                error="\n".join(
                    str(item.get("error") or item) if isinstance(item, dict) else str(item)
                    for item in errors[:5]
                )
                if errors
                else None,
            )
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=60 * 60 * 1000)
def publish_google_ads_page_feeds(
    job_id: Optional[int] = None,
    store_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
    website_ids: Optional[list[int]] = None,
    validate_only: Optional[bool] = None,
    max_urls: int = 100,
    create_dsa_criteria: bool = False,
) -> None:
    with SessionLocal() as session:
        query = select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
        selected_store_ids = [int(item) for item in (store_ids or []) if item]
        selected_account_ids = [int(item) for item in (account_ids or []) if item]
        selected_website_ids = [int(item) for item in (website_ids or []) if item]
        if selected_store_ids:
            query = query.where(OdooStoreGoogleAdsMapping.store_id.in_(selected_store_ids))
        if selected_account_ids:
            query = query.where(OdooStoreGoogleAdsMapping.account_id.in_(selected_account_ids))
        if selected_website_ids:
            query = query.where(OdooStoreGoogleAdsMapping.website_id.in_(selected_website_ids))
        mappings = session.scalars(query).all()
        mapping_count = len(mappings)
        if not mark_job_started(session, job_id, total=mapping_count):
            return
        account_by_id = {
            int(mapping.account_id): mapping.account
            for mapping in mappings
            if mapping.account is not None and mapping.account_id
        }
        api_leases: dict[int, dict[str, Any]] = {}
        try:
            if job_cancel_requested(session, job_id):
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                return
            errors: list[Any] = []
            leased_account_ids: list[int] = []
            for account_id, account in account_by_id.items():
                lease = acquire_google_ads_api_account_lease(
                    session,
                    account,
                    job_id=job_id,
                    purpose="google_ads_page_feed_publish",
                )
                if lease.get("acquired"):
                    api_leases[account_id] = lease
                    leased_account_ids.append(account_id)
                else:
                    errors.append(
                        {
                            "account_id": account_id,
                            "customer_id": account.customer_id,
                            "error": lease.get("reason") or lease.get("status") or "Google Ads API account lease was not acquired.",
                            "status": lease.get("status"),
                        }
                    )
            result = publish_page_feeds_for_mappings(
                session,
                store_ids=selected_store_ids or None,
                account_ids=leased_account_ids or [-1],
                website_ids=selected_website_ids or None,
                validate_only=validate_only,
                max_urls=max(1, min(int(max_urls or 100), 500)),
                create_dsa_criteria=bool(create_dsa_criteria),
                job_id=job_id,
            )
            processed = int(result.get("mappings") or 0)
            update_job_progress(session, job_id, current=processed, total=processed)
            errors.extend(result.get("errors") or [])
            mark_job_finished(
                session,
                job_id,
                BackgroundJobStatus.succeeded if not errors else BackgroundJobStatus.failed,
                current=processed,
                error="\n".join(str(item.get("error") or item) for item in errors[:5]) if errors else None,
            )
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
            raise
        finally:
            for account_id, lease in api_leases.items():
                if lease.get("acquired"):
                    account = account_by_id.get(account_id)
                    if account is not None:
                        release_google_ads_api_account_lease(session, account, job_id=job_id)


@dramatiq.actor(max_retries=0, time_limit=15 * 60 * 1000)
def sync_odoo_store_websites(
    store_ids: Optional[list[int]] = None,
    job_id: Optional[int] = None,
) -> None:
    with SessionLocal() as session:
        query = select(OdooStore).where(OdooStore.is_active.is_(True))
        selected_ids = [int(item) for item in (store_ids or []) if item]
        if selected_ids:
            query = query.where(OdooStore.id.in_(selected_ids))
        stores = session.scalars(query.order_by(OdooStore.name)).all()
        if not mark_job_started(session, job_id, total=len(stores)):
            return
        processed = 0
        errors = 0
        try:
            for store in stores:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=processed)
                    return
                try:
                    sync_store_websites(session, store)
                    store.last_sync_error = None
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - keep website discovery moving across stores.
                    errors += 1
                    store.last_sync_error = str(exc)
                    session.commit()
                finally:
                    processed += 1
                    update_job_progress(session, job_id, current=processed)
            mark_job_finished(
                session,
                job_id,
                BackgroundJobStatus.succeeded,
                current=processed,
                error=f"{errors} store website errors were skipped." if errors else None,
            )
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, current=processed, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=15 * 60 * 1000)
def sync_razorpay_receipts(days: int = 30, job_id: Optional[int] = None) -> None:
    with SessionLocal() as session:
        if not mark_job_started(session, job_id, total=1):
            return
        try:
            if job_cancel_requested(session, job_id):
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                return
            sync_razorpay_daily_receipts(session, days)
            update_job_progress(session, job_id, current=1)
            try:
                refresh_standard_cost_dashboard_snapshots(session)
            except Exception:  # noqa: BLE001 - snapshots are a cache, receipt data is already persisted.
                pass
            mark_job_finished(session, job_id, BackgroundJobStatus.succeeded, current=1)
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=30 * 60 * 1000)
def discover_google_ads_connection_accounts(connection_id: int, job_id: Optional[int] = None) -> None:
    with SessionLocal() as session:
        if not mark_job_started(session, job_id, total=1):
            return
        try:
            if job_cancel_requested(session, job_id):
                mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                return
            result = discover_accounts_for_connection(session, connection_id)
            for error in result.get("errors", [])[-20:]:
                record_google_ads_generic_error(
                    session,
                    RuntimeError(str(error)),
                    job_id=job_id,
                    context="account_discovery",
                    extra={"connection_id": connection_id},
                )
            update_job_progress(session, job_id, current=1)
            mark_job_finished(
                session,
                job_id,
                BackgroundJobStatus.succeeded,
                current=1,
                error="\n".join(result.get("errors", [])[-3:]) if result.get("errors") else None,
            )
        except Exception as exc:  # noqa: BLE001
            mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
            raise


@dramatiq.actor(max_retries=0, time_limit=30 * 60 * 1000)
def discover_google_analytics_connection(connection_id: int, job_id: Optional[int] = None) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = discover_analytics_connection(session, connection_id)
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = result
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1)
                mark_job_finished(session, job_id, BackgroundJobStatus.succeeded, current=1)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                connection = session.get(GoogleAnalyticsConnection, int(connection_id))
                if connection is not None:
                    connection.last_discovery_error = str(exc)[:2000]
                    session.commit()
                record_google_ads_generic_error(
                    session,
                    exc,
                    job_id=job_id,
                    context="google_analytics_discovery",
                    severity="manual_action_required",
                    extra={"connection_id": int(connection_id)},
                )
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=3 * 60 * 60 * 1000)
def sync_google_analytics_ecommerce(
    mode: str = "recent",
    days: int = 60,
    job_id: Optional[int] = None,
    connection_ids: Optional[list[int]] = None,
    property_ids: Optional[list[int]] = None,
    max_rows: int = 10_000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = sync_ga4_ecommerce_snapshots(
                    session,
                    mode=mode,
                    days=days,
                    max_rows=max_rows,
                    force=force,
                    source_job_id=job_id,
                    connection_ids=connection_ids,
                    property_ids=property_ids,
                )
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = {
                        "mode": result.get("mode"),
                        "scope_key": result.get("scope_key"),
                        "target_count": result.get("target_count"),
                        "dataset_count": result.get("dataset_count"),
                        "error_count": result.get("error_count"),
                        "landing_pages_imported": result.get("landing_pages_imported"),
                        "keywords_imported": result.get("keywords_imported"),
                        "search_terms_imported": result.get("search_terms_imported"),
                    }
                    if result.get("errors"):
                        payload["result_errors"] = result.get("errors")[:20]
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1, total=1)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not result.get("error_count") else BackgroundJobStatus.failed,
                    current=1,
                    error=f"{result.get('error_count')} GA4 dataset errors were recorded." if result.get("error_count") else None,
                )
            except Exception as exc:  # noqa: BLE001
                record_google_ads_generic_error(
                    session,
                    exc,
                    job_id=job_id,
                    context="google_analytics_ecommerce_sync",
                    severity="manual_action_required",
                    extra={
                        "mode": mode,
                        "days": days,
                        "max_rows": max_rows,
                        "connection_ids": connection_ids or [],
                        "property_ids": property_ids or [],
                    },
                )
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=2 * 60 * 60 * 1000)
def sync_google_analytics_search_terms(
    mode: str = "recent",
    days: int = 60,
    job_id: Optional[int] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 10_000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = sync_ga4_search_term_snapshots(
                    session,
                    mode=mode,
                    days=days,
                    max_rows=max_rows,
                    force=force,
                    source_job_id=job_id,
                    account_ids=account_ids,
                )
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = {
                        "mode": result.get("mode"),
                        "scope_key": result.get("scope_key"),
                        "target_count": result.get("target_count"),
                        "dataset_count": result.get("dataset_count"),
                        "error_count": result.get("error_count"),
                        "search_terms_imported": result.get("search_terms_imported"),
                    }
                    if result.get("errors"):
                        payload["result_errors"] = result.get("errors")[:20]
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1, total=1)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not result.get("error_count") else BackgroundJobStatus.failed,
                    current=1,
                    error=f"{result.get('error_count')} GA4 search-term dataset errors were recorded." if result.get("error_count") else None,
                )
            except Exception as exc:  # noqa: BLE001
                record_google_ads_generic_error(
                    session,
                    exc,
                    job_id=job_id,
                    context="google_analytics_search_terms_sync",
                    severity="manual_action_required",
                    extra={
                        "mode": mode,
                        "days": days,
                        "max_rows": max_rows,
                        "account_ids": account_ids or [],
                    },
                )
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=30 * 60 * 1000)
def discover_google_search_console_connection(connection_id: int, job_id: Optional[int] = None) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = discover_search_console_connection(session, connection_id)
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = result
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1)
                mark_job_finished(session, job_id, BackgroundJobStatus.succeeded, current=1)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                connection = session.get(GoogleSearchConsoleConnection, int(connection_id))
                if connection is not None:
                    connection.last_discovery_error = str(exc)[:2000]
                    session.commit()
                record_google_ads_generic_error(
                    session,
                    exc,
                    job_id=job_id,
                    context="google_search_console_discovery",
                    severity="manual_action_required",
                    extra={"connection_id": int(connection_id)},
                )
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
                raise


@dramatiq.actor(max_retries=0, time_limit=2 * 60 * 60 * 1000)
def sync_google_search_console_search_analytics(
    mode: str = "recent",
    days: int = 28,
    job_id: Optional[int] = None,
    connection_ids: Optional[list[int]] = None,
    site_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
    max_rows: int = 25_000,
    force: bool = False,
) -> None:
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        with SessionLocal() as session:
            if not mark_job_started(session, job_id, total=1):
                return
            try:
                if job_cancel_requested(session, job_id):
                    mark_job_finished(session, job_id, BackgroundJobStatus.canceled, current=0)
                    return
                result = sync_search_console_search_analytics(
                    session,
                    mode=mode,
                    days=days,
                    max_rows=max_rows,
                    force=force,
                    source_job_id=job_id,
                    connection_ids=connection_ids,
                    site_ids=site_ids,
                    account_ids=account_ids,
                )
                job = _job(session, job_id)
                if job is not None:
                    payload = dict(job.payload or {})
                    payload["result"] = {
                        "mode": result.get("mode"),
                        "scope_key": result.get("scope_key"),
                        "target_count": result.get("target_count"),
                        "dataset_count": result.get("dataset_count"),
                        "snapshot_count": result.get("snapshot_count"),
                        "error_count": result.get("error_count"),
                        "query_candidates_imported": result.get("query_candidates_imported"),
                    }
                    if result.get("errors"):
                        payload["result_errors"] = result.get("errors")[:20]
                    job.payload = payload
                    session.commit()
                update_job_progress(session, job_id, current=1, total=1)
                mark_job_finished(
                    session,
                    job_id,
                    BackgroundJobStatus.succeeded if not result.get("error_count") else BackgroundJobStatus.failed,
                    current=1,
                    error=f"{result.get('error_count')} Search Console dataset errors were recorded."
                    if result.get("error_count")
                    else None,
                )
            except Exception as exc:  # noqa: BLE001
                record_google_ads_generic_error(
                    session,
                    exc,
                    job_id=job_id,
                    context="google_search_console_search_analytics_sync",
                    severity="manual_action_required",
                    extra={
                        "mode": mode,
                        "days": days,
                        "max_rows": max_rows,
                        "connection_ids": connection_ids or [],
                        "site_ids": site_ids or [],
                        "account_ids": account_ids or [],
                    },
                )
                mark_job_finished(session, job_id, BackgroundJobStatus.failed, error=str(exc))
                raise
