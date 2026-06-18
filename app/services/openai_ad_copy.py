from __future__ import annotations

import json
import re
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool


ASSET_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "rsa": {
        "headlines": {"min": 3, "max": 15, "limit": 30, "target": 15},
        "descriptions": {"min": 2, "max": 4, "limit": 90, "target": 4},
    },
    "pmax": {
        "headlines": {"min": 3, "max": 15, "limit": 30, "target": 15},
        "long_headlines": {"min": 1, "max": 5, "limit": 90, "target": 5},
        "descriptions": {"min": 2, "max": 5, "limit": 90, "target": 5},
        "business_name": {"max": 25},
    },
    "dsa": {
        "headlines": {"min": 0, "max": 0, "limit": 0, "target": 0},
        "descriptions": {"min": 2, "max": 2, "limit": 90, "target": 2},
    },
}

BAD_GENERIC_ASSETS = {
    "best selling products",
    "buy from the brand",
    "easy online ordering",
    "fast delivery",
    "fresh deals online",
    "great value deals",
    "limited time offers",
    "premium selection",
    "save on your order",
    "secure checkout",
    "shop online today",
    "top rated range",
    "trusted quality",
}

DEFAULT_OPENAI_COPY_TIMEOUT_SECONDS = 12

STOPWORDS = {
    "about",
    "account",
    "after",
    "also",
    "available",
    "based",
    "because",
    "before",
    "best",
    "brand",
    "brands",
    "cart",
    "checkout",
    "click",
    "collection",
    "contact",
    "cookies",
    "australia",
    "australian",
    "canada",
    "canadian",
    "czech",
    "customer",
    "customers",
    "danish",
    "denmark",
    "delivery",
    "details",
    "email",
    "english",
    "europe",
    "european",
    "every",
    "find",
    "france",
    "french",
    "from",
    "germany",
    "german",
    "great",
    "home",
    "india",
    "indian",
    "information",
    "ireland",
    "irish",
    "latest",
    "learn",
    "login",
    "more",
    "online",
    "order",
    "page",
    "policy",
    "privacy",
    "product",
    "products",
    "quality",
    "range",
    "read",
    "republic",
    "reviews",
    "search",
    "secure",
    "shipping",
    "shop",
    "shopping",
    "store",
    "terms",
    "today",
    "trusted",
    "united",
    "using",
    "view",
    "with",
    "your",
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)


@lru_cache(maxsize=128)
def fetch_website_text(url: str) -> str:
    response = requests.get(url, timeout=(3, 4), headers={"User-Agent": "AdsManagerBot/1.0"})
    response.raise_for_status()
    parser = TextExtractor()
    parser.feed(response.text[:300000])
    text = " ".join(parser.parts)
    return re.sub(r"\s+", " ", text).strip()[:12000]


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.strip("\"'`")
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut[:limit].rstrip(" ,.;:")


def _unique(items: list[str], limit: int, count: int) -> list[str]:
    if count <= 0:
        return []
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        value = _trim(item, limit)
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
        if len(output) >= count:
            break
    return output


def _domain_brand(website_url: str) -> str:
    host = urlparse(website_url).netloc.lower().split(":")[0]
    host = host.removeprefix("www.")
    first = host.split(".")[0] if host else "Store"
    words = [word for word in re.split(r"[-_]+", first) if word]
    return _trim(" ".join(word.capitalize() for word in words) or "Store", 25)


def _candidate_terms(site_text: str, brand: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9&+-]{2,}", site_text.lower())
    counts: dict[str, int] = {}
    brand_words = {word.lower() for word in re.findall(r"[A-Za-z0-9]+", brand)}
    for word in words:
        clean = word.strip("-+&")
        if len(clean) < 4 or clean in STOPWORDS or clean in brand_words:
            continue
        if clean.isdigit():
            continue
        counts[clean] = counts.get(clean, 0) + 1
    terms = [word.title() for word, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]]
    return terms or ["Wellness", "Supplements", "Nutrition", "Health"]


