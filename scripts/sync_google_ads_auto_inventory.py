#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import GoogleAdsAccount
from app.services.google_ads_auto_inventory import sync_account_auto_live_inventory


def _clean_customer_id(value: str) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill read-only AUTO campaign inventory from Google Ads into Postgres snapshots.")
    parser.add_argument("customer_ids", nargs="+", help="Google Ads customer IDs to backfill.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    customer_ids = [_clean_customer_id(value) for value in args.customer_ids if _clean_customer_id(value)]
    if not customer_ids:
        raise SystemExit("No valid customer IDs supplied.")
    engine = create_engine(
        get_settings().sqlalchemy_sync_url,
        connect_args={"sslmode": "disable", "connect_timeout": 10},
        pool_pre_ping=True,
    )
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    with SessionLocal() as session:
        accounts = session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.customer_id.in_(customer_ids), GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.customer_id)
        ).all()
        found = {account.customer_id for account in accounts}
        missing = [customer_id for customer_id in customer_ids if customer_id not in found]
        for account in accounts:
            result = sync_account_auto_live_inventory(session, account)
            session.commit()
            datasets = result.get("datasets") or {}
            counts = ", ".join(f"{key}={value.get('row_count', 0)}" for key, value in sorted(datasets.items()))
            print(
                f"{account.name} ({account.customer_id}): {result.get('status')} "
                f"rows={result.get('row_count', 0)} metric_shells={result.get('campaign_metric_shells_saved', 0)} {counts}"
            )
            if result.get("errors"):
                print(f"  errors={len(result['errors'])}")
        if missing:
            print("Missing active accounts:", ", ".join(missing))
    engine.dispose()


if __name__ == "__main__":
    main()
