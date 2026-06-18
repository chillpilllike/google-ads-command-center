from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import (
    GoogleAdsAccount,
    GoogleAdsAccountDailyMetric,
    OdooSaleOrder,
    OdooStoreGoogleAdsMapping,
    SpendGuardSnapshot,
)
from app.services.currency_rates import convert_amount, get_latest_rate_snapshot_sync, snapshot_payload


def _num(values: dict[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_customer_id(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def priority_customer_ids(values: dict[str, Any]) -> set[str]:
    raw = values.get("spend_guard.priority_customer_ids", "")
    if isinstance(raw, list):
        parts = raw
    else:
        parts = str(raw or "").replace("\n", ",").split(",")
    return {clean_customer_id(part) for part in parts if clean_customer_id(part)}


def _store_weight_total(session: Session, store_id: int, website_id: int) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(OdooStoreGoogleAdsMapping.revenue_weight), 0.0)).where(
            OdooStoreGoogleAdsMapping.store_id == store_id,
            OdooStoreGoogleAdsMapping.website_id == int(website_id or 0),
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
    )
    return float(total or 0) or 1.0


def sales_and_margin_inr_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    hours: int = 12,
    rates: dict[str, Any] | None = None,
) -> tuple[float, float, list[dict[str, Any]]]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(int(hours or 12), 1))
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping).where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
    ).all()
    if not mappings:
        return 0.0, 0.0, []

    rates = rates or snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    total_sales_inr = 0.0
    total_margin_inr = 0.0
    details: list[dict[str, Any]] = []
    for mapping in mappings:
        website_id = int(mapping.website_id or 0)
        store_total_weight = _store_weight_total(session, mapping.store_id, website_id)
        allocation = float(mapping.revenue_weight or 1.0) / store_total_weight
        filters = [
            OdooSaleOrder.store_id == mapping.store_id,
            OdooSaleOrder.order_datetime >= since,
            OdooSaleOrder.state.in_(["sale", "done"]),
        ]
        if website_id:
            filters.append(OdooSaleOrder.website_id == website_id)
        rows = session.execute(
            select(
                OdooSaleOrder.currency_code,
                func.max(OdooSaleOrder.website_name),
                func.coalesce(func.sum(OdooSaleOrder.amount_total), 0.0),
                func.coalesce(func.sum(OdooSaleOrder.margin_amount), 0.0),
                func.count(OdooSaleOrder.margin_amount),
                func.count(OdooSaleOrder.id),
            )
            .where(*filters)
            .group_by(OdooSaleOrder.currency_code)
        ).all()
        store_sales_inr = 0.0
        store_margin_inr = 0.0
        for currency_code, website_name, amount, margin, margin_count, count in rows:
            raw_sales = float(amount or 0)
            raw_margin = float(margin or 0)
            converted_sales = convert_amount(raw_sales, currency_code or "UNKNOWN", "INR", rates)
            converted_margin = convert_amount(raw_margin, currency_code or "UNKNOWN", "INR", rates)
            conversion_fallback = False
            if converted_sales is None:
                converted_sales = raw_sales
                converted_margin = raw_margin
                conversion_fallback = True
            allocated_sales = converted_sales * allocation
            allocated_margin = (converted_margin or 0.0) * allocation
            store_sales_inr += allocated_sales
            store_margin_inr += allocated_margin
            details.append(
                {
                    "store_id": mapping.store_id,
                    "store_name": mapping.store.name if mapping.store else "",
                    "website_id": website_id,
                    "website_name": website_name or ("All websites" if not website_id else f"Website {website_id}"),
                    "currency": currency_code or "UNKNOWN",
                    "raw_sales": raw_sales,
                    "raw_margin": raw_margin,
                    "conversion_fallback": conversion_fallback,
                    "margin_order_count": int(margin_count or 0),
                    "order_count": int(count or 0),
                    "allocation": allocation,
                    "allocated_sales_inr": allocated_sales,
                    "allocated_margin_inr": allocated_margin,
                }
            )
        total_sales_inr += store_sales_inr
        total_margin_inr += store_margin_inr
    return total_sales_inr, total_margin_inr, details


