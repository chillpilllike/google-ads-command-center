from __future__ import annotations

import math
from typing import AsyncIterator, Optional, Union
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_session
from app.dependencies import require_user, settings, templates
from app.models import GoogleAdsAccount, GoogleAnalyticsSearchTermCandidate, User
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_analytics_search_term_csv import ga4_search_terms_csv_chunk, ga4_search_terms_csv_filename


router = APIRouter()

QUALITY_LABELS = {
    "revenue": "Revenue",
    "converting": "Converting",
    "clicked": "Clicked",
    "watch": "Watch",
}
DEFAULT_PER_PAGE = 100
MAX_COPY_TERMS = 5000
CSV_EXPORT_BATCH_SIZE = 1000


def _ga4_terms_url(
    *,
    account_id: Optional[int],
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    q: str = "",
    quality: str = "",
) -> str:
    params: dict[str, Union[str, int]] = {
        "page": max(int(page or 1), 1),
        "per_page": min(max(int(per_page or DEFAULT_PER_PAGE), 25), 500),
    }
    if account_id:
        params["account_id"] = int(account_id)
    if q:
        params["q"] = q
    if quality:
        params["quality"] = quality
    return "/ga-search-terms?" + urlencode(params)


def _ga4_terms_export_url(*, account_id: Optional[int], q: str = "", quality: str = "") -> str:
    params: dict[str, Union[str, int]] = {}
    if account_id:
        params["account_id"] = int(account_id)
    if q:
        params["q"] = q
    if quality:
        params["quality"] = quality
    return "/ga-search-terms/export" + (("?" + urlencode(params)) if params else "")


def _candidate_order() -> list:
    return [
        desc(GoogleAnalyticsSearchTermCandidate.score),
        desc(GoogleAnalyticsSearchTermCandidate.purchases),
        desc(GoogleAnalyticsSearchTermCandidate.revenue),
        desc(GoogleAnalyticsSearchTermCandidate.engaged_sessions),
        desc(GoogleAnalyticsSearchTermCandidate.sessions),
        desc(GoogleAnalyticsSearchTermCandidate.last_seen_at),
        GoogleAnalyticsSearchTermCandidate.search_term,
    ]


def _candidate_filters(*, account_id: int, q: str = "", quality: str = "") -> list:
    filters = [GoogleAnalyticsSearchTermCandidate.account_id == account_id]
    q = str(q or "").strip()
    quality = str(quality or "").strip().lower()
    if q:
        filters.append(GoogleAnalyticsSearchTermCandidate.search_term.ilike(f"%{q}%"))
    if quality in QUALITY_LABELS:
        filters.append(GoogleAnalyticsSearchTermCandidate.quality_label == quality)
    return filters


