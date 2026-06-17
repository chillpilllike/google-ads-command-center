from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import RazorpayDailyReceipt


def sync_razorpay_daily_receipts(session: Session, days: int) -> int:
    values = get_sync_setting_map(session)
    key_id = values.get("razorpay.key_id")
    key_secret = values.get("razorpay.key_secret")
    if not key_id or not key_secret:
        raise RuntimeError("Missing Razorpay settings: razorpay.key_id, razorpay.key_secret")

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=max(days - 1, 0))
    params = {
        "from": int(start_dt.timestamp()),
        "to": int(end_dt.timestamp()),
        "count": 100,
        "skip": 0,
    }
    aggregates: dict[tuple[date, str], dict[str, int]] = defaultdict(
        lambda: {
            "captured_amount_subunits": 0,
            "refunded_amount_subunits": 0,
            "fee_subunits": 0,
            "tax_subunits": 0,
            "captured_count": 0,
            "failed_count": 0,
            "authorized_count": 0,
        }
    )
    while True:
        response = requests.get(
            "https://api.razorpay.com/v1/payments",
            auth=(str(key_id), str(key_secret)),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        items = payload.get("items", [])
        for item in items:
            created = datetime.fromtimestamp(int(item["created_at"]), tz=timezone.utc).date()
            currency = str(item.get("base_currency") or item.get("currency") or "INR")
            aggregate = aggregates[(created, currency)]
            status = item.get("status")
            if status == "captured":
                aggregate["captured_amount_subunits"] += int(item.get("base_amount") or item.get("amount") or 0)
                aggregate["refunded_amount_subunits"] += int(item.get("amount_refunded") or 0)
                aggregate["fee_subunits"] += int(item.get("fee") or 0)
                aggregate["tax_subunits"] += int(item.get("tax") or 0)
                aggregate["captured_count"] += 1
            elif status == "failed":
                aggregate["failed_count"] += 1
            elif status == "authorized":
                aggregate["authorized_count"] += 1
        if len(items) < params["count"]:
            break
        params["skip"] += params["count"]

    saved = 0
    for (receipt_date, currency), values_for_day in aggregates.items():
        stmt = insert(RazorpayDailyReceipt).values(
            receipt_date=receipt_date,
            currency=currency,
            **values_for_day,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RazorpayDailyReceipt.receipt_date, RazorpayDailyReceipt.currency],
            set_=values_for_day,
        )
        session.execute(stmt)
        saved += 1
    session.commit()
    return saved
