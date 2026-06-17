from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import (
    AppSetting,
    GoogleAdsAccount,
    GoogleAdsConnection,
    GoogleAnalyticsConnection,
    GoogleAnalyticsDataSnapshot,
    GoogleAnalyticsProperty,
    GoogleAnalyticsWebStream,
    GoogleAnalyticsWebsiteMapping,
    OdooStore,
    OdooWebsite,
    User,
)
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_connection import clear_google_ads_connection_status_cache
from app.services.google_ads_oauth_state import (
    delete_google_ads_oauth_state,
    load_google_ads_oauth_state,
    store_google_ads_oauth_error,
)
from app.services.google_analytics import (
    GA4_RECENT_DAYS,
    GOOGLE_ANALYTICS_SCOPES,
    fetch_google_email,
    ga4_ads_enhancement_plan,
    ga4_status_summary,
)


router = APIRouter()
logger = logging.getLogger(__name__)
GOOGLE_ADS_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/datamanager",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)


def _google_oauth_client_config(client_id: str, client_secret: str) -> dict[str, dict[str, str]]:
    return {
        "web": {
            "client_id": str(client_id or ""),
            "client_secret": str(client_secret or ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


async def _google_oauth_setting_values(session: AsyncSession) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(AppSetting).where(AppSetting.key.in_(("google_ads.client_id", "google_ads.client_secret")))
        )
    ).all()
    return {row.key: row.value for row in rows}


def _checked(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _int_list(values: list[Any]) -> list[int]:
    parsed: list[int] = []
    for value in values:
        try:
            parsed_value = int(value)
        except (TypeError, ValueError):
            continue
        if parsed_value > 0 and parsed_value not in parsed:
            parsed.append(parsed_value)
    return parsed


def _analytics_url(**params: Any) -> str:
    cleaned = {key: value for key, value in params.items() if value not in (None, "", False)}
    return "/analytics" + (("?" + urlencode(cleaned)) if cleaned else "")


def _parse_website_key(raw: str) -> tuple[Optional[int], int]:
    text = str(raw or "").strip()
    if not text or ":" not in text:
        return None, 0
    left, right = text.split(":", 1)
    try:
        store_id = int(left)
        website_id = int(right)
    except ValueError:
        return None, 0
    return (store_id if store_id > 0 else None), max(website_id, 0)


async def _analytics_mapping_row(
    session: AsyncSession,
    *,
    analytics_property_id: int,
    analytics_stream_id: Optional[int],
    store_id: Optional[int],
    website_id: int,
    account_id: Optional[int],
) -> Optional[GoogleAnalyticsWebsiteMapping]:
    query = select(GoogleAnalyticsWebsiteMapping).where(
        GoogleAnalyticsWebsiteMapping.analytics_property_id == analytics_property_id,
        GoogleAnalyticsWebsiteMapping.website_id == int(website_id or 0),
    )
    query = query.where(
        GoogleAnalyticsWebsiteMapping.analytics_stream_id == analytics_stream_id
        if analytics_stream_id
        else GoogleAnalyticsWebsiteMapping.analytics_stream_id.is_(None)
    )
    query = query.where(
        GoogleAnalyticsWebsiteMapping.store_id == store_id
        if store_id
        else GoogleAnalyticsWebsiteMapping.store_id.is_(None)
    )
    query = query.where(
        GoogleAnalyticsWebsiteMapping.account_id == account_id
        if account_id
        else GoogleAnalyticsWebsiteMapping.account_id.is_(None)
    )
    return await session.scalar(query.limit(1))


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    plan_account_id: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    connections = (
        await session.scalars(
            select(GoogleAnalyticsConnection).order_by(
                desc(GoogleAnalyticsConnection.last_oauth_at),
                GoogleAnalyticsConnection.name,
            )
        )
    ).all()
    properties = (
        await session.scalars(
            select(GoogleAnalyticsProperty).order_by(
                GoogleAnalyticsProperty.account_display_name,
                GoogleAnalyticsProperty.display_name,
            )
        )
    ).all()
    streams = (
        await session.scalars(
            select(GoogleAnalyticsWebStream)
            .join(GoogleAnalyticsProperty)
            .order_by(GoogleAnalyticsProperty.display_name, GoogleAnalyticsWebStream.display_name)
        )
    ).all()
    mappings = (
        await session.scalars(
            select(GoogleAnalyticsWebsiteMapping)
            .where(GoogleAnalyticsWebsiteMapping.is_active.is_(True))
            .order_by(desc(GoogleAnalyticsWebsiteMapping.updated_at), desc(GoogleAnalyticsWebsiteMapping.id))
        )
    ).all()
    stores = (
        await session.scalars(select(OdooStore).where(OdooStore.is_active.is_(True)).order_by(OdooStore.name))
    ).all()
    websites = (
        await session.scalars(
            select(OdooWebsite).where(OdooWebsite.is_active.is_(True)).order_by(OdooWebsite.store_id, OdooWebsite.name)
        )
    ).all()
    accounts = (
        await session.scalars(
            select(GoogleAdsAccount).where(GoogleAdsAccount.is_active.is_(True)).order_by(GoogleAdsAccount.name)
        )
    ).all()
    selected_plan_account_id = int(plan_account_id or (accounts[0].id if accounts else 0) or 0) or None
    status_summary = await session.run_sync(lambda sync_session: ga4_status_summary(sync_session))
    enhancement_plan = await session.run_sync(
        lambda sync_session: ga4_ads_enhancement_plan(sync_session, selected_plan_account_id, limit=15)
    )
    latest_snapshot_rows = (
        await session.execute(
            select(
                GoogleAnalyticsDataSnapshot.id.label("id"),
                GoogleAnalyticsDataSnapshot.dataset_key.label("dataset_key"),
                GoogleAnalyticsDataSnapshot.scope_key.label("scope_key"),
                GoogleAnalyticsDataSnapshot.row_count.label("row_count"),
                GoogleAnalyticsDataSnapshot.fetched_at.label("fetched_at"),
                GoogleAnalyticsDataSnapshot.target_key.label("target_key"),
                GoogleAnalyticsProperty.display_name.label("property_name"),
                GoogleAnalyticsWebStream.display_name.label("stream_name"),
                GoogleAnalyticsWebStream.default_uri.label("default_uri"),
                GoogleAdsAccount.name.label("account_name"),
                GoogleAdsAccount.customer_id.label("customer_id"),
            )
            .select_from(GoogleAnalyticsDataSnapshot)
            .outerjoin(GoogleAnalyticsProperty, GoogleAnalyticsProperty.id == GoogleAnalyticsDataSnapshot.analytics_property_id)
            .outerjoin(GoogleAnalyticsWebStream, GoogleAnalyticsWebStream.id == GoogleAnalyticsDataSnapshot.analytics_stream_id)
            .outerjoin(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAnalyticsDataSnapshot.account_id)
            .order_by(desc(GoogleAnalyticsDataSnapshot.fetched_at), desc(GoogleAnalyticsDataSnapshot.id))
            .limit(24)
        )
    ).all()
    latest_snapshots = [dict(row._mapping) for row in latest_snapshot_rows]
    dataset_summary_rows = (
        await session.execute(
            select(
                GoogleAnalyticsDataSnapshot.dataset_key,
                func.count(GoogleAnalyticsDataSnapshot.id),
                func.sum(GoogleAnalyticsDataSnapshot.row_count),
                func.max(GoogleAnalyticsDataSnapshot.fetched_at),
            )
            .group_by(GoogleAnalyticsDataSnapshot.dataset_key)
            .order_by(desc(func.max(GoogleAnalyticsDataSnapshot.fetched_at)))
        )
    ).all()
    dataset_summaries = [
        {
            "dataset_key": row[0],
            "snapshot_count": int(row[1] or 0),
            "row_count": int(row[2] or 0),
            "latest_pull_at": row[3],
        }
        for row in dataset_summary_rows
    ]
    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "connections": connections,
            "properties": properties,
            "streams": streams,
            "mappings": mappings,
            "stores": stores,
            "websites": websites,
            "accounts": accounts,
            "status_summary": status_summary,
            "latest_snapshots": latest_snapshots,
            "dataset_summaries": dataset_summaries,
            "enhancement_plan": enhancement_plan,
            "selected_plan_account_id": selected_plan_account_id,
            "ga_oauth": request.query_params.get("ga_oauth"),
            "ga_oauth_error": request.query_params.get("ga_oauth_error"),
            "discover_job_id": request.query_params.get("discover_job_id"),
            "sync_job_id": request.query_params.get("sync_job_id"),
            "mapping_saved": request.query_params.get("mapping_saved") == "1",
            "mapping_deleted": request.query_params.get("mapping_deleted") == "1",
            "default_recent_days": GA4_RECENT_DAYS,
        },
    )


