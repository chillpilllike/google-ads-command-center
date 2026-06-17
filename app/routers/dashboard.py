from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import (
    AutoPilotEvent,
    BackgroundJob,
    BackgroundJobStatus,
    CostDashboardSnapshot,
    GoogleAdsAccount,
    GoogleAdsConversionGoalSnapshot,
    RunStatus,
    Strategy,
    StrategyRun,
    User,
)
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, request_background_job_cancel, save_job_message_id
from app.services.cost_dashboard_snapshot import upsert_cost_dashboard_snapshot
from app.services.currency_rates import get_latest_rate_snapshot, snapshot_payload
from app.services.dashboard import get_dashboard_static_data, get_recent_runs
from app.services.google_ads_api_errors import (
    acknowledge_unacknowledged_google_ads_api_errors,
    recent_unacknowledged_google_ads_api_errors,
)
from app.services.google_ads_connection import get_google_ads_connection_status


router = APIRouter()
_cost_dashboard_cache: dict[int, tuple[float, dict]] = {}
_conversion_goals_cache: dict[str, tuple[float, dict]] = {}
_dashboard_home_cache: dict[str, object] = {"expires_at": 0.0, "data": {}}
COST_DASHBOARD_CACHE_SECONDS = 30.0
CACHE_SECONDS = 120.0
DASHBOARD_HOME_CACHE_SECONDS = 20.0


def refresh_cost_dashboard_snapshot(days: int) -> dict:
    sync_engine = create_engine(
        settings.sqlalchemy_sync_url,
        connect_args={"sslmode": "disable", "connect_timeout": 10},
        poolclass=NullPool,
    )
    try:
        with Session(sync_engine) as sync_session:
            return upsert_cost_dashboard_snapshot(sync_session, days)
    finally:
        sync_engine.dispose()


async def attach_currency_rates(session: AsyncSession, data: dict) -> dict:
    enriched = dict(data or {})
    try:
        enriched["currency_rates"] = snapshot_payload(await get_latest_rate_snapshot(session))
    except Exception as exc:  # noqa: BLE001 - show saved dashboard even if rates fail.
        enriched.setdefault(
            "currency_rates",
            {
                "source": "openexchangerates",
                "base_currency": "USD",
                "rates": {"USD": 1.0},
                "rate_date": "",
                "fetched_at": "",
                "error": str(exc),
            },
        )
    return enriched


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    cached = _dashboard_home_cache.get("data")
    if float(_dashboard_home_cache.get("expires_at", 0.0)) > time.monotonic() and isinstance(cached, dict):
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "app_name": settings.app_name,
                **cached,
            },
        )

    accounts, strategies = await get_dashboard_static_data(session)
    runs = await get_recent_runs(session)
    queued = int(
        await session.scalar(
            select(func.count(BackgroundJob.id)).where(BackgroundJob.status == BackgroundJobStatus.queued)
        )
        or 0
    )
    running = int(
        await session.scalar(
            select(func.count(BackgroundJob.id)).where(
                BackgroundJob.status.in_([BackgroundJobStatus.running, BackgroundJobStatus.cancel_requested])
            )
        )
        or 0
    )
    counts = {
        "accounts": len(accounts),
        "queued": queued,
        "running": running,
    }
    google_ads_status = await get_google_ads_connection_status(session, include_accounts=False)
    data = {
        "accounts": accounts,
        "strategies": strategies,
        "runs": runs,
        "counts": counts,
        "google_ads_status": google_ads_status,
    }
    _dashboard_home_cache.update({"expires_at": time.monotonic() + DASHBOARD_HOME_CACHE_SECONDS, "data": data})
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            **data,
        },
    )


@router.get("/strategies", response_class=HTMLResponse)
async def strategies_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    strategies = (
        await session.scalars(select(Strategy).order_by(Strategy.id))
    ).all()
    events = (
        await session.scalars(select(AutoPilotEvent).order_by(AutoPilotEvent.created_at.desc()).limit(20))
    ).all()
    return templates.TemplateResponse(
        "strategies.html",
        {
            "request": request,
            "user": user,
            "strategies": strategies,
            "events": events,
            "app_name": settings.app_name,
        },
    )


