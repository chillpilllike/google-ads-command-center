from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GoogleAdsKeywordCandidate, GoogleAdsLandingPageCandidate
from app.services.google_ads_keyword_plan import clean_keyword, normalized_keyword, usable_keyword
from app.services.google_ads_landing_page_bank import canonical_landing_page_url, landing_page_hash, usable_landing_page_url


def _campaign_match(candidate: Any, campaign_id: Optional[int], campaign_name: str = "") -> bool:
    candidate_campaign_ids = [int(item) for item in (getattr(candidate, "campaign_ids", None) or []) if item]
    candidate_campaign_names = {str(item or "").strip().lower() for item in (getattr(candidate, "campaign_names", None) or [])}
    if campaign_id and int(campaign_id) in candidate_campaign_ids:
        return True
    if campaign_name and campaign_name.strip().lower() in candidate_campaign_names:
        return True
    return False


def keyword_duplicate_conflicts(
    session: Session,
    *,
    account_id: int,
    keywords: list[str],
    campaign_id: Optional[int] = None,
    campaign_name: str = "",
    allow_cross_campaign: bool = False,
) -> list[dict[str, Any]]:
    normalized_terms = []
    original_by_key: dict[str, str] = {}
    for keyword in keywords:
        cleaned = clean_keyword(keyword)
        if not usable_keyword(cleaned):
            continue
        key = normalized_keyword(cleaned)
        normalized_terms.append(key)
        original_by_key.setdefault(key, cleaned)
    normalized_terms = list(dict.fromkeys(normalized_terms))
    if not normalized_terms:
        return []
    rows = session.scalars(
        select(GoogleAdsKeywordCandidate).where(
            GoogleAdsKeywordCandidate.account_id == int(account_id),
            GoogleAdsKeywordCandidate.normalized_keyword.in_(normalized_terms),
        )
    ).all()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        same_campaign = _campaign_match(row, campaign_id, campaign_name)
        if same_campaign or not allow_cross_campaign:
            conflicts.append(
                {
                    "type": "keyword",
                    "value": original_by_key.get(row.normalized_keyword, row.keyword),
                    "normalized_value": row.normalized_keyword,
                    "candidate_id": row.id,
                    "reason": "same_campaign" if same_campaign else "same_account_existing",
                    "campaign_ids": row.campaign_ids or [],
                    "campaign_names": row.campaign_names or [],
                    "quality_label": row.quality_label,
                    "conversions": float(row.conversions or 0) + float(row.all_conversions or 0),
                    "conversion_value": float(row.conversion_value or 0) + float(row.all_conversions_value or 0),
                }
            )
    return conflicts


def landing_page_duplicate_conflicts(
    session: Session,
    *,
    account_id: int,
    urls: list[str],
    campaign_id: Optional[int] = None,
    campaign_name: str = "",
    allow_cross_campaign: bool = False,
) -> list[dict[str, Any]]:
    hashes = []
    original_by_hash: dict[str, str] = {}
    normalized_by_hash: dict[str, str] = {}
    for url in urls:
        if not usable_landing_page_url(url):
            continue
        normalized = canonical_landing_page_url(url)
        key = landing_page_hash(normalized)
        hashes.append(key)
        original_by_hash.setdefault(key, url)
        normalized_by_hash.setdefault(key, normalized)
    hashes = list(dict.fromkeys(hashes))
    if not hashes:
        return []
    rows = session.scalars(
        select(GoogleAdsLandingPageCandidate).where(
            GoogleAdsLandingPageCandidate.account_id == int(account_id),
            GoogleAdsLandingPageCandidate.normalized_url_hash.in_(hashes),
        )
    ).all()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        same_campaign = _campaign_match(row, campaign_id, campaign_name)
        if same_campaign or not allow_cross_campaign:
            conflicts.append(
                {
                    "type": "landing_page",
                    "value": original_by_hash.get(row.normalized_url_hash, row.url),
                    "normalized_value": normalized_by_hash.get(row.normalized_url_hash, row.normalized_url),
                    "candidate_id": row.id,
                    "reason": "same_campaign" if same_campaign else "same_account_existing",
                    "campaign_ids": row.campaign_ids or [],
                    "campaign_names": row.campaign_names or [],
                    "quality_label": row.quality_label,
                    "conversions": float(row.conversions or 0) + float(row.all_conversions or 0),
                    "conversion_value": float(row.conversion_value or 0) + float(row.all_conversions_value or 0),
                }
            )
    return conflicts


def campaign_factory_duplicate_report(
    session: Session,
    *,
    account_id: int,
    keywords: Optional[list[str]] = None,
    landing_page_urls: Optional[list[str]] = None,
    campaign_id: Optional[int] = None,
    campaign_name: str = "",
    allow_cross_campaign: bool = False,
) -> dict[str, Any]:
    keyword_conflicts = keyword_duplicate_conflicts(
        session,
        account_id=account_id,
        keywords=keywords or [],
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        allow_cross_campaign=allow_cross_campaign,
    )
    landing_page_conflicts = landing_page_duplicate_conflicts(
        session,
        account_id=account_id,
        urls=landing_page_urls or [],
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        allow_cross_campaign=allow_cross_campaign,
    )
    return {
        "account_id": int(account_id),
        "allow_cross_campaign": allow_cross_campaign,
        "blocked": bool(keyword_conflicts or landing_page_conflicts),
        "keyword_conflicts": keyword_conflicts,
        "landing_page_conflicts": landing_page_conflicts,
        "conflict_count": len(keyword_conflicts) + len(landing_page_conflicts),
    }
