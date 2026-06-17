from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, GoogleAdsAccount, GoogleAdsCampaignMetric, GoogleAdsConnection


SETTING_KEYS = (
    "google_ads.developer_token",
    "google_ads.client_id",
    "google_ads.client_secret",
    "google_ads.refresh_token",
    "google_ads.api_version",
    "google_ads.connected_email",
    "google_ads.oauth_connected_at",
)
_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
STATUS_CACHE_SECONDS = 15.0


def clear_google_ads_connection_status_cache() -> None:
    _status_cache.clear()


def _has_value(value: Any) -> bool:
    return value not in {None, ""}


def _date_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def clean_customer_id(value: str) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


async def upsert_connection_setting(
    session: AsyncSession,
    *,
    key: str,
    value: Any,
    category: str = "Google Ads connection",
    label: str,
    help_text: str,
    sensitive: bool = False,
) -> None:
    stmt = insert(AppSetting).values(
        key=key,
        value=value,
        category=category,
        label=label,
        help_text=help_text,
        input_type="password" if sensitive else "text",
        sensitive=sensitive,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": stmt.excluded.value,
            "category": stmt.excluded.category,
            "label": stmt.excluded.label,
            "help_text": stmt.excluded.help_text,
            "input_type": stmt.excluded.input_type,
            "sensitive": stmt.excluded.sensitive,
        },
    )
    await session.execute(stmt)