@router.get("/cost-dashboard", response_class=HTMLResponse)
async def cost_dashboard(
    request: Request,
    days: int = 30,
    period: str = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    days = min(max(days, 1), 90)
    period = "yesterday" if period == "yesterday" else "range"
    sync_queued = request.query_params.get("sync_queued") == "1" or request.query_params.get("sync") == "1"
    sync_job_id = request.query_params.get("job_id", "")
    cached = _cost_dashboard_cache.get(days)
    if (
        COST_DASHBOARD_CACHE_SECONDS > 0
        and cached
        and cached[0] > time.monotonic()
        and not sync_queued
    ):
        cached_data = await attach_currency_rates(session, cached[1])
        return templates.TemplateResponse(
            "cost_dashboard.html",
            {
                "request": request,
                "user": user,
                "app_name": settings.app_name,
                **cached_data,
                "period": period,
                "synced": sync_queued,
                "sync_job_id": sync_job_id,
            },
        )

    snapshot = await session.scalar(select(CostDashboardSnapshot).where(CostDashboardSnapshot.days == days))
    if snapshot is not None:
        data = dict(snapshot.data or {})
    else:
        data = await asyncio.to_thread(refresh_cost_dashboard_snapshot, days)

    data = await attach_currency_rates(session, data)
    if COST_DASHBOARD_CACHE_SECONDS > 0 and not sync_queued:
        _cost_dashboard_cache[days] = (time.monotonic() + COST_DASHBOARD_CACHE_SECONDS, data)
    return templates.TemplateResponse(
        "cost_dashboard.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            **data,
            "period": period,
            "synced": sync_queued,
            "sync_job_id": sync_job_id,
        },
    )


@router.get("/conversion-goals", response_class=HTMLResponse)
async def conversion_goals_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    cached = _conversion_goals_cache.get("index")
    if cached and cached[0] > time.monotonic() and request.query_params.get("sync") != "1":
        return templates.TemplateResponse(
            "conversion_goals.html",
            {
                "request": request,
                "user": user,
                "app_name": settings.app_name,
                **cached[1],
                "synced": False,
                "sync_job_id": request.query_params.get("job_id", ""),
            },
        )

    customer_goals = (
        await session.scalars(
            select(GoogleAdsConversionGoalSnapshot)
            .where(GoogleAdsConversionGoalSnapshot.level == "customer")
            .order_by(GoogleAdsConversionGoalSnapshot.account_id, GoogleAdsConversionGoalSnapshot.category)
            .limit(50)
        )
    ).all()
    campaign_goals = (
        await session.scalars(
            select(GoogleAdsConversionGoalSnapshot)
            .where(GoogleAdsConversionGoalSnapshot.level == "campaign")
            .order_by(GoogleAdsConversionGoalSnapshot.account_id, GoogleAdsConversionGoalSnapshot.campaign_name)
            .limit(50)
        )
    ).all()
    config_rows = (
        await session.scalars(
            select(GoogleAdsConversionGoalSnapshot)
            .where(GoogleAdsConversionGoalSnapshot.level == "campaign_config")
            .order_by(GoogleAdsConversionGoalSnapshot.account_id, GoogleAdsConversionGoalSnapshot.campaign_name)
            .limit(50)
        )
    ).all()
    counts = {
        "customer": await session.scalar(
            select(func.count(GoogleAdsConversionGoalSnapshot.id)).where(
                GoogleAdsConversionGoalSnapshot.level == "customer"
            )
        ),
        "campaign": await session.scalar(
            select(func.count(GoogleAdsConversionGoalSnapshot.id)).where(
                GoogleAdsConversionGoalSnapshot.level == "campaign"
            )
        ),
        "custom": await session.scalar(
            select(func.count(GoogleAdsConversionGoalSnapshot.id)).where(
                GoogleAdsConversionGoalSnapshot.level == "campaign_config",
                GoogleAdsConversionGoalSnapshot.custom_conversion_goal != "",
            )
        ),
    }
    data = {
        "customer_goals": customer_goals,
        "campaign_goals": campaign_goals,
        "config_rows": config_rows,
        "counts": counts,
    }
    _conversion_goals_cache["index"] = (time.monotonic() + CACHE_SECONDS, data)
    return templates.TemplateResponse(
        "conversion_goals.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            **data,
            "synced": request.query_params.get("sync") == "1",
            "sync_job_id": request.query_params.get("job_id", ""),
        },
    )


