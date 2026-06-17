from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import AppSetting, CurrencyRateSnapshot


SOURCE = "openexchangerates"
BASE_CURRENCY = "USD"
SETTING_KEY = "currency.openexchange_app_id"
TARGET_CURRENCIES = (
    "USD",
    "INR",
    "AUD",
    "CAD",
    "GBP",
    "EUR",
    "NZD",
    "JPY",
    "BRL",
    "MXN",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
)
OPENEXCHANGE_URL = "https://openexchangerates.org/api/latest.json"


def normalize_currency(value: Any) -> str:
    currency = str(value or "").strip().upper()
    return currency if currency else "UNKNOWN"


def normalize_rates(rates: dict[str, Any] | None) -> dict[str, float]:
    normalized: dict[str, float] = {"USD": 1.0}
    for currency, value in (rates or {}).items():
        code = normalize_currency(currency)
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            normalized[code] = rate
    return normalized


def convert_amount(
    amount: float,
    from_currency: str,
    to_currency: str,
    rates: dict[str, Any] | None,
) -> Optional[float]:
    source = normalize_currency(from_currency)
    target = normalize_currency(to_currency)
    if source == target:
        return float(amount or 0)
    normalized = normalize_rates(rates)
    source_rate = normalized.get(source)
    target_rate = normalized.get(target)
    if not source_rate or not target_rate:
        return None
    return (float(amount or 0) / source_rate) * target_rate


def snapshot_payload(snapshot: CurrencyRateSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {
            "source": SOURCE,
            "base_currency": BASE_CURRENCY,
            "rates": {"USD": 1.0},
            "rate_date": "",
            "fetched_at": "",
            "error": "",
        }
    return {
        "source": snapshot.source,
        "base_currency": snapshot.base_currency,
        "rates": normalize_rates(snapshot.rates),
        "rate_date": snapshot.rate_date.isoformat() if snapshot.rate_date else "",
        "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else "",
        "error": snapshot.error or "",
    }


def fetch_openexchange_rates(app_id: str) -> tuple[date, dict[str, float], datetime]:
    response = requests.get(
        OPENEXCHANGE_URL,
        params={
            "app_id": app_id,
            "symbols": ",".join(TARGET_CURRENCIES),
            "prettyprint": "0",
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    fetched_at = datetime.fromtimestamp(int(payload.get("timestamp") or 0), tz=timezone.utc)
    if fetched_at.year < 2000:
        fetched_at = datetime.now(timezone.utc)
    rates = normalize_rates(payload.get("rates"))
    missing = [currency for currency in TARGET_CURRENCIES if currency not in rates]
    if missing:
        raise RuntimeError(f"OpenExchange response missing rates for: {', '.join(missing)}")
    return fetched_at.date(), rates, fetched_at


def _latest_snapshot_query():
    return (
        select(CurrencyRateSnapshot)
        .where(
            CurrencyRateSnapshot.base_currency == BASE_CURRENCY,
            CurrencyRateSnapshot.source == SOURCE,
        )
        .order_by(CurrencyRateSnapshot.rate_date.desc(), CurrencyRateSnapshot.fetched_at.desc())
    )


def _has_target_rates(snapshot: CurrencyRateSnapshot | None) -> bool:
    if snapshot is None:
        return False
    rates = normalize_rates(snapshot.rates)
    return all(currency in rates for currency in TARGET_CURRENCIES)


def _upsert_snapshot(
    session: Session,
    *,
    rate_date: date,
    rates: dict[str, float],
    fetched_at: datetime,
    error: str = "",
) -> CurrencyRateSnapshot:
    stmt = insert(CurrencyRateSnapshot).values(
        rate_date=rate_date,
        base_currency=BASE_CURRENCY,
        source=SOURCE,
        rates=rates,
        fetched_at=fetched_at,
        error=error,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_currency_rate_snapshot",
        set_={
            "rates": stmt.excluded.rates,
            "fetched_at": stmt.excluded.fetched_at,
            "error": stmt.excluded.error,
        },
    )
    session.execute(stmt)
    session.commit()
    return session.scalars(_latest_snapshot_query()).first()


async def _upsert_snapshot_async(
    session: AsyncSession,
    *,
    rate_date: date,
    rates: dict[str, float],
    fetched_at: datetime,
    error: str = "",
) -> CurrencyRateSnapshot | None:
    stmt = insert(CurrencyRateSnapshot).values(
        rate_date=rate_date,
        base_currency=BASE_CURRENCY,
        source=SOURCE,
        rates=rates,
        fetched_at=fetched_at,
        error=error,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_currency_rate_snapshot",
        set_={
            "rates": stmt.excluded.rates,
            "fetched_at": stmt.excluded.fetched_at,
            "error": stmt.excluded.error,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return await session.scalar(_latest_snapshot_query())


def get_latest_rate_snapshot_sync(session: Session, *, force: bool = False) -> CurrencyRateSnapshot | None:
    today = datetime.now(timezone.utc).date()
    latest = session.scalars(_latest_snapshot_query()).first()
    if latest and latest.rate_date >= today and latest.rates and _has_target_rates(latest) and not force:
        return latest

    app_id = session.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
    app_id_text = str(app_id or "").strip()
    if not app_id_text:
        return latest

    try:
        rate_date, rates, fetched_at = fetch_openexchange_rates(app_id_text)
        return _upsert_snapshot(session, rate_date=rate_date, rates=rates, fetched_at=fetched_at)
    except Exception as exc:  # noqa: BLE001 - stale rates are better than breaking the dashboard.
        if latest:
            return latest
        return _upsert_snapshot(
            session,
            rate_date=today,
            rates={"USD": 1.0},
            fetched_at=datetime.now(timezone.utc),
            error=str(exc),
        )


async def get_latest_rate_snapshot(session: AsyncSession, *, force: bool = False) -> CurrencyRateSnapshot | None:
    today = datetime.now(timezone.utc).date()
    latest = await session.scalar(_latest_snapshot_query())
    if latest and latest.rate_date >= today and latest.rates and _has_target_rates(latest) and not force:
        return latest

    app_id = await session.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
    app_id_text = str(app_id or "").strip()
    if not app_id_text:
        return latest

    try:
        rate_date, rates, fetched_at = await asyncio.to_thread(fetch_openexchange_rates, app_id_text)
        return await _upsert_snapshot_async(session, rate_date=rate_date, rates=rates, fetched_at=fetched_at)
    except Exception as exc:  # noqa: BLE001
        if latest:
            return latest
        return await _upsert_snapshot_async(
            session,
            rate_date=today,
            rates={"USD": 1.0},
            fetched_at=datetime.now(timezone.utc),
            error=str(exc),
        )
