from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import AdDraft, BackgroundJob, BackgroundJobStatus, GoogleAdsAccount, GoogleAdsAutomationPreference, GoogleAdsKeywordCandidate, GoogleAdsNegativeKeywordCandidate, User
from app.runtime_role import primary_instance_required_result, runtime_role_status
from app.app_settings import get_sync_setting_map, parse_bool
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_automation import (
    automation_strategy_summary,
    budget_guard_due_now,
    clamp_int,
    load_audience_signal_inputs,
    peak_budget_due_now,
    preference_due_now,
    refresh_low_traffic_schedule,
    refresh_peak_budget_decision,
    save_audience_signal_inputs,
)


router = APIRouter()
STALE_QUEUED_AUTOMATION_JOB_AFTER = timedelta(hours=2)
STALE_RUNNING_AUTOMATION_JOB_AFTER = timedelta(hours=3)
DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK = 5
BUDGET_ONLY_REASONS = {"budget_guard", "peak_budget"}


def _customer_id_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace("\\n", "\n").replace(",", "\n").splitlines()
    return {"".join(ch for ch in item if ch.isdigit()) for item in raw_items if "".join(ch for ch in item if ch.isdigit())}


def _scheduler_tier(account: GoogleAdsAccount, primary_ids: set[str], secondary_ids: set[str]) -> int:
    customer_id = "".join(ch for ch in str(account.customer_id or "") if ch.isdigit())
    if customer_id in primary_ids:
        return 0
    if customer_id in secondary_ids:
        return 1
    return 2


def _checked(value: Optional[str]) -> bool:
    return value == "on"


def _optional_float(value: str) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clamp_float(value: Optional[float], default: float, low: float, high: float) -> float:
    if value is None:
        parsed = default
    else:
        parsed = float(value)
    return min(max(parsed, low), high)


async def _active_accounts(session: AsyncSession) -> list[GoogleAdsAccount]:
    return list(
        (
            await session.scalars(
                select(GoogleAdsAccount)
                .where(GoogleAdsAccount.is_active.is_(True))
                .order_by(GoogleAdsAccount.name)
            )
        ).all()
    )


async def _preference_for(session: AsyncSession, account: GoogleAdsAccount) -> GoogleAdsAutomationPreference:
    preference = await session.scalar(
        select(GoogleAdsAutomationPreference).where(GoogleAdsAutomationPreference.account_id == account.id)
    )
    if preference is not None:
        return preference
    preference = GoogleAdsAutomationPreference(account_id=account.id)
    session.add(preference)
    await session.commit()
    await session.refresh(preference)
    return preference


async def _automation_first_run_missing(session: AsyncSession, account_id: int) -> bool:
    active_draft_count = await session.scalar(
        select(func.count(AdDraft.id)).where(
            AdDraft.account_id == int(account_id),
            AdDraft.status.notin_(["retired", "removed"]),
        )
    )
    return int(active_draft_count or 0) == 0


async def _queue_automation_monitor_job(
    session: AsyncSession,
    *,
    account_ids: list[int],
    label: str,
    requested_by_id: Optional[int],
    force: bool = False,
    budget_only: bool = False,
    payload_extra: Optional[dict] = None,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="google_ads_automation_monitor",
        label=label,
        requested_by_id=requested_by_id,
        payload={
            "account_ids": account_ids,
            "force": bool(force),
            "budget_only": bool(budget_only),
            **(payload_extra or {}),
        },
    )
    try:
        from app.tasks import run_google_ads_automation_monitor

        message = run_google_ads_automation_monitor.send(job.id, account_ids, bool(force), budget_only)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


async def _queue_pause_automation_campaigns_job(
    session: AsyncSession,
    *,
    account: GoogleAdsAccount,
    requested_by_id: Optional[int],
    validate_only: bool = False,
    payload_extra: Optional[dict] = None,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="google_ads_pause_automation_campaigns",
        label=f"Pause automation campaigns: {account.name}",
        requested_by_id=requested_by_id,
        payload={
            "account_id": account.id,
            "customer_id": account.customer_id,
            "validate_only": bool(validate_only),
            **(payload_extra or {}),
        },
    )
    try:
        from app.tasks import pause_google_ads_automation_campaigns

        message = pause_google_ads_automation_campaigns.send(job.id, account.id, bool(validate_only))
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