def _generic_page_feed_terms(site_text: str, brand: str) -> list[str]:
    text = str(site_text or "").lower()
    candidates = [
        ("Supplements", ("supplement", "capsule", "tablet", "powder")),
        ("Vitamins", ("vitamin", "mineral")),
        ("Wellness", ("wellness", "health")),
        ("Nutrition", ("nutrition", "nutritional")),
        ("Health Products", ("health", "personal care")),
    ]
    terms = [label for label, needles in candidates if any(needle in text for needle in needles)]
    for fallback in ["Supplements", "Vitamins", "Wellness", "Nutrition", "Health Products"]:
        if fallback not in terms:
            terms.append(fallback)
    brand_words = {word.lower() for word in re.findall(r"[A-Za-z0-9]+", brand)}
    return [term for term in terms if term.lower() not in brand_words][:5]


def _geo_suffix(website_url: str) -> str:
    host = urlparse(website_url).netloc.lower().split(":")[0]
    host = host.removeprefix("www.")
    if host.endswith((".co.uk", ".uk")):
        return "UK"
    if host.endswith(".com.au") or host.endswith(".au"):
        return "AU"
    if host.endswith(".ca"):
        return "CA"
    if host.endswith(".co.nz") or host.endswith(".nz"):
        return "NZ"
    if host.endswith(".ie"):
        return "IE"
    if host.endswith(".in"):
        return "IN"
    if host.endswith(".com"):
        return "Online"
    return "Online"


def _asset_ok(value: str, source_terms: list[str], brand: str) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]+", "", value.lower()).strip()
    if normalized in BAD_GENERIC_ASSETS:
        return False
    source_lc = {term.lower() for term in source_terms}
    brand_lc = brand.lower()
    if normalized.startswith(("best ", "trusted ", "premium ", "top rated")):
        return any(term in normalized for term in source_lc) or brand_lc in normalized
    return True


def _generic_page_feed_assets(ad_type: str, business_name: str, website_url: str, site_text: str = "") -> dict[str, Any]:
    brand = _trim(business_name or _domain_brand(website_url), 25)
    terms = _generic_page_feed_terms(site_text, brand)
    suffix = _geo_suffix(website_url)
    generic_heads = [
        f"Vitamins & Supplements {suffix}",
        "Health Supplements Online",
        f"Buy Wellness Products {suffix}",
        "Nutrition Products Online",
        "Daily Vitamins & Minerals",
        f"Sports Nutrition Store {suffix}",
        f"Shop Supplements Online {suffix}",
        f"Online Supplement Store {suffix}",
        f"Wellness Products Online {suffix}",
        f"Health & Nutrition Store {suffix}",
        f"Order Vitamins Online {suffix}",
        "Supplement Products Online",
        f"Vitamins Minerals Store {suffix}",
        "Shop Health Products Online",
        "Nutrition & Wellness Store",
        f"Buy Supplements Online {suffix}",
        f"Health Products Online {suffix}",
        "Minerals & Vitamins Online",
        "Health & Wellness",
        f"{brand} Health Store",
    ]
    descriptions = [
        f"Shop {brand} for vitamins, supplements, minerals and wellness products online today.",
        "Browse health supplements, vitamins, minerals and daily wellness products online.",
        "Find nutrition, wellness and supplement products with clear details before ordering.",
        f"Order vitamins, minerals, supplements and health products online from {brand}.",
        "Explore supplement, wellness and nutrition essentials for everyday health needs.",
    ]
    assets = {
        "headlines": _quality_unique(generic_heads, 30, 15, terms, brand),
        "descriptions": _quality_unique(descriptions, 90, 5, terms, brand),
        "final_url": website_url,
        "ai_mode_default": True,
        "source_terms": terms[:8],
        "copy_strategy": {
            "mode": "generic_page_feed",
            "reason": "Page-feed campaigns keep visible ad assets generic while Google uses URL/page signals for product relevance.",
        },
    }
    if ad_type == "pmax":
        assets["long_headlines"] = _quality_unique(
            [
                f"Shop health, wellness, vitamins and supplement products online from {brand}",
                f"Browse vitamins, supplements, nutrition and wellness products online at {brand}",
                f"Find health supplements, vitamins, minerals and wellness products from {brand}",
                f"Order health, nutrition and supplement products online from the {brand} store",
                "Explore vitamins, minerals, wellness products and nutrition essentials online",
            ],
            90,
            5,
            terms,
            brand,
        )
        assets["business_name"] = brand
    if ad_type == "dsa":
        assets["headlines"] = []
        assets["descriptions"] = assets["descriptions"][:2]
    if ad_type == "rsa":
        assets["descriptions"] = assets["descriptions"][:4]
    return assets


