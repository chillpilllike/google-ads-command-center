from __future__ import annotations

import ssl
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import OdooSaleOrder, OdooStore, OdooWebsite


CONFIRMED_STATES = ("sale", "done")


def _normalize_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def _parse_odoo_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _many2one_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "")
    return ""


def _many2one_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _server_proxy(url: str) -> xmlrpc.client.ServerProxy:
    context = ssl.create_default_context()
    return xmlrpc.client.ServerProxy(url, allow_none=True, context=context)


def _authenticate_store(store: OdooStore) -> tuple[str, int, xmlrpc.client.ServerProxy]:
    base_url = _normalize_url(store.base_url)
    if not base_url:
        raise RuntimeError("Odoo URL is empty.")
    if not store.database or not store.username or not store.api_key:
        raise RuntimeError("Odoo database, user, and password/API key are required.")

    common = _server_proxy(f"{base_url}/xmlrpc/2/common")
    uid = common.authenticate(store.database, store.username, store.api_key, {})
    if not uid:
        raise RuntimeError("Odoo authentication failed.")
    models = _server_proxy(f"{base_url}/xmlrpc/2/object")
    return base_url, int(uid), models


def sync_store_websites(session: Session, store: OdooStore) -> int:
    _base_url, uid, models = _authenticate_store(store)
    fields = ["id", "name", "domain"]
    try:
        rows = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "website",
            "search_read",
            [[]],
            {"fields": fields, "order": "name", "limit": 1000},
        )
    except Exception:
        rows = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "website",
            "search_read",
            [[]],
            {"fields": ["id", "name"], "order": "name", "limit": 1000},
        )

    session.execute(
        update(OdooWebsite)
        .where(OdooWebsite.store_id == store.id)
        .values(is_active=False)
    )
    saved = 0
    for row in rows:
        stmt = insert(OdooWebsite).values(
            store_id=store.id,
            website_id=int(row["id"]),
            name=str(row.get("name") or f"Website {row['id']}"),
            domain=str(row.get("domain") or ""),
            is_active=True,
            fetched_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OdooWebsite.store_id, OdooWebsite.website_id],
            set_={
                "name": stmt.excluded.name,
                "domain": stmt.excluded.domain,
                "is_active": True,
                "fetched_at": stmt.excluded.fetched_at,
            },
        )
        session.execute(stmt)
        saved += 1
    session.commit()
    return saved


def sync_store_confirmed_orders(
    session: Session,
    store: OdooStore,
    *,
    hours: int = 12,
    website_ids: list[int] | None = None,
    commit_every: int = 250,
) -> int:
    _base_url, uid, models = _authenticate_store(store)
    since = datetime.now(timezone.utc) - timedelta(hours=max(int(hours or 12), 1))

    domain: list[Any] = [
        ["state", "in", list(CONFIRMED_STATES)],
        ["date_order", ">=", since.strftime("%Y-%m-%d %H:%M:%S")],
    ]
    selected_website_ids = [int(item) for item in (website_ids or []) if item]
    if selected_website_ids:
        domain.append(["website_id", "in", selected_website_ids])
    elif store.website_id:
        domain.append(["website_id", "=", int(store.website_id)])

    field_meta = models.execute_kw(
        store.database,
        uid,
        store.api_key,
        "sale.order",
        "fields_get",
        [],
        {"attributes": ["string"]},
    )
    fields = ["id", "name", "date_order", "state", "currency_id", "amount_total", "website_id"]
    for optional_field in ("margin", "margin_percent"):
        if optional_field in field_meta:
            fields.append(optional_field)
    rows = models.execute_kw(
        store.database,
        uid,
        store.api_key,
        "sale.order",
        "search_read",
        [domain],
        {"fields": fields, "order": "date_order desc", "limit": 5000},
    )

    saved = 0
    batch_size = max(int(commit_every or 250), 1)
    for row in rows:
        order_dt = _parse_odoo_datetime(row.get("date_order"))
        currency_code = _many2one_name(row.get("currency_id")).split(" ")[0].upper()
        website_id = _many2one_id(row.get("website_id"))
        website_name = _many2one_name(row.get("website_id"))
        margin_amount = _float_or_none(row.get("margin"))
        margin_percent = _float_or_none(row.get("margin_percent"))
        stmt = insert(OdooSaleOrder).values(
            store_id=store.id,
            odoo_order_id=int(row["id"]),
            order_name=str(row.get("name") or row["id"]),
            order_datetime=order_dt,
            state=str(row.get("state") or ""),
            currency_code=currency_code,
            amount_total=float(row.get("amount_total") or 0),
            website_id=website_id,
            website_name=website_name,
            margin_amount=margin_amount,
            margin_percent=margin_percent,
            payload=row,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[OdooSaleOrder.store_id, OdooSaleOrder.odoo_order_id],
            set_={
                "order_name": stmt.excluded.order_name,
                "order_datetime": stmt.excluded.order_datetime,
                "state": stmt.excluded.state,
                "currency_code": stmt.excluded.currency_code,
                "amount_total": stmt.excluded.amount_total,
                "website_id": stmt.excluded.website_id,
                "website_name": stmt.excluded.website_name,
                "margin_amount": stmt.excluded.margin_amount,
                "margin_percent": stmt.excluded.margin_percent,
                "payload": stmt.excluded.payload,
                "synced_at": stmt.excluded.synced_at,
            },
        )
        session.execute(stmt)
        saved += 1
        if saved % batch_size == 0:
            store.last_sync_at = datetime.now(timezone.utc)
            store.last_sync_error = None
            session.commit()

    store.last_sync_at = datetime.now(timezone.utc)
    store.last_sync_error = None
    session.commit()
    return saved


def active_store_count(session: Session) -> int:
    return len(session.scalars(select(OdooStore).where(OdooStore.is_active.is_(True))).all())
