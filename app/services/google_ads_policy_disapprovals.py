from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map
from app.models import GoogleAdsAccount, GoogleAdsNegativeKeywordCandidate, GoogleAdsPolicyDisapprovalTerm
from app.services.google_ads_keyword_plan import clean_keyword, normalized_keyword
from app.services.google_ads_live_campaign_creator import (
    _disapproved_policy_term_candidates,
    _is_valid_policy_restricted_term,
    _valid_exact_keyword,
    sync_restricted_policy_terms,
)
from app.services.google_ads_sync import build_client
from app.services.page_feed_restrictions import normalize_restricted_text


POLICY_REASON_LABEL = "policy_unapproved_substances_disapproved"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _term_evidence_map(scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for row in scan.get("evidence_rows") or []:
        if not isinstance(row, dict):
            continue
        for term in row.get("terms") or []:
            key = normalize_restricted_text(term)
            if not key:
                continue
            entry = evidence.setdefault(
                key,
                {
                    "term": term,
                    "rows": [],
                    "campaign_ids": [],
                    "campaign_names": [],
                    "scopes": [],
                },
            )
            if len(entry["rows"]) < 20:
                entry["rows"].append(row)
            if row.get("campaign_id") not in entry["campaign_ids"]:
                entry["campaign_ids"].append(row.get("campaign_id"))
            if row.get("campaign_name") not in entry["campaign_names"]:
                entry["campaign_names"].append(row.get("campaign_name"))
            if row.get("scope") not in entry["scopes"]:
                entry["scopes"].append(row.get("scope"))
    return evidence


def _negative_candidate_rows(
    account: GoogleAdsAccount,
    terms: list[str],
    evidence_by_key: dict[str, dict[str, Any]],
    *,
    pulled_at: datetime,
    source_job_id: Optional[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for term in terms:
        keyword = clean_keyword(term)
        if not _valid_exact_keyword(keyword) or not _is_valid_policy_restricted_term(keyword):
            continue
        key = normalized_keyword(keyword)
        evidence = evidence_by_key.get(normalize_restricted_text(term), {})
        rows.append(
            {
                "account_id": account.id,
                "scope_level": "account",
                "campaign_id": 0,
                "campaign_name": "",
                "ad_group_id": 0,
                "ad_group_name": "",
                "keyword": keyword,
                "normalized_keyword": key,
                "match_type": "exact",
                "reason_label": POLICY_REASON_LABEL,
                "review_status": "selected",
                "guard_status": "active",
                "confidence": 1.0,
                "guard_reasons": ["google_policy_disapproved_unapproved_substances"],
                "source_dataset_keys": ["google_ads_policy_summary"],
                "source_scope_keys": ["disapproved_unapproved_substances"],
                "source_snapshot_ids": [],
                "campaign_ids": [item for item in evidence.get("campaign_ids", []) if item],
                "campaign_names": [item for item in evidence.get("campaign_names", []) if item],
                "ad_group_ids": [],
                "ad_group_names": [],
                "impressions": 0,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0.0,
                "conversion_value": 0.0,
                "all_conversions": 0.0,
                "all_conversions_value": 0.0,
                "score": 10000.0,
                "source_json": {
                    "source": "google_ads_policy_summary",
                    "approval_status": "DISAPPROVED",
                    "policy_topic": "Unapproved substances",
                    "source_job_id": source_job_id,
                    "pulled_at": pulled_at.isoformat(),
                    "evidence": evidence,
                },
                "last_seen_at": pulled_at,
                "last_pulled_at": pulled_at,
                "last_source_job_id": source_job_id,
                "updated_at": pulled_at,
            }
        )
    return rows


def upsert_policy_negative_candidates(session: Session, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = insert(GoogleAdsNegativeKeywordCandidate).values(rows)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsNegativeKeywordCandidate.account_id,
            GoogleAdsNegativeKeywordCandidate.scope_level,
            GoogleAdsNegativeKeywordCandidate.campaign_id,
            GoogleAdsNegativeKeywordCandidate.ad_group_id,
            GoogleAdsNegativeKeywordCandidate.normalized_keyword,
            GoogleAdsNegativeKeywordCandidate.match_type,
        ],
        set_={
            "keyword": excluded.keyword,
            "reason_label": excluded.reason_label,
            "review_status": excluded.review_status,
            "guard_status": excluded.guard_status,
            "confidence": excluded.confidence,
            "guard_reasons": excluded.guard_reasons,
            "source_dataset_keys": excluded.source_dataset_keys,
            "source_scope_keys": excluded.source_scope_keys,
            "campaign_ids": excluded.campaign_ids,
            "campaign_names": excluded.campaign_names,
            "score": excluded.score,
            "source_json": excluded.source_json,
            "last_seen_at": excluded.last_seen_at,
            "last_pulled_at": excluded.last_pulled_at,
            "last_source_job_id": excluded.last_source_job_id,
            "updated_at": excluded.updated_at,
        },
    )
    session.execute(stmt)
    return len(rows)


def _negative_candidate_ids(session: Session, account: GoogleAdsAccount, terms: list[str]) -> dict[str, int]:
    keys = [normalized_keyword(clean_keyword(term)) for term in terms if term]
    if not keys:
        return {}
    rows = session.execute(
        select(GoogleAdsNegativeKeywordCandidate.normalized_keyword, GoogleAdsNegativeKeywordCandidate.id).where(
            GoogleAdsNegativeKeywordCandidate.account_id == account.id,
            GoogleAdsNegativeKeywordCandidate.scope_level == "account",
            GoogleAdsNegativeKeywordCandidate.normalized_keyword.in_(keys),
            GoogleAdsNegativeKeywordCandidate.match_type == "exact",
        )
    ).all()
    return {str(key): int(row_id) for key, row_id in rows}


def upsert_policy_disapproval_terms(
    session: Session,
    account: GoogleAdsAccount,
    terms: list[str],
    evidence_by_key: dict[str, dict[str, Any]],
    *,
    shared_set_resource: str = "",
    shared_keyword_resources: list[str] | None = None,
    pulled_at: datetime,
    source_job_id: Optional[int],
) -> int:
    if not terms:
        return 0
    terms = [term for term in terms if _is_valid_policy_restricted_term(term)]
    negative_ids = _negative_candidate_ids(session, account, terms)
    shared_resources = list(shared_keyword_resources or [])
    rows: list[dict[str, Any]] = []
    for index, term in enumerate(terms):
        keyword = clean_keyword(term)
        key = normalized_keyword(keyword)
        evidence = evidence_by_key.get(normalize_restricted_text(term), {})
        rows.append(
            {
                "account_id": account.id,
                "term": keyword,
                "normalized_term": key,
                "policy_topic": "Unapproved substances",
                "approval_status": "DISAPPROVED",
                "guard_status": "active",
                "occurrence_count": len(evidence.get("rows") or []),
                "evidence_json": {
                    "source": "google_ads_policy_summary",
                    "source_job_id": source_job_id,
                    "pulled_at": pulled_at.isoformat(),
                    "evidence": evidence,
                },
                "google_shared_set_resource_name": shared_set_resource,
                "google_shared_criterion_resource_name": shared_resources[index] if index < len(shared_resources) else "",
                "negative_candidate_id": negative_ids.get(key),
                "last_seen_at": pulled_at,
                "last_pulled_at": pulled_at,
                "last_source_job_id": source_job_id,
                "updated_at": pulled_at,
            }
        )
    stmt = insert(GoogleAdsPolicyDisapprovalTerm).values(rows)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            GoogleAdsPolicyDisapprovalTerm.account_id,
            GoogleAdsPolicyDisapprovalTerm.normalized_term,
            GoogleAdsPolicyDisapprovalTerm.policy_topic,
        ],
        set_={
            "term": excluded.term,
            "approval_status": excluded.approval_status,
            "guard_status": excluded.guard_status,
            "occurrence_count": excluded.occurrence_count,
            "evidence_json": excluded.evidence_json,
            "google_shared_set_resource_name": excluded.google_shared_set_resource_name,
            "google_shared_criterion_resource_name": excluded.google_shared_criterion_resource_name,
            "negative_candidate_id": excluded.negative_candidate_id,
            "last_seen_at": excluded.last_seen_at,
            "last_pulled_at": excluded.last_pulled_at,
            "last_source_job_id": excluded.last_source_job_id,
            "updated_at": excluded.updated_at,
        },
    )
    session.execute(stmt)
    return len(rows)


