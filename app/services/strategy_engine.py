from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsCampaignMetric, StrategyRecommendation


def _roas(value: float, cost_micros: int) -> Optional[float]:
    if cost_micros <= 0:
        return None
    return value / (cost_micros / 1_000_000)


def generate_value_strategy_recommendations(session: Session) -> int:
    settings = get_sync_setting_map(session)
    min_conversions = float(settings.get("optimizer.min_conversions_for_bid_change", 5) or 5)
    value_bidding_min_conversions = float(settings.get("optimizer.min_conversions_for_value_bidding", 15) or 15)
    min_cost_micros = int(float(settings.get("optimizer.min_cost_for_action", 25) or 25) * 1_000_000)
    min_clicks = int(settings.get("optimizer.zero_conversion_min_clicks", 10) or 10)

    today = date.today()
    last_7_start = today - timedelta(days=6)
    last_30_start = today - timedelta(days=29)
    session.execute(delete(StrategyRecommendation).where(StrategyRecommendation.status == "proposed"))
    saved = 0

    accounts = session.scalars(select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True))).all()
    for account in accounts:
        campaigns = session.execute(
            select(GoogleAdsCampaignMetric.campaign_id, GoogleAdsCampaignMetric.campaign_name)
            .where(
                GoogleAdsCampaignMetric.account_id == account.id,
                GoogleAdsCampaignMetric.campaign_status == "ENABLED",
            )
            .group_by(GoogleAdsCampaignMetric.campaign_id, GoogleAdsCampaignMetric.campaign_name)
        ).all()
        for campaign_id, campaign_name in campaigns:
            last_7 = session.execute(
                select(
                    func.sum(GoogleAdsCampaignMetric.cost_micros),
                    func.sum(GoogleAdsCampaignMetric.conversions),
                    func.sum(GoogleAdsCampaignMetric.conversions_value),
                    func.sum(GoogleAdsCampaignMetric.clicks),
                    func.sum(GoogleAdsCampaignMetric.all_conversions_value),
                ).where(
                    GoogleAdsCampaignMetric.account_id == account.id,
                    GoogleAdsCampaignMetric.campaign_id == campaign_id,
                    GoogleAdsCampaignMetric.metric_date >= last_7_start,
                )
            ).one()
            last_30 = session.execute(
                select(
                    func.sum(GoogleAdsCampaignMetric.cost_micros),
                    func.sum(GoogleAdsCampaignMetric.conversions),
                    func.sum(GoogleAdsCampaignMetric.conversions_value),
                    func.sum(GoogleAdsCampaignMetric.clicks),
                    func.sum(GoogleAdsCampaignMetric.all_conversions_value),
                ).where(
                    GoogleAdsCampaignMetric.account_id == account.id,
                    GoogleAdsCampaignMetric.campaign_id == campaign_id,
                    GoogleAdsCampaignMetric.metric_date >= last_30_start,
                )
            ).one()
            latest_day = session.execute(
                select(
                    GoogleAdsCampaignMetric.metric_date,
                    GoogleAdsCampaignMetric.cost_micros,
                    GoogleAdsCampaignMetric.conversions,
                    GoogleAdsCampaignMetric.conversions_value,
                    GoogleAdsCampaignMetric.clicks,
                    GoogleAdsCampaignMetric.target_roas,
                    GoogleAdsCampaignMetric.bidding_strategy_type,
                )
                .where(
                    GoogleAdsCampaignMetric.account_id == account.id,
                    GoogleAdsCampaignMetric.campaign_id == campaign_id,
                )
                .order_by(GoogleAdsCampaignMetric.metric_date.desc())
                .limit(1)
            ).one_or_none()
            cost_7 = int(last_7[0] or 0)
            conversions_7 = float(last_7[1] or 0)
            value_7 = float(last_7[2] or 0)
            clicks_7 = int(last_7[3] or 0)
            all_value_7 = float(last_7[4] or 0)
            cost_30 = int(last_30[0] or 0)
            conversions_30 = float(last_30[1] or 0)
            value_30 = float(last_30[2] or 0)
            clicks_30 = int(last_30[3] or 0)
            all_value_30 = float(last_30[4] or 0)
            roas_7 = _roas(value_7, cost_7)
            roas_30 = _roas(value_30, cost_30)
            latest_cost = int(latest_day[1] or 0) if latest_day else 0
            latest_value = float(latest_day[3] or 0) if latest_day else 0
            latest_clicks = int(latest_day[4] or 0) if latest_day else 0
            latest_roas = _roas(latest_value, latest_cost)
            avg_daily_cost = cost_30 / 30 if cost_30 else 0
            evidence = {
                "last_7": {
                    "cost": cost_7 / 1_000_000,
                    "conversions": conversions_7,
                    "conversion_value": value_7,
                    "all_conversion_value": all_value_7,
                    "clicks": clicks_7,
                    "roas": roas_7,
                },
                "last_30": {
                    "cost": cost_30 / 1_000_000,
                    "conversions": conversions_30,
                    "conversion_value": value_30,
                    "all_conversion_value": all_value_30,
                    "clicks": clicks_30,
                    "roas": roas_30,
                },
                "thresholds": {
                    "min_conversions_for_bid_change": min_conversions,
                    "min_conversions_for_value_bidding": value_bidding_min_conversions,
                    "min_cost_for_action": min_cost_micros / 1_000_000,
                    "zero_conversion_min_clicks": min_clicks,
                },
                "latest_day": {
                    "date": latest_day[0].isoformat() if latest_day else None,
                    "cost": latest_cost / 1_000_000,
                    "conversion_value": latest_value,
                    "clicks": latest_clicks,
                    "roas": latest_roas,
                    "target_roas": float(latest_day[5] or 0) if latest_day and latest_day[5] else None,
                    "bidding_strategy_type": str(latest_day[6]) if latest_day else None,
                },
            }

            recommendations: list[StrategyRecommendation] = []
            if conversions_30 > 0 and value_30 <= 0:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="missing_conversion_value",
                        severity="risk",
                        title="Conversions have no value",
                        summary="This campaign has conversions but no conversion value in the last 30 days. Fix conversion values before changing Target ROAS.",
                        evidence=evidence,
                    )
                )
            if value_30 <= 0 < all_value_30:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="value_only_in_all_conversions",
                        severity="risk",
                        title="Value is outside primary conversions",
                        summary="All conversions include value, but primary conversions do not. Review conversion action primary status and goal biddability before value bidding.",
                        evidence=evidence,
                    )
                )
            if conversions_30 >= value_bidding_min_conversions and roas_7 and roas_30 and roas_7 > roas_30 * 1.2:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="scale_value_winner",
                        severity="opportunity",
                        title="Scale value winner",
                        summary="Last 7 day ROAS is materially above the 30 day baseline with enough conversion volume. Review budget room or a guarded Target ROAS relaxation to increase conversion value.",
                        evidence=evidence,
                    )
                )
            if conversions_30 >= value_bidding_min_conversions and roas_7 and roas_30 and roas_7 < roas_30 * 0.7 and cost_7 >= min_cost_micros:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="roas_drop_watch",
                        severity="watch",
                        title="ROAS dropped against baseline",
                        summary="Last 7 day ROAS is materially below the 30 day baseline. Review recent changes, goal coverage, assets, and search terms before changing bid targets.",
                        evidence=evidence,
                    )
                )
            if conversions_30 >= value_bidding_min_conversions and roas_30 and roas_30 >= 2 and value_7 > 0:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="ready_for_guarded_value_scale",
                        severity="opportunity",
                        title="Ready for guarded value scale",
                        summary="This campaign has enough recent value conversions and positive ROAS. Candidate for budget expansion, value-rule review, or controlled Target ROAS tuning.",
                        evidence=evidence,
                    )
                )
            if cost_7 >= min_cost_micros and conversions_7 == 0 and clicks_7 >= min_clicks:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="waste_no_value",
                        severity="risk",
                        title="Spend without conversion value",
                        summary="Campaign spent in the last 7 days with enough clicks but no conversions. Review targeting, search terms, assets, landing page, and budget.",
                        evidence=evidence,
                    )
                )
            if latest_cost >= min_cost_micros and avg_daily_cost > 0 and latest_cost >= avg_daily_cost * 2 and latest_value <= 0:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="daily_spend_spike_without_value",
                        severity="risk",
                        title="Daily spend spike without value",
                        summary="Latest synced day spent at least twice the 30-day average and produced no conversion value. Review before allowing more budget.",
                        evidence=evidence,
                    )
                )
            if conversions_30 < value_bidding_min_conversions and cost_30 >= min_cost_micros and value_30 > 0:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="near_value_bidding_ready",
                        severity="watch",
                        title="Value bidding not ready yet",
                        summary="This campaign has conversion value but not enough recent conversion volume for Target ROAS changes. Keep collecting value data and avoid aggressive target changes.",
                        evidence=evidence,
                    )
                )
            if conversions_30 <= 0 and cost_30 >= min_cost_micros:
                recommendations.append(
                    StrategyRecommendation(
                        account_id=account.id,
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        recommendation_type="no_conversion_value_data",
                        severity="risk",
                        title="No conversion value data",
                        summary="This enabled campaign has 30-day spend but no conversion value. Fix goals, tracking, or traffic quality before any scale action.",
                        evidence=evidence,
                    )
                )
            for recommendation in recommendations:
                session.add(recommendation)
                saved += 1
    session.commit()
    return saved
