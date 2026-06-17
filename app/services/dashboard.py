from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GoogleAdsAccount, Strategy, StrategyRun


_dashboard_static_cache: dict[str, object] = {"expires_at": 0.0, "accounts": [], "strategies": []}
_recent_runs_cache: dict[str, object] = {"expires_at": 0.0, "runs": []}


async def get_dashboard_static_data(session: AsyncSession) -> tuple[list[GoogleAdsAccount], list[Strategy]]:
    now = time.monotonic()
    if float(_dashboard_static_cache["expires_at"]) > now:
        return (
            list(_dashboard_static_cache["accounts"]),
            list(_dashboard_static_cache["strategies"]),
        )

    accounts = (
        await session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.manager_name, GoogleAdsAccount.name)
        )
    ).all()
    strategies = (
        await session.scalars(select(Strategy).where(Strategy.is_active.is_(True)).order_by(Strategy.id))
    ).all()
    _dashboard_static_cache.update(
        {
            "expires_at": now + 300,
            "accounts": list(accounts),
            "strategies": list(strategies),
        }
    )
    return list(accounts), list(strategies)


async def get_recent_runs(session: AsyncSession) -> list[StrategyRun]:
    now = time.monotonic()
    if float(_recent_runs_cache["expires_at"]) > now:
        return list(_recent_runs_cache["runs"])
    runs = (
        await session.scalars(select(StrategyRun).order_by(StrategyRun.created_at.desc()).limit(8))
    ).all()
    _recent_runs_cache.update({"expires_at": now + 30, "runs": list(runs)})
    return list(runs)
