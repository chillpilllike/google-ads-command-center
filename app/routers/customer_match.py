from __future__ import annotations

import base64
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_session
from app.dependencies import require_user, settings, templates
from app.models import (
    BackgroundJob,
    GoogleAdsCustomerMatchPublication,
    OdooCustomerMatchMember,
    User,
)
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.customer_match import (
    customer_match_csv_chunk,
    customer_match_feed_token,
    customer_match_filename,
    customer_match_summary,
    ensure_customer_match_publications,
    public_customer_match_url,
)


router = APIRouter()
CSV_BATCH_SIZE = 1000


def _basic_credentials(request: Request) -> tuple[str, str]:
    header = str(request.headers.get("authorization") or "")
    if not header.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return "", ""
    username, _, password = decoded.partition(":")
    return username, password


def _authorized_public_feed(request: Request, publication: GoogleAdsCustomerMatchPublication) -> bool:
    token = str(request.query_params.get("token") or "").strip()
    if token and token == customer_match_feed_token(publication):
        return True
    username, password = _basic_credentials(request)
    return bool(
        publication.access_username
        and publication.access_password
        and username == publication.access_username
        and password == publication.access_password
    )


async def _stream_customer_match_csv(publication_id: int) -> AsyncIterator[str]:
    yield customer_match_csv_chunk([], include_header=True)
    offset = 0
    async with AsyncSessionLocal() as stream_session:
        publication = await stream_session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
        if publication is None:
            return
        while True:
            rows = (
                await stream_session.scalars(
                    select(OdooCustomerMatchMember)
                    .where(
                        OdooCustomerMatchMember.account_id == publication.account_id,
                        OdooCustomerMatchMember.store_id == publication.store_id,
                        OdooCustomerMatchMember.website_id == publication.website_id,
                    )
                    .order_by(desc(OdooCustomerMatchMember.last_order_at), desc(OdooCustomerMatchMember.id))
                    .offset(offset)
                    .limit(CSV_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            yield customer_match_csv_chunk(rows)
            offset += len(rows)


@router.get("/customer-match", response_class=HTMLResponse)
async def customer_match_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    await session.run_sync(lambda sync_session: ensure_customer_match_publications(sync_session))
    publications = (
        await session.scalars(
            select(GoogleAdsCustomerMatchPublication)
            .order_by(
                GoogleAdsCustomerMatchPublication.store_id,
                GoogleAdsCustomerMatchPublication.website_id,
                GoogleAdsCustomerMatchPublication.account_id,
            )
        )
    ).all()
    summaries = {}
    for publication in publications:
        summaries[publication.id] = await session.run_sync(
            lambda sync_session, publication_id=publication.id: customer_match_summary(
                sync_session,
                sync_session.get(GoogleAdsCustomerMatchPublication, publication_id),
            )
        )
    recent_jobs = (
        await session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.job_type == "customer_match_sync")
            .order_by(desc(BackgroundJob.created_at), desc(BackgroundJob.id))
            .limit(8)
        )
    ).all()
    return templates.TemplateResponse(
        "customer_match.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "publications": publications,
            "summaries": summaries,
            "recent_jobs": recent_jobs,
            "public_customer_match_url": public_customer_match_url,
            "saved": request.query_params.get("saved") == "1",
            "job_id": request.query_params.get("job_id", ""),
        },
    )


@router.post("/customer-match/publications/{publication_id}/policy")
async def update_customer_match_policy(
    publication_id: int,
    customer_match_policy_accepted: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    publication = await session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
    if publication is None:
        raise HTTPException(status_code=404, detail="Customer Match publication not found.")
    publication.customer_match_policy_accepted = customer_match_policy_accepted == "on"
    if publication.website_id == 0:
        publication.status = "blocked_all_websites"
    elif publication.customer_match_policy_accepted and publication.status in {"planned", "blocked_policy"}:
        publication.status = "ready"
    await session.commit()
    return RedirectResponse("/customer-match?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/customer-match/sync")
async def queue_customer_match_sync(
    publication_id: int = Form(...),
    lookback_days: int = Form(540),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    publication = await session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
    if publication is None:
        raise HTTPException(status_code=404, detail="Customer Match publication not found.")
    lookback_days = min(max(int(lookback_days or 540), 1), 540)
    job = await create_background_job(
        session,
        job_type="customer_match_sync",
        label=f"Customer Match sync: {publication.list_name or publication.website_name}",
        requested_by_id=user.id,
        payload={
            "publication_id": publication.id,
            "account_id": publication.account_id,
            "store_id": publication.store_id,
            "website_id": publication.website_id,
            "lookback_days": lookback_days,
        },
    )
    try:
        from app.tasks import sync_customer_match_publication

        message = sync_customer_match_publication.send(publication.id, lookback_days, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(f"/customer-match?job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/customer-match/feeds/{publication_id}.csv")
async def customer_match_feed(
    publication_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    publication = await session.get(GoogleAdsCustomerMatchPublication, int(publication_id))
    if publication is None:
        raise HTTPException(status_code=404, detail="Customer Match feed not found.")
    if not _authorized_public_feed(request, publication):
        raise HTTPException(
            status_code=401,
            detail="Customer Match feed authentication required.",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not publication.customer_match_policy_accepted or int(publication.website_id or 0) == 0:
        raise HTTPException(status_code=403, detail="Customer Match feed is not enabled for this website mapping.")
    filename = customer_match_filename(publication)
    response = StreamingResponse(
        _stream_customer_match_csv(publication.id),
        media_type="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response
