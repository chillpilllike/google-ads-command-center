from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdDraft, GoogleAdsAccount, GoogleAdsGeneratedAsset, GoogleAdsLandingPageCandidate, OdooProductPageSignal
from app.services.google_ads_landing_page_bank import canonical_landing_page_url, usable_landing_page_url


EDITOR_HEADERS = [
    "Campaign#Original",
    "Campaign",
    "Labels",
    "Campaign Type",
    "Networks",
    "Budget",
    "Budget type",
    "EU political ads",
    "Standard conversion goals",
    "Custom conversion goal",
    "Customer acquisition",
    "Languages",
    "Bid Strategy Type",
    "Bid Strategy Name",
    "Enhanced CPC",
    "Target ROAS",
    "Maximum CPC bid limit",
    "Start Date",
    "End Date",
    "Broad match keywords",
    "Ad Schedule",
    "Ad rotation",
    "Content exclusions",
    "Targeting method",
    "Exclusion method",
    "DSA Website",
    "DSA Language",
    "DSA targeting source",
    "DSA page feeds",
    "Google Merchant Center feed",
    "Campaign Priority",
    "Local Inventory Ads",
    "Shopping ads on excluded brands",
    "Inventory filter",
    "Audience targeting",
    "Flexible Reach",
    "AI Max",
    "Text customization",
    "Final URL expansion",
    "Image enhancement",
    "Image generation",
    "Landing page images",
    "Video enhancement",
    "Brand guidelines",
    "Ad Group#Original",
    "Ad Group",
    "Max CPC",
    "Max CPM",
    "Target CPA",
    "Max CPV",
    "Target CPV",
    "Percent CPC",
    "Target CPM",
    "Target CPC",
    "Desktop Bid Modifier",
    "Mobile Bid Modifier",
    "Tablet Bid Modifier",
    "TV Screen Bid Modifier",
    "Display Network Custom Bid Type",
    "Optimized targeting",
    "Strict age and gender targeting",
    "Search term matching",
    "Ad Group Type",
    "Channels",
    "Audience name",
    "Age demographic",
    "Gender demographic",
    "Income demographic",
    "Parental status demographic",
    "Remarketing audience segments",
    "Interest categories",
    "Life events",
    "Custom audience segments",
    "Detailed demographics",
    "Remarketing audience exclusions",
    "Tracking template",
    "Final URL suffix",
    "Custom parameters",
    "Asset Group#Original",
    "Asset Group",
    *[f"Headline {index}" for index in range(1, 16)],
    *[f"Long headline {index}" for index in range(1, 6)],
    *[f"Description {index}" for index in range(1, 6)],
    "Call to action",
    "Business name",
    "Video ID 1",
    "Path 1",
    "Path 2",
    "Final URL",
    "Final mobile URL",
    "Audience signal",
    "URL rule condition 1#Original",
    "URL rule condition 1",
    "URL rule value 1#Original",
    "URL rule value 1",
    "URL rule condition 2#Original",
    "URL rule condition 2",
    "URL rule value 2#Original",
    "URL rule value 2",
    "URL rule condition 3#Original",
    "URL rule condition 3",
    "URL rule value 3#Original",
    "URL rule value 3",
    "ID#Original",
    "ID",
    "Location#Original",
    "Location",
    "Reach",
    "Location groups#Original",
    "Location groups",
    "Radius#Original",
    "Radius",
    "Unit#Original",
    "Unit",
    "Bid Modifier",
    "Criterion Type",
    "Keyword#Original",
    "Keyword",
    "Criterion Type#Original",
    "Search theme#Original",
    "Search theme",
    "Ad type",
    *[f"Headline {index}#Original" for index in range(1, 16)],
    *[f"Headline {index} position" for index in range(1, 16)],
    *[f"Description {index}#Original" for index in range(1, 9)],
    *[f"Description {index} position" for index in range(1, 5)],
    "Description 6",
    "Description 7",
    "Description 8",
    "Business name#Original",
    "Link source",
    "Link Text#Original",
    "Link Text",
    "Description Line 1#Original",
    "Description Line 1",
    "Description Line 2#Original",
    "Description Line 2",
    "Upgraded extension",
    "Source",
    "Header#Original",
    "Header",
    "Snippet Values#Original",
    "Snippet Values",
    "Callout text#Original",
    "Callout text",
    "Type#Original",
    "Type",
    "Price qualifier",
    "Language#Original",
    "Language",
    "Currency#Original",
    "Currency",
    *[item for index in range(1, 9) for item in (
        f"Header {index}#Original",
        f"Header {index}",
        f"Description {index}#Original",
        f"Description {index}",
        f"Price {index}",
        f"Unit {index}",
        f"Final URL {index}",
        f"Final mobile URL {index}",
    )],
    "Campaign Status",
    "Ad Group Status",
    "Asset Group Status",
    "Status",
    "Approval Status",
    "Ad strength",
    "Comment",
]


INTERNAL_PUBLIC_TEXT = {
    "google conversion signal",
    "odoo sales signal",
    "conversion signal",
    "popular recent seller",
}


@dataclass
class EditorExportBundle:
    filename: str
    content_type: str
    payload: bytes
    manifest: dict[str, Any]


def _blank_row() -> dict[str, str]:
    return {header: "" for header in EDITOR_HEADERS}


