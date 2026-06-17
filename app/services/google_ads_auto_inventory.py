from __future__ import annotations

from datetime import date
from typing import Any

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsCampaignMetric
from app.services.google_ads_api_errors import summarize_google_ads_exception
from app.services.google_ads_live_campaign_creator import _google_ads_search
from app.services.google_ads_snapshot_store import upsert_snapshot
from app.services.google_ads_sync import build_client, enum_name


DATASET_AUTO_LIVE_INVENTORY = "auto_live_inventory"
AUTO_LIVE_INVENTORY_SCHEMA_VERSION = 1


def _micros_value(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _text_asset_values(items: Any) -> list[str]:
    values: list[str] = []
    for item in items or []:
        text = str(getattr(item, "text", "") or "").strip()
        if text:
            values.append(text)
    return values


def _final_urls(value: Any) -> list[str]:
    try:
        return [str(item) for item in (value or []) if str(item or "").strip()]
    except Exception:
        return []


def _campaign_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          customer.currency_code,
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversion_value.target_roas,
          campaign.target_spend.cpc_bid_ceiling_micros,
          campaign_budget.resource_name,
          campaign_budget.name,
          campaign_budget.amount_micros,
          campaign.ai_max_setting.enable_ai_max,
          campaign.network_settings.target_google_search,
          campaign.network_settings.target_search_network,
          campaign.network_settings.target_partner_search_network,
          campaign.network_settings.target_content_network
        FROM campaign
        WHERE campaign.name LIKE 'AUTO |%'
          AND campaign.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "customer_currency_code": str(row.customer.currency_code or ""),
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "campaign_resource_name": str(row.campaign.resource_name or ""),
                "campaign_status": enum_name(row.campaign.status),
                "channel_type": enum_name(row.campaign.advertising_channel_type),
                "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
                "target_roas": _float_value(row.campaign.maximize_conversion_value.target_roas),
                "cpc_bid_ceiling_micros": _micros_value(row.campaign.target_spend.cpc_bid_ceiling_micros),
                "campaign_budget_resource_name": str(row.campaign_budget.resource_name or ""),
                "campaign_budget_name": str(row.campaign_budget.name or ""),
                "budget_amount_micros": _micros_value(row.campaign_budget.amount_micros),
                "enable_ai_max": bool(row.campaign.ai_max_setting.enable_ai_max),
                "network_settings": {
                    "target_google_search": bool(row.campaign.network_settings.target_google_search),
                    "target_search_network": bool(row.campaign.network_settings.target_search_network),
                    "target_partner_search_network": bool(row.campaign.network_settings.target_partner_search_network),
                    "target_content_network": bool(row.campaign.network_settings.target_content_network),
                },
            }
        )
    return rows


def _ad_group_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group.resource_name,
          ad_group.status,
          ad_group.type
        FROM ad_group
        WHERE campaign.name LIKE 'AUTO |%'
          AND ad_group.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "ad_group_id": int(row.ad_group.id or 0),
                "ad_group_name": str(row.ad_group.name or ""),
                "ad_group_resource_name": str(row.ad_group.resource_name or ""),
                "ad_group_status": enum_name(row.ad_group.status),
                "ad_group_type": enum_name(row.ad_group.type_),
            }
        )
    return rows


def _ad_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group_ad.resource_name,
          ad_group_ad.status,
          ad_group_ad.ad.id,
          ad_group_ad.ad.resource_name,
          ad_group_ad.ad.type,
          ad_group_ad.ad.final_urls,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          ad_group_ad.ad.expanded_dynamic_search_ad.description,
          ad_group_ad.ad.expanded_dynamic_search_ad.description2
        FROM ad_group_ad
        WHERE campaign.name LIKE 'AUTO |%'
          AND ad_group_ad.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        ad = row.ad_group_ad.ad
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "ad_group_id": int(row.ad_group.id or 0),
                "ad_group_name": str(row.ad_group.name or ""),
                "ad_id": int(ad.id or 0),
                "ad_resource_name": str(ad.resource_name or ""),
                "ad_group_ad_resource_name": str(row.ad_group_ad.resource_name or ""),
                "ad_status": enum_name(row.ad_group_ad.status),
                "ad_type": enum_name(ad.type_),
                "final_urls": _final_urls(ad.final_urls),
                "responsive_search_ad": {
                    "headlines": _text_asset_values(ad.responsive_search_ad.headlines),
                    "descriptions": _text_asset_values(ad.responsive_search_ad.descriptions),
                },
                "expanded_dynamic_search_ad": {
                    "description": str(ad.expanded_dynamic_search_ad.description or ""),
                    "description2": str(ad.expanded_dynamic_search_ad.description2 or ""),
                },
            }
        )
    return rows


def _asset_group_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          asset_group.id,
          asset_group.name,
          asset_group.resource_name,
          asset_group.status,
          asset_group.final_urls
        FROM asset_group
        WHERE campaign.name LIKE 'AUTO |%'
          AND asset_group.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "asset_group_id": int(row.asset_group.id or 0),
                "asset_group_name": str(row.asset_group.name or ""),
                "asset_group_resource_name": str(row.asset_group.resource_name or ""),
                "asset_group_status": enum_name(row.asset_group.status),
                "final_urls": _final_urls(row.asset_group.final_urls),
            }
        )
    return rows


