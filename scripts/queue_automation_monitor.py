#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, select

from app.models import AdDraft, BackgroundJob, BackgroundJobStatus, GoogleAdsAccount
from app.runtime_role import primary_instance_required_result
from app.services.google_ads_automation import (
    budget_guard_due_now,
    enabled_automation_preferences,
    peak_budget_due_now,
    preference_due_now,
    refresh_low_traffic_schedule,
    refresh_peak_budget_decision,
)
from app.tasks import SessionLocal, run_google_ads_automation_monitor

STALE_QUEUED_AUTOMATION_JOB_AFTER = timedelta(hours=2)
STALE_RUNNING_AUTOMATION_JOB_AFTER = timedelta(hours=3)


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime_block = primary_instance_required_result()
    if runtime_block is not None:
        print(runtime_block["message"])
        print(runtime_block)
        return
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
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
        due_account_ids: list[int] = []
        due_reasons: dict[int, list[str]] = {}
        decisions: list[dict] = []
        for preference in preferences:
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
                due_account_ids.append(preference.account_id)
                due_reasons[preference.account_id] = reasons
        due_account_ids = list(dict.fromkeys(due_account_ids))
        budget_only_reasons = {"budget_guard", "peak_budget"}
        budget_only = bool(due_account_ids) and all(set(due_reasons.get(account_id) or []).issubset(budget_only_reasons) for account_id in due_account_ids)
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
