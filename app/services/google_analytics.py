from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    GoogleAdsAccount,
    GoogleAdsKeywordCandidate,
    GoogleAdsLandingPageCandidate,
    GoogleAnalyticsSearchTermCandidate,
    GoogleAnalyticsConnection,
    GoogleAnalyticsDataSnapshot,
    GoogleAnalyticsProperty,
    GoogleAnalyticsWebStream,
    GoogleAnalyticsWebsiteMapping,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.google_ads_landing_page_bank import (
    canonical_landing_page_url,
    landing_page_hash,
    usable_landing_page_url,
)
from app.services.google_ads_keyword_plan import clean_keyword, normalized_keyword, usable_keyword
from app.services.google_ads_snapshot_store import query_hash


GOOGLE_ANALYTICS_SCOPES = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
ADMIN_API_BASE = "https://analyticsadmin.googleapis.com/v1beta"
DATA_API_BASE = "https://analyticsdata.googleapis.com/v1beta"

GA4_ALL_TIME_START_DATE = date(2020, 1, 1)
GA4_RECENT_DAYS = 60
GA4_SNAPSHOT_TTL_HOURS = 24
GA4_REPORT_PAGE_SIZE = 10_000

DATASET_GA4_LANDING_PAGE_ECOMMERCE = "ga4_landing_page_ecommerce"
DATASET_GA4_ITEM_ECOMMERCE = "ga4_item_ecommerce"
DATASET_GA4_TRAFFIC_ECOMMERCE = "ga4_traffic_ecommerce"
DATASET_GA4_GOOGLE_ADS_SEARCH = "ga4_google_ads_search"

GA4_NON_ACQUISITION_PATH_SEGMENTS = {
    "address",
    "cart",
    "contact",
    "contactus",
    "checkout",
    "payment",
    "login",
    "terms",
    "privacy",
}
GA4_NON_PRODUCT_ITEM_NAMES = {
    "",
    "-",
    "(not set)",
    "standard delivery",
    "delivery",
    "shipping",
    "free shipping",
}
GA4_NON_PRODUCT_ITEM_NAME_PARTS = (
    "delivery",
    "shipping",
    "livraison",
)
GA4_CANDIDATE_UPSERT_BATCH_SIZE = 500


@dataclass(frozen=True)
class GA4ReportSpec:
    dataset_key: str
    title: str
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    order_metric: str


GA4_ECOMMERCE_REPORTS: tuple[GA4ReportSpec, ...] = (
    GA4ReportSpec(
        dataset_key=DATASET_GA4_LANDING_PAGE_ECOMMERCE,
        title="GA4 landing-page ecommerce",
        dimensions=("streamId", "landingPagePlusQueryString"),
        metrics=(
            "sessions",
            "engagedSessions",
            "engagementRate",
            "addToCarts",
            "checkouts",
            "ecommercePurchases",
            "purchaseRevenue",
            "totalRevenue",
        ),
        order_metric="purchaseRevenue",
    ),
    GA4ReportSpec(
        dataset_key=DATASET_GA4_ITEM_ECOMMERCE,
        title="GA4 item ecommerce",
        dimensions=("streamId", "itemId", "itemName", "itemBrand", "itemCategory"),
        metrics=(
            "itemsAddedToCart",
            "itemsCheckedOut",
            "itemsPurchased",
            "itemRevenue",
        ),
        order_metric="itemRevenue",
    ),
    GA4ReportSpec(
        dataset_key=DATASET_GA4_TRAFFIC_ECOMMERCE,
        title="GA4 traffic ecommerce",
        dimensions=("streamId", "sessionPrimaryChannelGroup", "sessionSourceMedium", "sessionManualCampaignName"),
        metrics=(
            "sessions",
            "engagedSessions",
            "engagementRate",
            "ecommercePurchases",
            "purchaseRevenue",
            "totalRevenue",
        ),
        order_metric="purchaseRevenue",
    ),
    GA4ReportSpec(
        dataset_key=DATASET_GA4_GOOGLE_ADS_SEARCH,
        title="GA4 Google Ads search ecommerce",
        dimensions=("streamId", "sessionGoogleAdsCustomerId", "sessionGoogleAdsCampaignName", "sessionGoogleAdsKeyword", "sessionGoogleAdsQuery"),
        metrics=(
            "sessions",
            "engagedSessions",
            "ecommercePurchases",
            "purchaseRevenue",
            "totalRevenue",
        ),
        order_metric="purchaseRevenue",
    ),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def analytics_property_id(resource_name: str) -> str:
    match = re.search(r"properties/(\d+)", str(resource_name or ""))
    return match.group(1) if match else str(resource_name or "").strip()


def analytics_stream_id(resource_name: str) -> str:
    match = re.search(r"dataStreams/(\d+)", str(resource_name or ""))
    return match.group(1) if match else str(resource_name or "").strip()


def _credentials_for_connection(connection: GoogleAnalyticsConnection) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=connection.refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=connection.client_id,
        client_secret=connection.client_secret,
        scopes=list(GOOGLE_ANALYTICS_SCOPES),
    )


def authorized_analytics_session(connection: GoogleAnalyticsConnection) -> AuthorizedSession:
    credentials = _credentials_for_connection(connection)
    credentials.refresh(Request())
    return AuthorizedSession(credentials)


def fetch_google_email(access_token: str) -> str:
    import requests

    response = requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if response.status_code >= 400:
        return ""
    payload = response.json()
    return str(payload.get("email") or "")


