from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import GoogleAdsAccount, GoogleAdsGeneratedAsset


PUBLISHABLE_ASSET_TYPES = {
    "business_name",
    "sitelink",
    "callout",
    "structured_snippet",
    "price",
    "promotion",
    "business_message",
}

PUBLISH_READY_STATUSES = {"draft", "publish_failed", "validated"}
REMOVE_READY_STATUSES = {"pending_remove"}

ASSET_FIELD_TYPES = {
    "business_name": "BUSINESS_NAME",
    "sitelink": "SITELINK",
    "callout": "CALLOUT",
    "structured_snippet": "STRUCTURED_SNIPPET",
    "price": "PRICE",
    "promotion": "PROMOTION",
    "business_message": "BUSINESS_MESSAGE",
}
PRE_MUTATE_LINK_LIMITS = {
    "callout": 20,
    "sitelink": 20,
    "structured_snippet": 20,
    "price": 20,
}
INTERNAL_AD_COPY_TERMS = {
    "conversion signal",
    "google conversion signal",
    "odoo sales signal",
    "popular recent seller",
    "strong margin signal",
    "useful product page",
    "real customer demand",
    "demand signal",
    "saved signal",
    "sales signal",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _gaql_string(value: str) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def _google_ads_search(service: Any, account: GoogleAdsAccount, query: str, *, timeout: int = 30) -> Any:
    try:
        return service.search(customer_id=account.customer_id, query=query, timeout=timeout)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return service.search(customer_id=account.customer_id, query=query)


def _micros(value: Any) -> int:
    try:
        return int(round(float(value or 0) * 1_000_000))
    except (TypeError, ValueError):
        return 0


def _enum_value(enum: Any, name: str, default: str = "UNSPECIFIED") -> Any:
    try:
        return getattr(enum, str(name or default).upper())
    except Exception:  # noqa: BLE001 - generated enum wrappers vary by version.
        return getattr(enum, default)


def _field_type(client: Any, asset_type: str) -> Any:
    return _enum_value(client.enums.AssetFieldTypeEnum, ASSET_FIELD_TYPES[asset_type])


def _structured_snippet_header(value: Any) -> str:
    text = str(value or "").strip().title()
    allowed = {
        "Amenities",
        "Brands",
        "Courses",
        "Degree Programs",
        "Destinations",
        "Featured Hotels",
        "Insurance Coverage",
        "Models",
        "Neighborhoods",
        "Service Catalog",
        "Shows",
        "Styles",
        "Types",
    }
    if text in allowed:
        return text
    if text in {"Categories", "Category", "Product Categories", "Products"}:
        return "Types"
    return "Types"


def _structured_snippet_value(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = " ".join(
        word[:1].upper() + word[1:].lower() if re.search(r"[A-Za-z]", word) else word
        for word in text.split(" ")
    )
    key = re.sub(r"[^a-z0-9]+", "", text.lower())
    if len(text) < 3 or len(text) > 25:
        return ""
    if key in {"all", "com", "www", "none", "misc", "other", "category", "categories", "false", "true"}:
        return ""
    if not re.search(r"[A-Za-z]", text):
        return ""
    if _has_internal_copy(text):
        return ""
    return text[:25]


def _public_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = " ".join(
        word[:1].upper() + word[1:].lower() if re.search(r"[A-Za-z]", word) else word
        for word in text.split(" ")
    )
    text = re.sub(r"\b(Ca|Au|Us|Nz)\$", lambda match: match.group(1).upper() + "$", text)
    return text[:limit].strip()


def _safe_public_text(value: Any, limit: int, *, fallback: str = "") -> str:
    text = _public_text(value, limit)
    if not text or _has_internal_copy(text):
        text = _public_text(fallback, limit)
    if _has_internal_copy(text):
        return ""
    return text


def _has_internal_copy(value: Any) -> bool:
    key = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return any(term in key for term in INTERNAL_AD_COPY_TERMS)


def _campaign_resource_by_name(client: Any, account: GoogleAdsAccount, name: str) -> str:
    if not name:
        return ""
    service = client.get_service("GoogleAdsService")
    code_match = re.search(r"\bAUTO-[A-Z0-9]{10}\b", name)
    if code_match:
        query = f"""
            SELECT campaign.resource_name
            FROM campaign
            WHERE campaign.name LIKE {_gaql_string('%' + code_match.group(0) + '%')}
              AND campaign.status != REMOVED
            LIMIT 1
        """
        for row in _google_ads_search(service, account, query):
            return str(row.campaign.resource_name or "")
    query = f"""
        SELECT campaign.resource_name
        FROM campaign
        WHERE campaign.name = {_gaql_string(name)}
          AND campaign.status != REMOVED
        LIMIT 1
    """
    for row in _google_ads_search(service, account, query):
        return str(row.campaign.resource_name or "")
    return ""


def _ad_group_resource_by_name(client: Any, account: GoogleAdsAccount, campaign_name: str, ad_group_name: str) -> str:
    if not campaign_name or not ad_group_name:
        return ""
    service = client.get_service("GoogleAdsService")
    code_match = re.search(r"\bAUTO-[A-Z0-9]{10}\b", campaign_name)
    if code_match:
        query = f"""
            SELECT ad_group.resource_name
            FROM ad_group
            WHERE campaign.name LIKE {_gaql_string('%' + code_match.group(0) + '%')}
              AND ad_group.name = {_gaql_string(ad_group_name)}
              AND campaign.status != REMOVED
              AND ad_group.status != REMOVED
            LIMIT 1
        """
        for row in _google_ads_search(service, account, query):
            return str(row.ad_group.resource_name or "")
    query = f"""
        SELECT ad_group.resource_name
        FROM ad_group
        WHERE campaign.name = {_gaql_string(campaign_name)}
          AND ad_group.name = {_gaql_string(ad_group_name)}
          AND campaign.status != REMOVED
          AND ad_group.status != REMOVED
        LIMIT 1
    """
    for row in _google_ads_search(service, account, query):
        return str(row.ad_group.resource_name or "")
    return ""


def _is_already_exists_error(exc: GoogleAdsException) -> bool:
    text = str(exc).lower()
    return (
        "resource being created already exists" in text
        or "already exists" in text
        or "data_constraint_violation" in text
        or "conflicted with existing data" in text
    )


def _is_limit_exceeded_error(exc: GoogleAdsException) -> bool:
    text = str(exc).lower()
    return (
        "limit on the number of allowed resources" in text
        or "resource_count_limit_exceeded" in text
        or "resource has been exhausted" in text
        or "quota" in text
    )


def _is_quota_exhausted_error(exc: Any) -> bool:
    text = str(exc).lower()
    return "too many requests" in text or "resource has been exhausted" in text or "retry in" in text


def _retry_after_seconds(exc: Any) -> int:
    match = re.search(r"retry in\s+(\d+)\s+seconds", str(exc), flags=re.I)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _asset_link_exists(
    client: Any,
    account: GoogleAdsAccount,
    *,
    scope_level: str,
    scope_resource_name: str,
    asset_resource_name: str,
    field_type_name: str,
) -> bool:
    service = client.get_service("GoogleAdsService")
    if scope_level == "account":
        query = f"""
            SELECT customer_asset.resource_name
            FROM customer_asset
            WHERE customer_asset.asset = {_gaql_string(asset_resource_name)}
              AND customer_asset.field_type = {field_type_name}
              AND customer_asset.status != REMOVED
            LIMIT 1
        """
    elif scope_level == "campaign":
        query = f"""
            SELECT campaign_asset.resource_name
            FROM campaign_asset
            WHERE campaign_asset.campaign = {_gaql_string(scope_resource_name)}
              AND campaign_asset.asset = {_gaql_string(asset_resource_name)}
              AND campaign_asset.field_type = {field_type_name}
              AND campaign_asset.status != REMOVED
            LIMIT 1
        """
    elif scope_level == "ad_group":
        query = f"""
            SELECT ad_group_asset.resource_name
            FROM ad_group_asset
            WHERE ad_group_asset.ad_group = {_gaql_string(scope_resource_name)}
              AND ad_group_asset.asset = {_gaql_string(asset_resource_name)}
              AND ad_group_asset.field_type = {field_type_name}
              AND ad_group_asset.status != REMOVED
            LIMIT 1
        """
    else:
        return False
    return any(_google_ads_search(service, account, query))


def _asset_link_count(
    client: Any,
    account: GoogleAdsAccount,
    *,
    scope_level: str,
    scope_resource_name: str,
    field_type_name: str,
    limit: int,
) -> int:
    service = client.get_service("GoogleAdsService")
    row_limit = max(int(limit or 1), 1) + 1
    if scope_level == "account":
        query = f"""
            SELECT customer_asset.resource_name
            FROM customer_asset
            WHERE customer_asset.field_type = {field_type_name}
              AND customer_asset.status != REMOVED
            LIMIT {row_limit}
        """
        return sum(1 for _ in _google_ads_search(service, account, query))
    if scope_level == "campaign":
        query = f"""
            SELECT campaign_asset.resource_name
            FROM campaign_asset
            WHERE campaign_asset.campaign = {_gaql_string(scope_resource_name)}
              AND campaign_asset.field_type = {field_type_name}
              AND campaign_asset.status != REMOVED
            LIMIT {row_limit}
        """
        return sum(1 for _ in _google_ads_search(service, account, query))
    if scope_level == "ad_group":
        query = f"""
            SELECT ad_group_asset.resource_name
            FROM ad_group_asset
            WHERE ad_group_asset.ad_group = {_gaql_string(scope_resource_name)}
              AND ad_group_asset.field_type = {field_type_name}
              AND ad_group_asset.status != REMOVED
            LIMIT {row_limit}
        """
        return sum(1 for _ in _google_ads_search(service, account, query))
    return 0


def _asset_link_resource_names(
    client: Any,
    account: GoogleAdsAccount,
    *,
    scope_level: str,
    scope_resource_name: str,
    asset_resource_name: str,
    field_type_name: str,
) -> list[str]:
    service = client.get_service("GoogleAdsService")
    if scope_level == "account":
        query = f"""
            SELECT customer_asset.resource_name
            FROM customer_asset
            WHERE customer_asset.asset = {_gaql_string(asset_resource_name)}
              AND customer_asset.field_type = {field_type_name}
              AND customer_asset.status != REMOVED
        """
        return [str(row.customer_asset.resource_name or "") for row in _google_ads_search(service, account, query)]
    if scope_level == "campaign":
        query = f"""
            SELECT campaign_asset.resource_name
            FROM campaign_asset
            WHERE campaign_asset.campaign = {_gaql_string(scope_resource_name)}
              AND campaign_asset.asset = {_gaql_string(asset_resource_name)}
              AND campaign_asset.field_type = {field_type_name}
              AND campaign_asset.status != REMOVED
        """
        return [str(row.campaign_asset.resource_name or "") for row in _google_ads_search(service, account, query)]
    if scope_level == "ad_group":
        query = f"""
            SELECT ad_group_asset.resource_name
            FROM ad_group_asset
            WHERE ad_group_asset.ad_group = {_gaql_string(scope_resource_name)}
              AND ad_group_asset.asset = {_gaql_string(asset_resource_name)}
              AND ad_group_asset.field_type = {field_type_name}
              AND ad_group_asset.status != REMOVED
        """
        return [str(row.ad_group_asset.resource_name or "") for row in _google_ads_search(service, account, query)]
    return []


def _remove_link(
    client: Any,
    account: GoogleAdsAccount,
    *,
    scope_level: str,
    link_resource_name: str,
    validate_only: bool,
) -> None:
    if not link_resource_name:
        return
    if scope_level == "account":
        operation = client.get_type("CustomerAssetOperation")
        operation.remove = link_resource_name
        request = client.get_type("MutateCustomerAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        client.get_service("CustomerAssetService").mutate_customer_assets(request=request, timeout=30)
        return
    if scope_level == "campaign":
        operation = client.get_type("CampaignAssetOperation")
        operation.remove = link_resource_name
        request = client.get_type("MutateCampaignAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        client.get_service("CampaignAssetService").mutate_campaign_assets(request=request, timeout=30)
        return
    if scope_level == "ad_group":
        operation = client.get_type("AdGroupAssetOperation")
        operation.remove = link_resource_name
        request = client.get_type("MutateAdGroupAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        client.get_service("AdGroupAssetService").mutate_ad_group_assets(request=request, timeout=30)


def _mutate_asset(client: Any, account: GoogleAdsAccount, row: GoogleAdsGeneratedAsset, *, validate_only: bool) -> str:
    payload = row.payload_json if isinstance(row.payload_json, dict) else {}
    operation = client.get_type("AssetOperation")
    asset = operation.create
    asset.name = (row.name or f"AUTO {row.asset_type} {row.id}")[:255]
    if row.asset_type == "business_name":
        business_name = _safe_public_text(payload.get("business_name") or row.name or account.name, 25, fallback=account.name or "Business")
        if not business_name:
            raise ValueError("Business-name asset is missing business_name.")
        asset.text_asset.text = business_name
    elif row.asset_type == "sitelink":
        final_url = str(payload.get("final_url") or "").strip()
        if not final_url:
            raise ValueError("Sitelink asset is missing final_url.")
        asset.final_urls.append(final_url)
        asset.sitelink_asset.link_text = _safe_public_text(payload.get("link_text") or row.name or "", 25, fallback="Shop Products")
        desc1 = str(payload.get("description1") or "").strip()
        desc2 = str(payload.get("description2") or "").strip()
        asset.sitelink_asset.description1 = _safe_public_text(desc1, 35, fallback="Shop online")
        asset.sitelink_asset.description2 = _safe_public_text(desc2, 35, fallback="Product details")
    elif row.asset_type == "callout":
        callout_text = _safe_public_text(payload.get("callout_text") or row.name or "", 25, fallback="Shop Online")
        if not callout_text:
            raise ValueError("Callout asset is missing safe public text.")
        asset.callout_asset.callout_text = callout_text
    elif row.asset_type == "structured_snippet":
        values: list[str] = []
        seen_values: set[str] = set()
        for item in payload.get("values") or []:
            value = _structured_snippet_value(item)
            key = value.lower()
            if value and key not in seen_values:
                seen_values.add(key)
                values.append(value)
        if len(values) < 3:
            raise ValueError("Structured snippet needs at least 3 values.")
        asset.structured_snippet_asset.header = _structured_snippet_header(payload.get("header") or "Types")
        asset.structured_snippet_asset.values.extend(values[:10])
    elif row.asset_type == "price":
        offerings = [item for item in (payload.get("price_offerings") or []) if isinstance(item, dict)]
        if len(offerings) < 3:
            raise ValueError("Price asset needs at least 3 offerings.")
        price_asset = asset.price_asset
        price_asset.type_ = _enum_value(client.enums.PriceExtensionTypeEnum, payload.get("type") or "PRODUCT_CATEGORIES")
        price_asset.price_qualifier = _enum_value(
            client.enums.PriceExtensionPriceQualifierEnum,
            payload.get("price_qualifier") or "FROM",
        )
        price_asset.language_code = str(payload.get("language_code") or "en")[:5]
        for item in offerings[:8]:
            final_url = str(item.get("final_url") or "").strip()
            amount_micros = _micros(item.get("price"))
            currency_code = str(item.get("currency_code") or payload.get("currency_code") or account.currency_code or "").upper()
            if not final_url or amount_micros <= 0 or not currency_code:
                continue
            offering = client.get_type("PriceOffering")
            offering.header = _safe_public_text(item.get("header") or "", 25, fallback="Products")
            description = item.get("description") or "Browse products"
            if _has_internal_copy(description):
                description = "Browse products"
            offering.description = _safe_public_text(description, 25, fallback="Browse products")
            offering.final_url = final_url
            offering.price.amount_micros = amount_micros
            offering.price.currency_code = currency_code
            offering.unit = _enum_value(client.enums.PriceExtensionPriceUnitEnum, item.get("unit") or "UNSPECIFIED")
            price_asset.price_offerings.append(offering)
        if len(price_asset.price_offerings) < 3:
            raise ValueError("Price asset has fewer than 3 valid offerings after filtering.")
    elif row.asset_type == "business_message":
        provider = str(payload.get("provider") or "WHATSAPP").upper()
        phone = re.sub(r"\\D+", "", str(payload.get("phone_number") or ""))
        if provider != "WHATSAPP" or not phone:
            raise ValueError("Only WhatsApp business-message assets with a phone number are supported.")
        bm = asset.business_message_asset
        bm.message_provider = _enum_value(client.enums.BusinessMessageProviderEnum, "WHATSAPP")
        bm.starter_message = str(payload.get("starter_message") or "Can I get help choosing a product?")[:140]
        bm.call_to_action = _enum_value(
            client.enums.BusinessMessageCallToActionTypeEnum,
            payload.get("call_to_action") if payload.get("call_to_action") != "MESSAGE" else "CONTACT_US",
            default="CONTACT_US",
        )
        bm.whatsapp_info.country_code = str(payload.get("country_code") or "+1").replace("+", "")[:8]
        bm.whatsapp_info.phone_number = phone[:40]
    elif row.asset_type == "promotion":
        raise ValueError("Promotion asset payload has no normalized discount amount yet; generation is kept in draft.")
    else:
        raise ValueError(f"Unsupported generated asset type: {row.asset_type}")

    request = client.get_type("MutateAssetsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = client.get_service("AssetService").mutate_assets(request=request, timeout=30)
    return str(response.results[0].resource_name or "")


def _mutate_link(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_type: str,
    asset_resource_name: str,
    scope_level: str,
    scope_resource_name: str = "",
    validate_only: bool,
) -> str:
    if scope_level == "account":
        operation = client.get_type("CustomerAssetOperation")
        link = operation.create
        link.asset = asset_resource_name
        link.field_type = _field_type(client, asset_type)
        request = client.get_type("MutateCustomerAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        response = client.get_service("CustomerAssetService").mutate_customer_assets(request=request, timeout=30)
        return str(response.results[0].resource_name or "")
    if scope_level == "campaign":
        if not scope_resource_name:
            raise ValueError("Campaign-scoped asset is missing campaign resource.")
        operation = client.get_type("CampaignAssetOperation")
        link = operation.create
        link.campaign = scope_resource_name
        link.asset = asset_resource_name
        link.field_type = _field_type(client, asset_type)
        request = client.get_type("MutateCampaignAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        response = client.get_service("CampaignAssetService").mutate_campaign_assets(request=request, timeout=30)
        return str(response.results[0].resource_name or "")
    if scope_level == "ad_group":
        if not scope_resource_name:
            raise ValueError("Ad-group-scoped asset is missing ad group resource.")
        operation = client.get_type("AdGroupAssetOperation")
        link = operation.create
        link.ad_group = scope_resource_name
        link.asset = asset_resource_name
        link.field_type = _field_type(client, asset_type)
        request = client.get_type("MutateAdGroupAssetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        response = client.get_service("AdGroupAssetService").mutate_ad_group_assets(request=request, timeout=30)
        return str(response.results[0].resource_name or "")
    raise ValueError(f"Unsupported asset scope: {scope_level}")


def publish_generated_assets(
    session: Session,
    client: Any,
    account: GoogleAdsAccount,
    *,
    validate_only: bool = False,
    max_assets: int = 500,
) -> dict[str, Any]:
    settings_map = get_sync_setting_map(session)
    defer_non_account_links = parse_bool(settings_map.get("asset_publisher.defer_campaign_and_ad_group_links", True))
    rows = session.scalars(
        select(GoogleAdsGeneratedAsset)
        .where(
            GoogleAdsGeneratedAsset.account_id == account.id,
            GoogleAdsGeneratedAsset.asset_type.in_(sorted(PUBLISHABLE_ASSET_TYPES)),
            GoogleAdsGeneratedAsset.status.in_(sorted(PUBLISH_READY_STATUSES | REMOVE_READY_STATUSES)),
        )
        .order_by(GoogleAdsGeneratedAsset.asset_type, GoogleAdsGeneratedAsset.id)
        .limit(max(1, int(max_assets or 500)))
    ).all()
    result = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "considered": len(rows),
        "created": 0,
        "linked": 0,
        "existing_links": 0,
        "removed_links": 0,
        "skipped": 0,
        "failed": 0,
        "quota_exhausted": False,
        "quota_retry_after_seconds": 0,
        "by_type": {},
        "errors": [],
    }
    campaign_resource_cache: dict[str, str] = {}
    ad_group_resource_cache: dict[tuple[str, str], str] = {}
    capped_link_scopes: set[tuple[str, str, str]] = set()
    link_count_cache: dict[tuple[str, str, str], int] = {}
    for row in rows:
        result["by_type"][row.asset_type] = int(result["by_type"].get(row.asset_type, 0)) + 1
        try:
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {"level": "account"}
            scope_level = str(scope.get("level") or "account").strip()
            if defer_non_account_links and scope_level in {"campaign", "ad_group"}:
                result["skipped"] += 1
                result["errors"].append(
                    {
                        "asset_id": row.id,
                        "asset_type": row.asset_type,
                        "status": "deferred_non_account_scope",
                        "scope_level": scope_level,
                    }
                )
                continue
            if row.asset_type == "promotion":
                result["skipped"] += 1
                continue
            scope_resource = ""
            if scope_level == "campaign":
                campaign_name = str(scope.get("campaign_name") or row.campaign_name or "").strip()
                if row.asset_type == "business_name" and "pmax" in campaign_name.lower():
                    row.status = "publish_limited"
                    row.last_error = "Skipped campaign-level PMax business-name link; Google requires PMax business assets at asset-group level unless Brand Guidelines is enabled."
                    row.updated_at = _utcnow()
                    session.commit()
                    result["skipped"] += 1
                    continue
                if campaign_name not in campaign_resource_cache:
                    campaign_resource_cache[campaign_name] = _campaign_resource_by_name(client, account, campaign_name)
                scope_resource = campaign_resource_cache[campaign_name]
                if not scope_resource:
                    row.status = "source_removed"
                    row.last_error = f"Skipped stale asset scope because campaign no longer exists: {campaign_name}"
                    row.updated_at = _utcnow()
                    session.commit()
                    result["skipped"] += 1
                    continue
            elif scope_level == "ad_group":
                campaign_name = str(scope.get("campaign_name") or row.campaign_name or "").strip()
                ad_group_name = str(scope.get("ad_group_name") or "").strip()
                key = (campaign_name, ad_group_name)
                if key not in ad_group_resource_cache:
                    ad_group_resource_cache[key] = _ad_group_resource_by_name(client, account, campaign_name, ad_group_name)
                scope_resource = ad_group_resource_cache[key]
                if not scope_resource:
                    row.status = "source_removed"
                    row.last_error = f"Skipped stale asset scope because campaign/ad group no longer exists: {campaign_name} / {ad_group_name}"
                    row.updated_at = _utcnow()
                    session.commit()
                    result["skipped"] += 1
                    continue
            scope_key = (scope_level, row.asset_type, scope_resource or "account")
            if scope_key in capped_link_scopes and row.status not in REMOVE_READY_STATUSES:
                row.status = "publish_limited"
                row.last_error = f"Skipped because Google Ads reported the {scope_level} {row.asset_type} asset-link limit is already reached."
                row.updated_at = _utcnow()
                session.commit()
                result["skipped"] += 1
                continue
            field_type_name = ASSET_FIELD_TYPES[row.asset_type]
            pre_limit = PRE_MUTATE_LINK_LIMITS.get(row.asset_type)
            if pre_limit and row.status not in REMOVE_READY_STATUSES:
                if scope_key not in link_count_cache:
                    link_count_cache[scope_key] = _asset_link_count(
                        client,
                        account,
                        scope_level=scope_level,
                        scope_resource_name=scope_resource,
                        field_type_name=field_type_name,
                        limit=pre_limit,
                    )
                if link_count_cache[scope_key] >= pre_limit:
                    capped_link_scopes.add(scope_key)
                    row.status = "publish_limited"
                    row.last_error = f"Skipped before mutation because Google Ads already has {link_count_cache[scope_key]} active {scope_level} {row.asset_type} links; local limit is {pre_limit}."
                    row.updated_at = _utcnow()
                    session.commit()
                    result["skipped"] += 1
                    continue
            replaced_resource = str(payload.get("replaced_google_resource_name") or "").strip()
            if replaced_resource:
                for link_resource in _asset_link_resource_names(
                    client,
                    account,
                    scope_level=scope_level,
                    scope_resource_name=scope_resource,
                    asset_resource_name=replaced_resource,
                    field_type_name=field_type_name,
                ):
                    _remove_link(
                        client,
                        account,
                        scope_level=scope_level,
                        link_resource_name=link_resource,
                        validate_only=validate_only,
                    )
                    result["removed_links"] += 1
                payload.pop("replaced_google_resource_name", None)
                row.payload_json = payload
                session.commit()
            if row.status in REMOVE_READY_STATUSES:
                asset_resource = str(row.google_resource_name or "").strip()
                if asset_resource:
                    for link_resource in _asset_link_resource_names(
                        client,
                        account,
                        scope_level=scope_level,
                        scope_resource_name=scope_resource,
                        asset_resource_name=asset_resource,
                        field_type_name=field_type_name,
                    ):
                        _remove_link(
                            client,
                            account,
                            scope_level=scope_level,
                            link_resource_name=link_resource,
                            validate_only=validate_only,
                        )
                        result["removed_links"] += 1
                row.status = "removed" if not validate_only else "validated_remove"
                row.last_error = ""
                row.updated_at = _utcnow()
                session.commit()
                continue
            asset_resource = str(row.google_resource_name or "").strip()
            if not asset_resource:
                asset_resource = _mutate_asset(client, account, row, validate_only=validate_only)
                row.google_resource_name = asset_resource
                row.status = "published" if not validate_only else "validated"
                row.last_error = ""
                row.updated_at = _utcnow()
                result["created"] += 1
                session.commit()
            link_resource = _mutate_link(
                client,
                account,
                asset_type=row.asset_type,
                asset_resource_name=asset_resource,
                scope_level=scope_level,
                scope_resource_name=scope_resource,
                validate_only=validate_only,
            )
            if link_resource == "existing":
                result["existing_links"] += 1
            else:
                result["linked"] += 1
                if pre_limit:
                    link_count_cache[scope_key] = int(link_count_cache.get(scope_key, 0)) + 1
            row.status = "published" if not validate_only else "validated"
            row.last_error = ""
            row.updated_at = _utcnow()
            session.commit()
        except GoogleAdsException as exc:
            session.rollback()
            row = session.get(GoogleAdsGeneratedAsset, row.id)
            if row is not None and row.google_resource_name and _is_already_exists_error(exc):
                row.last_error = ""
                row.status = "published" if not validate_only else "validated"
                row.updated_at = _utcnow()
                session.commit()
                result["existing_links"] += 1
            elif row is not None and _is_limit_exceeded_error(exc):
                row.last_error = str(exc)[:2000]
                row.status = "publish_limited"
                row.updated_at = _utcnow()
                session.commit()
                result["skipped"] += 1
                result["errors"].append({"asset_id": row.id, "asset_type": row.asset_type, "status": "publish_limited", "error": str(exc)[:500]})
                if _is_quota_exhausted_error(exc):
                    result["quota_exhausted"] = True
                    result["quota_retry_after_seconds"] = max(result.get("quota_retry_after_seconds") or 0, _retry_after_seconds(exc))
                    break
                payload = row.payload_json if isinstance(row.payload_json, dict) else {}
                scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {"level": "account"}
                capped_scope_level = str(scope.get("level") or "account").strip()
                capped_scope_resource = ""
                if capped_scope_level == "campaign":
                    capped_scope_resource = str(scope.get("campaign_name") or row.campaign_name or "").strip()
                elif capped_scope_level == "ad_group":
                    capped_scope_resource = f"{str(scope.get('campaign_name') or row.campaign_name or '').strip()}::{str(scope.get('ad_group_name') or '').strip()}"
                capped_link_scopes.add((capped_scope_level, row.asset_type, capped_scope_resource or "account"))
            elif row is not None:
                row.last_error = str(exc)[:2000]
                row.status = "publish_failed"
                row.updated_at = _utcnow()
                session.commit()
                result["failed"] += 1
                result["errors"].append({"asset_id": row.id if row else None, "asset_type": row.asset_type if row else "", "error": str(exc)[:500]})
        except Exception as exc:  # noqa: BLE001 - publish remaining assets unless Google says quota is exhausted.
            session.rollback()
            row = session.get(GoogleAdsGeneratedAsset, row.id)
            quota_exhausted = _is_quota_exhausted_error(exc)
            if row is not None:
                row.last_error = str(exc)[:2000]
                row.status = "publish_limited" if quota_exhausted else "publish_failed"
                row.updated_at = _utcnow()
                session.commit()
            if quota_exhausted:
                result["quota_exhausted"] = True
                result["quota_retry_after_seconds"] = max(
                    result.get("quota_retry_after_seconds") or 0,
                    _retry_after_seconds(exc),
                )
                result["skipped"] += 1
                result["errors"].append(
                    {
                        "asset_id": row.id if row else None,
                        "asset_type": row.asset_type if row else "",
                        "status": "publish_limited",
                        "error": str(exc)[:500],
                    }
                )
                break
            result["failed"] += 1
            result["errors"].append({"asset_id": row.id if row else None, "asset_type": row.asset_type if row else "", "error": str(exc)[:500]})
    return result
