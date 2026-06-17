from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import (
    AppSetting,
    BackgroundJob,
    GoogleAdsAccount,
    GoogleAdsPageFeedPublication,
    OdooProductPageSignal,
    OdooSaleOrder,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
    User,
)
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_page_feed_csv import (
    DEFAULT_MAX_URLS,
    accepted_public_feed_tokens,
    normalize_max_urls,
    normalize_public_base_url,
    page_feed_csv_text_for_kind,
    prepare_public_feed_for_mapping,
    public_feed_filename,
    public_feed_name,
    public_feed_url,
    select_public_feed_signals,
)


router = APIRouter()


def _none_int(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    return int(text)


def _none_float(value: str | None, default: float = 1.0) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    return float(text)


async def _queue_odoo_sync(
    *,
    session: AsyncSession,
    user: User,
    store_ids: list[int] | None,
    hours: int,
    label: str,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="odoo_sales_sync",
        label=label,
        requested_by_id=user.id,
        payload={"store_ids": store_ids or [], "hours": hours},
    )
    try:
        from app.tasks import sync_odoo_store_sales

        message = sync_odoo_store_sales.send(store_ids, hours, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - operator needs to see failed dispatch.
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


async def _queue_odoo_website_sync(
    *,
    session: AsyncSession,
    user: User,
    store_ids: list[int] | None,
    label: str,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="odoo_website_sync",
        label=label,
        requested_by_id=user.id,
        payload={"store_ids": store_ids or []},
    )
    try:
        from app.tasks import sync_odoo_store_websites

        message = sync_odoo_store_websites.send(store_ids, job.id)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - operator needs to see failed dispatch.
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


async def _queue_odoo_product_feed_sync(
    *,
    session: AsyncSession,
    user: User,
    store_ids: list[int] | None,
    days: int,
    label: str,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="odoo_product_feed_sync",
        label=label,
        requested_by_id=user.id,
        payload={"store_ids": store_ids or [], "days": days},
    )
    try:
        from app.tasks import sync_odoo_product_page_feeds

        message = sync_odoo_product_page_feeds.send(days, job.id, store_ids, None, None)
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - operator needs to see failed dispatch.
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


async def _queue_google_page_feed_publish(
    *,
    session: AsyncSession,
    user: User,
    store_ids: list[int] | None,
    account_ids: list[int] | None,
    website_ids: list[int] | None,
    validate_only: Optional[bool],
    max_urls: int,
    create_dsa_criteria: bool,
    label: str,
) -> BackgroundJob:
    job = await create_background_job(
        session,
        job_type="google_ads_page_feed_publish",
        label=label,
        requested_by_id=user.id,
        payload={
            "store_ids": store_ids or [],
            "account_ids": account_ids or [],
            "website_ids": website_ids or [],
            "validate_only": validate_only,
            "max_urls": max_urls,
            "create_dsa_criteria": create_dsa_criteria,
        },
    )
    try:
        from app.tasks import publish_google_ads_page_feeds

        message = publish_google_ads_page_feeds.send(
            job.id,
            store_ids,
            account_ids,
            website_ids,
            validate_only,
            max_urls,
            create_dsa_criteria,
        )
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001 - operator needs to see failed dispatch.
        await mark_job_dispatch_failed(session, job, str(exc))
    return job


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


@router.get("/stores", response_class=HTMLResponse)
async def stores_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
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
    mappings = (
        await session.scalars(
            select(OdooStoreGoogleAdsMapping)
            .where(OdooStoreGoogleAdsMapping.is_active.is_(True))
            .order_by(
                OdooStoreGoogleAdsMapping.store_id,
                OdooStoreGoogleAdsMapping.website_id,
                OdooStoreGoogleAdsMapping.account_id,
            )
        )
    ).all()
    website_lookup = {(website.store_id, website.website_id): website for website in websites}
    mapping_rows = [
        {
            "id": mapping.id,
            "store": mapping.store,
            "account": mapping.account,
            "website_id": mapping.website_id or 0,
            "website": website_lookup.get((mapping.store_id, mapping.website_id or 0)),
            "revenue_weight": mapping.revenue_weight or 1,
        }
        for mapping in mappings
    ]
    since = datetime.now(timezone.utc) - timedelta(hours=12)
    sales_rows = [
        {
            "store_id": int(store_id),
            "website_id": int(website_id or 0),
            "website_name": website_name or ("Website " + str(website_id) if website_id else "All websites"),
            "currency": currency_code or "UNKNOWN",
            "orders": int(order_count or 0),
            "amount": float(amount or 0),
            "margin": float(margin or 0),
        }
        for store_id, website_id, website_name, currency_code, order_count, amount, margin in (
            await session.execute(
                select(
                    OdooSaleOrder.store_id,
                    OdooSaleOrder.website_id,
                    func.max(OdooSaleOrder.website_name),
                    OdooSaleOrder.currency_code,
                    func.count(OdooSaleOrder.id),
                    func.coalesce(func.sum(OdooSaleOrder.amount_total), 0.0),
                    func.coalesce(func.sum(OdooSaleOrder.margin_amount), 0.0),
                )
                .where(
                    OdooSaleOrder.order_datetime >= since,
                    OdooSaleOrder.state.in_(["sale", "done"]),
                )
                .group_by(OdooSaleOrder.store_id, OdooSaleOrder.website_id, OdooSaleOrder.currency_code)
            )
        ).all()
    ]
    product_feed_rows = (
        await session.scalars(
            select(OdooProductPageSignal)
            .order_by(OdooProductPageSignal.synced_at.desc(), OdooProductPageSignal.margin_amount.desc())
            .limit(30)
        )
    ).all()
    product_feed_summary = [
        {
            "label": label,
            "count": int(count or 0),
            "synced_at": synced_at,
        }
        for label, count, synced_at in (
            await session.execute(
                select(
                    OdooProductPageSignal.label,
                    func.count(OdooProductPageSignal.id),
                    func.max(OdooProductPageSignal.synced_at),
                ).group_by(OdooProductPageSignal.label)
            )
        ).all()
    ]
    page_feed_publications = (
        await session.scalars(
            select(GoogleAdsPageFeedPublication)
            .order_by(GoogleAdsPageFeedPublication.updated_at.desc(), GoogleAdsPageFeedPublication.id.desc())
            .limit(20)
        )
    ).all()
    page_feed_base_url = await _page_feed_public_base_url(session, request)
    page_feed_publication_rows = []
    for publication in page_feed_publications:
        result = dict(publication.last_publish_json or {})
        result.setdefault("feed_name", public_feed_name(publication))
        result.setdefault("filename", public_feed_filename(publication))
        result["feed_url"] = public_feed_url(publication, page_feed_base_url)
        page_feed_publication_rows.append({"publication": publication, "result": result})
    return templates.TemplateResponse(
        "stores.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "stores": stores,
            "websites": websites,
            "accounts": accounts,
            "mappings": mappings,
            "mapping_rows": mapping_rows,
            "sales_rows": sales_rows,
            "product_feed_rows": product_feed_rows,
            "product_feed_summary": product_feed_summary,
            "page_feed_publications": page_feed_publications,
            "page_feed_publication_rows": page_feed_publication_rows,
            "page_feed_base_url": page_feed_base_url,
            "page_feed_default_max_urls": await _page_feed_default_max_urls(session),
            "saved": request.query_params.get("saved") == "1",
            "sync_job_id": request.query_params.get("job_id", ""),
            "website_sync_job_id": request.query_params.get("website_job_id", ""),
            "product_feed_job_id": request.query_params.get("product_feed_job_id", ""),
            "page_feed_publish_job_id": request.query_params.get("page_feed_publish_job_id", ""),
            "page_feed_generated": request.query_params.get("page_feed_generated") == "1",
            "deleted": request.query_params.get("deleted") == "1",
        },
    )


@router.post("/stores")
async def save_store(
    store_id: int = Form(0),
    name: str = Form(...),
    base_url: str = Form(...),
    database: str = Form(...),
    username: str = Form(...),
    api_key: str = Form(""),
    website_id: str = Form(""),
    is_multisite: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    store = await session.get(OdooStore, store_id) if store_id else None
    if store is None:
        store = OdooStore(name=name.strip())
        session.add(store)
    store.name = name.strip()
    store.base_url = base_url.strip().rstrip("/")
    store.database = database.strip()
    store.username = username.strip()
    if api_key.strip():
        store.api_key = api_key.strip()
    store.website_id = _none_int(website_id)
    store.is_multisite = is_multisite == "on"
    store.is_active = True
    await session.commit()
    return RedirectResponse("/stores?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/{store_id}/delete")
async def delete_store(
    store_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    store = await session.get(OdooStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found.")
    store.is_active = False
    await session.execute(
        delete(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.store_id == store.id)
    )
    await session.commit()
    return RedirectResponse("/stores?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/sync")
async def sync_stores(
    store_ids: list[int] = Form(default=[]),
    hours: int = Form(12),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    hours = min(max(int(hours or 12), 1), 168)
    selected_ids = list(dict.fromkeys(store_ids)) if store_ids else None
    job = await _queue_odoo_sync(
        session=session,
        user=user,
        store_ids=selected_ids,
        hours=hours,
        label="Sync selected Odoo confirmed orders" if selected_ids else "Sync all Odoo confirmed orders",
    )
    return RedirectResponse(f"/stores?job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/{store_id}/sync")
async def sync_store(
    store_id: int,
    hours: int = Form(12),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    store = await session.get(OdooStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found.")
    job = await _queue_odoo_sync(
        session=session,
        user=user,
        store_ids=[store_id],
        hours=min(max(int(hours or 12), 1), 168),
        label=f"Sync Odoo confirmed orders: {store.name}",
    )
    return RedirectResponse(f"/stores?job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/websites/sync")
async def sync_store_websites_all(
    store_ids: list[int] = Form(default=[]),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    selected_ids = list(dict.fromkeys(store_ids)) if store_ids else None
    job = await _queue_odoo_website_sync(
        session=session,
        user=user,
        store_ids=selected_ids,
        label="Sync selected Odoo websites" if selected_ids else "Sync all Odoo websites",
    )
    return RedirectResponse(f"/stores?website_job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/product-feed/sync")
async def sync_product_feeds_all(
    store_ids: list[int] = Form(default=[]),
    days: int = Form(7),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    days = min(max(int(days or 7), 1), 60)
    selected_ids = list(dict.fromkeys(store_ids)) if store_ids else None
    job = await _queue_odoo_product_feed_sync(
        session=session,
        user=user,
        store_ids=selected_ids,
        days=days,
        label="Sync selected Odoo product/page feeds" if selected_ids else "Sync all Odoo product/page feeds",
    )
    return RedirectResponse(f"/stores?product_feed_job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/page-feed/publish")
async def generate_page_feed_urls(
    request: Request,
    store_ids: list[int] = Form(default=[]),
    account_ids: list[int] = Form(default=[]),
    website_ids: list[int] = Form(default=[]),
    max_urls: int = Form(DEFAULT_MAX_URLS),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    selected_store_ids = list(dict.fromkeys(store_ids)) if store_ids else None
    selected_account_ids = list(dict.fromkeys(account_ids)) if account_ids else None
    selected_website_ids = list(dict.fromkeys(website_ids)) if website_ids else None
    max_urls = normalize_max_urls(max_urls, await _page_feed_default_max_urls(session))
    filters = [OdooStoreGoogleAdsMapping.is_active.is_(True)]
    if selected_store_ids:
        filters.append(OdooStoreGoogleAdsMapping.store_id.in_(selected_store_ids))
    if selected_account_ids:
        filters.append(OdooStoreGoogleAdsMapping.account_id.in_(selected_account_ids))
    if selected_website_ids:
        filters.append(OdooStoreGoogleAdsMapping.website_id.in_(selected_website_ids))
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
    public_base_url = await _page_feed_public_base_url(session, request)
    for mapping in mappings:
        await prepare_public_feed_for_mapping(
            session,
            mapping,
            public_base_url=public_base_url,
            max_urls=max_urls,
            feed_kind="best",
        )
    await session.commit()
    return RedirectResponse(
        "/page-feeds?generated=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/feeds/google/page-feed/{publication_id}/{token}/{filename}")
async def hosted_google_page_feed_csv(
    publication_id: int,
    token: str,
    filename: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    publication = await session.get(GoogleAdsPageFeedPublication, publication_id)
    if publication is None or token not in accepted_public_feed_tokens(publication):
        raise HTTPException(status_code=404, detail="Page feed not found.")
    expected_filename = public_feed_filename(publication)
    if not str(filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=404, detail="Page feed filename mismatch.")
    result = publication.last_publish_json if isinstance(publication.last_publish_json, dict) else {}
    max_urls = normalize_max_urls(result.get("max_urls"), DEFAULT_MAX_URLS)
    signals = await select_public_feed_signals(session, publication, max_urls=max_urls)
    csv_text = page_feed_csv_text_for_kind(signals, getattr(publication, "feed_kind", "best"))
    response = Response(content=csv_text, media_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'inline; filename="{expected_filename}"'
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.post("/stores/{store_id}/websites/sync")
async def sync_store_websites_one(
    store_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    store = await session.get(OdooStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found.")
    job = await _queue_odoo_website_sync(
        session=session,
        user=user,
        store_ids=[store_id],
        label=f"Sync Odoo websites: {store.name}",
    )
    return RedirectResponse(f"/stores?website_job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/mappings")
async def save_store_mappings(
    store_id: int = Form(...),
    website_id: int = Form(0),
    account_ids: list[int] = Form(...),
    revenue_weight: str = Form("1"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    store = await session.get(OdooStore, store_id)
    if store is None or not store.is_active:
        raise HTTPException(status_code=404, detail="Store not found.")
    if not account_ids:
        raise HTTPException(status_code=400, detail="Select at least one Google Ads account.")
    website_id = max(int(website_id or 0), 0)
    if website_id:
        website = await session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.store_id == store.id,
                OdooWebsite.website_id == website_id,
                OdooWebsite.is_active.is_(True),
            )
        )
        if website is None:
            raise HTTPException(status_code=400, detail="Select a website that belongs to this Odoo store.")
    weight = max(_none_float(revenue_weight, 1.0), 0.01)
    active_accounts = (
        await session.scalars(
            select(GoogleAdsAccount).where(
                GoogleAdsAccount.id.in_(list(dict.fromkeys(account_ids))),
                GoogleAdsAccount.is_active.is_(True),
            )
        )
    ).all()
    if not active_accounts:
        raise HTTPException(status_code=400, detail="No active Google Ads accounts selected.")
    for account in active_accounts:
        stmt = insert(OdooStoreGoogleAdsMapping).values(
            store_id=store.id,
            website_id=website_id,
            account_id=account.id,
            revenue_weight=weight,
            is_active=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                OdooStoreGoogleAdsMapping.store_id,
                OdooStoreGoogleAdsMapping.website_id,
                OdooStoreGoogleAdsMapping.account_id,
            ],
            set_={
                "revenue_weight": stmt.excluded.revenue_weight,
                "is_active": True,
            },
        )
        await session.execute(stmt)
    await session.commit()
    return RedirectResponse("/stores?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/stores/mappings/{mapping_id}/delete")
async def delete_store_mapping(
    mapping_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    mapping = await session.get(OdooStoreGoogleAdsMapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    mapping.is_active = False
    await session.commit()
    return RedirectResponse("/stores?saved=1", status_code=status.HTTP_303_SEE_OTHER)