def sales_inr_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    hours: int = 12,
    rates: dict[str, Any] | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    sales_inr, _margin_inr, details = sales_and_margin_inr_for_account(
        session,
        account,
        hours=hours,
        rates=rates,
    )
    return sales_inr, details


def guard_status_for_ratio(ratio: float, target_ratio: float, max_ratio: float) -> str:
    if ratio <= target_ratio:
        return "green"
    if ratio <= max_ratio:
        return "amber"
    return "red"


def _account_revenue_signal(session: Session, account: GoogleAdsAccount, *, hours: int) -> dict[str, Any]:
    since_date = (datetime.now(timezone.utc) - timedelta(hours=max(int(hours or 12), 1))).date()
    row = session.execute(
        select(
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions_value), 0.0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions), 0.0),
        ).where(
            GoogleAdsAccountDailyMetric.account_id == account.id,
            GoogleAdsAccountDailyMetric.metric_date >= since_date,
        )
    ).one()
    conversion_value = float(row[0] or 0)
    conversions = float(row[1] or 0)
    return {
        "google_conversion_value": conversion_value,
        "google_conversions": conversions,
        "has_google_revenue": conversion_value > 0 or conversions > 0,
    }


def account_spend_guard_from_cost(
    session: Session,
    account: GoogleAdsAccount,
    *,
    cost_by_currency: dict[str, float],
    hours: int | None = None,
) -> dict[str, Any]:
    settings = get_sync_setting_map(session)
    hours = int(hours or _num(settings, "spend_guard.window_hours", 12))
    target_ratio = _num(settings, "spend_guard.target_ratio", 0.15)
    exploration_margin = _num(settings, "spend_guard.exploration_margin", 0.05)
    rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    sales_inr, margin_inr, sales_details = sales_and_margin_inr_for_account(
        session,
        account,
        hours=hours,
        rates=rates,
    )

    ad_cost_inr = 0.0
    cost_details: list[dict[str, Any]] = []
    for currency_code, amount in cost_by_currency.items():
        converted = convert_amount(float(amount or 0), currency_code or "UNKNOWN", "INR", rates)
        if converted is None:
            continue
        ad_cost_inr += converted
        cost_details.append({"currency": currency_code, "raw_cost": float(amount or 0), "cost_inr": converted})

    margin_data_present = any(int(detail.get("margin_order_count") or 0) > 0 for detail in sales_details)
    priority = clean_customer_id(account.customer_id) in priority_customer_ids(settings)
    revenue_signal = _account_revenue_signal(session, account, hours=hours)
    has_revenue_signal = sales_inr > 0 or bool(revenue_signal.get("has_google_revenue"))
    normal_margin_cap_ratio = _num(settings, "spend_guard.margin_hard_cap_ratio", 0.20)
    priority_extra_ratio = max(_num(settings, "spend_guard.priority_margin_extra_ratio", 0.10), 0.0)
    priority_extra_applied = False
    if margin_data_present:
        basis_mode = "odoo_margin"
        basis_inr = margin_inr
        max_ratio = normal_margin_cap_ratio
        target_ratio = min(_num(settings, "spend_guard.margin_warning_ratio", 0.16), max_ratio)
        if priority and has_revenue_signal:
            priority_extra_applied = True
            max_ratio = normal_margin_cap_ratio + priority_extra_ratio
            target_ratio = normal_margin_cap_ratio
    else:
        basis_mode = "gross_sales_fallback"
        basis_inr = sales_inr
        max_ratio = target_ratio + exploration_margin
    ratio = (ad_cost_inr / basis_inr) if basis_inr > 0 else (max_ratio + 1.0 if ad_cost_inr > 0 else 0.0)
    status = guard_status_for_ratio(ratio, target_ratio, max_ratio)
    normal_margin_cap_inr = basis_inr * normal_margin_cap_ratio if margin_data_present else 0.0
    priority_extra_inr = basis_inr * priority_extra_ratio if priority_extra_applied else 0.0
    allowed_spend_inr = basis_inr * max_ratio if basis_inr > 0 else 0.0
    details = {
        "sales_details": sales_details,
        "cost_details": cost_details,
        "priority_account": priority,
        "revenue_signal": revenue_signal,
        "has_revenue_signal": has_revenue_signal,
        "normal_margin_cap_ratio": normal_margin_cap_ratio,
        "priority_margin_extra_ratio": priority_extra_ratio,
        "priority_extra_applied": priority_extra_applied,
        "normal_margin_cap_inr": normal_margin_cap_inr,
        "priority_extra_inr": priority_extra_inr,
        "allowed_spend_inr": allowed_spend_inr,
        "basis_mode": basis_mode,
        "basis_inr": basis_inr,
        "budget_basis_inr": basis_inr,
        "margin_inr": margin_inr,
        "target_ratio": target_ratio,
        "max_ratio": max_ratio,
        "window_hours": hours,
    }
    session.add(
        SpendGuardSnapshot(
            account_id=account.id,
            guard_window_hours=hours,
            status=status,
            sales_inr=sales_inr,
            margin_inr=margin_inr,
            ad_cost_inr=ad_cost_inr,
            spend_ratio=ratio,
            target_ratio=target_ratio,
            max_ratio=max_ratio,
            details=details,
        )
    )
    session.flush()
    return {
        "status": status,
        "sales_inr": sales_inr,
        "margin_inr": margin_inr,
        "budget_basis_inr": basis_inr,
        "ad_cost_inr": ad_cost_inr,
        "spend_ratio": ratio,
        "target_ratio": target_ratio,
        "max_ratio": max_ratio,
        "priority_account": priority,
        "priority_extra_applied": priority_extra_applied,
        "normal_margin_cap_inr": normal_margin_cap_inr,
        "priority_extra_inr": priority_extra_inr,
        "allowed_spend_inr": allowed_spend_inr,
        "details": details,
    }


def odoo_sales_budget_guard_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    window_days: int = 7,
    max_spend_ratio: float = 0.15,
) -> dict[str, Any]:
    window_days = min(max(int(window_days or 7), 1), 90)
    try:
        max_spend_ratio = min(max(float(max_spend_ratio), 0.01), 1.0)
    except (TypeError, ValueError):
        max_spend_ratio = 0.15
    target_ratio = max_spend_ratio * 0.85
    rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    sales_inr, margin_inr, sales_details = sales_and_margin_inr_for_account(
        session,
        account,
        hours=window_days * 24,
        rates=rates,
    )

    latest_metric_date = session.scalar(
        select(func.max(GoogleAdsAccountDailyMetric.metric_date)).where(
            GoogleAdsAccountDailyMetric.account_id == account.id,
        )
    )
    end_date = latest_metric_date or datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=window_days - 1)
    cost_rows = session.execute(
        select(
            GoogleAdsAccountDailyMetric.currency_code,
            GoogleAdsAccount.currency_code,
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0),
        )
        .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAccountDailyMetric.account_id)
        .where(
            GoogleAdsAccountDailyMetric.account_id == account.id,
            GoogleAdsAccountDailyMetric.metric_date >= start_date,
        )
        .group_by(GoogleAdsAccountDailyMetric.currency_code, GoogleAdsAccount.currency_code)
    ).all()

    ad_cost_inr = 0.0
    cost_details: list[dict[str, Any]] = []
    for metric_currency, account_currency, cost_micros in cost_rows:
        currency_code = metric_currency or account_currency or account.currency_code or "UNKNOWN"
        raw_cost = int(cost_micros or 0) / 1_000_000
        converted = convert_amount(raw_cost, currency_code, "INR", rates)
        if converted is None:
            converted = raw_cost
        ad_cost_inr += converted
        cost_details.append({"currency": currency_code, "raw_cost": raw_cost, "cost_inr": converted, "conversion_fallback": converted == raw_cost})

    ratio = (ad_cost_inr / sales_inr) if sales_inr > 0 else (max_spend_ratio + 1.0 if ad_cost_inr > 0 else 0.0)
    status = guard_status_for_ratio(ratio, target_ratio, max_spend_ratio)
    allowed_spend_inr = sales_inr * max_spend_ratio if sales_inr > 0 else 0.0
    remaining_spend_room_inr = max(0.0, allowed_spend_inr - ad_cost_inr)
    daily_spend_room_inr = remaining_spend_room_inr / max(window_days, 1)
    details = {
        "basis_mode": "odoo_gross_sales",
        "budget_basis_inr": sales_inr,
        "sales_details": sales_details,
        "cost_details": cost_details,
        "target_ratio": target_ratio,
        "max_ratio": max_spend_ratio,
        "allowed_spend_inr": allowed_spend_inr,
        "remaining_spend_room_inr": remaining_spend_room_inr,
        "daily_spend_room_inr": daily_spend_room_inr,
        "window_days": window_days,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    session.add(
        SpendGuardSnapshot(
            account_id=account.id,
            guard_window_hours=window_days * 24,
            status=status,
            sales_inr=sales_inr,
            margin_inr=margin_inr,
            ad_cost_inr=ad_cost_inr,
            spend_ratio=ratio,
            target_ratio=target_ratio,
            max_ratio=max_spend_ratio,
            details=details,
        )
    )
    session.flush()
    return {
        "status": status,
        "basis_mode": "odoo_gross_sales",
        "sales_inr": sales_inr,
        "margin_inr": margin_inr,
        "ad_cost_inr": ad_cost_inr,
        "spend_ratio": ratio,
        "target_ratio": target_ratio,
        "max_ratio": max_spend_ratio,
        "allowed_spend_inr": allowed_spend_inr,
        "remaining_spend_room_inr": remaining_spend_room_inr,
        "daily_spend_room_inr": daily_spend_room_inr,
        "window_days": window_days,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "details": details,
    }


def latest_spend_guard_summary(session: Session, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(SpendGuardSnapshot)
        .order_by(SpendGuardSnapshot.created_at.desc(), SpendGuardSnapshot.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "account_name": row.account.name if row.account else "All accounts",
            "customer_id": row.account.customer_id if row.account else "",
            "status": row.status,
            "sales_inr": row.sales_inr,
            "margin_inr": row.margin_inr,
            "ad_cost_inr": row.ad_cost_inr,
            "spend_ratio": row.spend_ratio,
            "target_ratio": row.target_ratio,
            "max_ratio": row.max_ratio,
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "priority_account": bool((row.details or {}).get("priority_account")),
            "priority_extra_applied": bool((row.details or {}).get("priority_extra_applied")),
            "normal_margin_cap_ratio": (row.details or {}).get("normal_margin_cap_ratio", 0.20),
            "priority_margin_extra_ratio": (row.details or {}).get("priority_margin_extra_ratio", 0.10),
            "normal_margin_cap_inr": (row.details or {}).get("normal_margin_cap_inr", 0.0),
            "priority_extra_inr": (row.details or {}).get("priority_extra_inr", 0.0),
            "allowed_spend_inr": (row.details or {}).get("allowed_spend_inr", 0.0),
            "has_revenue_signal": bool((row.details or {}).get("has_revenue_signal")),
            "basis_mode": (row.details or {}).get("basis_mode", "gross_sales_fallback"),
            "budget_basis_inr": (row.details or {}).get("budget_basis_inr", (row.details or {}).get("basis_inr", row.sales_inr)),
        }
        for row in rows
    ]
