from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    GoogleAdsAccount,
    GoogleAdsConnection,
    GoogleAdsCustomerMatchPublication,
    GoogleAdsCustomerMatchOutbox,
    OdooCustomerMatchMember,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.odoo_sales import _authenticate_store, _many2one_id, _many2one_name, _parse_odoo_datetime


CSV_HEADER = ["Email", "Phone", "First Name", "Last Name", "Country", "Zip"]
DEFAULT_LOOKBACK_DAYS = 540
DATA_MANAGER_BASE_URL = "https://datamanager.googleapis.com/v1"
DATA_MANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
DATA_MANAGER_BATCH_SIZE = 10000
DATA_MANAGER_QUOTA_RETRY_HOURS = 24
DATA_MANAGER_TRANSIENT_RETRY_HOURS = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataManagerApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, response_text: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.response_text = str(response_text or "")


def sha256_hex(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_email(value: Any) -> str:
    email = re.sub(r"\s+", "", str(value or "").strip().lower())
    return email if "@" in email and "." in email.rsplit("@", 1)[-1] else ""


def normalize_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def split_name(value: Any) -> tuple[str, str]:
    text = normalize_name(value)
    if not text:
        return "", ""
    parts = text.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def normalize_country(value: Any) -> str:
    text = str(value or "").strip().upper()
    if len(text) == 2 and text.isalpha():
        return text
    if len(text) == 3 and text.isalpha():
        return text
    return ""


def normalize_phone(value: Any, country_code: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("+"):
        digits = re.sub(r"\D+", "", text)
        return f"+{digits}" if len(digits) >= 8 else ""
    digits = re.sub(r"\D+", "", text)
    if len(digits) < 8:
        return ""
    if country_code.upper() in {"CA", "CAN", "US", "USA"} and len(digits) == 10:
        return f"+1{digits}"
    if digits.startswith("00") and len(digits) > 10:
        return f"+{digits[2:]}"
    return ""


def customer_match_feed_token(publication: GoogleAdsCustomerMatchPublication) -> str:
    settings = get_settings()
    token_source = ":".join(
        [
            str(publication.id or 0),
            str(publication.account_id or 0),
            str(publication.store_id or 0),
            str(publication.website_id or 0),
            str(publication.list_kind or ""),
            settings.secret_key,
        ]
    )
    return hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:32]


def public_customer_match_url(publication: GoogleAdsCustomerMatchPublication, *, base_url: str = "") -> str:
    base = str(base_url or get_settings().public_base_url or "").strip().rstrip("/")
    path = f"/customer-match/feeds/{publication.id}.csv?token={customer_match_feed_token(publication)}"
    return f"{base}{path}" if base else path


def google_ads_customer_id(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _connection_for_publication(publication: GoogleAdsCustomerMatchPublication) -> GoogleAdsConnection:
    account = publication.account
    connection = account.connection if account is not None else None
    if connection is None or not connection.refresh_token:
        raise DataManagerApiError("Google Ads OAuth connection is missing for this account.")
    if not connection.client_id or not connection.client_secret:
        raise DataManagerApiError("Google OAuth client id/secret is missing for this connection.")
    return connection


def authorized_data_manager_session(connection: GoogleAdsConnection) -> AuthorizedSession:
    credentials = Credentials(
        token=None,
        refresh_token=connection.refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=connection.client_id,
        client_secret=connection.client_secret,
        scopes=[DATA_MANAGER_SCOPE],
    )
    credentials.refresh(Request())
    return AuthorizedSession(credentials)


def data_manager_account_resource(account: GoogleAdsAccount) -> str:
    customer_id = google_ads_customer_id(account.customer_id)
    if not customer_id:
        raise DataManagerApiError("Google Ads customer id is missing for this publication.")
    return f"accountTypes/GOOGLE_ADS/accounts/{customer_id}"


def data_manager_login_account_resource(account: GoogleAdsAccount) -> str:
    manager_id = google_ads_customer_id(account.manager_customer_id)
    customer_id = google_ads_customer_id(account.customer_id)
    if manager_id and manager_id != customer_id:
        return f"accountTypes/GOOGLE_ADS/accounts/{manager_id}"
    return ""


def _headers_for_data_manager(account: GoogleAdsAccount) -> dict[str, str]:
    login_account = data_manager_login_account_resource(account)
    return {"login-account": login_account} if login_account else {}


def _post_data_manager_json(
    http: AuthorizedSession,
    url: str,
    payload: dict[str, Any],
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 60,
) -> dict[str, Any]:
    response = http.post(url, json=payload, params=params or {}, headers=headers or {}, timeout=timeout)
    if response.status_code >= 400:
        raise DataManagerApiError(
            f"Data Manager API error {response.status_code}: {response.text[:1000]}",
            status_code=response.status_code,
            response_text=response.text[:4000],
        )
    if not response.text.strip():
        return {}
    return response.json()


def _data_manager_user_list_payload(publication: GoogleAdsCustomerMatchPublication) -> dict[str, Any]:
    return {
        "displayName": publication.data_manager_user_list_name or publication.list_name,
        "description": (
            "Odoo purchasers synced by website/account mapping. "
            f"Store {publication.store_id}, website {publication.website_id}, publication {publication.id}."
        ),
        "membershipDuration": "46656000s",
        "membershipStatus": "OPEN",
        "integrationCode": f"odoo-cm-{publication.account_id}-{publication.store_id}-{publication.website_id}-{publication.list_kind}",
        "ingestedUserListInfo": {
            "uploadKeyTypes": ["CONTACT_ID"],
            "contactIdInfo": {"dataSourceType": "DATA_SOURCE_TYPE_FIRST_PARTY"},
        },
    }


def _extract_user_list_id(payload: dict[str, Any]) -> str:
    raw_id = payload.get("id")
    if raw_id:
        return str(raw_id)
    name = str(payload.get("name") or "")
    match = re.search(r"/userLists/(\d+)$", name)
    return match.group(1) if match else ""


def ensure_data_manager_user_list(
    session: Session,
    publication: GoogleAdsCustomerMatchPublication,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    if publication.data_manager_user_list_id:
        return {
            "status": "exists",
            "user_list_id": publication.data_manager_user_list_id,
            "resource": publication.data_manager_user_list_resource,
        }
    if not publication.customer_match_policy_accepted:
        raise DataManagerApiError("Customer Match policy acceptance is required before creating an API audience.")
    account = publication.account
    if account is None:
        raise DataManagerApiError("Google Ads account is missing for this publication.")
    connection = _connection_for_publication(publication)
    http = authorized_data_manager_session(connection)
    parent = data_manager_account_resource(account)
    payload = _data_manager_user_list_payload(publication)
    response = _post_data_manager_json(
        http,
        f"{DATA_MANAGER_BASE_URL}/{parent}/userLists",
        payload,
        params={"validateOnly": "true"} if validate_only else None,
        headers=_headers_for_data_manager(account),
    )
    user_list_id = _extract_user_list_id(response)
    if not validate_only:
        publication.data_manager_user_list_id = user_list_id
        publication.data_manager_user_list_resource = str(response.get("name") or "")
        publication.data_manager_user_list_name = str(response.get("displayName") or publication.list_name or "")
        publication.data_manager_status = "audience_ready" if user_list_id else "audience_created"
        publication.data_manager_last_response_json = response
        publication.data_manager_last_error = ""
        session.commit()
    return {"status": "validated" if validate_only else "created", "user_list_id": user_list_id, "response": response}


def _audience_member_payload(member: OdooCustomerMatchMember) -> Optional[dict[str, Any]]:
    identifiers: list[dict[str, Any]] = []
    if member.hashed_email:
        identifiers.append({"emailAddress": member.hashed_email})
    if member.hashed_phone:
        identifiers.append({"phoneNumber": member.hashed_phone})
    country = str(member.country_code or "").upper()[:2]
    if member.hashed_first_name and member.hashed_last_name and country and member.zip_code:
        identifiers.append(
            {
                "address": {
                    "givenName": member.hashed_first_name,
                    "familyName": member.hashed_last_name,
                    "regionCode": country,
                    "postalCode": member.zip_code,
                }
            }
        )
    if not identifiers:
        return None
    return {
        "destinationReferences": ["customer_match"],
        "compositeData": {"userData": {"userIdentifiers": identifiers[:10]}},
    }


def _destination_payload(publication: GoogleAdsCustomerMatchPublication) -> dict[str, Any]:
    account = publication.account
    if account is None:
        raise DataManagerApiError("Google Ads account is missing for this publication.")
    operating_account = {"accountType": "GOOGLE_ADS", "accountId": google_ads_customer_id(account.customer_id)}
    destination: dict[str, Any] = {
        "reference": "customer_match",
        "operatingAccount": operating_account,
        "productDestinationId": str(publication.data_manager_user_list_id or ""),
    }
    manager_id = google_ads_customer_id(account.manager_customer_id)
    if manager_id and manager_id != operating_account["accountId"]:
        destination["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": manager_id}
    return destination


def _is_quota_error(exc: DataManagerApiError) -> bool:
    text = exc.response_text.lower()
    return exc.status_code == 429 or "quota" in text or "rate" in text or "resource_exhausted" in text


def _is_reauth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient" in text and "scope" in text or "invalid_scope" in text or "reauth" in text


def _safe_slug(value: Any, default: str = "website") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text[:60].strip("-") or default


def _website_for_mapping(session: Session, mapping: OdooStoreGoogleAdsMapping) -> Optional[OdooWebsite]:
    if not int(mapping.website_id or 0):
        return None
    return session.scalar(
        select(OdooWebsite).where(
            OdooWebsite.store_id == mapping.store_id,
            OdooWebsite.website_id == int(mapping.website_id),
            OdooWebsite.is_active.is_(True),
        )
    )


def ensure_customer_match_publication(
    session: Session,
    mapping: OdooStoreGoogleAdsMapping,
    *,
    list_kind: str = "purchasers",
) -> GoogleAdsCustomerMatchPublication:
    website = _website_for_mapping(session, mapping)
    website_id = int(website.website_id if website is not None else mapping.website_id or 0)
    website_name = website.name if website is not None else ("All websites" if not website_id else f"Website {website_id}")
    row = session.scalar(
        select(GoogleAdsCustomerMatchPublication).where(
            GoogleAdsCustomerMatchPublication.account_id == mapping.account_id,
            GoogleAdsCustomerMatchPublication.store_id == mapping.store_id,
            GoogleAdsCustomerMatchPublication.website_id == website_id,
            GoogleAdsCustomerMatchPublication.list_kind == list_kind,
        )
    )
    if row is None:
        row = GoogleAdsCustomerMatchPublication(
            account_id=mapping.account_id,
            store_id=mapping.store_id,
            website_id=website_id,
            list_kind=list_kind,
            access_username=f"cm_{mapping.account.customer_id}_{website_id}",
            access_password=secrets.token_urlsafe(18),
        )
        session.add(row)
        session.flush()
    row.website_name = website_name
    row.list_name = (
        row.list_name
        or f"AUTO | Customer Match | {mapping.account.name} | {website_name} | {list_kind.title()}"
    )
    row.data_manager_user_list_name = row.data_manager_user_list_name or row.list_name
    if not row.data_manager_status or row.data_manager_status == "not_configured":
        row.data_manager_status = "planned"
    row.status = "blocked_all_websites" if not website_id else (row.status if row.status != "blocked_all_websites" else "planned")
    return row


def ensure_customer_match_publications(session: Session) -> list[GoogleAdsCustomerMatchPublication]:
    mappings = session.scalars(
        select(OdooStoreGoogleAdsMapping)
        .where(OdooStoreGoogleAdsMapping.is_active.is_(True))
        .order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)
    ).all()
    rows = [ensure_customer_match_publication(session, mapping) for mapping in mappings]
    session.commit()
    return rows


def enqueue_customer_match_outbox(
    session: Session,
    publication: GoogleAdsCustomerMatchPublication,
    customer_keys: Iterable[str],
) -> int:
    keys = [str(key) for key in customer_keys if key]
    if not keys:
        return 0
    member_ids = session.scalars(
        select(OdooCustomerMatchMember.id).where(
            OdooCustomerMatchMember.account_id == publication.account_id,
            OdooCustomerMatchMember.store_id == publication.store_id,
            OdooCustomerMatchMember.website_id == publication.website_id,
            OdooCustomerMatchMember.customer_key.in_(keys),
        )
    ).all()
    if not member_ids:
        return 0
    now = utcnow()
    rows = [
        {
            "publication_id": publication.id,
            "member_id": int(member_id),
            "action": "add",
            "status": "pending",
            "next_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        for member_id in member_ids
    ]
    stmt = insert(GoogleAdsCustomerMatchOutbox).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=[
            GoogleAdsCustomerMatchOutbox.publication_id,
            GoogleAdsCustomerMatchOutbox.member_id,
            GoogleAdsCustomerMatchOutbox.action,
        ]
    )
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def sync_customer_match_members_for_publication(
    session: Session,
    publication: GoogleAdsCustomerMatchPublication,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_orders: int = 20000,
) -> dict[str, Any]:
    if not publication.customer_match_policy_accepted:
        return {
            "status": "blocked_policy",
            "reason": "Customer Match policy acceptance is required before syncing customer identifiers.",
            "saved": 0,
        }
    if not int(publication.website_id or 0):
        return {
            "status": "blocked_all_websites",
            "reason": "Customer Match sync requires a specific Odoo website mapping; all-website feeds are disabled to prevent data mixing.",
            "saved": 0,
        }
    store: OdooStore = publication.store
    _base_url, uid, models = _authenticate_store(store)
    since = utcnow() - timedelta(days=max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 1))
    order_domain: list[Any] = [
        ["state", "in", ["sale", "done"]],
        ["date_order", ">=", since.strftime("%Y-%m-%d %H:%M:%S")],
        ["website_id", "=", int(publication.website_id)],
    ]
    order_fields = ["id", "name", "date_order", "currency_id", "amount_total", "website_id", "partner_id"]
    rows = models.execute_kw(
        store.database,
        uid,
        store.api_key,
        "sale.order",
        "search_read",
        [order_domain],
        {"fields": order_fields, "order": "date_order desc", "limit": max(int(max_orders or 20000), 1)},
    )
    partner_ids = list(
        dict.fromkeys(
            int(pid)
            for pid in (_many2one_id(row.get("partner_id")) for row in rows)
            if pid
        )
    )
    partner_by_id: dict[int, dict[str, Any]] = {}
    if partner_ids:
        partner_fields = ["id", "name", "email", "phone", "mobile", "country_id", "zip"]
        partners = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "res.partner",
            "read",
            [partner_ids],
            {"fields": partner_fields},
        )
        partner_by_id = {int(row["id"]): row for row in partners if isinstance(row, dict) and row.get("id")}

    now = utcnow()
    by_key: dict[str, dict[str, Any]] = {}
    skipped_no_identifier = 0
    for row in rows:
        partner_id = _many2one_id(row.get("partner_id")) or 0
        partner = partner_by_id.get(int(partner_id), {})
        country = normalize_country(_many2one_name(partner.get("country_id")))
        email = normalize_email(partner.get("email"))
        phone = normalize_phone(partner.get("mobile") or partner.get("phone"), country)
        first_name, last_name = split_name(partner.get("name") or _many2one_name(row.get("partner_id")))
        zip_code = re.sub(r"\s+", " ", str(partner.get("zip") or "").strip()).upper()
        hashed_email = sha256_hex(email)
        hashed_phone = sha256_hex(phone)
        hashed_first = sha256_hex(first_name)
        hashed_last = sha256_hex(last_name)
        if not (hashed_email or hashed_phone or (hashed_first and hashed_last and country and zip_code)):
            skipped_no_identifier += 1
            continue
        customer_key = hashlib.sha256(
            "|".join([hashed_email, hashed_phone, hashed_first, hashed_last, country, zip_code]).encode("utf-8")
        ).hexdigest()
        entry = by_key.setdefault(
            customer_key,
            {
                "account_id": publication.account_id,
                "store_id": publication.store_id,
                "website_id": publication.website_id,
                "website_name": publication.website_name,
                "odoo_partner_id": int(partner_id or 0),
                "customer_key": customer_key,
                "hashed_email": hashed_email,
                "hashed_phone": hashed_phone,
                "hashed_first_name": hashed_first,
                "hashed_last_name": hashed_last,
                "country_code": country[:3],
                "zip_code": zip_code[:40],
                "order_count": 0,
                "total_value": 0.0,
                "currency_code": _many2one_name(row.get("currency_id")).split(" ")[0].upper(),
                "first_order_at": None,
                "last_order_at": None,
                "source_order_ids": [],
                "source_json": {
                    "source": "odoo_sale_order_customer_match",
                    "publication_id": publication.id,
                    "pii_storage": "sha256_hashed_only",
                },
                "last_synced_at": now,
                "updated_at": now,
            },
        )
        order_dt = _parse_odoo_datetime(row.get("date_order"))
        entry["order_count"] += 1
        entry["total_value"] += float(row.get("amount_total") or 0)
        entry["first_order_at"] = min([value for value in [entry["first_order_at"], order_dt] if value is not None])
        entry["last_order_at"] = max([value for value in [entry["last_order_at"], order_dt] if value is not None])
        if len(entry["source_order_ids"]) < 100:
            entry["source_order_ids"].append(int(row["id"]))

    if by_key:
        stmt = insert(OdooCustomerMatchMember).values(list(by_key.values()))
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                OdooCustomerMatchMember.account_id,
                OdooCustomerMatchMember.store_id,
                OdooCustomerMatchMember.website_id,
                OdooCustomerMatchMember.customer_key,
            ],
            set_={
                "website_name": excluded.website_name,
                "odoo_partner_id": excluded.odoo_partner_id,
                "hashed_email": excluded.hashed_email,
                "hashed_phone": excluded.hashed_phone,
                "hashed_first_name": excluded.hashed_first_name,
                "hashed_last_name": excluded.hashed_last_name,
                "country_code": excluded.country_code,
                "zip_code": excluded.zip_code,
                "order_count": excluded.order_count,
                "total_value": excluded.total_value,
                "currency_code": excluded.currency_code,
                "first_order_at": excluded.first_order_at,
                "last_order_at": excluded.last_order_at,
                "source_order_ids": excluded.source_order_ids,
                "source_json": excluded.source_json,
                "last_synced_at": excluded.last_synced_at,
                "updated_at": excluded.updated_at,
            },
        )
        session.execute(stmt)
        session.flush()
        queued_for_api = enqueue_customer_match_outbox(session, publication, by_key.keys())
    else:
        queued_for_api = 0
    publication.last_synced_at = now
    publication.last_sync_json = {
        "status": "done",
        "lookback_days": lookback_days,
        "orders_seen": len(rows),
        "members_saved": len(by_key),
        "api_outbox_queued": queued_for_api,
        "skipped_no_identifier": skipped_no_identifier,
        "synced_at": now.isoformat(),
    }
    publication.last_error = ""
    publication.status = "ready" if publication.customer_match_policy_accepted else "planned"
    session.commit()
    return publication.last_sync_json