def _get_json(http: AuthorizedSession, url: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = http.get(url, params=params or {}, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Google Analytics API error {response.status_code}: {response.text[:1000]}")
    return response.json()


def _post_json(http: AuthorizedSession, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = http.post(url, json=payload, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Google Analytics API error {response.status_code}: {response.text[:1000]}")
    return response.json()


def _paged_get(http: AuthorizedSession, url: str, collection_key: str, *, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    token = ""
    while True:
        page_params = dict(params or {})
        if token:
            page_params["pageToken"] = token
        payload = _get_json(http, url, params=page_params)
        values = payload.get(collection_key)
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
        token = str(payload.get("nextPageToken") or "")
        if not token:
            return rows


def _upsert_property(
    session: Session,
    connection: GoogleAnalyticsConnection,
    *,
    account_resource_name: str,
    account_display_name: str,
    property_summary: dict[str, Any],
    property_payload: dict[str, Any],
    now: datetime,
) -> GoogleAnalyticsProperty:
    resource_name = str(property_summary.get("property") or property_payload.get("name") or "")
    row = session.scalar(
        select(GoogleAnalyticsProperty).where(GoogleAnalyticsProperty.property_resource_name == resource_name)
    )
    if row is None:
        row = GoogleAnalyticsProperty(
            connection_id=connection.id,
            property_resource_name=resource_name,
            property_id=analytics_property_id(resource_name),
        )
        session.add(row)
    row.connection_id = connection.id
    row.account_resource_name = account_resource_name
    row.account_display_name = account_display_name
    row.property_resource_name = resource_name
    row.property_id = analytics_property_id(resource_name)
    row.display_name = str(property_summary.get("displayName") or property_payload.get("displayName") or "")
    row.property_type = str(property_summary.get("propertyType") or property_payload.get("propertyType") or "")
    row.industry_category = str(property_payload.get("industryCategory") or "")
    row.time_zone = str(property_payload.get("timeZone") or "")
    row.currency_code = str(property_payload.get("currencyCode") or "")
    row.is_active = True
    row.last_discovery_at = now
    return row


def _upsert_web_stream(
    session: Session,
    property_row: GoogleAnalyticsProperty,
    stream_payload: dict[str, Any],
    *,
    now: datetime,
) -> GoogleAnalyticsWebStream | None:
    web_data = stream_payload.get("webStreamData") if isinstance(stream_payload.get("webStreamData"), dict) else {}
    resource_name = str(stream_payload.get("name") or "")
    if not resource_name or not web_data:
        return None
    row = session.scalar(
        select(GoogleAnalyticsWebStream).where(GoogleAnalyticsWebStream.stream_resource_name == resource_name)
    )
    if row is None:
        row = GoogleAnalyticsWebStream(
            analytics_property_id=property_row.id,
            stream_resource_name=resource_name,
            stream_id=analytics_stream_id(resource_name),
        )
        session.add(row)
    row.analytics_property_id = property_row.id
    row.stream_resource_name = resource_name
    row.stream_id = analytics_stream_id(resource_name)
    row.display_name = str(stream_payload.get("displayName") or "")
    row.default_uri = str(web_data.get("defaultUri") or "")
    row.measurement_id = str(web_data.get("measurementId") or "")
    row.is_active = True
    row.last_discovery_at = now
    return row


def _host(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    return (parsed.netloc or parsed.path).split("/")[0].lower().lstrip("www.")


def _root_domain(value: str) -> str:
    host = _host(value)
    parts = [part for part in host.split(".") if part]
    if len(parts) <= 2:
        return host
    second_level_suffixes = {
        "co.in",
        "co.nz",
        "co.uk",
        "com.au",
        "com.br",
        "com.cn",
        "com.hk",
        "com.mx",
        "com.sg",
        "com.tr",
        "com.tw",
        "net.au",
        "org.au",
    }
    suffix = ".".join(parts[-2:])
    if suffix in second_level_suffixes and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _mapping_exists(
    session: Session,
    *,
    analytics_property_id: int,
    analytics_stream_id: int | None,
    store_id: int | None,
    website_id: int,
    account_id: int | None,
) -> GoogleAnalyticsWebsiteMapping | None:
    query = select(GoogleAnalyticsWebsiteMapping).where(
        GoogleAnalyticsWebsiteMapping.analytics_property_id == analytics_property_id,
        GoogleAnalyticsWebsiteMapping.website_id == int(website_id or 0),
    )
    if analytics_stream_id is None:
        query = query.where(GoogleAnalyticsWebsiteMapping.analytics_stream_id.is_(None))
    else:
        query = query.where(GoogleAnalyticsWebsiteMapping.analytics_stream_id == analytics_stream_id)
    if store_id is None:
        query = query.where(GoogleAnalyticsWebsiteMapping.store_id.is_(None))
    else:
        query = query.where(GoogleAnalyticsWebsiteMapping.store_id == store_id)
    if account_id is None:
        query = query.where(GoogleAnalyticsWebsiteMapping.account_id.is_(None))
    else:
        query = query.where(GoogleAnalyticsWebsiteMapping.account_id == account_id)
    return session.scalar(query.limit(1))


def auto_map_analytics_streams(session: Session) -> int:
    streams = session.scalars(
        select(GoogleAnalyticsWebStream)
        .join(GoogleAnalyticsProperty)
        .order_by(GoogleAnalyticsWebStream.id)
    ).all()
    websites = session.scalars(select(OdooWebsite).where(OdooWebsite.is_active.is_(True))).all()
    website_by_root: dict[str, list[OdooWebsite]] = {}
    for website in websites:
        root = _root_domain(website.domain)
        if root:
            website_by_root.setdefault(root, []).append(website)
    account_mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
    ).all()
    accounts_by_website: dict[tuple[int, int], list[int]] = {}
    for mapping in account_mappings:
        accounts_by_website.setdefault((mapping.store_id, int(mapping.website_id or 0)), []).append(mapping.account_id)

    created = 0
    for stream in streams:
        root = _root_domain(stream.default_uri)
        if not root:
            continue
        matched_websites = website_by_root.get(root, [])
        for website in matched_websites:
            account_ids = accounts_by_website.get((website.store_id, int(website.website_id or 0))) or [None]
            for account_id in account_ids:
                row = _mapping_exists(
                    session,
                    analytics_property_id=stream.analytics_property_id,
                    analytics_stream_id=stream.id,
                    store_id=website.store_id,
                    website_id=website.website_id,
                    account_id=account_id,
                )
                if row is None:
                    row = GoogleAnalyticsWebsiteMapping(
                        analytics_property_id=stream.analytics_property_id,
                        analytics_stream_id=stream.id,
                        store_id=website.store_id,
                        website_id=website.website_id,
                        account_id=account_id,
                        match_source="domain_auto",
                        match_confidence=0.9,
                    )
                    session.add(row)
                    created += 1
                else:
                    row.is_active = True
                    row.match_source = row.match_source or "domain_auto"
                    row.match_confidence = max(float(row.match_confidence or 0), 0.9)
                stream.is_active = True
                stream.property.is_active = True
    return created


def ensure_account_ga4_mapping(
    session: Session,
    account: GoogleAdsAccount,
) -> dict[str, Any]:
    """Connect the account to matching GA4 streams from its mapped Odoo website."""
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping).where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
    ).all()
    if not mappings:
        return {
            "name": "ga4_property_auto_connect",
            "status": "skipped",
            "reason": "No active Odoo website mapping exists for this Google Ads account.",
            "matched": 0,
        }

    website_rows: list[tuple[OdooStoreGoogleAdsMapping, OdooWebsite, str]] = []
    for mapping in mappings:
        website = session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.store_id == mapping.store_id,
                OdooWebsite.website_id == int(mapping.website_id or 0),
                OdooWebsite.is_active.is_(True),
            )
        )
        if website is None:
            continue
        root = _root_domain(website.domain)
        if root:
            website_rows.append((mapping, website, root))
    if not website_rows:
        return {
            "name": "ga4_property_auto_connect",
            "status": "skipped",
            "reason": "The active Odoo mapping has no website domain to match against GA4 web streams.",
            "matched": 0,
        }

    streams = session.scalars(
        select(GoogleAnalyticsWebStream)
        .join(GoogleAnalyticsProperty)
        .order_by(GoogleAnalyticsProperty.display_name, GoogleAnalyticsWebStream.display_name, GoogleAnalyticsWebStream.id)
    ).all()
    connected: list[dict[str, Any]] = []
    activated_property_ids: set[int] = set()
    activated_stream_ids: set[int] = set()
    for stream in streams:
        stream_root = _root_domain(stream.default_uri)
        if not stream_root:
            continue
        for mapping, website, website_root in website_rows:
            if stream_root != website_root:
                continue
            row = _mapping_exists(
                session,
                analytics_property_id=stream.analytics_property_id,
                analytics_stream_id=stream.id,
                store_id=mapping.store_id,
                website_id=int(mapping.website_id or website.website_id or 0),
                account_id=account.id,
            )
            if row is None:
                row = GoogleAnalyticsWebsiteMapping(
                    analytics_property_id=stream.analytics_property_id,
                    analytics_stream_id=stream.id,
                    store_id=mapping.store_id,
                    website_id=int(mapping.website_id or website.website_id or 0),
                    account_id=account.id,
                    match_source="domain_auto_account_guard",
                    match_confidence=0.98,
                    is_active=True,
                )
                session.add(row)
                session.flush()
                action = "created"
            else:
                action = "reactivated" if not row.is_active else "verified"
                row.is_active = True
                if not row.match_source:
                    row.match_source = "domain_auto_account_guard"
                row.match_confidence = max(float(row.match_confidence or 0), 0.98)
            if not stream.is_active:
                activated_stream_ids.add(stream.id)
            if not stream.property.is_active:
                activated_property_ids.add(stream.property.id)
            stream.is_active = True
            stream.property.is_active = True
            connected.append(
                {
                    "action": action,
                    "mapping_id": row.id,
                    "property_id": stream.property.property_id,
                    "property_name": stream.property.display_name,
                    "stream_id": stream.stream_id,
                    "measurement_id": stream.measurement_id,
                    "default_uri": stream.default_uri,
                    "website_domain": website.domain,
                    "root_domain": website_root,
                }
            )

    if connected:
        session.commit()
        return {
            "name": "ga4_property_auto_connect",
            "status": "connected",
            "reason": "Matched the active Odoo website domain to GA4 web streams and activated the mapping before Ads planning.",
            "matched": len(connected),
            "activated_property_count": len(activated_property_ids),
            "activated_stream_count": len(activated_stream_ids),
            "connections": connected,
        }
    return {
        "name": "ga4_property_auto_connect",
        "status": "no_match",
        "reason": "No discovered GA4 web stream default URI matched the active Odoo website domain.",
        "matched": 0,
        "website_domains": [website.domain for _mapping, website, _root in website_rows],
    }


def discover_analytics_connection(session: Session, connection_id: int) -> dict[str, Any]:
    connection = session.get(GoogleAnalyticsConnection, int(connection_id))
    if connection is None:
        raise ValueError("Google Analytics connection not found.")
    now = utcnow()
    http = authorized_analytics_session(connection)
    account_summaries = _paged_get(http, f"{ADMIN_API_BASE}/accountSummaries", "accountSummaries", params={"pageSize": 200})
    property_count = 0
    stream_count = 0
    for account_summary in account_summaries:
        account_resource_name = str(account_summary.get("account") or "")
        account_display_name = str(account_summary.get("displayName") or "")
        property_summaries = account_summary.get("propertySummaries") if isinstance(account_summary.get("propertySummaries"), list) else []
        for property_summary in property_summaries:
            resource_name = str(property_summary.get("property") or "")
            if not resource_name:
                continue
            property_payload = _get_json(http, f"{ADMIN_API_BASE}/{resource_name}")
            property_row = _upsert_property(
                session,
                connection,
                account_resource_name=account_resource_name,
                account_display_name=account_display_name,
                property_summary=property_summary,
                property_payload=property_payload,
                now=now,
            )
            session.flush()
            property_count += 1
            streams = _paged_get(
                http,
                f"{ADMIN_API_BASE}/{resource_name}/dataStreams",
                "dataStreams",
                params={"pageSize": 200},
            )
            for stream_payload in streams:
                stream_row = _upsert_web_stream(session, property_row, stream_payload, now=now)
                if stream_row is not None:
                    stream_count += 1
    auto_mapping_count = auto_map_analytics_streams(session)
    connection.last_discovery_at = now
    connection.last_discovery_error = None
    connection.discovered_account_count = len(account_summaries)
    connection.discovered_property_count = property_count
    connection.discovered_stream_count = stream_count
    session.commit()
    return {
        "connection_id": connection.id,
        "account_count": len(account_summaries),
        "property_count": property_count,
        "stream_count": stream_count,
        "auto_mapping_count": auto_mapping_count,
        "discovered_at": now.isoformat(),
    }


def ga4_date_window(mode: str, days: int = GA4_RECENT_DAYS) -> tuple[str, date, date]:
    mode = "all_time" if str(mode or "").strip().lower() == "all_time" else "recent"
    end_date = date.today()
    if mode == "all_time":
        return mode, GA4_ALL_TIME_START_DATE, end_date
    days = min(max(int(days or GA4_RECENT_DAYS), 1), 365)
    return mode, end_date - timedelta(days=days - 1), end_date


def ga4_scope(mode: str, start_date: date, end_date: date, days: int = GA4_RECENT_DAYS) -> str:
    if mode == "all_time":
        return f"all_time:{start_date.isoformat()}:{end_date.isoformat()}"
    return f"last_{int(days)}d:{start_date.isoformat()}:{end_date.isoformat()}"


def _report_request(
    spec: GA4ReportSpec,
    *,
    start_date: date,
    end_date: date,
    stream: Optional[GoogleAnalyticsWebStream],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dateRanges": [{"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}],
        "dimensions": [{"name": name} for name in spec.dimensions],
        "metrics": [{"name": name} for name in spec.metrics],
        "limit": limit,
        "offset": offset,
        "keepEmptyRows": False,
        "orderBys": [{"metric": {"metricName": spec.order_metric}, "desc": True}],
    }
    if stream is not None and stream.stream_id:
        payload["dimensionFilter"] = {
            "filter": {
                "fieldName": "streamId",
                "stringFilter": {"matchType": "EXACT", "value": str(stream.stream_id)},
            }
        }
    return payload


