#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, select

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import AdDraft, BackgroundJob, BackgroundJobStatus, GoogleAdsAccount, OdooStoreGoogleAdsMapping
from app.runtime_role import primary_instance_required_result
from app.services.google_ads_automation import (
    active_google_ads_quota_retry_state,
    budget_guard_due_now,
    clamp_int,
    enabled_automation_preferences,
    peak_budget_due_now,
    preference_due_now,
    refresh_low_traffic_schedule,
    refresh_peak_budget_decision,
)
from app.services.google_ads_account_red_flags import account_api_red_flag
from app.tasks import (
    SessionLocal,
    publish_google_ads_page_feeds,
    run_google_ads_automation_monitor,
    sync_google_ads_daily_keywords,
    sync_google_ads_policy_disapproval_terms,
    sync_google_ads_universal_garbage_negatives,
)

STALE_QUEUED_AUTOMATION_JOB_AFTER = timedelta(hours=2)
STALE_RUNNING_AUTOMATION_JOB_AFTER = timedelta(hours=3)
DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK = 5
BUDGET_ONLY_REASONS = {"budget_guard", "peak_budget"}
DAILY_KEYWORD_SYNC_INTERVAL = timedelta(hours=24)
DAILY_KEYWORD_SYNC_DAYS_SETTING = "automation.daily_keyword_sync_days"
DAILY_KEYWORD_SYNC_MAX_ROWS_SETTING = "automation.daily_keyword_sync_max_rows"
DAILY_KEYWORD_SYNC_DEFAULT_DAYS = 60
DAILY_KEYWORD_SYNC_DEFAULT_MAX_ROWS = 10000
DAILY_PAGE_FEED_PUBLISH_INTERVAL = timedelta(hours=6)
DAILY_PAGE_FEED_PUBLISH_MAX_URLS_SETTING = "automation.page_feed_publish_max_urls_per_account"
DAILY_PAGE_FEED_PUBLISH_MAX_ACCOUNTS_SETTING = "automation.page_feed_publish_max_accounts_per_run"
DAILY_PAGE_FEED_PUBLISH_DEFAULT_MAX_URLS = 1000
DAILY_PAGE_FEED_PUBLISH_DEFAULT_MAX_ACCOUNTS = 30
UNIVERSAL_GARBAGE_NEGATIVE_SYNC_INTERVAL_SETTING = "automation.universal_garbage_negative_sync_interval_hours"
UNIVERSAL_GARBAGE_NEGATIVE_SYNC_MAX_ACCOUNTS_SETTING = "automation.universal_garbage_negative_sync_max_accounts"
UNIVERSAL_GARBAGE_NEGATIVE_SYNC_DEFAULT_INTERVAL = 2
UNIVERSAL_GARBAGE_NEGATIVE_SYNC_DEFAULT_MAX_ACCOUNTS = 100
POLICY_DISAPPROVAL_SYNC_INTERVAL_SETTING = "automation.policy_disapproval_sync_interval_hours"
POLICY_DISAPPROVAL_SYNC_MAX_ACCOUNTS_SETTING = "automation.policy_disapproval_sync_max_accounts"
POLICY_DISAPPROVAL_SYNC_DEFAULT_INTERVAL = 12
POLICY_DISAPPROVAL_SYNC_DEFAULT_MAX_ACCOUNTS = 100


def customer_id_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace("\\n", "\n").replace(",", "\n").splitlines()
    return {"".join(ch for ch in item if ch.isdigit()) for item in raw_items if "".join(ch for ch in item if ch.isdigit())}


def scheduler_tier(account: GoogleAdsAccount, primary_ids: set[str], secondary_ids: set[str]) -> int:
    customer_id = "".join(ch for ch in str(account.customer_id or "") if ch.isdigit())
    if customer_id in primary_ids:
        return 0
    if customer_id in secondary_ids:
        return 1
    return 2


