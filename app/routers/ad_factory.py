from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import require_user, settings, templates
from app.models import AdDraft, GoogleAdsAccount, OdooStoreGoogleAdsMapping, OdooWebsite, User
from app.app_settings import get_sync_setting_map, parse_bool
from app.services.background_jobs import create_background_job, mark_job_dispatch_failed, save_job_message_id
from app.services.openai_ad_copy import generate_ad_copy
from app.services.odoo_product_feed import product_feed_context_for_account
from app.services.google_ads_research_collector import latest_research_summary
from app.services.google_ads_keyword_plan import (
    KEYWORD_LOOKBACK_OPTIONS,
    google_keyword_plan_for_account,
    normalize_keyword_lookback,
)
from app.tasks import SessionLocal


router = APIRouter()


BIDDING_LABELS = {
    "maximize_conversion_value_target_roas": "Maximize conversion value with Target ROAS",
    "maximize_conversion_value_no_target": "Maximize conversion value without Target ROAS",
    "maximize_clicks": "Maximize clicks with maximum CPC bid limit",
}

CAMPAIGN_PRODUCT_MODES = {
    "best": "Best performing product feed only",
    "good": "Other good/watch products",
    "new": "New or cross-site learned products",
    "mixed": "Mixed winners, watch, and new products",
}

RSA_SOURCE_MODES = {
    "page_feed": "Odoo/page-feed driven",
    "google_keywords": "Google keyword-only RSA",
}


def _empty_website_url(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"", "http://", "https://"}


def _normalize_mapped_website_url(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return parsed._replace(path="", params="", query="", fragment="").geturl().rstrip("/")


async def _mapped_account_website_urls(session: AsyncSession, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    account_ids = [int(account_id) for account_id in account_ids if account_id]
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            select(OdooStoreGoogleAdsMapping, OdooWebsite)
            .outerjoin(
                OdooWebsite,
                and_(
                    OdooWebsite.store_id == OdooStoreGoogleAdsMapping.store_id,
                    OdooWebsite.website_id == OdooStoreGoogleAdsMapping.website_id,
                    OdooWebsite.is_active.is_(True),
                ),
            )
            .where(
                OdooStoreGoogleAdsMapping.is_active.is_(True),
                OdooStoreGoogleAdsMapping.account_id.in_(account_ids),
            )
            .order_by(
                OdooStoreGoogleAdsMapping.account_id,
                OdooStoreGoogleAdsMapping.website_id.desc(),
                OdooStoreGoogleAdsMapping.created_at.desc(),
            )
        )
    ).all()
    mapped: dict[int, dict[str, Any]] = {}
    for mapping, website in rows:
        if mapping.account_id in mapped:
            continue
        raw_url = website.domain if website and website.domain else mapping.store.base_url
        website_url = _normalize_mapped_website_url(raw_url)
        if not website_url:
            continue
        label = mapping.store.name
        if website and website.name:
            label = f"{mapping.store.name} / {website.name}"
        mapped[mapping.account_id] = {
            "url": website_url,
            "label": label,
            "store_id": mapping.store_id,
            "website_id": mapping.website_id or 0,
        }
    return mapped


def generate_copy_sync(
    ad_type: str,
    website_url: str,
    business_name: str,
    generic_page_feed_copy: bool = False,
    keyword_terms: list[str] | None = None,
) -> tuple[dict, dict, str]:
    with SessionLocal() as session:
        return generate_ad_copy(
            session,
            ad_type=ad_type,
            website_url=website_url,
            business_name=business_name,
            generic_page_feed_copy=generic_page_feed_copy,
            keyword_terms=keyword_terms,
        )