def customer_match_member_rows(
    session: Session,
    publication: GoogleAdsCustomerMatchPublication,
    *,
    limit: Optional[int] = None,
) -> Iterable[OdooCustomerMatchMember]:
    query = (
        select(OdooCustomerMatchMember)
        .where(
            OdooCustomerMatchMember.account_id == publication.account_id,
            OdooCustomerMatchMember.store_id == publication.store_id,
            OdooCustomerMatchMember.website_id == publication.website_id,
        )
        .order_by(OdooCustomerMatchMember.last_order_at.desc(), OdooCustomerMatchMember.id.desc())
    )
    if limit:
        query = query.limit(int(limit))
    return session.scalars(query).all()


def pending_customer_match_outbox_count(session: Session, publication: GoogleAdsCustomerMatchPublication) -> int:
    now = utcnow()
    return int(
        session.scalar(
            select(func.count(GoogleAdsCustomerMatchOutbox.id)).where(
                GoogleAdsCustomerMatchOutbox.publication_id == publication.id,
                GoogleAdsCustomerMatchOutbox.action == "add",
                GoogleAdsCustomerMatchOutbox.status.in_(["pending", "deferred"]),
                or_(
                    GoogleAdsCustomerMatchOutbox.next_attempt_at.is_(None),
                    GoogleAdsCustomerMatchOutbox.next_attempt_at <= now,
                ),
            )
        )
        or 0
    )


