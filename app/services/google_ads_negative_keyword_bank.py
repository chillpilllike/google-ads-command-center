from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import GoogleAdsAccount, GoogleAdsDataSnapshot, GoogleAdsKeywordCandidate, GoogleAdsNegativeKeywordCandidate
from app.services.google_ads_keyword_plan import clean_keyword, metric_value, normalized_keyword, usable_keyword
from app.services.google_ads_snapshot_store import DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS, DATASET_SEARCH_TERMS


MIN_WASTE_CLICKS = 6
MIN_WASTE_COST = 10.0
HIGH_WASTE_CLICKS = 12
HIGH_WASTE_COST = 30.0
MIN_PATTERN_CLICKS = 2
MIN_PATTERN_COST = 2.0
MIN_LOW_CTR_IMPRESSIONS = 1000
LOW_CTR_RATE = 0.002
SOURCE_ROW_LIMIT = 12
DEFAULT_AI_DECISION_LIMIT = 25
NEGATIVE_KEYWORD_DATASETS = {DATASET_SEARCH_TERMS, DATASET_AI_MAX_SEARCH_TERM_COMBINATIONS}

PROTECTED_BRAND_TERMS = {"nutricity", "nutri city"}
SAFE_ACCOUNT_WORDS = {"canada", "ca", "uk", "usa", "us", "au", "account", "ads", "google", "search"}
IRRELEVANT_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("employment_intent", re.compile(r"\b(job|jobs|career|careers|salary|hiring|indeed|work from home)\b", re.I)),
    ("research_only_intent", re.compile(r"\b(pdf|definition|meaning|wiki|wikipedia|reddit|forum|forums)\b", re.I)),
    ("medical_info_intent", re.compile(r"\b(side effect|side effects|symptom|symptoms|dosage|dose|contraindication|contraindications|interaction|interactions)\b", re.I)),
    ("free_discount_intent", re.compile(r"\b(free|coupon|coupons|promo code|discount code|printable coupon)\b", re.I)),
    ("marketplace_competitor_intent", re.compile(r"\b(amazon|walmart|costco|ebay|iherb|well\.ca)\b", re.I)),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rows_from_snapshot(snapshot: GoogleAdsDataSnapshot) -> list[dict[str, Any]]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def _payload_date_range(snapshot: GoogleAdsDataSnapshot) -> dict[str, Any]:
    payload = snapshot.payload_json if isinstance(snapshot.payload_json, dict) else {}
    value = payload.get("date_range")
    return value if isinstance(value, dict) else {}


def _append_unique(target: list[Any], value: Any, *, limit: int = 30) -> None:
    if value in {None, ""}:
        return
    if value not in target and len(target) < limit:
        target.append(value)


def _account_brand_terms(account: GoogleAdsAccount) -> set[str]:
    terms = set(PROTECTED_BRAND_TERMS)
    for word in re.findall(r"[a-z0-9]+", str(account.name or "").lower()):
        if len(word) >= 4 and word not in SAFE_ACCOUNT_WORDS:
            terms.add(word)
    return terms


def _irrelevant_reason(keyword: str) -> str:
    for label, pattern in IRRELEVANT_INTENT_PATTERNS:
        if pattern.search(keyword):
            return label
    return ""


def _negative_reason(
    *,
    keyword: str,
    impressions: int,
    clicks: int,
    cost: float,
) -> tuple[str, float, list[str]]:
    reasons: list[str] = []
    pattern_reason = _irrelevant_reason(keyword)
    if pattern_reason and (clicks >= MIN_PATTERN_CLICKS or cost >= MIN_PATTERN_COST or impressions >= 50):
        reasons.append(pattern_reason)
        return pattern_reason, 0.82, reasons
    if cost >= HIGH_WASTE_COST or clicks >= HIGH_WASTE_CLICKS:
        reasons.append("zero_conversion_high_waste")
        return "zero_conversion_high_waste", 0.9, reasons
    if clicks >= MIN_WASTE_CLICKS and cost >= MIN_WASTE_COST:
        reasons.append("zero_conversion_click_waste")
        return "zero_conversion_click_waste", 0.76, reasons
    ctr = (float(clicks) / float(impressions)) if impressions > 0 else 0.0
    if impressions >= MIN_LOW_CTR_IMPRESSIONS and ctr <= LOW_CTR_RATE:
        reasons.append("low_ctr_no_conversion")
        return "low_ctr_no_conversion", 0.55, reasons
    return "", 0.0, reasons