def _clean_cell(value: Any, *, max_len: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\u2013", "-").replace("\u2014", "-")).strip()
    lowered = text.lower()
    if lowered in INTERNAL_PUBLIC_TEXT:
        text = "Shop online"
    if max_len is not None and len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def _status(value: Any = None) -> str:
    text = str(value or "").strip().lower()
    if text in {"paused", "pause"}:
        return "Paused"
    if text in {"removed", "delete"}:
        return "Removed"
    return "Enabled"


def _today() -> str:
    return date.today().isoformat()


def _assets(draft: AdDraft) -> dict[str, Any]:
    return draft.generated_assets if isinstance(draft.generated_assets, dict) else {}


def _identity(assets: dict[str, Any]) -> dict[str, Any]:
    return assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}


def _campaign_name(draft: AdDraft) -> str:
    assets = _assets(draft)
    identity = _identity(assets)
    return _clean_cell(identity.get("campaign_name") or assets.get("campaign_name") or f"AUTO | Draft {draft.id}", max_len=255)


def _bidding(assets: dict[str, Any]) -> dict[str, Any]:
    return assets.get("bidding") if isinstance(assets.get("bidding"), dict) else {}


def _campaign_row(draft: AdDraft) -> dict[str, str]:
    assets = _assets(draft)
    bidding = _bidding(assets)
    campaign_name = _campaign_name(draft)
    is_pmax = draft.ad_type == "pmax"
    strategy = str(bidding.get("strategy") or "").lower()
    row = _blank_row()
    row.update(
        {
            "Campaign#Original": campaign_name,
            "Campaign": campaign_name,
            "Campaign Type": "Performance Max" if is_pmax else "Search",
            "Networks": "Google search;Search Partners;Display Network" if is_pmax else "Google search",
            "Budget": f"{float(bidding.get('daily_budget') or 1):.2f}",
            "Budget type": "Daily",
            "EU political ads": "Doesn't have EU political ads",
            "Standard conversion goals": "Account-level",
            "Customer acquisition": "Bid equally",
            "Languages": "All",
            "Bid Strategy Type": "Maximize clicks" if "click" in strategy else "Maximize conversion value",
            "Enhanced CPC": "Disabled",
            "Maximum CPC bid limit": f"{float(bidding.get('max_cpc_bid_limit') or 0):.2f}",
            "Start Date": _today(),
            "End Date": "[]",
            "Broad match keywords": "Off",
            "Ad Schedule": "[]",
            "Ad rotation": "Optimize for clicks",
            "Content exclusions": "[]",
            "Targeting method": "Location of presence or Area of interest",
            "Exclusion method": "Location of presence",
            "Google Merchant Center feed": "Enabled",
            "Campaign Priority": "Low",
            "Local Inventory Ads": "Enabled" if is_pmax else "Disabled",
            "Shopping ads on excluded brands": "Disabled",
            "Inventory filter": "*",
            "Audience targeting": "Audience" if is_pmax else "Audience segments",
            "Flexible Reach": "[]",
            "AI Max": "Disabled",
            "Text customization": "Enabled" if is_pmax else "Disabled",
            "Final URL expansion": "Enabled" if is_pmax else "Disabled",
            "Image enhancement": "Enabled" if is_pmax else "Disabled",
            "Image generation": "Disabled",
            "Landing page images": "Enabled" if is_pmax else "Disabled",
            "Video enhancement": "Enabled" if is_pmax else "Disabled",
            "Brand guidelines": "Disabled",
            "Campaign Status": "Enabled",
        }
    )
    target_roas = float(bidding.get("target_roas") or 0)
    if target_roas > 0 and "click" not in strategy:
        row["Target ROAS"] = f"{target_roas * 100:.4f}%"
    return row


def _ad_group_name(draft: AdDraft) -> str:
    assets = _assets(draft)
    clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
    for cluster in clusters:
        if isinstance(cluster, dict) and cluster.get("ad_group_name"):
            return _clean_cell(cluster.get("ad_group_name"), max_len=255)
    code = str((_identity(assets).get("campaign_code") or assets.get("campaign_code") or f"DRAFT-{draft.id}")).strip()
    if draft.ad_type == "dsa":
        return f"AUTO | Testing / Discovery | DSA Pages | {code}"[:255]
    return f"AUTO | Testing / Discovery | RSA Keywords | {code}"[:255]


def _ad_group_row(draft: AdDraft) -> dict[str, str]:
    assets = _assets(draft)
    bidding = _bidding(assets)
    row = _blank_row()
    name = _ad_group_name(draft)
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Languages": "All",
            "Audience targeting": "Audience segments",
            "Flexible Reach": "Genders;Ages;Parental status;Household incomes",
            "Ad Group#Original": name,
            "Ad Group": name,
            "Max CPC": f"{float(bidding.get('max_cpc_bid_limit') or 2.5):.2f}",
            "Max CPM": "0.01",
            "Target CPV": "0.01",
            "Target CPM": "0.01",
            "Display Network Custom Bid Type": "None",
            "Optimized targeting": "Disabled",
            "Strict age and gender targeting": "Disabled",
            "Search term matching": "Enabled",
            "Ad Group Type": "Standard",
            "Channels": "[]",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
        }
    )
    return row


