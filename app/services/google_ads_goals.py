from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsConversionGoalSnapshot
from app.services.google_ads_sync import build_client, enum_name
from app.services.google_ads_snapshot_store import (
    DATASET_CONVERSION_GOALS,
    get_fresh_snapshot,
    upsert_snapshot,
)


GOAL_SNAPSHOT_TTL_HOURS = 24


def customer_goal_query() -> str:
    return """
        SELECT
          customer_conversion_goal.resource_name,
          customer_conversion_goal.category,
          customer_conversion_goal.origin,
          customer_conversion_goal.biddable
        FROM customer_conversion_goal
    """


def campaign_goal_query() -> str:
    return """
        SELECT
          campaign_conversion_goal.resource_name,
          campaign_conversion_goal.category,
          campaign_conversion_goal.origin,
          campaign_conversion_goal.biddable,
          campaign.id,
          campaign.name
        FROM campaign_conversion_goal
        WHERE campaign.status != 'REMOVED'
    """


def campaign_goal_config_query() -> str:
    return """
        SELECT
          conversion_goal_campaign_config.custom_conversion_goal,
          conversion_goal_campaign_config.goal_config_level,
          campaign.id,
          campaign.name
        FROM conversion_goal_campaign_config
        WHERE campaign.status != 'REMOVED'
    """


def conversion_goal_query_bundle() -> str:
    return "\n\n".join([customer_goal_query(), campaign_goal_query(), campaign_goal_config_query()])


def _upsert_goal(
    session: Session,
    *,
    account_id: int,
    level: str,
    campaign_id: int = 0,
    campaign_name: Optional[str] = None,
    category: str,
    origin: str,
    biddable: Optional[bool] = None,
    goal_config_level: Optional[str] = None,
    custom_conversion_goal: Optional[str] = None,
) -> None:
    stmt = insert(GoogleAdsConversionGoalSnapshot).values(
        account_id=account_id,
        level=level,
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        category=category,
        origin=origin,
        biddable=biddable,
        goal_config_level=goal_config_level,
        custom_conversion_goal=custom_conversion_goal,
        synced_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsConversionGoalSnapshot.account_id,
            GoogleAdsConversionGoalSnapshot.level,
            GoogleAdsConversionGoalSnapshot.campaign_id,
            GoogleAdsConversionGoalSnapshot.category,
            GoogleAdsConversionGoalSnapshot.origin,
        ],
        set_={
            "campaign_name": stmt.excluded.campaign_name,
            "biddable": stmt.excluded.biddable,
            "goal_config_level": stmt.excluded.goal_config_level,
            "custom_conversion_goal": stmt.excluded.custom_conversion_goal,
            "synced_at": stmt.excluded.synced_at,
        },
    )
    session.execute(stmt)


def fetch_conversion_goal_rows_from_google(session: Session, account: GoogleAdsAccount, client=None) -> list[dict]:
    values = get_sync_setting_map(session)
    client = client or build_client(values, account.manager_customer_id, account.connection)
    ga_service = client.get_service("GoogleAdsService")
    rows: list[dict] = []

    for batch in ga_service.search_stream(customer_id=account.customer_id, query=customer_goal_query()):
        for row in batch.results:
            goal = row.customer_conversion_goal
            rows.append(
                {
                    "level": "customer",
                    "campaign_id": 0,
                    "campaign_name": None,
                    "resource_name": goal.resource_name,
                    "category": enum_name(goal.category),
                    "origin": enum_name(goal.origin),
                    "biddable": bool(goal.biddable),
                    "goal_config_level": None,
                    "custom_conversion_goal": None,
                }
            )

    for batch in ga_service.search_stream(customer_id=account.customer_id, query=campaign_goal_query()):
        for row in batch.results:
            goal = row.campaign_conversion_goal
            rows.append(
                {
                    "level": "campaign",
                    "campaign_id": int(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "resource_name": goal.resource_name,
                    "category": enum_name(goal.category),
                    "origin": enum_name(goal.origin),
                    "biddable": bool(goal.biddable),
                    "goal_config_level": None,
                    "custom_conversion_goal": None,
                }
            )

    for batch in ga_service.search_stream(customer_id=account.customer_id, query=campaign_goal_config_query()):
        for row in batch.results:
            config = row.conversion_goal_campaign_config
            rows.append(
                {
                    "level": "campaign_config",
                    "campaign_id": int(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "resource_name": "",
                    "category": "CONFIG",
                    "origin": "CONFIG",
                    "biddable": None,
                    "goal_config_level": enum_name(config.goal_config_level),
                    "custom_conversion_goal": str(config.custom_conversion_goal or ""),
                }
            )
    return rows


def get_or_fetch_conversion_goal_rows(
    session: Session,
    account: GoogleAdsAccount,
    client=None,
    source_job_id: int | None = None,
) -> list[dict]:
    snapshot = get_fresh_snapshot(
        session,
        dataset_key=DATASET_CONVERSION_GOALS,
        account=account,
        scope_key="all",
        query=conversion_goal_query_bundle(),
    )
    if snapshot is not None:
        rows = (snapshot.payload_json or {}).get("rows") or []
        if isinstance(rows, list):
            return rows

    rows = fetch_conversion_goal_rows_from_google(session, account, client)
    upsert_snapshot(
        session,
        dataset_key=DATASET_CONVERSION_GOALS,
        account=account,
        scope_key="all",
        query=conversion_goal_query_bundle(),
        payload={
            "customer_id": account.customer_id,
            "manager_customer_id": account.manager_customer_id,
            "rows": rows,
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=GOAL_SNAPSHOT_TTL_HOURS),
        source_job_id=source_job_id,
        row_count=len(rows),
    )
    return rows


def persist_conversion_goal_rows(session: Session, account: GoogleAdsAccount, rows: list[dict]) -> int:
    session.execute(
        delete(GoogleAdsConversionGoalSnapshot).where(
            GoogleAdsConversionGoalSnapshot.account_id == account.id
        )
    )
    saved = 0
    for row in rows:
        level = str(row.get("level") or "")
        if level not in {"customer", "campaign", "campaign_config"}:
            continue
        if level == "campaign_config":
            _upsert_goal(
                session,
                account_id=account.id,
                level="campaign_config",
                campaign_id=int(row.get("campaign_id") or 0),
                campaign_name=row.get("campaign_name"),
                category="CONFIG",
                origin="CONFIG",
                goal_config_level=row.get("goal_config_level"),
                custom_conversion_goal=str(row.get("custom_conversion_goal") or ""),
            )
        else:
            _upsert_goal(
                session,
                account_id=account.id,
                level=level,
                campaign_id=int(row.get("campaign_id") or 0),
                campaign_name=row.get("campaign_name"),
                category=str(row.get("category") or ""),
                origin=str(row.get("origin") or ""),
                biddable=bool(row.get("biddable")) if row.get("biddable") is not None else None,
            )
        saved += 1
    return saved


def sync_account_conversion_goals(
    session: Session,
    account: GoogleAdsAccount,
    source_job_id: int | None = None,
) -> int:
    rows = get_or_fetch_conversion_goal_rows(session, account, source_job_id=source_job_id)
    saved = persist_conversion_goal_rows(session, account, rows)
    session.commit()
    return saved
