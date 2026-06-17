from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.app_settings import RESTRICTED_PAGE_FEED_TITLE_TERMS_DEFAULT
from app.models import AppSetting


SETTING_KEY = "page_feed.restricted_title_terms"


def normalize_restricted_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_restricted_terms(raw: Any) -> list[str]:
    terms: list[str] = []
    for line in str(raw or "").splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        terms.append(term)
        if re.search(r"\s+and\s+", term, flags=re.IGNORECASE):
            for part in re.split(r"\s+and\s+", term, flags=re.IGNORECASE):
                part = part.strip()
                if part:
                    terms.append(part)
    normalized: list[str] = []
    for term in terms:
        item = normalize_restricted_text(term)
        if item:
            normalized.append(item)
    return list(dict.fromkeys(normalized))


def restricted_term_count(raw: Any) -> int:
    return len(parse_restricted_terms(raw))


def restricted_title_match(title: Any, terms: list[str]) -> str:
    normalized_title = normalize_restricted_text(title)
    if not normalized_title:
        return ""
    searchable = f" {normalized_title} "
    for term in terms:
        normalized_term = normalize_restricted_text(term)
        if normalized_term and f" {normalized_term} " in searchable:
            return normalized_term
    return ""


def restricted_terms_from_value(value: Any) -> list[str]:
    return parse_restricted_terms(value or RESTRICTED_PAGE_FEED_TITLE_TERMS_DEFAULT)


def get_restricted_title_terms_sync(session: Session) -> list[str]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == SETTING_KEY))
    value = row.value if row is not None else RESTRICTED_PAGE_FEED_TITLE_TERMS_DEFAULT
    return restricted_terms_from_value(value)


async def get_restricted_title_terms(session: AsyncSession) -> list[str]:
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
    return restricted_terms_from_value(value)
