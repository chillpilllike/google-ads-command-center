from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsConnection, GoogleAdsDataSnapshot


SCHEMA_VERSION = 1
DATASET_CAMPAIGN_DAILY = "campaign_daily"
DATASET_CONVERSION_GOALS = "conversion_goals"
DATASET_HOURLY_DELIVERY = "hourly_delivery"
DATASET_ACCOUNT_PROFILE = "account_profile"
DATASET_RECOMMENDATIONS = "recommendations"
DATASET_SEARCH_TERMS_WASTE = "search_terms_waste"
DATASET_CAMPAIGN_INSIGHTS = "campaign_insights"
DATASET_SEARCH_TERMS = "search_terms"
DATASET_SEARCH_TERM_INSIGHTS = "search_term_insights"
DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS = "ai_max_search_term_combinations"
DATASET_LANDING_PAGES = "landing_pages"
DATASET_TIME_SEGMENTS = "time_segments"
DATASET_GEO_SEGMENTS = "geo_segments"
DATASET_AUCTION_INSIGHTS_PROXY = "auction_insights_proxy"
DATASET_AUDIENCE_INSIGHTS = "audience_insights"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query or "")).strip()


def query_hash(query: str) -> str:
    if not query:
        return ""
    return hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()[:32]


def account_target_key(account: GoogleAdsAccount) -> str:
    return f"account:{account.id}:{account.customer_id}"


def connection_target_key(connection: GoogleAdsConnection) -> str:
    return f"connection:{connection.id}"


def target_key_for(
    *,
    account: GoogleAdsAccount | None = None,
    connection: GoogleAdsConnection | None = None,
    explicit_target_key: str = "",
) -> str:
    if explicit_target_key:
        return explicit_target_key
    if account is not None:
        return account_target_key(account)
    if connection is not None:
        return connection_target_key(connection)
    return "global"


def row_count_for(payload: dict[str, Any]) -> int:
    rows = payload.get("rows")
    if isinstance(rows, list):
        return len(rows)
    if isinstance(rows, dict):
        return len(rows)
    return 1 if payload else 0


def get_fresh_snapshot(
    session: Session,
    *,
    dataset_key: str,
    scope_key: str = "",
    query: str = "",
    query_hash_value: str = "",
    schema_version: int = SCHEMA_VERSION,
    account: GoogleAdsAccount | None = None,
    connection: GoogleAdsConnection | None = None,
    explicit_target_key: str = "",
    now: Optional[datetime] = None,
) -> GoogleAdsDataSnapshot | None:
    now = now or utcnow()
    target_key = target_key_for(
        account=account,
        connection=connection,
        explicit_target_key=explicit_target_key,
    )
    q_hash = query_hash_value or query_hash(query)
    return session.scalar(
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.target_key == target_key,
            GoogleAdsDataSnapshot.dataset_key == dataset_key,
            GoogleAdsDataSnapshot.scope_key == scope_key,
            GoogleAdsDataSnapshot.schema_version == schema_version,
            GoogleAdsDataSnapshot.query_hash == q_hash,
            or_(GoogleAdsDataSnapshot.expires_at.is_(None), GoogleAdsDataSnapshot.expires_at > now),
        )
        .order_by(GoogleAdsDataSnapshot.fetched_at.desc(), GoogleAdsDataSnapshot.id.desc())
        .limit(1)
    )


def upsert_snapshot(
    session: Session,
    *,
    dataset_key: str,
    payload: dict[str, Any],
    scope_key: str = "",
    query: str = "",
    query_hash_value: str = "",
    schema_version: int = SCHEMA_VERSION,
    account: GoogleAdsAccount | None = None,
    connection: GoogleAdsConnection | None = None,
    explicit_target_key: str = "",
    expires_at: Optional[datetime] = None,
    source_job_id: Optional[int] = None,
    row_count: Optional[int] = None,
) -> None:
    target_key = target_key_for(
        account=account,
        connection=connection,
        explicit_target_key=explicit_target_key,
    )
    q_hash = query_hash_value or query_hash(query)
    stmt = insert(GoogleAdsDataSnapshot).values(
        target_key=target_key,
        account_id=account.id if account is not None else None,
        connection_id=connection.id if connection is not None else None,
        source_job_id=source_job_id,
        dataset_key=dataset_key,
        scope_key=scope_key,
        query_hash=q_hash,
        schema_version=schema_version,
        row_count=row_count if row_count is not None else row_count_for(payload),
        payload_json=payload,
        fetched_at=utcnow(),
        expires_at=expires_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsDataSnapshot.target_key,
            GoogleAdsDataSnapshot.dataset_key,
            GoogleAdsDataSnapshot.scope_key,
            GoogleAdsDataSnapshot.schema_version,
            GoogleAdsDataSnapshot.query_hash,
        ],
        set_={
            "account_id": stmt.excluded.account_id,
            "connection_id": stmt.excluded.connection_id,
            "source_job_id": stmt.excluded.source_job_id,
            "row_count": stmt.excluded.row_count,
            "payload_json": stmt.excluded.payload_json,
            "fetched_at": stmt.excluded.fetched_at,
            "expires_at": stmt.excluded.expires_at,
        },
    )
    session.execute(stmt)
