from __future__ import annotations

from typing import Optional

from app.config import Settings, get_settings


def runtime_role_status(settings: Optional[Settings] = None) -> dict[str, object]:
    runtime_settings = settings or get_settings()
    return {
        "app_instance_role": runtime_settings.app_instance_role,
        "is_primary_instance": runtime_settings.is_primary_instance,
        "live_google_ads_allowed": runtime_settings.live_google_ads_allowed,
    }


def primary_instance_required_result(settings: Optional[Settings] = None) -> Optional[dict[str, object]]:
    status = runtime_role_status(settings)
    if status["live_google_ads_allowed"]:
        return None
    return {
        "mode": "skipped",
        "reason": "not_primary_instance",
        "message": "This app instance is not the primary automation controller; live Google Ads automation is disabled here.",
        **status,
    }