def _optional_float(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def build_bidding_config(
    *,
    account: GoogleAdsAccount,
    bidding_strategy: str,
    target_roas_percent: str,
    max_cpc_bid_limit: str,
    daily_budget: str,
) -> tuple[dict[str, Any], list[str]]:
    bidding_strategy = bidding_strategy.strip().lower()
    if bidding_strategy not in BIDDING_LABELS:
        return {}, ["Select a supported bidding strategy."]

    errors: list[str] = []
    target_roas = _optional_float(target_roas_percent)
    max_cpc = _optional_float(max_cpc_bid_limit)
    budget = _optional_float(daily_budget)
    if budget is None or budget <= 0:
        errors.append("Daily budget is required before this can be reviewed for publishing.")

    config: dict[str, Any] = {
        "strategy": bidding_strategy,
        "strategy_label": BIDDING_LABELS[bidding_strategy],
        "currency_code": account.currency_code or "",
        "daily_budget": budget,
    }
    if bidding_strategy == "maximize_conversion_value_target_roas":
        if target_roas is None or target_roas <= 0:
            errors.append("Target ROAS percentage is required for Target ROAS bidding.")
        config["target_roas_percent"] = target_roas
        config["target_roas"] = (target_roas / 100) if target_roas is not None else None
    elif bidding_strategy == "maximize_clicks":
        if max_cpc is None or max_cpc <= 0:
            errors.append("Maximum CPC bid limit is required for Maximize Clicks.")
        config["max_cpc_bid_limit"] = max_cpc
    return config, errors


def publish_readiness(draft: AdDraft, values: dict[str, Any]) -> dict[str, Any]:
    assets = draft.generated_assets or {}
    validation = draft.validation_json or {}
    bidding = assets.get("bidding") or {}
    product_feed = assets.get("odoo_product_feed") or {}
    blockers: list[str] = []
    warnings: list[str] = []
    if not validation.get("ok"):
        blockers.append("Creative validation must be clean before publishing.")
    if not bidding.get("strategy"):
        blockers.append("Bidding strategy is missing from this draft.")
    if not bidding.get("daily_budget"):
        blockers.append("Daily budget is missing from this draft.")
    if bidding.get("strategy") == "maximize_conversion_value_target_roas" and not bidding.get("target_roas"):
        blockers.append("Target ROAS is missing for the selected bidding strategy.")
    if bidding.get("strategy") == "maximize_clicks" and not bidding.get("max_cpc_bid_limit"):
        blockers.append("Maximum CPC bid limit is missing for Maximize Clicks.")
    if draft.ad_type == "pmax":
        blockers.append("PMax publishing needs image and logo assets before Google Ads will accept the asset group.")
    has_generated_keywords = bool(
        (assets.get("google_keyword_plan") or {}).get("terms")
        or any((cluster.get("exact_terms") or []) for cluster in (assets.get("keyword_clusters") or []) if isinstance(cluster, dict))
    )
    if draft.ad_type == "rsa" and not has_generated_keywords:
        blockers.append("RSA publishing needs campaign/ad group targeting or generated keywords before live creation.")
    if draft.ad_type == "rsa" and has_generated_keywords:
        warnings.append("RSA has generated exact keyword targets saved for publish planning.")
    if draft.ad_type == "dsa":
        warnings.append("DSA can publish only after domain targeting is confirmed for the website.")
    negative_targets = set(product_feed.get("negative_page_targets") or [])
    if draft.final_url and draft.final_url in negative_targets:
        blockers.append("Final URL is excluded by the Odoo/Google product feed.")
    if product_feed.get("using_fallback"):
        warnings.append("This draft is using fallback product data because no local Odoo sales were found for the mapped website.")
    if product_feed.get("page_feed_targets"):
        warnings.append("Use the saved Odoo page feed targets for DSA/PMax and exclude the saved negative page targets.")
    if not parse_bool(values.get("optimizer.allow_mutations", False)):
        blockers.append("Safety setting 'Allow mutations' is off.")
    if parse_bool(values.get("optimizer.dry_run", True)):
        blockers.append("Safety setting 'Dry run' is on.")
    blockers.append("Live Google Ads ad publishing is not enabled yet in this portal; this action records a publish check.")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checked": True,
    }