@router.get("/automation", response_class=HTMLResponse)
async def automation_page(
    request: Request,
    account_id: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    accounts = await _active_accounts(session)
    selected = None
    if account_id:
        selected = next((account for account in accounts if account.id == int(account_id)), None)
    if selected is None and accounts:
        selected = accounts[0]
    preference = await _preference_for(session, selected) if selected is not None else None
    audience_signal_inputs = (
        await session.run_sync(lambda sync_session: load_audience_signal_inputs(sync_session, selected))
        if selected is not None
        else {"manual_similar_urls": [], "manual_interests": []}
    )

    preference_rows = (
        await session.scalars(
            select(GoogleAdsAutomationPreference).where(
                GoogleAdsAutomationPreference.account_id.in_([account.id for account in accounts] or [0])
            )
        )
    ).all()
    preference_by_account = {row.account_id: row for row in preference_rows}

    keyword_summary_subq = (
        select(
            GoogleAdsKeywordCandidate.account_id.label("account_id"),
            func.count(GoogleAdsKeywordCandidate.id).label("keyword_count"),
            func.max(GoogleAdsKeywordCandidate.last_pulled_at).label("last_pulled_at"),
        )
        .group_by(GoogleAdsKeywordCandidate.account_id)
        .subquery()
    )
    keyword_rows = (
        await session.execute(
            select(
                GoogleAdsAccount.id,
                keyword_summary_subq.c.keyword_count,
                keyword_summary_subq.c.last_pulled_at,
            )
            .outerjoin(keyword_summary_subq, keyword_summary_subq.c.account_id == GoogleAdsAccount.id)
            .where(GoogleAdsAccount.id.in_([account.id for account in accounts] or [0]))
        )
    ).all()
    keyword_by_account = {
        int(row[0]): {"keyword_count": int(row[1] or 0), "last_pulled_at": row[2]}
        for row in keyword_rows
    }
    negative_summary_subq = (
        select(
            GoogleAdsNegativeKeywordCandidate.account_id.label("account_id"),
            func.count(GoogleAdsNegativeKeywordCandidate.id).label("negative_count"),
            func.max(GoogleAdsNegativeKeywordCandidate.last_pulled_at).label("last_pulled_at"),
        )
        .group_by(GoogleAdsNegativeKeywordCandidate.account_id)
        .subquery()
    )
    negative_rows = (
        await session.execute(
            select(
                GoogleAdsAccount.id,
                negative_summary_subq.c.negative_count,
                negative_summary_subq.c.last_pulled_at,
            )
            .outerjoin(negative_summary_subq, negative_summary_subq.c.account_id == GoogleAdsAccount.id)
            .where(GoogleAdsAccount.id.in_([account.id for account in accounts] or [0]))
        )
    ).all()
    negative_by_account = {
        int(row[0]): {"negative_count": int(row[1] or 0), "last_pulled_at": row[2]}
        for row in negative_rows
    }
    account_summaries = [
        {
            "account": account,
            "preference": preference_by_account.get(account.id),
            "keyword": keyword_by_account.get(account.id, {"keyword_count": 0, "last_pulled_at": None}),
            "negative": negative_by_account.get(account.id, {"negative_count": 0, "last_pulled_at": None}),
        }
        for account in accounts
    ]
    recent_jobs = (
        await session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.job_type == "google_ads_automation_monitor")
            .order_by(desc(BackgroundJob.created_at), desc(BackgroundJob.id))
            .limit(8)
        )
    ).all()
    return templates.TemplateResponse(
        "automation.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "accounts": accounts,
            "account_summaries": account_summaries,
            "selected_account": selected,
            "preference": preference,
            "strategy": automation_strategy_summary(preference),
            "audience_signal_inputs": audience_signal_inputs,
            "recent_jobs": recent_jobs,
            "saved": request.query_params.get("saved") == "1",
            "job_id": request.query_params.get("job_id", ""),
            "manual_confirmed": request.query_params.get("manual_confirmed", ""),
        },
    )


