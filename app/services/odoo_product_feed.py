from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote_plus, urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    GoogleAdsAccount,
    GoogleAdsDataSnapshot,
    GoogleAdsPageFeedPublication,
    OdooProductPageSignal,
    OdooStore,
    OdooStoreGoogleAdsMapping,
    OdooWebsite,
)
from app.services.google_ads_page_feed_csv import (
    feed_kind_label,
    normalize_feed_kind,
    public_feed_filename,
    public_feed_name,
)
from app.services.currency_rates import convert_amount, get_latest_rate_snapshot_sync, snapshot_payload
from app.services.odoo_sales import (
    CONFIRMED_STATES,
    _authenticate_store,
    _float_or_none,
    _many2one_id,
    _many2one_name,
    _parse_odoo_datetime,
)
from app.services.page_feed_restrictions import get_restricted_title_terms_sync, restricted_title_match


GOOGLE_LANDING_PAGE_DATASET = "analysis_top_landing_pages_7d"
GOOGLE_SEARCH_TERM_DATASET = "analysis_top_search_terms_7d"
GOOGLE_KEYWORD_DATASET = "analysis_top_keywords_7d"
PRODUCT_CODE_RE = re.compile(r"\[([^\]]{2,80})\]")
TOKEN_RE = re.compile(r"[a-z0-9]+")
LABEL_PRIORITY = {"winner": 0, "fallback": 1, "watch": 2, "exclude": 3}