@router.get("/ad-factory", response_class=HTMLResponse)
async def ad_factory_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> HTMLResponse:
    accounts = (
        await session.scalars(
            select(GoogleAdsAccount)
            .where(GoogleAdsAccount.is_active.is_(True))
            .order_by(GoogleAdsAccount.name)
        )
    ).all()
    drafts = (
        await session.scalars(
            select(AdDraft).order_by(AdDraft.created_at.desc()).limit(12)
        )
    ).all()
    account_domain_map = await _mapped_account_website_urls(session, [account.id for account in accounts])
    default_account = next((account for account in accounts if account.id in account_domain_map), accounts[0] if accounts else None)
    latest_research = {}
    return templates.TemplateResponse(
        "ad_factory.html",
        {
            "request": request,
            "user": user,
            "accounts": accounts,
            "drafts": drafts,
            "account_domain_map": account_domain_map,
            "default_ad_factory_account": default_account,
            "app_name": settings.app_name,
            "campaign_product_modes": CAMPAIGN_PRODUCT_MODES,
            "rsa_source_modes": RSA_SOURCE_MODES,
            "keyword_lookback_options": KEYWORD_LOOKBACK_OPTIONS,
            "latest_research": latest_research,
            "created": request.query_params.get("created") == "1",
            "approved": request.query_params.get("approved") == "1",
            "publish_checked": request.query_params.get("publish_checked") == "1",
            "insights_job_id": request.query_params.get("insights_job_id", ""),
            "deleted": request.query_params.get("deleted") == "1",
        },
    )