@router.post("/automation/preferences")
async def save_automation_preferences(
    account_id: int = Form(...),
    automation_enabled: Optional[str] = Form(None),
    monitor_only: Optional[str] = Form(None),
    keyword_discovery_enabled: Optional[str] = Form(None),
    negative_keyword_enabled: Optional[str] = Form(None),
    audience_signal_enabled: Optional[str] = Form(None),
    landing_page_enabled: Optional[str] = Form(None),
    auction_monitor_enabled: Optional[str] = Form(None),
    odoo_sales_guard_enabled: Optional[str] = Form(None),
    auto_apply_keywords_enabled: Optional[str] = Form(None),
    auto_apply_negatives_enabled: Optional[str] = Form(None),
    auto_create_campaigns_enabled: Optional[str] = Form(None),
    auto_pause_campaigns_enabled: Optional[str] = Form(None),
    manual_first_run_criteria_csv_enabled: Optional[str] = Form(None),
    auto_peak_budget_enabled: Optional[str] = Form(None),
    testing_bootstrap_enabled: Optional[str] = Form(None),
    testing_bootstrap_days: int = Form(15),
    pmax_min_7d_conversions: str = Form("15"),
    testing_sales_budget_pct: str = Form("5"),
    testing_keyword_limit: int = Form(0),
    testing_landing_page_limit: int = Form(0),
    peak_budget_increase_pct: str = Form("50"),
    peak_budget_warmup_minutes: int = Form(60),
    peak_budget_restore_delay_minutes: int = Form(0),
    daily_keyword_lookback_days: int = Form(120),
    all_time_refresh_interval_days: int = Form(7),
    api_call_budget_per_day: int = Form(750),
    max_daily_api_rows: int = Form(10000),
    mutation_cooldown_days: int = Form(3),
    schedule_mode: str = Form("dynamic_low_traffic"),
    scheduled_hour: int = Form(4),
    scheduled_minute: int = Form(20),
    schedule_timezone: str = Form("UTC"),
    odoo_sales_max_spend_pct: str = Form("15"),
    odoo_sales_guard_window_days: int = Form(7),
    budget_guard_check_interval_hours: int = Form(6),
    minimum_daily_budget_amount: str = Form("1"),
    underperforming_budget_reduce_pct: str = Form("20"),
    peak_budget_extra_spend_pct: str = Form("5"),
    peak_budget_check_interval_minutes: int = Form(60),
    target_cost_per_conversion: str = Form(""),
    target_roas: str = Form(""),
    manual_similar_urls: str = Form(""),
    manual_interests: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active Google Ads account.")
    preference = await _preference_for(session, account)
    was_enabled = bool(preference.automation_enabled)
    preference.automation_enabled = _checked(automation_enabled)
    preference.monitor_only = _checked(monitor_only)
    preference.keyword_discovery_enabled = _checked(keyword_discovery_enabled)
    preference.negative_keyword_enabled = _checked(negative_keyword_enabled)
    preference.audience_signal_enabled = _checked(audience_signal_enabled)
    preference.landing_page_enabled = _checked(landing_page_enabled)
    preference.auction_monitor_enabled = _checked(auction_monitor_enabled)
    preference.odoo_sales_guard_enabled = _checked(odoo_sales_guard_enabled)
    preference.auto_apply_keywords_enabled = _checked(auto_apply_keywords_enabled)
    preference.auto_apply_negatives_enabled = _checked(auto_apply_negatives_enabled)
    preference.auto_create_campaigns_enabled = _checked(auto_create_campaigns_enabled)
    preference.auto_pause_campaigns_enabled = _checked(auto_pause_campaigns_enabled)
    # Criteria publishing is paced by the daily API item limiter. Keep the legacy
    # first-run CSV deferral off so campaigns are not left empty after enable.
    preference.manual_first_run_criteria_csv_enabled = False
    preference.auto_peak_budget_enabled = _checked(auto_peak_budget_enabled)
    preference.testing_bootstrap_enabled = _checked(testing_bootstrap_enabled)
    preference.testing_bootstrap_days = clamp_int(testing_bootstrap_days, 15, 1, 60)
    preference.pmax_min_7d_conversions = _clamp_float(_optional_float(pmax_min_7d_conversions), 15, 15, 1000)
    preference.testing_sales_budget_ratio = _clamp_float(_optional_float(testing_sales_budget_pct), 5, 0.1, 100) / 100
    preference.testing_keyword_limit = max(int(testing_keyword_limit or 0), 0)
    preference.testing_landing_page_limit = max(int(testing_landing_page_limit or 0), 0)
    preference.peak_budget_increase_pct = _clamp_float(_optional_float(peak_budget_increase_pct), 50, 1, 200) / 100
    preference.peak_budget_warmup_minutes = clamp_int(peak_budget_warmup_minutes, 60, 15, 240)
    preference.peak_budget_restore_delay_minutes = clamp_int(peak_budget_restore_delay_minutes, 0, 0, 240)
    preference.daily_keyword_lookback_days = clamp_int(daily_keyword_lookback_days, 120, 1, 365)
    if preference.automation_enabled and not was_enabled:
        preference.last_keyword_pull_at = None
    preference.all_time_refresh_interval_days = clamp_int(all_time_refresh_interval_days, 7, 1, 90)
    preference.api_call_budget_per_day = clamp_int(api_call_budget_per_day, 750, 10, 100000)
    preference.max_daily_api_rows = clamp_int(max_daily_api_rows, 10000, 50, 250000)
    preference.mutation_cooldown_days = clamp_int(mutation_cooldown_days, 3, 1, 60)
    preference.schedule_mode = "manual" if str(schedule_mode or "").strip() == "manual" else "dynamic_low_traffic"
    preference.scheduled_hour = clamp_int(scheduled_hour, 4, 0, 23)
    preference.scheduled_minute = clamp_int(scheduled_minute, 20, 0, 59)
    preference.schedule_timezone = str(schedule_timezone or "UTC").strip()[:80] or "UTC"
    preference.odoo_sales_max_spend_ratio = _clamp_float(_optional_float(odoo_sales_max_spend_pct), 15, 1, 100) / 100
    preference.odoo_sales_guard_window_days = clamp_int(odoo_sales_guard_window_days, 7, 1, 90)
    preference.budget_guard_check_interval_hours = clamp_int(budget_guard_check_interval_hours, 6, 1, 24)
    preference.minimum_daily_budget_amount = _clamp_float(_optional_float(minimum_daily_budget_amount), 1, 0, 100000)
    preference.underperforming_budget_reduce_pct = _clamp_float(_optional_float(underperforming_budget_reduce_pct), 20, 1, 90) / 100
    preference.peak_budget_extra_spend_ratio = _clamp_float(_optional_float(peak_budget_extra_spend_pct), 5, 0, 100) / 100
    preference.peak_budget_check_interval_minutes = clamp_int(peak_budget_check_interval_minutes, 60, 15, 240)
    preference.target_cost_per_conversion = _optional_float(target_cost_per_conversion)
    preference.target_roas = _optional_float(target_roas)
    await session.run_sync(
        lambda sync_session: save_audience_signal_inputs(
            sync_session,
            sync_session.get(GoogleAdsAccount, int(account_id)),
            manual_similar_urls=manual_similar_urls,
            manual_interests=manual_interests,
        )
    )
    await session.commit()
    if preference.schedule_mode == "dynamic_low_traffic":
        await session.run_sync(
            lambda sync_session: refresh_low_traffic_schedule(
                sync_session,
                sync_session.get(GoogleAdsAutomationPreference, preference.id),
                fetch_time_zone=False,
            )
        )
    if preference.auto_peak_budget_enabled:
        await session.run_sync(
            lambda sync_session: refresh_peak_budget_decision(
                sync_session,
                sync_session.get(GoogleAdsAutomationPreference, preference.id),
                fetch_time_zone=False,
            )
        )
    queued_job_id = ""
    if was_enabled and not preference.automation_enabled and primary_instance_required_result() is None:
        job = await _queue_pause_automation_campaigns_job(
            session,
            account=account,
            requested_by_id=user.id,
            payload_extra={"queued_by": "automation_preferences_disable"},
        )
        queued_job_id = str(job.id)
    elif preference.automation_enabled and not was_enabled and primary_instance_required_result() is None:
        first_run_missing = await _automation_first_run_missing(session, account.id)
        active_job = await session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status.in_(
                    [
                        BackgroundJobStatus.queued,
                        BackgroundJobStatus.running,
                        BackgroundJobStatus.cancel_requested,
                    ]
                ),
            )
            .order_by(desc(BackgroundJob.created_at), desc(BackgroundJob.id))
            .limit(1)
        )
        if first_run_missing and active_job is None:
            job = await _queue_automation_monitor_job(
                session,
                account_ids=[account.id],
                label=f"Automation first run: {account.name}",
                requested_by_id=user.id,
                force=True,
                payload_extra={
                    "customer_id": account.customer_id,
                    "queued_by": "automation_preferences_enable",
                    "due_reasons": {account.id: ["first_run_bootstrap"]},
                },
            )
            queued_job_id = str(job.id)
    job_query = f"&job_id={queued_job_id}" if queued_job_id else ""
    return RedirectResponse(f"/automation?account_id={account.id}&saved=1{job_query}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/automation/run")