async def setting_values(session: AsyncSession, keys: tuple[str, ...] = SETTING_KEYS) -> dict[str, Any]:
    rows = (await session.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all()
    return {row.key: row.value for row in rows}


async def ensure_default_google_ads_connection(session: AsyncSession) -> GoogleAdsConnection | None:
    values = await setting_values(session)
    has_credentials = any(
        values.get(key)
        for key in (
            "google_ads.developer_token",
            "google_ads.client_id",
            "google_ads.client_secret",
            "google_ads.refresh_token",
        )
    )
    if not has_credentials:
        return None

    connection = await session.scalar(
        select(GoogleAdsConnection).where(GoogleAdsConnection.name == "Gofinch automation").limit(1)
    )
    if connection is None:
        connection = GoogleAdsConnection(name="Gofinch automation")
        session.add(connection)
        await session.flush()

    connection.email = str(values.get("google_ads.connected_email") or connection.email or "")
    connection.developer_token = str(values.get("google_ads.developer_token") or connection.developer_token or "")
    connection.client_id = str(values.get("google_ads.client_id") or connection.client_id or "")
    connection.client_secret = str(values.get("google_ads.client_secret") or connection.client_secret or "")
    connection.refresh_token = str(values.get("google_ads.refresh_token") or connection.refresh_token or "")
    connection.api_version = str(values.get("google_ads.api_version") or connection.api_version or "")
    connection.is_active = True

    rows = (
        await session.scalars(
            select(GoogleAdsAccount).where(
                GoogleAdsAccount.connection_id.is_(None),
                GoogleAdsAccount.is_active.is_(True),
            )
        )
    ).all()
    for account in rows:
        account.connection_id = connection.id
        account.source_label = account.source_label or "Imported from automation account config"
    return connection


async def get_google_ads_connection_status(
    session: AsyncSession,
    *,
    redirect_uri: str | None = None,
    force_refresh: bool = False,
    include_accounts: bool = True,
) -> dict[str, Any]:
    cache_key = f"{'full' if include_accounts else 'summary'}:{redirect_uri or 'default'}"
    cached = _status_cache.get(cache_key)
    if not force_refresh and cached and cached[0] > time.monotonic():
        return dict(cached[1])

    values = await setting_values(session)
    connections = (
        await session.scalars(select(GoogleAdsConnection).order_by(GoogleAdsConnection.name, GoogleAdsConnection.id))
    ).all()
    if not connections:
        await ensure_default_google_ads_connection(session)
        await session.flush()
        connections = (
            await session.scalars(select(GoogleAdsConnection).order_by(GoogleAdsConnection.name, GoogleAdsConnection.id))
        ).all()
    flags = {
        "developer_token": _has_value(values.get("google_ads.developer_token")),
        "client_id": _has_value(values.get("google_ads.client_id")),
        "client_secret": _has_value(values.get("google_ads.client_secret")),
        "refresh_token": _has_value(values.get("google_ads.refresh_token")),
    }
    missing = [
        label
        for key, label in (
            ("developer_token", "developer token"),
            ("client_id", "OAuth client ID"),
            ("client_secret", "OAuth client secret"),
            ("refresh_token", "refresh token"),
        )
        if not flags[key]
    ]
    accounts = []
    if include_accounts:
        accounts = (
            await session.scalars(
                select(GoogleAdsAccount)
                .where(GoogleAdsAccount.is_active.is_(True))
                .order_by(GoogleAdsAccount.manager_name, GoogleAdsAccount.name)
            )
        ).all()
    manager_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for account in accounts:
        key = (account.manager_customer_id or "", account.manager_name or "Unknown manager")
        manager = manager_rows.setdefault(
            key,
            {
                "manager_customer_id": account.manager_customer_id or "",
                "manager_name": account.manager_name or "Unknown manager",
                "account_count": 0,
                "currencies": set(),
            },
        )
        manager["account_count"] += 1
        if account.currency_code:
            manager["currencies"].add(account.currency_code)

    managers = []
    for manager in manager_rows.values():
        currencies = sorted(manager["currencies"])
        managers.append(
            {
                "manager_customer_id": manager["manager_customer_id"],
                "manager_name": manager["manager_name"],
                "account_count": manager["account_count"],
                "currencies": currencies,
                "currency_summary": ", ".join(currencies) if currencies else "UNKNOWN",
            }
        )
    managers.sort(key=lambda row: (row["manager_name"], row["manager_customer_id"]))

    currency_counts = defaultdict(int)
    if include_accounts:
        for account in accounts:
            currency_counts[account.currency_code or "UNKNOWN"] += 1
    else:
        for currency_code, account_count in (
            await session.execute(
                select(
                    GoogleAdsAccount.currency_code,
                    func.count(GoogleAdsAccount.id),
                )
                .where(GoogleAdsAccount.is_active.is_(True))
                .group_by(GoogleAdsAccount.currency_code)
            )
        ).all():
            currency_counts[currency_code or "UNKNOWN"] += int(account_count or 0)
    currency_summary = ", ".join(
        f"{currency} {count}" for currency, count in sorted(currency_counts.items())
    ) or "No synced accounts"
    account_counts_by_connection: dict[int, int] = defaultdict(int)
    manager_counts_by_connection: dict[int, set[str]] = defaultdict(set)
    currencies_by_connection: dict[int, set[str]] = defaultdict(set)
    if include_accounts:
        for account in accounts:
            if account.connection_id:
                account_counts_by_connection[int(account.connection_id)] += 1
                manager_counts_by_connection[int(account.connection_id)].add(account.manager_customer_id or "")
                currencies_by_connection[int(account.connection_id)].add(account.currency_code or "UNKNOWN")

    latest_metric_date = await session.scalar(select(func.max(GoogleAdsCampaignMetric.metric_date)))
    latest_sync = await session.scalar(select(func.max(GoogleAdsCampaignMetric.synced_at)))
    connected = any(bool(connection.refresh_token) for connection in connections) or all(flags.values())
    token_saved = flags["refresh_token"]
    oauth_client_ready = flags["client_id"] and flags["client_secret"]
    connection_rows = [
        {
            "id": connection.id,
            "name": connection.name,
            "email": connection.email or "Unknown until reconnect",
            "token_saved": bool(connection.refresh_token),
            "oauth_client_ready": bool(connection.client_id and connection.client_secret),
            "client_id": connection.client_id or "",
            "developer_token_saved": bool(connection.developer_token),
            "api_version": connection.api_version or "package default",
            "is_active": bool(connection.is_active),
            "last_oauth_at": _date_text(connection.last_oauth_at),
            "data_manager_scope_ready": bool(connection.last_oauth_at),
            "data_manager_scope_label": "Ready" if connection.last_oauth_at else "Reconnect required",
            "last_discovery_at": _date_text(connection.last_discovery_at),
            "last_discovery_error": connection.last_discovery_error or "",
            "discovered_manager_count": connection.discovered_manager_count or 0,
            "discovered_account_count": connection.discovered_account_count or 0,
            "linked_account_count": account_counts_by_connection[int(connection.id)],
            "linked_manager_count": len(manager_counts_by_connection[int(connection.id)]),
            "currency_summary": ", ".join(sorted(currencies_by_connection[int(connection.id)])) or "No linked accounts",
        }
        for connection in connections
    ]
    primary_email = next((row["email"] for row in connection_rows if row["email"] != "Unknown until reconnect"), "")
    data = {
        "connected": connected,
        "token_saved": token_saved or any(row["token_saved"] for row in connection_rows),
        "oauth_client_ready": oauth_client_ready,
        "flags": flags,
        "missing": missing,
        "status_label": "Ready" if connected else "Needs setup",
        "status_tone": "success" if connected else ("warning" if token_saved else "danger"),
        "connected_email": primary_email or values.get("google_ads.connected_email") or "Unknown until reconnect",
        "oauth_connected_at": values.get("google_ads.oauth_connected_at") or "",
        "api_version": values.get("google_ads.api_version") or "package default",
        "redirect_uri": redirect_uri or "",
        "redirect_uri_variants": list(
            dict.fromkeys(
                uri
                for uri in (
                    redirect_uri or "",
                    "http://localhost:8010/settings/google-ads/oauth/callback",
                    "http://127.0.0.1:8010/settings/google-ads/oauth/callback",
                )
                if uri
            )
        ),
        "manager_count": len(managers)
        if include_accounts
        else int(
            await session.scalar(
                select(func.count(func.distinct(GoogleAdsAccount.manager_customer_id))).where(
                    GoogleAdsAccount.is_active.is_(True)
                )
            )
            or 0
        ),
        "account_count": len(accounts)
        if include_accounts
        else int(
            await session.scalar(
                select(func.count(GoogleAdsAccount.id)).where(GoogleAdsAccount.is_active.is_(True))
            )
            or 0
        ),
        "currency_summary": currency_summary,
        "connection_count": len(connection_rows),
        "connections": connection_rows,
        "managers": managers,
        "accounts": [
            {
                "name": account.name,
                "customer_id": account.customer_id,
                "currency_code": account.currency_code or "UNKNOWN",
                "manager_name": account.manager_name or "Unknown manager",
                "manager_customer_id": account.manager_customer_id or "",
                "connection_id": account.connection_id,
                "connection_name": account.connection.name if account.connection else "",
                "connection_email": account.connection.email if account.connection else "",
            }
            for account in accounts
        ],
        "latest_metric_date": _date_text(latest_metric_date),
        "latest_sync": _date_text(latest_sync),
        "today": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _status_cache[cache_key] = (time.monotonic() + STATUS_CACHE_SECONDS, data)
    return dict(data)