def automation_first_run_missing(session, account_id: int) -> bool:
    active_draft_count = session.scalar(
        select(func.count(AdDraft.id)).where(
            AdDraft.account_id == int(account_id),
            AdDraft.status.notin_(["retired", "removed"]),
        )
    )
    return int(active_draft_count or 0) == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue Google Ads automation only when account low-traffic schedule is due.")
    parser.add_argument("--account-id", type=int, action="append", default=[], help="Database account id to consider.")
    parser.add_argument("--customer-id", action="append", default=[], help="Google Ads customer id to consider.")
    parser.add_argument("--force", action="store_true", help="Force Google snapshot refresh inside the monitor job.")
    parser.add_argument("--ignore-schedule", action="store_true", help="Queue selected enabled accounts immediately.")
    parser.add_argument("--recompute-schedule", action="store_true", help="Refresh the low-traffic runtime decision from saved time segments before checking due accounts.")
    parser.add_argument("--fetch-timezone", action="store_true", help="Use one Google Ads customer query to refresh the account timezone while recomputing.")
    parser.add_argument("--max-accounts", type=int, default=None, help="Maximum due accounts to queue in this scheduler tick.")
    parser.add_argument("--skip-daily-keywords", action="store_true", help="Do not queue the recurring Google Ads keyword/negative/landing-page bank sync.")
    return parser.parse_args()


def _active_job(session, job_type: str) -> BackgroundJob | None:
    return session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.job_type == job_type,
            BackgroundJob.status.in_(
                [
                    BackgroundJobStatus.queued,
                    BackgroundJobStatus.running,
                    BackgroundJobStatus.cancel_requested,
                ]
            ),
        )
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
        .limit(1)
    )