def _rsa_ad_row(draft: AdDraft) -> dict[str, str]:
    assets = _assets(draft)
    row = _blank_row()
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Ad Group": _ad_group_name(draft),
            "Final URL": _clean_cell(assets.get("final_url") or draft.final_url or draft.website_url),
            "Ad type": "Responsive search ad",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    for index, value in enumerate((assets.get("headlines") or [])[:15], start=1):
        text = _clean_cell(value, max_len=30)
        row[f"Headline {index}"] = text
        row[f"Headline {index}#Original"] = text
        row[f"Headline {index} position"] = " -"
    for index, value in enumerate((assets.get("descriptions") or [])[:4], start=1):
        text = _clean_cell(value, max_len=90)
        row[f"Description {index}"] = text
        row[f"Description {index}#Original"] = text
        row[f"Description {index} position"] = " -"
    return row


def _dsa_ad_row(draft: AdDraft) -> dict[str, str]:
    assets = _assets(draft)
    row = _blank_row()
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Ad Group": _ad_group_name(draft),
            "Ad type": "Expanded dynamic search ad",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    for index, value in enumerate((assets.get("descriptions") or [])[:2], start=1):
        text = _clean_cell(value, max_len=90)
        row[f"Description {index}"] = text
        row[f"Description {index}#Original"] = text
    return row


def _root_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")