def _ad_group_criterion_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          ad_group.id,
          ad_group.name,
          ad_group_criterion.criterion_id,
          ad_group_criterion.resource_name,
          ad_group_criterion.status,
          ad_group_criterion.type,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.webpage.criterion_name
        FROM ad_group_criterion
        WHERE campaign.name LIKE 'AUTO |%'
          AND ad_group_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "ad_group_id": int(row.ad_group.id or 0),
                "ad_group_name": str(row.ad_group.name or ""),
                "criterion_id": int(row.ad_group_criterion.criterion_id or 0),
                "criterion_resource_name": str(row.ad_group_criterion.resource_name or ""),
                "criterion_status": enum_name(row.ad_group_criterion.status),
                "criterion_type": enum_name(row.ad_group_criterion.type_),
                "keyword_text": str(row.ad_group_criterion.keyword.text or ""),
                "keyword_match_type": enum_name(row.ad_group_criterion.keyword.match_type),
                "webpage_criterion_name": str(row.ad_group_criterion.webpage.criterion_name or ""),
            }
        )
    return rows


def _campaign_criterion_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          campaign_criterion.criterion_id,
          campaign_criterion.resource_name,
          campaign_criterion.status,
          campaign_criterion.negative,
          campaign_criterion.type,
          campaign_criterion.keyword.text,
          campaign_criterion.webpage.criterion_name,
          campaign_criterion.location.geo_target_constant
        FROM campaign_criterion
        WHERE campaign.name LIKE 'AUTO |%'
          AND campaign_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "criterion_id": int(row.campaign_criterion.criterion_id or 0),
                "criterion_resource_name": str(row.campaign_criterion.resource_name or ""),
                "criterion_status": enum_name(row.campaign_criterion.status),
                "negative": bool(row.campaign_criterion.negative),
                "criterion_type": enum_name(row.campaign_criterion.type_),
                "keyword_text": str(row.campaign_criterion.keyword.text or ""),
                "webpage_criterion_name": str(row.campaign_criterion.webpage.criterion_name or ""),
                "geo_target_constant": str(row.campaign_criterion.location.geo_target_constant or ""),
            }
        )
    return rows


def _upsert_campaign_metric_shells(
    session: Session,
    account: GoogleAdsAccount,
    campaigns: list[dict[str, Any]],
) -> int:
    metric_date = date.today()
    saved = 0
    for campaign in campaigns:
        campaign_id = int(campaign.get("campaign_id") or 0)
        if not campaign_id:
            continue
        stmt = insert(GoogleAdsCampaignMetric).values(
            account_id=account.id,
            metric_date=metric_date,
            campaign_id=campaign_id,
            campaign_name=str(campaign.get("campaign_name") or ""),
            campaign_status=str(campaign.get("campaign_status") or "UNKNOWN"),
            channel_type=str(campaign.get("channel_type") or ""),
            bidding_strategy_type=str(campaign.get("bidding_strategy_type") or ""),
            cost_micros=0,
            impressions=0,
            clicks=0,
            conversions=0,
            conversions_value=0,
            all_conversions=0,
            all_conversions_value=0,
            target_roas=float(campaign.get("target_roas") or 0) or None,
            budget_amount_micros=int(campaign.get("budget_amount_micros") or 0) or None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsCampaignMetric.account_id,
                GoogleAdsCampaignMetric.metric_date,
                GoogleAdsCampaignMetric.campaign_id,
            ],
            set_={
                "campaign_name": stmt.excluded.campaign_name,
                "campaign_status": stmt.excluded.campaign_status,
                "channel_type": stmt.excluded.channel_type,
                "bidding_strategy_type": stmt.excluded.bidding_strategy_type,
                "target_roas": stmt.excluded.target_roas,
                "budget_amount_micros": stmt.excluded.budget_amount_micros,
                "synced_at": stmt.excluded.synced_at,
            },
        )
        session.execute(stmt)
        saved += 1
    return saved


def sync_account_auto_live_inventory(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    values = get_sync_setting_map(session)
    client = build_client(values, account.manager_customer_id, account.connection)
    result: dict[str, Any] = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "account_name": account.name,
        "status": "started",
        "datasets": {},
        "errors": [],
    }
    collectors = [
        ("campaigns", _campaign_rows),
        ("ad_groups", _ad_group_rows),
        ("ads", _ad_rows),
        ("asset_groups", _asset_group_rows),
        ("ad_group_criteria", _ad_group_criterion_rows),
        ("campaign_criteria", _campaign_criterion_rows),
    ]
    for key, collector in collectors:
        try:
            rows = collector(client, account)
            result["datasets"][key] = {"row_count": len(rows), "rows": rows}
        except GoogleAdsException as exc:
            result["errors"].append({"dataset": key, "google_ads_error": summarize_google_ads_exception(exc)})
            result["datasets"][key] = {"row_count": 0, "rows": []}
        except Exception as exc:  # noqa: BLE001 - keep other inventory datasets usable.
            result["errors"].append({"dataset": key, "error": str(exc)[:500]})
            result["datasets"][key] = {"row_count": 0, "rows": []}
    campaign_rows = list((result.get("datasets", {}).get("campaigns") or {}).get("rows") or [])
    result["campaign_metric_shells_saved"] = _upsert_campaign_metric_shells(session, account, campaign_rows)
    total_rows = sum(int(dataset.get("row_count") or 0) for dataset in result["datasets"].values())
    result["row_count"] = total_rows
    result["status"] = "partial" if result["errors"] and total_rows else "failed" if result["errors"] else "done"
    upsert_snapshot(
        session,
        dataset_key=DATASET_AUTO_LIVE_INVENTORY,
        scope_key="auto_campaigns",
        payload=result,
        query="AUTO live inventory backfill for campaign/ad group/ad/asset-group/criterion objects",
        schema_version=AUTO_LIVE_INVENTORY_SCHEMA_VERSION,
        account=account,
        row_count=total_rows,
    )
    return result
