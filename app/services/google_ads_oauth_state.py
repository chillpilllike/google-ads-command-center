from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting


STATE_KEY_PREFIX = "google_ads.oauth_state."
STATE_TTL_MINUTES = 30


def _state_key(state: str) -> str:
    return f"{STATE_KEY_PREFIX}{sha256(state.encode('utf-8')).hexdigest()}"


async def store_google_ads_oauth_state(
    session: AsyncSession,
    *,
    state: str,
    code_verifier: str,
    connection_id: int,
    connection_name: str,
    redirect_uri: str,
    flow_type: str = "google_ads",
) -> None:
    now = datetime.now(timezone.utc)
    key = _state_key(state)
    row = await session.scalar(select(AppSetting).where(AppSetting.key == key))
    payload = {
        "state": state,
        "code_verifier": code_verifier,
        "connection_id": int(connection_id or 0),
        "connection_name": str(connection_name or "")[:160],
        "redirect_uri": redirect_uri,
        "flow_type": str(flow_type or "google_ads")[:80],
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=STATE_TTL_MINUTES)).isoformat(),
    }
    if row is None:
        row = AppSetting(
            key=key,
            value=payload,
            category="google_ads_oauth",
            label="Google Ads OAuth state",
            help_text="Temporary reconnect state stored so browser redirects can resume safely.",
            input_type="json",
            sensitive=True,
        )
        session.add(row)
    else:
        row.value = payload
        row.category = "google_ads_oauth"
        row.label = "Google Ads OAuth state"
        row.help_text = "Temporary reconnect state stored so browser redirects can resume safely."
        row.input_type = "json"
        row.sensitive = True
    await session.commit()


async def load_google_ads_oauth_state(session: AsyncSession, state: str) -> dict[str, Any] | None:
    row = await session.scalar(select(AppSetting).where(AppSetting.key == _state_key(state)))
    if row is None or not isinstance(row.value, dict):
        return None
    expires_at_raw = str(row.value.get("expires_at") or "")
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError:
        expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        await session.delete(row)
        await session.commit()
        return None
    return dict(row.value)


async def delete_google_ads_oauth_state(session: AsyncSession, state: str) -> None:
    row = await session.scalar(select(AppSetting).where(AppSetting.key == _state_key(state)))
    if row is not None:
        await session.delete(row)


async def store_google_ads_oauth_error(
    session: AsyncSession,
    *,
    state: str,
    connection_id: int,
    message: str,
) -> None:
    now = datetime.now(timezone.utc)
    key = "google_ads.oauth_last_error"
    row = await session.scalar(select(AppSetting).where(AppSetting.key == key))
    payload = {
        "state": state,
        "connection_id": int(connection_id or 0),
        "message": str(message or "")[:1000],
        "created_at": now.isoformat(),
    }
    if row is None:
        row = AppSetting(
            key=key,
            value=payload,
            category="google_ads_oauth",
            label="Google Ads OAuth last error",
            help_text="Last reconnect error captured during token exchange.",
            input_type="json",
            sensitive=True,
        )
        session.add(row)
    else:
        row.value = payload
        row.category = "google_ads_oauth"
        row.label = "Google Ads OAuth last error"
        row.help_text = "Last reconnect error captured during token exchange."
        row.input_type = "json"
        row.sensitive = True
    await session.commit()