def _parse_report_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = [str(item.get("name") or "") for item in response.get("dimensionHeaders") or [] if isinstance(item, dict)]
    metrics = [str(item.get("name") or "") for item in response.get("metricHeaders") or [] if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for row in response.get("rows") or []:
        if not isinstance(row, dict):
            continue
        parsed: dict[str, Any] = {}
        for name, value in zip(dimensions, row.get("dimensionValues") or []):
            parsed[name] = value.get("value") if isinstance(value, dict) else ""
        for name, value in zip(metrics, row.get("metricValues") or []):
            raw = value.get("value") if isinstance(value, dict) else 0
            try:
                parsed[name] = float(raw)
            except (TypeError, ValueError):
                parsed[name] = raw
        rows.append(parsed)
    return rows


def _run_report(
    http: AuthorizedSession,
    property_row: GoogleAnalyticsProperty,
    spec: GA4ReportSpec,
    *,
    start_date: date,
    end_date: date,
    stream: Optional[GoogleAnalyticsWebStream],
    max_rows: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    property_id = analytics_property_id(property_row.property_resource_name or property_row.property_id)
    all_rows: list[dict[str, Any]] = []
    offset = 0
    first_request = _report_request(
        spec,
        start_date=start_date,
        end_date=end_date,
        stream=stream,
        limit=min(GA4_REPORT_PAGE_SIZE, max_rows),
        offset=0,
    )
    first_response_meta: dict[str, Any] = {}
    while len(all_rows) < max_rows:
        limit = min(GA4_REPORT_PAGE_SIZE, max_rows - len(all_rows))
        request_payload = _report_request(
            spec,
            start_date=start_date,
            end_date=end_date,
            stream=stream,
            limit=limit,
            offset=offset,
        )
        response = _post_json(http, f"{DATA_API_BASE}/properties/{property_id}:runReport", request_payload)
        if not first_response_meta:
            first_response_meta = {
                "row_count": int(response.get("rowCount") or 0),
                "metadata": response.get("metadata") or {},
                "property_quota": response.get("propertyQuota") or {},
            }
        rows = _parse_report_rows(response)
        if not rows:
            break
        all_rows.extend(rows)
        offset += len(rows)
        if len(rows) < limit:
            break
    return first_request, all_rows, first_response_meta


def ga4_target_key(
    property_row: GoogleAnalyticsProperty,
    stream: Optional[GoogleAnalyticsWebStream],
    account: Optional[GoogleAdsAccount],
) -> str:
    stream_part = stream.stream_id if stream is not None else "all"
    account_part = account.customer_id if account is not None else "unmapped"
    return f"ga4:property:{property_row.property_id}:stream:{stream_part}:ads:{account_part}"


def upsert_ga4_snapshot(
    session: Session,
    *,
    property_row: GoogleAnalyticsProperty,
    stream: Optional[GoogleAnalyticsWebStream],
    account: Optional[GoogleAdsAccount],
    dataset_key: str,
    payload: dict[str, Any],
    scope_key: str,
    query_payload: dict[str, Any],
    source_job_id: Optional[int],
    expires_at: Optional[datetime],
) -> None:
    target_key = ga4_target_key(property_row, stream, account)
    q_hash = query_hash(json.dumps(query_payload, sort_keys=True))
    stmt = insert(GoogleAnalyticsDataSnapshot).values(
        target_key=target_key,
        analytics_property_id=property_row.id,
        analytics_stream_id=stream.id if stream is not None else None,
        account_id=account.id if account is not None else None,
        source_job_id=source_job_id,
        dataset_key=dataset_key,
        scope_key=scope_key,
        query_hash=q_hash,
        schema_version=1,
        row_count=len(payload.get("rows") or []),
        payload_json=payload,
        fetched_at=utcnow(),
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAnalyticsDataSnapshot.target_key,
            GoogleAnalyticsDataSnapshot.dataset_key,
            GoogleAnalyticsDataSnapshot.scope_key,
            GoogleAnalyticsDataSnapshot.schema_version,
            GoogleAnalyticsDataSnapshot.query_hash,
        ],
        set_={
            "analytics_property_id": stmt.excluded.analytics_property_id,
            "analytics_stream_id": stmt.excluded.analytics_stream_id,
            "account_id": stmt.excluded.account_id,
            "source_job_id": stmt.excluded.source_job_id,
            "row_count": stmt.excluded.row_count,
            "payload_json": stmt.excluded.payload_json,
            "fetched_at": stmt.excluded.fetched_at,
            "expires_at": stmt.excluded.expires_at,
        },
    )
    session.execute(stmt)


def get_fresh_ga4_snapshot(
    session: Session,
    *,
    property_row: GoogleAnalyticsProperty,
    stream: Optional[GoogleAnalyticsWebStream],
    account: Optional[GoogleAdsAccount],
    dataset_key: str,
    scope_key: str,
    query_payload: dict[str, Any],
    now: Optional[datetime] = None,
) -> Optional[GoogleAnalyticsDataSnapshot]:
    now = now or utcnow()
    return session.scalar(
        select(GoogleAnalyticsDataSnapshot)
        .where(
            GoogleAnalyticsDataSnapshot.target_key == ga4_target_key(property_row, stream, account),
            GoogleAnalyticsDataSnapshot.dataset_key == dataset_key,
            GoogleAnalyticsDataSnapshot.scope_key == scope_key,
            GoogleAnalyticsDataSnapshot.schema_version == 1,
            GoogleAnalyticsDataSnapshot.query_hash == query_hash(json.dumps(query_payload, sort_keys=True)),
            or_(
                GoogleAnalyticsDataSnapshot.expires_at.is_(None),
                GoogleAnalyticsDataSnapshot.expires_at > now,
            ),
        )
        .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
        .limit(1)
    )


def active_ga4_report_targets(
    session: Session,
    *,
    connection_ids: Optional[list[int]] = None,
    property_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    query = (
        select(GoogleAnalyticsWebsiteMapping)
        .join(GoogleAnalyticsProperty, GoogleAnalyticsWebsiteMapping.analytics_property_id == GoogleAnalyticsProperty.id)
        .where(
            GoogleAnalyticsWebsiteMapping.is_active.is_(True),
            GoogleAnalyticsProperty.is_active.is_(True),
        )
        .order_by(GoogleAnalyticsProperty.display_name, GoogleAnalyticsWebsiteMapping.id)
    )
    if connection_ids:
        query = query.where(GoogleAnalyticsProperty.connection_id.in_([int(item) for item in connection_ids]))
    if property_ids:
        query = query.where(GoogleAnalyticsProperty.id.in_([int(item) for item in property_ids]))
    if account_ids:
        query = query.where(GoogleAnalyticsWebsiteMapping.account_id.in_([int(item) for item in account_ids]))
    mappings = session.scalars(query).all()
    targets = [
        {"mapping": mapping, "property": mapping.property, "stream": mapping.stream, "account": mapping.account}
        for mapping in mappings
    ]
    if targets:
        return targets

    stream_query = (
        select(GoogleAnalyticsWebStream)
        .join(GoogleAnalyticsProperty)
        .where(
            GoogleAnalyticsWebStream.is_active.is_(True),
            GoogleAnalyticsProperty.is_active.is_(True),
        )
        .order_by(GoogleAnalyticsProperty.display_name, GoogleAnalyticsWebStream.display_name)
    )
    if connection_ids:
        stream_query = stream_query.where(GoogleAnalyticsProperty.connection_id.in_([int(item) for item in connection_ids]))
    if property_ids:
        stream_query = stream_query.where(GoogleAnalyticsProperty.id.in_([int(item) for item in property_ids]))
    if account_ids:
        return []
    return [
        {"mapping": None, "property": stream.property, "stream": stream, "account": None}
        for stream in session.scalars(stream_query).all()
    ]


def _absolute_landing_url(stream: Optional[GoogleAnalyticsWebStream], landing_page: str) -> str:
    value = str(landing_page or "").strip()
    if not value or value in {"(not set)", "(other)"}:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    base = str(stream.default_uri if stream is not None else "").strip()
    if not base:
        return value
    parsed = urlsplit(base if "://" in base else f"https://{base}")
    host = parsed.netloc or parsed.path
    if not host:
        return value
    return f"https://{host}{value if value.startswith('/') else '/' + value}"


def _ga4_page_quality(row: dict[str, Any]) -> str:
    purchases = float(row.get("ecommercePurchases") or 0)
    revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
    engaged = float(row.get("engagedSessions") or 0)
    add_to_carts = float(row.get("addToCarts") or 0)
    checkouts = float(row.get("checkouts") or 0)
    if purchases > 0 and revenue > 0:
        return "revenue"
    if purchases > 0:
        return "converting"
    if add_to_carts > 0 or checkouts > 0:
        return "clicked"
    if engaged >= 10:
        return "clicked"
    return "watch"


def _ga4_landing_page_score(row: dict[str, Any]) -> float:
    purchases = float(row.get("ecommercePurchases") or 0)
    revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
    engaged = float(row.get("engagedSessions") or 0)
    sessions = float(row.get("sessions") or 0)
    add_to_carts = float(row.get("addToCarts") or 0)
    checkouts = float(row.get("checkouts") or 0)
    return (purchases * 150000) + (revenue * 1200) + (checkouts * 2000) + (add_to_carts * 600) + (engaged * 100) + sessions


def _ga4_item_quality(row: dict[str, Any]) -> str:
    purchases = float(row.get("itemsPurchased") or 0)
    revenue = float(row.get("itemRevenue") or 0)
    carts = float(row.get("itemsAddedToCart") or 0)
    checkouts = float(row.get("itemsCheckedOut") or 0)
    if purchases > 0 and revenue > 0:
        return "revenue"
    if purchases > 0:
        return "converting"
    if carts > 0 or checkouts > 0:
        return "clicked"
    return "watch"


def _ga4_item_score(row: dict[str, Any]) -> float:
    purchases = float(row.get("itemsPurchased") or 0)
    revenue = float(row.get("itemRevenue") or 0)
    carts = float(row.get("itemsAddedToCart") or 0)
    checkouts = float(row.get("itemsCheckedOut") or 0)
    return (purchases * 150000) + (revenue * 1200) + (checkouts * 2500) + (carts * 900)


def _ga4_search_term_quality(row: dict[str, Any]) -> str:
    purchases = float(row.get("ecommercePurchases") or 0)
    revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
    engaged = float(row.get("engagedSessions") or 0)
    if purchases > 0 and revenue > 0:
        return "revenue"
    if purchases > 0:
        return "converting"
    if engaged >= 3:
        return "clicked"
    return "watch"


def _ga4_search_term_score(row: dict[str, Any]) -> float:
    purchases = float(row.get("ecommercePurchases") or 0)
    revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
    engaged = float(row.get("engagedSessions") or 0)
    sessions = float(row.get("sessions") or 0)
    return (purchases * 150000) + (revenue * 1200) + (engaged * 200) + sessions


def _is_ga4_acquisition_url(value: str) -> bool:
    normalized = canonical_landing_page_url(value)
    if not usable_landing_page_url(normalized):
        return False
    path = urlsplit(normalized).path.lower()
    segments = {segment for segment in path.split("/") if segment}
    return not bool(segments.intersection(GA4_NON_ACQUISITION_PATH_SEGMENTS))


def _ga4_item_keyword(row: dict[str, Any]) -> str:
    name = re.sub(r"\s+", " ", str(row.get("itemName") or "").strip())
    if name.lower() in GA4_NON_PRODUCT_ITEM_NAMES:
        return ""
    if any(part in name.lower() for part in GA4_NON_PRODUCT_ITEM_NAME_PARTS):
        return ""
    brand = re.sub(r"\s+", " ", str(row.get("itemBrand") or "").strip())
    category = re.sub(r"\s+", " ", str(row.get("itemCategory") or "").strip())
    candidates = []
    if brand and brand.lower() not in {"(not set)", "-"} and brand.lower() not in name.lower():
        candidates.append(f"{brand} {name}")
    candidates.append(name)
    if category and category.lower() not in {"(not set)", "-"} and category.lower() not in name.lower():
        candidates.append(f"{name} {category}")
    for candidate in candidates:
        keyword = clean_keyword(candidate)
        if usable_keyword(keyword):
            return keyword
    return ""


def _ga4_search_term_text(row: dict[str, Any]) -> str:
    for key in ("sessionGoogleAdsQuery", "sessionGoogleAdsKeyword"):
        text = clean_keyword(str(row.get(key) or ""))
        if text and text not in {"(not set)", "(organic)", "(not provided)"} and usable_keyword(text):
            return text
    return ""


def _ga4_snapshot_rows(snapshot: GoogleAnalyticsDataSnapshot) -> list[dict[str, Any]]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    rows = payload.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _chunks(rows: list[dict[str, Any]], size: int = GA4_CANDIDATE_UPSERT_BATCH_SIZE) -> Iterable[list[dict[str, Any]]]:
    size = max(int(size or GA4_CANDIDATE_UPSERT_BATCH_SIZE), 1)
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def _dedupe_ga4_landing_page_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    quality_rank = {"watch": 0, "clicked": 1, "converting": 2, "revenue": 3}
    for candidate in candidates:
        account_id = int(candidate.get("account_id") or 0)
        url_hash = str(candidate.get("normalized_url_hash") or "")
        if not account_id or not url_hash:
            continue
        key = (account_id, url_hash)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
            continue
        if len(str(candidate.get("url") or "")) > len(str(existing.get("url") or "")):
            existing["url"] = candidate.get("url")
            existing["normalized_url"] = candidate.get("normalized_url")
        for list_key in ("source_dataset_keys", "source_scope_keys", "source_snapshot_ids", "campaign_ids", "campaign_names", "channel_types"):
            values = existing.setdefault(list_key, [])
            for value in candidate.get(list_key) or []:
                if value not in values:
                    values.append(value)
        for metric_key in (
            "impressions",
            "clicks",
            "cost",
            "conversions",
            "conversion_value",
            "all_conversions",
            "all_conversions_value",
            "score",
        ):
            existing[metric_key] = (existing.get(metric_key) or 0) + (candidate.get(metric_key) or 0)
        if quality_rank.get(str(candidate.get("quality_label") or "watch"), 0) > quality_rank.get(
            str(existing.get("quality_label") or "watch"),
            0,
        ):
            existing["quality_label"] = candidate.get("quality_label")
        existing_source = existing.get("source_json") if isinstance(existing.get("source_json"), dict) else {}
        candidate_source = candidate.get("source_json") if isinstance(candidate.get("source_json"), dict) else {}
        existing_rows = existing_source.setdefault("source_rows", [])
        for row in candidate_source.get("source_rows") or []:
            if len(existing_rows) >= 12:
                break
            existing_rows.append(row)
        existing["source_json"] = existing_source
    return list(by_key.values())


def _dedupe_ga4_landing_page_candidate_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Final guard for Postgres ON CONFLICT batches.

    Postgres cannot update the same conflict target twice within one INSERT
    statement, so keep this close to the insert even though the collector also
    dedupes the full candidate list.
    """
    return _dedupe_ga4_landing_page_candidates(batch)


def upsert_ga4_landing_page_candidates(
    session: Session,
    snapshots: list[GoogleAnalyticsDataSnapshot],
    *,
    source_job_id: Optional[int] = None,
) -> int:
    candidates: list[dict[str, Any]] = []
    now = utcnow()
    for snapshot in snapshots:
        if snapshot.dataset_key != DATASET_GA4_LANDING_PAGE_ECOMMERCE or snapshot.account_id is None:
            continue
        payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        stream = snapshot.stream
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = _absolute_landing_url(stream, str(row.get("landingPagePlusQueryString") or ""))
            if not _is_ga4_acquisition_url(url):
                continue
            normalized_url = canonical_landing_page_url(url)
            normalized_url_hash = landing_page_hash(normalized_url)
            sessions = int(float(row.get("sessions") or 0))
            engaged_sessions = int(float(row.get("engagedSessions") or 0))
            purchases = float(row.get("ecommercePurchases") or 0)
            revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
            source_row = {
                "dataset_key": snapshot.dataset_key,
                "snapshot_id": snapshot.id,
                "scope_key": snapshot.scope_key,
                "stream_id": row.get("streamId"),
                "landing_page": row.get("landingPagePlusQueryString"),
                "url": normalized_url,
                "sessions": sessions,
                "engaged_sessions": engaged_sessions,
                "engagement_rate": row.get("engagementRate"),
                "add_to_carts": row.get("addToCarts"),
                "checkouts": row.get("checkouts"),
                "purchases": purchases,
                "revenue": revenue,
            }
            candidate = {
                "account_id": snapshot.account_id,
                "url": normalized_url,
                "normalized_url": normalized_url,
                "normalized_url_hash": normalized_url_hash,
                "quality_label": _ga4_page_quality(row),
                "source_dataset_keys": [snapshot.dataset_key],
                "source_scope_keys": [snapshot.scope_key],
                "source_snapshot_ids": [snapshot.id],
                "campaign_ids": [],
                "campaign_names": [],
                "channel_types": ["GA4"],
                "impressions": sessions,
                "clicks": engaged_sessions,
                "cost": 0.0,
                "conversions": purchases,
                "conversion_value": revenue,
                "all_conversions": purchases,
                "all_conversions_value": revenue,
                "score": _ga4_landing_page_score(row),
                "source_json": {
                    "source": "google_analytics_4_ecommerce",
                    "source_job_id": source_job_id,
                    "pulled_at": now.isoformat(),
                    "dedupe_key": "account_id+normalized_url_hash",
                    "metric_semantics": {
                        "impressions": "ga4_sessions",
                        "clicks": "ga4_engaged_sessions",
                        "conversions": "ga4_ecommerce_purchases",
                        "conversion_value": "ga4_purchase_revenue",
                    },
                    "source_rows": [source_row],
                },
                "last_seen_at": now,
                "last_pulled_at": now,
                "last_source_job_id": source_job_id,
                "updated_at": now,
            }
            candidates.append(candidate)
    if not candidates:
        return 0
    candidates = _dedupe_ga4_landing_page_candidates(candidates)
    for batch in _chunks(candidates):
        batch = _dedupe_ga4_landing_page_candidate_batch(batch)
        if not batch:
            continue
        stmt = insert(GoogleAdsLandingPageCandidate).values(batch)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsLandingPageCandidate.account_id,
                GoogleAdsLandingPageCandidate.normalized_url_hash,
            ],
            set_={
                "url": excluded.url,
                "normalized_url": excluded.normalized_url,
                "quality_label": excluded.quality_label,
                "source_dataset_keys": excluded.source_dataset_keys,
                "source_scope_keys": excluded.source_scope_keys,
                "source_snapshot_ids": excluded.source_snapshot_ids,
                "campaign_ids": excluded.campaign_ids,
                "campaign_names": excluded.campaign_names,
                "channel_types": excluded.channel_types,
                "impressions": excluded.impressions,
                "clicks": excluded.clicks,
                "cost": excluded.cost,
                "conversions": excluded.conversions,
                "conversion_value": excluded.conversion_value,
                "all_conversions": excluded.all_conversions,
                "all_conversions_value": excluded.all_conversions_value,
                "score": excluded.score,
                "source_json": excluded.source_json,
                "last_seen_at": excluded.last_seen_at,
                "last_pulled_at": excluded.last_pulled_at,
                "last_source_job_id": excluded.last_source_job_id,
                "updated_at": excluded.updated_at,
            },
        )
        session.execute(stmt)
    return len(candidates)


def upsert_ga4_item_keyword_candidates(
    session: Session,
    snapshots: list[GoogleAnalyticsDataSnapshot],
    *,
    source_job_id: Optional[int] = None,
) -> int:
    candidates: list[dict[str, Any]] = []
    now = utcnow()
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.dataset_key != DATASET_GA4_ITEM_ECOMMERCE or snapshot.account_id is None:
            continue
        for row in _ga4_snapshot_rows(snapshot):
            keyword = _ga4_item_keyword(row)
            if not keyword:
                continue
            key = normalized_keyword(keyword)
            account_key = (int(snapshot.account_id), key)
            entry = by_key.setdefault(
                account_key,
                {
                    "account_id": snapshot.account_id,
                    "keyword": keyword,
                    "normalized_keyword": key,
                    "quality_label": "watch",
                    "review_status": "new",
                    "match_type": "exact",
                    "source_dataset_keys": [],
                    "source_scope_keys": [],
                    "source_snapshot_ids": [],
                    "campaign_ids": [],
                    "campaign_names": [],
                    "ad_group_names": [],
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                    "all_conversions": 0.0,
                    "all_conversions_value": 0.0,
                    "score": 0.0,
                    "source_rows": [],
                },
            )
            if len(keyword) > len(str(entry.get("keyword") or "")):
                entry["keyword"] = keyword
            if snapshot.dataset_key not in entry["source_dataset_keys"]:
                entry["source_dataset_keys"].append(snapshot.dataset_key)
            if snapshot.scope_key not in entry["source_scope_keys"]:
                entry["source_scope_keys"].append(snapshot.scope_key)
            if snapshot.id not in entry["source_snapshot_ids"]:
                entry["source_snapshot_ids"].append(snapshot.id)
            carts = float(row.get("itemsAddedToCart") or 0)
            checkouts = float(row.get("itemsCheckedOut") or 0)
            purchases = float(row.get("itemsPurchased") or 0)
            revenue = float(row.get("itemRevenue") or 0)
            entry["impressions"] += int(carts + checkouts + purchases)
            entry["clicks"] += int(carts + checkouts)
            entry["conversions"] += purchases
            entry["conversion_value"] += revenue
            entry["all_conversions"] += purchases
            entry["all_conversions_value"] += revenue
            entry["score"] += _ga4_item_score(row)
            if len(entry["source_rows"]) < 12:
                entry["source_rows"].append(
                    {
                        "dataset_key": snapshot.dataset_key,
                        "snapshot_id": snapshot.id,
                        "scope_key": snapshot.scope_key,
                        "stream_id": row.get("streamId"),
                        "item_id": row.get("itemId"),
                        "item_name": row.get("itemName"),
                        "item_brand": row.get("itemBrand"),
                        "item_category": row.get("itemCategory"),
                        "keyword": keyword,
                        "items_added_to_cart": carts,
                        "items_checked_out": checkouts,
                        "items_purchased": purchases,
                        "item_revenue": revenue,
                    }
                )
            entry_quality = _ga4_item_quality(row)
            quality_rank = {"watch": 0, "clicked": 1, "converting": 2, "revenue": 3}
            if quality_rank.get(entry_quality, 0) > quality_rank.get(str(entry.get("quality_label") or "watch"), 0):
                entry["quality_label"] = entry_quality

    for entry in by_key.values():
        source_rows = entry.pop("source_rows")
        entry["source_json"] = {
            "source": "google_analytics_4_item_ecommerce",
            "source_job_id": source_job_id,
            "pulled_at": now.isoformat(),
            "dedupe_key": "account_id+normalized_keyword",
            "metric_semantics": {
                "impressions": "ga4_item_cart_checkout_purchase_events",
                "clicks": "ga4_item_cart_checkout_intent_events",
                "conversions": "ga4_items_purchased",
                "conversion_value": "ga4_item_revenue",
            },
            "source_rows": source_rows,
        }
        entry["first_seen_at"] = now
        entry["last_seen_at"] = now
        entry["last_pulled_at"] = now
        entry["last_source_job_id"] = source_job_id
        entry["created_at"] = now
        entry["updated_at"] = now
        candidates.append(entry)

    if not candidates:
        return 0
    for batch in _chunks(candidates):
        stmt = insert(GoogleAdsKeywordCandidate).values(batch)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAdsKeywordCandidate.account_id,
                GoogleAdsKeywordCandidate.normalized_keyword,
            ],
            set_={
                "keyword": excluded.keyword,
                "quality_label": excluded.quality_label,
                "match_type": excluded.match_type,
                "source_dataset_keys": excluded.source_dataset_keys,
                "source_scope_keys": excluded.source_scope_keys,
                "source_snapshot_ids": excluded.source_snapshot_ids,
                "campaign_ids": excluded.campaign_ids,
                "campaign_names": excluded.campaign_names,
                "ad_group_names": excluded.ad_group_names,
                "impressions": excluded.impressions,
                "clicks": excluded.clicks,
                "cost": excluded.cost,
                "conversions": excluded.conversions,
                "conversion_value": excluded.conversion_value,
                "all_conversions": excluded.all_conversions,
                "all_conversions_value": excluded.all_conversions_value,
                "score": excluded.score,
                "source_json": excluded.source_json,
                "last_seen_at": excluded.last_seen_at,
                "last_pulled_at": excluded.last_pulled_at,
                "last_source_job_id": excluded.last_source_job_id,
                "updated_at": excluded.updated_at,
            },
        )
        session.execute(stmt)
    return len(candidates)


def upsert_ga4_search_term_candidates(
    session: Session,
    snapshots: list[GoogleAnalyticsDataSnapshot],
    *,
    source_job_id: Optional[int] = None,
) -> int:
    now = utcnow()
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for snapshot in snapshots:
        if snapshot.dataset_key != DATASET_GA4_GOOGLE_ADS_SEARCH or snapshot.account_id is None:
            continue
        for row in _ga4_snapshot_rows(snapshot):
            search_term = _ga4_search_term_text(row)
            if not search_term:
                continue
            key = normalized_keyword(search_term)
            account_key = (int(snapshot.account_id), key)
            entry = by_key.setdefault(
                account_key,
                {
                    "account_id": snapshot.account_id,
                    "analytics_property_id": snapshot.analytics_property_id,
                    "analytics_stream_id": snapshot.analytics_stream_id,
                    "search_term": search_term,
                    "normalized_search_term": key,
                    "keyword": clean_keyword(str(row.get("sessionGoogleAdsKeyword") or "")),
                    "campaign_name": str(row.get("sessionGoogleAdsCampaignName") or "").strip(),
                    "quality_label": "watch",
                    "review_status": "new",
                    "source_dataset_keys": [],
                    "source_scope_keys": [],
                    "source_snapshot_ids": [],
                    "sessions": 0,
                    "engaged_sessions": 0,
                    "purchases": 0.0,
                    "revenue": 0.0,
                    "score": 0.0,
                    "source_rows": [],
                },
            )
            if len(search_term) > len(str(entry.get("search_term") or "")):
                entry["search_term"] = search_term
            if snapshot.dataset_key not in entry["source_dataset_keys"]:
                entry["source_dataset_keys"].append(snapshot.dataset_key)
            if snapshot.scope_key not in entry["source_scope_keys"]:
                entry["source_scope_keys"].append(snapshot.scope_key)
            if snapshot.id not in entry["source_snapshot_ids"]:
                entry["source_snapshot_ids"].append(snapshot.id)
            sessions = int(float(row.get("sessions") or 0))
            engaged = int(float(row.get("engagedSessions") or 0))
            purchases = float(row.get("ecommercePurchases") or 0)
            revenue = float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
            entry["sessions"] += sessions
            entry["engaged_sessions"] += engaged
            entry["purchases"] += purchases
            entry["revenue"] += revenue
            entry["score"] += _ga4_search_term_score(row)
            quality = _ga4_search_term_quality(row)
            quality_rank = {"watch": 0, "clicked": 1, "converting": 2, "revenue": 3}
            if quality_rank.get(quality, 0) > quality_rank.get(str(entry.get("quality_label") or "watch"), 0):
                entry["quality_label"] = quality
            if len(entry["source_rows"]) < 12:
                entry["source_rows"].append(
                    {
                        "dataset_key": snapshot.dataset_key,
                        "snapshot_id": snapshot.id,
                        "scope_key": snapshot.scope_key,
                        "stream_id": row.get("streamId"),
                        "google_ads_customer_id": row.get("sessionGoogleAdsCustomerId"),
                        "campaign_name": row.get("sessionGoogleAdsCampaignName"),
                        "keyword": row.get("sessionGoogleAdsKeyword"),
                        "query": row.get("sessionGoogleAdsQuery"),
                        "sessions": sessions,
                        "engaged_sessions": engaged,
                        "purchases": purchases,
                        "revenue": revenue,
                    }
                )
    candidates: list[dict[str, Any]] = []
    for entry in by_key.values():
        source_rows = entry.pop("source_rows")
        entry["source_json"] = {
            "source": "google_analytics_4_google_ads_search",
            "source_job_id": source_job_id,
            "pulled_at": now.isoformat(),
            "dedupe_key": "account_id+normalized_search_term",
            "metric_semantics": {
                "sessions": "ga4_sessions",
                "engaged_sessions": "ga4_engaged_sessions",
                "purchases": "ga4_ecommerce_purchases",
                "revenue": "ga4_purchase_revenue",
            },
            "source_rows": source_rows,
        }
        entry["first_seen_at"] = now
        entry["last_seen_at"] = now
        entry["last_pulled_at"] = now
        entry["last_source_job_id"] = source_job_id
        entry["created_at"] = now
        entry["updated_at"] = now
        candidates.append(entry)

    if not candidates:
        return 0
    for batch in _chunks(candidates):
        stmt = insert(GoogleAnalyticsSearchTermCandidate).values(batch)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleAnalyticsSearchTermCandidate.account_id,
                GoogleAnalyticsSearchTermCandidate.normalized_search_term,
            ],
            set_={
                "analytics_property_id": excluded.analytics_property_id,
                "analytics_stream_id": excluded.analytics_stream_id,
                "search_term": excluded.search_term,
                "keyword": excluded.keyword,
                "campaign_name": excluded.campaign_name,
                "quality_label": excluded.quality_label,
                "source_dataset_keys": excluded.source_dataset_keys,
                "source_scope_keys": excluded.source_scope_keys,
                "source_snapshot_ids": excluded.source_snapshot_ids,
                "sessions": excluded.sessions,
                "engaged_sessions": excluded.engaged_sessions,
                "purchases": excluded.purchases,
                "revenue": excluded.revenue,
                "score": excluded.score,
                "source_json": excluded.source_json,
                "last_seen_at": excluded.last_seen_at,
                "last_pulled_at": excluded.last_pulled_at,
                "last_source_job_id": excluded.last_source_job_id,
                "updated_at": excluded.updated_at,
            },
        )
        session.execute(stmt)
    return len(candidates)


def sync_ga4_ecommerce_snapshots(
    session: Session,
    *,
    mode: str = "recent",
    days: int = GA4_RECENT_DAYS,
    max_rows: int = 10_000,
    force: bool = False,
    source_job_id: Optional[int] = None,
    connection_ids: Optional[list[int]] = None,
    property_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    mode, start_date, end_date = ga4_date_window(mode, days)
    max_rows = min(max(int(max_rows or 10_000), 50), 250_000 if mode == "all_time" else 50_000)
    scope_key = ga4_scope(mode, start_date, end_date, days)
    expires_at = utcnow() + timedelta(hours=GA4_SNAPSHOT_TTL_HOURS)
    targets = active_ga4_report_targets(
        session,
        connection_ids=connection_ids,
        property_ids=property_ids,
        account_ids=account_ids,
    )
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    snapshots_to_import: list[GoogleAnalyticsDataSnapshot] = []
    http_by_connection: dict[int, AuthorizedSession] = {}
    for target in targets:
        property_row: GoogleAnalyticsProperty = target["property"]
        stream: Optional[GoogleAnalyticsWebStream] = target.get("stream")
        account: Optional[GoogleAdsAccount] = target.get("account")
        for spec in GA4_ECOMMERCE_REPORTS:
            try:
                query_payload = _report_request(
                    spec,
                    start_date=start_date,
                    end_date=end_date,
                    stream=stream,
                    limit=min(GA4_REPORT_PAGE_SIZE, max_rows),
                    offset=0,
                )
                cached = None if force else get_fresh_ga4_snapshot(
                    session,
                    property_row=property_row,
                    stream=stream,
                    account=account,
                    dataset_key=spec.dataset_key,
                    scope_key=scope_key,
                    query_payload=query_payload,
                )
                if cached is not None:
                    snapshots_to_import.append(cached)
                    results.append(
                        {
                            "dataset_key": spec.dataset_key,
                            "property_id": property_row.property_id,
                            "stream_id": stream.stream_id if stream is not None else "",
                            "account_id": account.id if account is not None else None,
                            "rows": cached.row_count,
                            "status": "cached",
                            "snapshot_id": cached.id,
                            "fetched_at": cached.fetched_at.isoformat() if cached.fetched_at else None,
                        }
                    )
                    continue
                http = http_by_connection.get(property_row.connection_id)
                if http is None:
                    http = authorized_analytics_session(property_row.connection)
                    http_by_connection[property_row.connection_id] = http
                query_payload, rows, response_meta = _run_report(
                    http,
                    property_row,
                    spec,
                    start_date=start_date,
                    end_date=end_date,
                    stream=stream,
                    max_rows=max_rows,
                )
                payload = {
                    "dataset_key": spec.dataset_key,
                    "title": spec.title,
                    "date_range": {
                        "mode": mode,
                        "days": days if mode != "all_time" else None,
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                    },
                    "analytics_property": {
                        "id": property_row.id,
                        "property_id": property_row.property_id,
                        "resource_name": property_row.property_resource_name,
                        "display_name": property_row.display_name,
                        "time_zone": property_row.time_zone,
                        "currency_code": property_row.currency_code,
                    },
                    "web_stream": {
                        "id": stream.id if stream is not None else None,
                        "stream_id": stream.stream_id if stream is not None else "",
                        "display_name": stream.display_name if stream is not None else "",
                        "default_uri": stream.default_uri if stream is not None else "",
                    },
                    "google_ads_account": {
                        "id": account.id if account is not None else None,
                        "name": account.name if account is not None else "",
                        "customer_id": account.customer_id if account is not None else "",
                    },
                    "rows": rows,
                    "row_limit": max_rows,
                    "response_meta": response_meta,
                    "notes": [
                        "Stored in Postgres for landing-page scoring, ecommerce planning, audience ideas, and Ads automation safeguards.",
                        "Odoo actual sales remain the budget-spend source of truth; GA4 is an intent and page-quality enhancer.",
                    ],
                    "fetched_at": utcnow().isoformat(),
                }
                upsert_ga4_snapshot(
                    session,
                    property_row=property_row,
                    stream=stream,
                    account=account,
                    dataset_key=spec.dataset_key,
                    payload=payload,
                    scope_key=scope_key,
                    query_payload=query_payload,
                    source_job_id=source_job_id,
                    expires_at=expires_at,
                )
                property_row.last_pull_at = utcnow()
                session.commit()
                snapshot = session.scalar(
                    select(GoogleAnalyticsDataSnapshot)
                    .where(
                        GoogleAnalyticsDataSnapshot.target_key == ga4_target_key(property_row, stream, account),
                        GoogleAnalyticsDataSnapshot.dataset_key == spec.dataset_key,
                        GoogleAnalyticsDataSnapshot.scope_key == scope_key,
                        GoogleAnalyticsDataSnapshot.query_hash == query_hash(json.dumps(query_payload, sort_keys=True)),
                    )
                    .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
                    .limit(1)
                )
                if snapshot is not None:
                    snapshots_to_import.append(snapshot)
                results.append(
                    {
                        "dataset_key": spec.dataset_key,
                        "property_id": property_row.property_id,
                        "stream_id": stream.stream_id if stream is not None else "",
                        "account_id": account.id if account is not None else None,
                        "rows": len(rows),
                        "status": "fetched",
                    }
                )
            except Exception as exc:  # noqa: BLE001 - keep the other GA4 reports moving.
                session.rollback()
                errors.append(
                    {
                        "dataset_key": spec.dataset_key,
                        "property_id": property_row.property_id,
                        "stream_id": stream.stream_id if stream is not None else "",
                        "account_id": account.id if account is not None else None,
                        "error": str(exc)[:500],
                    }
                )
    imported_pages = upsert_ga4_landing_page_candidates(session, snapshots_to_import, source_job_id=source_job_id)
    imported_keywords = upsert_ga4_item_keyword_candidates(session, snapshots_to_import, source_job_id=source_job_id)
    imported_search_terms = upsert_ga4_search_term_candidates(session, snapshots_to_import, source_job_id=source_job_id)
    session.commit()
    return {
        "mode": mode,
        "scope_key": scope_key,
        "target_count": len(targets),
        "dataset_count": len(results),
        "error_count": len(errors),
        "landing_pages_imported": imported_pages,
        "keywords_imported": imported_keywords,
        "search_terms_imported": imported_search_terms,
        "datasets": results,
        "errors": errors,
        "pulled_at": utcnow().isoformat(),
    }


def sync_ga4_search_term_snapshots(
    session: Session,
    *,
    mode: str = "recent",
    days: int = GA4_RECENT_DAYS,
    max_rows: int = 10_000,
    force: bool = False,
    source_job_id: Optional[int] = None,
    connection_ids: Optional[list[int]] = None,
    property_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    mode, start_date, end_date = ga4_date_window(mode, days)
    max_rows = min(max(int(max_rows or 10_000), 50), 250_000 if mode == "all_time" else 50_000)
    scope_key = ga4_scope(mode, start_date, end_date, days)
    spec = next(item for item in GA4_ECOMMERCE_REPORTS if item.dataset_key == DATASET_GA4_GOOGLE_ADS_SEARCH)
    expires_at = utcnow() + timedelta(hours=GA4_SNAPSHOT_TTL_HOURS)
    targets = active_ga4_report_targets(
        session,
        connection_ids=connection_ids,
        property_ids=property_ids,
        account_ids=account_ids,
    )
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    snapshots_to_import: list[GoogleAnalyticsDataSnapshot] = []
    http_by_connection: dict[int, AuthorizedSession] = {}
    for target in targets:
        property_row: GoogleAnalyticsProperty = target["property"]
        stream: Optional[GoogleAnalyticsWebStream] = target.get("stream")
        account: Optional[GoogleAdsAccount] = target.get("account")
        try:
            query_payload = _report_request(
                spec,
                start_date=start_date,
                end_date=end_date,
                stream=stream,
                limit=min(GA4_REPORT_PAGE_SIZE, max_rows),
                offset=0,
            )
            cached = None if force else get_fresh_ga4_snapshot(
                session,
                property_row=property_row,
                stream=stream,
                account=account,
                dataset_key=spec.dataset_key,
                scope_key=scope_key,
                query_payload=query_payload,
            )
            if cached is not None:
                snapshots_to_import.append(cached)
                results.append(
                    {
                        "dataset_key": spec.dataset_key,
                        "property_id": property_row.property_id,
                        "stream_id": stream.stream_id if stream is not None else "",
                        "account_id": account.id if account is not None else None,
                        "rows": cached.row_count,
                        "status": "cached",
                        "snapshot_id": cached.id,
                        "fetched_at": cached.fetched_at.isoformat() if cached.fetched_at else None,
                    }
                )
                continue
            http = http_by_connection.get(property_row.connection_id)
            if http is None:
                http = authorized_analytics_session(property_row.connection)
                http_by_connection[property_row.connection_id] = http
            query_payload, rows, response_meta = _run_report(
                http,
                property_row,
                spec,
                start_date=start_date,
                end_date=end_date,
                stream=stream,
                max_rows=max_rows,
            )
            payload = {
                "dataset_key": spec.dataset_key,
                "title": spec.title,
                "date_range": {
                    "mode": mode,
                    "days": days if mode != "all_time" else None,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                },
                "analytics_property": {
                    "id": property_row.id,
                    "property_id": property_row.property_id,
                    "resource_name": property_row.property_resource_name,
                    "display_name": property_row.display_name,
                    "time_zone": property_row.time_zone,
                    "currency_code": property_row.currency_code,
                },
                "web_stream": {
                    "id": stream.id if stream is not None else None,
                    "stream_id": stream.stream_id if stream is not None else "",
                    "display_name": stream.display_name if stream is not None else "",
                    "default_uri": stream.default_uri if stream is not None else "",
                },
                "google_ads_account": {
                    "id": account.id if account is not None else None,
                    "name": account.name if account is not None else "",
                    "customer_id": account.customer_id if account is not None else "",
                },
                "rows": rows,
                "row_limit": max_rows,
                "response_meta": response_meta,
                "notes": [
                    "Stored in Postgres as a GA4 search-term bank for research, PMax search themes, and audience-signal ideas.",
                    "Search terms are deduped by mapped Google Ads account and normalized query text.",
                ],
                "fetched_at": utcnow().isoformat(),
            }
            upsert_ga4_snapshot(
                session,
                property_row=property_row,
                stream=stream,
                account=account,
                dataset_key=spec.dataset_key,
                payload=payload,
                scope_key=scope_key,
                query_payload=query_payload,
                source_job_id=source_job_id,
                expires_at=expires_at,
            )
            property_row.last_pull_at = utcnow()
            session.commit()
            snapshot = session.scalar(
                select(GoogleAnalyticsDataSnapshot)
                .where(
                    GoogleAnalyticsDataSnapshot.target_key == ga4_target_key(property_row, stream, account),
                    GoogleAnalyticsDataSnapshot.dataset_key == spec.dataset_key,
                    GoogleAnalyticsDataSnapshot.scope_key == scope_key,
                    GoogleAnalyticsDataSnapshot.query_hash == query_hash(json.dumps(query_payload, sort_keys=True)),
                )
                .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
                .limit(1)
            )
            if snapshot is not None:
                snapshots_to_import.append(snapshot)
            results.append(
                {
                    "dataset_key": spec.dataset_key,
                    "property_id": property_row.property_id,
                    "stream_id": stream.stream_id if stream is not None else "",
                    "account_id": account.id if account is not None else None,
                    "rows": len(rows),
                    "status": "fetched",
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep other mapped targets moving.
            session.rollback()
            errors.append(
                {
                    "dataset_key": spec.dataset_key,
                    "property_id": property_row.property_id,
                    "stream_id": stream.stream_id if stream is not None else "",
                    "account_id": account.id if account is not None else None,
                    "error": str(exc)[:500],
                }
            )
    imported_terms = upsert_ga4_search_term_candidates(session, snapshots_to_import, source_job_id=source_job_id)
    session.commit()
    return {
        "mode": mode,
        "scope_key": scope_key,
        "target_count": len(targets),
        "dataset_count": len(results),
        "error_count": len(errors),
        "search_terms_imported": imported_terms,
        "datasets": results,
        "errors": errors,
        "pulled_at": utcnow().isoformat(),
    }


def _rank_ga4_signal(item: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(item.get("score") or 0),
        float(item.get("revenue") or item.get("item_revenue") or 0),
        float(item.get("purchases") or item.get("items_purchased") or 0),
        float(item.get("add_to_carts") or item.get("items_added_to_cart") or 0)
        + float(item.get("checkouts") or item.get("items_checked_out") or 0),
    )


def _ga4_matrix_from_candidates(session: Session, account_id: int, *, limit: int) -> Optional[dict[str, Any]]:
    if not hasattr(session, "execute"):
        return None
    page_rows = session.execute(
        select(
            GoogleAdsLandingPageCandidate.url,
            GoogleAdsLandingPageCandidate.normalized_url,
            GoogleAdsLandingPageCandidate.quality_label,
            GoogleAdsLandingPageCandidate.source_dataset_keys,
            GoogleAdsLandingPageCandidate.source_snapshot_ids,
            GoogleAdsLandingPageCandidate.impressions,
            GoogleAdsLandingPageCandidate.clicks,
            GoogleAdsLandingPageCandidate.conversions,
            GoogleAdsLandingPageCandidate.conversion_value,
            GoogleAdsLandingPageCandidate.all_conversions,
            GoogleAdsLandingPageCandidate.all_conversions_value,
            GoogleAdsLandingPageCandidate.score,
        )
        .where(GoogleAdsLandingPageCandidate.account_id == int(account_id))
        .order_by(GoogleAdsLandingPageCandidate.score.desc(), GoogleAdsLandingPageCandidate.updated_at.desc())
        .limit(limit * 8)
    ).all()
    keyword_rows = session.execute(
        select(
            GoogleAdsKeywordCandidate.keyword,
            GoogleAdsKeywordCandidate.normalized_keyword,
            GoogleAdsKeywordCandidate.quality_label,
            GoogleAdsKeywordCandidate.source_dataset_keys,
            GoogleAdsKeywordCandidate.source_snapshot_ids,
            GoogleAdsKeywordCandidate.impressions,
            GoogleAdsKeywordCandidate.clicks,
            GoogleAdsKeywordCandidate.conversions,
            GoogleAdsKeywordCandidate.conversion_value,
            GoogleAdsKeywordCandidate.all_conversions,
            GoogleAdsKeywordCandidate.all_conversions_value,
            GoogleAdsKeywordCandidate.score,
        )
        .where(GoogleAdsKeywordCandidate.account_id == int(account_id))
        .order_by(GoogleAdsKeywordCandidate.score.desc(), GoogleAdsKeywordCandidate.updated_at.desc())
        .limit(limit * 8)
    ).all()
    search_rows = session.execute(
        select(
            GoogleAnalyticsSearchTermCandidate.search_term,
            GoogleAnalyticsSearchTermCandidate.normalized_search_term,
            GoogleAnalyticsSearchTermCandidate.keyword,
            GoogleAnalyticsSearchTermCandidate.campaign_name,
            GoogleAnalyticsSearchTermCandidate.source_snapshot_ids,
            GoogleAnalyticsSearchTermCandidate.sessions,
            GoogleAnalyticsSearchTermCandidate.engaged_sessions,
            GoogleAnalyticsSearchTermCandidate.purchases,
            GoogleAnalyticsSearchTermCandidate.revenue,
            GoogleAnalyticsSearchTermCandidate.score,
        )
        .where(GoogleAnalyticsSearchTermCandidate.account_id == int(account_id))
        .order_by(GoogleAnalyticsSearchTermCandidate.score.desc(), GoogleAnalyticsSearchTermCandidate.updated_at.desc())
        .limit(limit)
    ).all()
    if not page_rows and not keyword_rows and not search_rows:
        return None

    pages: list[dict[str, Any]] = []
    snapshot_ids: list[int] = []
    for row in page_rows:
        dataset_keys = list(row.source_dataset_keys or [])
        if dataset_keys and DATASET_GA4_LANDING_PAGE_ECOMMERCE not in dataset_keys and "GA4" not in dataset_keys:
            continue
        url = canonical_landing_page_url(str(row.normalized_url or row.url or ""))
        if not url:
            continue
        for snapshot_id in row.source_snapshot_ids or []:
            if snapshot_id not in snapshot_ids:
                snapshot_ids.append(snapshot_id)
        page = {
            "url": url,
            "page_key": url.lower(),
            "is_acquisition": _is_ga4_acquisition_url(url),
            "sessions": float(row.impressions or 0),
            "engaged_sessions": float(row.clicks or 0),
            "add_to_carts": 0.0,
            "checkouts": 0.0,
            "purchases": float(row.conversions or 0) + float(row.all_conversions or 0),
            "revenue": float(row.conversion_value or 0) + float(row.all_conversions_value or 0),
            "score": float(row.score or 0),
            "source_snapshot_ids": list(row.source_snapshot_ids or []),
            "quality_label": str(row.quality_label or ""),
        }
        pages.append(page)

    items: list[dict[str, Any]] = []
    for row in keyword_rows:
        dataset_keys = list(row.source_dataset_keys or [])
        if dataset_keys and DATASET_GA4_ITEM_ECOMMERCE not in dataset_keys:
            continue
        keyword = str(row.keyword or "").strip()
        key = str(row.normalized_keyword or normalized_keyword(keyword)).strip()
        if not keyword or not key:
            continue
        for snapshot_id in row.source_snapshot_ids or []:
            if snapshot_id not in snapshot_ids:
                snapshot_ids.append(snapshot_id)
        item = {
            "keyword": keyword,
            "theme_key": key,
            "item_ids": [],
            "item_names": [keyword],
            "item_brands": [],
            "item_categories": [],
            "items_added_to_cart": max(float(row.clicks or 0) - float(row.conversions or 0), 0.0),
            "items_checked_out": 0.0,
            "items_purchased": float(row.conversions or 0) + float(row.all_conversions or 0),
            "item_revenue": float(row.conversion_value or 0) + float(row.all_conversions_value or 0),
            "score": float(row.score or 0),
            "source_snapshot_ids": list(row.source_snapshot_ids or []),
            "quality_label": str(row.quality_label or ""),
        }
        items.append(item)

    search_terms = [
        {
            "search_term": str(row.search_term or ""),
            "theme_key": str(row.normalized_search_term or ""),
            "keyword": str(row.keyword or ""),
            "campaign_name": str(row.campaign_name or ""),
            "sessions": float(row.sessions or 0),
            "engaged_sessions": float(row.engaged_sessions or 0),
            "purchases": float(row.purchases or 0),
            "revenue": float(row.revenue or 0),
            "score": float(row.score or 0),
            "source_snapshot_ids": list(row.source_snapshot_ids or []),
        }
        for row in search_rows
    ]
    for row in search_rows:
        for snapshot_id in row.source_snapshot_ids or []:
            if snapshot_id not in snapshot_ids:
                snapshot_ids.append(snapshot_id)

    ranked_pages = sorted(pages, key=_rank_ga4_signal, reverse=True)
    ranked_items = sorted(items, key=_rank_ga4_signal, reverse=True)
    scale_pages = [item for item in ranked_pages if item["is_acquisition"] and (item["purchases"] > 0 or item["revenue"] > 0)][:limit]
    testing_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"] and item["purchases"] <= 0 and item["revenue"] <= 0 and (item["add_to_carts"] > 0 or item["checkouts"] > 0)
    ][:limit]
    protect_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"] and (item["purchases"] > 0 or item["revenue"] > 0 or item["add_to_carts"] > 0 or item["checkouts"] > 0)
    ][:limit]
    waste_review_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"]
        and item["sessions"] >= 25
        and item["engaged_sessions"] <= 1
        and item["add_to_carts"] <= 0
        and item["checkouts"] <= 0
        and item["purchases"] <= 0
        and item["revenue"] <= 0
    ][:limit]
    excluded_pages = [item for item in ranked_pages if not item["is_acquisition"]][:limit]
    scale_items = [item for item in ranked_items if item["items_purchased"] > 0 or item["item_revenue"] > 0][:limit]
    testing_items = [
        item
        for item in ranked_items
        if item["items_purchased"] <= 0 and item["item_revenue"] <= 0 and (item["items_added_to_cart"] > 0 or item["items_checked_out"] > 0)
    ][:limit]
    protect_keyword_keys = [item["theme_key"] for item in scale_items + testing_items]
    protect_page_keys = [item["page_key"] for item in protect_pages]
    paid_search = {
        "sessions": sum(item["sessions"] for item in search_terms),
        "engaged_sessions": sum(item["engaged_sessions"] for item in search_terms),
        "purchases": sum(item["purchases"] for item in search_terms),
        "revenue": sum(item["revenue"] for item in search_terms),
        "row_count": len(search_terms),
    }
    return {
        "generated_at": utcnow().isoformat(),
        "account_id": int(account_id),
        "source": "ga4_candidate_tables",
        "snapshot_ids": snapshot_ids,
        "datasets": {
            DATASET_GA4_LANDING_PAGE_ECOMMERCE: [],
            DATASET_GA4_ITEM_ECOMMERCE: [],
            DATASET_GA4_GOOGLE_ADS_SEARCH: [],
        },
        "summary": {
            "scale_page_count": len(scale_pages),
            "testing_page_count": len(testing_pages),
            "protected_page_count": len(protect_pages),
            "waste_review_page_count": len(waste_review_pages),
            "scale_item_keyword_count": len(scale_items),
            "testing_item_keyword_count": len(testing_items),
            "ga4_google_ads_search_rows": len(search_terms),
            "paid_search": paid_search,
        },
        "scale_landing_pages": scale_pages,
        "testing_landing_pages": testing_pages,
        "protect_from_negative_pages": protect_pages,
        "waste_review_pages": waste_review_pages,
        "excluded_non_acquisition_pages": excluded_pages,
        "scale_item_keywords": scale_items,
        "testing_item_keywords": testing_items,
        "ga4_search_terms": search_terms,
        "protect_keyword_keys": list(dict.fromkeys(protect_keyword_keys))[:limit],
        "protect_page_keys": list(dict.fromkeys(protect_page_keys))[:limit],
        "basis": (
            "GA4 purchases/revenue promote pages and product terms to Core / Scale. GA4 add-to-cart or checkout "
            "without purchase promotes them to Testing / Discovery and protects them from automatic negatives. "
            "This matrix was built from saved candidate tables to avoid reloading large raw GA4 snapshots."
        ),
    }


def ga4_ads_signal_matrix(session: Session, account_id: int, *, limit: int = 50) -> dict[str, Any]:
    limit = min(max(int(limit or 50), 5), 500)
    candidate_matrix = _ga4_matrix_from_candidates(session, int(account_id), limit=limit)
    if candidate_matrix is not None:
        return candidate_matrix
    snapshots = session.scalars(
        select(GoogleAnalyticsDataSnapshot)
        .where(
            GoogleAnalyticsDataSnapshot.account_id == int(account_id),
            GoogleAnalyticsDataSnapshot.dataset_key.in_(
                [
                    DATASET_GA4_LANDING_PAGE_ECOMMERCE,
                    DATASET_GA4_ITEM_ECOMMERCE,
                    DATASET_GA4_TRAFFIC_ECOMMERCE,
                    DATASET_GA4_GOOGLE_ADS_SEARCH,
                ]
            ),
        )
        .order_by(GoogleAnalyticsDataSnapshot.dataset_key, GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
        .limit(200)
    ).all()
    latest_by_dataset: dict[str, list[GoogleAnalyticsDataSnapshot]] = {}
    for snapshot in snapshots:
        latest_by_dataset.setdefault(snapshot.dataset_key, [])
        if len(latest_by_dataset[snapshot.dataset_key]) < 3:
            latest_by_dataset[snapshot.dataset_key].append(snapshot)

    pages: dict[str, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    traffic_rows: list[dict[str, Any]] = []
    search_rows: list[dict[str, Any]] = []

    for snapshot in latest_by_dataset.get(DATASET_GA4_LANDING_PAGE_ECOMMERCE, []):
        stream = snapshot.stream
        for row in _ga4_snapshot_rows(snapshot):
            url = canonical_landing_page_url(_absolute_landing_url(stream, str(row.get("landingPagePlusQueryString") or "")))
            if not url:
                continue
            page_key = url.lower()
            is_acquisition = _is_ga4_acquisition_url(url)
            entry = pages.setdefault(
                page_key,
                {
                    "url": url,
                    "page_key": page_key,
                    "is_acquisition": is_acquisition,
                    "sessions": 0.0,
                    "engaged_sessions": 0.0,
                    "add_to_carts": 0.0,
                    "checkouts": 0.0,
                    "purchases": 0.0,
                    "revenue": 0.0,
                    "score": 0.0,
                    "source_snapshot_ids": [],
                },
            )
            entry["is_acquisition"] = bool(entry["is_acquisition"] and is_acquisition)
            entry["sessions"] += float(row.get("sessions") or 0)
            entry["engaged_sessions"] += float(row.get("engagedSessions") or 0)
            entry["add_to_carts"] += float(row.get("addToCarts") or 0)
            entry["checkouts"] += float(row.get("checkouts") or 0)
            entry["purchases"] += float(row.get("ecommercePurchases") or 0)
            entry["revenue"] += float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
            entry["score"] += _ga4_landing_page_score(row)
            if snapshot.id not in entry["source_snapshot_ids"]:
                entry["source_snapshot_ids"].append(snapshot.id)

    for snapshot in latest_by_dataset.get(DATASET_GA4_ITEM_ECOMMERCE, []):
        for row in _ga4_snapshot_rows(snapshot):
            keyword = _ga4_item_keyword(row)
            if not keyword:
                continue
            key = normalized_keyword(keyword)
            entry = items.setdefault(
                key,
                {
                    "keyword": keyword,
                    "theme_key": key,
                    "item_ids": [],
                    "item_names": [],
                    "item_brands": [],
                    "item_categories": [],
                    "items_added_to_cart": 0.0,
                    "items_checked_out": 0.0,
                    "items_purchased": 0.0,
                    "item_revenue": 0.0,
                    "score": 0.0,
                    "source_snapshot_ids": [],
                },
            )
            for target, source_key in [
                ("item_ids", "itemId"),
                ("item_names", "itemName"),
                ("item_brands", "itemBrand"),
                ("item_categories", "itemCategory"),
            ]:
                value = str(row.get(source_key) or "").strip()
                if value and value not in entry[target] and len(entry[target]) < 10:
                    entry[target].append(value)
            entry["items_added_to_cart"] += float(row.get("itemsAddedToCart") or 0)
            entry["items_checked_out"] += float(row.get("itemsCheckedOut") or 0)
            entry["items_purchased"] += float(row.get("itemsPurchased") or 0)
            entry["item_revenue"] += float(row.get("itemRevenue") or 0)
            entry["score"] += _ga4_item_score(row)
            if snapshot.id not in entry["source_snapshot_ids"]:
                entry["source_snapshot_ids"].append(snapshot.id)

    for snapshot in latest_by_dataset.get(DATASET_GA4_TRAFFIC_ECOMMERCE, []):
        traffic_rows.extend(_ga4_snapshot_rows(snapshot))
    for snapshot in latest_by_dataset.get(DATASET_GA4_GOOGLE_ADS_SEARCH, []):
        search_rows.extend(_ga4_snapshot_rows(snapshot))

    ranked_pages = sorted(pages.values(), key=_rank_ga4_signal, reverse=True)
    ranked_items = sorted(items.values(), key=_rank_ga4_signal, reverse=True)
    scale_pages = [item for item in ranked_pages if item["is_acquisition"] and (item["purchases"] > 0 or item["revenue"] > 0)][:limit]
    testing_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"] and item["purchases"] <= 0 and item["revenue"] <= 0 and (item["add_to_carts"] > 0 or item["checkouts"] > 0)
    ][:limit]
    protect_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"] and (item["purchases"] > 0 or item["revenue"] > 0 or item["add_to_carts"] > 0 or item["checkouts"] > 0)
    ][:limit]
    waste_review_pages = [
        item
        for item in ranked_pages
        if item["is_acquisition"]
        and item["sessions"] >= 25
        and item["engaged_sessions"] <= 1
        and item["add_to_carts"] <= 0
        and item["checkouts"] <= 0
        and item["purchases"] <= 0
        and item["revenue"] <= 0
    ][:limit]
    excluded_pages = [item for item in ranked_pages if not item["is_acquisition"]][:limit]

    scale_items = [item for item in ranked_items if item["items_purchased"] > 0 or item["item_revenue"] > 0][:limit]
    testing_items = [
        item
        for item in ranked_items
        if item["items_purchased"] <= 0 and item["item_revenue"] <= 0 and (item["items_added_to_cart"] > 0 or item["items_checked_out"] > 0)
    ][:limit]
    protect_keyword_keys = [item["theme_key"] for item in scale_items + testing_items]
    protect_page_keys = [item["page_key"] for item in protect_pages]

    paid_search_rows = [
        row
        for row in traffic_rows
        if "paid search" in str(row.get("sessionPrimaryChannelGroup") or "").lower()
        or "google / cpc" in str(row.get("sessionSourceMedium") or "").lower()
        or "google cpc" in str(row.get("sessionSourceMedium") or "").lower()
    ]
    paid_search = {
        "sessions": sum(float(row.get("sessions") or 0) for row in paid_search_rows),
        "engaged_sessions": sum(float(row.get("engagedSessions") or 0) for row in paid_search_rows),
        "purchases": sum(float(row.get("ecommercePurchases") or 0) for row in paid_search_rows),
        "revenue": sum(float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0) for row in paid_search_rows),
        "row_count": len(paid_search_rows),
    }
    return {
        "generated_at": utcnow().isoformat(),
        "account_id": int(account_id),
        "source": "ga4_ecommerce_snapshots",
        "snapshot_ids": [snapshot.id for values in latest_by_dataset.values() for snapshot in values],
        "datasets": {key: [snapshot.id for snapshot in values] for key, values in latest_by_dataset.items()},
        "summary": {
            "scale_page_count": len(scale_pages),
            "testing_page_count": len(testing_pages),
            "protected_page_count": len(protect_pages),
            "waste_review_page_count": len(waste_review_pages),
            "scale_item_keyword_count": len(scale_items),
            "testing_item_keyword_count": len(testing_items),
            "ga4_google_ads_search_rows": len(search_rows),
            "paid_search": paid_search,
        },
        "scale_landing_pages": scale_pages,
        "testing_landing_pages": testing_pages,
        "protect_from_negative_pages": protect_pages,
        "waste_review_pages": waste_review_pages,
        "excluded_non_acquisition_pages": excluded_pages,
        "scale_item_keywords": scale_items,
        "testing_item_keywords": testing_items,
        "protect_keyword_keys": list(dict.fromkeys(protect_keyword_keys))[:limit],
        "protect_page_keys": list(dict.fromkeys(protect_page_keys))[:limit],
        "basis": (
            "GA4 purchases/revenue promote pages and product terms to Core / Scale. GA4 add-to-cart or checkout "
            "without purchase promotes them to Testing / Discovery and protects them from automatic negatives. "
            "Cart, checkout, payment, login, and policy URLs are excluded from targeting. Poor sessions with no "
            "engagement or funnel intent are only Waste review evidence, not an automatic negative by themselves."
        ),
    }


def ga4_ads_enhancement_plan(session: Session, account_id: Optional[int] = None, *, limit: int = 25) -> dict[str, Any]:
    if account_id:
        return ga4_ads_signal_matrix(session, int(account_id), limit=limit)
    query = (
        select(GoogleAnalyticsDataSnapshot)
        .where(GoogleAnalyticsDataSnapshot.dataset_key == DATASET_GA4_LANDING_PAGE_ECOMMERCE)
        .order_by(GoogleAnalyticsDataSnapshot.fetched_at.desc(), GoogleAnalyticsDataSnapshot.id.desc())
        .limit(200)
    )
    if account_id:
        query = query.where(GoogleAnalyticsDataSnapshot.account_id == int(account_id))
    snapshots = session.scalars(query).all()
    pages: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        stream = snapshot.stream
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = canonical_landing_page_url(_absolute_landing_url(stream, str(row.get("landingPagePlusQueryString") or "")))
            if not url:
                continue
            entry = pages.setdefault(
                url,
                {
                    "url": url,
                    "sessions": 0.0,
                    "engaged_sessions": 0.0,
                    "add_to_carts": 0.0,
                    "checkouts": 0.0,
                    "purchases": 0.0,
                    "revenue": 0.0,
                    "score": 0.0,
                    "source_snapshot_ids": [],
                },
            )
            entry["sessions"] += float(row.get("sessions") or 0)
            entry["engaged_sessions"] += float(row.get("engagedSessions") or 0)
            entry["add_to_carts"] += float(row.get("addToCarts") or 0)
            entry["checkouts"] += float(row.get("checkouts") or 0)
            entry["purchases"] += float(row.get("ecommercePurchases") or 0)
            entry["revenue"] += float(row.get("purchaseRevenue") or row.get("totalRevenue") or 0)
            entry["score"] += _ga4_landing_page_score(row)
            if snapshot.id not in entry["source_snapshot_ids"]:
                entry["source_snapshot_ids"].append(snapshot.id)
    ranked = sorted(pages.values(), key=lambda item: (item["score"], item["revenue"], item["purchases"]), reverse=True)
    scale = [item for item in ranked if item["purchases"] > 0 or item["revenue"] > 0][:limit]
    testing = [item for item in ranked if item["purchases"] <= 0 and item["add_to_carts"] + item["checkouts"] > 0][:limit]
    protect = [item for item in ranked if item["engaged_sessions"] >= 10 or item["add_to_carts"] > 0 or item["checkouts"] > 0][:limit]
    return {
        "generated_at": utcnow().isoformat(),
        "account_id": account_id,
        "source": "ga4_ecommerce_snapshots",
        "scale_landing_pages": scale,
        "testing_landing_pages": testing,
        "protect_from_waste_pages": protect,
        "basis": (
            "Scale pages have GA4 purchases or revenue. Testing pages have cart or checkout intent without purchase. "
            "Protected pages have engagement or funnel intent and should not be made negative/waste without stronger Ads evidence."
        ),
    }


def ga4_status_summary(session: Session) -> dict[str, Any]:
    return {
        "connection_count": int(session.scalar(select(func.count(GoogleAnalyticsConnection.id))) or 0),
        "property_count": int(session.scalar(select(func.count(GoogleAnalyticsProperty.id))) or 0),
        "stream_count": int(session.scalar(select(func.count(GoogleAnalyticsWebStream.id))) or 0),
        "mapping_count": int(session.scalar(select(func.count(GoogleAnalyticsWebsiteMapping.id))) or 0),
        "snapshot_count": int(session.scalar(select(func.count(GoogleAnalyticsDataSnapshot.id))) or 0),
        "latest_pull_at": session.scalar(select(func.max(GoogleAnalyticsDataSnapshot.fetched_at))),
    }
