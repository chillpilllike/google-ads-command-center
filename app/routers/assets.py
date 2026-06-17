from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import GoogleAdsAccount, GoogleAdsAssetAutomationPreference, GoogleAdsGeneratedAsset, User
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.google_ads_assets import ASSET_TYPE_LABELS
from app.tasks import generate_google_ads_assets


router = APIRouter()


def _checked(value: str | None) -> bool:
    return value == "on"


async def _selected_account(
    session: AsyncSession,
    account_id: Optional[int],
) -> tuple[list[GoogleAdsAccount], GoogleAdsAccount | None]:
    accounts = (
        await session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    selected = None
    if account_id:
        selected = next((account for account in accounts if account.id == int(account_id)), None)
    if selected is None and accounts:
        selected = accounts[0]
    return list(accounts), selected


async def _preference_for(session: AsyncSession, account: GoogleAdsAccount) -> GoogleAdsAssetAutomationPreference:
    preference = await session.scalar(
        select(GoogleAdsAssetAutomationPreference).where(GoogleAdsAssetAutomationPreference.account_id == account.id)
    )
    if preference is not None:
        return preference
    preference = GoogleAdsAssetAutomationPreference(account_id=account.id)
    session.add(preference)
    await session.commit()
    await session.refresh(preference)
    return preference


@router.get("/assets", response_class=HTMLResponse)
async def assets_page(
    request: Request,
    account_id: Optional[int] = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    accounts, selected = await _selected_account(session, account_id)
    preference = await _preference_for(session, selected) if selected else None
    generated_assets = []
    counts: dict[str, int] = {}
    if selected:
        generated_assets = (
            await session.scalars(
                select(GoogleAdsGeneratedAsset)
                .where(GoogleAdsGeneratedAsset.account_id == selected.id)
                .order_by(GoogleAdsGeneratedAsset.updated_at.desc(), GoogleAdsGeneratedAsset.id.desc())
                .limit(120)
            )
        ).all()
        for asset in generated_assets:
            counts[asset.asset_type] = counts.get(asset.asset_type, 0) + 1
    return templates.TemplateResponse(
        "assets.html",
        {
            "request": request,
            "user": user,
            "app_name": settings.app_name,
            "accounts": accounts,
            "selected_account": selected,
            "preference": preference,
            "generated_assets": generated_assets,
            "asset_counts": counts,
            "asset_type_labels": ASSET_TYPE_LABELS,
            "saved": request.query_params.get("saved") == "1",
            "generated_job_id": request.query_params.get("generated_job_id", ""),
            "generated_error": request.query_params.get("generated_error", ""),
        },
    )


@router.post("/assets/preferences")
async def save_asset_preferences(
    account_id: int = Form(...),
    auto_discount_promotions_enabled: Optional[str] = Form(None),
    auto_coupon_promotions_enabled: Optional[str] = Form(None),
    auto_sitelinks_enabled: Optional[str] = Form(None),
    auto_structured_snippets_enabled: Optional[str] = Form(None),
    auto_pmax_search_themes_enabled: Optional[str] = Form(None),
    auto_callouts_enabled: Optional[str] = Form(None),
    auto_price_assets_enabled: Optional[str] = Form(None),
    auto_business_messages_enabled: Optional[str] = Form(None),
    auto_campaign_asset_mapping_enabled: Optional[str] = Form(None),
    auto_ad_group_asset_mapping_enabled: Optional[str] = Form(None),
    whatsapp_country_code: str = Form("+1"),
    whatsapp_phone_number: str = Form(""),
    whatsapp_starter_message: str = Form("Can I get help choosing a product?"),
    whatsapp_call_to_action: str = Form("MESSAGE"),
    whatsapp_call_to_action_description: str = Form("Chat with us"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active Google Ads account.")
    preference = await session.scalar(
        select(GoogleAdsAssetAutomationPreference).where(GoogleAdsAssetAutomationPreference.account_id == account.id)
    )
    if preference is None:
        preference = GoogleAdsAssetAutomationPreference(account_id=account.id)
        session.add(preference)
    preference.auto_discount_promotions_enabled = _checked(auto_discount_promotions_enabled)
    preference.auto_coupon_promotions_enabled = _checked(auto_coupon_promotions_enabled)
    preference.auto_sitelinks_enabled = _checked(auto_sitelinks_enabled)
    preference.auto_structured_snippets_enabled = _checked(auto_structured_snippets_enabled)
    preference.auto_pmax_search_themes_enabled = _checked(auto_pmax_search_themes_enabled)
    preference.auto_callouts_enabled = _checked(auto_callouts_enabled)
    preference.auto_price_assets_enabled = _checked(auto_price_assets_enabled)
    preference.auto_business_messages_enabled = _checked(auto_business_messages_enabled)
    preference.auto_campaign_asset_mapping_enabled = _checked(auto_campaign_asset_mapping_enabled)
    preference.auto_ad_group_asset_mapping_enabled = _checked(auto_ad_group_asset_mapping_enabled)
    preference.whatsapp_country_code = whatsapp_country_code[:8]
    preference.whatsapp_phone_number = whatsapp_phone_number[:40]
    preference.whatsapp_starter_message = whatsapp_starter_message[:140]
    preference.whatsapp_call_to_action = whatsapp_call_to_action[:40]
    preference.whatsapp_call_to_action_description = whatsapp_call_to_action_description[:30]
    await session.commit()
    return RedirectResponse(f"/assets?account_id={account.id}&saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assets/generate")
async def queue_asset_generation(
    account_id: int = Form(...),
    include_sitelinks: Optional[str] = Form(None),
    include_structured_snippets: Optional[str] = Form(None),
    include_pmax_search_themes: Optional[str] = Form(None),
    include_promotions: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, int(account_id))
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active Google Ads account.")
    job = await create_background_job(
        session,
        job_type="assets_generate",
        label=f"Generate assets for {account.name}",
        requested_by_id=user.id,
        payload={
            "account_id": account.id,
            "customer_id": account.customer_id,
            "include_sitelinks": _checked(include_sitelinks),
            "include_structured_snippets": _checked(include_structured_snippets),
            "include_pmax_search_themes": _checked(include_pmax_search_themes),
            "include_promotions": _checked(include_promotions),
        },
    )
    try:
        message = generate_google_ads_assets.send(
            account.id,
            user.id,
            job.id,
            _checked(include_sitelinks),
            _checked(include_structured_snippets),
            _checked(include_pmax_search_themes),
            _checked(include_promotions),
        )
        await save_job_message_id(session, job, message.message_id)
    except Exception as exc:  # noqa: BLE001 - dispatch failures should be visible.
        await mark_job_dispatch_failed(session, job, str(exc))
        return RedirectResponse(
            f"/assets?account_id={account.id}&generated_error=dispatch",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(
        f"/assets?account_id={account.id}&generated_job_id={job.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/assets/generated/{asset_id}.json")
async def generated_asset_json(
    asset_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> JSONResponse:
    asset = await session.get(GoogleAdsGeneratedAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Generated asset not found.")
    return JSONResponse(
        {
            "id": asset.id,
            "account_id": asset.account_id,
            "asset_type": asset.asset_type,
            "name": asset.name,
            "source_type": asset.source_type,
            "source_key": asset.source_key,
            "status": asset.status,
            "google_resource_name": asset.google_resource_name,
            "campaign_id": asset.campaign_id,
            "campaign_name": asset.campaign_name,
            "payload_json": asset.payload_json,
            "source_json": asset.source_json,
            "last_error": asset.last_error,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
            "last_generated_at": asset.last_generated_at.isoformat() if asset.last_generated_at else None,
        }
    )