async def queue_automation_monitor(
    account_id: int = Form(...),
    force: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    if primary_instance_required_result() is not None:
        raise HTTPException(status_code=403, detail="This app instance is not primary; live automation can only be queued from the primary server.")
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active Google Ads account.")
    preference = await _preference_for(session, account)
    if not preference.automation_enabled:
        preference.automation_enabled = True
        preference.last_keyword_pull_at = None
        await session.commit()
    job = await _queue_automation_monitor_job(
        session,
        account_ids=[account.id],
        label=f"Automation monitor: {account.name}",
        requested_by_id=user.id,
        force=_checked(force),
        payload_extra={
            "customer_id": account.customer_id,
            "monitor_only": preference.monitor_only,
        },
    )
    return RedirectResponse(
        f"/automation?account_id={account.id}&job_id={job.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/api/automation/scheduler/tick")
async def automation_scheduler_tick(
    request: Request,
    recompute: int = 1,
    force: int = 0,
    max_accounts: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    runtime_block = primary_instance_required_result()
    if runtime_block is not None:
        return JSONResponse({"queued": False, "reason": "not_primary_instance", **runtime_role_status()})
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Automation scheduler tick is local-only.")
    preferences = (
        await session.scalars(
            select(GoogleAdsAutomationPreference)
            .options(selectinload(GoogleAdsAutomationPreference.account))
            .join(GoogleAdsAccount, GoogleAdsAccount.id == GoogleAdsAutomationPreference.account_id)
            .where(
                GoogleAdsAutomationPreference.automation_enabled.is_(True),
                GoogleAdsAccount.is_active.is_(True),
            )
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    now = datetime.now(timezone.utc)
    scheduler_settings = await session.run_sync(lambda sync_session: get_sync_setting_map(sync_session))
    primary_customer_ids = _customer_id_set(scheduler_settings.get("automation.scheduler_primary_customer_ids"))
    secondary_customer_ids = _customer_id_set(scheduler_settings.get("automation.scheduler_secondary_customer_ids"))
    include_unlisted = parse_bool(scheduler_settings.get("automation.scheduler_include_unlisted_accounts", False))
    due_candidates: list[tuple[int, datetime, str, int]] = []
    due_reasons: dict[int, list[str]] = {}
    due_tiers: dict[int, int] = {}
    decisions: list[dict] = []
    for preference in preferences:
        account = preference.account
        if recompute:
            schedule_decision = await session.run_sync(
                lambda sync_session, preference_id=preference.id: refresh_low_traffic_schedule(
                    sync_session,
                    sync_session.get(GoogleAdsAutomationPreference, preference_id),
                    fetch_time_zone=False,
                )
            )
            peak_decision = await session.run_sync(
                lambda sync_session, preference_id=preference.id: refresh_peak_budget_decision(
                    sync_session,
                    sync_session.get(GoogleAdsAutomationPreference, preference_id),
                    fetch_time_zone=False,
                )
            )
            decisions.append(
                {
                    "account_id": preference.account_id,
                    "customer_id": account.customer_id,
                    "priority_tier": _scheduler_tier(account, primary_customer_ids, secondary_customer_ids),
                    "time": schedule_decision.get("recommended_time"),
                    "time_zone": schedule_decision.get("time_zone"),
                    "low_hour_impressions": schedule_decision.get("low_hour_impressions"),
                    "traffic_peak_hour": schedule_decision.get("peak_hour"),
                    "traffic_peak_hour_impressions": schedule_decision.get("peak_hour_impressions"),
                    "conversion_peak_time": peak_decision.get("peak_time"),
                    "conversion_peak_conversions": peak_decision.get("peak_hour_conversions"),
                    "budget_boost_start_time": peak_decision.get("boost_start_time"),
                }
            )
            await session.refresh(preference)
        schedule_due = preference_due_now(preference, now=now)
        budget_guard_due = budget_guard_due_now(preference, now=now)
        peak_due = await session.run_sync(
            lambda sync_session, preference_id=preference.id: peak_budget_due_now(
                sync_session,
                sync_session.get(GoogleAdsAutomationPreference, preference_id),
                now=now,
            )
        )
        reasons = []
        if schedule_due:
            reasons.append("daily_low_traffic")
        if budget_guard_due:
            reasons.append("budget_guard")
        if peak_due:
            reasons.append("peak_budget")
        first_run_missing = await _automation_first_run_missing(session, preference.account_id)
        if first_run_missing:
            reasons.append("first_run_bootstrap")
        if force and not reasons:
            reasons.append("manual_force")
        if reasons:
            tier = _scheduler_tier(account, primary_customer_ids, secondary_customer_ids)
            heavy_reasons = not set(reasons).issubset(BUDGET_ONLY_REASONS)
            if tier > 1 and heavy_reasons and not include_unlisted and not force:
                decisions.append(
                    {
                        "account_id": preference.account_id,
                        "customer_id": account.customer_id,
                        "priority_tier": tier,
                        "status": "deferred_unlisted_basic_access",
                        "reasons": reasons,
                    }
                )
                continue
            due_candidates.append(
                (
                    tier,
                    preference.last_run_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
                    account.name,
                    preference.account_id,
                )
            )
            due_reasons[preference.account_id] = reasons
            due_tiers[preference.account_id] = tier
    due_account_ids = [account_id for _tier, _last_run, _name, account_id in sorted(due_candidates)]
    due_account_ids = list(dict.fromkeys(due_account_ids))
    original_due_account_ids = list(due_account_ids)
    configured_max_accounts = clamp_int(
        max_accounts
        if max_accounts is not None
        else scheduler_settings.get(
            "automation.scheduler_max_accounts_per_tick",
            DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK,
        ),
        DEFAULT_SCHEDULER_MAX_ACCOUNTS_PER_TICK,
        1,
        100,
    )
    deferred_account_ids: list[int] = []
    if len(due_account_ids) > configured_max_accounts:
        deferred_account_ids = due_account_ids[configured_max_accounts:]
        due_account_ids = due_account_ids[:configured_max_accounts]
        due_reasons = {account_id: due_reasons[account_id] for account_id in due_account_ids}
    budget_only = bool(due_account_ids) and all(set(due_reasons.get(account_id) or []).issubset(BUDGET_ONLY_REASONS) for account_id in due_account_ids)
    if not due_account_ids:
        return JSONResponse({"queued": False, "reason": "not_due", "schedule_decisions": decisions})
    stale_before = now - STALE_QUEUED_AUTOMATION_JOB_AFTER
    stale_jobs = (
        await session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status == BackgroundJobStatus.queued,
                BackgroundJob.started_at.is_(None),
                BackgroundJob.created_at < stale_before,
            )
        )
    ).all()
    for stale_job in stale_jobs:
        stale_job.status = BackgroundJobStatus.failed
        stale_job.finished_at = now
        stale_job.error = "Stale queued automation job never started; marked failed so scheduler can resume."
    if stale_jobs:
        await session.commit()
    stale_running_before = now - STALE_RUNNING_AUTOMATION_JOB_AFTER
    stale_running_jobs = (
        await session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "google_ads_automation_monitor",
                BackgroundJob.status.in_([BackgroundJobStatus.running, BackgroundJobStatus.cancel_requested]),
                BackgroundJob.started_at.is_not(None),
                BackgroundJob.started_at < stale_running_before,
            )
        )
    ).all()
    for stale_job in stale_running_jobs:
        stale_job.status = BackgroundJobStatus.failed
        stale_job.finished_at = now
        stale_job.error = "Stale running automation job exceeded the worker lease window; marked failed so scheduler can resume."
    if stale_running_jobs:
        await session.commit()
    active_job = await session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.job_type == "google_ads_automation_monitor",
            BackgroundJob.status.in_(
                [
                    BackgroundJobStatus.queued,
                    BackgroundJobStatus.running,
                    BackgroundJobStatus.cancel_requested,
                ]
            ),
        )
        .order_by(desc(BackgroundJob.created_at), desc(BackgroundJob.id))
        .limit(1)
    )
    if active_job is not None:
        return JSONResponse(
            {
                "queued": False,
                "reason": "active_job_exists",
                "active_job_id": active_job.id,
                "active_status": active_job.status.value,
                "due_account_ids": due_account_ids,
                "original_due_account_ids": original_due_account_ids,
                "deferred_account_ids": deferred_account_ids,
                "scheduler_max_accounts_per_tick": configured_max_accounts,
                "due_reasons": due_reasons,
                "due_tiers": {account_id: due_tiers.get(account_id) for account_id in due_account_ids},
                "budget_only": budget_only,
                "schedule_decisions": decisions,
            }
        )
    job = await create_background_job(
        session,
        job_type="google_ads_automation_monitor",
        label=f"Automation monitor: {len(due_account_ids)} due account(s)",
        requested_by_id=None,
        payload={
            "account_ids": due_account_ids,
            "force": bool(force),
            "budget_only": budget_only,
            "queued_by": "local_scheduler_tick",
            "due_reasons": due_reasons,
            "deferred_account_ids": deferred_account_ids,
            "scheduler_max_accounts_per_tick": configured_max_accounts,
            "original_due_count": len(original_due_account_ids),
            "priority": {
                "primary_customer_ids": sorted(primary_customer_ids),
                "secondary_customer_ids": sorted(secondary_customer_ids),
                "include_unlisted_accounts": include_unlisted,
                "due_tiers": {account_id: due_tiers.get(account_id) for account_id in due_account_ids},
            },
            "schedule_decisions": decisions,
        },
    )
    try:
        from app.tasks import run_google_ads_automation_monitor

        message = run_google_ads_automation_monitor.send(job.id, due_account_ids, bool(force), budget_only)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
        return JSONResponse({"queued": False, "reason": "dispatch_failed", "job_id": job.id, "error": str(exc)})
    return JSONResponse(
        {
            "queued": True,
            "job_id": job.id,
            "due_account_ids": due_account_ids,
            "original_due_account_ids": original_due_account_ids,
            "deferred_account_ids": deferred_account_ids,
            "scheduler_max_accounts_per_tick": configured_max_accounts,
            "due_reasons": due_reasons,
            "due_tiers": {account_id: due_tiers.get(account_id) for account_id in due_account_ids},
            "budget_only": budget_only,
            "schedule_decisions": decisions,
        }
    )
