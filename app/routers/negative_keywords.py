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
from app.models import GoogleAdsAccount, GoogleAdsNegativeKeywordCandidate, User
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_negative_keyword_bank import negative_match_text
from app.services.google_ads_negative_keyword_csv import negative_keyword_bank_csv_filename, negative_keyword_candidates_csv_chunk


router = APIRouter()

REASON_LABELS = {
    "zero_conversion_high_waste": "High waste, no conversion",
    "zero_conversion_click_waste": "Clicks + cost, no conversion",
    "low_ctr_no_conversion": "Low CTR, no conversion",
    "employment_intent": "Employment intent",
    "research_only_intent": "Research-only intent",
    "medical_info_intent": "Medical info intent",
    "free_discount_intent": "Free/discount intent",
    "marketplace_competitor_intent": "Marketplace competitor",
}
STATUS_LABELS = {
    "new": "New",
    "approved": "Approved",
    "rejected": "Rejected",
    "applied": "Applied",
}
DEFAULT_PER_PAGE = 100
MAX_COPY_TERMS = 5000
CSV_EXPORT_BATCH_SIZE = 1000


def _negative_keyword_url(
    *,
    account_id: Optional[int],
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    q: str = "",
    reason: str = "",
    review_status: str = "",
) -> str:
    params: dict[str, Union[str, int]] = {
        "page": max(int(page or 1), 1),
        "per_page": min(max(int(per_page or DEFAULT_PER_PAGE), 25), 500),
    }
    if account_id:
        params["account_id"] = int(account_id)
    if q:
        params["q"] = q
    if reason:
        params["reason"] = reason
    if review_status:
        params["review_status"] = review_status
    return "/negative-keywords?" + urlencode(params)


def _negative_keyword_export_url(
    *,
    account_id: Optional[int],
    q: str = "",
    reason: str = "",
    review_status: str = "",
) -> str:
    params: dict[str, Union[str, int]] = {}
    if account_id:
        params["account_id"] = int(account_id)
    if q:
        params["q"] = q
    if reason:
        params["reason"] = reason
    if review_status:
        params["review_status"] = review_status
    return "/negative-keywords/export" + (("?" + urlencode(params)) if params else "")


def _candidate_order() -> list:
    return [
        desc(GoogleAdsNegativeKeywordCandidate.confidence),
        desc(GoogleAdsNegativeKeywordCandidate.score),
        desc(GoogleAdsNegativeKeywordCandidate.cost),
        desc(GoogleAdsNegativeKeywordCandidate.clicks),
        desc(GoogleAdsNegativeKeywordCandidate.last_seen_at),
        GoogleAdsNegativeKeywordCandidate.keyword,
    ]


def _candidate_filters(
    *,
    account_id: int,
    q: str = "",
    reason: str = "",
    review_status: str = "",
) -> list:
    filters = [GoogleAdsNegativeKeywordCandidate.account_id == account_id]
    q = str(q or "").strip()
    reason = str(reason or "").strip().lower()
    review_status = str(review_status or "").strip().lower()
    if q:
        filters.append(GoogleAdsNegativeKeywordCandidate.keyword.ilike(f"%{q}%"))
    if reason in REASON_LABELS:
        filters.append(GoogleAdsNegativeKeywordCandidate.reason_label == reason)
    if review_status in STATUS_LABELS:
        filters.append(GoogleAdsNegativeKeywordCandidate.review_status == review_status)
    return filters


