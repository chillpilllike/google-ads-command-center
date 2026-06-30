from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import (
    GoogleAdsAccount,
    GoogleAdsPageFeedAsset,
    GoogleAdsPageFeedCampaignLink,
    GoogleAdsPageFeedPublication,
    OdooProductPageSignal,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.google_ads_api_errors import record_google_ads_api_error, record_google_ads_generic_error
from app.services.google_ads_account_red_flags import account_api_red_flag, upsert_account_api_red_flag
from app.services.google_ads_sync import build_client, enum_name
from app.services.page_feed_restrictions import get_restricted_title_terms_sync, restricted_title_match


MAX_PAGE_FEED_URLS = 1000
PAGE_FEED_LABEL = "odoo_winner"
FALLBACK_PAGE_FEED_LABEL = "odoo_fallback"
DSA_CRITERION_NAME = "Odoo winner page feed"


@dataclass
class PageFeedCandidate:
    signal_id: int
    store_id: int
    website_id: int
    website_name: str
    page_url: str
    page_url_hash: str
    product_code: str
    product_name: str
    label: str
    margin_amount: float
    margin_percent: float | None
    google_conversions: float
    labels: list[str]


@dataclass
class CampaignCandidate:
    campaign_id: int
    campaign_name: str
    resource_name: str
    channel_type: str
    is_dsa: bool
    is_pmax: bool
    pmax_text_asset_automation_opted_in: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def page_url_hash(page_url: str) -> str:
    return hashlib.md5(str(page_url or "").strip().encode("utf-8")).hexdigest()


def gaql_string(value: str) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def _google_ads_search(service: Any, account: GoogleAdsAccount, query: str, *, timeout: int = 30) -> Any:
    try:
        return service.search(customer_id=account.customer_id, query=query, timeout=timeout)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return service.search(customer_id=account.customer_id, query=query)


def _google_ads_mutate(service: Any, method_name: str, request: Any, *, timeout: int = 30) -> Any:
    method = getattr(service, method_name)
    try:
        return method(request=request, timeout=timeout)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return method(request=request)


def _is_suspended_account_error(exc: Any) -> bool:
    text = str(exc or "").upper()
    return "ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT" in text or "ACCOUNT IS SUSPENDED" in text


def chunked(items: list[Any], size: int = 100) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def safe_label(value: Any, *, prefix: str = "", max_length: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    if prefix:
        text = f"{prefix}_{text}" if text else prefix
    return text[:max_length].strip("_") or (prefix or "odoo")


def margin_tier(margin_percent: float | None) -> str:
    if margin_percent is None:
        return "margin_unknown"
    if margin_percent >= 0.30:
        return "margin_30_plus"
    if margin_percent >= 0.20:
        return "margin_20_plus"
    if margin_percent >= 0.10:
        return "margin_10_plus"
    return "margin_low"


def labels_for_signal(signal: OdooProductPageSignal) -> list[str]:
    base_label = FALLBACK_PAGE_FEED_LABEL if signal.label == "fallback" else PAGE_FEED_LABEL
    labels = [
        base_label,
        safe_label(signal.website_name or f"website_{signal.website_id}", prefix="site", max_length=45),
        safe_label(signal.product_code, prefix="sku", max_length=45),
        margin_tier(signal.margin_percent),
    ]
    return list(dict.fromkeys(label for label in labels if label))


def publication_name(store_name: str, website_name: str, account_name: str) -> str:
    name = f"Odoo {website_name or store_name} Page Feed"
    if len(name) > 120:
        name = name[:120].rstrip()
    return name or f"Odoo Page Feed {safe_label(account_name, max_length=30)}"


def selected_feed_signals(
    session: Session,
    *,
    account_id: int,
    store_id: int | None = None,
    website_id: int | None = None,
    limit: int = MAX_PAGE_FEED_URLS,
) -> list[PageFeedCandidate]:
    filters = [
        OdooProductPageSignal.account_id == account_id,
        OdooProductPageSignal.label.in_(["winner", "fallback"]),
        OdooProductPageSignal.product_url != "",
    ]
    exclusion_filters = [
        OdooProductPageSignal.account_id == account_id,
        OdooProductPageSignal.label == "exclude",
        OdooProductPageSignal.product_url != "",
    ]
    if store_id is not None:
        filters.append(OdooProductPageSignal.store_id == store_id)
        exclusion_filters.append(OdooProductPageSignal.store_id == store_id)
    if website_id is not None:
        filters.append(OdooProductPageSignal.website_id == website_id)
        exclusion_filters.append(OdooProductPageSignal.website_id == website_id)

    excluded_urls = set(
        session.scalars(
            select(OdooProductPageSignal.product_url).where(*exclusion_filters)
        ).all()
    )
    rows = session.scalars(
        select(OdooProductPageSignal)
        .where(*filters)
        .order_by(
            OdooProductPageSignal.label,
            OdooProductPageSignal.margin_amount.desc(),
            OdooProductPageSignal.google_conversion_value.desc(),
        )
        .limit(limit * 3)
    ).all()
    restricted_terms = get_restricted_title_terms_sync(session)
    by_url: dict[str, PageFeedCandidate] = {}
    for signal in rows:
        url = str(signal.product_url or "").strip()
        if not url or url in excluded_urls:
            continue
        if restricted_title_match(signal.product_name, restricted_terms):
            continue
        candidate = PageFeedCandidate(
            signal_id=signal.id,
            store_id=signal.store_id,
            website_id=signal.website_id,
            website_name=signal.website_name,
            page_url=url,
            page_url_hash=page_url_hash(url),
            product_code=signal.product_code,
            product_name=signal.product_name,
            label=signal.label,
            margin_amount=float(signal.margin_amount or 0),
            margin_percent=signal.margin_percent,
            google_conversions=float(signal.google_conversions or 0),
            labels=labels_for_signal(signal),
        )
        current = by_url.get(url)
        if current is None or (candidate.label == "winner" and current.label != "winner"):
            by_url[url] = candidate
    return list(by_url.values())[:limit]


def _upsert_publication(
    session: Session,
    *,
    account: GoogleAdsAccount,
    store: OdooStore,
    website: OdooWebsite | None,
    asset_set_name: str,
) -> GoogleAdsPageFeedPublication:
    website_id = int(website.website_id if website else 0)
    website_name = website.name if website else "All websites"
    stmt = insert(GoogleAdsPageFeedPublication).values(
        account_id=account.id,
        store_id=store.id,
        website_id=website_id,
        website_name=website_name,
        feed_kind="best",
        asset_set_name=asset_set_name,
        status="planned",
        updated_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsPageFeedPublication.account_id,
            GoogleAdsPageFeedPublication.store_id,
            GoogleAdsPageFeedPublication.website_id,
            GoogleAdsPageFeedPublication.feed_kind,
        ],
        set_={
            "website_name": stmt.excluded.website_name,
            "asset_set_name": stmt.excluded.asset_set_name,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    session.execute(stmt)
    session.flush()
    return session.scalar(
        select(GoogleAdsPageFeedPublication).where(
            GoogleAdsPageFeedPublication.account_id == account.id,
            GoogleAdsPageFeedPublication.store_id == store.id,
            GoogleAdsPageFeedPublication.website_id == website_id,
            GoogleAdsPageFeedPublication.feed_kind == "best",
        )
    )


def _upsert_asset_row(
    session: Session,
    publication: GoogleAdsPageFeedPublication,
    candidate: PageFeedCandidate,
) -> GoogleAdsPageFeedAsset:
    stmt = insert(GoogleAdsPageFeedAsset).values(
        publication_id=publication.id,
        signal_id=candidate.signal_id,
        page_url=candidate.page_url,
        page_url_hash=candidate.page_url_hash,
        labels=candidate.labels,
        status="planned",
        source_json={
            "product_code": candidate.product_code,
            "product_name": candidate.product_name,
            "label": candidate.label,
            "margin_amount": candidate.margin_amount,
            "margin_percent": candidate.margin_percent,
            "google_conversions": candidate.google_conversions,
        },
        synced_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[GoogleAdsPageFeedAsset.publication_id, GoogleAdsPageFeedAsset.page_url_hash],
        set_={
            "signal_id": stmt.excluded.signal_id,
            "page_url": stmt.excluded.page_url,
            "labels": stmt.excluded.labels,
            "source_json": stmt.excluded.source_json,
            "synced_at": stmt.excluded.synced_at,
        },
    )
    session.execute(stmt)
    session.flush()
    return session.scalar(
        select(GoogleAdsPageFeedAsset).where(
            GoogleAdsPageFeedAsset.publication_id == publication.id,
            GoogleAdsPageFeedAsset.page_url_hash == candidate.page_url_hash,
        )
    )


def _upsert_campaign_link_row(
    session: Session,
    publication: GoogleAdsPageFeedPublication,
    campaign: CampaignCandidate,
    *,
    status: str,
    last_error: str = "",
) -> GoogleAdsPageFeedCampaignLink:
    stmt = insert(GoogleAdsPageFeedCampaignLink).values(
        publication_id=publication.id,
        campaign_id=campaign.campaign_id,
        campaign_name=campaign.campaign_name,
        campaign_resource_name=campaign.resource_name,
        channel_type=campaign.channel_type,
        status=status,
        last_error=last_error,
        synced_at=utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[GoogleAdsPageFeedCampaignLink.publication_id, GoogleAdsPageFeedCampaignLink.campaign_id],
        set_={
            "campaign_name": stmt.excluded.campaign_name,
            "campaign_resource_name": stmt.excluded.campaign_resource_name,
            "channel_type": stmt.excluded.channel_type,
            "status": stmt.excluded.status,
            "last_error": stmt.excluded.last_error,
            "synced_at": stmt.excluded.synced_at,
        },
    )
    session.execute(stmt)
    session.flush()
    return session.scalar(
        select(GoogleAdsPageFeedCampaignLink).where(
            GoogleAdsPageFeedCampaignLink.publication_id == publication.id,
            GoogleAdsPageFeedCampaignLink.campaign_id == campaign.campaign_id,
        )
    )


def _search_one_asset_set(client: Any, account: GoogleAdsAccount, name: str) -> str:
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          asset_set.resource_name,
          asset_set.name,
          asset_set.type,
          asset_set.status
        FROM asset_set
        WHERE asset_set.name = {gaql_string(name)}
          AND asset_set.type = PAGE_FEED
          AND asset_set.status != REMOVED
        LIMIT 1
    """
    for row in _google_ads_search(ga_service, account, query):
        return row.asset_set.resource_name
    return ""


def _search_assets_by_url(client: Any, account: GoogleAdsAccount, page_urls: list[str]) -> dict[str, str]:
    if not page_urls:
        return {}
    ga_service = client.get_service("GoogleAdsService")
    found: dict[str, str] = {}
    for chunk in chunked(page_urls, 50):
        url_list = ", ".join(gaql_string(url) for url in chunk)
        query = f"""
            SELECT
              asset.resource_name,
              asset.page_feed_asset.page_url
            FROM asset
            WHERE asset.type = PAGE_FEED
              AND asset.page_feed_asset.page_url IN ({url_list})
        """
        for row in _google_ads_search(ga_service, account, query):
            found[str(row.asset.page_feed_asset.page_url)] = row.asset.resource_name
    return found


def _search_asset_set_links(client: Any, account: GoogleAdsAccount, asset_set_resource_name: str) -> dict[str, str]:
    if not asset_set_resource_name:
        return {}
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          asset_set_asset.resource_name,
          asset_set_asset.asset,
          asset_set_asset.asset_set,
          asset_set_asset.status
        FROM asset_set_asset
        WHERE asset_set_asset.asset_set = {gaql_string(asset_set_resource_name)}
          AND asset_set_asset.status != REMOVED
    """
    links: dict[str, str] = {}
    for row in _google_ads_search(ga_service, account, query):
        links[str(row.asset_set_asset.asset)] = row.asset_set_asset.resource_name
    return links


def _pmax_text_automation_opted_in(campaign: Any) -> bool:
    try:
        for setting in campaign.asset_automation_settings:
            automation_type = enum_name(setting.asset_automation_type)
            automation_status = enum_name(setting.asset_automation_status)
            if automation_type == "TEXT_ASSET_AUTOMATION" and automation_status == "OPTED_IN":
                return True
    except Exception:
        return False
    return False


def _campaign_rows(client: Any, account: GoogleAdsAccount, campaign_ids: list[int] | None = None) -> list[CampaignCandidate]:
    ga_service = client.get_service("GoogleAdsService")
    campaign_filter = ""
    if campaign_ids:
        campaign_filter = "AND campaign.id IN (" + ", ".join(str(int(item)) for item in campaign_ids) + ")"
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.dynamic_search_ads_setting.domain_name,
          campaign.asset_automation_settings
        FROM campaign
        WHERE campaign.status != REMOVED
          AND campaign.advertising_channel_type IN (PERFORMANCE_MAX, SEARCH)
          {campaign_filter}
    """
    rows: list[CampaignCandidate] = []
    for row in _google_ads_search(ga_service, account, query):
        channel_type = enum_name(row.campaign.advertising_channel_type)
        domain_name = str(row.campaign.dynamic_search_ads_setting.domain_name or "")
        is_pmax = channel_type == "PERFORMANCE_MAX"
        is_dsa = channel_type == "SEARCH" and bool(domain_name)
        if not is_pmax and not is_dsa:
            continue
        rows.append(
            CampaignCandidate(
                campaign_id=int(row.campaign.id),
                campaign_name=str(row.campaign.name or row.campaign.id),
                resource_name=row.campaign.resource_name,
                channel_type=channel_type,
                is_dsa=is_dsa,
                is_pmax=is_pmax,
                pmax_text_asset_automation_opted_in=_pmax_text_automation_opted_in(row.campaign),
            )
        )
    return rows


def _ad_group_rows_for_campaign(client: Any, account: GoogleAdsAccount, campaign_id: int) -> list[dict[str, Any]]:
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          ad_group.id,
          ad_group.name,
          ad_group.resource_name,
          ad_group.status,
          campaign.id
        FROM ad_group
        WHERE campaign.id = {int(campaign_id)}
          AND ad_group.status != REMOVED
    """
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(ga_service, account, query):
        rows.append(
            {
                "ad_group_id": int(row.ad_group.id),
                "ad_group_name": str(row.ad_group.name or row.ad_group.id),
                "resource_name": row.ad_group.resource_name,
            }
        )
    return rows


def _existing_dsa_label_criteria(client: Any, account: GoogleAdsAccount, campaign_id: int) -> set[str]:
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          ad_group.id,
          ad_group_criterion.resource_name,
          ad_group_criterion.webpage.conditions
        FROM ad_group_criterion
        WHERE campaign.id = {int(campaign_id)}
          AND ad_group_criterion.type = WEBPAGE
          AND ad_group_criterion.status != REMOVED
    """
    ad_group_resource_names: set[str] = set()
    for row in _google_ads_search(ga_service, account, query):
        try:
            for condition in row.ad_group_criterion.webpage.conditions:
                if enum_name(condition.operand) == "CUSTOM_LABEL" and str(condition.argument) == PAGE_FEED_LABEL:
                    ad_group_resource_names.add(row.ad_group.resource_name)
        except Exception:
            continue
    return ad_group_resource_names


def _mutate_asset_set(client: Any, account: GoogleAdsAccount, name: str, validate_only: bool) -> str:
    operation = client.get_type("AssetSetOperation")
    operation.create.name = name
    operation.create.type_ = client.enums.AssetSetTypeEnum.PAGE_FEED
    request = client.get_type("MutateAssetSetsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AssetSetService"), "mutate_asset_sets", request)
    return response.results[0].resource_name if response.results else ""


def _mutate_assets(client: Any, account: GoogleAdsAccount, assets: list[GoogleAdsPageFeedAsset], validate_only: bool) -> dict[int, str]:
    if not assets:
        return {}
    operations = []
    for asset in assets:
        operation = client.get_type("AssetOperation")
        operation.create.page_feed_asset.page_url = asset.page_url
        operation.create.page_feed_asset.labels.extend(list(asset.labels or []))
        operations.append(operation)
    request = client.get_type("MutateAssetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AssetService"), "mutate_assets", request)
    return {
        asset.id: result.resource_name
        for asset, result in zip(assets, response.results)
        if getattr(result, "resource_name", "")
    }


def _mutate_asset_set_asset_links(
    client: Any,
    account: GoogleAdsAccount,
    asset_set_resource_name: str,
    assets: list[GoogleAdsPageFeedAsset],
    validate_only: bool,
) -> dict[int, str]:
    assets = [asset for asset in assets if asset.asset_resource_name]
    if not assets:
        return {}
    operations = []
    for asset in assets:
        operation = client.get_type("AssetSetAssetOperation")
        operation.create.asset_set = asset_set_resource_name
        operation.create.asset = asset.asset_resource_name
        operations.append(operation)
    request = client.get_type("MutateAssetSetAssetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AssetSetAssetService"), "mutate_asset_set_assets", request)
    return {
        asset.id: result.resource_name
        for asset, result in zip(assets, response.results)
        if getattr(result, "resource_name", "")
    }


def _mutate_campaign_asset_set_links(
    client: Any,
    account: GoogleAdsAccount,
    asset_set_resource_name: str,
    links: list[GoogleAdsPageFeedCampaignLink],
    validate_only: bool,
) -> dict[int, str]:
    if not links:
        return {}
    operations = []
    for link in links:
        operation = client.get_type("CampaignAssetSetOperation")
        operation.create.campaign = link.campaign_resource_name
        operation.create.asset_set = asset_set_resource_name
        operations.append(operation)
    request = client.get_type("MutateCampaignAssetSetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignAssetSetService"), "mutate_campaign_asset_sets", request)
    return {
        link.id: result.resource_name
        for link, result in zip(links, response.results)
        if getattr(result, "resource_name", "")
    }


def _mutate_dsa_label_criteria(
    client: Any,
    account: GoogleAdsAccount,
    ad_groups: list[dict[str, Any]],
    validate_only: bool,
) -> list[str]:
    if not ad_groups:
        return []
    operations = []
    for ad_group in ad_groups:
        operation = client.get_type("AdGroupCriterionOperation")
        operation.create.ad_group = ad_group["resource_name"]
        operation.create.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        operation.create.webpage.criterion_name = DSA_CRITERION_NAME
        condition = client.get_type("WebpageConditionInfo")
        condition.operand = client.enums.WebpageConditionOperandEnum.CUSTOM_LABEL
        condition.operator = client.enums.WebpageConditionOperatorEnum.EQUALS
        condition.argument = PAGE_FEED_LABEL
        operation.create.webpage.conditions.append(condition)
        operations.append(operation)
    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupCriterionService"), "mutate_ad_group_criteria", request)
    return [result.resource_name for result in response.results if getattr(result, "resource_name", "")]


def publish_mapping_page_feed(
    session: Session,
    mapping: OdooStoreGoogleAdsMapping,
    *,
    validate_only: Optional[bool] = None,
    max_urls: int = MAX_PAGE_FEED_URLS,
    campaign_ids: list[int] | None = None,
    create_dsa_criteria: bool = False,
    job_id: int | None = None,
) -> dict[str, Any]:
    account = mapping.account
    store = mapping.store
    website = None
    if mapping.website_id:
        website = session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.store_id == store.id,
                OdooWebsite.website_id == mapping.website_id,
                OdooWebsite.is_active.is_(True),
            )
        )
    website_id = int(website.website_id if website else mapping.website_id or 0)
    website_name = website.name if website else "All websites"
    settings = get_sync_setting_map(session)
    can_mutate = parse_bool(settings.get("optimizer.allow_mutations", False)) and not parse_bool(settings.get("optimizer.dry_run", True))
    should_validate_only = bool(validate_only) if validate_only is not None else not can_mutate
    if not can_mutate:
        should_validate_only = True

    candidates = selected_feed_signals(
        session,
        account_id=account.id,
        store_id=store.id,
        website_id=website_id,
        limit=max_urls,
    )
    asset_set_name = publication_name(store.name, website_name, account.name)
    publication = _upsert_publication(
        session,
        account=account,
        store=store,
        website=website,
        asset_set_name=asset_set_name,
    )
    asset_rows = [_upsert_asset_row(session, publication, candidate) for candidate in candidates]
    session.commit()

    result: dict[str, Any] = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "store_id": store.id,
        "website_id": website_id,
        "website_name": website_name,
        "validate_only": should_validate_only,
        "can_mutate": can_mutate,
        "asset_set_name": asset_set_name,
        "selected_urls": len(candidates),
        "created_or_reused_assets": 0,
        "asset_set_asset_links": 0,
        "asset_set_asset_links_planned": 0,
        "campaign_links": 0,
        "campaign_links_planned": 0,
        "dsa_criteria": 0,
        "blocked_campaigns": [],
        "errors": [],
    }
    red_flag = account_api_red_flag(session, account)
    if red_flag is not None:
        publication.status = "blocked_by_account_red_flag"
        publication.last_error = red_flag["reason"]
        result.update({"status": "blocked_by_account_red_flag", "reason": red_flag["reason"], "red_flag": red_flag})
        publication.last_publish_json = result
        publication.updated_at = utcnow()
        session.commit()
        return result
    if not candidates:
        publication.status = "skipped"
        publication.last_error = "No winner or fallback Odoo page feed signals are available."
        publication.last_publish_json = result
        publication.updated_at = utcnow()
        session.commit()
        return result

    client = build_client(settings, account.manager_customer_id, account.connection)
    try:
        asset_set_resource_name = publication.asset_set_resource_name
        if not asset_set_resource_name:
            asset_set_resource_name = _search_one_asset_set(client, account, asset_set_name)
        if not asset_set_resource_name:
            asset_set_resource_name = _mutate_asset_set(client, account, asset_set_name, should_validate_only)
        if asset_set_resource_name and not should_validate_only:
            publication.asset_set_resource_name = asset_set_resource_name

        missing_asset_urls = [asset.page_url for asset in asset_rows if not asset.asset_resource_name]
        existing_assets = _search_assets_by_url(client, account, missing_asset_urls) if missing_asset_urls else {}
        for asset in asset_rows:
            existing_resource_name = asset.asset_resource_name or existing_assets.get(asset.page_url, "")
            if existing_resource_name:
                asset.asset_resource_name = existing_resource_name
                asset.status = "reused"

        assets_to_create = [asset for asset in asset_rows if not asset.asset_resource_name]
        created_assets = _mutate_assets(client, account, assets_to_create, should_validate_only)
        for asset in assets_to_create:
            resource_name = created_assets.get(asset.id, "")
            if resource_name and not should_validate_only:
                asset.asset_resource_name = resource_name
                asset.status = "published"
            elif should_validate_only:
                asset.status = "validated"
        result["created_or_reused_assets"] = len(asset_rows)

        if asset_set_resource_name:
            assets_missing_links = [
                asset
                for asset in asset_rows
                if asset.asset_resource_name and not asset.asset_set_asset_resource_name
            ]
            existing_links = _search_asset_set_links(client, account, asset_set_resource_name) if assets_missing_links else {}
            link_assets: list[GoogleAdsPageFeedAsset] = []
            for asset in asset_rows:
                if asset.asset_set_asset_resource_name:
                    asset.status = "linked"
                elif asset.asset_resource_name and asset.asset_resource_name in existing_links:
                    asset.asset_set_asset_resource_name = existing_links[asset.asset_resource_name]
                    asset.status = "linked"
                elif asset.asset_resource_name or should_validate_only:
                    link_assets.append(asset)
            created_links = _mutate_asset_set_asset_links(
                client,
                account,
                asset_set_resource_name,
                link_assets,
                should_validate_only,
            )
            for asset in link_assets:
                link_resource_name = created_links.get(asset.id, "")
                if link_resource_name and not should_validate_only:
                    asset.asset_set_asset_resource_name = link_resource_name
                    asset.status = "linked"
                elif should_validate_only:
                    asset.status = "validated"
            result["asset_set_asset_links"] = len(link_assets)
        else:
            result["asset_set_asset_links_planned"] = len(asset_rows)

        campaigns = _campaign_rows(client, account, campaign_ids=campaign_ids)
        campaign_links_to_create: list[GoogleAdsPageFeedCampaignLink] = []
        for campaign in campaigns:
            if campaign.is_pmax and not campaign.pmax_text_asset_automation_opted_in:
                blocked = {
                    "campaign_id": campaign.campaign_id,
                    "campaign_name": campaign.campaign_name,
                    "reason": "PMax TEXT_ASSET_AUTOMATION is not opted in.",
                }
                result["blocked_campaigns"].append(blocked)
                _upsert_campaign_link_row(
                    session,
                    publication,
                    campaign,
                    status="blocked",
                    last_error=blocked["reason"],
                )
                continue
            link = _upsert_campaign_link_row(session, publication, campaign, status="planned")
            if not link.campaign_asset_set_resource_name:
                campaign_links_to_create.append(link)

        if campaign_links_to_create and asset_set_resource_name:
            created_campaign_links = _mutate_campaign_asset_set_links(
                client,
                account,
                asset_set_resource_name,
                campaign_links_to_create,
                should_validate_only,
            )
            for link in campaign_links_to_create:
                resource_name = created_campaign_links.get(link.id, "")
                if resource_name and not should_validate_only:
                    link.campaign_asset_set_resource_name = resource_name
                    link.status = "linked"
                elif should_validate_only:
                    link.status = "validated"
            result["campaign_links"] = len(campaign_links_to_create)
        elif campaign_links_to_create:
            result["campaign_links_planned"] = len(campaign_links_to_create)

        if create_dsa_criteria:
            for campaign in campaigns:
                if not campaign.is_dsa:
                    continue
                existing_ad_groups = _existing_dsa_label_criteria(client, account, campaign.campaign_id)
                ad_groups = [
                    row
                    for row in _ad_group_rows_for_campaign(client, account, campaign.campaign_id)
                    if row["resource_name"] not in existing_ad_groups
                ]
                created_criteria = _mutate_dsa_label_criteria(client, account, ad_groups, should_validate_only)
                result["dsa_criteria"] += len(ad_groups)
                link = session.scalar(
                    select(GoogleAdsPageFeedCampaignLink).where(
                        GoogleAdsPageFeedCampaignLink.publication_id == publication.id,
                        GoogleAdsPageFeedCampaignLink.campaign_id == campaign.campaign_id,
                    )
                )
                if link is not None and created_criteria and not should_validate_only:
                    names = list(link.dsa_criterion_resource_names or [])
                    names.extend(created_criteria)
                    link.dsa_criterion_resource_names = list(dict.fromkeys(names))

        publication.status = "validated" if should_validate_only else "published"
        publication.last_error = ""
        publication.last_publish_json = result
        publication.updated_at = utcnow()
        session.commit()
        return result
    except GoogleAdsException as exc:
        if _is_suspended_account_error(exc):
            upsert_account_api_red_flag(
                session,
                account,
                status="blocked_by_suspended_account",
                reason="Google Ads rejected page-feed publishing with ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT.",
                source="page_feed_publish",
            )
        record_google_ads_api_error(
            session,
            exc,
            account=account,
            job_id=job_id,
            context="page_feed_publish",
            severity="manual_action_required",
            extra={
                "action_type": "publish_page_feed",
                "store_id": store.id,
                "website_id": website_id,
                "asset_set_name": asset_set_name,
            },
        )
        publication = session.get(GoogleAdsPageFeedPublication, publication.id)
        if publication is not None:
            publication.status = "failed"
            publication.last_error = str(exc)
            publication.last_publish_json = result
            publication.updated_at = utcnow()
            session.commit()
        raise
    except Exception as exc:
        if _is_suspended_account_error(exc):
            upsert_account_api_red_flag(
                session,
                account,
                status="blocked_by_suspended_account",
                reason="Google Ads rejected page-feed publishing with ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT.",
                source="page_feed_publish",
            )
        record_google_ads_generic_error(
            session,
            exc,
            account=account,
            job_id=job_id,
            context="page_feed_publish",
            severity="manual_action_required",
            extra={
                "action_type": "publish_page_feed",
                "store_id": store.id,
                "website_id": website_id,
                "asset_set_name": asset_set_name,
            },
        )
        publication = session.get(GoogleAdsPageFeedPublication, publication.id)
        if publication is not None:
            publication.status = "failed"
            publication.last_error = str(exc)
            publication.last_publish_json = result
            publication.updated_at = utcnow()
            session.commit()
        raise


def publish_page_feeds_for_mappings(
    session: Session,
    *,
    store_ids: list[int] | None = None,
    account_ids: list[int] | None = None,
    website_ids: list[int] | None = None,
    validate_only: Optional[bool] = None,
    max_urls: int = MAX_PAGE_FEED_URLS,
    campaign_ids: list[int] | None = None,
    create_dsa_criteria: bool = False,
    job_id: int | None = None,
) -> dict[str, Any]:
    query = select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
    if store_ids:
        query = query.where(OdooStoreGoogleAdsMapping.store_id.in_([int(item) for item in store_ids]))
    if account_ids:
        query = query.where(OdooStoreGoogleAdsMapping.account_id.in_([int(item) for item in account_ids]))
    if website_ids:
        query = query.where(OdooStoreGoogleAdsMapping.website_id.in_([int(item) for item in website_ids]))
    mappings = session.scalars(query.order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)).all()
    results = []
    errors = []
    for mapping in mappings:
        try:
            results.append(
                publish_mapping_page_feed(
                    session,
                    mapping,
                    validate_only=validate_only,
                    max_urls=max_urls,
                    campaign_ids=campaign_ids,
                    create_dsa_criteria=create_dsa_criteria,
                    job_id=job_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - move mapping by mapping and surface failures.
            session.rollback()
            errors.append({"mapping_id": mapping.id, "error": str(exc)})
    return {
        "mappings": len(mappings),
        "results": results,
        "errors": errors,
    }