def customer_match_outbox_summary(session: Session, publication: GoogleAdsCustomerMatchPublication) -> dict[str, int]:
    rows = session.execute(
        select(GoogleAdsCustomerMatchOutbox.status, func.count(GoogleAdsCustomerMatchOutbox.id))
        .where(GoogleAdsCustomerMatchOutbox.publication_id == publication.id)
        .group_by(GoogleAdsCustomerMatchOutbox.status)
    ).all()
    return {str(status): int(count or 0) for status, count in rows}


def push_customer_match_outbox_for_publication(
    session: Session,
    publication: GoogleAdsCustomerMatchPublication,
    *,
    batch_size: int = DATA_MANAGER_BATCH_SIZE,
    max_batches: int = 1,
    validate_only: bool = False,
) -> dict[str, Any]:
    if not publication.customer_match_policy_accepted:
        return {
            "status": "blocked_policy",
            "reason": "Customer Match policy acceptance is required before uploading to Google.",
            "sent": 0,
        }
    if not publication.data_manager_api_enabled:
        return {"status": "disabled", "sent": 0}
    if not int(publication.website_id or 0):
        return {
            "status": "blocked_all_websites",
            "reason": "Customer Match API sync requires a specific website mapping.",
            "sent": 0,
        }
    account = publication.account
    if account is None:
        return {"status": "failed", "reason": "Google Ads account is missing.", "sent": 0}

    started_at = utcnow()
    pending_before_auth = pending_customer_match_outbox_count(session, publication)
    if pending_before_auth <= 0:
        result = {
            "status": "done",
            "sent": 0,
            "pending": 0,
            "batches": [],
            "started_at": started_at.isoformat(),
            "finished_at": utcnow().isoformat(),
            "source": "outbox_cache",
        }
        publication.last_sync_json = {**(publication.last_sync_json or {}), "api_push": result}
        if publication.data_manager_status in {"planned", "audience_ready", "audience_created", "validated", "synced"}:
            publication.data_manager_status = "synced" if publication.data_manager_user_list_id else publication.data_manager_status
        session.commit()
        return result

    try:
        ensure_data_manager_user_list(session, publication, validate_only=False)
        connection = _connection_for_publication(publication)
        http = authorized_data_manager_session(connection)
    except Exception as exc:  # noqa: BLE001
        publication.data_manager_status = "blocked_oauth" if _is_reauth_error(exc) else "failed"
        publication.data_manager_last_error = str(exc)
        session.commit()
        return {"status": publication.data_manager_status, "reason": str(exc), "sent": 0}

    total_sent = 0
    batches: list[dict[str, Any]] = []
    now = utcnow()
    max_batches = max(int(max_batches or 1), 1)
    batch_size = min(max(int(batch_size or DATA_MANAGER_BATCH_SIZE), 1), DATA_MANAGER_BATCH_SIZE)
    for _batch_index in range(max_batches):
        outbox_rows = session.scalars(
            select(GoogleAdsCustomerMatchOutbox)
            .where(
                GoogleAdsCustomerMatchOutbox.publication_id == publication.id,
                GoogleAdsCustomerMatchOutbox.action == "add",
                GoogleAdsCustomerMatchOutbox.status.in_(["pending", "deferred"]),
                or_(
                    GoogleAdsCustomerMatchOutbox.next_attempt_at.is_(None),
                    GoogleAdsCustomerMatchOutbox.next_attempt_at <= now,
                ),
            )
            .order_by(GoogleAdsCustomerMatchOutbox.created_at, GoogleAdsCustomerMatchOutbox.id)
            .limit(batch_size)
        ).all()
        if not outbox_rows:
            break

        audience_members: list[dict[str, Any]] = []
        valid_outbox_rows: list[GoogleAdsCustomerMatchOutbox] = []
        invalid_ids: list[int] = []
        for outbox_row in outbox_rows:
            payload = _audience_member_payload(outbox_row.member)
            if payload is None:
                invalid_ids.append(outbox_row.id)
                continue
            audience_members.append(payload)
            valid_outbox_rows.append(outbox_row)

        if invalid_ids:
            session.query(GoogleAdsCustomerMatchOutbox).filter(GoogleAdsCustomerMatchOutbox.id.in_(invalid_ids)).update(
                {
                    "status": "failed",
                    "last_error": "No valid hashed identifiers available for this member.",
                    "last_attempt_at": now,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            session.commit()

        if not audience_members:
            continue

        request_payload = {
            "destinations": [_destination_payload(publication)],
            "audienceMembers": audience_members,
            "consent": {
                "adUserData": "CONSENT_GRANTED",
                "adPersonalization": "CONSENT_GRANTED",
            },
            "validateOnly": bool(validate_only),
            "encoding": "HEX",
            "termsOfService": {"customerMatchTermsOfServiceStatus": "ACCEPTED"},
        }
        try:
            response = _post_data_manager_json(
                http,
                f"{DATA_MANAGER_BASE_URL}/audienceMembers:ingest",
                request_payload,
                headers=_headers_for_data_manager(account),
                timeout=90,
            )
        except DataManagerApiError as exc:
            retry_at = now + timedelta(hours=DATA_MANAGER_QUOTA_RETRY_HOURS if _is_quota_error(exc) else DATA_MANAGER_TRANSIENT_RETRY_HOURS)
            status = "deferred" if (_is_quota_error(exc) or exc.status_code >= 500) else "failed"
            for outbox_row in valid_outbox_rows:
                outbox_row.status = status
                outbox_row.attempt_count = int(outbox_row.attempt_count or 0) + 1
                outbox_row.next_attempt_at = retry_at if status == "deferred" else None
                outbox_row.last_attempt_at = now
                outbox_row.last_error = str(exc)
            publication.data_manager_status = "quota_deferred" if _is_quota_error(exc) else status
            publication.data_manager_last_error = str(exc)
            session.commit()
            return {
                "status": publication.data_manager_status,
                "sent": total_sent,
                "deferred": len(valid_outbox_rows) if status == "deferred" else 0,
                "failed": len(valid_outbox_rows) if status == "failed" else 0,
                "next_attempt_at": retry_at.isoformat() if status == "deferred" else "",
                "reason": str(exc),
            }

        request_id = str(response.get("requestId") or "")
        for outbox_row in valid_outbox_rows:
            outbox_row.status = "pending" if validate_only else "sent"
            outbox_row.attempt_count = int(outbox_row.attempt_count or 0) + 1
            outbox_row.next_attempt_at = now if validate_only else None
            outbox_row.last_attempt_at = now
            outbox_row.sent_at = None if validate_only else now
            outbox_row.last_request_id = request_id
            outbox_row.last_error = ""
            outbox_row.response_json = response
        publication.data_manager_status = "validated" if validate_only else "synced"
        publication.data_manager_last_request_id = request_id
        publication.data_manager_last_response_json = response
        publication.data_manager_last_error = ""
        publication.data_manager_last_pushed_at = now
        session.commit()
        total_sent += len(valid_outbox_rows)
        batches.append({"request_id": request_id, "rows": len(valid_outbox_rows)})

    pending = pending_customer_match_outbox_count(session, publication)
    result = {
        "status": "done" if pending == 0 else "partial",
        "sent": total_sent,
        "pending": pending,
        "batches": batches,
        "started_at": started_at.isoformat(),
        "finished_at": utcnow().isoformat(),
    }
    publication.last_sync_json = {**(publication.last_sync_json or {}), "api_push": result}
    session.commit()
    return result


def customer_match_csv_chunk(rows: Iterable[OdooCustomerMatchMember], *, include_header: bool = False) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if include_header:
        writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [
                row.hashed_email or "",
                row.hashed_phone or "",
                row.hashed_first_name or "",
                row.hashed_last_name or "",
                row.country_code or "",
                row.zip_code or "",
            ]
        )
    return output.getvalue()


def customer_match_filename(publication: GoogleAdsCustomerMatchPublication) -> str:
    parts = [
        "customer-match",
        _safe_slug(publication.account.name if publication.account else publication.account_id, "account"),
        _safe_slug(publication.website_name or publication.website_id, "website"),
        _safe_slug(publication.list_kind, "list"),
    ]
    return "-".join(parts) + ".csv"


def customer_match_summary(session: Session, publication: GoogleAdsCustomerMatchPublication) -> dict[str, Any]:
    row = session.execute(
        select(
            func.count(OdooCustomerMatchMember.id),
            func.max(OdooCustomerMatchMember.last_order_at),
            func.max(OdooCustomerMatchMember.last_synced_at),
        ).where(
            OdooCustomerMatchMember.account_id == publication.account_id,
            OdooCustomerMatchMember.store_id == publication.store_id,
            OdooCustomerMatchMember.website_id == publication.website_id,
        )
    ).one()
    outbox = customer_match_outbox_summary(session, publication)
    return {
        "member_count": int(row[0] or 0),
        "last_order_at": row[1],
        "last_synced_at": row[2],
        "outbox_pending": int(outbox.get("pending", 0) + outbox.get("deferred", 0)),
        "outbox_sent": int(outbox.get("sent", 0)),
        "outbox_failed": int(outbox.get("failed", 0)),
    }
