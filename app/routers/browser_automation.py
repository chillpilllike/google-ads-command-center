from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ROOT_DIR, get_settings
from app.database import get_session
from app.dependencies import require_user, templates
from app.models import GoogleAdsAccount, User
from app.services.google_ads_browser_automation import (
    browser_automation_counts,
    claim_next_browser_automation_task,
    generate_browser_automation_tasks,
    get_or_create_browser_automation_token,
    list_browser_automation_tasks,
    mark_browser_automation_task_result,
    rotate_browser_automation_token,
    serialize_browser_task,
    verify_browser_automation_token,
)


router = APIRouter()
settings = get_settings()


def _public_base_url(request: Request) -> str:
    configured = str(settings.public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


async def _require_extension_token(
    session: AsyncSession,
    authorization: Optional[str],
    x_automation_token: Optional[str],
    token: Optional[str],
) -> None:
    candidate = ""
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
    candidate = candidate or str(x_automation_token or token or "").strip()
    ok = await session.run_sync(lambda sync_session: verify_browser_automation_token(sync_session, candidate))
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid browser automation token.")


@router.get("/browser-automation")
async def browser_automation_page(
    request: Request,
    account_id: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
):
    accounts = list(
        (
            await session.scalars(
                select(GoogleAdsAccount)
                .where(GoogleAdsAccount.is_active.is_(True))
                .order_by(GoogleAdsAccount.name.asc(), GoogleAdsAccount.customer_id.asc())
            )
        ).all()
    )
    selected = None
    if account_id:
        selected = await session.get(GoogleAdsAccount, int(account_id))
    if selected is None and accounts:
        selected = accounts[0]
    token = await session.run_sync(get_or_create_browser_automation_token)
    counts = await session.run_sync(lambda sync_session: browser_automation_counts(sync_session, selected.id if selected else None))
    tasks = await session.run_sync(
        lambda sync_session: list_browser_automation_tasks(sync_session, selected.id if selected else None, limit=250)
    )
    return templates.TemplateResponse(
        "browser_automation.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": user,
            "accounts": accounts,
            "selected_account": selected,
            "counts": counts,
            "tasks": tasks,
            "extension_token": token,
            "public_base_url": _public_base_url(request),
        },
    )


@router.post("/browser-automation/generate")
async def generate_browser_queue(
    account_id: int = Form(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account was not found.")
    result = await session.run_sync(lambda sync_session: generate_browser_automation_tasks(sync_session, account))
    await session.commit()
    return RedirectResponse(
        f"/browser-automation?account_id={account.id}&created={result.get('created', 0)}&updated={result.get('updated', 0)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/browser-automation/token/rotate")
async def rotate_browser_token(
    account_id: Optional[int] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    await session.run_sync(rotate_browser_automation_token)
    await session.commit()
    suffix = f"?account_id={int(account_id)}&token_rotated=1" if account_id else "?token_rotated=1"
    return RedirectResponse(f"/browser-automation{suffix}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/browser-automation/extension-config")
async def browser_extension_config(
    request: Request,
    account_id: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> dict[str, Any]:
    token = await session.run_sync(get_or_create_browser_automation_token)
    return {
        "appBaseUrl": _public_base_url(request),
        "token": token,
        "accountId": int(account_id) if account_id else None,
        "pollMs": 3500,
    }


@router.get("/browser-automation/extension.zip")
async def browser_extension_zip(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> Response:
    extension_dir = ROOT_DIR / "browser_extension" / "google_ads_command_center"
    if not extension_dir.exists():
        raise HTTPException(status_code=404, detail="Extension files were not found.")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(extension_dir.rglob("*")):
            if path.is_file():
                archive.write(path, Path("google_ads_command_center") / path.relative_to(extension_dir))
    return Response(
        payload.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=google-ads-command-center-extension.zip"},
    )


@router.get("/api/browser-automation/status")
async def extension_status(
    account_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
    x_automation_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_extension_token(session, authorization, x_automation_token, token)
    counts = await session.run_sync(lambda sync_session: browser_automation_counts(sync_session, account_id))
    return {"ok": True, "counts": counts}


@router.get("/api/browser-automation/next")
async def extension_next_task(
    worker_id: str = Query("browser-worker"),
    account_id: Optional[int] = None,
    authorization: Optional[str] = Header(None),
    x_automation_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_extension_token(session, authorization, x_automation_token, token)
    task = await session.run_sync(
        lambda sync_session: claim_next_browser_automation_task(sync_session, worker_id=worker_id, account_id=account_id)
    )
    await session.commit()
    if task is None:
        return {"ok": True, "task": None}
    return {"ok": True, "task": serialize_browser_task(task)}


@router.post("/api/browser-automation/tasks/{task_id}/result")
async def extension_task_result(
    task_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_automation_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _require_extension_token(session, authorization, x_automation_token, token)
    payload = await request.json()
    worker_id = str(payload.get("worker_id") or payload.get("workerId") or "browser-worker")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    status_value = str(payload.get("status") or "failed")
    try:
        task = await session.run_sync(
            lambda sync_session: mark_browser_automation_task_result(
                sync_session,
                task_id,
                status=status_value,
                worker_id=worker_id,
                result=result,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True, "task": serialize_browser_task(task)}
