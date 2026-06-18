from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    GoogleAdsAccount,
    GoogleSearchConsoleConnection,
    GoogleSearchConsoleDataSnapshot,
    GoogleSearchConsoleQueryCandidate,
    GoogleSearchConsoleSite,
    GoogleSearchConsoleWebsiteMapping,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.google_ads_landing_page_bank import canonical_landing_page_url, landing_page_hash
from app.services.google_ads_keyword_plan import clean_keyword, normalized_keyword, usable_keyword
from app.services.google_ads_snapshot_store import query_hash
from app.services.google_analytics import _root_domain, fetch_google_email


GOOGLE_SEARCH_CONSOLE_SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
SEARCH_CONSOLE_API_BASE = "https://www.googleapis.com/webmasters/v3"
DATASET_GSC_SEARCH_ANALYTICS = "gsc_search_analytics_query_page"
GSC_SNAPSHOT_TTL_HOURS = 24
GSC_RECENT_DAYS = 28
GSC_DAILY_DAYS = 3
GSC_ROW_LIMIT = 25_000
GSC_DAILY_ROW_LIMIT = 10_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _credentials_for_connection(connection: GoogleSearchConsoleConnection) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=connection.refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=connection.client_id,
        client_secret=connection.client_secret,
        scopes=list(GOOGLE_SEARCH_CONSOLE_SCOPES),
    )


def authorized_search_console_session(connection: GoogleSearchConsoleConnection) -> AuthorizedSession:
    credentials = _credentials_for_connection(connection)
    credentials.refresh(Request())
    return AuthorizedSession(credentials)