def _quality_unique(items: list[str], limit: int, count: int, source_terms: list[str], brand: str) -> list[str]:
    filtered = [item for item in items if _asset_ok(str(item), source_terms, brand)]
    return _unique(filtered, limit, count)


def _fallback_assets(
    ad_type: str,
    business_name: str,
    website_url: str,
    site_text: str = "",
    *,
    generic_page_feed_copy: bool = False,
) -> dict[str, Any]:
    if generic_page_feed_copy:
        return _generic_page_feed_assets(ad_type, business_name, website_url, site_text)
    brand = _trim(business_name or _domain_brand(website_url), 25)
    terms = _candidate_terms(site_text, brand)
    primary = terms[0]
    secondary = terms[1] if len(terms) > 1 else terms[0]
    tertiary = terms[2] if len(terms) > 2 else terms[0]
    generic_heads = [
        f"{brand} {primary}",
        f"Shop {primary}",
        f"Buy {primary} Online",
        f"{primary} Deals",
        f"{primary} Delivered",
        f"{brand} {secondary}",
        f"Shop {secondary}",
        f"Buy {secondary}",
        f"{secondary} Offers",
        f"{secondary} Online",
        f"{brand} {tertiary}",
        f"Order {tertiary}",
        f"{tertiary} Deals",
        f"Shop {brand}",
        f"Buy From {brand}",
        f"{brand} Store",
        f"{primary} For You",
        f"Explore {secondary}",
    ]
    descriptions = [
        f"Shop {brand} for {primary.lower()} and {secondary.lower()} with a simple checkout.",
        f"Find {primary.lower()} options from {brand} and order online in minutes.",
        f"Explore {secondary.lower()} and {tertiary.lower()} products on the {brand} website.",
        f"Buy {primary.lower()} online from {brand} with clear product details.",
        f"Choose {brand} for {primary.lower()} products matched to your needs.",
    ]
    assets = {
        "headlines": _quality_unique(generic_heads, 30, 15, terms, brand),
        "descriptions": _quality_unique(descriptions, 90, 5, terms, brand),
        "final_url": website_url,
        "ai_mode_default": True,
        "source_terms": terms[:8],
    }
    if ad_type == "pmax":
        assets["long_headlines"] = _quality_unique(
            [
                f"Shop {brand} online for {primary.lower()} and {secondary.lower()}",
                f"Explore {primary.lower()} products and order direct from {brand}",
                f"Find {secondary.lower()} and {tertiary.lower()} options on {brand}",
                f"Buy {primary.lower()} from {brand} with clear product details",
                f"Discover {brand} products for {primary.lower()} buyers",
            ],
            90,
            5,
            terms,
            brand,
        )
        assets["business_name"] = brand
    if ad_type == "dsa":
        assets["descriptions"] = assets["descriptions"][:2]
    if ad_type == "rsa":
        assets["descriptions"] = assets["descriptions"][:4]
    return assets


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _normalise_assets(
    ad_type: str,
    raw: dict[str, Any],
    business_name: str,
    website_url: str,
    ai_mode: bool,
    site_text: str = "",
    *,
    generic_page_feed_copy: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fallback = _fallback_assets(
        ad_type,
        business_name,
        website_url,
        site_text,
        generic_page_feed_copy=generic_page_feed_copy,
    )
    brand = _trim(raw.get("business_name") or business_name or fallback.get("business_name") or _domain_brand(website_url), 25)
    source_terms = list(fallback.get("source_terms") or [])
    if not generic_page_feed_copy:
        source_terms = list(raw.get("source_terms") or source_terms or _candidate_terms(site_text, brand))
    headlines = list(fallback["headlines"] if generic_page_feed_copy else raw.get("headlines") or fallback["headlines"])
    descriptions = list(
        fallback["descriptions"] if generic_page_feed_copy else raw.get("descriptions") or fallback["descriptions"]
    )
    assets: dict[str, Any] = {
        "headlines": _quality_unique(
            headlines + fallback["headlines"],
            30,
            ASSET_REQUIREMENTS[ad_type]["headlines"]["target"],
            source_terms,
            brand,
        ),
        "descriptions": _quality_unique(
            descriptions + fallback["descriptions"],
            90,
            ASSET_REQUIREMENTS[ad_type]["descriptions"]["target"],
            source_terms,
            brand,
        ),
        "final_url": raw.get("final_url") or website_url,
        "ai_mode_default": bool(ai_mode),
        "asset_requirements": ASSET_REQUIREMENTS[ad_type],
        "source_terms": source_terms[:8],
        "google_ai_settings": {
            "final_url_expansion": bool(ai_mode),
            "text_asset_automation": bool(ai_mode),
            "ai_max_requested": bool(ai_mode and ad_type in {"rsa", "dsa"}),
        },
    }
    if fallback.get("copy_strategy"):
        assets["copy_strategy"] = fallback["copy_strategy"]
    if ad_type == "pmax":
        assets["long_headlines"] = _quality_unique(
            fallback["long_headlines"]
            if generic_page_feed_copy
            else list(raw.get("long_headlines") or []) + fallback["long_headlines"],
            90,
            ASSET_REQUIREMENTS[ad_type]["long_headlines"]["target"],
            source_terms,
            brand,
        )
        assets["business_name"] = brand
    validation = validate_assets(ad_type, assets)
    return assets, validation


def validate_assets(ad_type: str, assets: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    headlines = assets.get("headlines") or []
    descriptions = assets.get("descriptions") or []
    requirements = ASSET_REQUIREMENTS[ad_type]
    if ad_type == "rsa":
        if len(headlines) != requirements["headlines"]["target"]:
            errors.append("RSA drafts must contain exactly 15 headlines in this app.")
        if len(descriptions) != requirements["descriptions"]["target"]:
            errors.append("RSA drafts must contain exactly 4 descriptions in this app.")
    if ad_type == "dsa":
        if len(descriptions) != requirements["descriptions"]["target"]:
            errors.append("Dynamic Search Ads must contain exactly 2 descriptions in this app.")
        if headlines:
            errors.append("DSA drafts must not store headlines because Google dynamically generates them.")
    if ad_type == "pmax":
        if len(headlines) != requirements["headlines"]["target"]:
            errors.append("PMax drafts must contain exactly 15 headlines in this app.")
        if len(assets.get("long_headlines") or []) != requirements["long_headlines"]["target"]:
            errors.append("PMax drafts must contain exactly 5 long headlines in this app.")
        if len(descriptions) != requirements["descriptions"]["target"]:
            errors.append("PMax drafts must contain exactly 5 descriptions in this app.")
        warnings.append("PMax live creation also requires image/logo assets; this draft stores the text assets first.")
    for headline in headlines:
        if len(headline) > 30:
            errors.append(f"Headline exceeds 30 chars: {headline}")
        if headline.lower() in BAD_GENERIC_ASSETS:
            errors.append(f"Headline is too generic: {headline}")
    for description in descriptions:
        if len(description) > 90:
            errors.append(f"Description exceeds 90 chars: {description}")
    for long_headline in assets.get("long_headlines") or []:
        if len(long_headline) > 90:
            errors.append(f"Long headline exceeds 90 chars: {long_headline}")
    if len(str(assets.get("business_name", ""))) > 25:
        errors.append("Business name exceeds 25 chars.")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "requirements": requirements,
    }


def generate_ad_copy(
    session: Session,
    *,
    ad_type: str,
    website_url: str,
    business_name: str = "",
    generic_page_feed_copy: bool = False,
    keyword_terms: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    settings = get_sync_setting_map(session)
    api_key = str(settings.get("openai.api_key") or "")
    model = str(settings.get("openai.model") or "gpt-5.2")
    ai_mode = parse_bool(settings.get("ad_factory.ai_mode_default", True))
    try:
        openai_timeout = int(settings.get("openai.ad_copy_timeout_seconds") or DEFAULT_OPENAI_COPY_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        openai_timeout = DEFAULT_OPENAI_COPY_TIMEOUT_SECONDS
    openai_timeout = max(3, min(openai_timeout, 20))
    site_text = ""
    try:
        site_text = fetch_website_text(website_url)
    except Exception:
        site_text = ""

    page_feed_instruction = ""
    if generic_page_feed_copy:
        page_feed_instruction = (
            "This is a multi-product page-feed campaign. Keep all visible ad copy generic to the "
            "store/category type. Do not use individual product names, SKU/code values, dosage, pack size, "
            "exact variants, or one product URL as the theme. Google/page-feed automation will handle "
            "product-level relevance from the submitted URLs. Use broad search keywords where relevant: "
            "vitamins, supplements, minerals, nutrition, wellness, health products, sports nutrition, "
            "online supplement store, and wellness products. "
        )
    keyword_terms = [str(term).strip() for term in (keyword_terms or []) if str(term).strip()]
    keyword_instruction = ""
    if keyword_terms and ad_type == "rsa":
        keyword_instruction = (
            "This is a keyword-only RSA draft using saved Google Ads search-term data. "
            "Use these Google-derived keyword themes as the keyword source for the RSA and do not use "
            "Odoo page-feed products as ad themes. Supplied keyword themes: "
            f"{', '.join(keyword_terms[:30])}. "
        )
    elif keyword_terms and ad_type == "pmax":
        keyword_instruction = (
            "This is a Performance Max draft using saved Google Ads insight terms as search-theme candidates. "
            "Use the supplied terms to shape the asset language, but keep visible copy natural and policy-safe. "
            "Return source_terms as the strongest deduped search-theme candidates only. Supplied PMax terms: "
            f"{', '.join(keyword_terms[:25])}. "
        )
    phrase_instruction = (
        "Use broad store/category phrases from the website text. "
        if generic_page_feed_copy
        else "Use specific product/category phrases from the website text. "
    )
    prompt = (
        "Generate Google Ads copy as strict JSON only. "
        "No markdown. No emojis. No unverifiable claims. No generic filler like 'trusted quality', "
        "'premium selection', 'top rated range', or 'best selling products' unless the website text proves it. "
        f"{page_feed_instruction}"
        f"{keyword_instruction}"
        f"{phrase_instruction}Prefer clear purchase-intent copy, "
        "brand terms, category terms, delivery/offer terms only when present, and short standalone assets "
        "that can be mixed in any order. Avoid repeated wording across headlines. "
        "Aim to use most of each Google character limit without padding: headlines should usually be "
        "24-30 characters, descriptions 80-90 characters, and PMax long_headlines 75-90 characters. "
        "Respect Google limits exactly: headlines <=30 chars, descriptions <=90 chars, "
        "PMax long_headlines <=90 chars, business_name <=25 chars. "
        "For RSA JSON must include exactly 15 headlines and 4 descriptions. "
        "For PMax JSON must include exactly 15 headlines, 5 long_headlines, 5 descriptions, and business_name. "
        "For DSA JSON must include exactly 2 descriptions and no headlines because Google dynamically generates headlines. "
        "Return JSON keys only: headlines, long_headlines, descriptions, business_name, source_terms. "
        f"Ad type: {ad_type}. Business name: {business_name}. URL: {website_url}. "
        f"Website text: {site_text[:9000]}"
    )
    if generic_page_feed_copy or not api_key:
        raw = {"source_terms": keyword_terms} if keyword_terms else {}
        assets, validation = _normalise_assets(
            ad_type,
            raw,
            business_name,
            website_url,
            ai_mode,
            site_text,
            generic_page_feed_copy=generic_page_feed_copy,
        )
        return assets, validation, prompt

    payload = {
        "model": model,
        "input": prompt,
        "text": {"format": {"type": "json_object"}},
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=openai_timeout,
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("output_text") or ""
        if not text:
            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        text += content.get("text", "")
        raw = _extract_json(text)
    except Exception:
        raw = {}
    if keyword_terms and ad_type in {"rsa", "pmax"}:
        raw_terms = [str(term).strip() for term in (raw.get("source_terms") or []) if str(term).strip()]
        raw["source_terms"] = list(dict.fromkeys(keyword_terms + raw_terms))
    assets, validation = _normalise_assets(ad_type, raw, business_name, website_url, ai_mode, site_text)
    return assets, validation, prompt
