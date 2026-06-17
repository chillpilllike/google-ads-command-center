from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackgroundJob, BackgroundJobStatus


TERMINAL_STATUSES = {
    BackgroundJobStatus.succeeded,
    BackgroundJobStatus.failed,
    BackgroundJobStatus.canceled,
}


async def create_background_job(
    session: AsyncSession,
    *,
    job_type: str,
    label: str,
    requested_by_id: int | None,
    payload: dict[str, Any] | None = None,
) -> BackgroundJob:
    job = BackgroundJob(
        job_type=job_type,
        label=label,
        requested_by_id=requested_by_id,
        payload=payload or {},
        status=BackgroundJobStatus.queued,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def save_job_message_id(session: AsyncSession, job: BackgroundJob, message_id: str) -> None:
    job.message_id = message_id
    await session.commit()


async def mark_job_dispatch_failed(session: AsyncSession, job: BackgroundJob, error: str) -> None:
    job.status = BackgroundJobStatus.failed
    job.error = error
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()


async def request_background_job_cancel(session: AsyncSession, job: BackgroundJob) -> None:
    if job.status in TERMINAL_STATUSES:
        return
    job.cancel_requested = True
    if job.status == BackgroundJobStatus.queued:
        job.status = BackgroundJobStatus.canceled
        job.finished_at = datetime.now(timezone.utc)
    else:
        job.status = BackgroundJobStatus.cancel_requested
    await session.commit()