def sync_account_policy_disapproval_terms(
    session: Session,
    account: GoogleAdsAccount,
    *,
    client: Any | None = None,
    validate_only: bool = False,
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    pulled_at = utcnow()
    client = client or build_client(get_sync_setting_map(session), account.manager_customer_id, account.connection)
    scan = _disapproved_policy_term_candidates(client, account)
    terms = [
        term
        for term in scan.get("terms", [])
        if _valid_exact_keyword(term) and _is_valid_policy_restricted_term(term)
    ]
    terms = list(dict.fromkeys(terms))
    evidence_by_key = _term_evidence_map(scan)
    negative_rows = _negative_candidate_rows(
        account,
        terms,
        evidence_by_key,
        pulled_at=pulled_at,
        source_job_id=source_job_id,
    )
    negative_saved = upsert_policy_negative_candidates(session, negative_rows)
    restricted_result = sync_restricted_policy_terms(session, client, account, validate_only=validate_only)
    shared_resources = list(restricted_result.get("new_shared_keyword_resources") or [])
    policy_saved = upsert_policy_disapproval_terms(
        session,
        account,
        terms,
        evidence_by_key,
        shared_set_resource=str(restricted_result.get("shared_set_resource_name") or ""),
        shared_keyword_resources=shared_resources,
        pulled_at=pulled_at,
        source_job_id=source_job_id,
    )
    session.commit()
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "term_count": len(terms),
        "terms": terms,
        "negative_saved": negative_saved,
        "policy_saved": policy_saved,
        "restricted_setting": restricted_result.get("setting_result") or {},
        "shared_keyword_resources": shared_resources,
        "scan_errors": scan.get("errors") or [],
        "policy_sync_errors": restricted_result.get("errors") or [],
    }