async def _stream_ga4_search_terms_csv(
    *,
    account_id: int,
    q: str = "",
    quality: str = "",
    selected_ids: Optional[list[int]] = None,
) -> AsyncIterator[str]:
    yield ga4_search_terms_csv_chunk([], include_header=True)
    offset = 0
    selected_ids = [int(item) for item in (selected_ids or []) if int(item) > 0]
    async with AsyncSessionLocal() as stream_session:
        while True:
            filters = _candidate_filters(account_id=account_id, q=q, quality=quality)
            if selected_ids:
                filters.append(GoogleAnalyticsSearchTermCandidate.id.in_(selected_ids))
            rows = (
                await stream_session.scalars(
                    select(GoogleAnalyticsSearchTermCandidate)
                    .where(*filters)
                    .order_by(*_candidate_order())
                    .offset(offset)
                    .limit(CSV_EXPORT_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            yield ga4_search_terms_csv_chunk(rows)
            offset += len(rows)


@router.get("/ga-search-terms", response_class=HTMLResponse)
async def ga4_search_terms_page(
    request: Request,
    account_id: Optional[int] = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    q: str = "",
    quality: str = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or DEFAULT_PER_PAGE), 25), 500)
    q = str(q or "").strip()
    quality = str(quality or "").strip().lower()
    if quality not in QUALITY_LABELS:
        quality = ""

    summary_subq = (
        select(
            GoogleAnalyticsSearchTermCandidate.account_id.label("account_id"),
            func.count(GoogleAnalyticsSearchTermCandidate.id).label("term_count"),
            func.max(GoogleAnalyticsSearchTermCandidate.last_seen_at).label("last_seen_at"),
            func.max(GoogleAnalyticsSearchTermCandidate.last_pulled_at).label("last_pulled_at"),
        )
        .group_by(GoogleAnalyticsSearchTermCandidate.account_id)
        .subquery()
    )
    account_rows = (
        await session.execute(
            select(
                GoogleAdsAccount,
                summary_subq.c.term_count,
                summary_subq.c.last_seen_at,
                summary_subq.c.last_pulled_at,
            )
            .outerjoin(summary_subq, summary_subq.c.account_id == GoogleAdsAccount.id)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    account_summaries = [
        {
            "account": row[0],
            "term_count": int(row[1] or 0),
            "last_seen_at": row[2],
            "last_pulled_at": row[3],
            "url": _ga4_terms_url(account_id=row[0].id, per_page=per_page, q=q, quality=quality),
        }
        for row in account_rows
    ]
    selected_account = None
    selected_account_summary = None
    if account_summaries:
        selected_account_summary = next(
            (item for item in account_summaries if item["account"].id == account_id),
            account_summaries[0],
        )
        selected_account = selected_account_summary["account"]

    term_rows: list[GoogleAnalyticsSearchTermCandidate] = []
    copy_terms: list[str] = []
    quality_counts: dict[str, int] = {}
    total = 0
    total_pages = 1
    if selected_account is not None:
        filters = _candidate_filters(account_id=selected_account.id, q=q, quality=quality)
        total = int(await session.scalar(select(func.count(GoogleAnalyticsSearchTermCandidate.id)).where(*filters)) or 0)
        total_pages = max(math.ceil(total / per_page), 1)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        term_rows = (
            await session.scalars(
                select(GoogleAnalyticsSearchTermCandidate)
                .where(*filters)
                .order_by(*_candidate_order())
                .offset(offset)
                .limit(per_page)
            )
        ).all()
        copy_terms = list(
            (
                await session.scalars(
                    select(GoogleAnalyticsSearchTermCandidate.search_term)
                    .where(*filters)
                    .order_by(*_candidate_order())
                    .limit(MAX_COPY_TERMS)
                )
            ).all()
        )
        count_rows = (
            await session.execute(
                select(GoogleAnalyticsSearchTermCandidate.quality_label, func.count(GoogleAnalyticsSearchTermCandidate.id))
                .where(GoogleAnalyticsSearchTermCandidate.account_id == selected_account.id)
                .group_by(GoogleAnalyticsSearchTermCandidate.quality_label)
            )
        ).all()
        quality_counts = {str(row[0] or ""): int(row[1] or 0) for row in count_rows}

    visible_terms = [row.search_term for row in term_rows]
    return templates.TemplateResponse(
        "ga_search_terms.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "account_summaries": account_summaries,
            "selected_account": selected_account,
            "selected_account_summary": selected_account_summary,
            "term_rows": term_rows,
            "visible_terms": visible_terms,
            "copy_terms": copy_terms,
            "copy_limit": MAX_COPY_TERMS,
            "copy_truncated": total > MAX_COPY_TERMS,
            "quality_labels": QUALITY_LABELS,
            "quality_counts": quality_counts,
            "selected_quality": quality,
            "q": q,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "prev_url": _ga4_terms_url(
                account_id=selected_account.id if selected_account else None,
                page=page - 1,
                per_page=per_page,
                q=q,
                quality=quality,
            ) if page > 1 else "",
            "next_url": _ga4_terms_url(
                account_id=selected_account.id if selected_account else None,
                page=page + 1,
                per_page=per_page,
                q=q,
                quality=quality,
            ) if page < total_pages else "",
            "first_url": _ga4_terms_url(
                account_id=selected_account.id if selected_account else None,
                page=1,
                per_page=per_page,
                q=q,
                quality=quality,
            ),
            "last_url": _ga4_terms_url(
                account_id=selected_account.id if selected_account else None,
                page=total_pages,
                per_page=per_page,
                q=q,
                quality=quality,
            ),
            "export_filtered_url": _ga4_terms_export_url(
                account_id=selected_account.id if selected_account else None,
                q=q,
                quality=quality,
            ),
            "sync_job_id": request.query_params.get("sync_job_id", ""),
            "sync_scope": request.query_params.get("sync_scope", ""),
        },
    )


@router.get("/ga-search-terms/export")
async def export_ga4_search_terms_csv(
    account_id: int,
    q: str = "",
    quality: str = "",
    selected_ids: Optional[list[int]] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> StreamingResponse:
    q = str(q or "").strip()
    quality = str(quality or "").strip().lower()
    if quality not in QUALITY_LABELS:
        quality = ""
    account = await session.scalar(
        select(GoogleAdsAccount).where(
            GoogleAdsAccount.id == int(account_id),
            GoogleAdsAccount.is_active.is_(True),
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account not found.")
    selected_ids = [int(item) for item in (selected_ids or []) if int(item) > 0]
    filename = ga4_search_terms_csv_filename(account, selected=bool(selected_ids))
    response = StreamingResponse(
        _stream_ga4_search_terms_csv(
            account_id=account.id,
            q=q,
            quality=quality,
            selected_ids=selected_ids,
        ),
        media_type="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.post("/ga-search-terms/sync")
async def sync_ga4_search_terms(
    account_id: int = Form(0),
    account_ids: Optional[list[int]] = Form(None),
    sync_scope: str = Form("selected"),
    pull_mode: str = Form("recent"),
    days: int = Form(60),
    max_rows: int = Form(10000),
    force: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    days = min(max(int(days or 60), 1), 365)
    pull_mode = "all_time" if pull_mode == "all_time" else "recent"
    max_rows = min(max(int(max_rows or 10000), 50), 250000 if pull_mode == "all_time" else 50000)
    sync_all = sync_scope == "all"
    selected_ids: list[int] = []
    if not sync_all:
        selected_ids = [int(item) for item in (account_ids or []) if item]
        if not selected_ids and account_id:
            selected_ids = [int(account_id)]
        selected_ids = list(dict.fromkeys(selected_ids))
        active_ids = (
            await session.scalars(
                select(GoogleAdsAccount.id).where(
                    GoogleAdsAccount.id.in_(selected_ids),
                    GoogleAdsAccount.is_active.is_(True),
                )
            )
        ).all()
        selected_ids = [int(item) for item in active_ids]
        if not selected_ids:
            raise HTTPException(status_code=400, detail="Select at least one active Google Ads account.")
    selected_account = await session.get(GoogleAdsAccount, selected_ids[0]) if selected_ids else None
    label_target = (
        f"{len(selected_ids)} selected account{'s' if len(selected_ids) != 1 else ''}"
        if selected_ids
        else "all active mapped accounts"
    )
    job = await create_background_job(
        session,
        job_type="google_analytics_search_terms_sync",
        label=f"GA4 {'all-time' if pull_mode == 'all_time' else str(days) + 'd'} search-term pull: {label_target}",
        requested_by_id=user.id,
        payload={
            "account_ids": selected_ids or None,
            "mode": pull_mode,
            "days": days,
            "max_rows": max_rows,
            "force": force == "on",
        },
    )
    try:
        from app.tasks import sync_google_analytics_search_terms

        message = sync_google_analytics_search_terms.send(
            pull_mode,
            days,
            job.id,
            selected_ids or None,
            max_rows,
            force == "on",
        )
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    target_account_id = selected_account.id if selected_account is not None else int(account_id or 0)
    return RedirectResponse(
        f"/ga-search-terms?account_id={target_account_id}&sync_job_id={job.id}&sync_scope={sync_scope}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
