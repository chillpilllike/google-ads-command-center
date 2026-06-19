from __future__ import annotations

import io
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user
from app.models import GoogleAdsAccount, User
from app.services.google_ads_editor_export import (
    build_google_ads_editor_dynamic_search_bundle,
    build_google_ads_editor_export_bundle,
)
from app.services.google_ads_manual_export import build_manual_automation_export, confirm_manual_automation_upload


router = APIRouter()


@router.get("/automation/google-ads-editor-export")
async def download_google_ads_editor_export(
    account_id: int,
    kind: str = "full",
    file: str = "zip",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> Response:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account was not found.")

    normalized_kind = str(kind or "full").strip().lower()
    builder = build_google_ads_editor_dynamic_search_bundle if normalized_kind in {"dynamic", "dynamic_search", "ai_max"} else build_google_ads_editor_export_bundle
    bundle = await session.run_sync(
        lambda sync_session: builder(sync_session, sync_session.get(GoogleAdsAccount, int(account_id)))
    )
    normalized_file = str(file or "zip").strip().lower()
    if normalized_file in {"main", "single", "csv", "tsv"}:
        main_file = str(bundle.manifest.get("main_file") or "google_ads_editor_import.tsv")
        with zipfile.ZipFile(io.BytesIO(bundle.payload), "r") as archive:
            payload = archive.read(main_file)
        filename = quote(bundle.filename.rsplit(".", 1)[0] + "-" + main_file)
        return Response(
            content=payload,
            media_type="text/tab-separated-values; charset=utf-16",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
            },
        )
    filename = quote(bundle.filename)
    return Response(
        content=bundle.payload,
        media_type=bundle.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/automation/manual-export")
async def download_manual_automation_export(
    account_id: int,
    kind: str = "keywords",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> Response:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account was not found.")

    try:
        export_file = await session.run_sync(
            lambda sync_session: build_manual_automation_export(
                sync_session,
                sync_session.get(GoogleAdsAccount, int(account_id)),
                kind=kind,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = quote(export_file.filename)
    return Response(
        content=export_file.content,
        media_type=export_file.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/automation/manual-upload-confirm")
async def confirm_manual_upload(
    account_id: int = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None:
        raise HTTPException(status_code=404, detail="Google Ads account was not found.")
    result = await session.run_sync(
        lambda sync_session: confirm_manual_automation_upload(
            sync_session,
            sync_session.get(GoogleAdsAccount, int(account_id)),
            user_id=user.id,
            note=note,
        )
    )
    await session.commit()
    return RedirectResponse(
        f"/automation?account_id={account.id}&manual_confirmed={int(result.get('draft_count') or 0)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
