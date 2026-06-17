#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select

from app.models import BackgroundJob, BackgroundJobStatus, GoogleAdsAccount
from app.tasks import SessionLocal, sync_google_ads_daily_keywords


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queue the daily Google Ads keyword-bank pull.")
    parser.add_argument("--days", type=int, default=60, help="Insight lookback window to pull.")
    parser.add_argument("--max-rows", type=int, default=5000, help="Maximum Google Ads rows per dataset.")
    parser.add_argument("--force", action="store_true", help="Refresh Google snapshots even when today's cache is fresh.")
    parser.add_argument("--account-id", type=int, action="append", default=[], help="Database account id to sync.")
    parser.add_argument("--customer-id", action="append", default=[], help="Google Ads customer id to sync.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    days = min(max(int(args.days or 60), 1), 365)
    max_rows = min(max(int(args.max_rows or 5000), 50), 50000)
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
        label = (
            f"Daily keyword pull: {len(selected_ids)} selected account(s)"
            if selected_ids
            else "Daily keyword pull: all active accounts"
        )
        job = BackgroundJob(
            job_type="google_ads_keyword_daily_sync",
            label=label,
            requested_by_id=None,
            payload={
                "account_ids": selected_ids or None,
                "days": days,
                "max_rows": max_rows,
                "force": bool(args.force),
                "queued_by": "scripts/queue_daily_keyword_sync.py",
            },
            status=BackgroundJobStatus.queued,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        try:
            message = sync_google_ads_daily_keywords.send(days, job.id, selected_ids or None, max_rows, bool(args.force))
            job.message_id = str(message.message_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - keep failed dispatch visible in Jobs.
            job.status = BackgroundJobStatus.failed
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            raise
        print(f"Queued daily keyword pull job #{job.id}")


if __name__ == "__main__":
    main()