def _get_json(http: AuthorizedSession, url: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = http.get(url, params=params or {}, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Google Search Console API error {response.status_code}: {response.text[:1000]}")
    return response.json()


def _post_json(http: AuthorizedSession, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = http.post(url, json=payload, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Google Search Console API error {response.status_code}: {response.text[:1000]}")
    return response.json()


def _site_root_domain(site_url: str) -> str:
    value = str(site_url or "").strip()
    if value.startswith("sc-domain:"):
        return _root_domain(value.replace("sc-domain:", "", 1))
    return _root_domain(value)


def _url_host(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    return (parsed.netloc or parsed.path).split("/")[0].lower().lstrip("www.")


def _site_matches_website(site_url: str, website_domain: str) -> bool:
    site_root = _site_root_domain(site_url)
    website_root = _root_domain(website_domain)
    if not site_root or not website_root or site_root != website_root:
        return False
    if str(site_url or "").startswith("sc-domain:"):
        return True
    site_host = _url_host(site_url)
    website_host = _url_host(website_domain)
    return not site_host or not website_host or site_host == website_host or site_host.endswith(f".{website_root}")


def _mapping_exists(
    session: Session,
    *,
    site_id: int,
    store_id: int | None,
    website_id: int,
    account_id: int | None,
) -> GoogleSearchConsoleWebsiteMapping | None:
    query = select(GoogleSearchConsoleWebsiteMapping).where(
        GoogleSearchConsoleWebsiteMapping.search_console_site_id == int(site_id),
        GoogleSearchConsoleWebsiteMapping.website_id == int(website_id or 0),
    )
    query = query.where(
        GoogleSearchConsoleWebsiteMapping.store_id.is_(None)
        if store_id is None
        else GoogleSearchConsoleWebsiteMapping.store_id == store_id
    )
    query = query.where(
        GoogleSearchConsoleWebsiteMapping.account_id.is_(None)
        if account_id is None
        else GoogleSearchConsoleWebsiteMapping.account_id == account_id
    )
    return session.scalar(query.limit(1))


def _upsert_site(
    session: Session,
    connection: GoogleSearchConsoleConnection,
    site_payload: dict[str, Any],
    *,
    now: datetime,
) -> GoogleSearchConsoleSite | None:
    site_url = str(site_payload.get("siteUrl") or "").strip()
    if not site_url:
        return None
    row = session.scalar(select(GoogleSearchConsoleSite).where(GoogleSearchConsoleSite.site_url == site_url).limit(1))
    if row is None:
        row = GoogleSearchConsoleSite(connection_id=connection.id, site_url=site_url)
        session.add(row)
    row.connection_id = connection.id
    row.site_url = site_url
    row.permission_level = str(site_payload.get("permissionLevel") or "")
    row.is_active = True
    row.last_discovery_at = now
    return row


def auto_map_search_console_sites(session: Session) -> int:
    sites = session.scalars(select(GoogleSearchConsoleSite).order_by(GoogleSearchConsoleSite.id)).all()
    websites = session.scalars(select(OdooWebsite).where(OdooWebsite.is_active.is_(True))).all()
    account_mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
    ).all()
    accounts_by_website: dict[tuple[int, int], list[int]] = {}
    for mapping in account_mappings:
        accounts_by_website.setdefault((mapping.store_id, int(mapping.website_id or 0)), []).append(mapping.account_id)

    created = 0
    for site in sites:
        for website in websites:
            if not _site_matches_website(site.site_url, website.domain):
                continue
            account_ids = accounts_by_website.get((website.store_id, int(website.website_id or 0))) or [None]
            for account_id in account_ids:
                row = _mapping_exists(
                    session,
                    site_id=site.id,
                    store_id=website.store_id,
                    website_id=int(website.website_id or 0),
                    account_id=account_id,
                )
                if row is None:
                    row = GoogleSearchConsoleWebsiteMapping(
                        search_console_site_id=site.id,
                        store_id=website.store_id,
                        website_id=int(website.website_id or 0),
                        account_id=account_id,
                        match_source="domain_auto",
                        match_confidence=0.92 if str(site.site_url).startswith("sc-domain:") else 0.98,
                    )
                    session.add(row)
                    created += 1
                else:
                    row.is_active = True
                    row.match_source = row.match_source or "domain_auto"
                    row.match_confidence = max(float(row.match_confidence or 0), 0.92)
                site.is_active = True
    return created


def discover_search_console_connection(session: Session, connection_id: int) -> dict[str, Any]:
    connection = session.get(GoogleSearchConsoleConnection, int(connection_id))
    if connection is None:
        raise ValueError("Google Search Console connection not found.")
    now = utcnow()
    http = authorized_search_console_session(connection)
    payload = _get_json(http, f"{SEARCH_CONSOLE_API_BASE}/sites")
    site_entries = payload.get("siteEntry") if isinstance(payload.get("siteEntry"), list) else []
    site_count = 0
    for site_payload in site_entries:
        if not isinstance(site_payload, dict):
            continue
        if _upsert_site(session, connection, site_payload, now=now) is not None:
            site_count += 1
    auto_mapping_count = auto_map_search_console_sites(session)
    connection.last_discovery_at = now
    connection.last_discovery_error = None
    connection.discovered_site_count = site_count
    session.commit()
    return {
        "connection_id": connection.id,
        "site_count": site_count,
        "auto_mapping_count": auto_mapping_count,
        "discovered_at": now.isoformat(),
    }


def ensure_account_search_console_mapping(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping).where(
            OdooStoreGoogleAdsMapping.account_id == account.id,
            OdooStoreGoogleAdsMapping.is_active.is_(True),
        )
    ).all()
    if not mappings:
        return {
            "name": "search_console_property_auto_connect",
            "status": "skipped",
            "reason": "No active Odoo website mapping exists for this Google Ads account.",
            "matched": 0,
        }

    website_rows: list[tuple[OdooStoreGoogleAdsMapping, OdooWebsite]] = []
    for mapping in mappings:
        website = session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.website_id == int(mapping.website_id or 0),
                OdooWebsite.store_id == mapping.store_id,
                OdooWebsite.is_active.is_(True),
            )
        )
        if website is not None and _root_domain(website.domain):
            website_rows.append((mapping, website))
    if not website_rows:
        return {
            "name": "search_console_property_auto_connect",
            "status": "skipped",
            "reason": "The active Odoo mapping has no website domain to match against Search Console sites.",
            "matched": 0,
        }

    sites = session.scalars(select(GoogleSearchConsoleSite).order_by(GoogleSearchConsoleSite.site_url)).all()
    connected: list[dict[str, Any]] = []
    for site in sites:
        for mapping, website in website_rows:
            if not _site_matches_website(site.site_url, website.domain):
                continue
            row = _mapping_exists(
                session,
                site_id=site.id,
                store_id=mapping.store_id,
                website_id=int(website.website_id or 0),
                account_id=account.id,
            )
            if row is None:
                row = GoogleSearchConsoleWebsiteMapping(
                    search_console_site_id=site.id,
                    store_id=mapping.store_id,
                    website_id=int(website.website_id or 0),
                    account_id=account.id,
                    match_source="domain_auto_account_guard",
                    match_confidence=0.99,
                    is_active=True,
                )
                session.add(row)
                session.flush()
                action = "created"
            else:
                action = "reactivated" if not row.is_active else "verified"
                row.is_active = True
                row.match_source = row.match_source or "domain_auto_account_guard"
                row.match_confidence = max(float(row.match_confidence or 0), 0.99)
            site.is_active = True
            connected.append(
                {
                    "action": action,
                    "mapping_id": row.id,
                    "site_url": site.site_url,
                    "permission_level": site.permission_level,
                    "website_domain": website.domain,
                    "root_domain": _root_domain(website.domain),
                }
            )
    if connected:
        session.commit()
        return {
            "name": "search_console_property_auto_connect",
            "status": "connected",
            "reason": "Matched the active Odoo website domain to Search Console sites before Ads planning.",
            "matched": len(connected),
            "connections": connected,
        }
    return {
        "name": "search_console_property_auto_connect",
        "status": "no_match",
        "reason": "No discovered Search Console site matched the active Odoo website domain.",
        "matched": 0,
        "website_domains": [website.domain for _mapping, website in website_rows],
    }


def gsc_date_window(mode: str, days: int = GSC_RECENT_DAYS) -> tuple[str, date, date]:
    raw_mode = str(mode or "").strip().lower()
    if raw_mode == "all_time":
        mode = "all_time"
    elif raw_mode in {"daily", "incremental"}:
        mode = "daily"
    else:
        mode = "recent"
    end_date = date.today()
    if mode == "all_time":
        return mode, date(2020, 1, 1), end_date
    max_days = 14 if mode == "daily" else 365
    days = min(max(int(days or (GSC_DAILY_DAYS if mode == "daily" else GSC_RECENT_DAYS)), 1), max_days)
    return mode, end_date - timedelta(days=days - 1), end_date


def gsc_scope(mode: str, start_date: date, end_date: date, days: int = GSC_RECENT_DAYS) -> str:
    if mode == "all_time":
        return f"all_time:{start_date.isoformat()}:{end_date.isoformat()}"
    if mode == "daily":
        return f"daily_last_{int(days)}d:{start_date.isoformat()}:{end_date.isoformat()}"
    return f"last_{int(days)}d:{start_date.isoformat()}:{end_date.isoformat()}"


def _search_analytics_request(
    *,
    start_date: date,
    end_date: date,
    limit: int,
    start_row: int,
    country: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": ["query", "page", "country", "device"],
        "rowLimit": min(max(int(limit or GSC_ROW_LIMIT), 1), GSC_ROW_LIMIT),
        "startRow": max(int(start_row or 0), 0),
        "dataState": "final",
    }
    if country:
        payload["dimensionFilterGroups"] = [
            {
                "filters": [
                    {
                        "dimension": "country",
                        "operator": "equals",
                        "expression": country.lower(),
                    }
                ]
            }
        ]
    return payload


def _parse_search_analytics_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in response.get("rows") or []:
        if not isinstance(row, dict):
            continue
        keys = row.get("keys") if isinstance(row.get("keys"), list) else []
        parsed = {
            "query": str(keys[0] if len(keys) > 0 else "").strip(),
            "page": str(keys[1] if len(keys) > 1 else "").strip(),
            "country": str(keys[2] if len(keys) > 2 else "").strip().lower(),
            "device": str(keys[3] if len(keys) > 3 else "").strip().lower(),
            "clicks": int(float(row.get("clicks") or 0)),
            "impressions": int(float(row.get("impressions") or 0)),
            "ctr": float(row.get("ctr") or 0),
            "position": float(row.get("position") or 0),
        }
        rows.append(parsed)
    return rows


def gsc_target_key(site: GoogleSearchConsoleSite, account: Optional[GoogleAdsAccount]) -> str:
    account_part = account.customer_id if account is not None else "unmapped"
    return f"gsc:site:{site.id}:{hashlib.sha256(site.site_url.encode()).hexdigest()[:16]}:ads:{account_part}"


def _get_fresh_gsc_snapshot(
    session: Session,
    *,
    site: GoogleSearchConsoleSite,
    account: Optional[GoogleAdsAccount],
    dataset_key: str,
    scope_key: str,
    query_payload: dict[str, Any],
    now: Optional[datetime] = None,
) -> Optional[GoogleSearchConsoleDataSnapshot]:
    now = now or utcnow()
    return session.scalar(
        select(GoogleSearchConsoleDataSnapshot)
        .where(
            GoogleSearchConsoleDataSnapshot.target_key == gsc_target_key(site, account),
            GoogleSearchConsoleDataSnapshot.dataset_key == dataset_key,
            GoogleSearchConsoleDataSnapshot.scope_key == scope_key,
            GoogleSearchConsoleDataSnapshot.schema_version == 1,
            GoogleSearchConsoleDataSnapshot.query_hash == query_hash(json.dumps(query_payload, sort_keys=True)),
            or_(
                GoogleSearchConsoleDataSnapshot.expires_at.is_(None),
                GoogleSearchConsoleDataSnapshot.expires_at > now,
            ),
        )
        .order_by(GoogleSearchConsoleDataSnapshot.fetched_at.desc(), GoogleSearchConsoleDataSnapshot.id.desc())
        .limit(1)
    )


def _upsert_gsc_snapshot(
    session: Session,
    *,
    site: GoogleSearchConsoleSite,
    account: Optional[GoogleAdsAccount],
    dataset_key: str,
    payload: dict[str, Any],
    scope_key: str,
    query_payload: dict[str, Any],
    source_job_id: Optional[int],
    expires_at: Optional[datetime],
) -> GoogleSearchConsoleDataSnapshot:
    target_key = gsc_target_key(site, account)
    q_hash = query_hash(json.dumps(query_payload, sort_keys=True))
    stmt = insert(GoogleSearchConsoleDataSnapshot).values(
        target_key=target_key,
        search_console_site_id=site.id,
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
            GoogleSearchConsoleDataSnapshot.target_key,
            GoogleSearchConsoleDataSnapshot.dataset_key,
            GoogleSearchConsoleDataSnapshot.scope_key,
            GoogleSearchConsoleDataSnapshot.schema_version,
            GoogleSearchConsoleDataSnapshot.query_hash,
        ],
        set_={
            "search_console_site_id": stmt.excluded.search_console_site_id,
            "account_id": stmt.excluded.account_id,
            "source_job_id": stmt.excluded.source_job_id,
            "row_count": stmt.excluded.row_count,
            "payload_json": stmt.excluded.payload_json,
            "fetched_at": stmt.excluded.fetched_at,
            "expires_at": stmt.excluded.expires_at,
        },
    ).returning(GoogleSearchConsoleDataSnapshot.id)
    snapshot_id = session.execute(stmt).scalar_one()
    return session.get(GoogleSearchConsoleDataSnapshot, snapshot_id)


