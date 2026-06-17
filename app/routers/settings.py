from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_settings import DEFINITIONS_BY_KEY, coerce_form_value, get_setting_rows, grouped_settings
from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import AppSetting, GoogleAdsConnection, User
from app.security import hash_password, verify_password
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_connection import clear_google_ads_connection_status_cache, get_google_ads_connection_status
from app.services.google_ads_oauth_state import (
    delete_google_ads_oauth_state,
    load_google_ads_oauth_state,
    store_google_ads_oauth_error,
    store_google_ads_oauth_state,
)
from app.services.page_feed_restrictions import SETTING_KEY as RESTRICTED_WORDS_SETTING_KEY, restricted_term_count
from app.tasks import discover_google_ads_connection_accounts


router = APIRouter()
GOOGLE_ADS_OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/datamanager",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)
def google_ads_redirect_uri(request: Request) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url.rstrip('/')}/settings/google-ads/oauth/callback"
    if settings.app_env != "production":
        host = request.url.hostname or "localhost"
        if host in {"localhost", "127.0.0.1"}:
            return f"http://{host}:8010/settings/google-ads/oauth/callback"
        return "http://localhost:8010/settings/google-ads/oauth/callback"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    if proto == "http" and host and not host.startswith(("localhost", "127.0.0.1")):
        proto = "https"
    if host:
        return f"{proto}://{host}/settings/google-ads/oauth/callback"
    return str(request.url_for("google_ads_oauth_callback")).replace("http://", "https://", 1)


def _google_ads_client_config(values: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        "web": {
            "client_id": str(values.get("google_ads.client_id") or ""),
            "client_secret": str(values.get("google_ads.client_secret") or ""),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


async def _google_ads_setting_values(session: AsyncSession) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(AppSetting).where(
                AppSetting.key.in_(
                    (
                        "google_ads.developer_token",
                        "google_ads.client_id",
                        "google_ads.client_secret",
                        "google_ads.refresh_token",
                        "google_ads.api_version",
                    )
                )
            )
        )
    ).all()
    return {row.key: row.value for row in rows}


def _fetch_google_email(access_token: str) -> str:
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


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    rows = await get_setting_rows(session)
    google_ads_status = await get_google_ads_connection_status(
        session,
        redirect_uri=google_ads_redirect_uri(request),
        force_refresh=bool(request.query_params.get("google_oauth") or request.query_params.get("google_oauth_error")),
    )
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "groups": grouped_settings(rows),
            "saved": request.query_params.get("saved") == "1",
            "google_ads_status": google_ads_status,
            "google_oauth": request.query_params.get("google_oauth"),
            "google_oauth_error": request.query_params.get("google_oauth_error"),
            "google_oauth_detail": request.query_params.get("google_oauth_detail"),
        },
    )


