from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import signal
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v24.errors.types.errors import GoogleAdsFailure
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.protobuf.json_format import MessageToDict
import requests
from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import (
    AdDraft,
    AppSetting,
    AutoPilotEvent,
    GoogleAdsAccount,
    GoogleAdsAutomationPreference,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.google_ads_account_red_flags import account_api_red_flag
from app.services.google_ads_api_errors import summarize_google_ads_exception
from app.services.google_ads_browser_automation import generate_browser_automation_tasks
from app.services.google_ads_landing_page_bank import usable_landing_page_url
from app.services.google_ads_pmax_gate import pmax_activation_gate
from app.services.google_ads_sync import build_client, enum_name
from app.services.page_feed_restrictions import normalize_restricted_text


LIVE_PUBLISH_STATUSES = {
    "ready_for_review",
    "publish_ready",
    "publish_blocked",
    "needs_review",
    "approved",
    "published_enabled",
    "published_paused",
}
SUPPORTED_AD_TYPES = {"dsa", "rsa", "pmax"}
GOOGLE_ADS_API_QUOTA_PREFIX = "google_ads_asset_publish_quota"
DSA_DISABLED_SETTING_PREFIX = "live_campaign_creator.dsa_disabled"
LIVE_CREATION_QUOTA_COOLDOWN_PREFIX = "live_campaign_creator.quota_cooldown"
CRITERIA_DAILY_ITEM_LIMIT_SETTING = "live_campaign_creator.criteria_daily_item_limit"
CRITERIA_DAILY_ITEM_STATE_PREFIX = "live_campaign_creator.criteria_daily_items"
DEFAULT_CRITERIA_DAILY_ITEM_LIMIT = 1000
LIVE_CAMPAIGN_PUBLISH_BATCH_LIMIT = 50
URL_INCLUSION_MUTATION_BATCH_LIMIT = 100
KEYWORD_MUTATION_BATCH_LIMIT = 100
LIVE_CREATION_SUCCESS_STATUSES = {"validated", "published_enabled", "published_paused", "resumed_existing"}
LIVE_PUBLISH_AD_TYPE_PRIORITY = {"dsa": 0, "rsa": 1, "pmax": 2}
FORCE_MINIMUM_BUDGET_SETTING = "automation.force_minimum_budget_when_budget_guard_blocked"
DEFER_PMAX_SIGNAL_MUTATIONS_SETTING = "live_campaign_creator.defer_pmax_signal_mutations"
DEFER_PMAX_SEARCH_THEME_MUTATIONS_SETTING = "live_campaign_creator.defer_pmax_search_theme_mutations"
DEFER_PMAX_AUDIENCE_SIGNAL_MUTATIONS_SETTING = "live_campaign_creator.defer_pmax_audience_signal_mutations"
PMAX_MEDIA_FIELD_TYPES = {
    "MARKETING_IMAGE": 3,
    "SQUARE_MARKETING_IMAGE": 3,
    "PORTRAIT_MARKETING_IMAGE": 1,
    "LOGO": 1,
    "YOUTUBE_VIDEO": 1,
}
PMAX_ASSET_GROUP_FINAL_URL_LIMIT = 0
PMAX_SEARCH_THEME_SIGNAL_LIMIT = 50
RESTRICTED_SHARED_SET_NAME = "restricted"
RESTRICTED_TERMS_SETTING_KEY = "page_feed.restricted_title_terms"
MAX_DRAFT_ASSET_JSON_REWRITE_BYTES = 1_000_000
LEGACY_PMAX_SCALE_SOURCE_SUFFIX = "pmax_scale_after_7d_conversions"
REPLACEMENT_PMAX_SCALE_SOURCE_SUFFIX = "pmax_scale_after_7d_conversions_s001"
CORE_SCALE_TARGET_ROAS = 6.67
TESTING_DISCOVERY_TARGET_ROAS = 5.0
FIX_WATCH_TARGET_ROAS = 3.5
WASTE_RECOVERY_TARGET_ROAS = 5.0
MINIMUM_DAILY_BUDGET_BY_CURRENCY = {
    "INR": 1000.0,
}
COUNTRY_TARGET_SETTING_PREFIX = "google_ads_account_country_target"
COUNTRY_TARGETS = {
    "AR": {"name": "Argentina", "geo_target_constant": "geoTargetConstants/2032"},
    "AT": {"name": "Austria", "geo_target_constant": "geoTargetConstants/2040"},
    "AU": {"name": "Australia", "geo_target_constant": "geoTargetConstants/2036"},
    "BE": {"name": "Belgium", "geo_target_constant": "geoTargetConstants/2056"},
    "BR": {"name": "Brazil", "geo_target_constant": "geoTargetConstants/2076"},
    "CA": {"name": "Canada", "geo_target_constant": "geoTargetConstants/2124"},
    "CH": {"name": "Switzerland", "geo_target_constant": "geoTargetConstants/2756"},
    "CL": {"name": "Chile", "geo_target_constant": "geoTargetConstants/2152"},
    "CN": {"name": "China", "geo_target_constant": "geoTargetConstants/2156"},
    "CY": {"name": "Cyprus", "geo_target_constant": "geoTargetConstants/2196"},
    "CZ": {"name": "Czechia", "geo_target_constant": "geoTargetConstants/2203"},
    "DE": {"name": "Germany", "geo_target_constant": "geoTargetConstants/2276"},
    "DK": {"name": "Denmark", "geo_target_constant": "geoTargetConstants/2208"},
    "EE": {"name": "Estonia", "geo_target_constant": "geoTargetConstants/2233"},
    "ES": {"name": "Spain", "geo_target_constant": "geoTargetConstants/2724"},
    "FI": {"name": "Finland", "geo_target_constant": "geoTargetConstants/2246"},
    "FR": {"name": "France", "geo_target_constant": "geoTargetConstants/2250"},
    "GB": {"name": "United Kingdom", "geo_target_constant": "geoTargetConstants/2826"},
    "GR": {"name": "Greece", "geo_target_constant": "geoTargetConstants/2300"},
    "ID": {"name": "Indonesia", "geo_target_constant": "geoTargetConstants/2360"},
    "IL": {"name": "Israel", "geo_target_constant": "geoTargetConstants/2376"},
    "IN": {"name": "India", "geo_target_constant": "geoTargetConstants/2356"},
    "IT": {"name": "Italy", "geo_target_constant": "geoTargetConstants/2380"},
    "JP": {"name": "Japan", "geo_target_constant": "geoTargetConstants/2392"},
    "KR": {"name": "South Korea", "geo_target_constant": "geoTargetConstants/2410"},
    "LA": {"name": "Laos", "geo_target_constant": "geoTargetConstants/2418"},
    "LT": {"name": "Lithuania", "geo_target_constant": "geoTargetConstants/2440"},
    "LU": {"name": "Luxembourg", "geo_target_constant": "geoTargetConstants/2442"},
    "LV": {"name": "Latvia", "geo_target_constant": "geoTargetConstants/2428"},
    "MX": {"name": "Mexico", "geo_target_constant": "geoTargetConstants/2484"},
    "MY": {"name": "Malaysia", "geo_target_constant": "geoTargetConstants/2458"},
    "NL": {"name": "Netherlands", "geo_target_constant": "geoTargetConstants/2528"},
    "NO": {"name": "Norway", "geo_target_constant": "geoTargetConstants/2578"},
    "NZ": {"name": "New Zealand", "geo_target_constant": "geoTargetConstants/2554"},
    "PL": {"name": "Poland", "geo_target_constant": "geoTargetConstants/2616"},
    "RO": {"name": "Romania", "geo_target_constant": "geoTargetConstants/2642"},
    "SE": {"name": "Sweden", "geo_target_constant": "geoTargetConstants/2752"},
    "SG": {"name": "Singapore", "geo_target_constant": "geoTargetConstants/2702"},
    "SI": {"name": "Slovenia", "geo_target_constant": "geoTargetConstants/2705"},
    "TR": {"name": "Turkiye", "geo_target_constant": "geoTargetConstants/2792"},
    "TW": {"name": "Taiwan", "geo_target_constant": "geoTargetConstants/2158"},
    "US": {"name": "United States", "geo_target_constant": "geoTargetConstants/2840"},
    "UY": {"name": "Uruguay", "geo_target_constant": "geoTargetConstants/2858"},
    "ZA": {"name": "South Africa", "geo_target_constant": "geoTargetConstants/2710"},
}
CURRENCY_DEFAULT_COUNTRY = {
    "AUD": "AU",
    "CAD": "CA",
    "CZK": "CZ",
    "GBP": "GB",
    "INR": "IN",
    "JPY": "JP",
    "NZD": "NZ",
    "USD": "US",
}
INTERNAL_PUBLIC_COPY_TERMS = {
    "conversion signal",
    "google conversion signal",
    "odoo sales signal",
    "popular recent seller",
    "strong margin signal",
    "useful product page",
    "real customer demand",
    "demand signal",
    "saved signal",
    "sales signal",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gaql_string(value: str) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def _criteria_daily_state_key(account: GoogleAdsAccount, now: Optional[datetime] = None) -> str:
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    day = current.astimezone(timezone.utc).date().isoformat()
    customer_id = str(getattr(account, "customer_id", "") or getattr(account, "id", "") or "unknown")
    return f"{CRITERIA_DAILY_ITEM_STATE_PREFIX}.{customer_id}.{day}"


def _parse_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 1)


def _session_info(session: Any) -> dict[str, Any]:
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        return info
    return {}


def _criteria_daily_item_limit(session: Session) -> int:
    info = _session_info(session)
    cached = info.get("live_criteria_daily_item_limit")
    if cached is not None:
        return _parse_positive_int(cached, DEFAULT_CRITERIA_DAILY_ITEM_LIMIT)
    if not hasattr(session, "scalar"):
        return DEFAULT_CRITERIA_DAILY_ITEM_LIMIT
    setting = session.scalar(select(AppSetting).where(AppSetting.key == CRITERIA_DAILY_ITEM_LIMIT_SETTING))
    limit = _parse_positive_int(setting.value if setting is not None else None, DEFAULT_CRITERIA_DAILY_ITEM_LIMIT)
    info["live_criteria_daily_item_limit"] = limit
    return limit


def _criteria_daily_scope_count(session: Session) -> int:
    return _parse_positive_int(_session_info(session).get("live_criteria_scope_count"), 1)


def _criteria_daily_reservation_plan(
    state: dict[str, Any],
    *,
    limit: int,
    scope_count: int,
    scope_key: str,
    kind: str,
    requested: int,
    now: Optional[datetime] = None,
) -> tuple[int, dict[str, Any]]:
    requested = max(int(requested or 0), 0)
    limit = _parse_positive_int(limit, DEFAULT_CRITERIA_DAILY_ITEM_LIMIT)
    scope_count = _parse_positive_int(scope_count, 1)
    next_state = dict(state or {})
    scopes = dict(next_state.get("scopes") or {})
    used_items = max(int(next_state.get("used_items") or 0), 0)
    remaining = max(limit - used_items, 0)
    scope_key = str(scope_key or kind or "criteria")
    scope_state = dict(scopes.get(scope_key) or {})
    scope_used = max(int(scope_state.get("used_items") or 0), 0)
    per_scope_cap = max(1, math.ceil(limit / scope_count))
    scope_remaining = max(per_scope_cap - scope_used, 0)
    allowed = min(requested, remaining, scope_remaining)
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    scopes[scope_key] = {
        **scope_state,
        "kind": kind,
        "used_items": scope_used + allowed,
        "last_requested_items": requested,
        "last_allowed_items": allowed,
        "updated_at": current.isoformat(),
    }
    next_state.update(
        {
            "limit": limit,
            "scope_count": scope_count,
            "used_items": used_items + allowed,
            "remaining_items": max(limit - used_items - allowed, 0),
            "scopes": scopes,
            "updated_at": current.isoformat(),
        }
    )
    return allowed, next_state


def _reserve_daily_criteria_items(
    session: Session,
    account: GoogleAdsAccount,
    *,
    kind: str,
    scope_key: str,
    requested: int,
    validate_only: bool,
) -> dict[str, Any]:
    requested = max(int(requested or 0), 0)
    if requested <= 0:
        return {
            "kind": kind,
            "scope_key": scope_key,
            "requested_items": 0,
            "allowed_items": 0,
            "daily_deferred_items": 0,
            "remaining_items": 0,
        }
    if validate_only:
        return {
            "kind": kind,
            "scope_key": scope_key,
            "requested_items": requested,
            "allowed_items": requested,
            "daily_deferred_items": 0,
            "remaining_items": None,
            "validate_only": True,
        }
    if not hasattr(session, "execute") or not hasattr(session, "scalar"):
        return {
            "kind": kind,
            "scope_key": scope_key,
            "requested_items": requested,
            "allowed_items": requested,
            "daily_deferred_items": 0,
            "remaining_items": None,
            "non_db_session": True,
        }
    state_key = _criteria_daily_state_key(account)
    lock_name = f"{CRITERIA_DAILY_ITEM_STATE_PREFIX}.{getattr(account, 'customer_id', '') or getattr(account, 'id', '')}"
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"), {"lock_name": lock_name})
    setting = session.scalar(select(AppSetting).where(AppSetting.key == state_key).with_for_update())
    state = setting.value if setting is not None and isinstance(setting.value, dict) else {}
    limit = _criteria_daily_item_limit(session)
    allowed, next_state = _criteria_daily_reservation_plan(
        state,
        limit=limit,
        scope_count=_criteria_daily_scope_count(session),
        scope_key=scope_key,
        kind=kind,
        requested=requested,
    )
    if setting is None:
        setting = AppSetting(
            key=state_key,
            value=next_state,
            category="Automation pacing",
            label=f"Daily criteria pacing {getattr(account, 'customer_id', '') or getattr(account, 'id', '')}",
            help_text="Tracks daily Google Ads criteria additions so first-run keyword and URL sync does not exhaust API quota.",
            input_type="json",
            sensitive=False,
        )
        session.add(setting)
    else:
        setting.value = next_state
    session.flush()
    return {
        "kind": kind,
        "scope_key": scope_key,
        "requested_items": requested,
        "allowed_items": allowed,
        "daily_deferred_items": max(requested - allowed, 0),
        "limit": limit,
        "scope_count": _criteria_daily_scope_count(session),
        "used_items": int(next_state.get("used_items") or 0),
        "remaining_items": int(next_state.get("remaining_items") or 0),
        "state_key": state_key,
    }


def _limit_daily_criteria_items(
    session: Session,
    account: GoogleAdsAccount,
    *,
    kind: str,
    scope_key: str,
    items: list[Any],
    validate_only: bool,
) -> tuple[list[Any], dict[str, Any]]:
    reservation = _reserve_daily_criteria_items(
        session,
        account,
        kind=kind,
        scope_key=scope_key,
        requested=len(items),
        validate_only=validate_only,
    )
    allowed = int(reservation.get("allowed_items") or 0)
    return items[:allowed], reservation


def _google_ads_search(service: Any, account: GoogleAdsAccount, query: str, *, timeout: int = 8) -> Any:
    try:
        return service.search(customer_id=account.customer_id, query=query, timeout=timeout)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return service.search(customer_id=account.customer_id, query=query)


def _is_transient_concurrent_modification_error(exc: Any) -> bool:
    text = str(exc or "").lower()
    return "concurrent_modification" in text or "modify the same resource at once" in text


def _google_ads_mutate(service: Any, method_name: str, request: Any, *, timeout: int = 30) -> Any:
    method = getattr(service, method_name)
    attempts = 3
    for attempt in range(attempts):
        try:
            return method(request=request, timeout=timeout)
        except TypeError as exc:
            if "timeout" not in str(exc):
                raise
            return method(request=request)
        except GoogleAdsException as exc:
            if attempt >= attempts - 1 or not _is_transient_concurrent_modification_error(exc):
                raise
            time.sleep(5 * (attempt + 1))
    return method(request=request, timeout=timeout)


def _sync_setting_value(session: Session, key: str, default: Any = None) -> Any:
    try:
        value = session.scalar(select(AppSetting.value).where(AppSetting.key == key).limit(1))
    except Exception:
        return default
    return default if value is None else value


def root_domain(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    host = (parsed.netloc or parsed.path).split("/")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _country_from_domain(value: str) -> str:
    host = root_domain(value)
    if not host:
        return ""
    suffixes = [
        (".com.au", "AU"),
        (".com.br", "BR"),
        (".co.nz", "NZ"),
        (".co.it", "IT"),
        (".co.il", "IL"),
        (".co.za", "ZA"),
        (".co.uk", "GB"),
        (".co.in", "IN"),
        (".ca", "CA"),
        (".ch", "CH"),
        (".au", "AU"),
        (".nz", "NZ"),
        (".uk", "GB"),
        (".in", "IN"),
        (".ar", "AR"),
        (".at", "AT"),
        (".be", "BE"),
        (".br", "BR"),
        (".cl", "CL"),
        (".cn", "CN"),
        (".cy", "CY"),
        (".cz", "CZ"),
        (".de", "DE"),
        (".dk", "DK"),
        (".ee", "EE"),
        (".es", "ES"),
        (".fi", "FI"),
        (".fr", "FR"),
        (".gr", "GR"),
        (".id", "ID"),
        (".il", "IL"),
        (".it", "IT"),
        (".jp", "JP"),
        (".kr", "KR"),
        (".la", "LA"),
        (".lt", "LT"),
        (".lu", "LU"),
        (".lv", "LV"),
        (".mx", "MX"),
        (".my", "MY"),
        (".nl", "NL"),
        (".no", "NO"),
        (".pl", "PL"),
        (".ro", "RO"),
        (".se", "SE"),
        (".sg", "SG"),
        (".si", "SI"),
        (".tr", "TR"),
        (".tw", "TW"),
        (".us", "US"),
        (".uy", "UY"),
        (".za", "ZA"),
    ]
    for suffix, country_code in suffixes:
        if host.endswith(suffix):
            return country_code
    return ""


def _country_from_text(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    if not text:
        return ""
    tokens = set(text.split())
    phrase_map = [
        ("czech republic", "CZ"),
        ("united kingdom", "GB"),
        ("new zealand", "NZ"),
        ("united states", "US"),
        ("south africa", "ZA"),
        ("south korea", "KR"),
        ("australia", "AU"),
        ("argentina", "AR"),
        ("austria", "AT"),
        ("belgium", "BE"),
        ("brazil", "BR"),
        ("canada", "CA"),
        ("chile", "CL"),
        ("china", "CN"),
        ("cyprus", "CY"),
        ("denmark", "DK"),
        ("estonia", "EE"),
        ("finland", "FI"),
        ("france", "FR"),
        ("germany", "DE"),
        ("greece", "GR"),
        ("indonesia", "ID"),
        ("india", "IN"),
        ("israel", "IL"),
        ("italy", "IT"),
        ("japan", "JP"),
        ("laos", "LA"),
        ("latvia", "LV"),
        ("lithuania", "LT"),
        ("luxembourg", "LU"),
        ("malaysia", "MY"),
        ("mexico", "MX"),
        ("netherlands", "NL"),
        ("norway", "NO"),
        ("poland", "PL"),
        ("romania", "RO"),
        ("singapore", "SG"),
        ("slovenia", "SI"),
        ("spain", "ES"),
        ("sweden", "SE"),
        ("switzerland", "CH"),
        ("taiwan", "TW"),
        ("turkey", "TR"),
        ("turkiye", "TR"),
        ("uruguay", "UY"),
    ]
    for phrase, country_code in phrase_map:
        if phrase in text:
            return country_code
    token_map = {
        "au": "AU",
        "aud": "AU",
        "australia": "AU",
        "ar": "AR",
        "argentina": "AR",
        "at": "AT",
        "austria": "AT",
        "be": "BE",
        "belgium": "BE",
        "br": "BR",
        "brazil": "BR",
        "ca": "CA",
        "cad": "CA",
        "canada": "CA",
        "ch": "CH",
        "switzerland": "CH",
        "cl": "CL",
        "chile": "CL",
        "cn": "CN",
        "china": "CN",
        "cy": "CY",
        "cyprus": "CY",
        "cz": "CZ",
        "czech": "CZ",
        "de": "DE",
        "germany": "DE",
        "dk": "DK",
        "denmark": "DK",
        "ee": "EE",
        "estonia": "EE",
        "es": "ES",
        "spain": "ES",
        "fi": "FI",
        "finland": "FI",
        "fr": "FR",
        "france": "FR",
        "gb": "GB",
        "uk": "GB",
        "gr": "GR",
        "greece": "GR",
        "id": "ID",
        "indonesia": "ID",
        "il": "IL",
        "israel": "IL",
        "in": "IN",
        "inr": "IN",
        "india": "IN",
        "it": "IT",
        "italy": "IT",
        "jp": "JP",
        "japan": "JP",
        "kr": "KR",
        "korea": "KR",
        "la": "LA",
        "laos": "LA",
        "lt": "LT",
        "lithuania": "LT",
        "lu": "LU",
        "luxembourg": "LU",
        "lv": "LV",
        "latvia": "LV",
        "mx": "MX",
        "mexico": "MX",
        "my": "MY",
        "malaysia": "MY",
        "nl": "NL",
        "netherlands": "NL",
        "no": "NO",
        "norway": "NO",
        "nz": "NZ",
        "nzd": "NZ",
        "pl": "PL",
        "poland": "PL",
        "ro": "RO",
        "romania": "RO",
        "se": "SE",
        "sweden": "SE",
        "sg": "SG",
        "singapore": "SG",
        "si": "SI",
        "slovenia": "SI",
        "tr": "TR",
        "turkey": "TR",
        "turkiye": "TR",
        "tw": "TW",
        "taiwan": "TW",
        "us": "US",
        "usa": "US",
        "usd": "US",
        "uy": "UY",
        "uruguay": "UY",
        "za": "ZA",
    }
    for token in tokens:
        if token in token_map:
            return token_map[token]
    return ""


def _country_target_payload(country_code: str, *, source: str = "", domain: str = "") -> dict[str, str]:
    code = str(country_code or "").upper()
    target = COUNTRY_TARGETS.get(code)
    if not target:
        return {}
    return {
        "country_code": code,
        "country_name": str(target["name"]),
        "geo_target_constant": str(target["geo_target_constant"]),
        "source": source,
        "domain": domain,
    }


def _record_account_country_target(
    session: Session,
    account: GoogleAdsAccount,
    target: dict[str, str],
) -> None:
    if not target:
        return
    key = f"{COUNTRY_TARGET_SETTING_PREFIX}.{account.customer_id}"
    value = {
        "customer_id": account.customer_id,
        "account_id": account.id,
        **target,
        "updated_at": utcnow().isoformat(),
    }
    try:
        setting = session.scalar(select(AppSetting).where(AppSetting.key == key))
    except Exception:
        return
    if setting is None:
        session.add(
            AppSetting(
                key=key,
                value=value,
                category="google_ads_automation",
                label=f"{account.name} default country target",
                help_text="Resolved from mapped Odoo website/domain/account signals and used for AUTO campaign targeting.",
                input_type="json",
                sensitive=False,
            )
        )
    else:
        setting.value = value
        setting.updated_at = utcnow()


def resolve_account_country_target(
    session: Session,
    account: GoogleAdsAccount,
    *,
    final_url: str = "",
) -> dict[str, str]:
    candidates: list[tuple[float, str, str, str]] = []
    if final_url:
        candidates.append((1000.0, _country_from_domain(final_url), "draft_final_url", root_domain(final_url)))
    try:
        mappings = session.scalars(
            select(OdooStoreGoogleAdsMapping)
            .where(
                OdooStoreGoogleAdsMapping.account_id == account.id,
                OdooStoreGoogleAdsMapping.is_active.is_(True),
            )
            .order_by(OdooStoreGoogleAdsMapping.revenue_weight.desc())
        ).all()
    except Exception:
        mappings = []
    for mapping in mappings:
        website = None
        try:
            website = session.scalar(
                select(OdooWebsite).where(
                    OdooWebsite.store_id == mapping.store_id,
                    OdooWebsite.website_id == mapping.website_id,
                    OdooWebsite.is_active.is_(True),
                )
            )
        except Exception:
            website = None
        domain = str(getattr(website, "domain", "") or getattr(mapping.store, "base_url", "") or "")
        candidates.append((float(mapping.revenue_weight or 1.0) * 100.0, _country_from_domain(domain), "odoo_website_domain", root_domain(domain)))
        candidates.append((float(mapping.revenue_weight or 1.0) * 50.0, _country_from_text(getattr(website, "name", "")), "odoo_website_name", root_domain(domain)))
    candidates.append((10.0, _country_from_text(account.name), "account_name", ""))
    candidates.append((5.0, CURRENCY_DEFAULT_COUNTRY.get(str(account.currency_code or "").upper(), ""), "account_currency", ""))
    for _score, country_code, source, domain in sorted(candidates, key=lambda item: item[0], reverse=True):
        payload = _country_target_payload(country_code, source=source, domain=domain)
        if payload:
            _record_account_country_target(session, account, payload)
            return payload
    return {}


def _micros(amount: float) -> int:
    return max(int(round(float(amount or 0) * 1_000_000)), 1_000_000)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _currency_minimum_daily_budget(account: GoogleAdsAccount | None, configured: Any = None) -> float:
    try:
        configured_amount = max(float(configured if configured is not None else 0.0), 0.0)
    except (TypeError, ValueError):
        configured_amount = 0.0
    currency_code = str(getattr(account, "currency_code", "") or "").upper()
    currency_floor = MINIMUM_DAILY_BUDGET_BY_CURRENCY.get(currency_code, 10.0)
    return max(configured_amount, currency_floor)


def _budget_block_result(
    session: Session,
    draft: AdDraft,
    *,
    ad_type: str,
    bidding: dict[str, Any],
    preference: GoogleAdsAutomationPreference,
) -> Optional[dict[str, Any]]:
    daily_budget = _float_value(bidding.get("daily_budget"), 0.0)
    budget_blocked = bool(bidding.get("budget_blocked"))
    force_minimum = parse_bool(_sync_setting_value(session, FORCE_MINIMUM_BUDGET_SETTING, False))
    if force_minimum:
        return None
    if not budget_blocked and daily_budget > 0:
        return None
    return {
        "draft_id": draft.id,
        "ad_type": ad_type,
        "status": "blocked_by_budget_guard",
        "reason": str(
            bidding.get("budget_block_reason")
            or "Automation budget is blocked by the Odoo sales guard or missing a positive daily budget."
        ),
        "budget_blocked": budget_blocked,
        "requested_daily_budget": daily_budget,
        "minimum_daily_budget_amount": _currency_minimum_daily_budget(getattr(preference, "account", None), preference.minimum_daily_budget_amount),
    }


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    return cleaned[:limit]


def _valid_exact_keyword(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).lower()
    if len(cleaned) < 2 or len(cleaned) > 80:
        return False
    if cleaned in {"standard delivery", "standard delivery (canada)", "shipping", "delivery"}:
        return False
    return True


def _keyword_key(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _clean_search_theme(text: str) -> str:
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in str(text or ""))
    return _clip(" ".join(cleaned.split()[:10]), 80)


def _call_with_alarm(func: Any, *, seconds: int, label: str) -> Any:
    if threading.current_thread() is not threading.main_thread():
        return func()

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"{label} timed out after {seconds} seconds.")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(max(int(seconds), 1))
    try:
        return func()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _contains_shipping_offer_text(text: Any) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    offer_words = ("free delivery", "free shipping", "delivery over", "shipping over", "standard delivery")
    if any(word in normalized for word in offer_words):
        return True
    if ("delivery" in normalized or "shipping" in normalized) and re.search(r"(?:ca\$|a\$|au\$|usd|cad|aud|£|€|\$)\s*\d", normalized):
        return True
    return False


def _title_from_theme(text: Any, *, limit: int = 30) -> str:
    words = _clean_search_theme(str(text or "")).split()
    small_words = {"and", "or", "of", "for", "with", "in", "to"}
    titled = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if index and lowered in small_words:
            titled.append(lowered)
        elif lowered in {"usa", "ca", "uk", "dhea", "dha", "epa", "b12", "d3", "k2"}:
            titled.append(lowered.upper())
        else:
            titled.append(lowered.capitalize())
    return _clip(" ".join(titled), limit)


def _append_unique_copy(values: list[str], value: Any, *, limit: int, max_items: int) -> None:
    if len(values) >= max_items:
        return
    text = _clip(str(value or ""), limit)
    normalized = normalize_restricted_text(text)
    if (
        not text
        or _contains_shipping_offer_text(text)
        or any(term in normalized for term in INTERNAL_PUBLIC_COPY_TERMS)
    ):
        return
    key = text.lower()
    if key not in {item.lower() for item in values}:
        values.append(text)


def _pmax_asset_group_copy_assets(
    assets: dict[str, Any],
    account: GoogleAdsAccount,
    group: dict[str, Any],
) -> dict[str, Any]:
    raw_themes = [
        _clean_search_theme(str(item or ""))
        for item in (group.get("search_themes") or assets.get("pmax_search_themes") or assets.get("source_terms") or [])
        if str(item or "").strip()
    ]
    themes = _dedupe_texts(raw_themes, limit=80, max_items=8)
    headlines: list[str] = []
    long_headlines: list[str] = []
    descriptions: list[str] = []

    for theme in themes[:5]:
        title = _title_from_theme(theme, limit=28)
        if not title:
            continue
        _append_unique_copy(headlines, title, limit=30, max_items=15)
        _append_unique_copy(headlines, f"Shop {title}", limit=30, max_items=15)
        _append_unique_copy(headlines, f"{title} Online", limit=30, max_items=15)
        _append_unique_copy(long_headlines, f"Shop {title} with trusted wellness brands online", limit=90, max_items=5)
        _append_unique_copy(descriptions, f"Find {title.lower()} and related wellness essentials from trusted brands.", limit=90, max_items=5)

    for headline in assets.get("headlines") or []:
        _append_unique_copy(headlines, headline, limit=30, max_items=15)
    for headline in assets.get("long_headlines") or []:
        _append_unique_copy(long_headlines, headline, limit=90, max_items=5)
    for description in assets.get("descriptions") or []:
        _append_unique_copy(descriptions, description, limit=90, max_items=5)

    business_name = _clip(str(assets.get("business_name") or account.name or "Business"), 25) or "Business"
    for fallback in (
        "Vitamins and Supplements",
        "Trusted Wellness Brands",
        "Shop Wellness Online",
        "Quality Supplements",
        "Personal Care Essentials",
        "Made In USA Supplements",
    ):
        _append_unique_copy(headlines, fallback, limit=30, max_items=15)
    for fallback in (
        "Shop vitamins, supplements and personal care essentials online.",
        "Explore trusted wellness products from reliable online store pages.",
        "Find quality supplements, beauty and health essentials in one store.",
    ):
        _append_unique_copy(descriptions, fallback, limit=90, max_items=5)
    for fallback in (
        "Shop trusted vitamins, supplements and personal care essentials online",
        "Find wellness products from quality brands across the online store",
    ):
        _append_unique_copy(long_headlines, fallback, limit=90, max_items=5)

    return {
        "headlines": headlines[:15],
        "long_headlines": long_headlines[:5],
        "descriptions": descriptions[:5],
        "business_name": business_name,
        "search_theme_inputs": themes,
        "shipping_offer_text_filtered": sum(
            1
            for value in (
                list(assets.get("headlines") or [])
                + list(assets.get("long_headlines") or [])
                + list(assets.get("descriptions") or [])
            )
            if _contains_shipping_offer_text(value)
        ),
    }


def _resource_id(resource_name: str) -> Optional[int]:
    try:
        return int(str(resource_name).rsplit("/", 1)[-1])
    except Exception:  # noqa: BLE001 - best effort for Google resource names.
        return None


def _partial_failure_operation_indexes(response: Any) -> set[int]:
    partial_failure = getattr(response, "partial_failure_error", None)
    if partial_failure is None or not getattr(partial_failure, "code", 0):
        return set()
    failed_indexes: set[int] = set()
    for detail in getattr(partial_failure, "details", []) or []:
        try:
            failure = GoogleAdsFailure.deserialize(detail.value)
        except Exception:  # noqa: BLE001 - Google may return a plain status message.
            continue
        for error in getattr(failure, "errors", []) or []:
            for element in getattr(error.location, "field_path_elements", []) or []:
                field_name = str(getattr(element, "field_name", "") or "")
                if field_name in {"operations", "mutate_operations"}:
                    index = getattr(element, "index", None)
                    if index is not None:
                        failed_indexes.add(int(index))
    return failed_indexes


def _successful_resource_names(response: Any) -> list[str]:
    failed_indexes = _partial_failure_operation_indexes(response)
    resources: list[str] = []
    for index, result in enumerate(getattr(response, "results", []) or []):
        if index in failed_indexes:
            continue
        resource_name = str(getattr(result, "resource_name", "") or "")
        if resource_name:
            resources.append(resource_name)
    return resources


def _rest_partial_failure_operation_indexes(payload: dict[str, Any]) -> set[int]:
    failed_indexes: set[int] = set()
    error = payload.get("partialFailureError")
    if not isinstance(error, dict):
        return failed_indexes
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for item in ((detail.get("errors") if isinstance(detail.get("errors"), list) else []) or []):
            location = item.get("location") if isinstance(item, dict) else {}
            for element in (location.get("fieldPathElements") or []):
                if element.get("fieldName") in {"operations", "mutate_operations"} and "index" in element:
                    try:
                        failed_indexes.add(int(element["index"]))
                    except (TypeError, ValueError):
                        pass
    return failed_indexes


def _google_ads_rest_headers(client: Any) -> dict[str, str]:
    credentials = client.credentials
    base_request = GoogleAuthRequest()

    def refresh_request(url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: int = 120, **kwargs: Any) -> Any:
        return base_request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=min(int(timeout or 15), 15),
            **kwargs,
        )

    if not getattr(credentials, "valid", False) or getattr(credentials, "expired", False):
        credentials.refresh(refresh_request)
    return {
        "Authorization": f"Bearer {credentials.token}",
        "developer-token": str(client.developer_token),
        "login-customer-id": str(client.login_customer_id),
        "Content-Type": "application/json",
    }


def _mutate_search_theme_signals_rest(
    client: Any,
    account: GoogleAdsAccount,
    *,
    operations: list[dict[str, Any]],
    validate_only: bool,
) -> list[str]:
    if not operations:
        return []
    url = f"https://googleads.googleapis.com/v24/customers/{account.customer_id}/googleAds:mutate"
    response = requests.post(
        url,
        headers=_google_ads_rest_headers(client),
        json={
            "mutateOperations": operations,
            "partialFailure": True,
            "validateOnly": validate_only,
        },
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Google Ads REST signal mutate returned non-JSON status {response.status_code}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Google Ads REST signal mutate failed with status {response.status_code}: {str(payload)[:500]}")
    failed_indexes = _rest_partial_failure_operation_indexes(payload)
    resources: list[str] = []
    for index, item in enumerate(payload.get("mutateOperationResponses") or []):
        if index in failed_indexes or not isinstance(item, dict):
            continue
        result = item.get("assetGroupSignalResult") or {}
        resource_name = str(result.get("resourceName") or "")
        if resource_name:
            resources.append(resource_name)
    return resources


def _draft_identity(draft: AdDraft) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
    return identity


def _draft_automation(draft: AdDraft) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    return automation


def _campaign_by_code(client: Any, account: GoogleAdsAccount, campaign_code: str) -> Optional[dict[str, Any]]:
    if not campaign_code:
        return None
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type
        FROM campaign
        WHERE campaign.name LIKE {gaql_string('%' + campaign_code + '%')}
          AND campaign.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "campaign_id": int(row.campaign.id),
            "campaign_name": str(row.campaign.name or ""),
            "campaign_resource_name": str(row.campaign.resource_name or ""),
            "campaign_status": enum_name(row.campaign.status),
            "channel_type": enum_name(row.campaign.advertising_channel_type),
            "matched_by": "campaign_code",
            "campaign_code": campaign_code,
        }
    return None


def _campaign_lane_prefix(campaign_name: str) -> str:
    name = re.sub(r"\s+", " ", str(campaign_name or "").strip())
    return re.sub(r"\s*\|\s*AUTO-[A-F0-9]{10}\s*$", "", name, flags=re.I).strip()


def _campaign_code_from_name(campaign_name: str) -> str:
    match = re.search(r"\bAUTO-[A-F0-9]{10}\b", str(campaign_name or "").upper())
    return match.group(0) if match else ""


def _campaign_by_lane_name(client: Any, account: GoogleAdsAccount, campaign_name: str) -> Optional[dict[str, Any]]:
    lane_prefix = _campaign_lane_prefix(campaign_name)
    if not lane_prefix or not lane_prefix.startswith("AUTO |") or not hasattr(client, "get_service"):
        return None
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.name LIKE {gaql_string(lane_prefix + ' | AUTO-%')}
          AND campaign.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    candidates: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        candidates.append(
            {
                "campaign_id": int(row.campaign.id),
                "campaign_name": str(row.campaign.name or ""),
                "campaign_resource_name": str(row.campaign.resource_name or ""),
                "campaign_status": enum_name(row.campaign.status),
                "channel_type": enum_name(row.campaign.advertising_channel_type),
                "matched_by": "campaign_lane_live_fallback",
                "campaign_code": _campaign_code_from_name(str(row.campaign.name or "")),
                "budget_amount_micros": int(row.campaign_budget.amount_micros or 0),
            }
        )
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            str(item.get("campaign_status") or "").upper() == "ENABLED",
            int(item.get("budget_amount_micros") or 0),
            int(item.get("campaign_id") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _campaign_by_code_or_lane(
    client: Any,
    account: GoogleAdsAccount,
    campaign_code: str,
    campaign_name: str,
) -> Optional[dict[str, Any]]:
    campaign = _campaign_by_code(client, account, campaign_code)
    if campaign is not None:
        return campaign
    return _campaign_by_lane_name(client, account, campaign_name)


def _max_cpc_bid_limit_for_account(account: GoogleAdsAccount) -> float:
    currency = str(account.currency_code or "").upper()
    if currency == "INR":
        return 300.0
    if currency in {"AUD", "CAD", "USD"}:
        return 2.5
    return 2.0


def _automation_campaign_revision_plan(name: str) -> Optional[dict[str, Any]]:
    if not str(name or "").startswith("AUTO |"):
        return None
    lowered = str(name or "").lower()
    is_core_scale = "core / scale" in lowered
    is_testing = "testing / discovery" in lowered
    is_fix_watch = "fix / watch" in lowered
    is_waste_recovery = "waste / recovery" in lowered
    ai_max_plan = {
        "enable_ai_max": True,
        "ai_max_text_automation": True,
        "ai_max_final_url_expansion": not is_core_scale,
    }
    if "| pmax " in lowered or " pmax " in lowered:
        if is_core_scale:
            return {"strategy": "target_roas", "target_roas": CORE_SCALE_TARGET_ROAS, "lane": "core_pmax"}
        if is_testing:
            return {"strategy": "target_roas", "target_roas": TESTING_DISCOVERY_TARGET_ROAS, "lane": "testing_pmax"}
        return None
    if "| dsa " in lowered or " dsa " in lowered:
        if is_core_scale:
            return {
                **ai_max_plan,
                "strategy": "target_roas",
                "target_roas": CORE_SCALE_TARGET_ROAS,
                "lane": "core_dsa_ai_max",
            }
        if is_testing:
            return {
                **ai_max_plan,
                "strategy": "target_roas",
                "target_roas": TESTING_DISCOVERY_TARGET_ROAS,
                "lane": "testing_dsa_ai_max",
            }
        return None
    if "| rsa " not in lowered and " rsa " not in lowered:
        return None
    if "cpc cap" in lowered:
        return {**ai_max_plan, "strategy": "retired_max_clicks", "lane": "legacy_max_clicks_paused"}
    if is_core_scale:
        desired_name = re.sub(r"RSA Max Clicks Scale Keywords", "RSA Target ROAS Scale Keywords", str(name), flags=re.I)
        return {**ai_max_plan, "strategy": "target_roas", "target_roas": CORE_SCALE_TARGET_ROAS, "desired_name": desired_name, "lane": "core_rsa_target_roas"}
    if is_testing:
        desired_name = re.sub(r"RSA Max Clicks Keywords", "RSA Target ROAS Keywords", str(name), flags=re.I)
        return {**ai_max_plan, "strategy": "target_roas", "target_roas": TESTING_DISCOVERY_TARGET_ROAS, "desired_name": desired_name, "lane": "testing_rsa_target_roas"}
    if is_fix_watch:
        desired_name = re.sub(r"RSA Max Clicks Repair Keywords", "RSA Target ROAS Repair Keywords", str(name), flags=re.I)
        return {**ai_max_plan, "strategy": "target_roas", "target_roas": FIX_WATCH_TARGET_ROAS, "desired_name": desired_name, "lane": "fix_rsa_target_roas"}
    if is_waste_recovery:
        desired_name = re.sub(r"RSA Max Clicks Recovery Keywords", "RSA Target ROAS Recovery Keywords", str(name), flags=re.I)
        return {**ai_max_plan, "strategy": "target_roas", "target_roas": WASTE_RECOVERY_TARGET_ROAS, "desired_name": desired_name, "lane": "waste_rsa_target_roas"}
    return None


def _is_legacy_max_clicks_auto_campaign(campaign: dict[str, Any], plan: dict[str, Any] | None) -> bool:
    name = str(campaign.get("campaign_name") or "").lower()
    strategy = str(campaign.get("bidding_strategy_type") or "").upper()
    lane = str((plan or {}).get("lane") or "").lower()
    return (
        "auto |" in name
        and (
            "max clicks" in name
            or "cpc cap" in name
            or "max_clicks" in lane
            or strategy in {"TARGET_SPEND", "MAXIMIZE_CLICKS"}
        )
    )


def _legacy_max_clicks_paused_name(name: str) -> str:
    text = str(name or "").strip() or "AUTO | Legacy Max Clicks"
    text = re.sub(r"RSA Max Clicks CPC Cap", "RSA Legacy Paused", text, flags=re.I)
    text = re.sub(r"RSA Max Clicks", "RSA Legacy Paused", text, flags=re.I)
    if "legacy paused" not in text.lower():
        text = f"{text} Legacy Paused"
    return text[:255]


def _automation_campaign_revision_rows(client: Any, account: GoogleAdsAccount) -> list[dict[str, Any]]:
    query = """
        SELECT
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversion_value.target_roas,
          campaign.target_spend.cpc_bid_ceiling_micros,
          campaign.ai_max_setting.enable_ai_max,
          campaign.asset_automation_settings,
          campaign.network_settings.target_google_search,
          campaign.network_settings.target_search_network,
          campaign.network_settings.target_partner_search_network,
          campaign.network_settings.target_content_network
        FROM campaign
        WHERE campaign.name LIKE 'AUTO |%'
          AND campaign.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    rows: list[dict[str, Any]] = []
    for row in _google_ads_search(service, account, query):
        asset_automation_settings = {}
        for setting in row.campaign.asset_automation_settings:
            asset_automation_settings[enum_name(setting.asset_automation_type)] = enum_name(setting.asset_automation_status)
        rows.append(
            {
                "campaign_id": int(row.campaign.id or 0),
                "campaign_name": str(row.campaign.name or ""),
                "campaign_resource_name": str(row.campaign.resource_name or ""),
                "campaign_status": enum_name(row.campaign.status),
                "channel_type": enum_name(row.campaign.advertising_channel_type),
                "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
                "target_roas": float(row.campaign.maximize_conversion_value.target_roas or 0),
                "cpc_bid_ceiling_micros": int(row.campaign.target_spend.cpc_bid_ceiling_micros or 0),
                "enable_ai_max": bool(row.campaign.ai_max_setting.enable_ai_max),
                "asset_automation_settings": asset_automation_settings,
                "target_google_search": bool(row.campaign.network_settings.target_google_search),
                "target_search_network": bool(row.campaign.network_settings.target_search_network),
                "target_partner_search_network": bool(row.campaign.network_settings.target_partner_search_network),
                "target_content_network": bool(row.campaign.network_settings.target_content_network),
            }
        )
    return rows


def _apply_campaign_revision(
    client: Any,
    account: GoogleAdsAccount,
    campaign: dict[str, Any],
    plan: dict[str, Any],
    *,
    validate_only: bool,
) -> list[str]:
    resource_name = str(campaign.get("campaign_resource_name") or "")
    if not resource_name:
        return []
    operation = client.get_type("CampaignOperation")
    update = operation.update
    update.resource_name = resource_name
    changed: list[str] = []
    desired_name = str(plan.get("desired_name") or "").strip()
    current_name = str(campaign.get("campaign_name") or "")
    if desired_name and desired_name != current_name:
        update.name = desired_name[:255]
        operation.update_mask.paths.append("name")
        changed.append("name")
    if bool(plan.get("pause")) and str(campaign.get("campaign_status") or "").upper() != "PAUSED":
        update.status = client.enums.CampaignStatusEnum.PAUSED
        operation.update_mask.paths.append("status")
        changed.append("pause_duplicate")
    if str(campaign.get("channel_type") or "").upper() == "SEARCH":
        if (
            not bool(campaign.get("target_google_search"))
            or bool(campaign.get("target_search_network"))
            or bool(campaign.get("target_partner_search_network"))
            or bool(campaign.get("target_content_network"))
        ):
            update.network_settings.target_google_search = True
            update.network_settings.target_search_network = False
            update.network_settings.target_partner_search_network = False
            update.network_settings.target_content_network = False
            operation.update_mask.paths.append("network_settings.target_google_search")
            operation.update_mask.paths.append("network_settings.target_search_network")
            operation.update_mask.paths.append("network_settings.target_partner_search_network")
            operation.update_mask.paths.append("network_settings.target_content_network")
            changed.append("networks")
        if "enable_ai_max" in plan:
            try:
                enable_ai_max = bool(plan.get("enable_ai_max"))
                current_automation_settings = campaign.get("asset_automation_settings")
                if not isinstance(current_automation_settings, dict):
                    current_automation_settings = {}
                automation_preferences = []
                if enable_ai_max:
                    automation_preferences = [
                        ("TEXT_ASSET_AUTOMATION", client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION, bool(plan.get("ai_max_text_automation", True))),
                        (
                            "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION",
                            client.enums.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION,
                            bool(plan.get("ai_max_final_url_expansion", True)),
                        ),
                    ]
                elif bool(campaign.get("enable_ai_max")):
                    automation_preferences = [
                        ("TEXT_ASSET_AUTOMATION", client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION, False),
                        ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", client.enums.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION, False),
                    ]
                for automation_name, automation_type, enabled in automation_preferences:
                    desired_status = "OPTED_IN" if enabled else "OPTED_OUT"
                    if str(current_automation_settings.get(automation_name) or "").upper() != desired_status:
                        asset_automation_setting = client.get_type("Campaign.AssetAutomationSetting")
                        asset_automation_setting.asset_automation_type = automation_type
                        asset_automation_setting.asset_automation_status = (
                            client.enums.AssetAutomationStatusEnum.OPTED_IN
                            if enabled
                            else client.enums.AssetAutomationStatusEnum.OPTED_OUT
                        )
                        update.asset_automation_settings.append(asset_automation_setting)
                if len(update.asset_automation_settings):
                    operation.update_mask.paths.append("asset_automation_settings")
                if bool(campaign.get("enable_ai_max")) != enable_ai_max:
                    update.ai_max_setting.enable_ai_max = enable_ai_max
                    operation.update_mask.paths.append("ai_max_setting.enable_ai_max")
                if "asset_automation_settings" in operation.update_mask.paths or "ai_max_setting.enable_ai_max" in operation.update_mask.paths:
                    changed.append("ai_max")
            except Exception:  # noqa: BLE001 - older client/test doubles may not expose AI Max.
                pass
    strategy = str(plan.get("strategy") or "")
    if strategy == "target_roas":
        target_roas = float(plan.get("target_roas") or 0)
        if target_roas > 0:
            update.maximize_conversion_value.target_roas = target_roas
        else:
            maximize_conversion_value = client.get_type("MaximizeConversionValue")
            update._pb.maximize_conversion_value.CopyFrom(maximize_conversion_value._pb)
        operation.update_mask.paths.append("maximize_conversion_value.target_roas")
        changed.append("target_roas")
    if not operation.update_mask.paths:
        return []
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    return changed


def enforce_automation_campaign_revisions(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    account = preference.account
    if account is None:
        return {"name": "automation_campaign_revisions", "status": "skipped", "reason": "Missing Google Ads account."}
    if preference.monitor_only and not validate_only:
        return {"name": "automation_campaign_revisions", "status": "blocked", "reason": "Monitor-only is enabled."}
    allow_mutations = parse_bool(_sync_setting_value(session, "optimizer.allow_mutations", False))
    dry_run = parse_bool(_sync_setting_value(session, "optimizer.dry_run", True))
    if not validate_only and (not allow_mutations or dry_run):
        return {
            "name": "automation_campaign_revisions",
            "status": "blocked_by_mutation_guard",
            "reason": "Global mutation guards block live campaign revisions until Allow mutations is on and Dry run is off.",
            "guard": {
                "optimizer_allow_mutations": allow_mutations,
                "optimizer_dry_run": dry_run,
                "monitor_only": bool(preference.monitor_only),
                "validate_only": bool(validate_only),
            },
        }
    settings = get_sync_setting_map(session)
    client = build_client(settings, account.manager_customer_id, account.connection)
    rows = _automation_campaign_revision_rows(client, account)
    has_core_scale_s001_pmax = any(
        "core / scale" in str(row.get("campaign_name") or "").lower()
        and "pmax" in str(row.get("campaign_name") or "").lower()
        and re.search(r"\bS\d{3}\b", str(row.get("campaign_name") or ""))
        for row in rows
    )
    revised: list[dict[str, Any]] = []
    skipped = 0
    errors: list[dict[str, Any]] = []
    for campaign in rows:
        plan = _automation_campaign_revision_plan(str(campaign.get("campaign_name") or ""))
        if not plan:
            skipped += 1
            continue
        if _is_legacy_max_clicks_auto_campaign(campaign, plan):
            plan = {
                **plan,
                "lane": "legacy_max_clicks_paused",
                "pause": True,
                "desired_name": _legacy_max_clicks_paused_name(str(campaign.get("campaign_name") or "")),
            }
        campaign_name_lower = str(campaign.get("campaign_name") or "").lower()
        if (
            has_core_scale_s001_pmax
            and plan.get("lane") == "core_pmax"
            and "core / scale" in campaign_name_lower
            and "pmax" in campaign_name_lower
            and not re.search(r"\bS\d{3}\b", str(campaign.get("campaign_name") or ""))
        ):
            plan = {
                **plan,
                "lane": "legacy_core_pmax_duplicate",
                "pause": True,
                "desired_name": str(campaign.get("campaign_name") or "").replace("PMax Target ROAS Scale |", "PMax Target ROAS Scale Legacy Paused |"),
            }
        try:
            changed = _apply_campaign_revision(client, account, campaign, plan, validate_only=validate_only)
            country_target = resolve_account_country_target(session, account)
            location_result = _ensure_campaign_country_target(
                client,
                account,
                campaign_id=int(campaign.get("campaign_id") or 0),
                campaign_resource_name=str(campaign.get("campaign_resource_name") or ""),
                country_target=country_target,
                validate_only=validate_only,
            )
            if location_result.get("status") in {"updated", "validated"}:
                changed.append("country_target")
            revised.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "lane": plan.get("lane"),
                    "changes": changed,
                    "country_target": location_result,
                }
            )
        except GoogleAdsException as exc:
            errors.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "google_ads_error": summarize_google_ads_exception(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep revising other campaigns.
            errors.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "error": str(exc)[:500],
                }
            )
    return {
        "name": "automation_campaign_revisions",
        "status": "partial" if errors and revised else "failed" if errors else "done",
        "campaigns_seen": len(rows),
        "revised": len(revised),
        "skipped": skipped,
        "items": revised,
        "errors": errors,
    }


def pause_automation_campaigns_for_account(
    session: Session,
    account: GoogleAdsAccount,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    if account is None:
        return {"name": "pause_automation_campaigns", "status": "skipped", "reason": "Missing Google Ads account."}
    allow_mutations = parse_bool(_sync_setting_value(session, "optimizer.allow_mutations", False))
    dry_run = parse_bool(_sync_setting_value(session, "optimizer.dry_run", True))
    if not validate_only and (not allow_mutations or dry_run):
        return {
            "name": "pause_automation_campaigns",
            "status": "blocked_by_mutation_guard",
            "reason": "Global mutation guards block live campaign pausing until Allow mutations is on and Dry run is off.",
            "guard": {
                "optimizer_allow_mutations": allow_mutations,
                "optimizer_dry_run": dry_run,
                "validate_only": bool(validate_only),
            },
        }
    settings = get_sync_setting_map(session)
    client = build_client(settings, account.manager_customer_id, account.connection)
    rows = _automation_campaign_revision_rows(client, account)
    paused: list[dict[str, Any]] = []
    already_paused: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for campaign in rows:
        if str(campaign.get("campaign_status") or "").upper() == "PAUSED":
            already_paused.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "campaign_status": campaign.get("campaign_status"),
                }
            )
            continue
        try:
            resource_name = _pause_campaign_if_needed(client, account, campaign, validate_only=validate_only)
            paused.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "resource_name": resource_name,
                }
            )
        except GoogleAdsException as exc:
            errors.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "error": summarize_google_ads_exception(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "campaign_id": campaign.get("campaign_id"),
                    "campaign_name": campaign.get("campaign_name"),
                    "error": str(exc)[:500],
                }
            )
    status = "failed" if errors and not paused else "partial" if errors else "paused"
    if not rows:
        status = "skipped"
    return {
        "name": "pause_automation_campaigns",
        "status": status,
        "validate_only": bool(validate_only),
        "automation_campaign_count": len(rows),
        "paused_count": len(paused),
        "already_paused_count": len(already_paused),
        "error_count": len(errors),
        "paused": paused,
        "already_paused": already_paused,
        "errors": errors,
    }


def _ad_group_by_name(client: Any, account: GoogleAdsAccount, campaign_id: int, name: str) -> Optional[dict[str, Any]]:
    query = f"""
        SELECT
          ad_group.id,
          ad_group.name,
          ad_group.resource_name,
          ad_group.status,
          ad_group.type
        FROM ad_group
        WHERE campaign.id = {int(campaign_id)}
          AND ad_group.name = {gaql_string(name)}
          AND ad_group.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "ad_group_id": int(row.ad_group.id),
            "ad_group_name": str(row.ad_group.name or ""),
            "ad_group_resource_name": str(row.ad_group.resource_name or ""),
            "ad_group_status": enum_name(row.ad_group.status),
            "ad_group_type": enum_name(row.ad_group.type_),
            "matched_by": "ad_group_name",
        }
    return None


def _dsa_ad_by_ad_group(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> Optional[dict[str, Any]]:
    query = f"""
        SELECT
          ad_group_ad.resource_name,
          ad_group_ad.status,
          ad_group_ad.ad.id,
          ad_group_ad.ad.resource_name,
          ad_group_ad.ad.type
        FROM ad_group_ad
        WHERE ad_group.id = {int(ad_group_id)}
          AND ad_group_ad.status != REMOVED
          AND ad_group_ad.ad.type = EXPANDED_DYNAMIC_SEARCH_AD
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "ad_id": int(row.ad_group_ad.ad.id),
            "ad_resource_name": str(row.ad_group_ad.ad.resource_name or ""),
            "ad_group_ad_resource_name": str(row.ad_group_ad.resource_name or ""),
            "ad_status": enum_name(row.ad_group_ad.status),
            "ad_type": enum_name(row.ad_group_ad.ad.type_),
            "matched_by": "ad_group_dsa_ad",
        }
    return None


def _rsa_ad_by_ad_group(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> Optional[dict[str, Any]]:
    query = f"""
        SELECT
          ad_group_ad.resource_name,
          ad_group_ad.status,
          ad_group_ad.ad.id,
          ad_group_ad.ad.resource_name,
          ad_group_ad.ad.type
        FROM ad_group_ad
        WHERE ad_group.id = {int(ad_group_id)}
          AND ad_group_ad.status != REMOVED
          AND ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "ad_id": int(row.ad_group_ad.ad.id),
            "ad_resource_name": str(row.ad_group_ad.ad.resource_name or ""),
            "ad_group_ad_resource_name": str(row.ad_group_ad.resource_name or ""),
            "ad_status": enum_name(row.ad_group_ad.status),
            "ad_type": enum_name(row.ad_group_ad.ad.type_),
            "matched_by": "ad_group_rsa_ad",
        }
    return None


def _webpage_criterion_by_ad_group(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> Optional[dict[str, Any]]:
    query = f"""
        SELECT
          ad_group_criterion.criterion_id,
          ad_group_criterion.resource_name,
          ad_group_criterion.status,
          ad_group_criterion.webpage.criterion_name
        FROM ad_group_criterion
        WHERE ad_group.id = {int(ad_group_id)}
          AND ad_group_criterion.type = WEBPAGE
          AND ad_group_criterion.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "criterion_id": int(row.ad_group_criterion.criterion_id),
            "criterion_resource_name": str(row.ad_group_criterion.resource_name or ""),
            "criterion_status": enum_name(row.ad_group_criterion.status),
            "criterion_name": str(row.ad_group_criterion.webpage.criterion_name or ""),
            "matched_by": "ad_group_webpage_criterion",
        }
    return None


def _keyword_texts_from_draft(draft: AdDraft, *, limit: Optional[int] = None) -> list[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    rejected = {
        " ".join(str(term or "").split()).lower()
        for term in (assets.get("rejected_exact_keywords") or [])
        if str(term or "").strip()
    }
    clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
    terms: list[str] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        for term in cluster.get("exact_terms") or []:
            text = " ".join(str(term or "").split()).lower()
            if text in rejected:
                continue
            if not _valid_exact_keyword(text):
                continue
            if text and text not in terms:
                terms.append(text)
            if limit and len(terms) >= limit:
                return terms
    for term in assets.get("source_terms") or []:
        text = " ".join(str(term or "").split()).lower()
        if text in rejected:
            continue
        if not _valid_exact_keyword(text):
            continue
        if text and text not in terms:
            terms.append(text)
        if limit and len(terms) >= limit:
            return terms
    return terms


def _rsa_ad_group_name_from_assets(assets: dict[str, Any], campaign_code: str) -> str:
    clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        name = str(cluster.get("ad_group_name") or "").strip()
        if name:
            return name[:255]
    return f"AUTO | Testing / Discovery | RSA Keywords | {campaign_code}"


def _negative_keyword_texts_from_draft(draft: AdDraft, *, limit: int = 100) -> list[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
    terms: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            match_type = str(item.get("match_type") or "exact").strip().lower()
            if match_type and match_type != "exact":
                continue
            text = str(item.get("keyword") or item.get("text") or "").strip()
        else:
            text = str(item or "").strip()
        cleaned = _keyword_key(text)
        if not _valid_exact_keyword(cleaned) or cleaned in seen:
            continue
        seen.add(cleaned)
        terms.append(cleaned)
        if len(terms) >= limit:
            break
    return terms


def _negative_page_urls_from_draft(draft: AdDraft, *, limit: int = 100) -> list[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    values = assets.get("negative_page_targets") if isinstance(assets.get("negative_page_targets"), list) else []
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value or "").strip()
        if not url or url in seen:
            continue
        parsed = urlsplit(url if "://" in url else f"https://{url}")
        if not parsed.netloc:
            continue
        normalized = f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path}".rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) >= limit:
            break
    return urls


def dsa_disabled_setting_key(account_id: int) -> str:
    return f"{DSA_DISABLED_SETTING_PREFIX}:{int(account_id)}"


def is_dsa_live_disabled(session: Session, account_id: int) -> bool:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == dsa_disabled_setting_key(account_id)))
    return bool(setting and str(setting.value or "").lower() in {"1", "true", "yes", "on"})


def disable_dsa_live_creation(session: Session, account_id: int, reason: str) -> None:
    key = dsa_disabled_setting_key(account_id)
    setting = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if setting is None:
        setting = AppSetting(
            key=key,
            value="true",
            category="automation",
            label="DSA live creation disabled",
            help_text="Automatically disabled after Google Ads rejects SEARCH_DYNAMIC_ADS for this account.",
            input_type="checkbox",
        )
        session.add(setting)
    else:
        setting.value = "true"
    reason_key = f"{key}:reason"
    reason_setting = session.scalar(select(AppSetting).where(AppSetting.key == reason_key))
    if reason_setting is None:
        session.add(
            AppSetting(
                key=reason_key,
                value=reason[:1000],
                category="automation",
                label="DSA live creation disabled reason",
                help_text="Latest Google Ads API reason for disabling live DSA creation.",
                input_type="textarea",
            )
        )
    else:
        reason_setting.value = reason[:1000]


def quota_cooldown_setting_key(account_id: int) -> str:
    return f"{LIVE_CREATION_QUOTA_COOLDOWN_PREFIX}:{int(account_id)}"


def _developer_token_hash(account: GoogleAdsAccount) -> str:
    token = ""
    if account.connection is not None:
        token = str(account.connection.developer_token or "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def _api_quota_setting_matches_account(setting: AppSetting, account: GoogleAdsAccount) -> bool:
    if setting.key == f"{GOOGLE_ADS_API_QUOTA_PREFIX}.{account.customer_id}":
        return True
    value = setting.value if isinstance(setting.value, dict) else {}
    if value.get("connection_id") and account.connection_id and int(value["connection_id"]) == int(account.connection_id):
        return True
    token_hash = _developer_token_hash(account)
    if token_hash and value.get("developer_token_hash") == token_hash:
        return True
    return False


def _parse_retry_seconds(text: str) -> int:
    match = re.search(r"retry\s+in\s+(\d+)\s+seconds", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return 3600
    try:
        return max(int(match.group(1)), 300)
    except ValueError:
        return 3600


def _is_quota_exhausted_error(value: Any) -> bool:
    text = json.dumps(value, default=str) if isinstance(value, dict) else str(value or "")
    lowered = text.lower()
    return (
        "resource has been exhausted" in lowered
        or "too many requests" in lowered
        or "resource_exhausted" in lowered
        or "too many" in lowered
        or "quota" in lowered
        or "rate limit" in lowered
    )


def live_creation_quota_cooldown(session: Session, account: Any) -> Optional[dict[str, Any]]:
    if not hasattr(session, "scalar"):
        return None
    rows: list[AppSetting] = []
    account_id = int(account.id if isinstance(account, GoogleAdsAccount) else account)
    setting = session.scalar(select(AppSetting).where(AppSetting.key == quota_cooldown_setting_key(account_id)))
    if setting is not None:
        rows.append(setting)
    if isinstance(account, GoogleAdsAccount) and hasattr(session, "scalars"):
        rows.extend(
            item
            for item in session.scalars(select(AppSetting).where(AppSetting.key.like(f"{GOOGLE_ADS_API_QUOTA_PREFIX}.%"))).all()
            if _api_quota_setting_matches_account(item, account)
        )
    active: list[tuple[datetime, AppSetting]] = []
    now = utcnow()
    for item in rows:
        if item is None or not isinstance(item.value, dict):
            continue
        retry_at_raw = str(item.value.get("retry_at") or item.value.get("retry_not_before") or "")
        try:
            retry_at = datetime.fromisoformat(retry_at_raw)
        except ValueError:
            continue
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        if retry_at > now:
            active.append((retry_at, item))
    if not active:
        return None
    retry_at, setting = max(active, key=lambda pair: pair[0])
    payload = setting.value if isinstance(setting.value, dict) else {}
    return {
        "status": "deferred_quota",
        "retry_at": retry_at.isoformat(),
        "retry_seconds_remaining": int((retry_at - now).total_seconds()),
        "reason": str(payload.get("reason") or "Google Ads mutation quota is cooling down."),
        "quota_key": setting.key,
    }


def defer_live_creation_for_quota(session: Session, account: GoogleAdsAccount, error: Any) -> dict[str, Any]:
    error_text = json.dumps(error, default=str) if isinstance(error, dict) else str(error or "")
    retry_seconds = _parse_retry_seconds(error_text)
    retry_at = utcnow() + timedelta(seconds=retry_seconds)
    payload = {
        "reason": "Google Ads returned RESOURCE_EXHAUSTED / too many requests for live campaign mutation.",
        "retry_at": retry_at.isoformat(),
        "retry_seconds": retry_seconds,
        "error": error_text[:1000],
    }
    key = quota_cooldown_setting_key(account.id)
    setting = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if setting is None:
        session.add(
            AppSetting(
                key=key,
                value=payload,
                category="automation",
                label="Google Ads live mutation quota cooldown",
                help_text="Automatically set when Google Ads asks the automation to retry live campaign mutations later.",
                input_type="json",
            )
        )
    else:
        setting.value = payload
    if account.connection_id:
        connection_payload = {
            "reason": "Google Ads API basic-access operations quota exhausted during live campaign/asset automation.",
            "connection_id": account.connection_id,
            "developer_token_hash": _developer_token_hash(account),
            "customer_id": account.customer_id,
            "account_id": account.id,
            "recorded_at": utcnow().isoformat(),
            "retry_not_before": retry_at.isoformat(),
            "retry_after_seconds": retry_seconds,
            "error": error_text[:1000],
        }
        connection_key = f"{GOOGLE_ADS_API_QUOTA_PREFIX}.connection.{account.connection_id}"
        connection_setting = session.scalar(select(AppSetting).where(AppSetting.key == connection_key))
        if connection_setting is None:
            session.add(
                AppSetting(
                    key=connection_key,
                    value=connection_payload,
                    category="automation",
                    label="Google Ads API connection quota cooldown",
                    help_text="Automatically set when Google Ads basic-access operations quota is exhausted for a shared connection.",
                    input_type="json",
                )
            )
        else:
            connection_setting.value = connection_payload
    session.flush()
    browser_queue: dict[str, Any] = {}
    try:
        browser_queue = generate_browser_automation_tasks(session, account)
    except Exception as exc:  # noqa: BLE001 - quota cooldown must still be recorded if browser fallback queueing fails.
        browser_queue = {"status": "failed", "error": str(exc)[:500]}
    return {
        "status": "deferred_quota",
        "retry_at": retry_at.isoformat(),
        "retry_seconds": retry_seconds,
        "reason": payload["reason"],
        "browser_queue": browser_queue,
    }


def _existing_exact_keyword_map(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> dict[str, dict[str, str]]:
    if not ad_group_id:
        return {}
    query = f"""
        SELECT
          ad_group_criterion.resource_name,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.status
        FROM ad_group_criterion
        WHERE ad_group.id = {int(ad_group_id)}
          AND ad_group_criterion.type = KEYWORD
          AND ad_group_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    existing: dict[str, dict[str, str]] = {}
    for row in _google_ads_search(service, account, query):
        if enum_name(row.ad_group_criterion.keyword.match_type) != "EXACT":
            continue
        text = _keyword_key(row.ad_group_criterion.keyword.text)
        if text:
            existing[text] = {
                "resource_name": str(row.ad_group_criterion.resource_name or ""),
                "status": enum_name(row.ad_group_criterion.status),
            }
    return existing


def _existing_exact_keywords(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> set[str]:
    return set(_existing_exact_keyword_map(client, account, ad_group_id))


def _existing_campaign_negative_exact_keywords(client: Any, account: GoogleAdsAccount, campaign_id: int) -> set[str]:
    if not campaign_id:
        return set()
    query = f"""
        SELECT
          campaign_criterion.keyword.text,
          campaign_criterion.keyword.match_type,
          campaign_criterion.negative,
          campaign_criterion.status
        FROM campaign_criterion
        WHERE campaign.id = {int(campaign_id)}
          AND campaign_criterion.type = KEYWORD
          AND campaign_criterion.negative = TRUE
          AND campaign_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        if enum_name(row.campaign_criterion.keyword.match_type) != "EXACT":
            continue
        text = _keyword_key(row.campaign_criterion.keyword.text)
        if text:
            existing.add(text)
    return existing


def _existing_campaign_negative_webpages(client: Any, account: GoogleAdsAccount, campaign_id: int) -> set[str]:
    if not campaign_id:
        return set()
    query = f"""
        SELECT
          campaign_criterion.webpage.conditions,
          campaign_criterion.negative,
          campaign_criterion.status
        FROM campaign_criterion
        WHERE campaign.id = {int(campaign_id)}
          AND campaign_criterion.type = WEBPAGE
          AND campaign_criterion.negative = TRUE
          AND campaign_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        for condition in row.campaign_criterion.webpage.conditions:
            argument = str(condition.argument or "").strip().rstrip("/")
            if argument:
                existing.add(argument)
    return existing


def _create_campaign_budget(
    client: Any,
    account: GoogleAdsAccount,
    *,
    name: str,
    amount_micros: int,
    validate_only: bool,
) -> str:
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create
    budget.name = name[:255]
    budget.amount_micros = int(amount_micros)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignBudgetService"), "mutate_campaign_budgets", request)
    return str(response.results[0].resource_name or "")


def _set_campaign_safety_fields(client: Any, campaign: Any) -> None:
    try:
        campaign.network_settings.target_google_search = True
        campaign.network_settings.target_search_network = False
        campaign.network_settings.target_partner_search_network = False
        campaign.network_settings.target_content_network = False
    except Exception:  # noqa: BLE001 - fake clients and older API versions may not expose all fields.
        pass
    try:
        campaign.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001 - field is version dependent.
        pass


def _enable_campaign_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    campaign: dict[str, Any],
    *,
    validate_only: bool,
) -> Optional[str]:
    if str(campaign.get("campaign_status") or "").upper() == "ENABLED":
        return None
    resource_name = str(campaign.get("campaign_resource_name") or "")
    if not resource_name:
        return None
    operation = client.get_type("CampaignOperation")
    update = operation.update
    update.resource_name = resource_name
    update.status = client.enums.CampaignStatusEnum.ENABLED
    operation.update_mask.paths.append("status")
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    campaign["campaign_status"] = "ENABLED"
    return str(response.results[0].resource_name or resource_name)


def _pause_campaign_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    campaign: dict[str, Any],
    *,
    validate_only: bool,
) -> Optional[str]:
    if str(campaign.get("campaign_status") or "").upper() == "PAUSED":
        return None
    resource_name = str(campaign.get("campaign_resource_name") or "")
    if not resource_name:
        return None
    operation = client.get_type("CampaignOperation")
    update = operation.update
    update.resource_name = resource_name
    update.status = client.enums.CampaignStatusEnum.PAUSED
    operation.update_mask.paths.append("status")
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    campaign["campaign_status"] = "PAUSED"
    return str(response.results[0].resource_name or resource_name)


def _enable_ad_group_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    ad_group: dict[str, Any],
    *,
    validate_only: bool,
) -> Optional[str]:
    if str(ad_group.get("ad_group_status") or "").upper() == "ENABLED":
        return None
    resource_name = str(ad_group.get("ad_group_resource_name") or "")
    if not resource_name:
        return None
    operation = client.get_type("AdGroupOperation")
    update = operation.update
    update.resource_name = resource_name
    update.status = client.enums.AdGroupStatusEnum.ENABLED
    operation.update_mask.paths.append("status")
    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupService"), "mutate_ad_groups", request)
    ad_group["ad_group_status"] = "ENABLED"
    return str(response.results[0].resource_name or resource_name)


def _rename_ad_group_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    ad_group: dict[str, Any],
    *,
    desired_name: str,
    validate_only: bool,
) -> Optional[str]:
    current_name = str(ad_group.get("ad_group_name") or "")
    if not desired_name or current_name == desired_name:
        return None
    resource_name = str(ad_group.get("ad_group_resource_name") or "")
    if not resource_name:
        return None
    operation = client.get_type("AdGroupOperation")
    update = operation.update
    update.resource_name = resource_name
    update.name = desired_name[:255]
    operation.update_mask.paths.append("name")
    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupService"), "mutate_ad_groups", request)
    ad_group["ad_group_name"] = desired_name
    return str(response.results[0].resource_name or resource_name)


def _enable_ad_group_ad_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    ad: dict[str, Any],
    *,
    validate_only: bool,
) -> Optional[str]:
    if str(ad.get("ad_status") or "").upper() == "ENABLED":
        return None
    resource_name = str(ad.get("ad_group_ad_resource_name") or "")
    if not resource_name:
        return None
    operation = client.get_type("AdGroupAdOperation")
    update = operation.update
    update.resource_name = resource_name
    update.status = client.enums.AdGroupAdStatusEnum.ENABLED
    operation.update_mask.paths.append("status")
    request = client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupAdService"), "mutate_ad_group_ads", request)
    ad["ad_status"] = "ENABLED"
    return str(response.results[0].resource_name or resource_name)


def _enable_campaign_ai_max(
    client: Any,
    account: GoogleAdsAccount,
    campaign_resource_name: str,
    *,
    final_url_expansion: bool = True,
    validate_only: bool,
) -> Optional[str]:
    if not campaign_resource_name:
        return None
    try:
        operation = client.get_type("CampaignOperation")
        update = operation.update
        update.resource_name = campaign_resource_name
        update.ai_max_setting.enable_ai_max = True
        _append_campaign_ai_max_asset_settings(client, update, final_url_expansion=final_url_expansion)
    except Exception:  # noqa: BLE001 - older client/test doubles may not expose AI Max.
        return None
    operation.update_mask.paths.append("ai_max_setting.enable_ai_max")
    operation.update_mask.paths.append("asset_automation_settings")
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    return str(response.results[0].resource_name or campaign_resource_name)


def _append_campaign_ai_max_asset_settings(
    client: Any,
    campaign: Any,
    *,
    final_url_expansion: bool,
) -> None:
    preferences = (
        (client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION, True),
        (client.enums.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION, bool(final_url_expansion)),
    )
    for automation_type, enabled in preferences:
        setting = client.get_type("Campaign.AssetAutomationSetting")
        setting.asset_automation_type = automation_type
        setting.asset_automation_status = (
            client.enums.AssetAutomationStatusEnum.OPTED_IN
            if enabled
            else client.enums.AssetAutomationStatusEnum.OPTED_OUT
        )
        campaign.asset_automation_settings.append(setting)


def _create_search_campaign(
    client: Any,
    account: GoogleAdsAccount,
    *,
    name: str,
    budget_resource_name: str,
    max_cpc_micros: int,
    bidding: Optional[dict[str, Any]] = None,
    domain_name: str = "",
    enable_ai_max: bool = False,
    ai_max_final_url_expansion: bool = True,
    validate_only: bool,
) -> str:
    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name[:255]
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    bidding = bidding if isinstance(bidding, dict) else {}
    strategy = str(bidding.get("strategy") or "").strip().lower()
    target_roas = _float_value(bidding.get("target_roas"), 0.0)
    if domain_name:
        campaign.dynamic_search_ads_setting.domain_name = domain_name
        campaign.dynamic_search_ads_setting.language_code = "en"
    if enable_ai_max:
        try:
            campaign.ai_max_setting.enable_ai_max = True
            _append_campaign_ai_max_asset_settings(
                client,
                campaign,
                final_url_expansion=bool(ai_max_final_url_expansion),
            )
        except Exception:  # noqa: BLE001 - older client/test doubles may not expose AI Max yet.
            pass
    if strategy == "maximize_conversion_value_target_roas" and target_roas > 0:
        campaign.maximize_conversion_value.target_roas = float(target_roas)
    else:
        maximize_conversion_value = client.get_type("MaximizeConversionValue")
        campaign._pb.maximize_conversion_value.CopyFrom(maximize_conversion_value._pb)
    _set_campaign_safety_fields(client, campaign)
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    return str(response.results[0].resource_name or "")


def _create_ad_group(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_resource_name: str,
    name: str,
    ad_group_type: str,
    max_cpc_micros: int,
    validate_only: bool,
) -> str:
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create
    ad_group.name = name[:255]
    ad_group.campaign = campaign_resource_name
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.cpc_bid_micros = int(max_cpc_micros)
    if str(ad_group_type or "").lower() in {"dsa", "dynamic", "search_dynamic_ads"}:
        ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_DYNAMIC_ADS
    else:
        ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupService"), "mutate_ad_groups", request)
    return str(response.results[0].resource_name or "")


def _is_dynamic_search_ad_group(ad_group: dict[str, Any] | None) -> bool:
    return str((ad_group or {}).get("ad_group_type") or "").upper() == "SEARCH_DYNAMIC_ADS"


def _create_dsa_ad(
    client: Any,
    account: GoogleAdsAccount,
    *,
    ad_group_resource_name: str,
    descriptions: list[str],
    validate_only: bool,
) -> str:
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    dsa = ad_group_ad.ad.expanded_dynamic_search_ad
    dsa.description = _clip(descriptions[0] if descriptions else "Shop quality wellness products online.", 90)
    if len(descriptions) > 1:
        dsa.description2 = _clip(descriptions[1], 90)
    request = client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    _google_ads_mutate(client.get_service("AdGroupAdService"), "mutate_ad_group_ads", request)
    return ""