def active_search_console_report_targets(
    session: Session,
    *,
    connection_ids: Optional[list[int]] = None,
    site_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    query = (
        select(GoogleSearchConsoleWebsiteMapping)
        .join(GoogleSearchConsoleSite, GoogleSearchConsoleWebsiteMapping.search_console_site_id == GoogleSearchConsoleSite.id)
        .where(
            GoogleSearchConsoleWebsiteMapping.is_active.is_(True),
            GoogleSearchConsoleSite.is_active.is_(True),
        )
        .order_by(GoogleSearchConsoleSite.site_url, GoogleSearchConsoleWebsiteMapping.id)
    )
    if connection_ids:
        query = query.where(GoogleSearchConsoleSite.connection_id.in_([int(item) for item in connection_ids]))
    if site_ids:
        query = query.where(GoogleSearchConsoleSite.id.in_([int(item) for item in site_ids]))
    if account_ids:
        query = query.where(GoogleSearchConsoleWebsiteMapping.account_id.in_([int(item) for item in account_ids]))
    mappings = session.scalars(query).all()
    targets = [{"mapping": mapping, "site": mapping.site, "account": mapping.account} for mapping in mappings]
    if targets:
        return targets

    site_query = select(GoogleSearchConsoleSite).where(GoogleSearchConsoleSite.is_active.is_(True)).order_by(
        GoogleSearchConsoleSite.site_url
    )
    if connection_ids:
        site_query = site_query.where(GoogleSearchConsoleSite.connection_id.in_([int(item) for item in connection_ids]))
    if site_ids:
        site_query = site_query.where(GoogleSearchConsoleSite.id.in_([int(item) for item in site_ids]))
    if account_ids:
        return []
    return [{"mapping": None, "site": site, "account": None} for site in session.scalars(site_query).all()]


def _run_search_analytics(
    http: AuthorizedSession,
    site: GoogleSearchConsoleSite,
    *,
    start_date: date,
    end_date: date,
    max_rows: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    start_row = 0
    limit = min(max(int(max_rows or GSC_ROW_LIMIT), 1), GSC_ROW_LIMIT)
    first_request = _search_analytics_request(start_date=start_date, end_date=end_date, limit=limit, start_row=0)
    encoded_site = quote(site.site_url, safe="")
    while len(all_rows) < max_rows:
        request_payload = _search_analytics_request(
            start_date=start_date,
            end_date=end_date,
            limit=min(limit, max_rows - len(all_rows)),
            start_row=start_row,
        )
        response = _post_json(http, f"{SEARCH_CONSOLE_API_BASE}/sites/{encoded_site}/searchAnalytics/query", request_payload)
        rows = _parse_search_analytics_rows(response)
        if not rows:
            break
        all_rows.extend(rows)
        start_row += len(rows)
        if len(rows) < request_payload["rowLimit"]:
            break
    return first_request, all_rows, {"api_limit_note": "Search Console returns top rows and may not expose every row."}


def _gsc_quality(row: dict[str, Any]) -> str:
    clicks = int(row.get("clicks") or 0)
    impressions = int(row.get("impressions") or 0)
    position = float(row.get("position") or 0)
    if clicks >= 10:
        return "clicked"
    if clicks > 0:
        return "watch"
    if impressions >= 100 and (position <= 20 or position == 0):
        return "opportunity"
    return "watch"


def _gsc_score(row: dict[str, Any]) -> float:
    clicks = float(row.get("clicks") or 0)
    impressions = float(row.get("impressions") or 0)
    ctr = float(row.get("ctr") or 0)
    position = float(row.get("position") or 0)
    position_boost = max(0.0, 25.0 - position) * 20 if position else 0.0
    opportunity_boost = impressions * max(0.0, position - 3.0) * 0.05 if clicks == 0 else 0.0
    return (clicks * 2500) + (impressions * 4) + (ctr * 1500) + position_boost + opportunity_boost


def _gsc_snapshot_rows(snapshot: GoogleSearchConsoleDataSnapshot) -> list[dict[str, Any]]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    rows = payload.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, str, str, str, str], dict[str, Any]] = {}
    quality_rank = {"watch": 0, "opportunity": 1, "clicked": 2}
    for candidate in candidates:
        key = (
            int(candidate.get("account_id") or 0),
            str(candidate.get("normalized_query") or ""),
            str(candidate.get("page_url_hash") or ""),
            str(candidate.get("country") or ""),
            str(candidate.get("device") or ""),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
            continue
        for metric in ("clicks", "impressions", "score"):
            existing[metric] = (existing.get(metric) or 0) + (candidate.get(metric) or 0)
        existing["ctr"] = max(float(existing.get("ctr") or 0), float(candidate.get("ctr") or 0))
        existing["position"] = min(
            float(existing.get("position") or candidate.get("position") or 0),
            float(candidate.get("position") or existing.get("position") or 0),
        )
        if quality_rank.get(candidate.get("quality_label") or "watch", 0) > quality_rank.get(
            existing.get("quality_label") or "watch",
            0,
        ):
            existing["quality_label"] = candidate.get("quality_label")
        for list_key in ("source_dataset_keys", "source_scope_keys", "source_snapshot_ids"):
            values = existing.setdefault(list_key, [])
            for value in candidate.get(list_key) or []:
                if value not in values:
                    values.append(value)
    return list(by_key.values())


def upsert_search_console_query_candidates(
    session: Session,
    snapshots: list[GoogleSearchConsoleDataSnapshot],
    *,
    source_job_id: Optional[int] = None,
) -> int:
    candidates: list[dict[str, Any]] = []
    now = utcnow()
    for snapshot in snapshots:
        if snapshot.dataset_key != DATASET_GSC_SEARCH_ANALYTICS or snapshot.account_id is None:
            continue
        for row in _gsc_snapshot_rows(snapshot):
            query = clean_keyword(str(row.get("query") or ""))
            if not usable_keyword(query):
                continue
            page_url = canonical_landing_page_url(str(row.get("page") or ""))
            if not page_url:
                continue
            normalized = normalized_keyword(query)
            source_row = {
                "dataset_key": snapshot.dataset_key,
                "snapshot_id": snapshot.id,
                "scope_key": snapshot.scope_key,
                "query": query,
                "page": page_url,
                "country": row.get("country"),
                "device": row.get("device"),
                "clicks": row.get("clicks"),
                "impressions": row.get("impressions"),
                "ctr": row.get("ctr"),
                "position": row.get("position"),
            }
            candidates.append(
                {
                    "account_id": snapshot.account_id,
                    "search_console_site_id": snapshot.search_console_site_id,
                    "query": query,
                    "normalized_query": normalized,
                    "page_url": page_url,
                    "page_url_hash": landing_page_hash(page_url),
                    "country": str(row.get("country") or "").lower(),
                    "device": str(row.get("device") or "").lower(),
                    "quality_label": _gsc_quality(row),
                    "review_status": "new",
                    "source_dataset_keys": [snapshot.dataset_key],
                    "source_scope_keys": [snapshot.scope_key],
                    "source_snapshot_ids": [snapshot.id],
                    "clicks": int(row.get("clicks") or 0),
                    "impressions": int(row.get("impressions") or 0),
                    "ctr": float(row.get("ctr") or 0),
                    "position": float(row.get("position") or 0),
                    "score": _gsc_score(row),
                    "source_json": {
                        "source": "google_search_console_search_analytics",
                        "source_job_id": source_job_id,
                        "pulled_at": now.isoformat(),
                        "dedupe_key": "account_id+normalized_query+page_url_hash+country+device",
                        "source_rows": [source_row],
                    },
                    "last_seen_at": now,
                    "last_pulled_at": now,
                    "last_source_job_id": source_job_id,
                }
            )
    saved = 0
    for batch_start in range(0, len(candidates), 500):
        batch = _dedupe_candidates(candidates[batch_start : batch_start + 500])
        if not batch:
            continue
        stmt = insert(GoogleSearchConsoleQueryCandidate).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                GoogleSearchConsoleQueryCandidate.account_id,
                GoogleSearchConsoleQueryCandidate.normalized_query,
                GoogleSearchConsoleQueryCandidate.page_url_hash,
                GoogleSearchConsoleQueryCandidate.country,
                GoogleSearchConsoleQueryCandidate.device,
            ],
            set_={
                "query": stmt.excluded.query,
                "search_console_site_id": stmt.excluded.search_console_site_id,
                "page_url": stmt.excluded.page_url,
                "quality_label": stmt.excluded.quality_label,
                "source_dataset_keys": stmt.excluded.source_dataset_keys,
                "source_scope_keys": stmt.excluded.source_scope_keys,
                "source_snapshot_ids": stmt.excluded.source_snapshot_ids,
                "clicks": stmt.excluded.clicks,
                "impressions": stmt.excluded.impressions,
                "ctr": stmt.excluded.ctr,
                "position": stmt.excluded.position,
                "score": stmt.excluded.score,
                "source_json": stmt.excluded.source_json,
                "last_seen_at": stmt.excluded.last_seen_at,
                "last_pulled_at": stmt.excluded.last_pulled_at,
                "last_source_job_id": stmt.excluded.last_source_job_id,
            },
        )
        session.execute(stmt)
        saved += len(batch)
    if saved:
        session.flush()
    return saved


