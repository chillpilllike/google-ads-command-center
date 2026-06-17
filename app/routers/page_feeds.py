from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import (
    AppSetting,
    GoogleAdsAccount,
    GoogleAdsPageFeedPublication,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
    User,
)
from app.services.google_ads_page_feed_csv import (
    DEFAULT_MAX_URLS,
    PAGE_FEED_KINDS,
    feed_kind_label,
    normalize_feed_kind,
    normalize_max_urls,
    normalize_public_base_url,
    prepare_public_feed_for_mapping,
    public_feed_filename,
    public_feed_name,
    public_feed_url,
)


router = APIRouter()


def _optional_int(value: Optional[str]) -> Optional[int]:
    text = str(value or "").strip()
    return int(text) if text else None


async def _page_feed_public_base_url(session: AsyncSession, request: Request) -> str:
    saved_url = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == "page_feed.public_base_url")
    )
    base_url = normalize_public_base_url(str(saved_url or ""))
    if base_url:
        return base_url
    return str(request.base_url).rstrip("/")


async def _page_feed_default_max_urls(session: AsyncSession) -> int:
    saved_limit = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == "page_feed.default_max_urls")
    )
    return normalize_max_urls(saved_limit, DEFAULT_MAX_URLS)


async def _filters_context(session: AsyncSession) -> dict[str, object]:
    stores = (
        await session.scalars(
            select(OdooStore)
            .where(OdooStore.is_active.is_(True))
            .order_by(OdooStore.name)
        )
    ).all()
    websites = (
        await session.scalars(
            select(OdooWebsite)
            .where(OdooWebsite.is_active.is_(True))
            .order_by(OdooWebsite.store_id, OdooWebsite.name)
        )
    ).all()
    accounts = (
        await session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    return {"stores": stores, "websites": websites, "accounts": accounts}


@router.get("/page-feeds", response_class=HTMLResponse)
async def page_feeds_page(
    request: Request,
    store_id: Optional[str] = None,
    account_id: Optional[str] = None,
    website_id: Optional[str] = None,
    feed_kind: str = "",
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    selected_store_id = _optional_int(store_id)
    selected_account_id = _optional_int(account_id)
    selected_website_id = _optional_int(website_id)
    selected_feed_kind = normalize_feed_kind(feed_kind) if feed_kind else ""
    query = select(GoogleAdsPageFeedPublication)
    if selected_store_id:
        query = query.where(GoogleAdsPageFeedPublication.store_id == selected_store_id)
    if selected_account_id:
        query = query.where(GoogleAdsPageFeedPublication.account_id == selected_account_id)
    if selected_website_id is not None:
        query = query.where(GoogleAdsPageFeedPublication.website_id == selected_website_id)
    if selected_feed_kind:
        query = query.where(GoogleAdsPageFeedPublication.feed_kind == selected_feed_kind)
    publications = (
        await session.scalars(
            query.order_by(
                GoogleAdsPageFeedPublication.updated_at.desc(),
                GoogleAdsPageFeedPublication.id.desc(),
            ).limit(250)
        )
    ).all()
    public_base_url = await _page_feed_public_base_url(session, request)
    rows = []
    for publication in publications:
        result = dict(publication.last_publish_json or {})
        feed_kind_value = normalize_feed_kind(publication.feed_kind)
        result.setdefault("feed_name", public_feed_name(publication))
        result.setdefault("filename", public_feed_filename(publication))
        result.setdefault("feed_kind", feed_kind_value)
        result.setdefault("feed_kind_label", feed_kind_label(feed_kind_value))
        result["feed_url"] = public_feed_url(publication, public_base_url)
        rows.append({"publication": publication, "result": result})
    context = await _filters_context(session)
    return templates.TemplateResponse(
        "page_feeds.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "page_feed_rows": rows,
            "page_feed_base_url": public_base_url,
            "page_feed_default_max_urls": await _page_feed_default_max_urls(session),
            "feed_kinds": PAGE_FEED_KINDS,
            "selected_store_id": selected_store_id or "",
            "selected_account_id": selected_account_id or "",
            "selected_website_id": "" if selected_website_id is None else selected_website_id,
            "selected_feed_kind": selected_feed_kind,
            "generated": request.query_params.get("generated") == "1",
            "generated_count": request.query_params.get("generated_count", ""),
            "no_mappings": request.query_params.get("no_mappings") == "1",
            **context,
        },
    )


@router.post("/page-feeds/generate")
async def generate_page_feeds(
    request: Request,
    store_id: str = Form(""),
    account_id: str = Form(""),
    website_id: str = Form(""),
    feed_kinds: list[str] = Form(default=[]),
    max_urls: int = Form(DEFAULT_MAX_URLS),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    selected_store_id = _optional_int(store_id)
    selected_account_id = _optional_int(account_id)
    selected_website_id = _optional_int(website_id)
    selected_feed_kinds = [
        normalize_feed_kind(item)
        for item in (feed_kinds or [])
        if str(item or "").strip()
    ]
    selected_feed_kinds = list(dict.fromkeys(selected_feed_kinds)) or ["best"]
    max_urls = normalize_max_urls(max_urls, await _page_feed_default_max_urls(session))
    filters = [OdooStoreGoogleAdsMapping.is_active.is_(True)]
    if selected_store_id:
        filters.append(OdooStoreGoogleAdsMapping.store_id == selected_store_id)
    if selected_account_id:
        filters.append(OdooStoreGoogleAdsMapping.account_id == selected_account_id)
    if selected_website_id is not None:
        filters.append(OdooStoreGoogleAdsMapping.website_id == selected_website_id)
    mappings = (
        await session.scalars(
            select(OdooStoreGoogleAdsMapping)
            .where(*filters)
            .order_by(
                OdooStoreGoogleAdsMapping.store_id,
                OdooStoreGoogleAdsMapping.website_id,
                OdooStoreGoogleAdsMapping.account_id,
            )
        )
    ).all()
    if not mappings:
        return RedirectResponse("/page-feeds?no_mappings=1", status_code=status.HTTP_303_SEE_OTHER)
    public_base_url = await _page_feed_public_base_url(session, request)
    generated_count = 0
    for mapping in mappings:
        for kind in selected_feed_kinds:
            await prepare_public_feed_for_mapping(
                session,
                mapping,
                public_base_url=public_base_url,
                max_urls=max_urls,
                feed_kind=kind,
            )
            generated_count += 1
    await session.commit()
    params = [f"generated=1", f"generated_count={generated_count}"]
    if selected_store_id:
        params.append(f"store_id={selected_store_id}")
    if selected_account_id:
        params.append(f"account_id={selected_account_id}")
    if selected_website_id is not None:
        params.append(f"website_id={selected_website_id}")
    return RedirectResponse(
        "/page-feeds?" + "&".join(params),
        status_code=status.HTTP_303_SEE_OTHER,
    )
