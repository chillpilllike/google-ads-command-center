from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    CostDashboardSnapshot,
    GoogleAdsAccount,
    GoogleAdsAccountDailyMetric,
    GoogleAdsCampaignMetric,
    OdooSaleOrder,
    OdooStore,
    RazorpayDailyReceipt,
    StrategyRecommendation,
)
from app.services.currency_rates import get_latest_rate_snapshot_sync, snapshot_payload
from app.services.spend_guard import latest_spend_guard_summary


def money(value: float) -> str:
    return f"{value:,.2f}"


def roas_text(value: float, cost: float) -> str:
    if cost <= 0:
        return "n/a"
    return f"{value / cost:,.2f}"


def build_action_cards(
    *,
    days: int,
    ad_breakdown: list[dict[str, Any]],
    synced_accounts_count: int,
    total_accounts_count: int,
    paused_cost: float,
    recommendations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    total_cost = sum(float(row["cost"]) for row in ad_breakdown)
    total_value = sum(float(row["value"]) for row in ad_breakdown)
    total_conversions = sum(float(row["conversions"]) for row in ad_breakdown)
    mixed_currency = len(ad_breakdown) > 1
    cards: list[dict[str, str]] = []

    if synced_accounts_count < total_accounts_count:
        cards.append(
            {
                "tone": "warning",
                "icon": "ti-database-import",
                "title": "Review data coverage",
                "summary": f"{synced_accounts_count} of {total_accounts_count} active accounts have campaign metrics in this window. Others may have no spend or no returned campaign rows.",
            }
        )
    if mixed_currency:
        cards.append(
            {
                "tone": "info",
                "icon": "ti-currency",
                "title": "Use currency-safe totals",
                "summary": "Costs are converted into INR, USD, and AUD using the saved daily exchange-rate snapshot. Keep raw currency rows for audit checks.",
            }
        )
    if total_cost > 0 and total_conversions < 15:
        cards.append(
            {
                "tone": "warning",
                "icon": "ti-shield-half",
                "title": "Hold Target ROAS changes",
                "summary": "Synced data has spend but fewer than 15 recent conversions. Keep value bidding target changes in review mode and focus on goals/tracking first.",
            }
        )
    if total_cost > 0 and total_value > 0 and total_value / total_cost >= 2.5 and total_conversions >= 10:
        cards.append(
            {
                "tone": "success",
                "icon": "ti-trending-up",
                "title": "Protect value winners",
                "summary": f"Current synced ROAS is {total_value / total_cost:,.2f}. Prioritize budget room only for campaigns with value and enough conversion volume.",
            }
        )
    if paused_cost > 0:
        cards.append(
            {
                "tone": "info",
                "icon": "ti-player-pause",
                "title": "Paused spend is visible",
                "summary": f"{paused_cost:,.2f} of synced {days}-day spend came from paused campaigns. The dashboard shows it for accounting, but strategy actions stay on enabled campaigns.",
            }
        )
    risk_count = sum(1 for item in recommendations if item["severity"] == "risk")
    opportunity_count = sum(1 for item in recommendations if item["severity"] == "opportunity")
    if risk_count:
        cards.append(
            {
                "tone": "danger",
                "icon": "ti-alert-triangle",
                "title": "Fix risks before scaling",
                "summary": f"{risk_count} risk recommendation{'s' if risk_count != 1 else ''} found. Treat these as blockers before applying bulk budget or Target ROAS changes.",
            }
        )
    if opportunity_count:
        cards.append(
            {
                "tone": "success",
                "icon": "ti-bolt",
                "title": "Review scale candidates",
                "summary": f"{opportunity_count} opportunity recommendation{'s' if opportunity_count != 1 else ''} found. Review evidence, then queue guarded validation before apply.",
            }
        )
    if not cards:
        cards.append(
            {
                "tone": "secondary",
                "icon": "ti-circle-check",
                "title": "No urgent action",
                "summary": "No high-priority strategy signal was found from the synced data. Keep syncing daily and review conversion value coverage.",
            }
        )
    return cards[:5]


def _date_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def build_cost_dashboard_data(session: Session, days: int) -> dict[str, Any]:
    days = min(max(int(days), 1), 90)
    try:
        currency_rates = snapshot_payload(get_latest_rate_snapshot_sync(session))
    except Exception as exc:  # noqa: BLE001 - dashboard snapshots should survive exchange-rate outages.
        currency_rates = {
            "source": "openexchangerates",
            "base_currency": "USD",
            "rates": {"USD": 1.0},
            "rate_date": "",
            "fetched_at": "",
            "error": str(exc),
        }
    latest_metric_date = session.scalar(select(func.max(GoogleAdsAccountDailyMetric.metric_date)))
    if latest_metric_date is None:
        latest_metric_date = session.scalar(select(func.max(GoogleAdsCampaignMetric.metric_date)))
    end_date = latest_metric_date or date.today()
    start_date = end_date - timedelta(days=days - 1)
    yesterday_date = date.today() - timedelta(days=1)
    total_accounts_count = int(
        session.scalar(select(func.count(GoogleAdsAccount.id)).where(GoogleAdsAccount.is_active.is_(True))) or 0
    )
    ad_total_rows = session.execute(
        select(
            GoogleAdsAccountDailyMetric.currency_code,
            GoogleAdsAccount.currency_code,
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions_value), 0),
            func.coalesce(func.sum(GoogleAdsAccountDailyMetric.clicks), 0),
        )
        .join(GoogleAdsAccountDailyMetric, GoogleAdsAccountDailyMetric.account_id == GoogleAdsAccount.id)
        .where(GoogleAdsAccountDailyMetric.metric_date >= start_date)
        .group_by(GoogleAdsAccountDailyMetric.currency_code, GoogleAdsAccount.currency_code)
        .order_by(func.sum(GoogleAdsAccountDailyMetric.cost_micros).desc())
    ).all()
    ad_breakdown = [
        {
            "currency": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost": int(cost_micros or 0) / 1_000_000,
            "conversions": float(conversions or 0),
            "value": float(conversion_value or 0),
            "clicks": int(clicks or 0),
        }
        for metric_currency_code, account_currency_code, cost_micros, conversions, conversion_value, clicks in ad_total_rows
    ]
    primary_ad_total = ad_breakdown[0] if ad_breakdown else {
        "currency": "",
        "cost": 0.0,
        "conversions": 0.0,
        "value": 0.0,
        "clicks": 0,
    }
    ad_cost = float(primary_ad_total["cost"])
    conversion_value = float(primary_ad_total["value"])
    conversions = float(primary_ad_total["conversions"])
    clicks = int(primary_ad_total["clicks"])
    yesterday_rows = [
        {
            "currency": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost": int(cost_micros or 0) / 1_000_000,
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
            "clicks": int(clicks_row or 0),
        }
        for metric_currency_code, account_currency_code, cost_micros, value, conversions_row, clicks_row in session.execute(
            select(
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions_value), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.clicks), 0),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAccountDailyMetric.account_id)
            .where(GoogleAdsAccountDailyMetric.metric_date == yesterday_date)
            .group_by(GoogleAdsAccountDailyMetric.currency_code, GoogleAdsAccount.currency_code)
            .order_by(func.sum(GoogleAdsAccountDailyMetric.cost_micros).desc())
        ).all()
    ]
    yesterday_primary = yesterday_rows[0] if yesterday_rows else {"currency": "", "cost": 0.0}
    yesterday_prefix = f"{yesterday_primary['currency']} " if yesterday_primary.get("currency") else ""
    coverage = session.execute(
        select(
            func.count(func.distinct(GoogleAdsAccountDailyMetric.account_id)).filter(
                GoogleAdsAccountDailyMetric.cost_micros > 0
            ),
        ).where(GoogleAdsAccountDailyMetric.metric_date >= start_date)
    ).one()
    synced_accounts_count = coverage[0]
    synced_campaign_count = session.scalar(
        select(func.count(func.distinct(GoogleAdsCampaignMetric.campaign_id))).where(
            GoogleAdsCampaignMetric.metric_date >= start_date
        )
    )
    paused_cost_micros = int(
        session.scalar(
            select(func.coalesce(func.sum(GoogleAdsCampaignMetric.cost_micros), 0)).where(
                GoogleAdsCampaignMetric.metric_date >= start_date,
                GoogleAdsCampaignMetric.campaign_status == "PAUSED",
            )
        )
        or 0
    )
    receipt_rows = [
        {
            "currency": currency,
            "amount": int(amount or 0),
            "fee": int(fee or 0),
            "tax": int(tax or 0),
            "count": int(count or 0),
        }
        for currency, amount, fee, tax, count in session.execute(
            select(
                RazorpayDailyReceipt.currency,
                func.sum(RazorpayDailyReceipt.captured_amount_subunits),
                func.sum(RazorpayDailyReceipt.fee_subunits),
                func.sum(RazorpayDailyReceipt.tax_subunits),
                func.sum(RazorpayDailyReceipt.captured_count),
            )
            .where(RazorpayDailyReceipt.receipt_date >= start_date)
            .group_by(RazorpayDailyReceipt.currency)
            .order_by(RazorpayDailyReceipt.currency)
        ).all()
    ]
    receipt_daily_rows = [
        {
            "receipt_date": _date_text(receipt_date),
            "currency": currency,
            "amount": int(amount or 0),
            "count": int(count or 0),
        }
        for receipt_date, currency, amount, count in session.execute(
            select(
                RazorpayDailyReceipt.receipt_date,
                RazorpayDailyReceipt.currency,
                func.sum(RazorpayDailyReceipt.captured_amount_subunits),
                func.sum(RazorpayDailyReceipt.captured_count),
            )
            .where(RazorpayDailyReceipt.receipt_date >= start_date)
            .group_by(RazorpayDailyReceipt.receipt_date, RazorpayDailyReceipt.currency)
            .order_by(RazorpayDailyReceipt.receipt_date.desc(), RazorpayDailyReceipt.currency)
            .limit(14)
        ).all()
    ]
    daily_rows = [
        {
            "metric_date": _date_text(metric_date),
            "currency_code": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
            "clicks": int(clicks_row or 0),
        }
        for metric_date, metric_currency_code, account_currency_code, cost_micros, value, conversions_row, clicks_row in session.execute(
            select(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
                func.sum(GoogleAdsAccountDailyMetric.cost_micros),
                func.sum(GoogleAdsAccountDailyMetric.conversions_value),
                func.sum(GoogleAdsAccountDailyMetric.conversions),
                func.sum(GoogleAdsAccountDailyMetric.clicks),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAccountDailyMetric.account_id)
            .where(GoogleAdsAccountDailyMetric.metric_date >= start_date)
            .group_by(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
            )
            .order_by(GoogleAdsAccountDailyMetric.metric_date.desc(), GoogleAdsAccount.currency_code)
            .limit(120)
        ).all()
    ]
    daily_account_rows = [
        {
            "account_id": int(account_id),
            "metric_date": _date_text(metric_date),
            "account_name": account_name,
            "customer_id": customer_id,
            "currency_code": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
            "clicks": int(clicks_row or 0),
        }
        for metric_date, account_id, account_name, customer_id, metric_currency_code, account_currency_code, cost_micros, value, conversions_row, clicks_row in session.execute(
            select(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
                func.sum(GoogleAdsAccountDailyMetric.cost_micros),
                func.sum(GoogleAdsAccountDailyMetric.conversions_value),
                func.sum(GoogleAdsAccountDailyMetric.conversions),
                func.sum(GoogleAdsAccountDailyMetric.clicks),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAccountDailyMetric.account_id)
            .where(GoogleAdsAccountDailyMetric.metric_date >= start_date)
            .group_by(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
            )
            .order_by(GoogleAdsAccountDailyMetric.metric_date.desc(), func.sum(GoogleAdsAccountDailyMetric.cost_micros).desc())
        ).all()
    ]
    yesterday_account_rows = [
        {
            "account_id": int(account_id),
            "metric_date": _date_text(metric_date),
            "account_name": account_name,
            "customer_id": customer_id,
            "currency_code": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
            "clicks": int(clicks_row or 0),
        }
        for metric_date, account_id, account_name, customer_id, metric_currency_code, account_currency_code, cost_micros, value, conversions_row, clicks_row in session.execute(
            select(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
                func.sum(GoogleAdsAccountDailyMetric.cost_micros),
                func.sum(GoogleAdsAccountDailyMetric.conversions_value),
                func.sum(GoogleAdsAccountDailyMetric.conversions),
                func.sum(GoogleAdsAccountDailyMetric.clicks),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAccountDailyMetric.account_id)
            .where(GoogleAdsAccountDailyMetric.metric_date == yesterday_date)
            .group_by(
                GoogleAdsAccountDailyMetric.metric_date,
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                GoogleAdsAccountDailyMetric.currency_code,
                GoogleAdsAccount.currency_code,
            )
            .order_by(func.sum(GoogleAdsAccountDailyMetric.cost_micros).desc(), GoogleAdsAccount.name)
        ).all()
    ]
    account_filter_rows = [
        {
            "account_id": int(account_id),
            "account_name": account_name,
            "customer_id": customer_id,
            "currency_code": metric_currency_code or account_currency_code or "UNKNOWN",
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
            "clicks": int(clicks_row or 0),
        }
        for account_id, account_name, customer_id, metric_currency_code, account_currency_code, cost_micros, value, conversions_row, clicks_row in session.execute(
            select(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                func.max(GoogleAdsAccountDailyMetric.currency_code),
                GoogleAdsAccount.currency_code,
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions_value), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.conversions), 0),
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.clicks), 0),
            )
            .outerjoin(
                GoogleAdsAccountDailyMetric,
                and_(
                    GoogleAdsAccountDailyMetric.account_id == GoogleAdsAccount.id,
                    GoogleAdsAccountDailyMetric.metric_date >= start_date,
                ),
            )
            .where(GoogleAdsAccount.is_active.is_(True))
            .group_by(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.customer_id,
                GoogleAdsAccount.currency_code,
            )
            .order_by(
                func.coalesce(func.sum(GoogleAdsAccountDailyMetric.cost_micros), 0).desc(),
                GoogleAdsAccount.name,
            )
        ).all()
    ]
    campaign_rows = [
        {
            "account_id": int(account_id),
            "campaign_id": int(campaign_id),
            "account_name": account_name,
            "currency_code": currency_code or "UNKNOWN",
            "campaign_name": campaign_name,
            "campaign_status": campaign_status,
            "channel_type": channel_type,
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
        }
        for account_id, account_name, currency_code, campaign_id, campaign_name, campaign_status, channel_type, cost_micros, value, conversions_row in session.execute(
            select(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.currency_code,
                GoogleAdsCampaignMetric.campaign_id,
                GoogleAdsCampaignMetric.campaign_name,
                GoogleAdsCampaignMetric.campaign_status,
                GoogleAdsCampaignMetric.channel_type,
                func.sum(GoogleAdsCampaignMetric.cost_micros),
                func.sum(GoogleAdsCampaignMetric.conversions_value),
                func.sum(GoogleAdsCampaignMetric.conversions),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsCampaignMetric.account_id)
            .where(GoogleAdsCampaignMetric.metric_date >= start_date)
            .group_by(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.currency_code,
                GoogleAdsCampaignMetric.campaign_id,
                GoogleAdsCampaignMetric.campaign_name,
                GoogleAdsCampaignMetric.campaign_status,
                GoogleAdsCampaignMetric.channel_type,
            )
            .order_by(func.sum(GoogleAdsCampaignMetric.cost_micros).desc())
            .limit(500)
        ).all()
    ]
    yesterday_campaign_rows = [
        {
            "account_id": int(account_id),
            "campaign_id": int(campaign_id),
            "account_name": account_name,
            "currency_code": currency_code or "UNKNOWN",
            "campaign_name": campaign_name,
            "campaign_status": campaign_status,
            "channel_type": channel_type,
            "cost_micros": int(cost_micros or 0),
            "value": float(value or 0),
            "conversions": float(conversions_row or 0),
        }
        for account_id, account_name, currency_code, campaign_id, campaign_name, campaign_status, channel_type, cost_micros, value, conversions_row in session.execute(
            select(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.currency_code,
                GoogleAdsCampaignMetric.campaign_id,
                GoogleAdsCampaignMetric.campaign_name,
                GoogleAdsCampaignMetric.campaign_status,
                GoogleAdsCampaignMetric.channel_type,
                func.sum(GoogleAdsCampaignMetric.cost_micros),
                func.sum(GoogleAdsCampaignMetric.conversions_value),
                func.sum(GoogleAdsCampaignMetric.conversions),
            )
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsCampaignMetric.account_id)
            .where(GoogleAdsCampaignMetric.metric_date == yesterday_date)
            .group_by(
                GoogleAdsAccount.id,
                GoogleAdsAccount.name,
                GoogleAdsAccount.currency_code,
                GoogleAdsCampaignMetric.campaign_id,
                GoogleAdsCampaignMetric.campaign_name,
                GoogleAdsCampaignMetric.campaign_status,
                GoogleAdsCampaignMetric.channel_type,
            )
            .order_by(func.sum(GoogleAdsCampaignMetric.cost_micros).desc())
            .limit(500)
        ).all()
    ]
    recommendations = [
        {
            "account_name": recommendation.account.name if recommendation.account else "",
            "campaign_name": recommendation.campaign_name or "Account",
            "severity": recommendation.severity,
            "recommendation_type": recommendation.recommendation_type,
            "title": recommendation.title,
            "summary": recommendation.summary,
        }
        for recommendation in session.scalars(
            select(StrategyRecommendation)
            .where(StrategyRecommendation.status == "proposed")
            .order_by(StrategyRecommendation.created_at.desc())
            .limit(10)
        ).all()
    ]
    spend_guard_rows = latest_spend_guard_summary(session)
    odoo_sales_rows = [
        {
            "store_name": store_name,
            "website_id": int(website_id or 0),
            "website_name": website_name or ("Website " + str(website_id) if website_id else "All websites"),
            "currency": currency_code or "UNKNOWN",
            "orders": int(order_count or 0),
            "amount": float(amount or 0),
            "margin": float(margin or 0),
            "last_order_at": last_order_at.isoformat() if last_order_at else "",
        }
        for store_name, website_id, website_name, currency_code, order_count, amount, margin, last_order_at in session.execute(
            select(
                OdooStore.name,
                OdooSaleOrder.website_id,
                func.max(OdooSaleOrder.website_name),
                OdooSaleOrder.currency_code,
                func.count(OdooSaleOrder.id),
                func.coalesce(func.sum(OdooSaleOrder.amount_total), 0.0),
                func.coalesce(func.sum(OdooSaleOrder.margin_amount), 0.0),
                func.max(OdooSaleOrder.order_datetime),
            )
            .join(OdooStore, OdooStore.id == OdooSaleOrder.store_id)
            .where(
                OdooStore.is_active.is_(True),
                OdooSaleOrder.order_datetime >= datetime.now(timezone.utc) - timedelta(hours=12),
                OdooSaleOrder.state.in_(["sale", "done"]),
            )
            .group_by(OdooStore.name, OdooSaleOrder.website_id, OdooSaleOrder.currency_code)
            .order_by(func.sum(OdooSaleOrder.amount_total).desc())
            .limit(20)
        ).all()
    ]
    action_cards = build_action_cards(
        days=days,
        ad_breakdown=ad_breakdown,
        synced_accounts_count=int(synced_accounts_count or 0),
        total_accounts_count=total_accounts_count,
        paused_cost=paused_cost_micros / 1_000_000,
        recommendations=recommendations,
    )
    currency_prefix = f"{primary_ad_total['currency']} " if primary_ad_total.get("currency") else ""
    return {
        "days": days,
        "ad_cost": f"{currency_prefix}{money(ad_cost)}" if ad_breakdown else "0.00",
        "yesterday_cost": f"{yesterday_prefix}{money(float(yesterday_primary['cost']))}" if yesterday_rows else "0.00",
        "yesterday_date": _date_text(yesterday_date),
        "yesterday_breakdown": yesterday_rows,
        "conversion_value": f"{currency_prefix}{money(float(conversion_value or 0))}" if ad_breakdown else "0.00",
        "conversions": money(float(conversions or 0)),
        "clicks": int(clicks or 0),
        "roas": roas_text(conversion_value, ad_cost),
        "ad_breakdown": ad_breakdown,
        "synced_accounts_count": int(synced_accounts_count or 0),
        "synced_campaign_count": int(synced_campaign_count or 0),
        "total_accounts_count": total_accounts_count,
        "paused_cost": money(paused_cost_micros / 1_000_000),
        "action_cards": action_cards,
        "receipt_rows": receipt_rows,
        "receipt_daily_rows": receipt_daily_rows,
        "daily_rows": daily_rows,
        "daily_account_rows": daily_account_rows,
        "yesterday_account_rows": yesterday_account_rows,
        "account_rows": account_filter_rows,
        "account_filter_rows": account_filter_rows,
        "campaign_rows": campaign_rows,
        "yesterday_campaign_rows": yesterday_campaign_rows,
        "recommendations": recommendations,
        "spend_guard_rows": spend_guard_rows,
        "odoo_sales_rows": odoo_sales_rows,
        "currency_rates": currency_rates,
        "metric_start_date": _date_text(start_date),
        "metric_end_date": _date_text(end_date),
        "snapshot_generated_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert_cost_dashboard_snapshot(session: Session, days: int) -> dict[str, Any]:
    data = build_cost_dashboard_data(session, days)
    stmt = insert(CostDashboardSnapshot).values(days=days, data=data)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CostDashboardSnapshot.days],
        set_={"data": stmt.excluded.data, "generated_at": func.now()},
    )
    session.execute(stmt)
    session.commit()
    return data


def refresh_standard_cost_dashboard_snapshots(session: Session) -> None:
    for days in (1, 7, 30, 90):
        upsert_cost_dashboard_snapshot(session, days)