@router.post("/sync/conversion-goals")
async def sync_conversion_goals(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    job = await create_background_job(
        session,
        job_type="conversion_goal_sync",
        label="Sync conversion goals",
        requested_by_id=user.id,
        payload={},
    )
    try:
        from app.tasks import sync_conversion_goal_snapshots

        message = sync_conversion_goal_snapshots.send(job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - show dispatch errors in the UI.
        await mark_job_dispatch_failed(session, job, str(exc))
    _dashboard_home_cache["expires_at"] = 0.0
    return RedirectResponse(f"/conversion-goals?sync=1&job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sync/google-ads")
async def sync_google_ads(
    days: int = Form(30),
    period: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    days = min(max(days, 1), 90)
    period = "yesterday" if period == "yesterday" else ""
    sync_days = max(days, 2) if period == "yesterday" else days
    _cost_dashboard_cache.pop(days, None)
    _cost_dashboard_cache.pop(sync_days, None)
    _dashboard_home_cache["expires_at"] = 0.0
    job = await create_background_job(
        session,
        job_type="google_ads_sync",
        label=f"Sync Google Ads {sync_days}d performance",
        requested_by_id=user.id,
        payload={"days": sync_days, "period": period or "range"},
    )
    try:
        from app.tasks import sync_google_ads_performance

        message = sync_google_ads_performance.send(sync_days, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    period_query = "&period=yesterday" if period == "yesterday" else ""
    return RedirectResponse(f"/cost-dashboard?days={days}{period_query}&sync_queued=1&job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sync/google-ads/accounts")
async def sync_selected_google_ads_accounts(
    account_ids: list[int] = Form(...),
    days: int = Form(30),
    period: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    selected_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="Select at least one Google Ads account to sync.")
    days = min(max(days, 1), 90)
    period = "yesterday" if period == "yesterday" else ""
    sync_days = max(days, 2) if period == "yesterday" else days
    _cost_dashboard_cache.pop(days, None)
    _cost_dashboard_cache.pop(sync_days, None)
    _dashboard_home_cache["expires_at"] = 0.0
    active_count = int(
        await session.scalar(
            select(func.count(GoogleAdsAccount.id)).where(
                GoogleAdsAccount.id.in_(selected_ids),
                GoogleAdsAccount.is_active.is_(True),
            )
        )
        or 0
    )
    job = await create_background_job(
        session,
        job_type="google_ads_selected_sync",
        label=f"Sync {active_count} selected Google Ads account{'s' if active_count != 1 else ''}",
        requested_by_id=user.id,
        payload={"days": sync_days, "period": period or "range", "account_ids": selected_ids},
    )
    try:
        from app.tasks import sync_google_ads_performance

        message = sync_google_ads_performance.send(sync_days, job.id, selected_ids)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    period_query = "&period=yesterday" if period == "yesterday" else ""
    return RedirectResponse(f"/cost-dashboard?days={days}{period_query}&sync_queued=1&job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/sync/razorpay")
async def sync_razorpay(
    days: int = Form(30),
    period: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    days = min(max(days, 1), 90)
    period = "yesterday" if period == "yesterday" else ""
    sync_days = max(days, 2) if period == "yesterday" else days
    _cost_dashboard_cache.pop(days, None)
    _cost_dashboard_cache.pop(sync_days, None)
    _dashboard_home_cache["expires_at"] = 0.0
    job = await create_background_job(
        session,
        job_type="razorpay_sync",
        label=f"Sync Razorpay {sync_days}d receipts",
        requested_by_id=user.id,
        payload={"days": sync_days, "period": period or "range"},
    )
    try:
        from app.tasks import sync_razorpay_receipts

        message = sync_razorpay_receipts.send(sync_days, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    period_query = "&period=yesterday" if period == "yesterday" else ""
    return RedirectResponse(f"/cost-dashboard?days={days}{period_query}&sync_queued=1&job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/runs")
async def create_run(
    request: Request,
    account_ids: list[int] = Form(...),
    strategy_keys: list[str] = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    if not account_ids or not strategy_keys:
        raise HTTPException(status_code=400, detail="Select at least one account and one strategy.")

    active_account_count = await session.scalar(
        select(func.count(GoogleAdsAccount.id)).where(
            GoogleAdsAccount.id.in_(account_ids),
            GoogleAdsAccount.is_active.is_(True),
        )
    )
    active_strategy_count = await session.scalar(
        select(func.count(Strategy.id)).where(Strategy.key.in_(strategy_keys), Strategy.is_active.is_(True))
    )
    if active_account_count != len(set(account_ids)) or active_strategy_count != len(set(strategy_keys)):
        raise HTTPException(status_code=400, detail="One or more selected accounts or strategies are inactive.")

    run = StrategyRun(
        status=RunStatus.queued,
        requested_by_id=user.id,
        account_ids=list(dict.fromkeys(account_ids)),
        strategy_keys=list(dict.fromkeys(strategy_keys)),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    job = await create_background_job(
        session,
        job_type="strategy_run",
        label=f"Strategy run #{run.id}",
        requested_by_id=user.id,
        payload={"run_id": run.id, "account_count": len(run.account_ids), "strategy_keys": run.strategy_keys},
    )
    try:
        from app.tasks import execute_strategy_run

        message = execute_strategy_run.send(run.id, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    _dashboard_home_cache["expires_at"] = 0.0
    return RedirectResponse(f"/runs/{run.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/jobs", response_class=HTMLResponse)
async def background_jobs_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    jobs = (
        await session.scalars(
            select(BackgroundJob)
            .order_by(BackgroundJob.created_at.desc())
            .limit(100)
        )
    ).all()
    active_statuses = {BackgroundJobStatus.queued, BackgroundJobStatus.running, BackgroundJobStatus.cancel_requested}
    return templates.TemplateResponse(
        "jobs.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "jobs": jobs,
            "has_active_jobs": any(job.status in active_statuses for job in jobs),
            "killed": request.query_params.get("killed") == "1",
        },
    )


@router.post("/jobs/{job_id}/kill")
async def kill_background_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    job = await session.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Background job not found.")
    await request_background_job_cancel(session, job)
    _dashboard_home_cache["expires_at"] = 0.0
    return RedirectResponse("/jobs?killed=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/jobs")
async def background_jobs_api(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    jobs = (
        await session.scalars(
            select(BackgroundJob)
            .order_by(BackgroundJob.created_at.desc())
            .limit(50)
        )
    ).all()
    return JSONResponse(
        {
            "jobs": [
                {
                    "id": job.id,
                    "label": job.label,
                    "job_type": job.job_type,
                    "status": job.status.value,
                    "progress_current": job.progress_current,
                    "progress_total": job.progress_total,
                    "cancel_requested": job.cancel_requested,
                    "error": job.error or "",
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                }
                for job in jobs
            ]
        }
    )


@router.get("/api/google-ads-errors/recent")
async def recent_google_ads_api_errors(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    return JSONResponse(await recent_unacknowledged_google_ads_api_errors(session))


@router.post("/api/google-ads-errors/acknowledge")
async def acknowledge_google_ads_api_errors(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    return JSONResponse({"acknowledged": await acknowledge_unacknowledged_google_ads_api_errors(session)})


@router.post("/google-ads-errors/acknowledge")
async def acknowledge_google_ads_api_errors_redirect(
    request: Request,
    next_url: str = Form("/"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    await acknowledge_unacknowledged_google_ads_api_errors(session)
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    return RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request,
    run_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    run = await session.get(StrategyRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run": run, "user": user, "app_name": settings.app_name},
    )


@router.get("/api/runs/{run_id}")
async def run_status(
    run_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    run = await session.get(StrategyRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return JSONResponse(
        {
            "id": run.id,
            "status": run.status.value,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "items": [
                {
                    "account": item.account.name,
                    "customer_id": item.account.customer_id,
                    "strategy_key": item.strategy_key,
                    "status": item.status.value,
                    "error": item.error,
                }
                for item in run.accounts
            ],
        }
    )
