from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsAutomationPreference, GoogleAdsCampaignMetric


def pmax_conversion_threshold(preference: GoogleAdsAutomationPreference) -> float:
    return min(max(float(preference.pmax_min_7d_conversions or 15.0), 15.0), 1000.0)


def auto_search_campaign_conversion_totals(
    session: Session,
    account: GoogleAdsAccount,
    *,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    if session is None or not hasattr(session, "execute"):
        return {
            "status": "unavailable",
            "reason": "No database session is available to read automation Search campaign conversions.",
            "conversions": 0.0,
            "primary_conversions": 0.0,
            "all_conversions": 0.0,
            "campaign_count": 0,
        }

    conditions = [
        GoogleAdsCampaignMetric.account_id == account.id,
        GoogleAdsCampaignMetric.campaign_name.ilike("AUTO |%"),
        func.upper(GoogleAdsCampaignMetric.channel_type) == "SEARCH",
    ]
    start_date = None
    end_date = None
    if days is not None:
        safe_days = min(max(int(days or 0), 1), 3650)
        today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        start_date = today - timedelta(days=safe_days - 1)
        end_date = today
        conditions.append(GoogleAdsCampaignMetric.metric_date >= start_date)

    row = session.execute(
        select(
            func.coalesce(func.sum(GoogleAdsCampaignMetric.conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.all_conversions), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.conversions_value), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.all_conversions_value), 0.0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.clicks), 0),
            func.coalesce(func.sum(GoogleAdsCampaignMetric.impressions), 0),
            func.count(func.distinct(GoogleAdsCampaignMetric.campaign_id)),
            func.max(GoogleAdsCampaignMetric.metric_date),
            func.max(GoogleAdsCampaignMetric.synced_at),
        ).where(*conditions)
    ).one()

    primary_conversions = float(row[0] or 0.0)
    all_conversions = float(row[1] or 0.0)
    return {
        "status": "ok",
        "scope": "automation_search_campaigns",
        "days": days,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "conversions": max(primary_conversions, all_conversions),
        "primary_conversions": primary_conversions,
        "all_conversions": all_conversions,
        "conversion_value": max(float(row[2] or 0.0), float(row[3] or 0.0)),
        "clicks": int(row[4] or 0),
        "impressions": int(row[5] or 0),
        "campaign_count": int(row[6] or 0),
        "latest_metric_date": row[7].isoformat() if row[7] else None,
        "latest_synced_at": row[8].isoformat() if row[8] else None,
    }


def pmax_activation_gate(
    session: Session,
    account: GoogleAdsAccount,
    preference: GoogleAdsAutomationPreference,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    threshold = pmax_conversion_threshold(preference)
    metrics = auto_search_campaign_conversion_totals(session, account, now=now)
    conversions = float(metrics.get("conversions") or 0.0)
    allowed = conversions >= threshold
    return {
        "allowed": allowed,
        "threshold": threshold,
        "conversions": conversions,
        "metrics": metrics,
        "reason": (
            f"PMax is allowed because automation-owned Search campaigns have {conversions:g} conversions, "
            f"meeting the {threshold:g} conversion threshold."
            if allowed
            else f"PMax is held until automation-owned Search campaigns reach {threshold:g} conversions. "
            f"Current saved Search conversions: {conversions:g}."
        ),
    }