@router.post("/ad-factory/generate")
async def generate_ad_draft(
    account_id: int = Form(...),
    ad_type: str = Form(...),
    website_url: str = Form(...),
    final_url: str = Form(""),
    business_name: str = Form(""),
    bidding_strategy: str = Form(...),
    target_roas_percent: str = Form(""),
    max_cpc_bid_limit: str = Form(""),
    daily_budget: str = Form(""),
    campaign_product_mode: str = Form("best"),
    rsa_source_mode: str = Form("page_feed"),
    rsa_keyword_lookback: str = Form("60"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    ad_type = ad_type.strip().lower()
    if ad_type not in {"pmax", "dsa", "rsa"}:
        raise HTTPException(status_code=400, detail="Unsupported ad type.")
    account = await session.get(GoogleAdsAccount, account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active account.")
    website_url = website_url.strip()
    submitted_final_url = (final_url or "").strip()
    if _empty_website_url(website_url):
        mapped_urls = await _mapped_account_website_urls(session, [account.id])
        website_url = str((mapped_urls.get(account.id) or {}).get("url") or "").strip()
    if not website_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Website URL must start with http:// or https://, or the account must be mapped to an Odoo website.")
    campaign_product_mode = campaign_product_mode.strip().lower()
    if campaign_product_mode not in CAMPAIGN_PRODUCT_MODES:
        raise HTTPException(status_code=400, detail="Select a supported product feed mode.")
    rsa_source_mode = str(rsa_source_mode or "page_feed").strip().lower()
    if rsa_source_mode not in RSA_SOURCE_MODES:
        rsa_source_mode = "page_feed"
    if rsa_source_mode == "google_keywords" and ad_type != "rsa":
        raise HTTPException(status_code=400, detail="Google keyword-only source is available for Responsive Search Ads only.")
    rsa_keyword_lookback = normalize_keyword_lookback(rsa_keyword_lookback)
    keyword_plan: dict[str, Any] = {}
    with SessionLocal() as sync_session:
        if rsa_source_mode == "google_keywords":
            product_feed = {
                "account_id": account.id,
                "campaign_mode": "none",
                "campaign_mode_label": "No product feed; Google keyword-only RSA",
                "hosted_page_feed": {},
                "has_local_sales": False,
                "using_fallback": False,
                "winners": [],
                "fallback": [],
                "watch": [],
                "exclusions": [],
                "page_feed_targets": [],
                "negative_page_targets": [],
                "keyword_clusters": [],
                "source_terms": [],
            }
            keyword_plan = google_keyword_plan_for_account(sync_session, account, lookback=rsa_keyword_lookback, limit=30)
        else:
            product_feed = product_feed_context_for_account(sync_session, account.id, campaign_mode=campaign_product_mode)
        research_summary = latest_research_summary(sync_session, account)
    has_page_feed_targets = bool(product_feed.get("page_feed_targets"))
    final_url = submitted_final_url or website_url
    copy_source_url = website_url if has_page_feed_targets else final_url
    bidding, bidding_errors = build_bidding_config(
        account=account,
        bidding_strategy=bidding_strategy,
        target_roas_percent=target_roas_percent,
        max_cpc_bid_limit=max_cpc_bid_limit,
        daily_budget=daily_budget,
    )
    if bidding_errors:
        raise HTTPException(status_code=400, detail=" ".join(bidding_errors))

    assets, validation, prompt = await asyncio.to_thread(
        generate_copy_sync,
        ad_type,
        copy_source_url or website_url,
        business_name.strip(),
        has_page_feed_targets,
        keyword_plan.get("terms") or [],
    )
    assets["final_url"] = final_url
    assets["copy_source_url"] = copy_source_url
    assets["bidding"] = bidding
    assets["odoo_product_feed"] = product_feed
    assets["campaign_product_mode"] = {
        "key": campaign_product_mode,
        "label": CAMPAIGN_PRODUCT_MODES[campaign_product_mode],
    }
    assets["rsa_source_mode"] = {
        "key": rsa_source_mode,
        "label": RSA_SOURCE_MODES[rsa_source_mode],
    }
    assets["google_ads_research"] = research_summary
    assets["page_feed_targets"] = product_feed.get("page_feed_targets") or []
    assets["negative_page_targets"] = product_feed.get("negative_page_targets") or []
    assets["keyword_clusters"] = product_feed.get("keyword_clusters") or []
    existing_terms = [str(term) for term in (assets.get("source_terms") or []) if str(term).strip()]
    feed_terms = [str(term) for term in (product_feed.get("source_terms") or []) if str(term).strip()]
    if has_page_feed_targets:
        assets["source_terms"] = list(dict.fromkeys(existing_terms))[:30]
        assets["product_feed_source_terms"] = feed_terms[:30]
        assets["copy_strategy"] = {
            "mode": "generic_page_feed",
            "reason": "Visible ad assets stay generic because product relevance comes from page feed URLs and Google automation.",
        }
    else:
        assets["source_terms"] = list(dict.fromkeys(feed_terms + existing_terms))[:30]
    if rsa_source_mode == "google_keywords":
        keyword_terms = [str(term) for term in keyword_plan.get("terms") or [] if str(term).strip()]
        assets["source_terms"] = keyword_terms[:30]
        assets["google_keyword_plan"] = keyword_plan
        assets["keyword_clusters"] = [
            {
                "ad_group_name": f"Google keyword RSA - {keyword_plan.get('lookback_label', 'saved data')}",
                "source": "saved_google_ads_snapshots",
                "match_type": "exact",
                "exact_terms": keyword_terms[:30],
            }
        ] if keyword_terms else []
        assets["copy_strategy"] = {
            "mode": "google_keyword_only_rsa",
            "reason": "RSA keywords come only from saved Google Ads search-term snapshots for the selected lookback.",
        }
    validation.setdefault("warnings", [])
    if has_page_feed_targets:
        validation["warnings"].append(
            "Page-feed draft uses generic store/category copy; product-specific matching should come from the page feed URLs and Google automation."
        )
    if product_feed.get("using_fallback"):
        validation["warnings"].append("No local Odoo sales were found for this mapped website, so the draft uses fallback product/page signals.")
    if not product_feed.get("page_feed_targets"):
        if rsa_source_mode == "google_keywords":
            validation["warnings"].append("This RSA draft intentionally skips Odoo page feeds and uses saved Google keyword data only.")
        else:
            validation["warnings"].append("No Odoo product/page feed targets are saved for this account yet. Run Product feed from Stores.")
    if rsa_source_mode == "google_keywords":
        if keyword_plan.get("terms"):
            validation["warnings"].append(
                f"Google keyword-only RSA uses {len(keyword_plan.get('terms') or [])} saved terms from {keyword_plan.get('lookback_label')}."
            )
        else:
            validation.setdefault("errors", [])
            validation["errors"].append(
                f"No saved clicked Google keywords were found for {keyword_plan.get('lookback_label', 'the selected lookback')}. Run insight sync first."
            )
            validation["ok"] = False
    if final_url in set(product_feed.get("negative_page_targets") or []):
        validation.setdefault("errors", [])
        validation["errors"].append("Final URL is excluded by the Odoo/Google product feed.")
        validation["ok"] = False
    validation["bidding"] = {"ok": not bidding_errors, "errors": bidding_errors}
    if not research_summary.get("datasets"):
        validation["warnings"].append("No saved 60-day Google Ads insight snapshots were found for this account. Run the insight sync before final campaign planning.")
    draft = AdDraft(
        account_id=account.id,
        created_by_id=user.id,
        ad_type=ad_type,
        status="ready_for_review" if validation.get("ok") else "needs_review",
        website_url=website_url,
        final_url=final_url,
        business_name=business_name.strip(),
        prompt=prompt,
        generated_assets=assets,
        validation_json=validation,
    )
    session.add(draft)
    await session.commit()
    return RedirectResponse(f"/ad-factory?created=1#draft-{draft.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ad-factory/insights/sync")
async def sync_ad_factory_insights(
    account_id: int = Form(...),
    days: int = Form(60),
    max_rows: int = Form(5000),
    force: str = Form(""),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    account = await session.get(GoogleAdsAccount, account_id)
    if account is None or not account.is_active:
        raise HTTPException(status_code=400, detail="Select an active account.")
    days = min(max(int(days or 60), 1), 365)
    max_rows = min(max(int(max_rows or 5000), 50), 50000)
    job = await create_background_job(
        session,
        job_type="google_ads_research_sync",
        label=f"Sync {days}d Google Ads research: {account.name}",
        requested_by_id=user.id,
        payload={
            "account_ids": [account.id],
            "days": days,
            "max_rows": max_rows,
            "force": force == "on",
        },
    )
    try:
        from app.tasks import sync_google_ads_research

        message = sync_google_ads_research.send(days, job.id, [account.id], max_rows, force == "on")
        await save_job_message_id(session, job, str(message.message_id))
    except Exception as exc:  # noqa: BLE001
        await mark_job_dispatch_failed(session, job, str(exc))
    return RedirectResponse(f"/ad-factory?insights_job_id={job.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ad-factory/drafts/{draft_id}/approve")
async def approve_ad_draft(
    draft_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    draft = await session.get(AdDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    if not (draft.validation_json or {}).get("ok"):
        raise HTTPException(status_code=400, detail="Fix validation issues before approval.")
    assets = dict(draft.generated_assets or {})
    assets["review"] = {
        "approved_by": user.email,
        "approved": True,
    }
    draft.generated_assets = assets
    draft.status = "approved"
    await session.commit()
    return RedirectResponse(f"/ad-factory?approved=1#draft-{draft.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ad-factory/drafts/{draft_id}/publish")
async def publish_ad_draft(
    draft_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    draft = await session.get(AdDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    with SessionLocal() as sync_session:
        values = get_sync_setting_map(sync_session)
    result = publish_readiness(draft, values)
    assets = dict(draft.generated_assets or {})
    assets["publish_result"] = {
        **result,
        "checked_by": user.email,
    }
    draft.generated_assets = assets
    draft.status = "publish_ready" if result["ok"] else "publish_blocked"
    await session.commit()
    return RedirectResponse(f"/ad-factory?publish_checked=1#draft-{draft.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/ad-factory/drafts/{draft_id}/delete")
async def delete_ad_draft(
    draft_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_user),
) -> RedirectResponse:
    draft = await session.get(AdDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    await session.delete(draft)
    await session.commit()
    return RedirectResponse("/ad-factory?deleted=1", status_code=status.HTTP_303_SEE_OTHER)
