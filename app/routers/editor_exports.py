from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user
from app.models import GoogleAdsAccount, User
from app.services.google_ads_editor_export import (
    build_google_ads_editor_dynamic_search_bundle,
    build_google_ads_editor_export_bundle,
)


router = APIRouter()


@router.get("/automation/google-ads-editor-export")
async def download_google_ads_editor_export(
    account_id: int,
    kind: str = "full",
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
    filename = quote(bundle.filename)
    return Response(
        content=bundle.payload,
        media_type=bundle.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )
