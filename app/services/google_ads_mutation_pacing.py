from __future__ import annotations

import os
import random
import threading
import time
from typing import Any, Callable

from google.ads.googleads.errors import GoogleAdsException


_LOCK = threading.Lock()
_NEXT_GLOBAL_AT = 0.0
_NEXT_CUSTOMER_AT: dict[str, float] = {}


def _float_env(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        value = default
    return max(low, min(value, high))


def _mutation_customer_id(request: Any) -> str:
    return str(getattr(request, "customer_id", "") or "").strip()


def _mutation_operation_count(request: Any) -> int:
    for attr in (
        "operations",
        "mutate_operations",
        "campaign_operations",
        "campaign_budget_operations",
        "ad_group_operations",
        "ad_group_ad_operations",
        "ad_group_criterion_operations",
        "campaign_criterion_operations",
        "shared_set_operations",
        "shared_criterion_operations",
        "asset_operations",
        "asset_set_operations",
        "asset_set_asset_operations",
        "campaign_asset_set_operations",
        "campaign_shared_set_operations",
        "asset_group_operations",
    ):
        value = getattr(request, attr, None)
        if value is None:
            continue
        try:
            return len(value)
        except TypeError:
            continue
    return 1


def _pace_mutation_request(request: Any, method_name: str) -> dict[str, Any]:
    if os.getenv("GOOGLE_ADS_MUTATION_PACING_DISABLED", "").lower() in {"1", "true", "yes"}:
        return {"enabled": False, "slept_seconds": 0.0}

    customer_id = _mutation_customer_id(request)
    operation_count = _mutation_operation_count(request)
    global_delay = _float_env("GOOGLE_ADS_MUTATION_GLOBAL_MIN_SECONDS", 4.0, 0.0, 120.0)
    customer_delay = _float_env("GOOGLE_ADS_MUTATION_CUSTOMER_MIN_SECONDS", 25.0, 0.0, 900.0)
    large_batch_delay = _float_env("GOOGLE_ADS_MUTATION_LARGE_BATCH_EXTRA_SECONDS", 8.0, 0.0, 300.0)
    jitter = _float_env("GOOGLE_ADS_MUTATION_JITTER_SECONDS", 1.5, 0.0, 30.0)
    if operation_count >= 500:
        global_delay += large_batch_delay
        customer_delay += large_batch_delay

    now = time.monotonic()
    with _LOCK:
        global _NEXT_GLOBAL_AT
        next_at = max(_NEXT_GLOBAL_AT, _NEXT_CUSTOMER_AT.get(customer_id, 0.0) if customer_id else 0.0)
        sleep_seconds = max(next_at - now, 0.0)
        after_request = max(now + sleep_seconds, time.monotonic()) + random.uniform(0.0, jitter)
        _NEXT_GLOBAL_AT = max(_NEXT_GLOBAL_AT, after_request + global_delay)
        if customer_id:
            _NEXT_CUSTOMER_AT[customer_id] = max(
                _NEXT_CUSTOMER_AT.get(customer_id, 0.0),
                after_request + customer_delay,
            )
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)
    return {
        "enabled": True,
        "customer_id": customer_id,
        "method_name": method_name,
        "operation_count": operation_count,
        "slept_seconds": round(sleep_seconds, 3),
        "global_delay_seconds": global_delay,
        "customer_delay_seconds": customer_delay,
    }


def _is_transient_concurrent_modification_error(exc: Any) -> bool:
    text = str(exc or "").lower()
    return "concurrent_modification" in text or "modify the same resource at once" in text


def paced_google_ads_mutate(
    service: Any,
    method_name: str,
    request: Any,
    *,
    timeout: int = 30,
    attempts: int = 3,
) -> Any:
    method: Callable[..., Any] = getattr(service, method_name)
    for attempt in range(max(int(attempts or 1), 1)):
        _pace_mutation_request(request, method_name)
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
