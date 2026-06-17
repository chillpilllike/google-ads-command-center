from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import CampaignOptimizationRun, GoogleAdsAccount, User
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id


router = APIRouter()


OPTIMIZATION_CAMPAIGN_TYPES = {
    "pmax": "Performance Max",
    "dsa": "Dynamic Search Ads",
    "search": "Search",
    "all": "All active campaign types",
}


def _optional_int_text(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return int(digits)


async def _queue_campaign_optimization(
    *,
    account_id: int,
    campaign_type: str,
    campaign_id_text: str,
    days: int,
    max_rows: int,
    force: str,
    use_openai: str,
    apply_local_feed_labels: str,
    session: AsyncSession,
    user: User,
) -> tuple[CampaignOptimizationRun, int]:
    account = await session.get(GoogleAdsAccount, account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active account.")
    campaign_type = str(campaign_type or "all").strip().lower()
    if campaign_type not in OPTIMIZATION_CAMPAIGN_TYPES:
        raise HTTPException(status_code=400, detail="Select a supported campaign type.")
    days = min(max(int(days or 30), 1), 90)
    max_rows = min(max(int(max_rows or 3000), 50), 20000)
    campaign_id = _optional_int_text(campaign_id_text)
    run = CampaignOptimizationRun(
        account_id=account.id,
        requested_by_id=user.id,
        campaign_id=campaign_id,
        campaign_type=campaign_type,
        days=days,
        max_rows=max_rows,
        force_refresh=force == "on",
        use_openai=use_openai == "on",
        apply_local_feed_labels=apply_local_feed_labels == "on",
        status="queued",
        summary_json={
            "queued_by": user.email,
            "note": "Optimization run queued. It will reuse cached optimizer snapshots where possible.",
        },
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    job = await create_background_job(
        session,
        job_type="campaign_optimization",
        label=f"Optimize {OPTIMIZATION_CAMPAIGN_TYPES[campaign_type]}: {account.name}",
        requested_by_id=user.id,
        payload={
            "run_id": run.id,
            "account_id": account.id,
            "campaign_id": campaign_id,
            "campaign_type": campaign_type,
            "days": days,
            "max_rows": max_rows,
            "force": force == "on",
            "use_openai": use_openai == "on",
            "apply_local_feed_labels": apply_local_feed_labels == "on",
        },
    )
    try:
        from app.tasks import optimize_google_ads_campaign

        message = optimize_google_ads_campaign.send(run.id, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.error = str(exc)
        await session.commit()
        await mark_job_dispatch_failed(session, job, str(exc))
    return run, job.id


@router.get("/optimize-campaigns", response_class=HTMLResponse)
async def optimize_campaigns_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    accounts = (
        await session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    optimization_runs = (
        await session.scalars(
            select(CampaignOptimizationRun)
            .order_by(CampaignOptimizationRun.created_at.desc(), CampaignOptimizationRun.id.desc())
            .limit(30)
        )
    ).all()
    return templates.TemplateResponse(
        "optimize_campaigns.html",
        {
            "request": request,
            "user": user,
            "accounts": accounts,
            "optimization_runs": optimization_runs,
            "app_name": settings.app_name,
            "optimization_campaign_types": OPTIMIZATION_CAMPAIGN_TYPES,
            "optimization_job_id": request.query_params.get("optimization_job_id", ""),
            "optimization_run_id": request.query_params.get("optimization_run_id", ""),
        },
    )


@router.post("/optimize-campaigns")
@router.post("/ad-factory/optimize")
async def optimize_existing_campaign(
    account_id: int = Form(...),
    campaign_type: str = Form("all"),
    campaign_id_text: str = Form(""),
    days: int = Form(30),
    max_rows: int = Form(3000),
    force: str = Form(""),
    use_openai: str = Form(""),
    apply_local_feed_labels: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    run, job_id = await _queue_campaign_optimization(
        account_id=account_id,
        campaign_type=campaign_type,
        campaign_id_text=campaign_id_text,
        days=days,
        max_rows=max_rows,
        force=force,
        use_openai=use_openai,
        apply_local_feed_labels=apply_local_feed_labels,
        session=session,
        user=user,
    )
    return RedirectResponse(
        f"/optimize-campaigns?optimization_job_id={job_id}&optimization_run_id={run.id}#optimization-runs",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/optimize-campaigns/runs/{run_id}.json")
@router.get("/ad-factory/optimization-runs/{run_id}.json")
async def optimization_run_json(
    run_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> dict[str, Any]:
    run = await session.get(CampaignOptimizationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Optimization run not found.")
    return {
        "id": run.id,
        "account": {
            "id": run.account.id,
            "name": run.account.name,
            "customer_id": run.account.customer_id,
        },
        "campaign_id": run.campaign_id,
        "campaign_name": run.campaign_name,
        "campaign_type": run.campaign_type,
        "status": run.status,
        "days": run.days,
        "max_rows": run.max_rows,
        "summary": run.summary_json or {},
        "actions": run.actions_json or {},
        "source_snapshot_ids": run.source_snapshot_ids or [],
        "openai": run.openai_response_json or {},
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
