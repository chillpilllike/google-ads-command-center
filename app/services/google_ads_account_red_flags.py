from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import AppSetting, GoogleAdsAccount, GoogleAdsAutomationPreference


RED_FLAG_PREFIX = "google_ads.account_api_red_flag"
BLOCKED_STATUS_TERMS = ("suspended", "circumventing", "compromised", "compromized")


def clean_customer_id(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def account_red_flag_key(customer_id: Any) -> str:
    return f"{RED_FLAG_PREFIX}.{clean_customer_id(customer_id)}"


def account_status_is_api_blocked(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(term in text for term in BLOCKED_STATUS_TERMS)


def account_api_red_flag(session: Session, account: Optional[GoogleAdsAccount]) -> Optional[dict[str, Any]]:
    if account is None:
        return None
    row = session.scalar(select(AppSetting).where(AppSetting.key == account_red_flag_key(account.customer_id)).limit(1))
    if row is None or getattr(row, "key", "") != account_red_flag_key(account.customer_id) or not isinstance(row.value, dict):
        return None
    value = row.value
    if value.get("active") is False:
        return None
    return {
        "account_id": account.id,
        "customer_id": clean_customer_id(account.customer_id),
        "account_name": account.name,
        "status": "blocked_by_account_red_flag",
        "reason": str(value.get("reason") or value.get("status") or "Google Ads account is red-flagged."),
        "red_flag": value,
    }


def red_flag_blocked_result(account: GoogleAdsAccount, red_flag: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": account.id,
        "customer_id": clean_customer_id(account.customer_id),
        "account_name": account.name,
        "mode": "skipped",
        "status": "blocked_by_account_red_flag",
        "google_ads_api_blocked": True,
        "steps": [
            {
                "name": "account_api_red_flag",
                "status": "blocked_by_account_red_flag",
                "reason": red_flag.get("reason"),
                "red_flag": red_flag.get("red_flag") or red_flag,
            }
        ],
    }


def mark_preference_account_red_flagged(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    red_flag: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    preference.last_run_at = now
    preference.last_analysis_at = now
    preference.last_error = str(red_flag.get("reason") or "Account red-flag blocks Google Ads API calls.")[:2000]
    preference.strategy_summary_json = red_flag_blocked_result(preference.account, red_flag) if preference.account else red_flag
    session.commit()


def upsert_account_api_red_flag(
    session: Session,
    account: GoogleAdsAccount,
    *,
    status: str,
    reason: str = "",
    source: str = "",
    source_url: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "active": True,
        "status": status,
        "reason": reason or status,
        "source": source,
        "source_url": source_url,
        "account_id": account.id,
        "account_name": account.name,
        "customer_id": clean_customer_id(account.customer_id),
        "manager_customer_id": clean_customer_id(account.manager_customer_id),
        "updated_at": now,
    }
    stmt = insert(AppSetting).values(
        key=account_red_flag_key(account.customer_id),
        value=payload,
        category="google_ads_account_red_flags",
        label=f"{account.name} API red flag",
        help_text="Blocks all automation Google Ads API calls for suspended/circumventing account-level policy issues.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "category": stmt.excluded.category,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
            "input_type": stmt.excluded.input_type,
            "sensitive": stmt.excluded.sensitive,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    session.execute(stmt)