def _root_domain(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    return host


def _country_name_from_url_or_account(account: GoogleAdsAccount, url: str = "") -> str:
    host = _root_domain(url)
    if host.endswith(".ca"):
        return "Canada"
    if host.endswith(".com.au") or host.endswith(".au"):
        return "Australia"
    if host.endswith(".co.uk") or host.endswith(".uk"):
        return "United Kingdom"
    text = f"{account.name} {account.currency_code}".lower()
    if "canada" in text or " ca" in f" {text} " or "cad" in text:
        return "Canada"
    if "australia" in text or " aud" in f" {text} ":
        return "Australia"
    if "uk" in text or "gbp" in text:
        return "United Kingdom"
    return ""


def _dynamic_business_name(account: GoogleAdsAccount, root_url: str) -> str:
    host = _root_domain(root_url)
    if "nutricity" in host.lower() or "nutricity" in str(account.name or "").lower():
        return "NutriCity"
    if host:
        return host.split(".")[0].replace("-", " ").title()[:25]
    return _clean_cell(account.name, max_len=25)


def _safe_dynamic_descriptions(account: GoogleAdsAccount, root_url: str) -> list[str]:
    country = _country_name_from_url_or_account(account, root_url)
    suffix = f" in {country}" if country else ""
    return [
        _clean_cell(f"Shop quality supplements{suffix}. Fast shipping and trusted service.", max_len=90),
        _clean_cell(f"Browse wellness products{suffix} with clear prices and secure checkout.", max_len=90),
    ]


def _safe_dynamic_headlines(account: GoogleAdsAccount, root_url: str) -> list[str]:
    business = _dynamic_business_name(account, root_url)
    country = _country_name_from_url_or_account(account, root_url)
    values = [
        business,
        f"{business} Online",
        "Shop Supplements",
        "Wellness Products",
        "Vitamins Online",
        "Trusted Supplement Store",
    ]
    if country:
        values.insert(2, f"{business} {country}")
    return [_clean_cell(value, max_len=30) for value in values if _clean_cell(value)]


def _dynamic_search_campaign_name(account: GoogleAdsAccount) -> str:
    code = re.sub(r"\D+", "", str(account.customer_id or ""))[-8:] or str(account.id or "0")
    return f"AUTO | Testing / Discovery | AI Max Dynamic Search | AUTO-DSA{code}"[:255]


def _dynamic_search_ad_group_name(account: GoogleAdsAccount) -> str:
    code = re.sub(r"\D+", "", str(account.customer_id or ""))[-8:] or str(account.id or "0")
    return f"AUTO | Testing / Discovery | URL Inclusion Pages | AUTO-DSA{code}"[:255]


def _dynamic_search_budget_and_roas(drafts: list[AdDraft]) -> tuple[float, float]:
    for draft in drafts:
        assets = _assets(draft)
        bidding = _bidding(assets)
        if str(draft.ad_type or "").lower() in {"dsa", "rsa"} and bidding:
            budget = float(bidding.get("daily_budget") or 0)
            roas = float(bidding.get("target_roas") or 0)
            if budget > 0:
                return max(budget, 25.0), roas
    return 25.0, 0.0


def _candidate_urls_from_drafts(drafts: list[AdDraft], *, limit: Optional[int] = None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for draft in drafts:
        assets = _assets(draft)
        sources = [
            assets.get("url_inclusion_targets"),
            assets.get("page_feed_targets"),
            assets.get("landing_page_candidates"),
            [assets.get("final_url"), draft.final_url, draft.website_url],
        ]
        for source in sources:
            if not isinstance(source, list):
                continue
            for item in source:
                value = item.get("url") or item.get("final_url") or item.get("landing_page") if isinstance(item, dict) else item
                url = canonical_landing_page_url(_clean_cell(value))
                if (
                    not url.startswith(("http://", "https://"))
                    or not usable_landing_page_url(url)
                    or _is_broad_or_category_landing_page(url)
                ):
                    continue
                key = url.rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                urls.append(key)
                if limit and len(urls) >= limit:
                    return urls
    return urls


def _candidate_urls_from_db(session: Session, account: GoogleAdsAccount, *, limit: Optional[int] = None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        url = canonical_landing_page_url(_clean_cell(value))
        if (
            not url.startswith(("http://", "https://"))
            or not usable_landing_page_url(url)
            or _is_broad_or_category_landing_page(url)
        ):
            return
        key = url.rstrip("/")
        if key in seen:
            return
        seen.add(key)
        urls.append(key)

    landing_pages = session.scalars(
        select(GoogleAdsLandingPageCandidate)
        .where(GoogleAdsLandingPageCandidate.account_id == account.id)
        .order_by(GoogleAdsLandingPageCandidate.score.desc(), GoogleAdsLandingPageCandidate.updated_at.desc())
    ).all()
    for candidate in landing_pages:
        add(candidate.url or candidate.normalized_url)
        if limit and len(urls) >= limit:
            return urls

    product_pages = session.scalars(
        select(OdooProductPageSignal)
        .where(OdooProductPageSignal.account_id == account.id)
        .order_by(
            OdooProductPageSignal.google_conversions.desc(),
            OdooProductPageSignal.sales_amount.desc(),
            OdooProductPageSignal.order_count.desc(),
        )
    ).all()
    for signal in product_pages:
        add(signal.product_url)
        if limit and len(urls) >= limit:
            return urls
    return urls


def _is_broad_or_category_landing_page(url: Any) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    path = parsed.path.strip("/").lower()
    if not path:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if path in {"shop", "products", "product"}:
        return True
    if path.startswith(("shop/category/", "category/", "collections/", "collection/", "brands/", "brand/")):
        return True
    return any(segment in {"cart", "checkout", "contact", "about", "blog", "privacy", "terms"} for segment in segments)


def _dynamic_root_url(account: GoogleAdsAccount, drafts: list[AdDraft], urls: list[str]) -> str:
    for draft in drafts:
        for value in [_assets(draft).get("final_url"), draft.final_url, draft.website_url]:
            root = _root_url(str(value or ""))
            if root:
                return root
    for url in urls:
        root = _root_url(url)
        if root:
            return root
    return ""


def _dynamic_search_campaign_row(
    account: GoogleAdsAccount,
    *,
    campaign_name: str,
    root_url: str,
    budget: float,
    target_roas: float,
) -> dict[str, str]:
    row = _blank_row()
    row.update(
        {
            "Campaign#Original": campaign_name,
            "Campaign": campaign_name,
            "Campaign Type": "Search",
            "Networks": "Google search",
            "Budget": f"{float(budget or 1):.2f}",
            "Budget type": "Daily",
            "EU political ads": "Doesn't have EU political ads",
            "Standard conversion goals": "Account-level",
            "Customer acquisition": "Bid equally",
            "Languages": "English",
            "Bid Strategy Type": "Maximize conversion value",
            "Enhanced CPC": "Disabled",
            "Start Date": _today(),
            "End Date": "[]",
            "Broad match keywords": "Off",
            "Ad Schedule": "[]",
            "Ad rotation": "Optimize for clicks",
            "Content exclusions": "[]",
            "Targeting method": "Location of presence or Area of interest",
            "Exclusion method": "Location of presence",
            "DSA Website": _root_domain(root_url),
            "DSA Language": "en",
            "DSA targeting source": "Google and Page feed",
            "DSA page feeds": f"AUTO Page Feed | {account.customer_id}",
            "Audience targeting": "Audience segments",
            "Flexible Reach": "[]",
            "AI Max": "Enabled",
            "Text customization": "Enabled",
            "Final URL expansion": "Enabled",
            "Campaign Status": "Enabled",
        }
    )
    if target_roas > 0:
        row["Target ROAS"] = f"{target_roas * 100:.4f}%"
    return row


def _dynamic_search_ad_group_row(*, campaign_name: str, ad_group_name: str) -> dict[str, str]:
    row = _blank_row()
    row.update(
        {
            "Campaign": campaign_name,
            "Ad Group#Original": ad_group_name,
            "Ad Group": ad_group_name,
            "Max CPC": "2.50",
            "Max CPM": "0.01",
            "Target CPV": "0.01",
            "Target CPM": "0.01",
            "Display Network Custom Bid Type": "None",
            "Optimized targeting": "Disabled",
            "Strict age and gender targeting": "Disabled",
            "Search term matching": "Enabled",
            "Ad Group Type": "Standard",
            "Channels": "[]",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
        }
    )
    return row


def _dynamic_url_inclusion_row(*, campaign_name: str, ad_group_name: str, url: str) -> dict[str, str]:
    row = _blank_row()
    row.update(
        {
            "Campaign": campaign_name,
            "Ad Group": ad_group_name,
            "URL rule condition 1#Original": "URL",
            "URL rule condition 1": "URL",
            "URL rule value 1#Original": url,
            "URL rule value 1": url,
            "URL rule condition 2#Original": "None",
            "URL rule condition 2": "None",
            "URL rule value 2#Original": "[]",
            "URL rule value 2": "[]",
            "URL rule condition 3#Original": "None",
            "URL rule condition 3": "None",
            "URL rule value 3#Original": "[]",
            "URL rule value 3": "[]",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    return row


def _dynamic_expanded_search_ad_row(account: GoogleAdsAccount, *, campaign_name: str, ad_group_name: str, root_url: str) -> dict[str, str]:
    descriptions = _safe_dynamic_descriptions(account, root_url)
    row = _blank_row()
    row.update(
        {
            "Campaign": campaign_name,
            "Ad Group": ad_group_name,
            "Ad type": "Expanded dynamic search ad",
            "Description Line 1#Original": descriptions[0],
            "Description Line 1": descriptions[0],
            "Description Line 2#Original": descriptions[1],
            "Description Line 2": descriptions[1],
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    return row


def _dynamic_rsa_companion_row(account: GoogleAdsAccount, *, campaign_name: str, ad_group_name: str, root_url: str) -> dict[str, str]:
    row = _blank_row()
    row.update(
        {
            "Campaign": campaign_name,
            "Ad Group": ad_group_name,
            "Final URL": root_url,
            "Ad type": "Responsive search ad",
            "Business name": _dynamic_business_name(account, root_url),
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    for index, value in enumerate(_safe_dynamic_headlines(account, root_url)[:15], start=1):
        row[f"Headline {index}#Original"] = value
        row[f"Headline {index}"] = value
        row[f"Headline {index} position"] = " -"
    for index, value in enumerate(_safe_dynamic_descriptions(account, root_url)[:4], start=1):
        row[f"Description {index}#Original"] = value
        row[f"Description {index}"] = value
        row[f"Description {index} position"] = " -"
    return row


def _iter_dynamic_search_editor_rows(
    session: Session,
    account: GoogleAdsAccount,
    drafts: list[AdDraft],
) -> Iterable[dict[str, str]]:
    urls = _candidate_urls_from_drafts(drafts)
    existing = {url.rstrip("/") for url in urls}
    for url in _candidate_urls_from_db(session, account):
        key = url.rstrip("/")
        if key not in existing:
            existing.add(key)
            urls.append(key)
    root_url = _dynamic_root_url(account, drafts, urls)
    if root_url and root_url.rstrip("/") not in existing:
        urls.insert(0, root_url.rstrip("/"))
    campaign_name = _dynamic_search_campaign_name(account)
    ad_group_name = _dynamic_search_ad_group_name(account)
    budget, target_roas = _dynamic_search_budget_and_roas(drafts)
    yield _dynamic_search_campaign_row(
        account,
        campaign_name=campaign_name,
        root_url=root_url,
        budget=budget,
        target_roas=target_roas,
    )
    yield _dynamic_search_ad_group_row(campaign_name=campaign_name, ad_group_name=ad_group_name)
    for url in urls:
        yield _dynamic_url_inclusion_row(campaign_name=campaign_name, ad_group_name=ad_group_name, url=url)
    yield _dynamic_expanded_search_ad_row(account, campaign_name=campaign_name, ad_group_name=ad_group_name, root_url=root_url)
    if root_url:
        yield _dynamic_rsa_companion_row(account, campaign_name=campaign_name, ad_group_name=ad_group_name, root_url=root_url)


def _keyword_terms(draft: AdDraft, *, limit: Optional[int] = None) -> list[str]:
    assets = _assets(draft)
    terms: list[str] = []
    seen: set[str] = set()
    clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        for term in cluster.get("exact_terms") or []:
            text = _clean_cell(term, max_len=80).lower()
            if text and text not in seen:
                seen.add(text)
                terms.append(text)
            if limit and len(terms) >= limit:
                return terms
    for term in assets.get("source_terms") or []:
        text = _clean_cell(term, max_len=80).lower()
        if text and text not in seen:
            seen.add(text)
            terms.append(text)
        if limit and len(terms) >= limit:
            return terms
    return terms


def _keyword_row(draft: AdDraft, keyword: str) -> dict[str, str]:
    row = _blank_row()
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Ad Group": _ad_group_name(draft),
            "Criterion Type": "Exact",
            "Keyword#Original": keyword,
            "Keyword": keyword,
            "Criterion Type#Original": "Exact",
            "Campaign Status": "Enabled",
            "Ad Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    return row


def _negative_keyword_rows(draft: AdDraft) -> list[dict[str, str]]:
    assets = _assets(draft)
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
    for item in items:
        text = item.get("keyword") if isinstance(item, dict) else item
        keyword = _clean_cell(text, max_len=80).lower()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        row = _blank_row()
        row.update(
            {
                "Campaign": _campaign_name(draft),
                "Criterion Type": "Campaign Negative Exact",
                "Keyword#Original": keyword,
                "Keyword": keyword,
                "Criterion Type#Original": "Campaign Negative Exact",
                "Campaign Status": "Enabled",
                "Status": "Enabled",
            }
        )
        output.append(row)
    return output


def _negative_url_rows(draft: AdDraft) -> list[dict[str, str]]:
    assets = _assets(draft)
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in assets.get("negative_page_targets") or []:
        url = _clean_cell(value)
        if not url or url in seen:
            continue
        seen.add(url)
        row = _blank_row()
        row.update(
            {
                "Campaign": _campaign_name(draft),
                "URL rule condition 1#Original": "URL",
                "URL rule condition 1": "URL",
                "URL rule value 1#Original": url,
                "URL rule value 1": url,
                "URL rule condition 2#Original": "None",
                "URL rule condition 2": "None",
                "URL rule value 2#Original": "[]",
                "URL rule value 2": "[]",
                "URL rule condition 3#Original": "None",
                "URL rule condition 3": "None",
                "URL rule value 3#Original": "[]",
                "URL rule value 3": "[]",
                "Criterion Type": "Campaign Negative",
                "Campaign Status": "Enabled",
                "Status": "Enabled",
            }
        )
        output.append(row)
    return output


def _asset_group_name(draft: AdDraft, group: dict[str, Any]) -> str:
    assets = _assets(draft)
    code = _identity(assets).get("campaign_code") or assets.get("campaign_code") or f"DRAFT-{draft.id}"
    group_code = group.get("asset_group_code") or f"AG{int(group.get('asset_group_index') or 1):03d}"
    category = _identity(assets).get("category") or "PMax"
    return f"AUTO | {category} | PMax Asset Group | {code} | {group_code}"[:255]


def _pmax_asset_group_row(draft: AdDraft, group: dict[str, Any]) -> dict[str, str]:
    assets = _assets(draft)
    row = _blank_row()
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Asset Group#Original": _asset_group_name(draft, group),
            "Asset Group": _asset_group_name(draft, group),
            "Custom audience segments": _clean_cell(
                f"AUTO | PMax Custom Segment | {assets.get('campaign_code') or _identity(assets).get('campaign_code')} | {group.get('asset_group_code') or ''}",
                max_len=255,
            ),
            "Business name": _clean_cell(assets.get("business_name") or draft.business_name or draft.account.name, max_len=25),
            "Final URL": _clean_cell(assets.get("final_url") or draft.final_url or draft.website_url),
            "Campaign Status": "Enabled",
            "Asset Group Status": "Enabled",
        }
    )
    for index, value in enumerate((assets.get("headlines") or [])[:15], start=1):
        row[f"Headline {index}"] = _clean_cell(value, max_len=30)
    for index, value in enumerate((assets.get("long_headlines") or [])[:5], start=1):
        row[f"Long headline {index}"] = _clean_cell(value, max_len=90)
    for index, value in enumerate((assets.get("descriptions") or [])[:5], start=1):
        row[f"Description {index}"] = _clean_cell(value, max_len=90)
    return row


def _search_theme_row(draft: AdDraft, group: dict[str, Any], theme: str) -> dict[str, str]:
    text = _clean_cell(theme, max_len=80)
    row = _blank_row()
    row.update(
        {
            "Campaign": _campaign_name(draft),
            "Asset Group": _asset_group_name(draft, group),
            "Search theme#Original": text,
            "Search theme": text,
            "Campaign Status": "Enabled",
            "Asset Group Status": "Enabled",
            "Status": "Enabled",
        }
    )
    return row


def _generated_asset_row(asset: GoogleAdsGeneratedAsset) -> dict[str, str]:
    payload = asset.payload_json if isinstance(asset.payload_json, dict) else {}
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    row = _blank_row()
    row["Campaign"] = _clean_cell(scope.get("campaign_name") or asset.campaign_name)
    row["Ad Group"] = _clean_cell(scope.get("ad_group_name") or "")
    row["Link source"] = "Advertiser"
    row["Source"] = "Advertiser"
    row["Upgraded extension"] = "[]"
    row["Campaign Status"] = "Enabled"
    row["Status"] = _status(asset.status)
    if row["Ad Group"]:
        row["Ad Group Status"] = "Enabled"
    if asset.asset_type == "sitelink":
        row["Final URL"] = _clean_cell(payload.get("final_url") or payload.get("url"))
        row["Link Text#Original"] = _clean_cell(payload.get("link_text") or asset.name, max_len=25)
        row["Link Text"] = row["Link Text#Original"]
        row["Description Line 1#Original"] = _clean_cell(payload.get("description1"), max_len=35)
        row["Description Line 1"] = row["Description Line 1#Original"]
        row["Description Line 2#Original"] = _clean_cell(payload.get("description2"), max_len=35)
        row["Description Line 2"] = row["Description Line 2#Original"]
    elif asset.asset_type == "callout":
        row["Callout text#Original"] = _clean_cell(payload.get("callout_text") or asset.name, max_len=25)
        row["Callout text"] = row["Callout text#Original"]
    elif asset.asset_type == "structured_snippet":
        row["Header#Original"] = _clean_cell(payload.get("header") or "Types", max_len=25)
        row["Header"] = row["Header#Original"]
        values = [_clean_cell(value, max_len=25) for value in payload.get("values") or [] if _clean_cell(value)]
        row["Snippet Values#Original"] = "\n".join(values[:10])
        row["Snippet Values"] = row["Snippet Values#Original"]
    elif asset.asset_type == "price":
        row["Type#Original"] = "Product categories"
        row["Type"] = "Product categories"
        row["Price qualifier"] = "From"
        row["Language#Original"] = _clean_cell(payload.get("language_code") or "en")
        row["Language"] = row["Language#Original"]
        offerings = [item for item in payload.get("price_offerings") or [] if isinstance(item, dict)]
        currency = _clean_cell(payload.get("currency_code") or (offerings[0].get("currency_code") if offerings else ""))
        row["Currency#Original"] = currency
        row["Currency"] = currency
        for index, item in enumerate(offerings[:8], start=1):
            row[f"Header {index}#Original"] = _clean_cell(item.get("header"), max_len=25)
            row[f"Header {index}"] = row[f"Header {index}#Original"]
            row[f"Description {index}#Original"] = _clean_cell(item.get("description"), max_len=25)
            row[f"Description {index}"] = row[f"Description {index}#Original"]
            row[f"Price {index}"] = f"{float(item.get('price') or 0):.2f}"
            row[f"Final URL {index}"] = _clean_cell(item.get("final_url") or item.get("url"))
    elif asset.asset_type == "business_name":
        text = _clean_cell(payload.get("business_name") or asset.name, max_len=25)
        row["Business name#Original"] = text
        row["Business name"] = text
    elif asset.asset_type == "headline":
        text = _clean_cell(payload.get("text") or asset.name, max_len=30)
        row["Headline 1#Original"] = text
        row["Headline 1"] = text
        row["Headline 1 position"] = " -"
    elif asset.asset_type == "description":
        text = _clean_cell(payload.get("text") or asset.name, max_len=90)
        row["Description 1#Original"] = text
        row["Description 1"] = text
        row["Description 1 position"] = " -"
    return row


def _iter_editor_rows(drafts: list[AdDraft], generated_assets: list[GoogleAdsGeneratedAsset]) -> Iterable[dict[str, str]]:
    emitted_campaigns: set[str] = set()
    for draft in drafts:
        campaign = _campaign_name(draft)
        if campaign not in emitted_campaigns:
            emitted_campaigns.add(campaign)
            yield _campaign_row(draft)
        if draft.ad_type in {"rsa", "dsa"}:
            yield _ad_group_row(draft)
            yield _rsa_ad_row(draft) if draft.ad_type == "rsa" else _dsa_ad_row(draft)
            for keyword in _keyword_terms(draft):
                yield _keyword_row(draft, keyword)
        if draft.ad_type == "pmax":
            groups = _assets(draft).get("pmax_asset_groups") if isinstance(_assets(draft).get("pmax_asset_groups"), list) else []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                yield _pmax_asset_group_row(draft, group)
                for theme in group.get("search_themes") or []:
                    yield _search_theme_row(draft, group, theme)
        yield from _negative_keyword_rows(draft)
        yield from _negative_url_rows(draft)
    for asset in generated_assets:
        row = _generated_asset_row(asset)
        if any(value for value in row.values()):
            yield row


def _tsv_bytes(headers: list[str], rows: list[dict[str, Any]], *, encoding: str = "utf-16") -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, delimiter="\t", extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue().encode(encoding)


def _simple_rows(editor_rows: list[dict[str, str]], columns: list[str], predicate) -> list[dict[str, str]]:
    return [{column: row.get(column, "") for column in columns} for row in editor_rows if predicate(row)]


def _zip_editor_bundle(
    account: GoogleAdsAccount,
    *,
    safe_suffix: str,
    main_file: str,
    editor_rows: list[dict[str, str]],
    by_kind: dict[str, list[dict[str, str]]],
    manifest: dict[str, Any],
) -> EditorExportBundle:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(main_file, _tsv_bytes(EDITOR_HEADERS, editor_rows))
        for name, rows in by_kind.items():
            headers = list(rows[0].keys()) if rows else ["Status"]
            archive.writestr(name, _tsv_bytes(headers, rows))
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", account.name).strip("-") or account.customer_id
    return EditorExportBundle(
        filename=f"{safe_name}-{safe_suffix}-{date.today().isoformat()}.zip",
        content_type="application/zip",
        payload=zip_buffer.getvalue(),
        manifest=manifest,
    )


def build_google_ads_editor_export_bundle(session: Session, account: GoogleAdsAccount) -> EditorExportBundle:
    drafts = session.scalars(
        select(AdDraft)
        .where(AdDraft.account_id == account.id, AdDraft.generated_assets != {})
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
    ).all()
    generated_assets = session.scalars(
        select(GoogleAdsGeneratedAsset)
        .where(GoogleAdsGeneratedAsset.account_id == account.id)
        .order_by(GoogleAdsGeneratedAsset.asset_type, GoogleAdsGeneratedAsset.id)
    ).all()
    editor_rows = list(_iter_editor_rows(list(drafts), list(generated_assets)))
    by_kind = {
        "campaigns.tsv": _simple_rows(editor_rows, ["Campaign", "Campaign Type", "Budget", "Bid Strategy Type", "Target ROAS", "Maximum CPC bid limit", "Campaign Status"], lambda row: bool(row.get("Campaign Type"))),
        "ad_groups.tsv": _simple_rows(editor_rows, ["Campaign", "Ad Group", "Max CPC", "Ad Group Type", "Ad Group Status"], lambda row: bool(row.get("Ad Group Type"))),
        "ads.tsv": _simple_rows(editor_rows, ["Campaign", "Ad Group", "Ad type", "Final URL", *[f"Headline {i}" for i in range(1, 16)], *[f"Description {i}" for i in range(1, 5)]], lambda row: bool(row.get("Ad type"))),
        "keywords.tsv": _simple_rows(editor_rows, ["Campaign", "Ad Group", "Criterion Type", "Keyword", "Status"], lambda row: bool(row.get("Keyword")) and not str(row.get("Criterion Type") or "").startswith("Campaign Negative")),
        "negative_keywords.tsv": _simple_rows(editor_rows, ["Campaign", "Criterion Type", "Keyword", "Status"], lambda row: bool(row.get("Keyword")) and str(row.get("Criterion Type") or "").startswith("Campaign Negative")),
        "pmax_asset_groups.tsv": _simple_rows(editor_rows, ["Campaign", "Asset Group", "Business name", "Final URL", "Asset Group Status"], lambda row: bool(row.get("Asset Group")) and any(row.get(f"Headline {i}") for i in range(1, 16))),
        "pmax_search_themes.tsv": _simple_rows(editor_rows, ["Campaign", "Asset Group", "Search theme", "Status"], lambda row: bool(row.get("Search theme"))),
        "assets.tsv": _simple_rows(editor_rows, ["Campaign", "Ad Group", "Business name", "Headline 1", "Description 1", "Link Text", "Callout text", "Header", "Snippet Values", "Type", "Currency", "Status"], lambda row: bool(row.get("Business name#Original") or row.get("Headline 1#Original") or row.get("Description 1#Original") or row.get("Link Text") or row.get("Callout text") or row.get("Snippet Values") or row.get("Type"))),
    }
    manifest = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "account_name": account.name,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "format": "Google Ads Editor UTF-16 tab-delimited import bundle",
        "main_file": "google_ads_editor_import.tsv",
        "editor_row_count": len(editor_rows),
        "draft_count": len(drafts),
        "generated_asset_count": len(generated_assets),
        "files": {name: len(rows) for name, rows in by_kind.items()},
        "notes": [
            "Upload google_ads_editor_import.tsv into Google Ads Editor for manual quota fallback.",
            "The split TSV files are audit/helper files for reviewing specific entity groups.",
            "Rows use campaign/ad group/asset group names and #Original columns where available so Editor can update matching entities.",
        ],
    }
    return _zip_editor_bundle(
        account,
        safe_suffix="google-ads-editor-export",
        main_file="google_ads_editor_import.tsv",
        editor_rows=editor_rows,
        by_kind=by_kind,
        manifest=manifest,
    )


def build_google_ads_editor_dynamic_search_bundle(session: Session, account: GoogleAdsAccount) -> EditorExportBundle:
    drafts = session.scalars(
        select(AdDraft)
        .where(AdDraft.account_id == account.id, AdDraft.generated_assets != {})
        .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
    ).all()
    editor_rows = list(_iter_dynamic_search_editor_rows(session, account, list(drafts)))
    by_kind = {
        "dynamic_search_campaigns.tsv": _simple_rows(
            editor_rows,
            [
                "Campaign",
                "Campaign Type",
                "Networks",
                "Budget",
                "Bid Strategy Type",
                "Target ROAS",
                "DSA Website",
                "DSA Language",
                "DSA targeting source",
                "AI Max",
                "Text customization",
                "Final URL expansion",
                "Campaign Status",
            ],
            lambda row: bool(row.get("Campaign Type")),
        ),
        "dynamic_search_ad_groups.tsv": _simple_rows(
            editor_rows,
            ["Campaign", "Ad Group", "Ad Group Type", "Search term matching", "Ad Group Status"],
            lambda row: bool(row.get("Ad Group Type")),
        ),
        "dynamic_search_url_inclusions.tsv": _simple_rows(
            editor_rows,
            ["Campaign", "Ad Group", "URL rule condition 1", "URL rule value 1", "Status"],
            lambda row: bool(row.get("URL rule value 1")),
        ),
        "dynamic_search_ads.tsv": _simple_rows(
            editor_rows,
            [
                "Campaign",
                "Ad Group",
                "Ad type",
                "Final URL",
                "Description Line 1",
                "Description Line 2",
                "Business name",
                *[f"Headline {i}" for i in range(1, 7)],
                *[f"Description {i}" for i in range(1, 3)],
                "Status",
            ],
            lambda row: bool(row.get("Ad type")),
        ),
    }
    manifest = {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "account_name": account.name,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "format": "Google Ads Editor UTF-16 tab-delimited AI Max Dynamic Search import bundle",
        "main_file": "dynamic_search_ai_max_editor_import.tsv",
        "editor_row_count": len(editor_rows),
        "draft_count": len(drafts),
        "url_inclusion_count": len(by_kind["dynamic_search_url_inclusions.tsv"]),
        "notes": [
            "Google Ads API v24 rejects new Dynamic Search Ad creation with ENUM_VALUE_NOT_PERMITTED.",
            "This bundle mirrors the current Google Ads Editor Dynamic Search/AI Max format for manual quota fallback.",
            "For API publishing, automation should use AI Max Search campaigns plus ad group WEBPAGE URL inclusions and RSA ads.",
        ],
    }
    return _zip_editor_bundle(
        account,
        safe_suffix="dynamic-search-ai-max-editor-export",
        main_file="dynamic_search_ai_max_editor_import.tsv",
        editor_rows=editor_rows,
        by_kind=by_kind,
        manifest=manifest,
    )