@dataclass
class ProductAggregate:
    product_code: str
    product_name: str
    product_ids: set[int] = field(default_factory=set)
    template_ids: set[int] = field(default_factory=set)
    order_ids: set[int] = field(default_factory=set)
    currency_code: str = ""
    quantity: float = 0.0
    sales_amount: float = 0.0
    margin_amount: float = 0.0
    product_url: str = ""
    brand_name: str = ""
    category_names: list[str] = field(default_factory=list)
    public_category_ids: list[int] = field(default_factory=list)
    list_price: float | None = None
    stock_qty: float | None = None
    published: bool | None = None
    active: bool | None = None
    daily: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class GoogleMetrics:
    cost: float = 0.0
    clicks: int = 0
    conversions: float = 0.0
    conversion_value: float = 0.0
    best_url: str = ""
    landing_pages: list[dict[str, Any]] = field(default_factory=list)
    search_terms: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[dict[str, Any]] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _chunked(items: list[int], size: int = 500) -> Iterable[list[int]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _limit_text(value: Any, length: int) -> str:
    return str(value or "").strip()[:length]


def _normalize_code(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _many2many_ids(value: Any) -> list[int]:
    if not value:
        return []
    if isinstance(value, list):
        ids: list[int] = []
        for item in value:
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, (list, tuple)) and item:
                try:
                    ids.append(int(item[0]))
                except (TypeError, ValueError):
                    continue
        return ids
    return []


def _field_label(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "").strip()
    return str(value or "").strip()


def _extract_product_code(name: str) -> str:
    match = PRODUCT_CODE_RE.search(str(name or ""))
    return match.group(1).strip() if match else ""


def _clean_product_name(name: str) -> str:
    return PRODUCT_CODE_RE.sub("", str(name or "")).strip(" -")


def _tokens(value: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(str(value or "").lower()) if len(token) >= 4]


def _normalize_domain_url(domain: str, fallback: str) -> str:
    raw = str(domain or fallback or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    return "https://" + raw.lstrip("/")


def _same_host(url: str, domain_url: str) -> bool:
    if not url or not domain_url:
        return True
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        domain_host = urlparse(domain_url).netloc.lower().removeprefix("www.")
    except Exception:
        return True
    return not domain_host or host == domain_host


def _absolute_product_url(domain_url: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if not text.startswith("/"):
        text = "/" + text
    return domain_url.rstrip("/") + text


def _fallback_search_url(domain_url: str, product: ProductAggregate) -> str:
    query = product.product_code or product.product_name
    return f"{domain_url.rstrip('/')}/shop?search={quote_plus(query)}" if domain_url else ""


def _is_broad_or_excluded_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    path = parsed.path.strip("/").lower()
    if not path:
        return True
    broad_markers = (
        "shop/category",
        "category/",
        "collections/",
        "cart",
        "checkout",
        "contact",
        "about",
        "blog",
        "privacy",
        "terms",
    )
    return any(marker in path for marker in broad_markers)


def _title_from_url(url: str) -> str:
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return "Google landing page"
    slug = path.split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    parts = [part for part in slug.split("-") if part and not re.fullmatch(r"[a-z0-9]{8,}", part)]
    return " ".join(parts[:12]).title() or "Google landing page"


def _latest_snapshot_rows(session: Session, account: GoogleAdsAccount, dataset_key: str) -> list[dict[str, Any]]:
    snapshot = session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key == dataset_key,
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None or not isinstance(snapshot.payload_json, dict):
        return []
    rows = snapshot.payload_json.get("rows")
    return rows if isinstance(rows, list) else []


def _safe_fields_get(models: Any, store: OdooStore, uid: int, model: str) -> dict[str, Any]:
    try:
        return models.execute_kw(
            store.database,
            uid,
            store.api_key,
            model,
            "fields_get",
            [],
            {"attributes": ["string"]},
        )
    except Exception:
        return {}


def _safe_read(
    models: Any,
    store: OdooStore,
    uid: int,
    model: str,
    ids: list[int],
    fields: list[str],
) -> list[dict[str, Any]]:
    if not ids:
        return []
    try:
        return models.execute_kw(
            store.database,
            uid,
            store.api_key,
            model,
            "read",
            [ids],
            {"fields": fields},
        )
    except Exception:
        minimal_fields = [field_name for field_name in ("id", "name", "display_name") if field_name in fields]
        if not minimal_fields:
            return []
        try:
            return models.execute_kw(
                store.database,
                uid,
                store.api_key,
                model,
                "read",
                [ids],
                {"fields": minimal_fields},
            )
        except Exception:
            return []


def _read_product_metadata(models: Any, store: OdooStore, uid: int, product_ids: set[int]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    if not product_ids:
        return {}, {}

    product_meta = _safe_fields_get(models, store, uid, "product.product")
    product_fields = [
        field_name
        for field_name in (
            "id",
            "name",
            "display_name",
            "default_code",
            "product_tmpl_id",
            "active",
            "qty_available",
            "virtual_available",
            "free_qty",
            "list_price",
            "lst_price",
            "categ_id",
            "public_categ_ids",
            "product_brand_id",
            "brand_id",
            "x_brand",
            "x_studio_brand",
        )
        if field_name in product_meta
    ]
    if "id" not in product_fields:
        product_fields.insert(0, "id")

    product_rows: list[dict[str, Any]] = []
    for chunk in _chunked(sorted(product_ids)):
        product_rows.extend(_safe_read(models, store, uid, "product.product", chunk, product_fields))
    products = {int(row["id"]): row for row in product_rows if row.get("id")}

    template_ids = {
        template_id
        for template_id in (_many2one_id(row.get("product_tmpl_id")) for row in product_rows)
        if template_id
    }
    template_meta = _safe_fields_get(models, store, uid, "product.template")
    template_fields = [
        field_name
        for field_name in (
            "id",
            "name",
            "default_code",
            "website_url",
            "active",
            "is_published",
            "website_published",
            "qty_available",
            "virtual_available",
            "free_qty",
            "list_price",
            "lst_price",
            "categ_id",
            "public_categ_ids",
            "product_brand_id",
            "brand_id",
            "x_brand",
            "x_studio_brand",
            "attribute_line_ids",
        )
        if field_name in template_meta
    ]
    if "id" not in template_fields:
        template_fields.insert(0, "id")

    template_rows: list[dict[str, Any]] = []
    for chunk in _chunked(sorted(template_ids)):
        template_rows.extend(_safe_read(models, store, uid, "product.template", chunk, template_fields))
    templates = {int(row["id"]): row for row in template_rows if row.get("id")}
    _attach_brand_attribute_values(models, store, uid, templates)
    return products, templates


def _attach_brand_attribute_values(models: Any, store: OdooStore, uid: int, templates: dict[int, dict[str, Any]]) -> None:
    line_ids = sorted(
        {
            line_id
            for row in templates.values()
            for line_id in _many2many_ids(row.get("attribute_line_ids"))
        }
    )
    if not line_ids:
        return
    line_meta = _safe_fields_get(models, store, uid, "product.template.attribute.line")
    if not line_meta:
        return
    line_fields = [
        field_name
        for field_name in ("id", "product_tmpl_id", "attribute_id", "value_ids")
        if field_name in line_meta
    ]
    if "id" not in line_fields:
        line_fields.insert(0, "id")
    lines: list[dict[str, Any]] = []
    for chunk in _chunked(line_ids):
        lines.extend(_safe_read(models, store, uid, "product.template.attribute.line", chunk, line_fields))
    value_ids = sorted(
        {
            value_id
            for line in lines
            if "brand" in _field_label(line.get("attribute_id")).lower()
            for value_id in _many2many_ids(line.get("value_ids"))
        }
    )
    value_names: dict[int, str] = {}
    if value_ids:
        value_meta = _safe_fields_get(models, store, uid, "product.attribute.value")
        value_fields = [field_name for field_name in ("id", "name") if field_name in value_meta]
        if "id" not in value_fields:
            value_fields.insert(0, "id")
        value_rows: list[dict[str, Any]] = []
        for chunk in _chunked(value_ids):
            value_rows.extend(_safe_read(models, store, uid, "product.attribute.value", chunk, value_fields))
        value_names = {int(row["id"]): str(row.get("name") or "").strip() for row in value_rows if row.get("id")}
    for line in lines:
        if "brand" not in _field_label(line.get("attribute_id")).lower():
            continue
        template_id = _many2one_id(line.get("product_tmpl_id"))
        if not template_id or template_id not in templates:
            continue
        values = [
            value_names.get(value_id, "")
            for value_id in _many2many_ids(line.get("value_ids"))
            if value_names.get(value_id, "")
        ]
        if values:
            templates[template_id]["_tp_brand_attribute_values"] = list(dict.fromkeys(values))


def _product_url_from_metadata(
    *,
    domain_url: str,
    product_row: dict[str, Any],
    template_row: dict[str, Any],
) -> str:
    for row in (template_row, product_row):
        url = row.get("website_url") if isinstance(row, dict) else ""
        if url:
            return _absolute_product_url(domain_url, str(url))
    return ""


def _stock_from_metadata(product_row: dict[str, Any], template_row: dict[str, Any]) -> float | None:
    for row in (template_row, product_row):
        if not isinstance(row, dict):
            continue
        for field_name in ("free_qty", "virtual_available", "qty_available"):
            if field_name in row and row.get(field_name) not in {None, ""}:
                return _as_float(row.get(field_name))
    return None


def _published_from_metadata(template_row: dict[str, Any]) -> bool | None:
    if not isinstance(template_row, dict):
        return None
    for field_name in ("is_published", "website_published"):
        if field_name in template_row:
            return bool(template_row.get(field_name))
    return None


def _active_from_metadata(product_row: dict[str, Any], template_row: dict[str, Any]) -> bool | None:
    states: list[bool] = []
    for row in (product_row, template_row):
        if isinstance(row, dict) and "active" in row:
            states.append(bool(row.get("active")))
    return all(states) if states else None


def _metadata_brand(product_row: dict[str, Any], template_row: dict[str, Any]) -> str:
    for row in (template_row, product_row):
        if not isinstance(row, dict):
            continue
        for field_name in ("product_brand_id", "brand_id", "x_brand", "x_studio_brand"):
            value = _field_label(row.get(field_name))
            if value:
                return value
        brand_values = [str(item or "").strip() for item in row.get("_tp_brand_attribute_values") or [] if str(item or "").strip()]
        if brand_values:
            return brand_values[0]
    return ""


def _metadata_categories(product_row: dict[str, Any], template_row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for row in (template_row, product_row):
        if not isinstance(row, dict):
            continue
        for field_name in ("categ_id", "public_categ_ids"):
            raw = row.get(field_name)
            if isinstance(raw, (list, tuple)) and len(raw) >= 2 and isinstance(raw[0], int):
                values.append(str(raw[1] or "").strip())
            elif isinstance(raw, list):
                for item in raw:
                    label = _field_label(item)
                    if label and not label.isdigit():
                        values.append(label)
    return list(dict.fromkeys(value for value in values if value))


def _metadata_public_category_ids(product_row: dict[str, Any], template_row: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for row in (template_row, product_row):
        if not isinstance(row, dict):
            continue
        ids.extend(_many2many_ids(row.get("public_categ_ids")))
    return list(dict.fromkeys(value for value in ids if value))


def _metadata_list_price(product_row: dict[str, Any], template_row: dict[str, Any]) -> float | None:
    for row in (template_row, product_row):
        if not isinstance(row, dict):
            continue
        for field_name in ("list_price", "lst_price"):
            if field_name in row and row.get(field_name) not in {None, ""}:
                price = _as_float(row.get(field_name))
                if price > 0:
                    return price
    return None


def _currency_code_from_m2o(value: Any) -> str:
    name = str(value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else value or "")
    token = name.strip().split(" ")[0].upper()
    return token if re.fullmatch(r"[A-Z]{3}", token) else ""


def _website_currency_code(models: Any, store: OdooStore, uid: int, website: OdooWebsite | None) -> str:
    if website is None:
        return ""
    meta = _safe_fields_get(models, store, uid, "website")
    fields = [field_name for field_name in ("id", "currency_id", "pricelist_id", "default_pricelist_id") if field_name in meta]
    if "id" not in fields:
        fields.insert(0, "id")
    try:
        rows = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "website",
            "read",
            [[int(website.website_id)]],
            {"fields": fields},
        )
    except Exception:
        rows = []
    row = rows[0] if rows else {}
    direct = _currency_code_from_m2o(row.get("currency_id"))
    if direct:
        return direct
    pricelist_id = _many2one_id(row.get("pricelist_id")) or _many2one_id(row.get("default_pricelist_id"))
    if not pricelist_id:
        return ""
    pricelist_meta = _safe_fields_get(models, store, uid, "product.pricelist")
    if "currency_id" not in pricelist_meta:
        return ""
    try:
        pricelists = models.execute_kw(
            store.database,
            uid,
            store.api_key,
            "product.pricelist",
            "read",
            [[int(pricelist_id)]],
            {"fields": ["id", "currency_id"]},
        )
    except Exception:
        pricelists = []
    return _currency_code_from_m2o((pricelists[0] if pricelists else {}).get("currency_id"))


def _fetch_odoo_product_totals(
    session: Session,
    *,
    store: OdooStore,
    website: OdooWebsite | None,
    days: int,
    domain_url: str,
) -> tuple[list[ProductAggregate], dict[str, Any]]:
    _base_url, uid, models = _authenticate_store(store)
    since = _utcnow() - timedelta(days=max(int(days or 7), 1))
    website_currency_code = _website_currency_code(models, store, uid, website)

    order_fields_meta = _safe_fields_get(models, store, uid, "sale.order")
    order_fields = [
        field_name
        for field_name in ("id", "name", "date_order", "state", "currency_id", "amount_total", "website_id", "order_line", "margin", "margin_percent")
        if field_name in order_fields_meta
    ]
    if "id" not in order_fields:
        order_fields.insert(0, "id")

    order_domain: list[Any] = [
        ["state", "in", list(CONFIRMED_STATES)],
        ["date_order", ">=", since.strftime("%Y-%m-%d %H:%M:%S")],
    ]
    if website is not None:
        order_domain.append(["website_id", "=", int(website.website_id)])
    elif store.website_id:
        order_domain.append(["website_id", "=", int(store.website_id)])

    orders = models.execute_kw(
        store.database,
        uid,
        store.api_key,
        "sale.order",
        "search_read",
        [order_domain],
        {"fields": order_fields, "order": "date_order desc", "limit": 5000},
    )
    order_ids = [int(row["id"]) for row in orders if row.get("id")]
    orders_by_id = {int(row["id"]): row for row in orders if row.get("id")}
    metadata = {
        "orders_found": len(order_ids),
        "line_rows_found": 0,
        "window_start": since.isoformat(),
        "window_end": _utcnow().isoformat(),
        "website_currency_code": website_currency_code,
        "pricelist_currency_code": website_currency_code,
    }
    if not order_ids:
        return [], metadata

    line_fields_meta = _safe_fields_get(models, store, uid, "sale.order.line")
    line_fields = [
        field_name
        for field_name in (
            "id",
            "order_id",
            "product_id",
            "product_uom_qty",
            "qty_delivered",
            "price_subtotal",
            "price_total",
            "margin",
            "margin_percent",
            "purchase_price",
            "display_type",
            "is_delivery",
        )
        if field_name in line_fields_meta
    ]
    for required in ("id", "order_id", "product_id"):
        if required not in line_fields:
            line_fields.insert(0, required)

    line_rows: list[dict[str, Any]] = []
    for chunk in _chunked(order_ids):
        domain: list[Any] = [["order_id", "in", chunk]]
        if "display_type" in line_fields_meta:
            domain.append(["display_type", "=", False])
        line_rows.extend(
            models.execute_kw(
                store.database,
                uid,
                store.api_key,
                "sale.order.line",
                "search_read",
                [domain],
                {"fields": line_fields, "limit": 20000},
            )
        )
    metadata["line_rows_found"] = len(line_rows)
    product_ids = {
        product_id
        for product_id in (_many2one_id(row.get("product_id")) for row in line_rows)
        if product_id
    }
    product_rows, template_rows = _read_product_metadata(models, store, uid, product_ids)

    aggregates: dict[str, ProductAggregate] = {}
    for line in line_rows:
        if line.get("display_type") or line.get("is_delivery"):
            continue
        product_id = _many2one_id(line.get("product_id"))
        if not product_id:
            continue
        order_id = _many2one_id(line.get("order_id"))
        order = orders_by_id.get(order_id or 0, {})
        raw_product_name = _many2one_name(line.get("product_id")) or str(product_id)
        if raw_product_name.strip().lower() in {"delivery", "shipping", "shipping charges"}:
            continue
        product_row = product_rows.get(product_id, {})
        template_id = _many2one_id(product_row.get("product_tmpl_id"))
        template_row = template_rows.get(template_id or 0, {})
        code = str(product_row.get("default_code") or template_row.get("default_code") or _extract_product_code(raw_product_name) or product_id)
        name = str(template_row.get("name") or product_row.get("display_name") or product_row.get("name") or _clean_product_name(raw_product_name))
        name = _clean_product_name(name)
        key = _normalize_code(code) or f"product{product_id}"
        brand_name = _metadata_brand(product_row, template_row)
        category_names = _metadata_categories(product_row, template_row)
        public_category_ids = _metadata_public_category_ids(product_row, template_row)
        list_price = _metadata_list_price(product_row, template_row)

        subtotal = _as_float(line.get("price_subtotal") if "price_subtotal" in line else line.get("price_total"))
        quantity = _as_float(line.get("product_uom_qty") if "product_uom_qty" in line else line.get("qty_delivered"))
        line_margin = _float_or_none(line.get("margin"))
        if line_margin is None:
            order_margin = _float_or_none(order.get("margin")) or 0.0
            order_total = _as_float(order.get("amount_total"))
            line_margin = (order_margin * (subtotal / order_total)) if order_total else 0.0
        if subtotal <= 0 and quantity <= 0:
            continue

        currency_code = _many2one_name(order.get("currency_id")).split(" ")[0].upper()
        order_date = _parse_odoo_datetime(order.get("date_order")).date().isoformat()
        aggregate = aggregates.get(key)
        if aggregate is None:
            aggregate = ProductAggregate(
                product_code=str(code),
                product_name=name,
                currency_code=currency_code,
                product_url=_product_url_from_metadata(
                    domain_url=domain_url,
                    product_row=product_row,
                    template_row=template_row,
                ),
                brand_name=brand_name,
                category_names=category_names,
                public_category_ids=public_category_ids,
                list_price=list_price,
                stock_qty=_stock_from_metadata(product_row, template_row),
                published=_published_from_metadata(template_row),
                active=_active_from_metadata(product_row, template_row),
            )
            aggregates[key] = aggregate
        aggregate.product_ids.add(product_id)
        if template_id:
            aggregate.template_ids.add(template_id)
        if order_id:
            aggregate.order_ids.add(order_id)
        aggregate.quantity += quantity
        aggregate.sales_amount += subtotal
        aggregate.margin_amount += float(line_margin or 0)
        if currency_code and not aggregate.currency_code:
            aggregate.currency_code = currency_code
        day = aggregate.daily.setdefault(order_date, {"orders": 0.0, "quantity": 0.0, "sales": 0.0, "margin": 0.0})
        day["quantity"] += quantity
        day["sales"] += subtotal
        day["margin"] += float(line_margin or 0)

    for aggregate in aggregates.values():
        order_dates: dict[str, set[int]] = defaultdict(set)
        for line in line_rows:
            product_id = _many2one_id(line.get("product_id"))
            if product_id not in aggregate.product_ids:
                continue
            order_id = _many2one_id(line.get("order_id"))
            order = orders_by_id.get(order_id or 0, {})
            order_date = _parse_odoo_datetime(order.get("date_order")).date().isoformat()
            if order_id:
                order_dates[order_date].add(order_id)
        for day, order_set in order_dates.items():
            aggregate.daily.setdefault(day, {"orders": 0.0, "quantity": 0.0, "sales": 0.0, "margin": 0.0})["orders"] = float(len(order_set))

    return list(aggregates.values()), metadata


def _accumulate_landing_metrics(metrics: GoogleMetrics, row: dict[str, Any]) -> None:
    metrics.cost += _as_float(row.get("cost"))
    metrics.clicks += _as_int(row.get("clicks"))
    metrics.conversions += _as_float(row.get("conversions"))
    metrics.conversion_value += _as_float(row.get("conversion_value"))
    metrics.landing_pages.append(row)
    if not metrics.best_url or _as_float(row.get("conversions")) > 0:
        metrics.best_url = str(row.get("url") or metrics.best_url)


def _row_matches_product(row_text: str, product: ProductAggregate) -> bool:
    row_norm = _normalize_code(row_text)
    code_norm = _normalize_code(product.product_code)
    if code_norm and len(code_norm) >= 4 and code_norm in row_norm:
        return True
    product_tokens = _tokens(product.product_name)
    if len(product_tokens) < 2:
        return False
    found = sum(1 for token in product_tokens[:8] if token in str(row_text or "").lower())
    return found >= 2


def _match_google_metrics(
    *,
    landing_rows: list[dict[str, Any]],
    search_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    product: ProductAggregate,
    domain_url: str,
) -> GoogleMetrics:
    metrics = GoogleMetrics()
    for row in landing_rows:
        url = str(row.get("url") or "")
        if not _same_host(url, domain_url):
            continue
        if _row_matches_product(url, product):
            _accumulate_landing_metrics(metrics, row)
    for row in search_rows:
        term = str(row.get("search_term") or "")
        if _row_matches_product(term, product):
            metrics.search_terms.append(row)
    for row in keyword_rows:
        keyword = str(row.get("keyword") or "")
        if _row_matches_product(keyword, product):
            metrics.keywords.append(row)
    metrics.search_terms.sort(key=lambda item: (_as_float(item.get("conversions")), _as_float(item.get("conversion_value")), -_as_float(item.get("cost"))), reverse=True)
    metrics.keywords.sort(key=lambda item: (_as_float(item.get("conversions")), _as_float(item.get("conversion_value")), -_as_float(item.get("cost"))), reverse=True)
    return metrics


def _label_product(product: ProductAggregate, metrics: GoogleMetrics, product_url: str) -> tuple[str, str]:
    margin_percent = product.margin_amount / product.sales_amount if product.sales_amount else 0.0
    stock_note = " Odoo stock reports zero, but recent confirmed sales exist, so stock is kept as evidence instead of blocking the page." if product.stock_qty is not None and product.stock_qty <= 0 and product.sales_amount > 0 else ""
    if _is_broad_or_excluded_url(product_url):
        return "exclude", "Broad, category, checkout, or homepage URL is not safe for DSA/PMax targeting."
    if product.active is False:
        return "exclude", "Product is inactive in Odoo."
    if product.published is False:
        return "exclude", "Product is not published on the website."
    if product.stock_qty is not None and product.stock_qty <= 0 and product.sales_amount <= 0:
        return "exclude", "Product appears out of stock in Odoo."
    if product.sales_amount <= 0 or product.margin_amount <= 0:
        return "exclude", "No positive Odoo sales margin was found for this page."
    if margin_percent < 0.10:
        return "exclude", "Odoo margin is below 10%, so paid traffic is unsafe."
    if product.margin_amount >= 35 and margin_percent >= 0.18 and (len(product.order_ids) >= 2 or product.quantity >= 2):
        return "winner", "High-margin Odoo product with repeat sales." + stock_note
    if metrics.conversions > 0 and margin_percent >= 0.12:
        return "winner", "Odoo margin is positive and cached Google data shows conversions." + stock_note
    return "watch", "Positive Odoo sales exist, but more volume or stronger margin is needed before aggressive spend." + stock_note


def _google_only_exclusions(
    *,
    account: GoogleAdsAccount,
    store: OdooStore,
    website: OdooWebsite | None,
    domain_url: str,
    landing_rows: list[dict[str, Any]],
    matched_urls: set[str],
    days: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in landing_rows:
        url = str(row.get("url") or "")
        if not url or url in matched_urls or not _same_host(url, domain_url):
            continue
        cost = _as_float(row.get("cost"))
        conversions = _as_float(row.get("conversions"))
        if not _is_broad_or_excluded_url(url) and not (cost >= 5 and conversions <= 0):
            continue
        rows.append(
            {
                "store_id": store.id,
                "website_id": int(website.website_id if website else 0),
                "account_id": account.id,
                "website_name": website.name if website else "All websites",
                "domain": domain_url,
                "product_code": "url:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
                "product_name": _title_from_url(url),
                "product_url": url,
                "currency_code": account.currency_code or "",
                "order_count": 0,
                "quantity": 0.0,
                "sales_amount": 0.0,
                "margin_amount": 0.0,
                "margin_percent": None,
                "google_cost": cost,
                "google_clicks": _as_int(row.get("clicks")),
                "google_conversions": conversions,
                "google_conversion_value": _as_float(row.get("conversion_value")),
                "zero_conversion_cost": cost if conversions <= 0 else 0.0,
                "label": "exclude",
                "reason": "Cached Google landing page has broad/unsafe targeting or paid clicks with zero conversions.",
                "source_json": {
                    "source": "google_landing_page_exclusion",
                    "days": days,
                    "landing_page": row,
                },
            }
        )
    return rows


def _fallback_rows(
    *,
    session: Session,
    account: GoogleAdsAccount,
    store: OdooStore,
    website: OdooWebsite | None,
    domain_url: str,
    landing_rows: list[dict[str, Any]],
    days: int,
    restricted_terms: list[str],
) -> list[dict[str, Any]]:
    website_id = int(website.website_id if website else 0)
    rows: list[dict[str, Any]] = []
    global_winners = session.scalars(
        select(OdooProductPageSignal)
        .where(
            OdooProductPageSignal.label.in_(["winner", "watch"]),
            OdooProductPageSignal.product_url != "",
        )
        .order_by(OdooProductPageSignal.label, OdooProductPageSignal.margin_amount.desc())
        .limit(10)
    ).all()
    for signal in global_winners:
        blocked_term = restricted_title_match(signal.product_name, restricted_terms)
        if blocked_term:
            continue
        rows.append(
            {
                "store_id": store.id,
                "website_id": website_id,
                "account_id": account.id,
                "website_name": website.name if website else "All websites",
                "domain": domain_url,
                "product_code": _limit_text(signal.product_code or f"fallback:{signal.id}", 160),
                "product_name": _limit_text(signal.product_name, 500),
                "product_url": _limit_text(signal.product_url, 1000),
                "currency_code": signal.currency_code or "",
                "order_count": 0,
                "quantity": 0.0,
                "sales_amount": 0.0,
                "margin_amount": 0.0,
                "margin_percent": None,
                "google_cost": 0.0,
                "google_clicks": 0,
                "google_conversions": 0.0,
                "google_conversion_value": 0.0,
                "zero_conversion_cost": 0.0,
                "label": "fallback",
                "reason": "No local Odoo sales found, so this uses proven products from other mapped websites.",
                "source_json": {
                    "source": "global_odoo_product_signal_fallback",
                    "days": days,
                    "fallback_from_signal_id": signal.id,
                    "fallback_from_account_id": signal.account_id,
                    "fallback_from_website_id": signal.website_id,
                },
            }
        )
    if rows:
        return rows

    for row in landing_rows:
        url = str(row.get("url") or "")
        if not url or not _same_host(url, domain_url) or _is_broad_or_excluded_url(url):
            continue
        if _as_float(row.get("conversions")) <= 0:
            continue
        rows.append(
            {
                "store_id": store.id,
                "website_id": website_id,
                "account_id": account.id,
                "website_name": website.name if website else "All websites",
                "domain": domain_url,
                "product_code": "fallback:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
                "product_name": _title_from_url(url),
                "product_url": _limit_text(url, 1000),
                "currency_code": account.currency_code or "",
                "order_count": 0,
                "quantity": 0.0,
                "sales_amount": 0.0,
                "margin_amount": 0.0,
                "margin_percent": None,
                "google_cost": _as_float(row.get("cost")),
                "google_clicks": _as_int(row.get("clicks")),
                "google_conversions": _as_float(row.get("conversions")),
                "google_conversion_value": _as_float(row.get("conversion_value")),
                "zero_conversion_cost": 0.0,
                "label": "fallback",
                "reason": "No local Odoo sales found, so this uses the account's cached converting landing pages.",
                "source_json": {
                    "source": "google_converting_landing_page_fallback",
                    "days": days,
                    "landing_page": row,
                },
            }
        )
    return rows[:10]


def _upsert_signal(session: Session, values: dict[str, Any]) -> None:
    product_url = _limit_text(values.get("product_url") or "", 1000)
    values = {
        **values,
        "product_code": _limit_text(values.get("product_code") or "unknown", 160),
        "product_name": _limit_text(values.get("product_name") or "Unknown product", 500),
        "product_url": product_url,
        "product_url_hash": hashlib.md5(product_url.encode("utf-8")).hexdigest(),
        "synced_at": _utcnow(),
    }
    stmt = insert(OdooProductPageSignal).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            OdooProductPageSignal.store_id,
            OdooProductPageSignal.website_id,
            OdooProductPageSignal.account_id,
            OdooProductPageSignal.product_code,
            OdooProductPageSignal.product_url_hash,
        ],
        set_={
            "website_name": stmt.excluded.website_name,
            "domain": stmt.excluded.domain,
            "product_name": stmt.excluded.product_name,
            "product_url": stmt.excluded.product_url,
            "currency_code": stmt.excluded.currency_code,
            "order_count": stmt.excluded.order_count,
            "quantity": stmt.excluded.quantity,
            "sales_amount": stmt.excluded.sales_amount,
            "margin_amount": stmt.excluded.margin_amount,
            "margin_percent": stmt.excluded.margin_percent,
            "google_cost": stmt.excluded.google_cost,
            "google_clicks": stmt.excluded.google_clicks,
            "google_conversions": stmt.excluded.google_conversions,
            "google_conversion_value": stmt.excluded.google_conversion_value,
            "zero_conversion_cost": stmt.excluded.zero_conversion_cost,
            "label": stmt.excluded.label,
            "reason": stmt.excluded.reason,
            "source_json": stmt.excluded.source_json,
            "synced_at": stmt.excluded.synced_at,
        },
    )
    session.execute(stmt)


def sync_mapping_product_page_feed(
    session: Session,
    mapping: OdooStoreGoogleAdsMapping,
    *,
    days: int = 7,
) -> dict[str, Any]:
    store = mapping.store
    account = mapping.account
    website = None
    if mapping.website_id:
        website = session.scalar(
            select(OdooWebsite).where(
                OdooWebsite.store_id == store.id,
                OdooWebsite.website_id == mapping.website_id,
                OdooWebsite.is_active.is_(True),
            )
        )
    domain_url = _normalize_domain_url(website.domain if website else "", store.base_url)
    landing_rows = _latest_snapshot_rows(session, account, GOOGLE_LANDING_PAGE_DATASET)
    search_rows = _latest_snapshot_rows(session, account, GOOGLE_SEARCH_TERM_DATASET)
    keyword_rows = _latest_snapshot_rows(session, account, GOOGLE_KEYWORD_DATASET)
    restricted_terms = get_restricted_title_terms_sync(session)
    products, odoo_meta = _fetch_odoo_product_totals(
        session,
        store=store,
        website=website,
        days=days,
        domain_url=domain_url,
    )
    website_currency_code = str(odoo_meta.get("website_currency_code") or "")
    rate_snapshot = get_latest_rate_snapshot_sync(session)
    rate_payload = snapshot_payload(rate_snapshot)
    rates = rate_payload.get("rates") or {}
    matched_urls: set[str] = set()
    counters = {"winner": 0, "watch": 0, "exclude": 0, "fallback": 0}
    upserted = 0

    for product in products:
        metrics = _match_google_metrics(
            landing_rows=landing_rows,
            search_rows=search_rows,
            keyword_rows=keyword_rows,
            product=product,
            domain_url=domain_url,
        )
        product_url = product.product_url or metrics.best_url or _fallback_search_url(domain_url, product)
        if product_url:
            matched_urls.add(product_url)
        for landing_page in metrics.landing_pages:
            matched_url = str(landing_page.get("url") or "")
            if matched_url:
                matched_urls.add(matched_url)
        blocked_term = restricted_title_match(product.product_name, restricted_terms)
        if blocked_term:
            label = "exclude"
            reason = f"Odoo product title contains restricted page-feed term: {blocked_term}."
        else:
            label, reason = _label_product(product, metrics, product_url)
        margin_percent = product.margin_amount / product.sales_amount if product.sales_amount else None
        margin_account_currency = convert_amount(
            product.margin_amount,
            product.currency_code,
            account.currency_code or product.currency_code,
            rates,
        )
        search_terms = [
            str(row.get("search_term") or "")
            for row in metrics.search_terms[:12]
            if row.get("search_term")
        ]
        keywords = [
            str(row.get("keyword") or "")
            for row in metrics.keywords[:12]
            if row.get("keyword")
        ]
        _upsert_signal(
            session,
            {
                "store_id": store.id,
                "website_id": int(website.website_id if website else mapping.website_id or 0),
                "account_id": account.id,
                "website_name": website.name if website else "All websites",
                "domain": domain_url,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "product_url": product_url,
                "currency_code": product.currency_code or "",
                "order_count": len(product.order_ids),
                "quantity": product.quantity,
                "sales_amount": product.sales_amount,
                "margin_amount": product.margin_amount,
                "margin_percent": margin_percent,
                "google_cost": metrics.cost,
                "google_clicks": metrics.clicks,
                "google_conversions": metrics.conversions,
                "google_conversion_value": metrics.conversion_value,
                "zero_conversion_cost": metrics.cost if metrics.conversions <= 0 else 0.0,
                "label": label,
                "reason": reason,
                "source_json": {
                    "source": "odoo_sale_order_line",
                    "days": days,
                    "odoo": odoo_meta,
                    "daily": product.daily,
                    "product_ids": sorted(product.product_ids),
                    "template_ids": sorted(product.template_ids),
                    "brand": product.brand_name,
                    "brand_name": product.brand_name,
                    "categories": product.category_names,
                    "category_names": product.category_names,
                    "public_category_ids": product.public_category_ids,
                    "list_price": product.list_price,
                    "website_currency_code": website_currency_code,
                    "pricelist_currency_code": website_currency_code,
                    "stock_qty": product.stock_qty,
                    "published": product.published,
                    "active": product.active,
                    "matched_landing_pages": metrics.landing_pages[:10],
                    "matched_search_terms": metrics.search_terms[:20],
                    "matched_keywords": metrics.keywords[:20],
                    "exact_terms": search_terms or keywords,
                    "margin_account_currency": margin_account_currency,
                    "account_currency": account.currency_code or "",
                    "normal_spend_cap": (margin_account_currency * 0.20) if margin_account_currency is not None else None,
                    "priority_spend_cap": (margin_account_currency * 0.30) if margin_account_currency is not None else None,
                    "rate_snapshot": {
                        "rate_date": rate_payload.get("rate_date"),
                        "source": rate_payload.get("source"),
                        "error": rate_payload.get("error"),
                    },
                },
            },
        )
        counters[label] += 1
        upserted += 1
        if upserted % 100 == 0:
            session.commit()

    if not products:
        for row in _fallback_rows(
            session=session,
            account=account,
            store=store,
            website=website,
            domain_url=domain_url,
            landing_rows=landing_rows,
            days=days,
            restricted_terms=restricted_terms,
        ):
            _upsert_signal(session, row)
            counters["fallback"] += 1
            upserted += 1
            if upserted % 100 == 0:
                session.commit()

    for row in _google_only_exclusions(
        account=account,
        store=store,
        website=website,
        domain_url=domain_url,
        landing_rows=landing_rows,
        matched_urls=matched_urls,
        days=days,
    ):
        _upsert_signal(session, row)
        counters["exclude"] += 1
        upserted += 1
        if upserted % 100 == 0:
            session.commit()

    session.commit()
    return {
        "mapping_id": mapping.id,
        "store_id": store.id,
        "website_id": int(website.website_id if website else mapping.website_id or 0),
        "account_id": account.id,
        "domain": domain_url,
        "days": days,
        "odoo": odoo_meta,
        "counts": counters,
    }


def sync_mapped_product_page_feeds(
    session: Session,
    *,
    days: int = 7,
    store_ids: list[int] | None = None,
    account_ids: list[int] | None = None,
    website_ids: list[int] | None = None,
) -> dict[str, Any]:
    query = select(OdooStoreGoogleAdsMapping).where(OdooStoreGoogleAdsMapping.is_active.is_(True))
    if store_ids:
        query = query.where(OdooStoreGoogleAdsMapping.store_id.in_([int(item) for item in store_ids]))
    if account_ids:
        query = query.where(OdooStoreGoogleAdsMapping.account_id.in_([int(item) for item in account_ids]))
    if website_ids:
        query = query.where(OdooStoreGoogleAdsMapping.website_id.in_([int(item) for item in website_ids]))
    mappings = session.scalars(query.order_by(OdooStoreGoogleAdsMapping.store_id, OdooStoreGoogleAdsMapping.website_id)).all()
    results = []
    errors = []
    for mapping in mappings:
        try:
            results.append(sync_mapping_product_page_feed(session, mapping, days=days))
        except Exception as exc:  # noqa: BLE001 - feed generation should move mapping by mapping.
            session.rollback()
            errors.append({"mapping_id": mapping.id, "error": str(exc)})
    return {
        "days": days,
        "mappings": len(mappings),
        "results": results,
        "errors": errors,
    }


def sync_store_product_page_feeds(session: Session, store: OdooStore, *, days: int = 7) -> dict[str, Any]:
    return sync_mapped_product_page_feeds(session, days=days, store_ids=[store.id])


def _signal_to_context(signal: OdooProductPageSignal) -> dict[str, Any]:
    source = signal.source_json or {}
    return {
        "id": signal.id,
        "label": signal.label,
        "product_code": signal.product_code,
        "product_name": signal.product_name,
        "product_url": signal.product_url,
        "currency_code": signal.currency_code,
        "order_count": signal.order_count,
        "quantity": signal.quantity,
        "sales_amount": signal.sales_amount,
        "margin_amount": signal.margin_amount,
        "margin_percent": signal.margin_percent,
        "google_cost": signal.google_cost,
        "google_conversions": signal.google_conversions,
        "google_conversion_value": signal.google_conversion_value,
        "zero_conversion_cost": signal.zero_conversion_cost,
        "reason": signal.reason,
        "exact_terms": source.get("exact_terms") or [],
        "normal_spend_cap": source.get("normal_spend_cap"),
        "priority_spend_cap": source.get("priority_spend_cap"),
    }


def product_feed_context_for_account(
    session: Session,
    account_id: int,
    *,
    limit: int = 12,
    campaign_mode: str = "best",
) -> dict[str, Any]:
    campaign_mode = str(campaign_mode or "best").strip().lower()
    if campaign_mode not in {"best", "good", "new", "mixed"}:
        campaign_mode = "best"
    feed_kind = normalize_feed_kind(campaign_mode)

    def fetch(label: str, *, by_zero_cost: bool = False) -> list[OdooProductPageSignal]:
        order_by = (
            OdooProductPageSignal.zero_conversion_cost.desc(),
            OdooProductPageSignal.google_cost.desc(),
        ) if by_zero_cost else (
            OdooProductPageSignal.margin_amount.desc(),
            OdooProductPageSignal.google_conversion_value.desc(),
        )
        return session.scalars(
            select(OdooProductPageSignal)
            .where(
                OdooProductPageSignal.account_id == account_id,
                OdooProductPageSignal.label == label,
            )
            .order_by(*order_by)
            .limit(limit)
        ).all()

    winner_signals = fetch("winner")
    fallback_signals = fetch("fallback")
    watch_signals = fetch("watch")
    exclusion_signals = fetch("exclude", by_zero_cost=True)
    if not any([winner_signals, fallback_signals, watch_signals]):
        winner_signals = session.scalars(
            select(OdooProductPageSignal)
            .where(OdooProductPageSignal.label.in_(["winner", "watch"]))
            .order_by(OdooProductPageSignal.margin_amount.desc())
            .limit(limit)
        ).all()

    winners = [_signal_to_context(signal) for signal in winner_signals]
    fallback = [_signal_to_context(signal) for signal in fallback_signals]
    watch = [_signal_to_context(signal) for signal in watch_signals]
    exclusions = [_signal_to_context(signal) for signal in exclusion_signals]
    if campaign_mode == "best":
        active_targets = winners or fallback or watch
        mode_label = "Best performing products"
    elif campaign_mode == "good":
        active_targets = (watch or winners or fallback)[:limit]
        mode_label = "Other good/watch products"
    elif campaign_mode == "new":
        active_targets = (fallback or watch or winners)[:limit]
        mode_label = "New or cross-site learned products"
    else:
        active_targets = (winners[: max(1, limit // 2)] + watch[: max(1, limit // 3)] + fallback[: max(1, limit // 4)])[:limit]
        mode_label = "Mixed winner/watch/new products"
    source_terms: list[str] = []
    clusters: list[dict[str, Any]] = []
    for item in active_targets[:limit]:
        terms = [str(term) for term in (item.get("exact_terms") or []) if str(term).strip()]
        source_terms.extend(terms)
        if terms:
            clusters.append(
                {
                    "ad_group_name": _limit_text(item["product_name"], 80),
                    "product_url": item["product_url"],
                    "product_code": item["product_code"],
                    "exact_terms": terms[:10],
                }
            )
    source_terms = list(dict.fromkeys(source_terms))[:30]
    hosted_page_feed = {}
    publication = session.scalar(
        select(GoogleAdsPageFeedPublication)
        .where(
            GoogleAdsPageFeedPublication.account_id == account_id,
            GoogleAdsPageFeedPublication.feed_kind == feed_kind,
            GoogleAdsPageFeedPublication.status == "feed_url_ready",
        )
        .order_by(GoogleAdsPageFeedPublication.updated_at.desc(), GoogleAdsPageFeedPublication.id.desc())
        .limit(1)
    )
    if publication is not None:
        last_publish = publication.last_publish_json if isinstance(publication.last_publish_json, dict) else {}
        hosted_page_feed = {
            "id": publication.id,
            "feed_kind": feed_kind,
            "feed_kind_label": feed_kind_label(feed_kind),
            "feed_name": public_feed_name(publication),
            "filename": public_feed_filename(publication),
            "feed_url": str(last_publish.get("feed_url") or ""),
            "selected_urls": int(last_publish.get("selected_urls") or 0),
            "updated_at": publication.updated_at.isoformat() if publication.updated_at else None,
        }
    return {
        "account_id": account_id,
        "campaign_mode": campaign_mode,
        "campaign_mode_label": mode_label,
        "hosted_page_feed": hosted_page_feed,
        "has_local_sales": bool(winners or watch),
        "using_fallback": not bool(winners or watch) and bool(fallback),
        "winners": winners,
        "fallback": fallback,
        "watch": watch,
        "exclusions": exclusions,
        "page_feed_targets": [item["product_url"] for item in active_targets if item.get("product_url")][:limit],
        "negative_page_targets": [item["product_url"] for item in exclusions if item.get("product_url")][:limit],
        "keyword_clusters": clusters,
        "source_terms": source_terms,
        "generated_at": _utcnow().isoformat(),
    }


def recent_product_feed_rows(session: Session, *, limit: int = 30) -> list[OdooProductPageSignal]:
    return session.scalars(
        select(OdooProductPageSignal)
        .order_by(
            OdooProductPageSignal.synced_at.desc(),
            OdooProductPageSignal.label,
            OdooProductPageSignal.margin_amount.desc(),
        )
        .limit(limit)
    ).all()


def product_feed_summary(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            OdooProductPageSignal.label,
            func.count(OdooProductPageSignal.id),
            func.max(OdooProductPageSignal.synced_at),
        ).group_by(OdooProductPageSignal.label)
    ).all()
    return [
        {
            "label": label,
            "count": int(count or 0),
            "synced_at": synced_at,
        }
        for label, count, synced_at in sorted(rows, key=lambda item: LABEL_PRIORITY.get(str(item[0]), 9))
    ]