def _create_rsa_ad(
    client: Any,
    account: GoogleAdsAccount,
    *,
    ad_group_resource_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    validate_only: bool,
) -> str:
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ad_group_ad.ad
    ad.final_urls.append(final_url)
    rsa = ad.responsive_search_ad
    text_asset_type = type(client.get_type("AdTextAsset"))
    for headline in (headlines or [])[:15]:
        asset = text_asset_type()
        asset.text = _clip(headline, 30)
        rsa.headlines.append(asset)
    for description in (descriptions or [])[:4]:
        asset = text_asset_type()
        asset.text = _clip(description, 90)
        rsa.descriptions.append(asset)
    request = client.get_type("MutateAdGroupAdsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    _google_ads_mutate(client.get_service("AdGroupAdService"), "mutate_ad_group_ads", request)
    return ""


def _create_all_pages_webpage_criterion(
    client: Any,
    account: GoogleAdsAccount,
    *,
    ad_group_resource_name: str,
    validate_only: bool,
) -> str:
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.create
    criterion.ad_group = ad_group_resource_name
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
    criterion.webpage.criterion_name = "AUTO all pages"
    condition = client.get_type("WebpageConditionInfo")
    condition.operand = client.enums.WebpageConditionOperandEnum.URL
    condition.argument = "/"
    criterion.webpage.conditions.append(condition)
    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupCriterionService"), "mutate_ad_group_criteria", request)
    return str(response.results[0].resource_name or "")


def _webpage_url_key(url: Any) -> str:
    value = " ".join(str(url or "").split()).strip()
    if not value:
        return ""
    if not usable_landing_page_url(value):
        return ""
    return value.rstrip("/")


def _is_broad_or_category_landing_page(url: Any) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
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


def _pmax_final_urls_from_assets(
    assets: dict[str, Any],
    fallback_url: str,
    *,
    limit: Optional[int] = PMAX_ASSET_GROUP_FINAL_URL_LIMIT,
    exclude_broad_pages: bool = False,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    sources = [
        assets.get("url_inclusion_targets"),
        assets.get("page_feed_targets"),
        assets.get("landing_page_candidates"),
    ]
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, dict):
                raw_url = item.get("url") or item.get("final_url") or item.get("landing_page")
            else:
                raw_url = item
            key = _webpage_url_key(raw_url)
            if not key or key in seen:
                continue
            if exclude_broad_pages and _is_broad_or_category_landing_page(key):
                continue
            seen.add(key)
            urls.append(key)
            if limit and len(urls) >= limit:
                return urls
    fallback_key = _webpage_url_key(fallback_url)
    if (
        not urls
        and fallback_key
        and fallback_key not in seen
        and not (exclude_broad_pages and _is_broad_or_category_landing_page(fallback_key))
    ):
        urls.append(fallback_key)
    return urls[:limit] if limit else urls


def _url_inclusion_urls_from_draft(draft: AdDraft, *, limit: Optional[int] = None) -> list[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = _draft_identity(draft)
    category = str(identity.get("category") or assets.get("category") or assets.get("campaign_name") or "").lower()
    core_scale_only = "core / scale" in category or "core scale" in category
    sources = [
        assets.get("url_inclusion_targets"),
        assets.get("page_feed_targets"),
        assets.get("landing_page_candidates"),
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            url = ""
            if isinstance(item, dict):
                url = str(item.get("url") or item.get("final_url") or item.get("landing_page") or "").strip()
            else:
                url = str(item or "").strip()
            key = _webpage_url_key(url)
            if not key or key in seen:
                continue
            if core_scale_only and _is_broad_or_category_landing_page(key):
                continue
            seen.add(key)
            urls.append(key)
            if limit and len(urls) >= limit:
                return urls
    return urls


def _manual_first_run_criteria_csv_pending(preference: GoogleAdsAutomationPreference, draft: AdDraft) -> bool:
    if not bool(getattr(preference, "manual_first_run_criteria_csv_enabled", False)):
        return False
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    manual_upload = assets.get("manual_upload") if isinstance(assets.get("manual_upload"), dict) else {}
    live_publish = assets.get("live_publish") if isinstance(assets.get("live_publish"), dict) else {}
    return str(manual_upload.get("status") or "").lower() != "confirmed" and str(
        live_publish.get("status") or ""
    ).lower() != "manual_uploaded_confirmed"


def _manual_csv_deferred_keyword_result(draft: AdDraft) -> dict[str, Any]:
    keywords = _keyword_texts_from_draft(draft)
    return {
        "keyword_count": len(keywords),
        "existing_keyword_count": 0,
        "new_keyword_count": len(keywords),
        "keyword_resources": [],
        "manual_csv_deferred": True,
        "manual_csv_deferred_entities": ["keywords"],
        "manual_csv_export_kind": "keywords",
        "manual_csv_reason": "First-run bulk criteria CSV mode is enabled and manual upload is not confirmed.",
    }


def _manual_csv_deferred_negative_keyword_result(draft: AdDraft) -> dict[str, Any]:
    keywords = _negative_keyword_texts_from_draft(draft)
    return {
        "negative_keyword_count": len(keywords),
        "existing_negative_keyword_count": 0,
        "new_negative_keyword_count": len(keywords),
        "negative_keyword_resources": [],
        "manual_csv_deferred": True,
        "manual_csv_deferred_entities": ["negative_keywords"],
        "manual_csv_export_kind": "negative_keywords",
        "manual_csv_reason": "First-run bulk criteria CSV mode is enabled and manual upload is not confirmed.",
    }


def _manual_csv_deferred_negative_page_result(draft: AdDraft) -> dict[str, Any]:
    urls = _negative_page_urls_from_draft(draft)
    return {
        "negative_page_count": len(urls),
        "existing_negative_page_count": 0,
        "new_negative_page_count": len(urls),
        "negative_page_resources": [],
        "manual_csv_deferred": True,
        "manual_csv_deferred_entities": ["url_exclusions"],
        "manual_csv_export_kind": "url_exclusions",
        "manual_csv_reason": "First-run bulk criteria CSV mode is enabled and manual upload is not confirmed.",
    }


def _manual_csv_deferred_url_inclusion_result(draft: AdDraft) -> dict[str, Any]:
    urls = _url_inclusion_urls_from_draft(draft)
    return {
        "url_inclusion_count": len(urls),
        "existing_url_inclusion_count": 0,
        "new_url_inclusion_count": len(urls),
        "url_inclusion_resources": [],
        "manual_csv_deferred": True,
        "manual_csv_deferred_entities": ["url_inclusions"],
        "manual_csv_export_kind": "url_inclusions",
        "manual_csv_reason": "First-run bulk criteria CSV mode is enabled and manual upload is not confirmed.",
    }


def _append_manual_csv_deferred_operation(operations: list[dict[str, str]], entity: str, count: int) -> None:
    if count <= 0:
        return
    operations.append(
        {
            "operation": f"defer_{entity}_to_manual_csv",
            "resource_name": f"{count} planned rows",
        }
    )


def _existing_ad_group_webpage_inclusions(client: Any, account: GoogleAdsAccount, ad_group_id: int) -> set[str]:
    if not ad_group_id:
        return set()
    query = f"""
        SELECT
          ad_group_criterion.webpage.conditions,
          ad_group_criterion.negative,
          ad_group_criterion.status
        FROM ad_group_criterion
        WHERE ad_group.id = {int(ad_group_id)}
          AND ad_group_criterion.type = WEBPAGE
          AND ad_group_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        if bool(getattr(row.ad_group_criterion, "negative", False)):
            continue
        for condition in row.ad_group_criterion.webpage.conditions:
            argument = _webpage_url_key(condition.argument)
            if argument:
                existing.add(argument)
    return existing


def _create_ad_group_webpage_inclusions(
    client: Any,
    account: GoogleAdsAccount,
    *,
    ad_group_resource_name: str,
    urls: list[str],
    validate_only: bool,
) -> list[str]:
    resources: list[str] = []
    service = client.get_service("AdGroupCriterionService")
    for start in range(0, len(urls), URL_INCLUSION_MUTATION_BATCH_LIMIT):
        operations = []
        for url in urls[start : start + URL_INCLUSION_MUTATION_BATCH_LIMIT]:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = ad_group_resource_name
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.webpage.criterion_name = _clip(f"AUTO included page | {url}", 255)
            condition = client.get_type("WebpageConditionInfo")
            condition.operand = client.enums.WebpageConditionOperandEnum.URL
            condition.operator = client.enums.WebpageConditionOperatorEnum.EQUALS
            condition.argument = url
            criterion.webpage.conditions.append(condition)
            operations.append(operation)
        if not operations:
            continue
        request = client.get_type("MutateAdGroupCriteriaRequest")
        request.customer_id = account.customer_id
        request.operations.extend(operations)
        request.partial_failure = True
        request.validate_only = validate_only
        response = _google_ads_mutate(service, "mutate_ad_group_criteria", request)
        resources.extend(_successful_resource_names(response))
    return resources


def _apply_ad_group_webpage_inclusions(
    client: Any,
    account: GoogleAdsAccount,
    draft: AdDraft,
    *,
    session: Session,
    ad_group_id: int,
    ad_group_resource_name: str,
    validate_only: bool,
) -> dict[str, Any]:
    urls = _url_inclusion_urls_from_draft(draft)
    if not urls:
        return {
            "url_inclusion_count": 0,
            "existing_url_inclusion_count": 0,
            "new_url_inclusion_count": 0,
            "url_inclusion_resources": [],
        }
    existing = _existing_ad_group_webpage_inclusions(client, account, ad_group_id) if ad_group_id else set()
    to_create = [url for url in urls if _webpage_url_key(url) not in existing]
    planned_to_create = len(to_create)
    to_create, reservation = _limit_daily_criteria_items(
        session,
        account,
        kind="ad_group_url_inclusions",
        scope_key=f"ad_group_url_inclusions:{ad_group_resource_name}",
        items=to_create,
        validate_only=validate_only,
    )
    resources = _create_ad_group_webpage_inclusions(
        client,
        account,
        ad_group_resource_name=ad_group_resource_name,
        urls=to_create,
        validate_only=validate_only,
    )
    return {
        "url_inclusion_count": len(urls),
        "existing_url_inclusion_count": len(existing),
        "planned_new_url_inclusion_count": planned_to_create,
        "new_url_inclusion_count": len(to_create),
        "daily_deferred_url_inclusion_count": max(planned_to_create - len(to_create), 0),
        "url_inclusion_daily_budget": reservation,
        "url_inclusion_resources": resources,
    }


def _create_exact_keywords(
    client: Any,
    account: GoogleAdsAccount,
    *,
    ad_group_resource_name: str,
    keywords: list[str],
    validate_only: bool,
) -> list[str]:
    resources: list[str] = []
    service = client.get_service("AdGroupCriterionService")
    for start in range(0, len(keywords), KEYWORD_MUTATION_BATCH_LIMIT):
        operations = []
        for keyword in keywords[start : start + KEYWORD_MUTATION_BATCH_LIMIT]:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = ad_group_resource_name
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = keyword
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT
            operations.append(operation)
        if not operations:
            continue
        request = client.get_type("MutateAdGroupCriteriaRequest")
        request.customer_id = account.customer_id
        request.operations.extend(operations)
        request.partial_failure = True
        request.validate_only = validate_only
        response = _google_ads_mutate(service, "mutate_ad_group_criteria", request)
        resources.extend(_successful_resource_names(response))
    return resources


def _enable_ad_group_criteria_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    *,
    criterion_resource_names: list[str],
    validate_only: bool,
) -> list[str]:
    operations = []
    for resource_name in criterion_resource_names:
        if not resource_name:
            continue
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = resource_name
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        operation.update_mask.paths.append("status")
        operations.append(operation)
    if not operations:
        return []
    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupCriterionService"), "mutate_ad_group_criteria", request)
    return _successful_resource_names(response)


def _pause_ad_group_criteria_if_needed(
    client: Any,
    account: GoogleAdsAccount,
    *,
    criterion_resource_names: list[str],
    validate_only: bool,
) -> list[str]:
    operations = []
    for resource_name in criterion_resource_names:
        if not resource_name:
            continue
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = resource_name
        criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
        operation.update_mask.paths.append("status")
        operations.append(operation)
    if not operations:
        return []
    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AdGroupCriterionService"), "mutate_ad_group_criteria", request)
    return _successful_resource_names(response)


def _create_campaign_negative_exact_keywords(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_resource_name: str,
    keywords: list[str],
    validate_only: bool,
) -> list[str]:
    resources: list[str] = []
    service = client.get_service("CampaignCriterionService")
    for start in range(0, len(keywords), KEYWORD_MUTATION_BATCH_LIMIT):
        operations = []
        for keyword in keywords[start : start + KEYWORD_MUTATION_BATCH_LIMIT]:
            operation = client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_resource_name
            criterion.negative = True
            criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            criterion.keyword.text = keyword
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT
            operations.append(operation)
        if not operations:
            continue
        request = client.get_type("MutateCampaignCriteriaRequest")
        request.customer_id = account.customer_id
        request.operations.extend(operations)
        request.partial_failure = True
        request.validate_only = validate_only
        response = _google_ads_mutate(service, "mutate_campaign_criteria", request)
        resources.extend(_successful_resource_names(response))
    return resources


def _create_campaign_negative_webpages(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_resource_name: str,
    urls: list[str],
    validate_only: bool,
) -> list[str]:
    resources: list[str] = []
    service = client.get_service("CampaignCriterionService")
    for start in range(0, len(urls), URL_INCLUSION_MUTATION_BATCH_LIMIT):
        operations = []
        for url in urls[start : start + URL_INCLUSION_MUTATION_BATCH_LIMIT]:
            operation = client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_resource_name
            criterion.negative = True
            criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            criterion.webpage.criterion_name = _clip(f"AUTO excluded page | {url}", 255)
            condition = client.get_type("WebpageConditionInfo")
            condition.operand = client.enums.WebpageConditionOperandEnum.URL
            condition.argument = url
            criterion.webpage.conditions.append(condition)
            operations.append(operation)
        if not operations:
            continue
        request = client.get_type("MutateCampaignCriteriaRequest")
        request.customer_id = account.customer_id
        request.operations.extend(operations)
        request.partial_failure = True
        request.validate_only = validate_only
        response = _google_ads_mutate(service, "mutate_campaign_criteria", request)
        resources.extend(_successful_resource_names(response))
    return resources


def _existing_campaign_location_targets(client: Any, account: GoogleAdsAccount, campaign_id: int) -> list[dict[str, str]]:
    if not campaign_id or not hasattr(client, "get_service"):
        return []
    query = f"""
        SELECT
          campaign_criterion.resource_name,
          campaign_criterion.location.geo_target_constant,
          campaign_criterion.negative,
          campaign_criterion.status
        FROM campaign_criterion
        WHERE campaign.id = {int(campaign_id)}
          AND campaign_criterion.type = LOCATION
          AND campaign_criterion.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    if not hasattr(service, "search"):
        return []
    rows: list[dict[str, str]] = []
    for row in _google_ads_search(service, account, query):
        rows.append(
            {
                "resource_name": str(row.campaign_criterion.resource_name or ""),
                "geo_target_constant": str(row.campaign_criterion.location.geo_target_constant or ""),
                "negative": bool(row.campaign_criterion.negative),
                "status": enum_name(row.campaign_criterion.status),
            }
        )
    return rows


def _ensure_campaign_country_target(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_id: int,
    campaign_resource_name: str,
    country_target: dict[str, str],
    validate_only: bool,
) -> dict[str, Any]:
    target_resource = str(country_target.get("geo_target_constant") or "")
    if not campaign_id or not campaign_resource_name or not target_resource:
        return {"status": "skipped", "reason": "No resolved country target."}
    if not hasattr(client, "get_service") or not hasattr(client, "get_type"):
        return {"status": "skipped", "reason": "Google Ads client does not expose mutation services."}
    service = client.get_service("CampaignCriterionService")
    if not hasattr(service, "mutate_campaign_criteria"):
        return {"status": "skipped", "reason": "Google Ads client does not expose campaign criterion mutation."}
    existing = _existing_campaign_location_targets(client, account, campaign_id)
    positive_existing = [item for item in existing if not item.get("negative")]
    has_target = any(item.get("geo_target_constant") == target_resource for item in positive_existing)
    to_remove = [
        item["resource_name"]
        for item in positive_existing
        if item.get("resource_name") and item.get("geo_target_constant") != target_resource
    ]
    operations = []
    for resource_name in to_remove:
        operation = client.get_type("CampaignCriterionOperation")
        if not hasattr(operation, "remove"):
            return {"status": "skipped", "reason": "Google Ads client does not expose campaign criterion operations."}
        operation.remove = resource_name
        operations.append(operation)
    if not has_target:
        operation = client.get_type("CampaignCriterionOperation")
        if not hasattr(operation, "create") or not hasattr(client.enums, "CampaignCriterionStatusEnum"):
            return {"status": "skipped", "reason": "Google Ads client does not expose location criterion fields."}
        criterion = operation.create
        criterion.campaign = campaign_resource_name
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        criterion.location.geo_target_constant = target_resource
        operations.append(operation)
    if not operations:
        return {
            "status": "unchanged",
            "country_code": country_target.get("country_code"),
            "country_name": country_target.get("country_name"),
            "geo_target_constant": target_resource,
            "existing_location_count": len(existing),
        }
    request = client.get_type("MutateCampaignCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignCriterionService"), "mutate_campaign_criteria", request)
    resources = _successful_resource_names(response)
    return {
        "status": "validated" if validate_only else "updated",
        "country_code": country_target.get("country_code"),
        "country_name": country_target.get("country_name"),
        "geo_target_constant": target_resource,
        "removed_other_positive_locations": len(to_remove),
        "created_country_target": not has_target,
        "resources": resources,
    }


def _apply_campaign_negative_keywords(
    client: Any,
    account: GoogleAdsAccount,
    draft: AdDraft,
    *,
    session: Session,
    campaign_id: int,
    campaign_resource_name: str,
    validate_only: bool,
) -> dict[str, Any]:
    keywords = _negative_keyword_texts_from_draft(draft)
    if not keywords:
        return {
            "negative_keyword_count": 0,
            "existing_negative_keyword_count": 0,
            "new_negative_keyword_count": 0,
            "negative_keyword_resources": [],
        }
    existing = _existing_campaign_negative_exact_keywords(client, account, campaign_id) if campaign_id else set()
    to_create = [keyword for keyword in keywords if keyword not in existing]
    planned_to_create = len(to_create)
    to_create, reservation = _limit_daily_criteria_items(
        session,
        account,
        kind="campaign_negative_keywords",
        scope_key=f"campaign_negative_keywords:{campaign_resource_name}",
        items=to_create,
        validate_only=validate_only,
    )
    resources = _create_campaign_negative_exact_keywords(
        client,
        account,
        campaign_resource_name=campaign_resource_name,
        keywords=to_create,
        validate_only=validate_only,
    )
    return {
        "negative_keyword_count": len(keywords),
        "existing_negative_keyword_count": len(existing),
        "planned_new_negative_keyword_count": planned_to_create,
        "new_negative_keyword_count": len(to_create),
        "daily_deferred_negative_keyword_count": max(planned_to_create - len(to_create), 0),
        "negative_keyword_daily_budget": reservation,
        "negative_keyword_resources": resources,
    }


def _apply_campaign_negative_webpages(
    client: Any,
    account: GoogleAdsAccount,
    draft: AdDraft,
    *,
    session: Session,
    campaign_id: int,
    campaign_resource_name: str,
    validate_only: bool,
) -> dict[str, Any]:
    urls = _negative_page_urls_from_draft(draft)
    if not urls:
        return {
            "negative_page_count": 0,
            "existing_negative_page_count": 0,
            "new_negative_page_count": 0,
            "negative_page_resources": [],
        }
    existing = _existing_campaign_negative_webpages(client, account, campaign_id) if campaign_id else set()
    to_create = [url for url in urls if url.rstrip("/") not in existing]
    planned_to_create = len(to_create)
    to_create, reservation = _limit_daily_criteria_items(
        session,
        account,
        kind="campaign_url_exclusions",
        scope_key=f"campaign_url_exclusions:{campaign_resource_name}",
        items=to_create,
        validate_only=validate_only,
    )
    resources = _create_campaign_negative_webpages(
        client,
        account,
        campaign_resource_name=campaign_resource_name,
        urls=to_create,
        validate_only=validate_only,
    )
    return {
        "negative_page_count": len(urls),
        "existing_negative_page_count": len(existing),
        "planned_new_negative_page_count": planned_to_create,
        "new_negative_page_count": len(to_create),
        "daily_deferred_negative_page_count": max(planned_to_create - len(to_create), 0),
        "negative_page_daily_budget": reservation,
        "negative_page_resources": resources,
    }


def _create_performance_max_campaign(
    client: Any,
    account: GoogleAdsAccount,
    *,
    name: str,
    budget_resource_name: str,
    target_roas: float,
    validate_only: bool,
) -> str:
    operation = client.get_type("CampaignOperation")
    campaign = operation.create
    campaign.name = name[:255]
    campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    campaign.maximize_conversion_value.target_roas = float(target_roas or 0)
    try:
        campaign.brand_guidelines_enabled = False
    except Exception:  # noqa: BLE001 - field is version dependent.
        pass
    try:
        campaign.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001 - field is version dependent.
        pass
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request)
    return str(response.results[0].resource_name or "")


def _asset_group_by_name(client: Any, account: GoogleAdsAccount, campaign_id: int, name: str) -> Optional[dict[str, Any]]:
    query = f"""
        SELECT
          asset_group.id,
          asset_group.name,
          asset_group.resource_name,
          asset_group.status
        FROM asset_group
        WHERE campaign.id = {int(campaign_id)}
          AND asset_group.name = {gaql_string(name)}
          AND asset_group.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return {
            "asset_group_id": int(row.asset_group.id),
            "asset_group_name": str(row.asset_group.name or ""),
            "asset_group_resource_name": str(row.asset_group.resource_name or ""),
            "asset_group_status": enum_name(row.asset_group.status),
            "matched_by": "asset_group_name",
        }
    return None


def _asset_groups_by_name(client: Any, account: GoogleAdsAccount, campaign_id: int) -> dict[str, dict[str, Any]]:
    if not campaign_id or not hasattr(client, "get_service"):
        return {}
    query = f"""
        SELECT
          asset_group.id,
          asset_group.name,
          asset_group.resource_name,
          asset_group.status
        FROM asset_group
        WHERE campaign.id = {int(campaign_id)}
          AND asset_group.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    groups: dict[str, dict[str, Any]] = {}
    for row in _google_ads_search(service, account, query):
        name = str(row.asset_group.name or "")
        if not name:
            continue
        groups[name] = {
            "asset_group_id": int(row.asset_group.id),
            "asset_group_name": name,
            "asset_group_resource_name": str(row.asset_group.resource_name or ""),
            "asset_group_status": enum_name(row.asset_group.status),
            "matched_by": "asset_group_name",
        }
    return groups


def _create_asset_group(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_resource_name: str,
    name: str,
    final_url: str,
    final_urls: Optional[list[str]] = None,
    validate_only: bool,
) -> str:
    operation = client.get_type("AssetGroupOperation")
    asset_group = operation.create
    asset_group.name = name[:255]
    asset_group.campaign = campaign_resource_name
    asset_group.status = client.enums.AssetGroupStatusEnum.ENABLED
    for url in (final_urls or [final_url]):
        if url:
            asset_group.final_urls.append(url)
    request = client.get_type("MutateAssetGroupsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("AssetGroupService"), "mutate_asset_groups", request)
    return str(response.results[0].resource_name or "")


def _create_asset_group_with_assets(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_resource_name: str,
    name: str,
    final_url: str,
    final_urls: Optional[list[str]] = None,
    links: list[tuple[str, str]],
    validate_only: bool,
) -> dict[str, Any]:
    temp_asset_group_resource = client.get_service("AssetGroupService").asset_group_path(account.customer_id, -1)
    operations = []

    group_operation = client.get_type("MutateOperation")
    asset_group = group_operation.asset_group_operation.create
    asset_group.resource_name = temp_asset_group_resource
    asset_group.name = name[:255]
    asset_group.campaign = campaign_resource_name
    asset_group.status = client.enums.AssetGroupStatusEnum.ENABLED
    for url in (final_urls or [final_url]):
        if url:
            asset_group.final_urls.append(url)
    operations.append(group_operation)

    seen_links: set[tuple[str, str]] = set()
    for asset_resource_name, field_type in links:
        if not asset_resource_name or not field_type:
            continue
        key = (asset_resource_name, field_type)
        if key in seen_links:
            continue
        seen_links.add(key)
        link_operation = client.get_type("MutateOperation")
        link = link_operation.asset_group_asset_operation.create
        link.asset_group = temp_asset_group_resource
        link.asset = asset_resource_name
        link.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        try:
            link.status = client.enums.AssetLinkStatusEnum.ENABLED
        except Exception:  # noqa: BLE001 - status field enum is version dependent.
            pass
        operations.append(link_operation)

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = account.customer_id
    request.mutate_operations.extend(operations)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("GoogleAdsService"), "mutate", request)
    responses = list(response.mutate_operation_responses or [])
    asset_group_resource = (
        str(responses[0].asset_group_result.resource_name or temp_asset_group_resource)
        if responses
        else temp_asset_group_resource
    )
    link_resources = [
        str(item.asset_group_asset_result.resource_name or "")
        for item in responses[1:]
        if str(item.asset_group_asset_result.resource_name or "")
    ]
    return {
        "asset_group_resource_name": asset_group_resource,
        "asset_link_resources": link_resources,
    }


def _dedupe_texts(values: list[str], *, limit: int, max_items: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clip(str(value or ""), limit)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= max_items:
            break
    return output


def _existing_text_assets(client: Any, account: GoogleAdsAccount, texts: list[str]) -> dict[str, str]:
    texts = _dedupe_texts(texts, limit=90, max_items=80)
    if not texts:
        return {}
    chunks = []
    for index in range(0, len(texts), 20):
        quoted = ", ".join(gaql_string(item) for item in texts[index : index + 20])
        chunks.append(quoted)
    service = client.get_service("GoogleAdsService")
    found: dict[str, str] = {}
    for chunk in chunks:
        query = f"""
            SELECT
              asset.resource_name,
              asset.text_asset.text
            FROM asset
            WHERE asset.type = TEXT
              AND asset.text_asset.text IN ({chunk})
        """
        for row in _google_ads_search(service, account, query):
            text = str(row.asset.text_asset.text or "")
            if text:
                found[text.lower()] = str(row.asset.resource_name or "")
    return found


def _create_text_assets(
    client: Any,
    account: GoogleAdsAccount,
    *,
    texts: list[str],
    validate_only: bool,
) -> dict[str, str]:
    texts = _dedupe_texts(texts, limit=90, max_items=80)
    existing = _existing_text_assets(client, account, texts)
    missing = [text for text in texts if text.lower() not in existing]
    if not missing:
        return {text: existing[text.lower()] for text in texts if text.lower() in existing}
    operations = []
    for text in missing:
        operation = client.get_type("AssetOperation")
        asset = operation.create
        asset.name = _clip(f"AUTO text | {text}", 255)
        asset.text_asset.text = text
        operations.append(operation)
    request = client.get_type("MutateAssetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.validate_only = validate_only
    response = _call_with_alarm(
        lambda: client.get_service("AssetService").mutate_assets(request=request, timeout=30),
        seconds=35,
        label="AssetService.mutate_assets",
    )
    created = {text.lower(): str(result.resource_name or "") for text, result in zip(missing, response.results)}
    merged = {**existing, **created}
    return {text: merged[text.lower()] for text in texts if text.lower() in merged}


def _existing_pmax_media_assets(client: Any, account: GoogleAdsAccount) -> dict[str, list[str]]:
    service = client.get_service("GoogleAdsService")
    assets: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    def add(field_type: str, resource_name: str) -> None:
        if field_type not in PMAX_MEDIA_FIELD_TYPES or not resource_name:
            return
        bucket = assets.setdefault(field_type, [])
        bucket_seen = seen.setdefault(field_type, set())
        if resource_name not in bucket_seen:
            bucket.append(resource_name)
            bucket_seen.add(resource_name)

    image_query = """
        SELECT
          asset.resource_name,
          asset.name,
          asset.image_asset.full_size.width_pixels,
          asset.image_asset.full_size.height_pixels
        FROM asset
        WHERE asset.type = IMAGE
        LIMIT 300
    """
    square_candidates: list[str] = []
    logo_candidates: list[str] = []
    wide_candidates: list[str] = []
    portrait_candidates: list[str] = []
    for row in _google_ads_search(service, account, image_query):
        resource_name = str(row.asset.resource_name or "")
        width = int(row.asset.image_asset.full_size.width_pixels or 0)
        height = int(row.asset.image_asset.full_size.height_pixels or 0)
        name = str(row.asset.name or "").lower()
        if not resource_name or width <= 0 or height <= 0:
            continue
        ratio = width / height
        is_exact_square = width == height
        if "logo" in name and is_exact_square and width >= 128:
            logo_candidates.append(resource_name)
        if is_exact_square and width >= 300:
            square_candidates.append(resource_name)
        elif width >= 600 and height >= 314 and 1.6 <= ratio <= 2.2:
            wide_candidates.append(resource_name)
        elif width >= 480 and height >= 600 and 0.65 <= ratio <= 0.9:
            portrait_candidates.append(resource_name)
    for resource_name in logo_candidates:
        add("LOGO", resource_name)
    if len(assets.get("LOGO") or []) < PMAX_MEDIA_FIELD_TYPES["LOGO"]:
        for resource_name in square_candidates:
            add("LOGO", resource_name)
    for resource_name in wide_candidates:
        add("MARKETING_IMAGE", resource_name)
    for resource_name in square_candidates:
        add("SQUARE_MARKETING_IMAGE", resource_name)
    for resource_name in portrait_candidates:
        add("PORTRAIT_MARKETING_IMAGE", resource_name)

    youtube_query = """
        SELECT
          asset.resource_name,
          asset.youtube_video_asset.youtube_video_id
        FROM asset
        WHERE asset.type = YOUTUBE_VIDEO
        LIMIT 50
    """
    for row in _google_ads_search(service, account, youtube_query):
        add("YOUTUBE_VIDEO", str(row.asset.resource_name or ""))
    return assets


def _existing_asset_group_assets(client: Any, account: GoogleAdsAccount, asset_group_resource_name: str) -> set[tuple[str, str]]:
    if not asset_group_resource_name:
        return set()
    query = f"""
        SELECT
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = {gaql_string(asset_group_resource_name)}
          AND asset_group_asset.status != REMOVED
    """
    service = client.get_service("GoogleAdsService")
    return {
        (str(row.asset_group_asset.asset or ""), enum_name(row.asset_group_asset.field_type))
        for row in _google_ads_search(service, account, query)
        if str(row.asset_group_asset.asset or "")
    }


def _link_asset_group_assets(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_group_resource_name: str,
    links: list[tuple[str, str]],
    validate_only: bool,
) -> list[str]:
    existing = _existing_asset_group_assets(client, account, asset_group_resource_name)
    operations = []
    planned: list[tuple[str, str]] = []
    for asset_resource_name, field_type in links:
        if not asset_resource_name or not field_type or (asset_resource_name, field_type) in existing:
            continue
        operation = client.get_type("AssetGroupAssetOperation")
        link = operation.create
        link.asset_group = asset_group_resource_name
        link.asset = asset_resource_name
        link.field_type = getattr(client.enums.AssetFieldTypeEnum, field_type)
        try:
            link.status = client.enums.AssetLinkStatusEnum.ENABLED
        except Exception:  # noqa: BLE001 - status field enum is version dependent.
            pass
        operations.append(operation)
        planned.append((asset_resource_name, field_type))
    if not operations:
        return []
    request = client.get_type("MutateAssetGroupAssetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _call_with_alarm(
        lambda: client.get_service("AssetGroupAssetService").mutate_asset_group_assets(request=request, timeout=30),
        seconds=35,
        label="AssetGroupAssetService.mutate_asset_group_assets",
    )
    return _successful_resource_names(response)


def _existing_asset_group_search_themes(client: Any, account: GoogleAdsAccount, asset_group_resource_name: str) -> set[str]:
    if not asset_group_resource_name:
        return set()
    query = f"""
        SELECT
          asset_group_signal.search_theme.text
        FROM asset_group_signal
        WHERE asset_group_signal.asset_group = {gaql_string(asset_group_resource_name)}
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        raw = str(row.asset_group_signal.search_theme.text or "").strip()
        cleaned = _clean_search_theme(raw)
        if raw:
            existing.add(raw.lower())
        if cleaned:
            existing.add(cleaned.lower())
    return existing


def _existing_campaign_search_theme_keys(client: Any, account: GoogleAdsAccount, campaign_id: int) -> set[str]:
    if not campaign_id:
        return set()
    query = f"""
        SELECT
          asset_group_signal.search_theme.text
        FROM asset_group_signal
        WHERE campaign.id = {int(campaign_id)}
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        raw = str(row.asset_group_signal.search_theme.text or "").strip()
        key = _keyword_key(_clean_search_theme(raw))
        if key:
            existing.add(key)
    return existing


def _create_search_theme_signals(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_group_resource_name: str,
    search_themes: list[str],
    campaign_theme_keys: Optional[set[str]] = None,
    validate_only: bool,
    max_successes: Optional[int] = None,
) -> list[str]:
    existing = _existing_asset_group_search_themes(client, account, asset_group_resource_name)
    campaign_theme_keys = campaign_theme_keys if campaign_theme_keys is not None else set()
    themes = _dedupe_texts([_clean_search_theme(theme) for theme in search_themes], limit=80, max_items=PMAX_SEARCH_THEME_SIGNAL_LIMIT)
    if max_successes is not None:
        max_successes = max(int(max_successes), 0)
        if max_successes <= 0:
            return []
        operations: list[dict[str, Any]] = []
        accepted_keys: list[tuple[str, str]] = []
        for theme in themes:
            key = _keyword_key(theme)
            if not theme or theme.lower() in existing or key in campaign_theme_keys:
                continue
            operations.append(
                {
                    "assetGroupSignalOperation": {
                        "create": {
                            "assetGroup": asset_group_resource_name,
                            "searchTheme": {"text": theme},
                        }
                    }
                }
            )
            accepted_keys.append((theme.lower(), key))
            if len(operations) >= max_successes:
                break
        if not operations:
            return []
        created = _mutate_search_theme_signals_rest(
            client,
            account,
            operations=operations,
            validate_only=validate_only,
        )
        for theme_key, campaign_key in accepted_keys:
            existing.add(theme_key)
            campaign_theme_keys.add(campaign_key)
        return created
    operations = []
    for theme in themes:
        key = _keyword_key(theme)
        if not theme or theme.lower() in existing or key in campaign_theme_keys:
            continue
        campaign_theme_keys.add(key)
        operations.append(
            {
                "assetGroupSignalOperation": {
                    "create": {
                        "assetGroup": asset_group_resource_name,
                        "searchTheme": {"text": theme},
                    }
                }
            }
        )
    if not operations:
        return []
    return _mutate_search_theme_signals_rest(
        client,
        account,
        operations=operations,
        validate_only=validate_only,
    )


def _search_theme_market_label(assets: dict[str, Any]) -> str:
    identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
    root_domain = str(identity.get("root_domain") or assets.get("final_url") or "").lower()
    if ".ca" in root_domain:
        return "canada"
    if ".com.au" in root_domain or root_domain.endswith(".au"):
        return "australia"
    if ".co.uk" in root_domain or root_domain.endswith(".uk"):
        return "uk"
    if ".co.nz" in root_domain or root_domain.endswith(".nz"):
        return "new zealand"
    return ""


def _policy_safe_search_theme_fallback_terms(assets: dict[str, Any]) -> list[str]:
    market = _search_theme_market_label(assets)
    suffix = f" {market}" if market else ""
    base_terms = [
        "makeup cosmetics",
        "beauty products online",
        "natural skincare",
        "organic beauty",
        "bath body products",
        "personal care products",
        "skin care store",
        "hair care products",
        "clean beauty",
        "cruelty free beauty",
        "body care products",
        "online beauty store",
        "beauty essentials",
        "self care products",
        "personal grooming products",
        "pet supplies",
        "dog food supplies",
        "cat food supplies",
        "sporting goods",
        "fitness accessories",
        "office supplies",
        "home wellness products",
        "healthy lifestyle store",
        "natural personal care",
        "wellness store",
        "beauty and personal care",
        "online personal care store",
        "natural body care",
        "face care products",
        "body wash products",
        "shampoo conditioner",
        "deodorant personal care",
        "hand cream",
        "lip care",
        "beauty deals",
        "beauty shop",
        "personal care deals",
        "bath care",
        "grooming essentials",
        "skin moisturizer",
        "beauty brands",
        "bath products online",
    ]
    return [f"{term}{suffix}".strip() for term in base_terms]


def _pmax_search_theme_backfill_terms(assets: dict[str, Any]) -> list[str]:
    plan = assets.get("pmax_search_theme_plan") if isinstance(assets.get("pmax_search_theme_plan"), dict) else {}
    raw_terms: list[str] = []
    for key in (
        "additional_asset_group_candidates",
        "pending_partial_asset_group_candidates",
        "overflow_candidates",
        "active",
    ):
        for item in plan.get(key) or []:
            if isinstance(item, dict):
                raw_terms.append(str(item.get("search_theme") or ""))
            else:
                raw_terms.append(str(item or ""))
    for campaign_plan in plan.get("campaign_plans") or []:
        if not isinstance(campaign_plan, dict):
            continue
        for group in campaign_plan.get("asset_groups") or []:
            if isinstance(group, dict):
                raw_terms.extend(str(item or "") for item in group.get("search_themes") or [])
    raw_terms.extend(_policy_safe_search_theme_fallback_terms(assets))
    return _dedupe_texts([_clean_search_theme(item) for item in raw_terms], limit=80, max_items=800)


def _search_themes_needed_for_asset_group(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_group_resource_name: str,
    planned_terms: list[str],
    campaign_theme_keys: set[str],
    backfill_terms: list[str],
) -> tuple[list[str], int, int]:
    if not hasattr(client, "get_service"):
        existing = set()
    else:
        existing = _existing_asset_group_search_themes(client, account, asset_group_resource_name)
    missing = max(PMAX_SEARCH_THEME_SIGNAL_LIMIT - len(existing), 0)
    if missing <= 0:
        return [], len(existing), 0
    selected: list[str] = []
    selected_keys: set[str] = set()
    existing_keys = {_keyword_key(item) for item in existing}
    candidate_limit = min(PMAX_SEARCH_THEME_SIGNAL_LIMIT, max(missing, missing + 25))
    for source in (planned_terms, backfill_terms):
        for term in source:
            cleaned = _clean_search_theme(term)
            key = _keyword_key(cleaned)
            if not cleaned or key in selected_keys or key in existing_keys or key in campaign_theme_keys:
                continue
            selected.append(cleaned)
            selected_keys.add(key)
            if len(selected) >= candidate_limit:
                return selected, len(existing), missing
    return selected, len(existing), missing


def _audience_terms_from_plan(plan: dict[str, Any], group: dict[str, Any]) -> list[str]:
    terms = []
    terms.extend(str(item or "") for item in (group.get("search_themes") or []) if str(item or "").strip())
    terms.extend(str(item or "") for item in (plan.get("custom_segment_terms") or []) if str(item or "").strip())
    return _dedupe_texts([_clean_search_theme(item) for item in terms], limit=80, max_items=50)


def _audience_urls_from_plan(plan: dict[str, Any]) -> list[str]:
    urls = []
    for item in plan.get("similar_website_urls") or []:
        root = root_domain(str(item or ""))
        if root:
            urls.append(root)
    return _dedupe_texts(urls, limit=250, max_items=50)


def _audience_signal_names(campaign_code: str, group_code: str) -> tuple[str, str]:
    suffix = f"{campaign_code} | {group_code}"
    return (
        _clip(f"AUTO | PMax Custom Segment | {suffix}", 255),
        _clip(f"AUTO | PMax Audience Signal | {suffix}", 255),
    )


def _custom_audience_by_name(client: Any, account: GoogleAdsAccount, name: str) -> Optional[str]:
    if not name:
        return None
    query = f"""
        SELECT
          custom_audience.resource_name,
          custom_audience.name,
          custom_audience.status
        FROM custom_audience
        WHERE custom_audience.name = {gaql_string(name)}
          AND custom_audience.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return str(row.custom_audience.resource_name or "")
    return None


def _audience_by_name(client: Any, account: GoogleAdsAccount, name: str) -> Optional[str]:
    if not name:
        return None
    query = f"""
        SELECT
          audience.resource_name,
          audience.name,
          audience.status
        FROM audience
        WHERE audience.name = {gaql_string(name)}
          AND audience.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return str(row.audience.resource_name or "")
    return None


def _create_custom_audience(
    client: Any,
    account: GoogleAdsAccount,
    *,
    name: str,
    terms: list[str],
    urls: list[str],
    validate_only: bool,
) -> str:
    operation = client.get_type("CustomAudienceOperation")
    custom_audience = operation.create
    custom_audience.name = name
    custom_audience.description = _clip("Automation PMax audience signal from saved keyword, GA4, auction and manual inputs.", 255)
    custom_audience.type_ = client.enums.CustomAudienceTypeEnum.SEARCH
    member_type = type(client.get_type("CustomAudienceMember"))
    for term in terms:
        member = member_type()
        member.member_type = client.enums.CustomAudienceMemberTypeEnum.KEYWORD
        member.keyword = term
        custom_audience.members.append(member)
    for url in urls:
        member = member_type()
        member.member_type = client.enums.CustomAudienceMemberTypeEnum.URL
        member.url = url
        custom_audience.members.append(member)
    request = client.get_type("MutateCustomAudiencesRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _call_with_alarm(
        lambda: client.get_service("CustomAudienceService").mutate_custom_audiences(request=request, timeout=30),
        seconds=35,
        label="CustomAudienceService.mutate_custom_audiences",
    )
    return str(response.results[0].resource_name or "")


def _create_audience(
    client: Any,
    account: GoogleAdsAccount,
    *,
    name: str,
    custom_audience_resource_name: str,
    google_audience_segments: list[dict[str, Any]],
    asset_group_resource_name: str,
    validate_only: bool,
) -> str:
    operation = client.get_type("AudienceOperation")
    audience = operation.create
    audience.name = name
    audience.description = _clip("Automation PMax asset-group signal audience.", 255)
    audience.scope = client.enums.AudienceScopeEnum.ASSET_GROUP
    audience.asset_group = asset_group_resource_name
    dimension = client.get_type("AudienceDimension")
    if custom_audience_resource_name:
        segment = client.get_type("AudienceSegment")
        segment.custom_audience.custom_audience = custom_audience_resource_name
        dimension.audience_segments.segments.append(segment)
    for item in google_audience_segments:
        resource_name = str(item.get("resource_name") or "").strip()
        if not resource_name:
            continue
        segment = client.get_type("AudienceSegment")
        kind = str(item.get("type") or "").strip().lower()
        if kind == "detailed_demographic":
            segment.detailed_demographic.detailed_demographic = resource_name
        elif kind == "life_event":
            segment.life_event.life_event = resource_name
        else:
            segment.user_interest.user_interest = resource_name
        dimension.audience_segments.segments.append(segment)
    if not dimension.audience_segments.segments:
        return ""
    audience.dimensions.append(dimension)
    request = client.get_type("MutateAudiencesRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _call_with_alarm(
        lambda: client.get_service("AudienceService").mutate_audiences(request=request, timeout=30),
        seconds=35,
        label="AudienceService.mutate_audiences",
    )
    return str(response.results[0].resource_name or "")


def _ensure_pmax_audience_signal_audience(
    client: Any,
    account: GoogleAdsAccount,
    *,
    campaign_code: str,
    group_code: str,
    asset_group_resource_name: str,
    plan: dict[str, Any],
    group: dict[str, Any],
    validate_only: bool,
) -> dict[str, Any]:
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return {"status": "skipped", "reason": "Audience signals are disabled or empty."}
    terms = _audience_terms_from_plan(plan, group)
    urls = _audience_urls_from_plan(plan)
    google_segments = [
        item
        for item in (plan.get("google_audience_segments") or [])
        if isinstance(item, dict) and str(item.get("resource_name") or "").strip()
    ][:20]
    if not terms and not urls and not google_segments:
        return {"status": "skipped", "reason": "No audience-signal terms or similar URLs are available."}
    custom_name, audience_name = _audience_signal_names(campaign_code, group_code)
    custom_resource = None if validate_only else _custom_audience_by_name(client, account, custom_name)
    created_custom = False
    if (terms or urls) and not custom_resource:
        custom_resource = _create_custom_audience(
            client,
            account,
            name=custom_name,
            terms=terms,
            urls=urls,
            validate_only=validate_only,
        )
        created_custom = True
    audience_resource = None if validate_only else _audience_by_name(client, account, audience_name)
    created_audience = False
    if not audience_resource:
        audience_resource = _create_audience(
            client,
            account,
            name=audience_name,
            custom_audience_resource_name=custom_resource,
            google_audience_segments=google_segments,
            asset_group_resource_name=asset_group_resource_name,
            validate_only=validate_only,
        )
        created_audience = True
    return {
        "status": "ready",
        "audience_resource_name": audience_resource,
        "custom_audience_resource_name": custom_resource,
        "custom_audience_name": custom_name,
        "audience_name": audience_name,
        "term_count": len(terms),
        "similar_url_count": len(urls),
        "google_audience_segment_count": len(google_segments),
        "created_custom_audience": created_custom,
        "created_audience": created_audience,
    }


def _existing_asset_group_audience_signals(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_group_resource_name: str,
) -> set[str]:
    query = f"""
        SELECT
          asset_group_signal.audience.audience
        FROM asset_group_signal
        WHERE asset_group_signal.asset_group = {gaql_string(asset_group_resource_name)}
    """
    service = client.get_service("GoogleAdsService")
    values: set[str] = set()
    for row in _google_ads_search(service, account, query):
        resource = str(row.asset_group_signal.audience.audience or "").strip()
        if resource:
            values.add(resource)
    return values


def _create_audience_signal(
    client: Any,
    account: GoogleAdsAccount,
    *,
    asset_group_resource_name: str,
    audience_resource_name: str,
    validate_only: bool,
) -> list[str]:
    if not asset_group_resource_name or not audience_resource_name:
        return []
    existing = _existing_asset_group_audience_signals(
        client,
        account,
        asset_group_resource_name=asset_group_resource_name,
    )
    if audience_resource_name in existing:
        return []
    operation = client.get_type("MutateOperation")
    signal = operation.asset_group_signal_operation.create
    signal.asset_group = asset_group_resource_name
    signal.audience.audience = audience_resource_name
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = account.customer_id
    request.mutate_operations.append(operation)
    try:
        request.partial_failure = True
    except Exception:  # noqa: BLE001 - field availability varies by request type.
        pass
    request.validate_only = validate_only
    response = _call_with_alarm(
        lambda: client.get_service("GoogleAdsService").mutate(request=request, timeout=30),
        seconds=35,
        label="GoogleAdsService.mutate_asset_group_signal_audience",
    )
    resources: list[str] = []
    for item in getattr(response, "mutate_operation_responses", []) or []:
        resource = str(getattr(item.asset_group_signal_result, "resource_name", "") or "")
        if resource:
            resources.append(resource)
    return resources


def _message_strings(value: Any) -> list[str]:
    try:
        if hasattr(value, "_pb"):
            value = MessageToDict(value._pb, preserving_proto_field_name=True)
    except Exception:  # noqa: BLE001 - best effort only for policy evidence.
        pass
    strings: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            strings.extend(_message_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            strings.extend(_message_strings(item))
    elif isinstance(value, str):
        cleaned = " ".join(value.split())
        if cleaned:
            strings.append(cleaned)
    return strings


def _restricted_terms_from_policy_strings(strings: list[str]) -> list[str]:
    candidates: list[str] = []
    joined_context = " | ".join(strings).lower()
    if "unapproved" not in joined_context and "substance" not in joined_context:
        return []
    for text in strings:
        working = re.sub(r"(?i)^destination contains:?", "", text).strip(" .:-")
        working = re.sub(r"(?i)^destination contains", "", working).strip(" .:-")
        working = re.sub(r"(?i)^contains:?", "", working).strip(" .:-")
        if not working:
            continue
        if re.search(
            r"(?i)(remove any references|read the policy|unapproved substances|prohibited from ad text|certificate required)",
            working,
        ):
            continue
        for part in re.split(r",|\band\b|;", working, flags=re.IGNORECASE):
            term = part.strip(" .:-")
            term = re.sub(r"\s+", " ", term)
            if len(term) < 3 or len(term) > 80:
                continue
            normalized = normalize_restricted_text(term)
            if not normalized or normalized in {
                "destination contains",
                "contains",
                "unapproved substances",
                "remove any references",
                "read the policy",
                "certificate required in canada",
            }:
                continue
            candidates.append(term)
    return list(dict.fromkeys(candidates))


def _disapproved_policy_term_candidates(client: Any, account: GoogleAdsAccount) -> dict[str, Any]:
    service = client.get_service("GoogleAdsService")
    found_terms: list[str] = []
    evidence_rows: list[dict[str, Any]] = []
    queries = [
        (
            "ad_group_ad",
            """
                SELECT
                  campaign.id,
                  campaign.name,
                  ad_group_ad.ad.id,
                  ad_group_ad.policy_summary.approval_status,
                  ad_group_ad.policy_summary.policy_topic_entries
                FROM ad_group_ad
                WHERE campaign.name LIKE 'AUTO |%'
                  AND ad_group_ad.status != REMOVED
                  AND ad_group_ad.policy_summary.approval_status = DISAPPROVED
                LIMIT 200
            """,
        ),
        (
            "asset_group_asset",
            """
                SELECT
                  campaign.id,
                  campaign.name,
                  asset_group.id,
                  asset_group_asset.policy_summary.approval_status,
                  asset_group_asset.policy_summary.policy_topic_entries
                FROM asset_group_asset
                WHERE campaign.name LIKE 'AUTO |%'
                  AND asset_group_asset.status != REMOVED
                  AND asset_group_asset.policy_summary.approval_status = DISAPPROVED
                LIMIT 500
            """,
        ),
    ]
    errors: list[str] = []
    for scope, query in queries:
        try:
            rows = _google_ads_search(service, account, query)
            for row in rows:
                policy_summary = (
                    row.ad_group_ad.policy_summary
                    if scope == "ad_group_ad"
                    else row.asset_group_asset.policy_summary
                )
                strings = _message_strings(policy_summary)
                terms = _restricted_terms_from_policy_strings(strings)
                if terms:
                    found_terms.extend(terms)
                    evidence_rows.append(
                        {
                            "scope": scope,
                            "campaign_id": int(row.campaign.id or 0),
                            "campaign_name": str(row.campaign.name or ""),
                            "terms": terms,
                            "policy_strings": strings[:12],
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - policy fields vary; do not block publisher.
            errors.append(f"{scope}: {str(exc)[:240]}")
    return {
        "terms": list(dict.fromkeys(found_terms)),
        "evidence_rows": evidence_rows,
        "errors": errors,
    }


def _append_restricted_terms_setting(session: Session, terms: list[str]) -> dict[str, Any]:
    if not terms:
        return {"new_terms": [], "existing_term_count": 0, "updated": False}
    row = session.scalar(select(AppSetting).where(AppSetting.key == RESTRICTED_TERMS_SETTING_KEY))
    if row is None:
        row = AppSetting(
            key=RESTRICTED_TERMS_SETTING_KEY,
            value="",
            category="Feeds",
            label="Restricted page-feed title terms",
            help_text="Products whose Odoo title contains any term are excluded from Google page feeds.",
            input_type="textarea",
            sensitive=False,
        )
        session.add(row)
        session.flush()
    existing_lines = [str(item).strip() for item in str(row.value or "").splitlines() if str(item).strip()]
    existing_keys = {normalize_restricted_text(item) for item in existing_lines if normalize_restricted_text(item)}
    new_terms: list[str] = []
    for term in terms:
        normalized = normalize_restricted_text(term)
        if normalized and normalized not in existing_keys:
            existing_keys.add(normalized)
            existing_lines.append(term)
            new_terms.append(term)
    if new_terms:
        row.value = "\n".join(existing_lines)
    return {"new_terms": new_terms, "existing_term_count": len(existing_lines) - len(new_terms), "updated": bool(new_terms)}


def _shared_negative_set_by_name(client: Any, account: GoogleAdsAccount, name: str) -> Optional[str]:
    query = f"""
        SELECT
          shared_set.resource_name,
          shared_set.name,
          shared_set.type,
          shared_set.status
        FROM shared_set
        WHERE shared_set.name = {gaql_string(name)}
          AND shared_set.type = NEGATIVE_KEYWORDS
          AND shared_set.status != REMOVED
        LIMIT 1
    """
    service = client.get_service("GoogleAdsService")
    for row in _google_ads_search(service, account, query):
        return str(row.shared_set.resource_name or "")
    return None


def _create_shared_negative_set(client: Any, account: GoogleAdsAccount, *, name: str, validate_only: bool) -> str:
    operation = client.get_type("SharedSetOperation")
    shared_set = operation.create
    shared_set.name = name
    shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
    request = client.get_type("MutateSharedSetsRequest")
    request.customer_id = account.customer_id
    request.operations.append(operation)
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("SharedSetService"), "mutate_shared_sets", request)
    return str(response.results[0].resource_name or "")


def _existing_shared_negative_keywords(client: Any, account: GoogleAdsAccount, shared_set_resource_name: str) -> set[str]:
    query = f"""
        SELECT
          shared_criterion.keyword.text
        FROM shared_criterion
        WHERE shared_criterion.shared_set = {gaql_string(shared_set_resource_name)}
          AND shared_criterion.type = KEYWORD
    """
    service = client.get_service("GoogleAdsService")
    existing: set[str] = set()
    for row in _google_ads_search(service, account, query):
        key = _keyword_key(row.shared_criterion.keyword.text)
        if key:
            existing.add(key)
    return existing


def _create_shared_negative_keywords(
    client: Any,
    account: GoogleAdsAccount,
    *,
    shared_set_resource_name: str,
    terms: list[str],
    validate_only: bool,
) -> list[str]:
    if not shared_set_resource_name or not terms:
        return []
    existing = set() if validate_only else _existing_shared_negative_keywords(client, account, shared_set_resource_name)
    operations = []
    for term in _dedupe_texts(terms, limit=80, max_items=200):
        key = _keyword_key(term)
        if not key or key in existing:
            continue
        operation = client.get_type("SharedCriterionOperation")
        criterion = operation.create
        criterion.shared_set = shared_set_resource_name
        criterion.keyword.text = term
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.EXACT
        operations.append(operation)
        existing.add(key)
    if not operations:
        return []
    request = client.get_type("MutateSharedCriteriaRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = _google_ads_mutate(client.get_service("SharedCriterionService"), "mutate_shared_criteria", request)
    return _successful_resource_names(response)


def sync_restricted_policy_terms(
    session: Session,
    client: Any,
    account: GoogleAdsAccount,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    settings_map = get_sync_setting_map(session)
    if not parse_bool(settings_map.get("live_campaign_creator.restricted_policy_scan_enabled", True)):
        return {
            "name": "restricted_policy_terms",
            "status": "skipped_disabled",
            "term_count": 0,
            "terms": [],
            "new_postgres_terms": [],
            "shared_set_name": RESTRICTED_SHARED_SET_NAME,
            "shared_set_resource_name": None,
            "created_shared_set": False,
            "new_shared_keyword_count": 0,
            "new_shared_keyword_resources": [],
            "evidence_rows": [],
            "errors": [],
        }
    try:
        timeout_seconds = int(float(settings_map.get("live_campaign_creator.restricted_policy_scan_timeout_seconds", 20) or 20))
    except (TypeError, ValueError):
        timeout_seconds = 20
    timeout_seconds = min(max(timeout_seconds, 5), 60)
    try:
        scan = _call_with_alarm(
            lambda: _disapproved_policy_term_candidates(client, account),
            seconds=timeout_seconds,
            label="restricted policy scan",
        )
    except TimeoutError as exc:
        return {
            "name": "restricted_policy_terms",
            "status": "skipped_timeout",
            "term_count": 0,
            "terms": [],
            "new_postgres_terms": [],
            "shared_set_name": RESTRICTED_SHARED_SET_NAME,
            "shared_set_resource_name": None,
            "created_shared_set": False,
            "new_shared_keyword_count": 0,
            "new_shared_keyword_resources": [],
            "evidence_rows": [],
            "errors": [str(exc)],
        }
    except Exception as exc:  # noqa: BLE001 - policy sync should not block live publishing.
        return {
            "name": "restricted_policy_terms",
            "status": "skipped_error",
            "term_count": 0,
            "terms": [],
            "new_postgres_terms": [],
            "shared_set_name": RESTRICTED_SHARED_SET_NAME,
            "shared_set_resource_name": None,
            "created_shared_set": False,
            "new_shared_keyword_count": 0,
            "new_shared_keyword_resources": [],
            "evidence_rows": [],
            "errors": [str(exc)[:500]],
        }
    terms = [term for term in scan["terms"] if _valid_exact_keyword(term)]
    setting_result = _append_restricted_terms_setting(session, terms)
    shared_set_resource = None if validate_only else _shared_negative_set_by_name(client, account, RESTRICTED_SHARED_SET_NAME)
    created_shared_set = False
    if terms and not shared_set_resource:
        shared_set_resource = _create_shared_negative_set(
            client,
            account,
            name=RESTRICTED_SHARED_SET_NAME,
            validate_only=validate_only,
        )
        created_shared_set = True
    shared_keyword_resources = _create_shared_negative_keywords(
        client,
        account,
        shared_set_resource_name=str(shared_set_resource or ""),
        terms=terms,
        validate_only=validate_only,
    )
    return {
        "name": "restricted_policy_terms",
        "status": "updated" if setting_result.get("new_terms") or shared_keyword_resources else "skipped",
        "term_count": len(terms),
        "terms": terms,
        "new_postgres_terms": setting_result.get("new_terms") or [],
        "shared_set_name": RESTRICTED_SHARED_SET_NAME,
        "shared_set_resource_name": shared_set_resource,
        "created_shared_set": created_shared_set,
        "new_shared_keyword_count": len(shared_keyword_resources),
        "new_shared_keyword_resources": shared_keyword_resources,
        "evidence_rows": scan.get("evidence_rows") or [],
        "errors": scan.get("errors") or [],
    }


def _pmax_asset_group_plan(assets: dict[str, Any]) -> list[dict[str, Any]]:
    groups = [item for item in (assets.get("pmax_asset_groups") or []) if isinstance(item, dict)]
    if groups:
        return groups
    themes = [str(item or "").strip() for item in (assets.get("pmax_search_themes") or assets.get("source_terms") or []) if str(item or "").strip()]
    return [
        {
            "asset_group_index": 1,
            "asset_group_code": "AG001",
            "search_themes": themes[:50],
        }
    ]


def _text_asset_links_for_pmax(client: Any, text_assets: dict[str, str], assets: dict[str, Any]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for headline in _dedupe_texts([str(item) for item in (assets.get("headlines") or [])], limit=30, max_items=15):
        resource = text_assets.get(headline)
        if resource:
            links.append((resource, "HEADLINE"))
    for headline in _dedupe_texts([str(item) for item in (assets.get("long_headlines") or [])], limit=90, max_items=5):
        resource = text_assets.get(headline)
        if resource:
            links.append((resource, "LONG_HEADLINE"))
    for description in _dedupe_texts([str(item) for item in (assets.get("descriptions") or [])], limit=90, max_items=5):
        resource = text_assets.get(description)
        if resource:
            links.append((resource, "DESCRIPTION"))
    business_name = _clip(str(assets.get("business_name") or ""), 25)
    resource = text_assets.get(business_name)
    if business_name and resource:
        links.append((resource, "BUSINESS_NAME"))
    return links


def _media_asset_links_for_pmax(media_assets: dict[str, list[str]]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for field_type, count in PMAX_MEDIA_FIELD_TYPES.items():
        for resource_name in (media_assets.get(field_type) or [])[:count]:
            links.append((resource_name, field_type))
    return links


def publish_dsa_draft(
    session: Session,
    client: Any,
    account: GoogleAdsAccount,
    preference: GoogleAdsAutomationPreference,
    draft: AdDraft,
    *,
    validate_only: bool = False,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = _draft_identity(draft)
    campaign_code = str(identity.get("campaign_code") or "").strip()
    campaign_name = str(identity.get("campaign_name") or assets.get("campaign_name") or "").strip()
    if not campaign_code or not campaign_name:
        return {"draft_id": draft.id, "ad_type": draft.ad_type, "status": "blocked", "reason": "Missing automation campaign identity."}

    bidding = assets.get("bidding") if isinstance(assets.get("bidding"), dict) else {}
    budget_block = _budget_block_result(session, draft, ad_type="dsa", bidding=bidding, preference=preference)
    if budget_block is not None:
        return budget_block
    daily_budget = _float_value(bidding.get("daily_budget"), 0.0)
    budget_blocked = bool(bidding.get("budget_blocked"))
    effective_daily_budget = max(daily_budget, _currency_minimum_daily_budget(account, preference.minimum_daily_budget_amount), 1.0)
    max_cpc = _float_value(bidding.get("max_cpc_bid_limit"), 2.5)
    final_url = str(assets.get("final_url") or draft.final_url or draft.website_url or "").strip()
    domain = root_domain(final_url)
    page_targeting = assets.get("page_targeting") if isinstance(assets.get("page_targeting"), dict) else {}
    requires_url_inclusions = bool(page_targeting.get("requires_url_inclusions")) or str(
        page_targeting.get("mode") or ""
    ).startswith("core_scale")
    planned_url_inclusions = _url_inclusion_urls_from_draft(draft)
    criteria_csv_pending = _manual_first_run_criteria_csv_pending(preference, draft)
    if requires_url_inclusions and not planned_url_inclusions:
        return {
            "draft_id": draft.id,
            "ad_type": "dsa",
            "status": "blocked_missing_core_url_inclusions",
            "reason": "Core / Scale DSA requires proven URL inclusions and will not fall back to all-pages targeting.",
            "campaign_code": campaign_code,
            "campaign_name": campaign_name,
        }
    category = _clip(str(identity.get("category") or "Testing / Discovery"), 44)
    ai_max_final_url_expansion = "core / scale" not in str(category or campaign_name or "").lower()
    base_ad_group_name = f"AUTO | {category} | Expanded DSA AI Max Target | {campaign_code}"
    ad_group_name = base_ad_group_name

    operations: list[dict[str, str]] = []

    def checkpoint(name: str, status: str = "started", **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                name,
                status,
                draft_id=draft.id,
                ad_type="dsa",
                campaign_name=campaign_name,
                campaign_code=campaign_code,
                **extra,
            )
        except Exception:
            pass

    existing_campaign = _campaign_by_code_or_lane(client, account, campaign_code, campaign_name)
    campaign = existing_campaign
    created_campaign = False
    if campaign is None:
        budget_resource = _create_campaign_budget(
            client,
            account,
            name=f"{campaign_name[:220]} budget",
            amount_micros=_micros(effective_daily_budget),
            validate_only=validate_only,
        )
        campaign_resource = _create_search_campaign(
            client,
            account,
            name=campaign_name,
            budget_resource_name=budget_resource,
            max_cpc_micros=_micros(max_cpc),
            bidding=bidding,
            domain_name=domain,
            enable_ai_max=True,
            ai_max_final_url_expansion=ai_max_final_url_expansion,
            validate_only=validate_only,
        )
        campaign = {
            "campaign_id": _resource_id(campaign_resource),
            "campaign_name": campaign_name,
            "campaign_resource_name": campaign_resource,
            "campaign_status": "ENABLED",
            "channel_type": "SEARCH",
            "matched_by": "created",
            "campaign_code": campaign_code,
            "budget_resource_name": budget_resource,
        }
        created_campaign = True
        operations.append({"operation": "create_campaign_budget", "resource_name": budget_resource})
        operations.append({"operation": "create_search_ai_max_dynamic_campaign", "resource_name": campaign_resource})

    campaign_id = int(campaign.get("campaign_id") or 0)
    campaign_resource = str(campaign.get("campaign_resource_name") or "")
    ai_max_resource = None if created_campaign else _enable_campaign_ai_max(
        client,
        account,
        campaign_resource,
        final_url_expansion=ai_max_final_url_expansion,
        validate_only=validate_only,
    )
    if ai_max_resource:
        operations.append({"operation": "enable_campaign_ai_max", "resource_name": ai_max_resource})
    enabled_campaign_resource = _enable_campaign_if_needed(client, account, campaign, validate_only=validate_only)
    if enabled_campaign_resource:
        operations.append({"operation": "enable_existing_campaign", "resource_name": enabled_campaign_resource})
    country_target = resolve_account_country_target(session, account, final_url=final_url)
    checkpoint("dsa_campaign_targets")
    location_result = _ensure_campaign_country_target(
        client,
        account,
        campaign_id=campaign_id,
        campaign_resource_name=campaign_resource,
        country_target=country_target,
        validate_only=validate_only,
    )
    checkpoint("dsa_campaign_targets", location_result.get("status") or "done")
    if location_result.get("status") in {"updated", "validated"}:
        operations.append(
            {
                "operation": "set_campaign_country_target",
                "resource_name": str(location_result.get("geo_target_constant") or ""),
            }
        )
    checkpoint("dsa_negative_keywords")
    if criteria_csv_pending:
        negative_result = _manual_csv_deferred_negative_keyword_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_negative_keywords",
            int(negative_result.get("negative_keyword_count") or 0),
        )
    else:
        negative_result = _apply_campaign_negative_keywords(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    checkpoint("dsa_negative_keywords", "done", created=len(negative_result.get("negative_keyword_resources") or []))
    checkpoint("dsa_page_exclusions")
    if criteria_csv_pending:
        page_result = _manual_csv_deferred_negative_page_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_url_exclusions",
            int(page_result.get("negative_page_count") or 0),
        )
    else:
        page_result = _apply_campaign_negative_webpages(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    checkpoint("dsa_page_exclusions", "done", created=len(page_result.get("negative_page_resources") or []))
    if negative_result["negative_keyword_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_exact_keywords",
                "resource_name": f"{len(negative_result['negative_keyword_resources'])} negatives",
            }
        )
    if page_result["negative_page_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_webpages",
                "resource_name": f"{len(page_result['negative_page_resources'])} page exclusions",
            }
        )
    ad_group = _ad_group_by_name(client, account, campaign_id, ad_group_name) if campaign_id else None
    if ad_group is not None and not _is_dynamic_search_ad_group(ad_group):
        return {
            "draft_id": draft.id,
            "ad_type": "dsa",
            "status": "blocked_legacy_standard_ad_group",
            "reason": "Automation-owned DSA ad group name is already used by a non-dynamic Search ad group; rename/remove that legacy AUTO ad group before publishing the closed-loop DSA lane.",
            "campaign_code": campaign_code,
            "campaign_name": campaign_name,
            "campaign": campaign,
            "ad_group": ad_group,
            "operations": operations,
        }
    if _is_dynamic_search_ad_group(ad_group):
        enabled_ad_group_resource = _enable_ad_group_if_needed(client, account, ad_group, validate_only=validate_only)
        if enabled_ad_group_resource:
            operations.append({"operation": "enable_existing_dynamic_ad_group", "resource_name": enabled_ad_group_resource})
        ad_group_id = int(ad_group.get("ad_group_id") or 0)
        dsa_ad = _dsa_ad_by_ad_group(client, account, ad_group_id) if ad_group_id else None
        if dsa_ad is None:
            descriptions = [str(item) for item in (assets.get("descriptions") or []) if str(item).strip()]
            ad_resource = _create_dsa_ad(
                client,
                account,
                ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
                descriptions=descriptions,
                validate_only=validate_only,
            )
            dsa_ad = {
                "ad_group_ad_resource_name": ad_resource,
                "ad_status": "ENABLED",
                "ad_type": "EXPANDED_DYNAMIC_SEARCH_AD",
                "matched_by": "created",
            }
            operations.append({"operation": "create_expanded_dynamic_search_ad", "resource_name": ad_resource})
        enabled_ad_resource = _enable_ad_group_ad_if_needed(client, account, dsa_ad, validate_only=validate_only)
        if enabled_ad_resource:
            operations.append({"operation": "enable_existing_dynamic_search_ad", "resource_name": enabled_ad_resource})
        if criteria_csv_pending:
            inclusion_result = _manual_csv_deferred_url_inclusion_result(draft)
            _append_manual_csv_deferred_operation(
                operations,
                "ad_group_url_inclusions",
                int(inclusion_result.get("url_inclusion_count") or 0),
            )
        else:
            try:
                inclusion_result = _apply_ad_group_webpage_inclusions(
                    client,
                    account,
                    draft,
                    session=session,
                    ad_group_id=ad_group_id,
                    ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
                    validate_only=validate_only,
                )
            except GoogleAdsException as exc:
                inclusion_result = {
                    "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                    "existing_url_inclusion_count": 0,
                    "new_url_inclusion_count": 0,
                    "url_inclusion_resources": [],
                    "url_inclusion_error": summarize_google_ads_exception(exc),
                }
            except Exception as exc:  # noqa: BLE001 - URL inclusions should not kill DSA creation.
                inclusion_result = {
                    "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                    "existing_url_inclusion_count": 0,
                    "new_url_inclusion_count": 0,
                    "url_inclusion_resources": [],
                    "url_inclusion_error": {"message": str(exc)[:500]},
                }
        if inclusion_result["url_inclusion_resources"]:
            operations.append(
                {
                    "operation": "create_dynamic_ad_group_webpage_inclusions",
                    "resource_name": f"{len(inclusion_result['url_inclusion_resources'])} URL inclusions",
                }
            )
        webpage = None
        if not inclusion_result["url_inclusion_count"]:
            webpage = _webpage_criterion_by_ad_group(client, account, ad_group_id) if ad_group_id else None
        if webpage is None and not inclusion_result["url_inclusion_count"]:
            criterion_resource = _create_all_pages_webpage_criterion(
                client,
                account,
                ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
                validate_only=validate_only,
            )
            webpage = {
                "criterion_resource_name": criterion_resource,
                "criterion_status": "ENABLED",
                "criterion_name": "AUTO all pages",
                "matched_by": "created",
            }
            operations.append({"operation": "create_dynamic_all_pages_webpage_criterion", "resource_name": criterion_resource})
        return {
            "draft_id": draft.id,
            "ad_type": "dsa",
            "api_dynamic_mode": "ai_max_expanded_dynamic_search_ad_existing_dynamic_ad_group",
            "status": "validated" if validate_only else "published_enabled",
            "campaign_code": campaign_code,
            "campaign_name": campaign_name,
            "budget_blocked": budget_blocked,
            "effective_daily_budget": effective_daily_budget,
            "currency_code": account.currency_code,
            "force_paused": False,
            "campaign": campaign,
            "country_target": location_result,
            "ad_group": ad_group,
            "ad": dsa_ad,
            "webpage_criterion": webpage,
            **inclusion_result,
            **negative_result,
            **page_result,
            "operations": operations,
        }
    if ad_group is None:
        ad_group_resource = _create_ad_group(
            client,
            account,
            campaign_resource_name=str(campaign["campaign_resource_name"]),
            name=ad_group_name,
            ad_group_type="dsa",
            max_cpc_micros=_micros(max_cpc),
            validate_only=validate_only,
        )
        ad_group = {
            "ad_group_id": _resource_id(ad_group_resource),
            "ad_group_name": ad_group_name,
            "ad_group_resource_name": ad_group_resource,
            "ad_group_status": "ENABLED",
            "ad_group_type": "SEARCH_DYNAMIC_ADS",
            "matched_by": "created",
        }
        operations.append({"operation": "create_dynamic_search_ad_group", "resource_name": ad_group_resource})
    enabled_ad_group_resource = _enable_ad_group_if_needed(client, account, ad_group, validate_only=validate_only)
    if enabled_ad_group_resource:
        operations.append({"operation": "enable_existing_ad_group", "resource_name": enabled_ad_group_resource})

    ad_group_id = int(ad_group.get("ad_group_id") or 0)
    dsa_ad = _dsa_ad_by_ad_group(client, account, ad_group_id) if ad_group_id else None
    if dsa_ad is None:
        descriptions = [str(item) for item in (assets.get("descriptions") or []) if str(item).strip()]
        ad_resource = _create_dsa_ad(
            client,
            account,
            ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
            descriptions=descriptions,
            validate_only=validate_only,
        )
        dsa_ad = {
            "ad_group_ad_resource_name": ad_resource,
            "ad_status": "ENABLED",
            "ad_type": "EXPANDED_DYNAMIC_SEARCH_AD",
            "matched_by": "created",
        }
        operations.append({"operation": "create_expanded_dynamic_search_ad", "resource_name": ad_resource})
    enabled_ad_resource = _enable_ad_group_ad_if_needed(client, account, dsa_ad, validate_only=validate_only)
    if enabled_ad_resource:
        operations.append({"operation": "enable_existing_ad_group_ad", "resource_name": enabled_ad_resource})

    if criteria_csv_pending:
        inclusion_result = _manual_csv_deferred_url_inclusion_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "ad_group_url_inclusions",
            int(inclusion_result.get("url_inclusion_count") or 0),
        )
    else:
        try:
            inclusion_result = _apply_ad_group_webpage_inclusions(
                client,
                account,
                draft,
                session=session,
                ad_group_id=ad_group_id,
                ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
                validate_only=validate_only,
            )
        except GoogleAdsException as exc:
            inclusion_result = {
                "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                "existing_url_inclusion_count": 0,
                "new_url_inclusion_count": 0,
                "url_inclusion_resources": [],
                "url_inclusion_error": summarize_google_ads_exception(exc),
            }
        except Exception as exc:  # noqa: BLE001 - URL inclusions should not kill DSA creation.
            inclusion_result = {
                "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                "existing_url_inclusion_count": 0,
                "new_url_inclusion_count": 0,
                "url_inclusion_resources": [],
                "url_inclusion_error": {"message": str(exc)[:500]},
            }
    if inclusion_result["url_inclusion_resources"]:
        operations.append(
            {
                "operation": "create_ad_group_webpage_inclusions",
                "resource_name": f"{len(inclusion_result['url_inclusion_resources'])} URL inclusions",
            }
        )
    webpage = None
    if not inclusion_result["url_inclusion_count"]:
        webpage = _webpage_criterion_by_ad_group(client, account, ad_group_id) if ad_group_id else None
    if webpage is None and not inclusion_result["url_inclusion_count"]:
        criterion_resource = _create_all_pages_webpage_criterion(
            client,
            account,
            ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
            validate_only=validate_only,
        )
        webpage = {
            "criterion_resource_name": criterion_resource,
            "criterion_status": "ENABLED",
            "criterion_name": "AUTO all pages",
            "matched_by": "created",
        }
        operations.append({"operation": "create_all_pages_webpage_criterion", "resource_name": criterion_resource})

    return {
        "draft_id": draft.id,
        "ad_type": "dsa",
        "api_dynamic_mode": "ai_max_expanded_dynamic_search_ad_closed_loop",
        "status": "validated" if validate_only else "published_enabled",
        "campaign_code": campaign_code,
        "campaign_name": campaign_name,
        "budget_blocked": budget_blocked,
        "effective_daily_budget": effective_daily_budget,
        "currency_code": account.currency_code,
        "force_paused": False,
        "campaign": campaign,
        "country_target": location_result,
        "ad_group": ad_group,
        "ad": dsa_ad,
        "webpage_criterion": webpage,
        **inclusion_result,
        **negative_result,
        **page_result,
        "operations": operations,
    }


def publish_rsa_draft(
    session: Session,
    client: Any,
    account: GoogleAdsAccount,
    preference: GoogleAdsAutomationPreference,
    draft: AdDraft,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = _draft_identity(draft)
    campaign_code = str(identity.get("campaign_code") or "").strip()
    campaign_name = str(identity.get("campaign_name") or assets.get("campaign_name") or "").strip()
    if not campaign_code or not campaign_name:
        return {"draft_id": draft.id, "ad_type": draft.ad_type, "status": "blocked", "reason": "Missing automation campaign identity."}
    final_url = str(assets.get("final_url") or draft.final_url or draft.website_url or "").strip()
    if not final_url:
        return {"draft_id": draft.id, "ad_type": draft.ad_type, "status": "blocked", "reason": "Missing final URL."}

    bidding = assets.get("bidding") if isinstance(assets.get("bidding"), dict) else {}
    budget_block = _budget_block_result(session, draft, ad_type="rsa", bidding=bidding, preference=preference)
    if budget_block is not None:
        return budget_block
    daily_budget = _float_value(bidding.get("daily_budget"), 0.0)
    budget_blocked = bool(bidding.get("budget_blocked"))
    effective_daily_budget = max(daily_budget, _currency_minimum_daily_budget(account, preference.minimum_daily_budget_amount), 1.0)
    max_cpc = _float_value(bidding.get("max_cpc_bid_limit"), 2.5)
    ad_group_name = _rsa_ad_group_name_from_assets(assets, campaign_code)
    legacy_ad_group_name = f"AUTO | Testing / Discovery | RSA Keywords | {campaign_code}"
    ai_max_final_url_expansion = "core / scale" not in str(campaign_name or "").lower()
    criteria_csv_pending = _manual_first_run_criteria_csv_pending(preference, draft)

    operations: list[dict[str, str]] = []
    existing_campaign = _campaign_by_code_or_lane(client, account, campaign_code, campaign_name)
    campaign = existing_campaign
    created_campaign = False
    if campaign is None:
        budget_resource = _create_campaign_budget(
            client,
            account,
            name=f"{campaign_name[:220]} budget",
            amount_micros=_micros(effective_daily_budget),
            validate_only=validate_only,
        )
        campaign_resource = _create_search_campaign(
            client,
            account,
            name=campaign_name,
            budget_resource_name=budget_resource,
            max_cpc_micros=_micros(max_cpc),
            bidding=bidding,
            enable_ai_max=True,
            ai_max_final_url_expansion=ai_max_final_url_expansion,
            validate_only=validate_only,
        )
        campaign = {
            "campaign_id": _resource_id(campaign_resource),
            "campaign_name": campaign_name,
            "campaign_resource_name": campaign_resource,
            "campaign_status": "ENABLED",
            "channel_type": "SEARCH",
            "matched_by": "created",
            "campaign_code": campaign_code,
            "budget_resource_name": budget_resource,
        }
        created_campaign = True
        operations.append({"operation": "create_campaign_budget", "resource_name": budget_resource})
        operations.append({"operation": "create_search_rsa_campaign", "resource_name": campaign_resource})

    campaign_id = int(campaign.get("campaign_id") or 0)
    campaign_resource = str(campaign.get("campaign_resource_name") or "")
    ai_max_resource = None if created_campaign else _enable_campaign_ai_max(
        client,
        account,
        campaign_resource,
        final_url_expansion=ai_max_final_url_expansion,
        validate_only=validate_only,
    )
    if ai_max_resource:
        operations.append({"operation": "enable_campaign_ai_max", "resource_name": ai_max_resource})
    enabled_campaign_resource = _enable_campaign_if_needed(client, account, campaign, validate_only=validate_only)
    if enabled_campaign_resource:
        operations.append({"operation": "enable_existing_campaign", "resource_name": enabled_campaign_resource})
    country_target = resolve_account_country_target(session, account, final_url=final_url)
    location_result = _ensure_campaign_country_target(
        client,
        account,
        campaign_id=campaign_id,
        campaign_resource_name=campaign_resource,
        country_target=country_target,
        validate_only=validate_only,
    )
    if location_result.get("status") in {"updated", "validated"}:
        operations.append(
            {
                "operation": "set_campaign_country_target",
                "resource_name": str(location_result.get("geo_target_constant") or ""),
            }
        )
    if criteria_csv_pending:
        negative_result = _manual_csv_deferred_negative_keyword_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_negative_keywords",
            int(negative_result.get("negative_keyword_count") or 0),
        )
    else:
        negative_result = _apply_campaign_negative_keywords(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    if criteria_csv_pending:
        page_result = _manual_csv_deferred_negative_page_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_url_exclusions",
            int(page_result.get("negative_page_count") or 0),
        )
    else:
        page_result = _apply_campaign_negative_webpages(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    if negative_result["negative_keyword_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_exact_keywords",
                "resource_name": f"{len(negative_result['negative_keyword_resources'])} negatives",
            }
        )
    if page_result["negative_page_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_webpages",
                "resource_name": f"{len(page_result['negative_page_resources'])} page exclusions",
            }
        )
    ad_group = _ad_group_by_name(client, account, campaign_id, ad_group_name) if campaign_id else None
    if ad_group is None and campaign_id and ad_group_name != legacy_ad_group_name:
        ad_group = _ad_group_by_name(client, account, campaign_id, legacy_ad_group_name)
    if ad_group is None:
        ad_group_resource = _create_ad_group(
            client,
            account,
            campaign_resource_name=str(campaign["campaign_resource_name"]),
            name=ad_group_name,
            ad_group_type="rsa",
            max_cpc_micros=_micros(max_cpc),
            validate_only=validate_only,
        )
        ad_group = {
            "ad_group_id": _resource_id(ad_group_resource),
            "ad_group_name": ad_group_name,
            "ad_group_resource_name": ad_group_resource,
            "ad_group_status": "ENABLED",
            "ad_group_type": "SEARCH_STANDARD",
            "matched_by": "created",
        }
        operations.append({"operation": "create_rsa_ad_group", "resource_name": ad_group_resource})
    renamed_ad_group_resource = _rename_ad_group_if_needed(
        client,
        account,
        ad_group,
        desired_name=ad_group_name,
        validate_only=validate_only,
    )
    if renamed_ad_group_resource:
        operations.append({"operation": "rename_rsa_ad_group", "resource_name": renamed_ad_group_resource})
    enabled_ad_group_resource = _enable_ad_group_if_needed(client, account, ad_group, validate_only=validate_only)
    if enabled_ad_group_resource:
        operations.append({"operation": "enable_existing_ad_group", "resource_name": enabled_ad_group_resource})

    ad_group_id = int(ad_group.get("ad_group_id") or 0)
    rsa_ad = _rsa_ad_by_ad_group(client, account, ad_group_id) if ad_group_id else None
    if rsa_ad is None:
        headlines = [str(item) for item in (assets.get("headlines") or []) if str(item).strip()]
        descriptions = [str(item) for item in (assets.get("descriptions") or []) if str(item).strip()]
        ad_resource = _create_rsa_ad(
            client,
            account,
            ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
            final_url=final_url,
            headlines=headlines,
            descriptions=descriptions,
            validate_only=validate_only,
        )
        rsa_ad = {
            "ad_group_ad_resource_name": ad_resource,
            "ad_status": "ENABLED",
            "ad_type": "RESPONSIVE_SEARCH_AD",
            "matched_by": "created",
        }
        operations.append({"operation": "create_responsive_search_ad", "resource_name": ad_resource})
    enabled_ad_resource = _enable_ad_group_ad_if_needed(client, account, rsa_ad, validate_only=validate_only)
    if enabled_ad_resource:
        operations.append({"operation": "enable_existing_ad_group_ad", "resource_name": enabled_ad_resource})

    if criteria_csv_pending:
        keyword_result = {
            **_manual_csv_deferred_keyword_result(draft),
            "enabled_existing_keyword_count": 0,
            "paused_stale_keyword_count": 0,
            "enabled_keyword_resources": [],
            "paused_keyword_resources": [],
        }
        _append_manual_csv_deferred_operation(
            operations,
            "exact_keywords",
            int(keyword_result.get("keyword_count") or 0),
        )
    else:
        keywords = _keyword_texts_from_draft(draft)
        existing_keyword_map = _existing_exact_keyword_map(client, account, ad_group_id) if ad_group_id else {}
        existing_keywords = set(existing_keyword_map)
        desired_keywords = set(keywords)
        keywords_to_create = [keyword for keyword in keywords if keyword not in existing_keywords]
        keywords_to_enable = [
            info["resource_name"]
            for keyword, info in existing_keyword_map.items()
            if keyword in keywords and str(info.get("status") or "").upper() != "ENABLED"
        ]
        keywords_to_pause = [
            info["resource_name"]
            for keyword, info in existing_keyword_map.items()
            if keyword not in desired_keywords and str(info.get("status") or "").upper() == "ENABLED"
        ]
        planned_keywords_to_create = len(keywords_to_create)
        keywords_to_create, keyword_daily_budget = _limit_daily_criteria_items(
            session,
            account,
            kind="exact_keywords",
            scope_key=f"exact_keywords:{ad_group['ad_group_resource_name']}",
            items=keywords_to_create,
            validate_only=validate_only,
        )
        enabled_keyword_resources = _enable_ad_group_criteria_if_needed(
            client,
            account,
            criterion_resource_names=keywords_to_enable,
            validate_only=validate_only,
        )
        paused_keyword_resources = _pause_ad_group_criteria_if_needed(
            client,
            account,
            criterion_resource_names=keywords_to_pause,
            validate_only=validate_only,
        )
        keyword_resources = _create_exact_keywords(
            client,
            account,
            ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
            keywords=keywords_to_create,
            validate_only=validate_only,
        )
        keyword_result = {
            "keyword_count": len(keywords),
            "existing_keyword_count": len(existing_keywords),
            "enabled_existing_keyword_count": len(enabled_keyword_resources),
            "paused_stale_keyword_count": len(paused_keyword_resources),
            "new_keyword_count": len(keywords_to_create),
            "planned_new_keyword_count": planned_keywords_to_create,
            "daily_deferred_keyword_count": max(planned_keywords_to_create - len(keywords_to_create), 0),
            "keyword_daily_budget": keyword_daily_budget,
            "keyword_resources": keyword_resources,
            "enabled_keyword_resources": enabled_keyword_resources,
            "paused_keyword_resources": paused_keyword_resources,
        }
        if enabled_keyword_resources:
            operations.append(
                {
                    "operation": "enable_existing_exact_keywords",
                    "resource_name": f"{len(enabled_keyword_resources)} keywords",
                }
            )
        if paused_keyword_resources:
            operations.append(
                {
                    "operation": "pause_stale_exact_keywords",
                    "resource_name": f"{len(paused_keyword_resources)} keywords",
                }
            )
        if keyword_resources:
            operations.append({"operation": "create_enabled_exact_keywords", "resource_name": f"{len(keyword_resources)} keywords"})

    if criteria_csv_pending:
        inclusion_result = _manual_csv_deferred_url_inclusion_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "ad_group_url_inclusions",
            int(inclusion_result.get("url_inclusion_count") or 0),
        )
    else:
        try:
            inclusion_result = _apply_ad_group_webpage_inclusions(
                client,
                account,
                draft,
                session=session,
                ad_group_id=ad_group_id,
                ad_group_resource_name=str(ad_group["ad_group_resource_name"]),
                validate_only=validate_only,
            )
        except GoogleAdsException as exc:
            inclusion_result = {
                "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                "existing_url_inclusion_count": 0,
                "new_url_inclusion_count": 0,
                "url_inclusion_resources": [],
                "url_inclusion_error": summarize_google_ads_exception(exc),
            }
        except Exception as exc:  # noqa: BLE001 - URL inclusions should not kill RSA creation.
            inclusion_result = {
                "url_inclusion_count": len(_url_inclusion_urls_from_draft(draft)),
                "existing_url_inclusion_count": 0,
                "new_url_inclusion_count": 0,
                "url_inclusion_resources": [],
                "url_inclusion_error": {"message": str(exc)[:500]},
            }
    if inclusion_result["url_inclusion_resources"]:
        operations.append(
            {
                "operation": "create_ad_group_webpage_inclusions",
                "resource_name": f"{len(inclusion_result['url_inclusion_resources'])} URL inclusions",
            }
        )

    return {
        "draft_id": draft.id,
        "ad_type": "rsa",
        "status": "validated" if validate_only else "published_enabled",
        "campaign_code": campaign_code,
        "campaign_name": campaign_name,
        "budget_blocked": budget_blocked,
        "effective_daily_budget": effective_daily_budget,
        "currency_code": account.currency_code,
        "force_paused": False,
        "campaign": campaign,
        "country_target": location_result,
        "ad_group": ad_group,
        "ad": rsa_ad,
        **keyword_result,
        **inclusion_result,
        **negative_result,
        **page_result,
        "operations": operations,
    }


def publish_pmax_draft(
    session: Session,
    client: Any,
    account: GoogleAdsAccount,
    preference: GoogleAdsAutomationPreference,
    draft: AdDraft,
    *,
    validate_only: bool = False,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = _draft_identity(draft)
    campaign_code = str(identity.get("campaign_code") or "").strip()
    campaign_name = str(identity.get("campaign_name") or assets.get("campaign_name") or "").strip()
    if not campaign_code or not campaign_name:
        return {"draft_id": draft.id, "ad_type": draft.ad_type, "status": "blocked", "reason": "Missing automation campaign identity."}
    final_url = str(assets.get("final_url") or draft.final_url or draft.website_url or "").strip()
    if not final_url:
        return {"draft_id": draft.id, "ad_type": draft.ad_type, "status": "blocked", "reason": "Missing final URL."}

    pmax_gate = pmax_activation_gate(session, account, preference)
    if not bool(pmax_gate.get("allowed")):
        operations: list[dict[str, Any]] = []
        existing_campaign = _campaign_by_code_or_lane(client, account, campaign_code, campaign_name)
        if existing_campaign and str(existing_campaign.get("campaign_status") or "").upper() == "ENABLED":
            paused = _pause_campaign_if_needed(client, account, existing_campaign, validate_only=validate_only)
            if paused:
                operations.append({"operation": "pause_pmax_below_search_conversion_gate", "resource_name": paused})
        return {
            "draft_id": draft.id,
            "ad_type": "pmax",
            "status": "blocked_pmax_conversion_gate",
            "reason": str(pmax_gate.get("reason") or "PMax is held until automation-owned Search campaigns reach the conversion gate."),
            "campaign_code": campaign_code,
            "campaign_name": campaign_name,
            "campaign": existing_campaign,
            "pmax_gate": pmax_gate,
            "operations": operations,
        }

    def checkpoint(name: str, status: str = "started", **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(
                name,
                status,
                draft_id=draft.id,
                ad_type="pmax",
                campaign_name=campaign_name,
                campaign_code=campaign_code,
                **extra,
            )
        except Exception:
            pass

    bidding = assets.get("bidding") if isinstance(assets.get("bidding"), dict) else {}
    budget_block = _budget_block_result(session, draft, ad_type="pmax", bidding=bidding, preference=preference)
    if budget_block is not None:
        return budget_block
    daily_budget = _float_value(bidding.get("daily_budget"), 0.0)
    effective_daily_budget = max(daily_budget, _currency_minimum_daily_budget(account, preference.minimum_daily_budget_amount), 1.0)
    target_roas = _float_value(bidding.get("target_roas"), 3.5)
    criteria_csv_pending = _manual_first_run_criteria_csv_pending(preference, draft)
    category_label = str((assets.get("campaign_identity") or {}).get("category") or "Core / Scale").strip() or "Core / Scale"
    pmax_final_urls = _pmax_final_urls_from_assets(
        assets,
        final_url,
        exclude_broad_pages="core / scale" in category_label.lower(),
    )
    if "core / scale" in category_label.lower() and not pmax_final_urls:
        return {
            "draft_id": draft.id,
            "ad_type": "pmax",
            "status": "blocked_missing_core_product_urls",
            "reason": "Core / Scale PMax requires product/performance final URLs; category, homepage, and navigation URLs are asset-only.",
            "campaign_code": campaign_code,
            "campaign_name": campaign_name,
            "operations": [],
        }

    operations: list[dict[str, Any]] = []
    checkpoint("pmax_campaign_lookup")
    existing_campaign = _campaign_by_code_or_lane(client, account, campaign_code, campaign_name)
    checkpoint("pmax_campaign_lookup", "found" if existing_campaign else "missing")
    campaign = existing_campaign
    if campaign is None:
        checkpoint("pmax_campaign_create")
        budget_resource = _create_campaign_budget(
            client,
            account,
            name=f"{campaign_name[:220]} budget",
            amount_micros=_micros(effective_daily_budget),
            validate_only=validate_only,
        )
        campaign_resource = _create_performance_max_campaign(
            client,
            account,
            name=campaign_name,
            budget_resource_name=budget_resource,
            target_roas=target_roas,
            validate_only=validate_only,
        )
        campaign = {
            "campaign_id": _resource_id(campaign_resource),
            "campaign_name": campaign_name,
            "campaign_resource_name": campaign_resource,
            "campaign_status": "ENABLED",
            "channel_type": "PERFORMANCE_MAX",
            "matched_by": "created",
            "campaign_code": campaign_code,
            "budget_resource_name": budget_resource,
        }
        operations.append({"operation": "create_campaign_budget", "resource_name": budget_resource})
        operations.append({"operation": "create_performance_max_campaign", "resource_name": campaign_resource})
        checkpoint("pmax_campaign_create", "done", campaign_resource_name=campaign_resource)

    campaign_id = int(campaign.get("campaign_id") or 0)
    campaign_resource = str(campaign.get("campaign_resource_name") or "")
    if not campaign_id or not campaign_resource:
        return {
            "draft_id": draft.id,
            "ad_type": "pmax",
            "status": "blocked",
            "reason": "Could not resolve PMax campaign id/resource after create or lookup.",
            "campaign": campaign,
        }
    enabled_campaign_resource = _enable_campaign_if_needed(client, account, campaign, validate_only=validate_only)
    if enabled_campaign_resource:
        operations.append({"operation": "enable_existing_campaign", "resource_name": enabled_campaign_resource})
    country_target = resolve_account_country_target(session, account, final_url=final_url)
    location_result = _ensure_campaign_country_target(
        client,
        account,
        campaign_id=campaign_id,
        campaign_resource_name=campaign_resource,
        country_target=country_target,
        validate_only=validate_only,
    )
    if location_result.get("status") in {"updated", "validated"}:
        operations.append(
            {
                "operation": "set_campaign_country_target",
                "resource_name": str(location_result.get("geo_target_constant") or ""),
            }
        )
    if criteria_csv_pending:
        negative_result = _manual_csv_deferred_negative_keyword_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_negative_keywords",
            int(negative_result.get("negative_keyword_count") or 0),
        )
    else:
        negative_result = _apply_campaign_negative_keywords(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    if criteria_csv_pending:
        page_result = _manual_csv_deferred_negative_page_result(draft)
        _append_manual_csv_deferred_operation(
            operations,
            "campaign_url_exclusions",
            int(page_result.get("negative_page_count") or 0),
        )
    else:
        page_result = _apply_campaign_negative_webpages(
            client,
            account,
            draft,
            session=session,
            campaign_id=campaign_id,
            campaign_resource_name=campaign_resource,
            validate_only=validate_only,
        )
    if negative_result["negative_keyword_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_exact_keywords",
                "resource_name": f"{len(negative_result['negative_keyword_resources'])} negatives",
            }
        )
    if page_result["negative_page_resources"]:
        operations.append(
            {
                "operation": "create_campaign_negative_webpages",
                "resource_name": f"{len(page_result['negative_page_resources'])} page exclusions",
            }
        )

    checkpoint("pmax_media_assets")
    media_assets = _existing_pmax_media_assets(client, account)
    media_missing = [
        field_type
        for field_type, count in PMAX_MEDIA_FIELD_TYPES.items()
        if len(media_assets.get(field_type) or []) < count
    ]
    if media_missing:
        checkpoint("pmax_media_assets", "blocked_missing_media_assets", missing_field_types=media_missing)
        return {
            "draft_id": draft.id,
            "ad_type": "pmax",
            "status": "blocked_missing_media_assets",
            "reason": "PMax live creation needs reusable image/logo/video assets already present in the account.",
            "missing_field_types": media_missing,
            "media_asset_counts": {key: len(value) for key, value in media_assets.items()},
            "campaign": campaign,
        }
    checkpoint("pmax_media_assets", "done", counts={key: len(value) for key, value in media_assets.items()})

    asset_group_results: list[dict[str, Any]] = []
    media_links = _media_asset_links_for_pmax(media_assets)
    all_text_asset_resources: set[str] = set()
    legacy_defer_signal_mutations = parse_bool(_sync_setting_value(session, DEFER_PMAX_SIGNAL_MUTATIONS_SETTING, False))
    defer_search_theme_mutations = criteria_csv_pending or parse_bool(
        _sync_setting_value(session, DEFER_PMAX_SEARCH_THEME_MUTATIONS_SETTING, False)
    )
    defer_audience_signal_mutations = parse_bool(
        _sync_setting_value(session, DEFER_PMAX_AUDIENCE_SIGNAL_MUTATIONS_SETTING, legacy_defer_signal_mutations)
    )
    search_theme_signals_blocked_reason: Optional[str] = None
    audience_signals_blocked_reason: Optional[str] = None
    campaign_search_theme_keys = set() if defer_search_theme_mutations else _existing_campaign_search_theme_keys(client, account, campaign_id)
    search_theme_backfill_terms = _pmax_search_theme_backfill_terms(assets)
    existing_asset_groups = _asset_groups_by_name(client, account, campaign_id)
    for group in _pmax_asset_group_plan(assets):
        group_code = str(group.get("asset_group_code") or f"AG{len(asset_group_results) + 1:03d}")
        group_name = f"AUTO | {category_label} | PMax Asset Group | {campaign_code} | {group_code}"
        checkpoint("pmax_asset_group", "started", group_code=group_code, group_name=group_name)
        group_copy_assets = _pmax_asset_group_copy_assets(assets, account, group)
        group_text_values = (
            [str(item) for item in group_copy_assets["headlines"]]
            + [str(item) for item in group_copy_assets["long_headlines"]]
            + [str(item) for item in group_copy_assets["descriptions"]]
            + [str(group_copy_assets["business_name"])]
        )
        checkpoint("pmax_text_assets", "started", group_code=group_code, text_count=len(group_text_values))
        text_assets = _create_text_assets(client, account, texts=group_text_values, validate_only=validate_only)
        checkpoint("pmax_text_assets", "done", group_code=group_code, text_asset_count=len(text_assets))
        all_text_asset_resources.update(resource for resource in text_assets.values() if resource)
        group_links = _text_asset_links_for_pmax(client, text_assets, group_copy_assets) + media_links
        if text_assets:
            operations.append(
                {
                    "operation": "create_or_reuse_pmax_group_text_assets",
                    "resource_name": f"{len(text_assets)} text assets for {group_code}",
                }
            )
        asset_group = existing_asset_groups.get(group_name)
        if asset_group is None and not existing_asset_groups:
            asset_group = _asset_group_by_name(client, account, campaign_id, group_name)
        linked_assets: list[str] = []
        if asset_group is None:
            checkpoint("pmax_asset_group_create", "started", group_code=group_code)
            created_group = _create_asset_group_with_assets(
                client,
                account,
                campaign_resource_name=campaign_resource,
                name=group_name,
                final_url=final_url,
                final_urls=pmax_final_urls,
                links=group_links,
                validate_only=validate_only,
            )
            asset_group_resource = str(created_group.get("asset_group_resource_name") or "")
            linked_assets = [str(item) for item in (created_group.get("asset_link_resources") or []) if str(item)]
            asset_group = {
                "asset_group_id": _resource_id(asset_group_resource),
                "asset_group_name": group_name,
                "asset_group_resource_name": asset_group_resource,
                "asset_group_status": "ENABLED",
                "matched_by": "created",
            }
            existing_asset_groups[group_name] = asset_group
            operations.append({"operation": "create_pmax_asset_group_with_assets", "resource_name": asset_group_resource})
            checkpoint("pmax_asset_group_create", "done", group_code=group_code, asset_group_resource_name=asset_group_resource)
        group_resource = str(asset_group.get("asset_group_resource_name") or "")
        if not linked_assets:
            checkpoint("pmax_asset_group_links", "started", group_code=group_code)
            linked_assets = _link_asset_group_assets(
                client,
                account,
                asset_group_resource_name=group_resource,
                links=group_links,
                validate_only=validate_only,
            )
            checkpoint("pmax_asset_group_links", "done", group_code=group_code, linked_count=len(linked_assets))
        search_themes = _dedupe_texts(
            [_clean_search_theme(str(item or "").strip()) for item in (group.get("search_themes") or []) if str(item or "").strip()],
            limit=80,
            max_items=PMAX_SEARCH_THEME_SIGNAL_LIMIT,
        )
        if defer_search_theme_mutations:
            search_themes_to_create = search_themes[:PMAX_SEARCH_THEME_SIGNAL_LIMIT]
            existing_search_theme_count = 0
            missing_search_theme_count = len(search_themes_to_create)
        else:
            search_themes_to_create, existing_search_theme_count, missing_search_theme_count = _search_themes_needed_for_asset_group(
                client,
                account,
                asset_group_resource_name=group_resource,
                planned_terms=search_themes,
                campaign_theme_keys=campaign_search_theme_keys,
                backfill_terms=search_theme_backfill_terms,
            )
        search_theme_error: dict[str, Any] | None = None
        if defer_search_theme_mutations:
            signal_resources = []
            if criteria_csv_pending:
                _append_manual_csv_deferred_operation(operations, "pmax_search_themes", len(search_themes_to_create))
                search_theme_error = {
                    "deferred": "First-run bulk criteria CSV mode is enabled; PMax search themes are in the Keywords CSV.",
                    "manual_csv_export_kind": "keywords",
                }
            else:
                search_theme_error = {"deferred": "PMax search-theme signal mutations are deferred by setting."}
        elif search_theme_signals_blocked_reason:
            signal_resources = []
            search_theme_error = {"skipped_after_previous_timeout": search_theme_signals_blocked_reason}
        else:
            try:
                planned_search_theme_signal_count = len(search_themes_to_create)
                search_themes_to_create, search_theme_daily_budget = _limit_daily_criteria_items(
                    session,
                    account,
                    kind="pmax_search_themes",
                    scope_key=f"pmax_search_themes:{group_resource}",
                    items=search_themes_to_create,
                    validate_only=validate_only,
                )
                checkpoint("pmax_search_themes", "started", group_code=group_code, planned=len(search_themes_to_create))
                signal_resources = _create_search_theme_signals(
                    client,
                    account,
                    asset_group_resource_name=group_resource,
                    search_themes=search_themes_to_create,
                    campaign_theme_keys=campaign_search_theme_keys,
                    validate_only=validate_only,
                    max_successes=min(missing_search_theme_count, len(search_themes_to_create)),
                )
                checkpoint("pmax_search_themes", "done", group_code=group_code, created=len(signal_resources))
                if len(search_themes_to_create) < planned_search_theme_signal_count:
                    search_theme_error = {
                        "daily_paced": True,
                        "planned_new_search_theme_count": planned_search_theme_signal_count,
                        "daily_deferred_search_theme_count": planned_search_theme_signal_count - len(search_themes_to_create),
                        "search_theme_daily_budget": search_theme_daily_budget,
                    }
            except GoogleAdsException as exc:
                signal_resources = []
                search_theme_error = {"google_ads_error": summarize_google_ads_exception(exc)}
            except Exception as exc:  # noqa: BLE001 - keep remaining asset groups resumable.
                signal_resources = []
                message = str(exc)[:500]
                search_theme_error = {"error": message}
                if isinstance(exc, TimeoutError) or "timed out" in message.lower():
                    search_theme_signals_blocked_reason = message
        audience_plan = (
            group.get("audience_signal_plan")
            if isinstance(group.get("audience_signal_plan"), dict)
            else assets.get("audience_signal_plan")
            if isinstance(assets.get("audience_signal_plan"), dict)
            else {}
        )
        if defer_audience_signal_mutations:
            audience_result = {
                "status": "deferred",
                "reason": "PMax audience signal mutations are deferred by setting.",
            }
        elif audience_signals_blocked_reason:
            audience_result = {
                "status": "skipped_after_previous_timeout",
                "reason": audience_signals_blocked_reason,
            }
        else:
            try:
                checkpoint("pmax_audience_signal", "started", group_code=group_code)
                audience_result = _ensure_pmax_audience_signal_audience(
                    client,
                    account,
                    campaign_code=campaign_code,
                    group_code=group_code,
                    asset_group_resource_name=group_resource,
                    plan=audience_plan,
                    group=group,
                    validate_only=validate_only,
                )
                checkpoint("pmax_audience_signal", audience_result.get("status") or "done", group_code=group_code)
            except GoogleAdsException as exc:
                audience_result = {
                    "status": "failed",
                    "google_ads_error": summarize_google_ads_exception(exc),
                }
            except Exception as exc:  # noqa: BLE001 - keep PMax asset publishing moving.
                message = str(exc)[:500]
                audience_result = {
                    "status": "failed",
                    "error": message,
                }
                if isinstance(exc, TimeoutError) or "timed out" in message.lower():
                    audience_signals_blocked_reason = message
        audience_signal_resources: list[str] = []
        audience_resource = str(audience_result.get("audience_resource_name") or "")
        if audience_resource:
            try:
                checkpoint("pmax_audience_signal_link", "started", group_code=group_code)
                audience_signal_resources = _create_audience_signal(
                    client,
                    account,
                    asset_group_resource_name=group_resource,
                    audience_resource_name=audience_resource,
                    validate_only=validate_only,
                )
                checkpoint("pmax_audience_signal_link", "done", group_code=group_code, created=len(audience_signal_resources))
            except GoogleAdsException as exc:
                audience_result = {
                    **audience_result,
                    "status": "signal_failed",
                    "google_ads_error": summarize_google_ads_exception(exc),
                }
            except Exception as exc:  # noqa: BLE001 - keep asset group publishing resumable.
                message = str(exc)[:500]
                audience_result = {
                    **audience_result,
                    "status": "signal_failed",
                    "error": message,
                }
                if isinstance(exc, TimeoutError) or "timed out" in message.lower():
                    audience_signals_blocked_reason = message
        if linked_assets:
            operations.append({"operation": "link_pmax_asset_group_assets", "resource_name": f"{len(linked_assets)} links"})
        if signal_resources:
            operations.append({"operation": "create_pmax_search_theme_signals", "resource_name": f"{len(signal_resources)} search themes"})
        if audience_signal_resources:
            operations.append({"operation": "create_pmax_audience_signal", "resource_name": f"{len(audience_signal_resources)} audience signal"})
        asset_group_results.append(
            {
                **asset_group,
                "asset_link_count": len(linked_assets),
                "text_asset_count": len(text_assets),
                "headline_count": len(group_copy_assets["headlines"]),
                "long_headline_count": len(group_copy_assets["long_headlines"]),
                "description_count": len(group_copy_assets["descriptions"]),
                "shipping_offer_text_filtered_count": int(group_copy_assets["shipping_offer_text_filtered"]),
                "search_theme_count": min(PMAX_SEARCH_THEME_SIGNAL_LIMIT, existing_search_theme_count + len(signal_resources)),
                "existing_search_theme_count": existing_search_theme_count,
                "planned_search_theme_count": len(search_themes),
                "new_search_theme_count": len(signal_resources),
                "search_theme_signal_error": search_theme_error,
                "audience_signal": audience_result,
                "new_audience_signal_count": len(audience_signal_resources),
                "final_url_count": len(pmax_final_urls),
                "final_urls": pmax_final_urls,
            }
        )
        checkpoint("pmax_asset_group", "done", group_code=group_code)

    return {
        "draft_id": draft.id,
        "ad_type": "pmax",
        "status": "validated" if validate_only else "published_enabled",
        "campaign_code": campaign_code,
        "campaign_name": campaign_name,
        "effective_daily_budget": effective_daily_budget,
        "target_roas": target_roas,
        "currency_code": account.currency_code,
        "force_paused": False,
        "campaign": campaign,
        "country_target": location_result,
        "asset_group_count": len(asset_group_results),
        "asset_groups": asset_group_results,
        "text_asset_count": len(all_text_asset_resources),
        "media_asset_counts": {key: len(value) for key, value in media_assets.items()},
        **negative_result,
        **page_result,
        "operations": operations,
    }


def _store_live_result(draft: AdDraft, result: dict[str, Any]) -> None:
    live_entry = {
        **_compact_live_result(result),
        "recorded_at": utcnow().isoformat(),
    }
    validation = dict(getattr(draft, "validation_json", None) or {})
    validation_history = list(validation.get("live_creation_history") or [])
    validation_history.append(live_entry)
    validation["live_creation_result"] = live_entry
    validation["live_creation_history"] = validation_history[-10:]
    if hasattr(draft, "validation_json"):
        draft.validation_json = validation

    assets = dict(getattr(draft, "generated_assets", None) or {})
    try:
        asset_json_size = len(json.dumps(assets, default=str))
    except Exception:  # noqa: BLE001 - best effort guard; small/invalid payloads can still be rewritten.
        asset_json_size = 0
    if asset_json_size > MAX_DRAFT_ASSET_JSON_REWRITE_BYTES:
        if result.get("status") in LIVE_CREATION_SUCCESS_STATUSES:
            draft.status = "published_enabled" if result.get("status") == "published_enabled" else draft.status
        return
    automation = dict(assets.get("automation") or {})
    live_history = list(automation.get("live_creation_history") or [])
    live_history.append(live_entry)
    automation["live_creation_result"] = live_entry
    automation["live_creation_history"] = live_history[-10:]
    assets["automation"] = automation
    assets["live_creation_result"] = live_entry
    if result.get("campaign"):
        assets["existing_google_campaign"] = result.get("campaign")
    draft.generated_assets = assets
    if result.get("status") in LIVE_CREATION_SUCCESS_STATUSES:
        draft.status = "published_enabled" if result.get("status") == "published_enabled" else draft.status


def _live_publish_sort_key(draft: AdDraft) -> tuple[int, int, int]:
    status_priority = 1 if _draft_status_value(draft) in LIVE_CREATION_SUCCESS_STATUSES else 0
    return (
        status_priority,
        LIVE_PUBLISH_AD_TYPE_PRIORITY.get(str(draft.ad_type or "").lower(), 99),
        -int(getattr(draft, "id", 0) or 0),
    )


def _planned_criteria_scope_count(drafts: list[AdDraft]) -> int:
    total = 0
    for draft in drafts:
        assets = dict(getattr(draft, "generated_assets", None) or {})
        ad_type = str(getattr(draft, "ad_type", "") or "").lower()
        if ad_type in {"dsa", "rsa", "pmax"} and _negative_keyword_texts_from_draft(draft):
            total += 1
        if ad_type in {"dsa", "rsa", "pmax"} and _negative_page_urls_from_draft(draft):
            total += 1
        if ad_type in {"dsa", "rsa"} and _url_inclusion_urls_from_draft(draft):
            total += 1
        if ad_type == "rsa" and _keyword_texts_from_draft(draft):
            total += 1
        if ad_type == "pmax":
            for group in _pmax_asset_group_plan(assets):
                if group.get("search_themes"):
                    total += 1
    return max(total, 1)


def _compact_resource(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    keep = {
        "resource_name",
        "campaign_resource_name",
        "campaign_id",
        "campaign_name",
        "campaign_code",
        "campaign_status",
        "channel_type",
        "ad_group_resource_name",
        "ad_group_id",
        "ad_group_name",
        "ad_group_status",
        "ad_group_type",
        "ad_group_ad_resource_name",
        "ad_resource_name",
        "ad_id",
        "ad_status",
        "ad_type",
        "asset_group_resource_name",
        "asset_group_id",
        "asset_group_name",
        "asset_group_status",
        "matched_by",
    }
    return {key: value.get(key) for key in keep if key in value}


def _operation_summary(operations: Any) -> dict[str, Any]:
    if not isinstance(operations, list):
        return {"total": 0, "by_operation": {}}
    by_operation: dict[str, int] = {}
    for item in operations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("operation") or "unknown")
        by_operation[name] = by_operation.get(name, 0) + 1
    return {
        "total": len(operations),
        "by_operation": by_operation,
        "sample": [
            _compact_resource(item)
            for item in operations[:25]
            if isinstance(item, dict)
        ],
    }


def _compact_asset_group_result(group: Any) -> Any:
    if not isinstance(group, dict):
        return group
    keep = {
        "asset_group_index",
        "asset_group_code",
        "asset_group_id",
        "asset_group_name",
        "asset_group_resource_name",
        "asset_group_status",
        "asset_link_count",
        "text_asset_count",
        "headline_count",
        "long_headline_count",
        "description_count",
        "shipping_offer_text_filtered_count",
        "search_theme_count",
        "existing_search_theme_count",
        "planned_search_theme_count",
        "new_search_theme_count",
        "new_audience_signal_count",
    }
    compact = {key: group.get(key) for key in keep if key in group}
    if isinstance(group.get("search_theme_signal_error"), str) and group.get("search_theme_signal_error"):
        compact["search_theme_signal_error"] = str(group.get("search_theme_signal_error"))[:500]
    audience_signal = group.get("audience_signal")
    if isinstance(audience_signal, dict):
        compact["audience_signal"] = {
            "status": audience_signal.get("status"),
            "audience_resource_name": audience_signal.get("audience_resource_name"),
            "error": str(audience_signal.get("error") or "")[:500] if audience_signal.get("error") else "",
        }
    return compact


def _compact_live_result(result: dict[str, Any]) -> dict[str, Any]:
    scalar_keys = {
        "draft_id",
        "ad_type",
        "status",
        "reason",
        "legacy_source_key",
        "replacement_source_suffix",
        "campaign_code",
        "campaign_name",
        "effective_daily_budget",
        "target_roas",
        "currency_code",
        "force_paused",
        "keyword_count",
        "new_keyword_count",
        "existing_keyword_count",
        "asset_group_count",
        "text_asset_count",
        "error_count",
        "result_count",
        "validate_only",
        "manual_csv_deferred",
        "manual_csv_deferred_entities",
        "manual_csv_export_kind",
        "manual_csv_reason",
    }
    compact = {key: result.get(key) for key in scalar_keys if key in result}
    for key in ("campaign", "ad_group", "ad"):
        if key in result:
            compact[key] = _compact_resource(result.get(key))
    if isinstance(result.get("asset_groups"), list):
        compact["asset_groups"] = [_compact_asset_group_result(item) for item in result.get("asset_groups", [])]
    if "operations" in result:
        compact["operations"] = _operation_summary(result.get("operations"))
    if isinstance(result.get("media_asset_counts"), dict):
        compact["media_asset_counts"] = result.get("media_asset_counts")
    if isinstance(result.get("live_creation"), dict):
        compact["live_creation"] = result.get("live_creation")
    if isinstance(result.get("pmax_gate"), dict):
        compact["pmax_gate"] = result.get("pmax_gate")
    if isinstance(result.get("google_ads_error"), dict):
        compact["google_ads_error"] = result.get("google_ads_error")
    if result.get("error"):
        compact["error"] = str(result.get("error"))[:1000]
    return compact


def _draft_source_key(draft: AdDraft) -> str:
    return str((_draft_automation(draft).get("source_key") or "")).strip()


def _draft_status_value(draft: AdDraft) -> str:
    status = getattr(draft, "status", "")
    return str(getattr(status, "value", status) or "")


def _is_replaced_legacy_pmax_draft(draft: AdDraft, drafts: list[AdDraft]) -> bool:
    if draft.ad_type != "pmax":
        return False
    source_key = _draft_source_key(draft)
    if not source_key.endswith(LEGACY_PMAX_SCALE_SOURCE_SUFFIX):
        return False
    replacement_key = source_key[: -len(LEGACY_PMAX_SCALE_SOURCE_SUFFIX)] + REPLACEMENT_PMAX_SCALE_SOURCE_SUFFIX
    return any(
        other.id != draft.id
        and other.ad_type == "pmax"
        and _draft_status_value(other) in LIVE_PUBLISH_STATUSES
        and _draft_source_key(other) == replacement_key
        for other in drafts
    )


def _skip_replaced_legacy_pmax_draft(
    client: Any,
    account: GoogleAdsAccount,
    draft: AdDraft,
    *,
    validate_only: bool,
) -> dict[str, Any]:
    identity = _draft_identity(draft)
    campaign_code = str(identity.get("campaign_code") or "").strip()
    campaign = _campaign_by_code(client, account, campaign_code) if campaign_code else None
    operations: list[dict[str, Any]] = []
    if campaign and str(campaign.get("campaign_status") or "").upper() == "ENABLED":
        paused = _pause_campaign_if_needed(client, account, campaign, validate_only=validate_only)
        if paused:
            operations.append({"operation": "pause_replaced_legacy_campaign", "resource_name": paused})
    return {
        "draft_id": draft.id,
        "ad_type": draft.ad_type,
        "status": "skipped_replaced_legacy_draft",
        "reason": "Replaced legacy unsuffixed Core / Scale PMax source key; S001 campaign is now the stable automation target.",
        "legacy_source_key": _draft_source_key(draft),
        "replacement_source_suffix": REPLACEMENT_PMAX_SCALE_SOURCE_SUFFIX,
        "campaign_code": campaign_code,
        "campaign": campaign,
        "operations": operations,
    }


def _draft_live_creation_control(draft: AdDraft) -> dict[str, Any]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    control = assets.get("live_creation")
    if isinstance(control, dict):
        return control
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    control = automation.get("live_creation")
    if isinstance(control, dict):
        return control
    return {"enabled": True, "phase": "legacy", "reason": "Legacy draft without explicit live gate."}


def _skip_live_creation_gate(draft: AdDraft) -> dict[str, Any]:
    control = _draft_live_creation_control(draft)
    identity = _draft_identity(draft)
    return {
        "draft_id": draft.id,
        "ad_type": draft.ad_type,
        "status": "skipped_live_gate",
        "reason": str(control.get("reason") or "Draft is intentionally held from live creation by automation policy."),
        "live_creation": control,
        "campaign_code": str(identity.get("campaign_code") or ""),
        "campaign_name": str(identity.get("campaign_name") or ""),
        "operations": [],
    }


def _live_creation_batch_status(results: list[dict[str, Any]], errors: list[dict[str, Any]]) -> str:
    if errors:
        has_success = any(str(result.get("status") or "") in LIVE_CREATION_SUCCESS_STATUSES for result in results)
        return "partial" if has_success else "failed"
    if any(str(result.get("status") or "") in LIVE_CREATION_SUCCESS_STATUSES for result in results):
        return "done"
    if any(str(result.get("status") or "").startswith("blocked") for result in results):
        return "blocked"
    return "skipped" if results else "skipped"


def publish_automation_campaigns(
    session: Session,
    preference: GoogleAdsAutomationPreference,
    *,
    validate_only: bool = False,
    limit: int = LIVE_CAMPAIGN_PUBLISH_BATCH_LIMIT,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    account = preference.account
    if account is None:
        return {"name": "live_campaign_creation", "status": "skipped", "reason": "Missing Google Ads account."}
    if preference.monitor_only and not validate_only:
        return {"name": "live_campaign_creation", "status": "blocked", "reason": "Monitor-only is enabled."}
    allow_mutations = parse_bool(_sync_setting_value(session, "optimizer.allow_mutations", False))
    dry_run = parse_bool(_sync_setting_value(session, "optimizer.dry_run", True))
    if not validate_only and (not allow_mutations or dry_run):
        return {
            "name": "live_campaign_creation",
            "status": "blocked_by_mutation_guard",
            "reason": "Global mutation guards block live campaign creation until Allow mutations is on and Dry run is off.",
            "guard": {
                "optimizer_allow_mutations": allow_mutations,
                "optimizer_dry_run": dry_run,
                "monitor_only": bool(preference.monitor_only),
                "validate_only": bool(validate_only),
            },
        }
    quota_cooldown = None if validate_only else live_creation_quota_cooldown(session, account)
    if quota_cooldown is not None:
        return {
            "name": "live_campaign_creation",
            **quota_cooldown,
        }

    batch_limit = max(int(limit or 10), 1)
    supported_ad_types = set(SUPPORTED_AD_TYPES)
    pmax_gate = pmax_activation_gate(session, account, preference)
    if not bool(pmax_gate.get("allowed")):
        supported_ad_types.discard("pmax")
    drafts = list(
        session.scalars(
            select(AdDraft)
            .options(
                load_only(
                    AdDraft.id,
                    AdDraft.account_id,
                    AdDraft.ad_type,
                    AdDraft.status,
                    AdDraft.website_url,
                    AdDraft.final_url,
                    AdDraft.business_name,
                    AdDraft.generated_assets,
                    AdDraft.created_at,
                )
            )
            .where(AdDraft.account_id == preference.account_id, AdDraft.ad_type.in_(supported_ad_types))
            .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
            .limit(min(max(batch_limit * 2, batch_limit), LIVE_CAMPAIGN_PUBLISH_BATCH_LIMIT))
        ).all()
    )
    drafts = sorted(
        [
        draft
        for draft in drafts
        if _draft_status_value(draft) in LIVE_PUBLISH_STATUSES
        and str((_draft_automation(draft).get("source_key") or "")).startswith("automation:")
        ],
        key=_live_publish_sort_key,
    )[:batch_limit]
    if not drafts:
        return {"name": "live_campaign_creation", "status": "skipped", "reason": "No eligible automation DSA/RSA drafts."}
    red_flag = account_api_red_flag(session, account)
    if red_flag is not None:
        return {"name": "live_campaign_creation", "status": "blocked_by_account_red_flag", "reason": red_flag["reason"], "red_flag": red_flag}

    session_info = _session_info(session)
    previous_scope_count = session_info.get("live_criteria_scope_count")
    criteria_scope_count = _planned_criteria_scope_count(drafts)
    session_info["live_criteria_scope_count"] = criteria_scope_count
    client = build_client({}, account.manager_customer_id, account.connection)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    def checkpoint(name: str, status: str = "started", **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(name, status, **extra)
        except Exception:
            pass

    checkpoint("live_campaign_batch", "started", draft_count=len(drafts), validate_only=validate_only)
    for draft in drafts:
        draft_id = draft.id
        draft_type = draft.ad_type
        try:
            checkpoint(
                "live_campaign_draft",
                "started",
                draft_id=draft_id,
                ad_type=draft_type,
                campaign_name=str((_draft_identity(draft).get("campaign_name") or ""))[:255],
            )
            if _is_replaced_legacy_pmax_draft(draft, drafts):
                result = _skip_replaced_legacy_pmax_draft(
                    client,
                    account,
                    draft,
                    validate_only=validate_only,
                )
            elif not validate_only and not bool(_draft_live_creation_control(draft).get("enabled", True)):
                result = _skip_live_creation_gate(draft)
            elif draft.ad_type == "dsa":
                result = publish_dsa_draft(
                    session,
                    client,
                    account,
                    preference,
                    draft,
                    validate_only=validate_only,
                    progress_callback=checkpoint,
                )
            elif draft.ad_type == "rsa":
                result = publish_rsa_draft(session, client, account, preference, draft, validate_only=validate_only)
            elif draft.ad_type == "pmax":
                result = publish_pmax_draft(
                    session,
                    client,
                    account,
                    preference,
                    draft,
                    validate_only=validate_only,
                    progress_callback=checkpoint,
                )
            else:
                result = {
                    "draft_id": draft.id,
                    "ad_type": draft.ad_type,
                    "status": "unsupported",
                    "reason": "Only DSA, RSA, and PMax automation drafts are currently live-created.",
                }
            _store_live_result(draft, result)
            session.flush()
            results.append(result)
            checkpoint(
                "live_campaign_draft",
                result.get("status") or "done",
                draft_id=draft_id,
                ad_type=draft_type,
                campaign_name=str((_draft_identity(draft).get("campaign_name") or ""))[:255],
            )
        except GoogleAdsException as exc:
            session.rollback()
            summary = summarize_google_ads_exception(exc)
            if draft_type == "dsa" and "ENUM_VALUE_NOT_PERMITTED" in str(summary).upper():
                disable_dsa_live_creation(session, account.id, str(summary.get("message") or "DSA enum rejected."))
            error = {
                "draft_id": draft_id,
                "ad_type": draft_type,
                "status": "failed",
                "google_ads_error": summary,
            }
            if _is_quota_exhausted_error(summary):
                error["quota_cooldown"] = defer_live_creation_for_quota(session, account, summary)
            try:
                refreshed = session.get(AdDraft, draft_id)
                if refreshed is not None:
                    _store_live_result(refreshed, error)
                    session.flush()
            except Exception as store_exc:  # noqa: BLE001 - a local store failure should not poison the batch.
                session.rollback()
                error["local_store_error"] = str(store_exc)[:500]
            errors.append(error)
            checkpoint("live_campaign_draft", "failed", draft_id=draft_id, ad_type=draft_type)
            if _is_quota_exhausted_error(summary):
                break
        except Exception as exc:  # noqa: BLE001 - keep other drafts resumable.
            session.rollback()
            error = {
                "draft_id": draft_id,
                "ad_type": draft_type,
                "status": "failed",
                "error": str(exc)[:500],
            }
            if _is_quota_exhausted_error(exc):
                error["quota_cooldown"] = defer_live_creation_for_quota(session, account, exc)
            try:
                refreshed = session.get(AdDraft, draft_id)
                if refreshed is not None:
                    _store_live_result(refreshed, error)
                    session.flush()
            except Exception as store_exc:  # noqa: BLE001 - a local store failure should not poison the batch.
                session.rollback()
                error["local_store_error"] = str(store_exc)[:500]
            errors.append(error)
            checkpoint("live_campaign_draft", "failed", draft_id=draft_id, ad_type=draft_type)
            if _is_quota_exhausted_error(exc):
                break

    restricted_policy_result: Optional[dict[str, Any]] = None
    quota_cooldown = None if validate_only else live_creation_quota_cooldown(session, account)
    if quota_cooldown is not None:
        restricted_policy_result = {"name": "restricted_policy_terms", **quota_cooldown}
    else:
        try:
            checkpoint("restricted_policy_terms", "started")
            restricted_policy_result = sync_restricted_policy_terms(
                session,
                client,
                account,
                validate_only=validate_only,
            )
            checkpoint(
                "restricted_policy_terms",
                restricted_policy_result.get("status") if isinstance(restricted_policy_result, dict) else "done",
            )
        except Exception as exc:  # noqa: BLE001 - policy scanner must not block live campaign creation.
            if _is_quota_exhausted_error(exc) and not validate_only:
                restricted_policy_result = {"name": "restricted_policy_terms", **defer_live_creation_for_quota(session, account, exc)}
            else:
                restricted_policy_result = {
                    "name": "restricted_policy_terms",
                    "status": "failed",
                    "error": str(exc)[:500],
                }

    status = _live_creation_batch_status(results, errors)
    summary = (
        "Automation live campaign creation validated."
        if validate_only and status == "done"
        else "Automation live campaign creation created or resumed paused coded campaigns."
        if status == "done"
        else "Automation live campaign creation was blocked by account or global guards."
        if status == "blocked"
        else "Automation live campaign creation found no eligible draft to create."
        if status == "skipped"
        else "Automation live campaign creation partially completed."
        if status == "partial"
        else "Automation live campaign creation failed."
    )
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=None,
            campaign_name=None,
            action_type="live_campaign_creation",
            status=status,
            summary=summary,
            evidence={
                "validate_only": validate_only,
                "results": [_compact_live_result(item) for item in results],
                "errors": [_compact_live_result(item) for item in errors],
                "restricted_policy_terms": restricted_policy_result,
            },
            result_json={"result_count": len(results), "error_count": len(errors)},
        )
    )
    session.flush()
    if previous_scope_count is None:
        session_info.pop("live_criteria_scope_count", None)
    else:
        session_info["live_criteria_scope_count"] = previous_scope_count
    return {
        "name": "live_campaign_creation",
        "status": status,
        "validate_only": validate_only,
        "result_count": len(results),
        "error_count": len(errors),
        "daily_criteria_scope_count": criteria_scope_count,
        "results": [_compact_live_result(item) for item in results],
        "errors": [_compact_live_result(item) for item in errors],
        "restricted_policy_terms": restricted_policy_result,
    }
