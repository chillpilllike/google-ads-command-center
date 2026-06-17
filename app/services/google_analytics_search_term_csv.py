from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Iterable

from app.models import GoogleAdsAccount, GoogleAnalyticsSearchTermCandidate


GA4_SEARCH_TERM_CSV_COLUMNS = [
    "Search term",
    "Normalized search term",
    "Keyword",
    "Campaign",
    "Quality",
    "Review status",
    "Sessions",
    "Engaged sessions",
    "Purchases",
    "Revenue",
    "Score",
    "Source datasets",
    "Source scopes",
    "First seen",
    "Last seen",
    "Last pulled",
]


def _joined(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(str(item or "").strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "").strip()


def _number(value: object) -> object:
    return value if value is not None else 0


def ga4_search_term_csv_row(candidate: GoogleAnalyticsSearchTermCandidate) -> list[object]:
    return [
        candidate.search_term or "",
        candidate.normalized_search_term or "",
        candidate.keyword or "",
        candidate.campaign_name or "",
        candidate.quality_label or "",
        candidate.review_status or "",
        _number(candidate.sessions),
        _number(candidate.engaged_sessions),
        _number(candidate.purchases),
        _number(candidate.revenue),
        _number(candidate.score),
        _joined(candidate.source_dataset_keys),
        _joined(candidate.source_scope_keys),
        _timestamp(candidate.first_seen_at),
        _timestamp(candidate.last_seen_at),
        _timestamp(candidate.last_pulled_at),
    ]


def ga4_search_terms_csv_chunk(
    candidates: Iterable[GoogleAnalyticsSearchTermCandidate],
    *,
    include_header: bool = False,
) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if include_header:
        writer.writerow(GA4_SEARCH_TERM_CSV_COLUMNS)
    for candidate in candidates:
        writer.writerow(ga4_search_term_csv_row(candidate))
    return output.getvalue()


def ga4_search_terms_csv_filename(account: GoogleAdsAccount, *, selected: bool = False) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", str(account.name or "").lower()).strip("-")
    customer_id = re.sub(r"\D+", "", str(account.customer_id or ""))
    slug_parts = [part for part in [name, customer_id] if part]
    slug = "-".join(slug_parts) or f"account-{account.id or 0}"
    suffix = "-selected" if selected else ""
    return f"ga4-search-terms-{slug}{suffix}.csv"
