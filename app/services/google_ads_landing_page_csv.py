from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Iterable

from app.models import GoogleAdsAccount, GoogleAdsLandingPageCandidate


LANDING_PAGE_BANK_CSV_COLUMNS = [
    "URL",
    "Normalized URL",
    "Quality",
    "Review status",
    "Impressions",
    "Clicks",
    "Cost",
    "Conversions",
    "Conversion value",
    "All conversions",
    "All conversions value",
    "Score",
    "Campaigns",
    "Channels",
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


def landing_page_candidate_csv_row(candidate: GoogleAdsLandingPageCandidate) -> list[object]:
    return [
        candidate.url or "",
        candidate.normalized_url or "",
        candidate.quality_label or "",
        candidate.review_status or "",
        _number(candidate.impressions),
        _number(candidate.clicks),
        _number(candidate.cost),
        _number(candidate.conversions),
        _number(candidate.conversion_value),
        _number(candidate.all_conversions),
        _number(candidate.all_conversions_value),
        _number(candidate.score),
        _joined(candidate.campaign_names),
        _joined(candidate.channel_types),
        _joined(candidate.source_dataset_keys),
        _joined(candidate.source_scope_keys),
        _timestamp(candidate.first_seen_at),
        _timestamp(candidate.last_seen_at),
        _timestamp(candidate.last_pulled_at),
    ]


def landing_page_candidates_csv_chunk(
    candidates: Iterable[GoogleAdsLandingPageCandidate],
    *,
    include_header: bool = False,
) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    if include_header:
        writer.writerow(LANDING_PAGE_BANK_CSV_COLUMNS)
    for candidate in candidates:
        writer.writerow(landing_page_candidate_csv_row(candidate))
    return output.getvalue()


def landing_page_bank_csv_filename(account: GoogleAdsAccount, *, selected: bool = False) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", str(account.name or "").lower()).strip("-")
    customer_id = re.sub(r"\D+", "", str(account.customer_id or ""))
    slug_parts = [part for part in [name, customer_id] if part]
    slug = "-".join(slug_parts) or f"account-{account.id or 0}"
    suffix = "-selected" if selected else ""
    return f"landing-page-bank-{slug}{suffix}.csv"