async def _stream_negative_keyword_candidates_csv(
    *,
    account_id: int,
    q: str = "",
    reason: str = "",
    review_status: str = "",
    selected_ids: Optional[list[int]] = None,
) -> AsyncIterator[str]:
    yield negative_keyword_candidates_csv_chunk([], include_header=True)
    offset = 0
    selected_ids = [int(item) for item in (selected_ids or []) if int(item) > 0]
    async with AsyncSessionLocal() as stream_session:
        while True:
            filters = _candidate_filters(account_id=account_id, q=q, reason=reason, review_status=review_status)
            if selected_ids:
                filters.append(GoogleAdsNegativeKeywordCandidate.id.in_(selected_ids))
            rows = (
                await stream_session.scalars(
                    select(GoogleAdsNegativeKeywordCandidate)
                    .where(*filters)
                    .order_by(*_candidate_order())
                    .offset(offset)
                    .limit(CSV_EXPORT_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            yield negative_keyword_candidates_csv_chunk(rows)
            offset += len(rows)


@router.get("/negative-keywords", response_class=HTMLResponse)
async def negative_keyword_bank_page(
    request: Request,
    account_id: Optional[int] = None,
    page: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
    q: str = "",
    reason: str = "",
    review_status: str = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or DEFAULT_PER_PAGE), 25), 500)
    q = str(q or "").strip()
    reason = str(reason or "").strip().lower()
    review_status = str(review_status or "").strip().lower()
    if reason not in REASON_LABELS:
        reason = ""
    if review_status not in STATUS_LABELS:
        review_status = ""

    summary_subq = (
        select(
            GoogleAdsNegativeKeywordCandidate.account_id.label("account_id"),
            func.count(GoogleAdsNegativeKeywordCandidate.id).label("negative_count"),
            func.max(GoogleAdsNegativeKeywordCandidate.last_seen_at).label("last_seen_at"),
            func.max(GoogleAdsNegativeKeywordCandidate.last_pulled_at).label("last_pulled_at"),
        )
        .group_by(GoogleAdsNegativeKeywordCandidate.account_id)
        .subquery()
    )
    account_rows = (
        await session.execute(
            select(
                GoogleAdsAccount,
                summary_subq.c.negative_count,
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
            "negative_count": int(row[1] or 0),
            "last_seen_at": row[2],
            "last_pulled_at": row[3],
            "url": _negative_keyword_url(
                account_id=row[0].id,
                per_page=per_page,
                q=q,
                reason=reason,
                review_status=review_status,
            ),
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

    page_rows: list[GoogleAdsNegativeKeywordCandidate] = []
    copy_terms: list[str] = []
    reason_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    total = 0
    total_pages = 1
    if selected_account is not None:
        filters = _candidate_filters(account_id=selected_account.id, q=q, reason=reason, review_status=review_status)
        total = int(await session.scalar(select(func.count(GoogleAdsNegativeKeywordCandidate.id)).where(*filters)) or 0)
        total_pages = max(math.ceil(total / per_page), 1)
        page = min(page, total_pages)
        offset = (page - 1) * per_page
        page_rows = (
            await session.scalars(
                select(GoogleAdsNegativeKeywordCandidate)
                .where(*filters)
                .order_by(*_candidate_order())
                .offset(offset)
                .limit(per_page)
            )
        ).all()
        copy_rows = (
            await session.scalars(
                select(GoogleAdsNegativeKeywordCandidate)
                .where(*filters)
                .order_by(*_candidate_order())
                .limit(MAX_COPY_TERMS)
            )
        ).all()
        copy_terms = [negative_match_text(row.keyword, row.match_type) for row in copy_rows]
        reason_rows = (
            await session.execute(
                select(GoogleAdsNegativeKeywordCandidate.reason_label, func.count(GoogleAdsNegativeKeywordCandidate.id))
                .where(GoogleAdsNegativeKeywordCandidate.account_id == selected_account.id)
                .group_by(GoogleAdsNegativeKeywordCandidate.reason_label)
            )
        ).all()
        reason_counts = {str(row[0] or ""): int(row[1] or 0) for row in reason_rows}
        status_rows = (
            await session.execute(
                select(GoogleAdsNegativeKeywordCandidate.review_status, func.count(GoogleAdsNegativeKeywordCandidate.id))
                .where(GoogleAdsNegativeKeywordCandidate.account_id == selected_account.id)
                .group_by(GoogleAdsNegativeKeywordCandidate.review_status)
            )
        ).all()
        status_counts = {str(row[0] or ""): int(row[1] or 0) for row in status_rows}

    visible_terms = [negative_match_text(row.keyword, row.match_type) for row in page_rows]
    return templates.TemplateResponse(
        "negative_keywords.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "account_summaries": account_summaries,
            "selected_account": selected_account,
            "selected_account_summary": selected_account_summary,
            "page_rows": page_rows,
            "visible_terms": visible_terms,
            "copy_terms": copy_terms,
            "copy_limit": MAX_COPY_TERMS,
            "copy_truncated": total > MAX_COPY_TERMS,
            "reason_labels": REASON_LABELS,
            "reason_counts": reason_counts,
            "selected_reason": reason,
            "status_labels": STATUS_LABELS,
            "status_counts": status_counts,
            "selected_status": review_status,
            "q": q,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "prev_url": _negative_keyword_url(
                account_id=selected_account.id if selected_account else None,
                page=page - 1,
                per_page=per_page,
                q=q,
                reason=reason,
                review_status=review_status,
            ) if page > 1 else "",
            "next_url": _negative_keyword_url(
                account_id=selected_account.id if selected_account else None,
                page=page + 1,
                per_page=per_page,
                q=q,
                reason=reason,
                review_status=review_status,
            ) if page < total_pages else "",
            "first_url": _negative_keyword_url(
                account_id=selected_account.id if selected_account else None,
                page=1,
                per_page=per_page,
                q=q,
                reason=reason,
                review_status=review_status,
            ),
            "last_url": _negative_keyword_url(
                account_id=selected_account.id if selected_account else None,
                page=total_pages,
                per_page=per_page,
                q=q,
                reason=reason,
                review_status=review_status,
            ),
            "export_filtered_url": _negative_keyword_export_url(
                account_id=selected_account.id if selected_account else None,
                q=q,
                reason=reason,
                review_status=review_status,
            ),
            "sync_job_id": request.query_params.get("sync_job_id", ""),
            "sync_scope": request.query_params.get("sync_scope", ""),
        },
    )


@router.get("/negative-keywords/export")
async def export_negative_keyword_bank_csv(
    account_id: int,
    q: str = "",
    reason: str = "",
    review_status: str = "",
    selected_ids: Optional[list[int]] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> StreamingResponse:
    q = str(q or "").strip()
    reason = str(reason or "").strip().lower()
    review_status = str(review_status or "").strip().lower()
    if reason not in REASON_LABELS:
        reason = ""
    if review_status not in STATUS_LABELS:
        review_status = ""
    account = await session.scalar(
        select(GoogleAdsAccount).where(
            GoogleAdsAccount.id == int(account_id),
            GoogleAdsAccount.is_active.is_(True),
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account not found.")
    selected_ids = [int(item) for item in (selected_ids or []) if int(item) > 0]
    filename = negative_keyword_bank_csv_filename(account, selected=bool(selected_ids))
    response = StreamingResponse(
        _stream_negative_keyword_candidates_csv(
            account_id=account.id,
            q=q,
            reason=reason,
            review_status=review_status,
            selected_ids=selected_ids,
        ),
        media_type="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.post("/negative-keywords/sync")
async def sync_negative_keyword_bank(
    account_id: int = Form(0),
    account_ids: Optional[list[int]] = Form(None),
    sync_scope: str = Form("selected"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    sync_all = sync_scope == "all"
    selected_ids: list[int] = []
    if not sync_all:
        selected_ids = [int(item) for item in (account_ids or []) if item]
        if not selected_ids and account_id:
            selected_ids = [int(account_id)]
        selected_ids = list(dict.fromkeys(selected_ids))
        active_ids = (
            await session.scalars(
                select(GoogleAdsAccount.id)
                .where(
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
        else "all active accounts"
    )
    job = await create_background_job(
        session,
        job_type="google_ads_negative_keyword_candidate_sync",
        label=f"Negative keyword candidate refresh from saved snapshots: {label_target}",
        requested_by_id=user.id,
        payload={
            "account_ids": selected_ids or None,
            "source": "saved_search_terms_snapshots",
        },
    )
    try:
        from app.tasks import sync_google_ads_negative_keyword_candidates

        message = sync_google_ads_negative_keyword_candidates.send(job.id, selected_ids or None)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    target_account_id = selected_account.id if selected_account is not None else int(account_id or 0)
    return RedirectResponse(
        f"/negative-keywords?account_id={target_account_id}&sync_job_id={job.id}&sync_scope={sync_scope}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