def _score_negative_candidate(
    *,
    impressions: int,
    clicks: int,
    cost: float,
    reason_label: str,
    confidence: float,
) -> float:
    pattern_bonus = 250.0 if reason_label.endswith("_intent") else 0.0
    low_ctr_bonus = 75.0 if reason_label == "low_ctr_no_conversion" else 0.0
    return (cost * 100.0) + (clicks * 25.0) + (impressions * 0.05) + pattern_bonus + low_ctr_bonus + (confidence * 100.0)


def _extract_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(value[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _decision_key(item: dict[str, Any]) -> tuple[int, str]:
    return (int(item.get("campaign_id") or 0), str(item.get("normalized_keyword") or "").strip().lower())


def _apply_ai_negative_keyword_decisions(
    session: Session,
    account: GoogleAdsAccount,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    settings = get_sync_setting_map(session)
    if not parse_bool(settings.get("openai.negative_keyword_decisions_enabled", True)):
        return {"status": "disabled", "reviewed": 0}
    api_key = str(settings.get("openai.api_key") or "").strip()
    if not api_key:
        return {"status": "skipped_no_api_key", "reviewed": 0}
    limit = min(max(int(settings.get("openai.negative_keyword_decision_limit") or DEFAULT_AI_DECISION_LIMIT), 1), 100)
    review_items = candidates[:limit]
    if not review_items:
        return {"status": "skipped_empty", "reviewed": 0}

    model = str(settings.get("openai.model") or "gpt-5.2")
    compact_items = []
    for item in review_items:
        source_rows = []
        source_json = item.get("source_json") if isinstance(item.get("source_json"), dict) else {}
        for row in source_json.get("source_rows") or []:
            if isinstance(row, dict):
                source_rows.append(
                    {
                        "search_term": row.get("search_term"),
                        "landing_page": row.get("landing_page"),
                        "headline": row.get("headline"),
                        "impressions": row.get("impressions"),
                        "clicks": row.get("clicks"),
                        "cost": row.get("cost"),
                    }
                )
        compact_items.append(
            {
                "campaign_id": item.get("campaign_id"),
                "campaign_name": item.get("campaign_name"),
                "keyword": item.get("keyword"),
                "normalized_keyword": item.get("normalized_keyword"),
                "rule_reason": item.get("reason_label"),
                "rule_confidence": item.get("confidence"),
                "impressions": item.get("impressions"),
                "clicks": item.get("clicks"),
                "cost": round(float(item.get("cost") or 0), 2),
                "conversions": item.get("conversions"),
                "conversion_value": item.get("conversion_value"),
                "source_rows": source_rows[:3],
            }
        )
    prompt = (
        "You are reviewing Google Ads negative keyword candidates for a supplements ecommerce account. "
        "Hard guards have already removed terms with conversions, conversion value, protected brand terms, "
        "and positive keyword-bank matches. Decide only among the remaining candidates. "
        "Return strict JSON with key decisions. Each decision must include campaign_id, normalized_keyword, "
        "decision as approve_negative, reject_negative, or hold_review, confidence 0-1, and a short reason. "
        "Approve only when the term is clearly irrelevant, wasteful, competitor/marketplace intent, employment intent, "
        "research-only intent, medical-info intent, free/coupon intent, or repeated no-conversion waste. "
        "Reject if it could be a buyer term for supplements. Hold when evidence is too thin. "
        f"Account: {account.name}. Candidates: {json.dumps(compact_items, ensure_ascii=True)}"
    )
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": prompt, "text": {"format": {"type": "json_object"}}},
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("output_text") or ""
        if not text:
            for item in body.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        text += content.get("text", "")
        parsed = _extract_json(text)
    except Exception as exc:  # noqa: BLE001 - keep deterministic negative review working.
        return {"status": "failed", "reviewed": 0, "error": str(exc)[:300]}

    raw_decisions = parsed.get("decisions") if isinstance(parsed.get("decisions"), list) else []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for decision in raw_decisions:
        if not isinstance(decision, dict):
            continue
        key = (
            int(decision.get("campaign_id") or 0),
            str(decision.get("normalized_keyword") or "").strip().lower(),
        )
        if key[1]:
            by_key[key] = decision

    applied = 0
    approved = 0
    rejected = 0
    held = 0
    for item in review_items:
        decision = by_key.get(_decision_key(item))
        if not decision:
            continue
        action = str(decision.get("decision") or "").strip().lower()
        confidence = max(0.0, min(float(decision.get("confidence") or 0), 1.0))
        source_json = item.get("source_json") if isinstance(item.get("source_json"), dict) else {}
        source_json["ai_decision"] = {
            "provider": "openai",
            "model": model,
            "decision": action,
            "confidence": confidence,
            "reason": str(decision.get("reason") or "")[:300],
            "decided_at": utcnow().isoformat(),
        }
        item["source_json"] = source_json
        applied += 1
        if action == "approve_negative":
            item["guard_status"] = "candidate"
            item["review_status"] = "new"
            item["confidence"] = max(float(item.get("confidence") or 0), confidence)
            _append_unique(item["guard_reasons"], "openai_approved_negative")
            approved += 1
        elif action == "reject_negative":
            item["guard_status"] = "blocked"
            item["review_status"] = "rejected"
            _append_unique(item["guard_reasons"], "openai_rejected_negative")
            rejected += 1
        elif action == "hold_review":
            item["guard_status"] = "candidate"
            item["review_status"] = "needs_review"
            _append_unique(item["guard_reasons"], "openai_hold_review")
            held += 1
    return {
        "status": "done",
        "reviewed": len(review_items),
        "applied": applied,
        "approved": approved,
        "rejected": rejected,
        "held": held,
    }


def negative_match_text(keyword: str, match_type: str = "exact") -> str:
    text = clean_keyword(keyword)
    lowered = str(match_type or "exact").strip().lower()
    if lowered == "phrase":
        return f'"{text}"'
    if lowered == "broad":
        return text
    return f"[{text}]"


def positive_keyword_guard_terms(session: Session, account_id: int) -> set[str]:
    rows = session.scalars(
        select(GoogleAdsKeywordCandidate.normalized_keyword).where(
            GoogleAdsKeywordCandidate.account_id == int(account_id),
            or_(
                GoogleAdsKeywordCandidate.quality_label.in_(["revenue", "converting"]),
                GoogleAdsKeywordCandidate.conversions > 0,
                GoogleAdsKeywordCandidate.all_conversions > 0,
                GoogleAdsKeywordCandidate.conversion_value > 0,
                GoogleAdsKeywordCandidate.all_conversions_value > 0,
            ),
        )
    ).all()
    return {str(row or "").strip().lower() for row in rows if str(row or "").strip()}


def latest_negative_keyword_snapshots(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
) -> list[GoogleAdsDataSnapshot]:
    query = (
        select(GoogleAdsDataSnapshot)
        .where(
            GoogleAdsDataSnapshot.account_id == account.id,
            GoogleAdsDataSnapshot.dataset_key.in_(list(NEGATIVE_KEYWORD_DATASETS)),
        )
        .order_by(
            GoogleAdsDataSnapshot.dataset_key,
            GoogleAdsDataSnapshot.fetched_at.desc(),
            GoogleAdsDataSnapshot.id.desc(),
        )
    )
    if scope_key:
        query = query.where(GoogleAdsDataSnapshot.scope_key == scope_key)
    snapshots = session.scalars(query).all()
    if scope_key:
        return list(snapshots)
    latest_by_dataset: dict[str, GoogleAdsDataSnapshot] = {}
    for snapshot in snapshots:
        latest_by_dataset.setdefault(snapshot.dataset_key, snapshot)
    return list(latest_by_dataset.values())


def negative_keyword_candidates_from_snapshots(
    account: GoogleAdsAccount,
    snapshots: list[GoogleAdsDataSnapshot],
    *,
    positive_terms: Optional[set[str]] = None,
    pulled_at: Optional[datetime] = None,
    source_job_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    pulled_at = pulled_at or utcnow()
    positive_terms = {str(item or "").strip().lower() for item in (positive_terms or set()) if str(item or "").strip()}
    brand_terms = _account_brand_terms(account)
    grouped: dict[tuple[int, int, str], dict[str, Any]] = {}
    skipped = {
        "protected_positive": 0,
        "protected_brand": 0,
        "has_conversions": 0,
        "low_evidence": 0,
        "unusable": 0,
        "non_search_terms_dataset": 0,
    }

    for snapshot in snapshots:
        if snapshot.dataset_key not in NEGATIVE_KEYWORD_DATASETS:
            skipped["non_search_terms_dataset"] += 1
            continue
        date_range = _payload_date_range(snapshot)
        for row in _rows_from_snapshot(snapshot):
            if not isinstance(row, dict):
                continue
            keyword = clean_keyword(str(row.get("search_term") or ""))
            if not usable_keyword(keyword):
                skipped["unusable"] += 1
                continue
            key = normalized_keyword(keyword)
            conversions = metric_value(row, "conversions")
            all_conversions = metric_value(row, "all_conversions")
            conversion_value = metric_value(row, "conversion_value")
            all_conversions_value = metric_value(row, "all_conversions_value")
            if conversions > 0 or all_conversions > 0 or conversion_value > 0 or all_conversions_value > 0:
                skipped["has_conversions"] += 1
                continue
            if key in positive_terms:
                skipped["protected_positive"] += 1
                continue
            if any(term in key for term in brand_terms):
                skipped["protected_brand"] += 1
                continue

            impressions = int(metric_value(row, "impressions"))
            clicks = int(metric_value(row, "clicks"))
            cost = metric_value(row, "cost")
            reason_label, confidence, guard_reasons = _negative_reason(
                keyword=keyword,
                impressions=impressions,
                clicks=clicks,
                cost=cost,
            )
            if not reason_label:
                skipped["low_evidence"] += 1
                continue

            campaign_id = int(row.get("campaign_id") or 0)
            ad_group_id = 0
            group_key = (campaign_id, ad_group_id, key)
            entry = grouped.setdefault(
                group_key,
                {
                    "account_id": account.id,
                    "scope_level": "campaign",
                    "campaign_id": campaign_id,
                    "campaign_name": str(row.get("campaign_name") or "").strip()[:255],
                    "ad_group_id": ad_group_id,
                    "ad_group_name": "",
                    "keyword": keyword,
                    "normalized_keyword": key,
                    "match_type": "exact",
                    "reason_label": reason_label,
                    "review_status": "new",
                    "guard_status": "candidate",
                    "confidence": confidence,
                    "guard_reasons": [],
                    "source_dataset_keys": [],
                    "source_scope_keys": [],
                    "source_snapshot_ids": [],
                    "campaign_ids": [],
                    "campaign_names": [],
                    "ad_group_ids": [],
                    "ad_group_names": [],
                    "impressions": 0,
                    "clicks": 0,
                    "cost": 0.0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                    "all_conversions": 0.0,
                    "all_conversions_value": 0.0,
                    "score": 0.0,
                    "source_rows": [],
                    "date_ranges": [],
                },
            )
            if len(keyword) > len(str(entry.get("keyword") or "")):
                entry["keyword"] = keyword
            if confidence > float(entry.get("confidence") or 0):
                entry["reason_label"] = reason_label
                entry["confidence"] = confidence
            for guard_reason in guard_reasons:
                _append_unique(entry["guard_reasons"], guard_reason)
            _append_unique(entry["source_dataset_keys"], snapshot.dataset_key)
            _append_unique(entry["source_scope_keys"], snapshot.scope_key)
            _append_unique(entry["source_snapshot_ids"], snapshot.id)
            _append_unique(entry["campaign_ids"], campaign_id or None)
            _append_unique(entry["campaign_names"], str(row.get("campaign_name") or "").strip())
            _append_unique(entry["ad_group_ids"], int(row.get("ad_group_id") or 0) or None)
            _append_unique(entry["ad_group_names"], str(row.get("ad_group_name") or "").strip())
            if date_range and date_range not in entry["date_ranges"]:
                entry["date_ranges"].append(date_range)
            entry["impressions"] += impressions
            entry["clicks"] += clicks
            entry["cost"] += cost
            if len(entry["source_rows"]) < SOURCE_ROW_LIMIT:
                entry["source_rows"].append(
                    {
                        "dataset_key": snapshot.dataset_key,
                        "scope_key": snapshot.scope_key,
                        "campaign_id": row.get("campaign_id"),
                        "campaign_name": row.get("campaign_name"),
                        "ad_group_id": row.get("ad_group_id"),
                        "ad_group_name": row.get("ad_group_name"),
                        "search_term": keyword,
                        "landing_page": row.get("landing_page") or row.get("url") or row.get("expanded_final_url"),
                        "headline": row.get("headline"),
                        "match_source": row.get("match_source") or row.get("source"),
                        "impressions": impressions,
                        "clicks": clicks,
                        "cost": cost,
                    }
                )

    candidates: list[dict[str, Any]] = []
    for entry in grouped.values():
        entry["score"] = _score_negative_candidate(
            impressions=int(entry.get("impressions") or 0),
            clicks=int(entry.get("clicks") or 0),
            cost=float(entry.get("cost") or 0),
            reason_label=str(entry.get("reason_label") or ""),
            confidence=float(entry.get("confidence") or 0),
        )
        entry["source_json"] = {
            "source": "google_ads_search_terms_negative_review",
            "source_job_id": source_job_id,
            "pulled_at": pulled_at.isoformat(),
            "dedupe_key": "account_id+scope_level+campaign_id+ad_group_id+normalized_keyword+match_type",
            "rule_thresholds": {
                "min_waste_clicks": MIN_WASTE_CLICKS,
                "min_waste_cost": MIN_WASTE_COST,
                "high_waste_clicks": HIGH_WASTE_CLICKS,
                "high_waste_cost": HIGH_WASTE_COST,
                "low_ctr_impressions": MIN_LOW_CTR_IMPRESSIONS,
                "low_ctr_rate": LOW_CTR_RATE,
            },
            "date_ranges": entry.pop("date_ranges"),
            "source_rows": entry.pop("source_rows"),
        }
        entry["last_seen_at"] = pulled_at
        entry["last_pulled_at"] = pulled_at
        entry["last_source_job_id"] = source_job_id
        entry["updated_at"] = pulled_at
        candidates.append(entry)
    candidates.sort(
        key=lambda item: (
            float(item.get("confidence") or 0),
            float(item.get("score") or 0),
            int(item.get("clicks") or 0),
            float(item.get("cost") or 0),
        ),
        reverse=True,
    )
    return candidates, skipped


def upsert_negative_keyword_candidates(session: Session, candidates: list[dict[str, Any]]) -> int:
    if not candidates:
        return 0
    stmt = insert(GoogleAdsNegativeKeywordCandidate).values(candidates)
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
            "campaign_name": excluded.campaign_name,
            "ad_group_name": excluded.ad_group_name,
            "keyword": excluded.keyword,
            "reason_label": excluded.reason_label,
            "guard_status": excluded.guard_status,
            "confidence": excluded.confidence,
            "guard_reasons": excluded.guard_reasons,
            "source_dataset_keys": excluded.source_dataset_keys,
            "source_scope_keys": excluded.source_scope_keys,
            "source_snapshot_ids": excluded.source_snapshot_ids,
            "campaign_ids": excluded.campaign_ids,
            "campaign_names": excluded.campaign_names,
            "ad_group_ids": excluded.ad_group_ids,
            "ad_group_names": excluded.ad_group_names,
            "impressions": excluded.impressions,
            "clicks": excluded.clicks,
            "cost": excluded.cost,
            "conversions": excluded.conversions,
            "conversion_value": excluded.conversion_value,
            "all_conversions": excluded.all_conversions,
            "all_conversions_value": excluded.all_conversions_value,
            "score": excluded.score,
            "source_json": excluded.source_json,
            "last_seen_at": excluded.last_seen_at,
            "last_pulled_at": excluded.last_pulled_at,
            "last_source_job_id": excluded.last_source_job_id,
            "updated_at": excluded.updated_at,
        },
    )
    session.execute(stmt)
    return len(candidates)


def sync_account_negative_keyword_candidates(
    session: Session,
    account: GoogleAdsAccount,
    *,
    scope_key: str = "",
    source_job_id: Optional[int] = None,
) -> dict[str, Any]:
    pulled_at = utcnow()
    fresh_cutoff = pulled_at - timedelta(hours=24)
    existing_count, existing_last_pulled_at = session.execute(
        select(
            func.count(GoogleAdsNegativeKeywordCandidate.id),
            func.max(GoogleAdsNegativeKeywordCandidate.last_pulled_at),
        ).where(GoogleAdsNegativeKeywordCandidate.account_id == account.id)
    ).one()
    if existing_count and existing_last_pulled_at and existing_last_pulled_at >= fresh_cutoff:
        return {
            "account_id": account.id,
            "customer_id": account.customer_id,
            "scope_key": scope_key,
            "snapshot_ids": [],
            "candidate_count": int(existing_count or 0),
            "saved": 0,
            "cached": True,
            "skipped": {},
            "last_pulled_at": existing_last_pulled_at.isoformat(),
            "pulled_at": pulled_at.isoformat(),
        }
    snapshots = latest_negative_keyword_snapshots(session, account, scope_key=scope_key)
    positive_terms = positive_keyword_guard_terms(session, account.id)
    candidates, skipped = negative_keyword_candidates_from_snapshots(
        account,
        snapshots,
        positive_terms=positive_terms,
        pulled_at=pulled_at,
        source_job_id=source_job_id,
    )
    ai_decision_result = _apply_ai_negative_keyword_decisions(session, account, candidates)
    snapshot_ids = [snapshot.id for snapshot in snapshots]
    saved = upsert_negative_keyword_candidates(session, candidates)
    session.commit()
    return {
        "account_id": account.id,
        "customer_id": account.customer_id,
        "scope_key": scope_key,
        "snapshot_ids": snapshot_ids,
        "candidate_count": len(candidates),
        "saved": saved,
        "skipped": skipped,
        "ai_decision_result": ai_decision_result,
        "pulled_at": pulled_at.isoformat(),
    }