@router.post("/analytics/connect")
async def start_google_analytics_oauth(
    request: Request,
    connection_id: int = Form(0),
    connection_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    values = await _google_oauth_setting_values(session)
    connection = await session.get(GoogleAnalyticsConnection, connection_id) if connection_id else None
    client_id = (connection.client_id if connection else "") or values.get("google_ads.client_id")
    client_secret = (connection.client_secret if connection else "") or values.get("google_ads.client_secret")
    if not client_id or not client_secret:
        return RedirectResponse(
            _analytics_url(ga_oauth_error="missing_client"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return RedirectResponse(
            _analytics_url(ga_oauth_error="missing_package"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    redirect_uri = str(request.url_for("google_analytics_oauth_callback"))
    if redirect_uri.startswith("http://") and settings.app_env != "production":
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow = Flow.from_client_config(
        _google_oauth_client_config(str(client_id), str(client_secret)),
        scopes=list(GOOGLE_ANALYTICS_SCOPES),
        redirect_uri=redirect_uri,
    )
    authorization_url, state_value = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["google_analytics_oauth_state"] = state_value
    request.session["google_analytics_oauth_code_verifier"] = getattr(flow, "code_verifier", "") or ""
    request.session["google_analytics_oauth_connection_id"] = int(connection.id) if connection else 0
    request.session["google_analytics_oauth_connection_name"] = connection_name.strip()[:160]
    return RedirectResponse(authorization_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/analytics/oauth/callback", name="google_analytics_oauth_callback")
async def google_analytics_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    google_ads_expected_state = request.session.get("google_ads_oauth_state")
    stored_google_ads_state = await load_google_ads_oauth_state(session, state) if state else None
    if code and state and (google_ads_expected_state == state or stored_google_ads_state is not None):
        request.session.pop("google_ads_oauth_state", None)
        code_verifier = request.session.pop("google_ads_oauth_code_verifier", None)
        connection_id = int(request.session.pop("google_ads_oauth_connection_id", 0) or 0)
        connection_name = str(request.session.pop("google_ads_oauth_connection_name", "") or "").strip()
        if stored_google_ads_state and google_ads_expected_state != state:
            code_verifier = str(stored_google_ads_state.get("code_verifier") or "")
            connection_id = int(stored_google_ads_state.get("connection_id") or 0)
            connection_name = str(stored_google_ads_state.get("connection_name") or "").strip()
        values = await session.scalars(
            select(AppSetting).where(
                AppSetting.key.in_(
                    (
                        "google_ads.developer_token",
                        "google_ads.client_id",
                        "google_ads.client_secret",
                        "google_ads.api_version",
                    )
                )
            )
        )
        setting_values = {row.key: row.value for row in values}
        connection = await session.get(GoogleAdsConnection, connection_id) if connection_id else None
        client_id = (connection.client_id if connection else "") or setting_values.get("google_ads.client_id")
        client_secret = (connection.client_secret if connection else "") or setting_values.get("google_ads.client_secret")
        if not client_id or not client_secret:
            return RedirectResponse("/settings?google_oauth_error=missing_client", status_code=status.HTTP_303_SEE_OTHER)
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError:
            return RedirectResponse("/settings?google_oauth_error=missing_package", status_code=status.HTTP_303_SEE_OTHER)
        redirect_uri = str((stored_google_ads_state or {}).get("redirect_uri") or request.url_for("google_analytics_oauth_callback"))
        if redirect_uri.startswith("http://") and settings.app_env != "production":
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow = Flow.from_client_config(
            _google_oauth_client_config(str(client_id), str(client_secret)),
            scopes=list(GOOGLE_ADS_OAUTH_SCOPES),
            redirect_uri=redirect_uri,
        )
        if code_verifier:
            flow.code_verifier = str(code_verifier)
        try:
            flow.fetch_token(code=code)
        except Exception as exc:
            logger.exception("Google Ads OAuth token exchange failed through analytics callback")
            detail = f"{exc.__class__.__name__}: {exc}"
            await store_google_ads_oauth_error(session, state=state, connection_id=connection_id, message=detail)
            return RedirectResponse(
                f"/settings?google_oauth_error=token&google_oauth_detail={quote(detail[:300], safe='')}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        credentials = flow.credentials
        refresh_token = str((credentials.refresh_token if credentials else "") or (connection.refresh_token if connection else "") or "")
        if not refresh_token:
            return RedirectResponse("/settings?google_oauth_error=no_refresh_token", status_code=status.HTTP_303_SEE_OTHER)
        email = fetch_google_email(credentials.token or "") if credentials else ""
        if connection is None:
            connection = GoogleAdsConnection(name=connection_name or email or "Google Ads Gmail")
            session.add(connection)
            await session.flush()
        connection.name = connection_name or connection.name or email or "Google Ads Gmail"
        connection.email = email or connection.email or ""
        connection.developer_token = str(setting_values.get("google_ads.developer_token") or connection.developer_token or "")
        connection.client_id = str(client_id or connection.client_id or "")
        connection.client_secret = str(client_secret or connection.client_secret or "")
        connection.refresh_token = refresh_token
        connection.api_version = str(setting_values.get("google_ads.api_version") or connection.api_version or "")
        connection.is_active = True
        connection.last_oauth_at = datetime.now(timezone.utc)
        await delete_google_ads_oauth_state(session, state)
        await session.commit()
        clear_google_ads_connection_status_cache()
        return RedirectResponse("/settings?google_oauth=connected", status_code=status.HTTP_303_SEE_OTHER)

    user_id = request.session.get("user_id")
    user = await session.get(User, int(user_id)) if user_id else None
    if user is None or not user.is_active:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    expected_state = request.session.pop("google_analytics_oauth_state", None)
    code_verifier = request.session.pop("google_analytics_oauth_code_verifier", None)
    connection_id = int(request.session.pop("google_analytics_oauth_connection_id", 0) or 0)
    connection_name = str(request.session.pop("google_analytics_oauth_connection_name", "") or "").strip()
    if not code or not state or expected_state != state:
        return RedirectResponse(_analytics_url(ga_oauth_error="state"), status_code=status.HTTP_303_SEE_OTHER)

    values = await _google_oauth_setting_values(session)
    connection = await session.get(GoogleAnalyticsConnection, connection_id) if connection_id else None
    client_id = (connection.client_id if connection else "") or values.get("google_ads.client_id")
    client_secret = (connection.client_secret if connection else "") or values.get("google_ads.client_secret")
    if not client_id or not client_secret:
        return RedirectResponse(_analytics_url(ga_oauth_error="missing_client"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return RedirectResponse(
            _analytics_url(ga_oauth_error="missing_package"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    redirect_uri = str(request.url_for("google_analytics_oauth_callback"))
    if redirect_uri.startswith("http://") and settings.app_env != "production":
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow = Flow.from_client_config(
        _google_oauth_client_config(str(client_id), str(client_secret)),
        scopes=list(GOOGLE_ANALYTICS_SCOPES),
        redirect_uri=redirect_uri,
    )
    if code_verifier:
        flow.code_verifier = str(code_verifier)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.exception("Google Analytics OAuth token exchange failed")
        error_code = exc.__class__.__name__
        return RedirectResponse(
            _analytics_url(ga_oauth_error="token", ga_oauth_detail=error_code),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    credentials = flow.credentials
    refresh_token = str((credentials.refresh_token if credentials else "") or (connection.refresh_token if connection else "") or "")
    if not refresh_token:
        return RedirectResponse(
            _analytics_url(ga_oauth_error="no_refresh_token"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    email = fetch_google_email(credentials.token or "") if credentials else ""
    if connection is None:
        connection = GoogleAnalyticsConnection(name=connection_name or email or "Google Analytics Gmail")
        session.add(connection)
        await session.flush()

    connection.name = connection_name or connection.name or email or "Google Analytics Gmail"
    connection.email = email or connection.email or ""
    connection.client_id = str(client_id or connection.client_id or "")
    connection.client_secret = str(client_secret or connection.client_secret or "")
    connection.refresh_token = refresh_token
    connection.is_active = True
    connection.last_oauth_at = datetime.now(timezone.utc)

    await session.commit()
    job = await create_background_job(
        session,
        job_type="google_analytics_discovery",
        label=f"Discover GA4 properties for {connection.name}",
        requested_by_id=user.id,
        payload={"connection_id": connection.id, "queued_by": "oauth_callback"},
    )
    try:
        from app.tasks import discover_google_analytics_connection

        message = discover_google_analytics_connection.send(connection.id, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - show dispatch failure on the Analytics page.
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(
        _analytics_url(ga_oauth="connected", discover_job_id=job.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/analytics/connections/{connection_id}/discover")
async def queue_google_analytics_discovery(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    connection = await session.get(GoogleAnalyticsConnection, connection_id)
    if connection is None:
        return RedirectResponse(
            _analytics_url(ga_oauth_error="connection_missing"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    job = await create_background_job(
        session,
        job_type="google_analytics_discovery",
        label=f"Discover GA4 properties for {connection.name}",
        requested_by_id=user.id,
        payload={"connection_id": connection_id},
    )
    try:
        from app.tasks import discover_google_analytics_connection

        message = discover_google_analytics_connection.send(connection_id, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(_analytics_url(discover_job_id=job.id), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/analytics/ecommerce/sync")
async def queue_google_analytics_ecommerce_sync(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    form = await request.form()
    mode = "all_time" if str(form.get("pull_mode") or "").strip().lower() == "all_time" else "recent"
    days = min(max(int(form.get("days") or GA4_RECENT_DAYS), 1), 365)
    max_rows = min(max(int(form.get("max_rows") or 10_000), 50), 250_000 if mode == "all_time" else 50_000)
    sync_scope = str(form.get("sync_scope") or "selected").strip().lower()
    selected_connection_ids = _int_list(form.getlist("connection_ids"))
    selected_property_ids = _int_list(form.getlist("property_ids"))
    connection_ids = None if sync_scope == "all" else selected_connection_ids or None
    property_ids = selected_property_ids or None
    if sync_scope != "all" and not connection_ids and not property_ids:
        return RedirectResponse(_analytics_url(ga_oauth_error="select_connection"), status_code=status.HTTP_303_SEE_OTHER)
    label_scope = "all GA4 properties" if sync_scope == "all" else "selected GA4 properties"
    job = await create_background_job(
        session,
        job_type="google_analytics_ecommerce_sync",
        label=f"GA4 ecommerce pull: {mode.replace('_', ' ')} for {label_scope}",
        requested_by_id=user.id,
        payload={
            "mode": mode,
            "days": days,
            "max_rows": max_rows,
            "force": _checked(form.get("force")),
            "connection_ids": connection_ids or [],
            "property_ids": property_ids or [],
        },
    )
    try:
        from app.tasks import sync_google_analytics_ecommerce

        message = sync_google_analytics_ecommerce.send(
            mode,
            days,
            job.id,
            connection_ids,
            property_ids,
            max_rows,
            _checked(form.get("force")),
        )
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(_analytics_url(sync_job_id=job.id), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/analytics/mappings")
async def save_google_analytics_mapping(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    form = await request.form()
    analytics_property_id = int(form.get("analytics_property_id") or 0)
    analytics_stream_id = int(form.get("analytics_stream_id") or 0) or None
    account_id = int(form.get("account_id") or 0) or None
    store_id, website_id = _parse_website_key(str(form.get("website_key") or ""))
    property_row = await session.get(GoogleAnalyticsProperty, analytics_property_id)
    if property_row is None:
        raise HTTPException(status_code=400, detail="Select a Google Analytics property.")
    stream = await session.get(GoogleAnalyticsWebStream, analytics_stream_id) if analytics_stream_id else None
    if stream is not None and stream.analytics_property_id != property_row.id:
        raise HTTPException(status_code=400, detail="The selected stream does not belong to that property.")
    if account_id and await session.get(GoogleAdsAccount, account_id) is None:
        raise HTTPException(status_code=400, detail="Select a valid Google Ads account.")
    if store_id and await session.get(OdooStore, store_id) is None:
        raise HTTPException(status_code=400, detail="Select a valid Odoo store.")

    row = await _analytics_mapping_row(
        session,
        analytics_property_id=property_row.id,
        analytics_stream_id=stream.id if stream is not None else None,
        store_id=store_id,
        website_id=website_id,
        account_id=account_id,
    )
    if row is None:
        row = GoogleAnalyticsWebsiteMapping(
            analytics_property_id=property_row.id,
            analytics_stream_id=stream.id if stream is not None else None,
            store_id=store_id,
            website_id=website_id,
            account_id=account_id,
            match_source="manual",
            match_confidence=1.0,
        )
        session.add(row)
    row.match_source = "manual"
    row.match_confidence = 1.0
    row.is_active = True
    await session.commit()
    return RedirectResponse(_analytics_url(mapping_saved=1), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/analytics/mappings/{mapping_id}/delete")
async def delete_google_analytics_mapping(
    mapping_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    row = await session.get(GoogleAnalyticsWebsiteMapping, mapping_id)
    if row is not None:
        row.is_active = False
        await session.commit()
    return RedirectResponse(_analytics_url(mapping_deleted=1), status_code=status.HTTP_303_SEE_OTHER)