def _queue_daily_keyword_sync_if_due(session, now: datetime) -> None:
    if _active_job(session, "google_ads_keyword_daily_sync") is not None:
        return
    latest_finished = session.scalar(
        select(BackgroundJob.finished_at)
        .where(
            BackgroundJob.job_type == "google_ads_keyword_daily_sync",
            BackgroundJob.status == BackgroundJobStatus.succeeded,
            BackgroundJob.finished_at.is_not(None),
        )
        .order_by(BackgroundJob.finished_at.desc())
        .limit(1)
    )
    if latest_finished is not None and latest_finished.astimezone(timezone.utc) > now - DAILY_KEYWORD_SYNC_INTERVAL:
        return
    scheduler_settings = get_sync_setting_map(session)
    days = clamp_int(
        scheduler_settings.get(DAILY_KEYWORD_SYNC_DAYS_SETTING),
        DAILY_KEYWORD_SYNC_DEFAULT_DAYS,
        1,
        365,
    )
    max_rows = clamp_int(
        scheduler_settings.get(DAILY_KEYWORD_SYNC_MAX_ROWS_SETTING),
        DAILY_KEYWORD_SYNC_DEFAULT_MAX_ROWS,
        50,
        50_000,
    )
    job = BackgroundJob(
        job_type="google_ads_keyword_daily_sync",
        label="Daily Google Ads keyword, negative, and landing-page bank sync",
        requested_by_id=None,
        payload={
            "account_ids": None,
            "days": days,
            "max_rows": max_rows,
            "force": False,
            "queued_by": "scripts/queue_automation_monitor.py",
            "reason": "recurring_daily_keyword_negative_landing_page_sync",
        },
        status=BackgroundJobStatus.queued,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        message = sync_google_ads_daily_keywords.send(days, job.id, None, max_rows, False)
        job.message_id = str(message.message_id)
        session.commit()
        print(f"Queued daily Google Ads keyword sync job #{job.id}")
    except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
        job.status = BackgroundJobStatus.failed
        job.error = str(exc)
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise


def _queue_daily_page_feed_publish_if_due(session, now: datetime, scheduler_settings: dict) -> None:
    if _active_job(session, "google_ads_page_feed_publish") is not None:
        return
    latest_finished = session.scalar(
        select(BackgroundJob.finished_at)
        .where(
            BackgroundJob.job_type == "google_ads_page_feed_publish",
            BackgroundJob.status == BackgroundJobStatus.succeeded,
            BackgroundJob.finished_at.is_not(None),
        )
        .order_by(BackgroundJob.finished_at.desc())
        .limit(1)
    )
    if latest_finished is not None and latest_finished.astimezone(timezone.utc) > now - DAILY_PAGE_FEED_PUBLISH_INTERVAL:
        return

    primary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_primary_customer_ids"))
    secondary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_secondary_customer_ids"))
    max_accounts = clamp_int(
        scheduler_settings.get(DAILY_PAGE_FEED_PUBLISH_MAX_ACCOUNTS_SETTING),
        DAILY_PAGE_FEED_PUBLISH_DEFAULT_MAX_ACCOUNTS,
        1,
        100,
    )
    max_urls = clamp_int(
        scheduler_settings.get(DAILY_PAGE_FEED_PUBLISH_MAX_URLS_SETTING),
        DAILY_PAGE_FEED_PUBLISH_DEFAULT_MAX_URLS,
        1,
        5000,
    )
    mapped_account_ids = set(
        session.scalars(
            select(OdooStoreGoogleAdsMapping.account_id).where(
                OdooStoreGoogleAdsMapping.is_active.is_(True),
                OdooStoreGoogleAdsMapping.account_id.is_not(None),
            )
        ).all()
    )
    candidates = []
    for preference in enabled_automation_preferences(session):
        account = preference.account
        if account.id not in mapped_account_ids:
            continue
        if account_api_red_flag(session, account) is not None:
            continue
        if active_google_ads_quota_retry_state(session, account, now=now):
            continue
        tier = scheduler_tier(account, primary_customer_ids, secondary_customer_ids)
        candidates.append((tier, preference.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc), account.name, account.id))
    account_ids = [account_id for _tier, _last_run, _name, account_id in sorted(candidates)[:max_accounts]]
    if not account_ids:
        return

    job = BackgroundJob(
        job_type="google_ads_page_feed_publish",
        label=f"Daily Google Ads page-feed URL publish: {len(account_ids)} account(s)",
        requested_by_id=None,
        payload={
            "account_ids": account_ids,
            "max_urls": max_urls,
            "validate_only": None,
            "create_dsa_criteria": False,
            "queued_by": "scripts/queue_automation_monitor.py",
            "reason": "recurring_daily_page_feed_url_publish",
            "priority": {
                "primary_customer_ids": sorted(primary_customer_ids),
                "secondary_customer_ids": sorted(secondary_customer_ids),
            },
        },
        status=BackgroundJobStatus.queued,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        message = publish_google_ads_page_feeds.send(job.id, None, account_ids, None, None, max_urls, False)
        job.message_id = str(message.message_id)
        session.commit()
        print(f"Queued daily Google Ads page-feed publish job #{job.id} for account ids {account_ids}")
    except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
        job.status = BackgroundJobStatus.failed
        job.error = str(exc)
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise


def _queue_universal_garbage_negative_sync_if_due(session, now: datetime, scheduler_settings: dict) -> None:
    if _active_job(session, "google_ads_universal_garbage_negative_sync") is not None:
        return
    interval_hours = clamp_int(
        scheduler_settings.get(UNIVERSAL_GARBAGE_NEGATIVE_SYNC_INTERVAL_SETTING),
        UNIVERSAL_GARBAGE_NEGATIVE_SYNC_DEFAULT_INTERVAL,
        1,
        24,
    )
    latest_finished = session.scalar(
        select(BackgroundJob.finished_at)
        .where(
            BackgroundJob.job_type == "google_ads_universal_garbage_negative_sync",
            BackgroundJob.status == BackgroundJobStatus.succeeded,
            BackgroundJob.finished_at.is_not(None),
        )
        .order_by(BackgroundJob.finished_at.desc())
        .limit(1)
    )
    if latest_finished is not None and latest_finished.astimezone(timezone.utc) > now - timedelta(hours=interval_hours):
        return

    primary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_primary_customer_ids"))
    secondary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_secondary_customer_ids"))
    max_accounts = clamp_int(
        scheduler_settings.get(UNIVERSAL_GARBAGE_NEGATIVE_SYNC_MAX_ACCOUNTS_SETTING),
        UNIVERSAL_GARBAGE_NEGATIVE_SYNC_DEFAULT_MAX_ACCOUNTS,
        1,
        500,
    )
    candidates = []
    for preference in enabled_automation_preferences(session):
        account = preference.account
        if account_api_red_flag(session, account) is not None:
            continue
        tier = scheduler_tier(account, primary_customer_ids, secondary_customer_ids)
        quota_retry = active_google_ads_quota_retry_state(session, account, now=now)
        candidates.append(
            (
                99 if quota_retry else tier,
                preference.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
                account.name or "",
                account.id,
            )
        )
    account_ids = [account_id for _tier, _last_run, _name, account_id in sorted(candidates)[:max_accounts]]
    if not account_ids:
        return

    job = BackgroundJob(
        job_type="google_ads_universal_garbage_negative_sync",
        label=f"Universal garbage negative sync: {len(account_ids)} account(s)",
        requested_by_id=None,
        payload={
            "account_ids": account_ids,
            "validate_only": False,
            "queued_by": "scripts/queue_automation_monitor.py",
            "reason": "recurring_universal_garbage_negative_protection",
            "interval_hours": interval_hours,
            "priority": {
                "primary_customer_ids": sorted(primary_customer_ids),
                "secondary_customer_ids": sorted(secondary_customer_ids),
            },
        },
        status=BackgroundJobStatus.queued,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        message = sync_google_ads_universal_garbage_negatives.send(job.id, account_ids, False)
        job.message_id = str(message.message_id)
        session.commit()
        print(f"Queued universal garbage negative sync job #{job.id} for account ids {account_ids}")
    except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
        job.status = BackgroundJobStatus.failed
        job.error = str(exc)
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise


def _queue_policy_disapproval_sync_if_due(session, now: datetime, scheduler_settings: dict) -> None:
    if _active_job(session, "google_ads_policy_disapproval_sync") is not None:
        return
    interval_hours = clamp_int(
        scheduler_settings.get(POLICY_DISAPPROVAL_SYNC_INTERVAL_SETTING),
        POLICY_DISAPPROVAL_SYNC_DEFAULT_INTERVAL,
        1,
        72,
    )
    latest_finished = session.scalar(
        select(BackgroundJob.finished_at)
        .where(
            BackgroundJob.job_type == "google_ads_policy_disapproval_sync",
            BackgroundJob.status == BackgroundJobStatus.succeeded,
            BackgroundJob.finished_at.is_not(None),
        )
        .order_by(BackgroundJob.finished_at.desc())
        .limit(1)
    )
    if latest_finished is not None and latest_finished.astimezone(timezone.utc) > now - timedelta(hours=interval_hours):
        return

    primary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_primary_customer_ids"))
    secondary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_secondary_customer_ids"))
    max_accounts = clamp_int(
        scheduler_settings.get(POLICY_DISAPPROVAL_SYNC_MAX_ACCOUNTS_SETTING),
        POLICY_DISAPPROVAL_SYNC_DEFAULT_MAX_ACCOUNTS,
        1,
        500,
    )
    candidates = []
    for preference in enabled_automation_preferences(session):
        account = preference.account
        if account_api_red_flag(session, account) is not None:
            continue
        tier = scheduler_tier(account, primary_customer_ids, secondary_customer_ids)
        quota_retry = active_google_ads_quota_retry_state(session, account, now=now)
        candidates.append(
            (
                99 if quota_retry else tier,
                preference.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
                account.name or "",
                account.id,
            )
        )
    account_ids = [account_id for _tier, _last_run, _name, account_id in sorted(candidates)[:max_accounts]]
    if not account_ids:
        return

    job = BackgroundJob(
        job_type="google_ads_policy_disapproval_sync",
        label=f"Daily Google Ads policy-disapproval restricted-term sync: {len(account_ids)} account(s)",
        requested_by_id=None,
        payload={
            "account_ids": account_ids,
            "queued_by": "scripts/queue_automation_monitor.py",
            "reason": "recurring_policy_disapproval_unapproved_substance_protection",
            "interval_hours": interval_hours,
            "priority": {
                "primary_customer_ids": sorted(primary_customer_ids),
                "secondary_customer_ids": sorted(secondary_customer_ids),
            },
        },
        status=BackgroundJobStatus.queued,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        message = sync_google_ads_policy_disapproval_terms.send(job.id, account_ids)
        job.message_id = str(message.message_id)
        session.commit()
        print(f"Queued Google Ads policy-disapproval sync job #{job.id} for account ids {account_ids}")
    except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
        job.status = BackgroundJobStatus.failed
        job.error = str(exc)
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        raise


def main() -> None:
    args = parse_args()
    runtime_block = primary_instance_required_result()
    if runtime_block is not None:
        print(runtime_block["message"])
        print(runtime_block)
        return
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        scheduler_settings = get_sync_setting_map(session)
        if not args.skip_daily_keywords:
            _queue_daily_keyword_sync_if_due(session, now)
            _queue_daily_page_feed_publish_if_due(session, now, scheduler_settings)
            _queue_policy_disapproval_sync_if_due(session, now, scheduler_settings)
            _queue_universal_garbage_negative_sync_if_due(session, now, scheduler_settings)
        selected_ids = [int(item) for item in args.account_id if item]
        customer_ids = [str(item).replace("-", "").strip() for item in args.customer_id if str(item).strip()]
        if customer_ids:
            accounts = session.scalars(
                select(GoogleAdsAccount).where(
                    GoogleAdsAccount.customer_id.in_(customer_ids),
                    GoogleAdsAccount.is_active.is_(True),
                )
            ).all()
            selected_ids.extend(account.id for account in accounts)
        selected_ids = list(dict.fromkeys(selected_ids))
        preferences = enabled_automation_preferences(session, account_ids=selected_ids or None)
        primary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_primary_customer_ids"))
        secondary_customer_ids = customer_id_set(scheduler_settings.get("automation.scheduler_secondary_customer_ids"))
        include_unlisted = parse_bool(scheduler_settings.get("automation.scheduler_include_unlisted_accounts", False))
        configured_max_accounts = clamp_int(
            args.max_accounts
            if args.max_accounts is not None
            else scheduler_settings.get("automation.scheduler_max_accounts_per_tick", DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK),
            DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK,
            1,
            100,
        )
        due_candidates: list[tuple[int, datetime, str, int]] = []
        due_reasons: dict[int, list[str]] = {}
        due_tiers: dict[int, int] = {}
        decisions: list[dict] = []
        for preference in preferences:
            account = preference.account
            if args.recompute_schedule:
                schedule_decision = refresh_low_traffic_schedule(
                    session,
                    preference,
                    fetch_time_zone=bool(args.fetch_timezone),
                )
                peak_decision = refresh_peak_budget_decision(
                    session,
                    preference,
                    fetch_time_zone=False,
                )
                decisions.append(
                    {
                        "account_id": preference.account_id,
                        "customer_id": account.customer_id,
                        "priority_tier": scheduler_tier(account, primary_customer_ids, secondary_customer_ids),
                        "time": schedule_decision.get("recommended_time"),
                        "time_zone": schedule_decision.get("time_zone"),
                        "low_hour_impressions": schedule_decision.get("low_hour_impressions"),
                        "traffic_peak_hour": schedule_decision.get("peak_hour"),
                        "traffic_peak_hour_impressions": schedule_decision.get("peak_hour_impressions"),
                        "conversion_peak_time": peak_decision.get("peak_time"),
                        "conversion_peak_conversions": peak_decision.get("peak_hour_conversions"),
                        "budget_boost_start_time": peak_decision.get("boost_start_time"),
                    }
                )
            schedule_due = preference_due_now(preference, now=now)
            budget_guard_due = budget_guard_due_now(preference, now=now)
            peak_due = peak_budget_due_now(session, preference, now=now)
            reasons = []
            if args.ignore_schedule or schedule_due:
                reasons.append("daily_low_traffic")
            if budget_guard_due:
                reasons.append("budget_guard")
            if peak_due:
                reasons.append("peak_budget")
            if automation_first_run_missing(session, preference.account_id):
                reasons.append("first_run_bootstrap")
            if reasons:
                tier = scheduler_tier(account, primary_customer_ids, secondary_customer_ids)
                heavy_reasons = not set(reasons).issubset(BUDGET_ONLY_REASONS)
                quota_retry = active_google_ads_quota_retry_state(session, account, now=now)
                if quota_retry:
                    decisions.append(
                        {
                            "account_id": preference.account_id,
                            "customer_id": account.customer_id,
                            "priority_tier": tier,
                            "status": "deferred_google_ads_quota",
                            "reasons": reasons,
                            "retry_not_before": quota_retry.get("retry_not_before"),
                            "quota_key": quota_retry.get("quota_key"),
                        }
                    )
                    continue
                if tier > 1 and heavy_reasons and not include_unlisted and not selected_ids:
                    decisions.append(
                        {
                            "account_id": preference.account_id,
                            "customer_id": account.customer_id,
                            "priority_tier": tier,
                            "status": "deferred_unlisted_basic_access",
                            "reasons": reasons,
                        }
                    )
                    continue
                due_candidates.append(
                    (
                        tier,
                        preference.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
                        account.name,
                        preference.account_id,
                    )
                )
                due_reasons[preference.account_id] = reasons
                due_tiers[preference.account_id] = tier
        due_account_ids = [account_id for _tier, _last_run, _name, account_id in sorted(due_candidates)]
        due_account_ids = list(dict.fromkeys(due_account_ids))
        original_due_account_ids = list(due_account_ids)
        deferred_account_ids: list[int] = []
        if len(due_account_ids) > configured_max_accounts:
            deferred_account_ids = due_account_ids[configured_max_accounts:]
            due_account_ids = due_account_ids[:configured_max_accounts]
            due_reasons = {account_id: due_reasons[account_id] for account_id in due_account_ids}
        budget_only = bool(due_account_ids) and all(set(due_reasons.get(account_id) or []).issubset(BUDGET_ONLY_REASONS) for account_id in due_account_ids)
        if not due_account_ids:
            print("No enabled automation accounts are due right now.")
            if decisions:
                print({"schedule_decisions": decisions})
            return
        stale_queued_before = now - STALE_QUEUED_AUTOMATION_JOB_AFTER
        stale_jobs = session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status == BackgroundJobStatus.queued,
                BackgroundJob.started_at.is_(None),
                BackgroundJob.created_at < stale_queued_before,
            )
        ).all()
        for stale_job in stale_jobs:
            stale_job.status = BackgroundJobStatus.failed
            stale_job.finished_at = now
            stale_job.error = "Stale queued automation job never started; marked failed so scheduler can resume."
        stale_running_before = now - STALE_RUNNING_AUTOMATION_JOB_AFTER
        stale_running_jobs = session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status.in_([BackgroundJobStatus.running, BackgroundJobStatus.cancel_requested]),
                BackgroundJob.started_at.is_not(None),
                BackgroundJob.started_at < stale_running_before,
            )
        ).all()
        for stale_job in stale_running_jobs:
            stale_job.status = BackgroundJobStatus.failed
            stale_job.finished_at = now
            stale_job.error = "Stale running automation job exceeded the worker lease window; marked failed so scheduler can resume."
        if stale_jobs or stale_running_jobs:
            session.commit()
        active_job = session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status.in_(
                    [
                        BackgroundJobStatus.queued,
                        BackgroundJobStatus.running,
                        BackgroundJobStatus.cancel_requested,
                    ]
                ),
            )
            .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
            .limit(1)
        )
        if active_job is not None:
            print(f"Automation monitor job #{active_job.id} is already {active_job.status.value}; not queuing another job.")
            return
        label = (
            f"Automation monitor: {len(due_account_ids)} due account(s)"
            if len(due_account_ids) != 1
            else f"Automation monitor: account #{due_account_ids[0]}"
        )
        job = BackgroundJob(
            job_type="google_ads_automation_monitor",
            label=label,
            requested_by_id=None,
            payload={
                "account_ids": due_account_ids,
                "force": bool(args.force),
                "budget_only": budget_only,
                "queued_by": "scripts/queue_automation_monitor.py",
                "due_reasons": due_reasons,
                "deferred_account_ids": deferred_account_ids,
                "scheduler_max_accounts_per_tick": configured_max_accounts,
                "original_due_count": len(original_due_account_ids),
                "priority": {
                    "primary_customer_ids": sorted(primary_customer_ids),
                    "secondary_customer_ids": sorted(secondary_customer_ids),
                    "include_unlisted_accounts": include_unlisted,
                    "due_tiers": {account_id: due_tiers.get(account_id) for account_id in due_account_ids},
                },
                "schedule_decisions": decisions,
            },
            status=BackgroundJobStatus.queued,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            message = run_google_ads_automation_monitor.send(job.id, due_account_ids, bool(args.force), budget_only)
            job.message_id = str(message.message_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
            job.status = BackgroundJobStatus.failed
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            raise
        print(f"Queued automation monitor job #{job.id} for account ids {due_account_ids}")


if __name__ == "__main__":
    main()