def sync_search_console_search_analytics(
    session: Session,
    *,
    mode: str = "recent",
    days: int = GSC_RECENT_DAYS,
    max_rows: int = GSC_ROW_LIMIT,
    force: bool = False,
    source_job_id: Optional[int] = None,
    connection_ids: Optional[list[int]] = None,
    site_ids: Optional[list[int]] = None,
    account_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    mode, start_date, end_date = gsc_date_window(mode, days=days)
    scope_key = gsc_scope(mode, start_date, end_date, days=days)
    now = utcnow()
    expires_at = now + timedelta(hours=GSC_SNAPSHOT_TTL_HOURS)
    targets = active_search_console_report_targets(
        session,
        connection_ids=connection_ids,
        site_ids=site_ids,
        account_ids=account_ids,
    )
    errors: list[dict[str, Any]] = []
    snapshots: list[GoogleSearchConsoleDataSnapshot] = []
    dataset_count = 0
    for target in targets:
        site: GoogleSearchConsoleSite = target["site"]
        account: GoogleAdsAccount | None = target.get("account")
        try:
            http = authorized_search_console_session(site.connection)
            query_payload = _search_analytics_request(
                start_date=start_date,
                end_date=end_date,
                limit=min(max_rows, GSC_ROW_LIMIT),
                start_row=0,
            )
            if not force:
                fresh = _get_fresh_gsc_snapshot(
                    session,
                    site=site,
                    account=account,
                    dataset_key=DATASET_GSC_SEARCH_ANALYTICS,
                    scope_key=scope_key,
                    query_payload=query_payload,
                    now=now,
                )
                if fresh is not None:
                    snapshots.append(fresh)
                    continue
            request_payload, rows, response_meta = _run_search_analytics(
                http,
                site,
                start_date=start_date,
                end_date=end_date,
                max_rows=max_rows,
            )
            payload = {
                "site_url": site.site_url,
                "account_id": account.id if account is not None else None,
                "customer_id": account.customer_id if account is not None else "",
                "date_range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
                "dimensions": ["query", "page", "country", "device"],
                "rows": rows,
                "response_meta": response_meta,
            }
            snapshot = _upsert_gsc_snapshot(
                session,
                site=site,
                account=account,
                dataset_key=DATASET_GSC_SEARCH_ANALYTICS,
                payload=payload,
                scope_key=scope_key,
                query_payload=request_payload,
                source_job_id=source_job_id,
                expires_at=expires_at,
            )
            snapshots.append(snapshot)
            dataset_count += 1
            site.last_pull_at = now
            session.commit()
        except Exception as exc:  # noqa: BLE001 - record all targets and continue.
            session.rollback()
            errors.append(
                {
                    "site_url": site.site_url,
                    "account_id": account.id if account is not None else None,
                    "error": str(exc)[:1000],
                }
            )
    candidates_saved = upsert_search_console_query_candidates(session, snapshots, source_job_id=source_job_id)
    session.commit()
    return {
        "mode": mode,
        "scope_key": scope_key,
        "target_count": len(targets),
        "dataset_count": dataset_count,
        "snapshot_count": len(snapshots),
        "query_candidates_imported": candidates_saved,
        "error_count": len(errors),
        "errors": errors,
    }


def search_console_ads_signal_matrix(session: Session, account_id: int, *, limit: int = 100) -> dict[str, Any]:
    limit = max(min(int(limit or 100), 500), 1)
    rows = session.scalars(
        select(GoogleSearchConsoleQueryCandidate)
        .where(
            GoogleSearchConsoleQueryCandidate.account_id == int(account_id),
            GoogleSearchConsoleQueryCandidate.review_status != "rejected",
        )
        .order_by(
            GoogleSearchConsoleQueryCandidate.score.desc(),
            GoogleSearchConsoleQueryCandidate.clicks.desc(),
            GoogleSearchConsoleQueryCandidate.impressions.desc(),
            GoogleSearchConsoleQueryCandidate.position.asc(),
        )
        .limit(limit)
    ).all()
    core_queries: list[dict[str, Any]] = []
    testing_queries: list[dict[str, Any]] = []
    page_map: dict[str, dict[str, Any]] = {}
    protect_keyword_keys: set[str] = set()
    protect_page_keys: set[str] = set()
    for row in rows:
        payload = {
            "query": row.query,
            "normalized_query": row.normalized_query,
            "page_url": row.page_url,
            "country": row.country,
            "device": row.device,
            "clicks": row.clicks,
            "impressions": row.impressions,
            "ctr": row.ctr,
            "position": row.position,
            "quality_label": row.quality_label,
            "score": row.score,
        }
        if row.clicks > 0:
            core_queries.append(payload)
            protect_keyword_keys.add(row.normalized_query)
            protect_page_keys.add(canonical_landing_page_url(row.page_url).lower())
        elif row.impressions >= 50:
            testing_queries.append(payload)
        if row.page_url:
            page_key = canonical_landing_page_url(row.page_url)
            page_entry = page_map.setdefault(
                page_key,
                {
                    "page_url": row.page_url,
                    "clicks": 0,
                    "impressions": 0,
                    "queries": [],
                    "score": 0.0,
                },
            )
            page_entry["clicks"] += int(row.clicks or 0)
            page_entry["impressions"] += int(row.impressions or 0)
            page_entry["score"] += float(row.score or 0)
            if len(page_entry["queries"]) < 12:
                page_entry["queries"].append(row.query)
    top_pages = sorted(page_map.values(), key=lambda item: (item["score"], item["clicks"], item["impressions"]), reverse=True)
    snapshot_ids = [
        item
        for item in session.scalars(
            select(GoogleSearchConsoleDataSnapshot.id)
            .where(GoogleSearchConsoleDataSnapshot.account_id == int(account_id))
            .order_by(GoogleSearchConsoleDataSnapshot.fetched_at.desc(), GoogleSearchConsoleDataSnapshot.id.desc())
            .limit(20)
        ).all()
    ]
    return {
        "generated_at": utcnow().isoformat(),
        "account_id": int(account_id),
        "status": "ready",
        "snapshot_ids": snapshot_ids,
        "core_queries": core_queries[:limit],
        "testing_queries": testing_queries[:limit],
        "top_pages": top_pages[:limit],
        "protect_keyword_keys": sorted(protect_keyword_keys),
        "protect_page_keys": sorted(protect_page_keys),
        "summary": {
            "candidate_count": len(rows),
            "core_query_count": len(core_queries),
            "testing_query_count": len(testing_queries),
            "top_page_count": len(top_pages),
        },
        "basis": (
            "Search Console organic query/page/country/device rows are used as relevance evidence. "
            "Clicked query-page pairs can feed Core/Scale themes, high-impression unclicked pairs feed Testing, "
            "and clicked pages are protected from Waste until Ads, GA4, and Odoo sales data contradict them."
        ),
    }


def search_console_keyword_terms(matrix: dict[str, Any], *, limit: int = 50) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for bucket in ("core_queries", "testing_queries"):
        for row in matrix.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            term = clean_keyword(str(row.get("query") or ""))
            key = normalized_keyword(term)
            if not term or not key or key in seen or not usable_keyword(term):
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= limit:
                return terms
    return terms