@router.post("/settings")
async def update_settings(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    form = await request.form()
    rows = await get_setting_rows(session)
    rows_by_key = {row.key: row for row in rows}
    for key, definition in DEFINITIONS_BY_KEY.items():
        row = rows_by_key.get(key)
        if row is None:
            row = AppSetting(
                key=definition.key,
                value=definition.default,
                category=definition.category,
                label=definition.label,
                help_text=definition.help_text,
                input_type=definition.input_type,
                sensitive=definition.sensitive,
            )
            session.add(row)
        raw = form.get(key)
        if raw is None and definition.input_type != "checkbox":
            continue
        if definition.sensitive and definition.input_type == "password" and (raw is None or str(raw).strip() == ""):
            continue
        row.value = coerce_form_value(definition, str(raw) if raw is not None else None)
        row.category = definition.category
        row.label = definition.label
        row.help_text = definition.help_text
        row.input_type = definition.input_type
        row.sensitive = definition.sensitive
    await session.commit()
    return RedirectResponse("/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


async def _start_google_ads_oauth_redirect(
    request: Request,
    session: AsyncSession,
    *,
    connection_id: int = 0,
    connection_name: str = "",
) -> RedirectResponse:
    values = await _google_ads_setting_values(session)
    connection = await session.get(GoogleAdsConnection, connection_id) if connection_id else None
    client_id = (connection.client_id if connection else "") or values.get("google_ads.client_id")
    client_secret = (connection.client_secret if connection else "") or values.get("google_ads.client_secret")
    if not client_id or not client_secret:
        return RedirectResponse(
            "/settings?google_oauth_error=missing_client",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return RedirectResponse(
            "/settings?google_oauth_error=missing_package",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    redirect_uri = google_ads_redirect_uri(request)
    if redirect_uri.startswith("http://") and settings.app_env != "production":
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    oauth_values = dict(values)
    oauth_values["google_ads.client_id"] = client_id
    oauth_values["google_ads.client_secret"] = client_secret
    flow = Flow.from_client_config(
        _google_ads_client_config(oauth_values),
        scopes=list(GOOGLE_ADS_OAUTH_SCOPES),
        redirect_uri=redirect_uri,
    )
    authorization_url, state_value = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["google_ads_oauth_state"] = state_value
    request.session["google_ads_oauth_code_verifier"] = getattr(flow, "code_verifier", "") or ""
    request.session["google_ads_oauth_connection_id"] = int(connection.id) if connection else 0
    request.session["google_ads_oauth_connection_name"] = connection_name.strip()[:160]
    await store_google_ads_oauth_state(
        session,
        state=state_value,
        code_verifier=getattr(flow, "code_verifier", "") or "",
        connection_id=int(connection.id) if connection else 0,
        connection_name=connection_name.strip()[:160],
        redirect_uri=redirect_uri,
    )
    return RedirectResponse(authorization_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/google-ads/reconnect")
async def start_google_ads_oauth(
    request: Request,
    connection_id: int = Form(0),
    connection_name: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    return await _start_google_ads_oauth_redirect(
        request,
        session,
        connection_id=int(connection_id or 0),
        connection_name=connection_name,
    )


@router.get("/settings/google-ads/reconnect")
async def start_google_ads_oauth_from_link(
    request: Request,
    connection_id: int = 0,
    connection_name: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if settings.app_env == "production" and not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return await _start_google_ads_oauth_redirect(
        request,
        session,
        connection_id=int(connection_id or 0),
        connection_name=connection_name,
    )


@router.get("/settings/google-ads/oauth/callback", name="google_ads_oauth_callback")
async def google_ads_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    expected_state = request.session.pop("google_ads_oauth_state", None)
    code_verifier = request.session.pop("google_ads_oauth_code_verifier", None)
    connection_id = int(request.session.pop("google_ads_oauth_connection_id", 0) or 0)
    connection_name = str(request.session.pop("google_ads_oauth_connection_name", "") or "").strip()
    stored_oauth_state = await load_google_ads_oauth_state(session, state) if state else None
    if stored_oauth_state and expected_state != state:
        code_verifier = str(stored_oauth_state.get("code_verifier") or "")
        connection_id = int(stored_oauth_state.get("connection_id") or 0)
        connection_name = str(stored_oauth_state.get("connection_name") or "").strip()
    if not code or not state or (expected_state != state and stored_oauth_state is None):
        return RedirectResponse(
            "/settings?google_oauth_error=state",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    values = await _google_ads_setting_values(session)
    connection = await session.get(GoogleAdsConnection, connection_id) if connection_id else None
    client_id = (connection.client_id if connection else "") or values.get("google_ads.client_id")
    client_secret = (connection.client_secret if connection else "") or values.get("google_ads.client_secret")
    oauth_values = dict(values)
    oauth_values["google_ads.client_id"] = client_id
    oauth_values["google_ads.client_secret"] = client_secret
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        return RedirectResponse(
            "/settings?google_oauth_error=missing_package",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    redirect_uri = str((stored_oauth_state or {}).get("redirect_uri") or google_ads_redirect_uri(request))
    if redirect_uri.startswith("http://") and settings.app_env != "production":
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = Flow.from_client_config(
        _google_ads_client_config(oauth_values),
        scopes=list(GOOGLE_ADS_OAUTH_SCOPES),
        redirect_uri=redirect_uri,
    )
    if code_verifier:
        flow.code_verifier = str(code_verifier)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        detail = f"{exc.__class__.__name__}: {exc}"
        await store_google_ads_oauth_error(session, state=state, connection_id=connection_id, message=detail)
        return RedirectResponse(
            f"/settings?google_oauth_error=token&google_oauth_detail={quote(detail[:300], safe='')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    credentials = flow.credentials
    if not credentials or not credentials.refresh_token:
        return RedirectResponse(
            "/settings?google_oauth_error=no_refresh_token",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    email = _fetch_google_email(credentials.token or "")
    if connection is None:
        connection = GoogleAdsConnection(name=connection_name or email or "Google Ads Gmail")
        session.add(connection)
        await session.flush()

    connection.name = connection_name or connection.name or email or "Google Ads Gmail"
    connection.email = email or connection.email or ""
    connection.developer_token = str(connection.developer_token or values.get("google_ads.developer_token") or "")
    connection.client_id = str(client_id or connection.client_id or "")
    connection.client_secret = str(client_secret or connection.client_secret or "")
    connection.refresh_token = credentials.refresh_token
    connection.api_version = str(values.get("google_ads.api_version") or connection.api_version or "")
    connection.is_active = True
    connection.last_oauth_at = datetime.now(timezone.utc)

    await delete_google_ads_oauth_state(session, state)
    await session.commit()
    clear_google_ads_connection_status_cache()
    return RedirectResponse("/settings?google_oauth=connected", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/google-ads/connections/{connection_id}/discover")
async def discover_google_ads_accounts(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    connection = await session.get(GoogleAdsConnection, connection_id)
    if connection is None:
        return RedirectResponse("/settings?google_oauth_error=connection_missing", status_code=status.HTTP_303_SEE_OTHER)
    clear_google_ads_connection_status_cache()
    job = await create_background_job(
        session,
        job_type="google_ads_discovery",
        label=f"Discover accounts for {connection.name}",
        requested_by_id=user.id,
        payload={"connection_id": connection_id},
    )
    try:
        message = discover_google_ads_connection_accounts.send(connection_id, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(f"/settings?google_oauth=discovery_queued&job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


async def _restricted_words_row(session: AsyncSession) -> AppSetting:
    definition = DEFINITIONS_BY_KEY[RESTRICTED_WORDS_SETTING_KEY]
    row = await session.scalar(select(AppSetting).where(AppSetting.key == definition.key))
    if row is None:
        row = AppSetting(
            key=definition.key,
            value=definition.default,
            category=definition.category,
            label=definition.label,
            help_text=definition.help_text,
            input_type=definition.input_type,
            sensitive=definition.sensitive,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@router.get("/restricted-words", response_class=HTMLResponse)
async def restricted_words_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    row = await _restricted_words_row(session)
    return templates.TemplateResponse(
        "restricted_words.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "terms": row.value or "",
            "term_count": restricted_term_count(row.value or ""),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/restricted-words")
async def update_restricted_words(
    terms: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    row = await _restricted_words_row(session)
    definition = DEFINITIONS_BY_KEY[RESTRICTED_WORDS_SETTING_KEY]
    row.value = "\n".join(line.strip() for line in str(terms or "").splitlines() if line.strip())
    row.category = definition.category
    row.label = definition.label
    row.help_text = definition.help_text
    row.input_type = definition.input_type
    row.sensitive = definition.sensitive
    await session.commit()
    return RedirectResponse("/restricted-words?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/user-settings", response_class=HTMLResponse)
async def user_settings_page(
    request: Request,
    user: User = Depends(require_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "user_settings.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "saved": request.query_params.get("saved") == "1",
            "error": None,
        },
    )


@router.post("/user-settings")
async def update_user_settings(
    request: Request,
    username: str = Form(...),
    current_password: str = Form(""),
    new_password: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
):
    username = username.strip().lower()
    if not username:
        return templates.TemplateResponse(
            "user_settings.html",
            {
                "request": request,
                "user": user,
                "app_name": settings.app_name,
                "saved": False,
                "error": "Username is required.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    existing = await session.scalar(select(User).where(User.email == username, User.id != user.id))
    if existing is not None:
        return templates.TemplateResponse(
            "user_settings.html",
            {
                "request": request,
                "user": user,
                "app_name": settings.app_name,
                "saved": False,
                "error": "That username is already in use.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if new_password.strip():
        if not verify_password(current_password.strip(), user.password_hash):
            return templates.TemplateResponse(
                "user_settings.html",
                {
                    "request": request,
                    "user": user,
                    "app_name": settings.app_name,
                    "saved": False,
                    "error": "Current password is incorrect.",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        user.password_hash = hash_password(new_password.strip())

    user.email = username
    await session.commit()
    return RedirectResponse("/user-settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)
