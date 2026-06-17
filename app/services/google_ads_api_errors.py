from __future__ import annotations

from typing import Any

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsApiError


def _compact(value: Any, *, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _google_ads_error_code(error: Any) -> str:
    error_code = getattr(error, "error_code", None)
    if error_code is None:
        return ""
    try:
        code_pb = getattr(error_code, "_pb", None)
        field = code_pb.WhichOneof("error_code") if code_pb is not None else None
        if field:
            value = getattr(error_code, field)
            value_name = getattr(value, "name", None) or str(value).split(".")[-1]
            return f"{field}.{value_name}"
    except Exception:  # noqa: BLE001 - best effort formatting for proto variants.
        pass
    return _compact(error_code, limit=180)


def summarize_google_ads_exception(exc: GoogleAdsException) -> dict[str, Any]:
    status = ""
    try:
        status = getattr(exc.error.code(), "name", "") or str(exc.error.code())
    except Exception:  # noqa: BLE001 - some client errors do not expose a grpc status.
        status = ""

    messages: list[str] = []
    codes: list[str] = []
    locations: list[str] = []
    try:
        for error in exc.failure.errors:
            message = _compact(getattr(error, "message", ""))
            if message:
                messages.append(message)
            code = _google_ads_error_code(error)
            if code:
                codes.append(code)
            try:
                location = ".".join(
                    str(part.field_name)
                    for part in error.location.field_path_elements
                    if getattr(part, "field_name", "")
                )
                if location:
                    locations.append(location)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        messages.append(_compact(exc))

    unique_codes = list(dict.fromkeys(code for code in codes if code))
    error_code = unique_codes[0] if unique_codes else status
    if status and status not in error_code:
        error_code = f"{status}:{error_code}" if error_code else status

    message = " | ".join(dict.fromkeys(messages)) or _compact(exc)
    request_id = _compact(getattr(exc, "request_id", ""), limit=120)
    return {
        "message": message,
        "error_code": _compact(error_code, limit=180),
        "request_id": request_id,
        "details": {
            "status": status,
            "codes": unique_codes,
            "locations": list(dict.fromkeys(locations)),
            "exception_class": exc.__class__.__name__,
        },
    }


def classify_google_ads_error(summary: dict[str, Any]) -> str:
    haystack = " ".join(
        [
            str(summary.get("error_code") or ""),
            str(summary.get("message") or ""),
            str(summary.get("details") or ""),
        ]
    ).upper()
    if any(token in haystack for token in ["RESOURCE_EXHAUSTED", "QUOTA", "RATE", "LIMIT"]):
        return "quota_or_limit"
    return str(summary.get("error_code") or "")[:180]


def google_ads_api_error_payload(error: GoogleAdsApiError) -> dict[str, object]:
    details = error.details or {}
    return {
        "id": error.id,
        "context": error.context,
        "severity": error.severity,
        "error_code": error.error_code,
        "request_id": error.request_id,
        "message": error.message,
        "created_at": error.created_at.isoformat() if error.created_at else None,
        "account": error.account.name if error.account else "",
        "customer_id": error.account.customer_id if error.account else "",
        "job_id": error.job_id,
        "job_label": error.job.label if error.job else "",
        "action_type": details.get("action_type", ""),
        "campaign_id": details.get("campaign_id", ""),
    }


def _manual_action_where():
    return (
        GoogleAdsApiError.acknowledged.is_(False),
        or_(
            GoogleAdsApiError.severity == "manual_action_required",
            GoogleAdsApiError.error_code == "quota_or_limit",
            GoogleAdsApiError.context.in_(["autopilot_apply", "optimizer_apply"]),
        ),
    )


async def recent_unacknowledged_google_ads_api_errors(
    session: AsyncSession,
    *,
    limit: int = 6,
) -> dict[str, object]:
    count = int(
        await session.scalar(
            select(func.count(GoogleAdsApiError.id)).where(GoogleAdsApiError.acknowledged.is_(False))
        )
        or 0
    )
    errors = (
        await session.scalars(
            select(GoogleAdsApiError)
            .where(GoogleAdsApiError.acknowledged.is_(False))
            .order_by(GoogleAdsApiError.created_at.desc(), GoogleAdsApiError.id.desc())
            .limit(limit)
        )
    ).all()
    manual_count = int(
        await session.scalar(select(func.count(GoogleAdsApiError.id)).where(*_manual_action_where()))
        or 0
    )
    manual_errors = (
        await session.scalars(
            select(GoogleAdsApiError)
            .where(*_manual_action_where())
            .order_by(GoogleAdsApiError.created_at.desc(), GoogleAdsApiError.id.desc())
            .limit(limit)
        )
    ).all()
    return {
        "count": count,
        "errors": [google_ads_api_error_payload(error) for error in errors],
        "manual_action_count": manual_count,
        "manual_action_errors": [google_ads_api_error_payload(error) for error in manual_errors],
    }


async def acknowledge_unacknowledged_google_ads_api_errors(
    session: AsyncSession,
    *,
    limit: int = 250,
) -> int:
    errors = (
        await session.scalars(
            select(GoogleAdsApiError)
            .where(GoogleAdsApiError.acknowledged.is_(False))
            .order_by(GoogleAdsApiError.created_at.desc(), GoogleAdsApiError.id.desc())
            .limit(limit)
        )
    ).all()
    for error in errors:
        error.acknowledged = True
    await session.commit()
    return len(errors)


def _account_details(account: GoogleAdsAccount | None) -> dict[str, Any]:
    if account is None:
        return {}
    return {
        "account_id": account.id,
        "account_name": account.name,
        "customer_id": account.customer_id,
        "manager_name": account.manager_name,
        "manager_customer_id": account.manager_customer_id,
        "connection_id": account.connection_id,
    }


def record_google_ads_api_error(
    session: Session,
    exc: GoogleAdsException,
    *,
    account: GoogleAdsAccount | None = None,
    job_id: int | None = None,
    context: str = "google_ads",
    severity: str = "error",
    extra: dict[str, Any] | None = None,
) -> None:
    summary = summarize_google_ads_exception(exc)
    summary["error_code"] = classify_google_ads_error(summary) or str(summary.get("error_code") or "")
    _record_error_row(
        session,
        account=account,
        job_id=job_id,
        context=context,
        severity=severity,
        error_code=str(summary.get("error_code") or ""),
        request_id=str(summary.get("request_id") or ""),
        message=str(summary.get("message") or exc),
        details={**summary.get("details", {}), **(extra or {}), **_account_details(account)},
    )


def record_google_ads_generic_error(
    session: Session,
    exc: Exception,
    *,
    account: GoogleAdsAccount | None = None,
    job_id: int | None = None,
    context: str = "google_ads",
    severity: str = "error",
    extra: dict[str, Any] | None = None,
) -> None:
    message = _compact(exc)
    upper_message = message.upper()
    error_code = exc.__class__.__name__
    if any(token in upper_message for token in ["RESOURCE_EXHAUSTED", "QUOTA", "RATE", "LIMIT"]):
        error_code = "quota_or_limit"
    _record_error_row(
        session,
        account=account,
        job_id=job_id,
        context=context,
        severity=severity,
        error_code=error_code,
        request_id="",
        message=message,
        details={
            "exception_class": exc.__class__.__name__,
            **(extra or {}),
            **_account_details(account),
        },
    )


def _record_error_row(
    session: Session,
    *,
    account: GoogleAdsAccount | None,
    job_id: int | None,
    context: str,
    severity: str,
    error_code: str,
    request_id: str,
    message: str,
    details: dict[str, Any],
) -> None:
    try:
        session.rollback()
        session.add(
            GoogleAdsApiError(
                account_id=account.id if account else None,
                connection_id=account.connection_id if account else None,
                job_id=job_id,
                context=context[:120],
                severity=severity[:40],
                error_code=(error_code or "")[:180],
                request_id=(request_id or "")[:120],
                message=_compact(message),
                details=details,
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 - error reporting must never break the worker.
        session.rollback()
