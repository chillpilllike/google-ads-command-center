from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.models import AdDraft, AutoPilotEvent, GoogleAdsAccount
from app.services.google_ads_live_campaign_creator import (
    _draft_identity,
    _keyword_key,
    _keyword_texts_from_draft,
    _negative_keyword_texts_from_draft,
    _negative_page_urls_from_draft,
    _url_inclusion_urls_from_draft,
    _valid_exact_keyword,
    _webpage_url_key,
)


ACTIVE_DRAFT_STATUSES = {
    "ready_for_review",
    "publish_ready",
    "publish_blocked",
    "needs_review",
    "approved",
    "published_enabled",
    "published_paused",
}
LANE_PRIORITY = {
    "waste": 0,
    "core": 1,
    "max_clicks": 2,
    "testing": 3,
    "fix": 4,
    "other": 5,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _automation_source_key(draft: AdDraft) -> str:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    return str(automation.get("source_key") or "")


def _lane_text(draft: AdDraft) -> str:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    identity = _draft_identity(draft)
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    campaign_identity = automation.get("campaign_identity") if isinstance(automation.get("campaign_identity"), dict) else {}
    pieces = [
        getattr(draft, "ad_type", ""),
        identity.get("category"),
        identity.get("campaign_name"),
        campaign_identity.get("category"),
        campaign_identity.get("campaign_name"),
        assets.get("category"),
        assets.get("campaign_name"),
    ]
    return " ".join(str(piece or "").lower() for piece in pieces)


def _lane_class(draft: AdDraft) -> str:
    text = _lane_text(draft)
    if "waste" in text or "recovery" in text:
        return "waste"
    if "core" in text or "scale" in text:
        return "core"
    if "max clicks" in text or "max_clicks" in text or "maximize_clicks" in text or "bootstrap" in text:
        return "max_clicks"
    if "testing" in text or "discovery" in text:
        return "testing"
    if "fix" in text or "watch" in text:
        return "fix"
    return "other"


def _draft_label(draft: AdDraft) -> str:
    identity = _draft_identity(draft)
    return str(identity.get("campaign_name") or f"draft:{draft.id}")


def _positive_search_terms(draft: AdDraft) -> set[str]:
    terms = {_keyword_key(term) for term in _keyword_texts_from_draft(draft, limit=1000)}
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    for term in assets.get("pmax_search_themes") or []:
        key = _keyword_key(term)
        if key and _valid_exact_keyword(key):
            terms.add(key)
    for group in assets.get("pmax_asset_groups") or []:
        if not isinstance(group, dict):
            continue
        for term in group.get("search_themes") or []:
            key = _keyword_key(term)
            if key and _valid_exact_keyword(key):
                terms.add(key)
    return {term for term in terms if term}


def _positive_urls(draft: AdDraft) -> set[str]:
    return {_webpage_url_key(url) for url in _url_inclusion_urls_from_draft(draft, limit=1000) if _webpage_url_key(url)}


def _negative_terms(draft: AdDraft) -> set[str]:
    return {_keyword_key(term) for term in _negative_keyword_texts_from_draft(draft, limit=5000) if _keyword_key(term)}


def _negative_intent_terms(draft: AdDraft) -> set[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
    terms: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            if str(item.get("source") or "").strip().lower() == "lane_conflict_resolver":
                continue
            match_type = str(item.get("match_type") or "exact").strip().lower()
            if match_type and match_type != "exact":
                continue
            text = item.get("keyword") or item.get("text") or ""
        else:
            text = item
        key = _keyword_key(text)
        if key and _valid_exact_keyword(key):
            terms.add(key)
    return terms


def _negative_urls(draft: AdDraft) -> set[str]:
    return {_webpage_url_key(url) for url in _negative_page_urls_from_draft(draft, limit=5000) if _webpage_url_key(url)}


def _resolver_negative_intent_url_keys(assets: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in assets.get("lane_conflict_url_exclusions") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "").strip().lower() != "lane_conflict_resolver":
            continue
        if str(item.get("owner") or "").strip().lower() != "negative_intent_bank":
            continue
        key = _webpage_url_key(item.get("url") or "")
        if key:
            keys.add(key)
    return keys


def _negative_intent_urls(draft: AdDraft) -> set[str]:
    assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
    resolver_urls = _resolver_negative_intent_url_keys(assets)
    return {url for url in _negative_urls(draft) if url not in resolver_urls}


def _prune_stale_resolver_negative_intent(assets: dict[str, Any], negative_intent_terms: set[str], negative_intent_urls: set[str]) -> bool:
    changed = False
    items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
    if items:
        next_items: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                key = _keyword_key(item.get("keyword") or item.get("text") or "")
                is_resolver_negative_intent = (
                    str(item.get("source") or "").strip().lower() == "lane_conflict_resolver"
                    and str(item.get("owner") or "").strip().lower() == "negative_intent_bank"
                )
                if is_resolver_negative_intent and key and key not in negative_intent_terms:
                    changed = True
                    continue
            next_items.append(item)
        if changed:
            assets["negative_keywords"] = next_items

    values = assets.get("negative_page_targets") if isinstance(assets.get("negative_page_targets"), list) else []
    stale_url_keys = _resolver_negative_intent_url_keys(assets) - negative_intent_urls
    if stale_url_keys and values:
        next_values = [value for value in values if _webpage_url_key(value) not in stale_url_keys]
        if len(next_values) != len(values):
            assets["negative_page_targets"] = next_values
            changed = True
    governance = assets.get("lane_conflict_url_exclusions") if isinstance(assets.get("lane_conflict_url_exclusions"), list) else []
    if stale_url_keys and governance:
        next_governance = []
        for item in governance:
            key = _webpage_url_key(item.get("url") or "") if isinstance(item, dict) else ""
            if key in stale_url_keys:
                changed = True
                continue
            next_governance.append(item)
        assets["lane_conflict_url_exclusions"] = next_governance
    return changed


def _winner(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(rows, key=lambda row: (LANE_PRIORITY.get(row["lane_class"], 99), row["draft"].created_at or utcnow(), row["draft"].id))[0]


def _append_negative_keyword(assets: dict[str, Any], term: str, reason: str, owner: str) -> bool:
    key = _keyword_key(term)
    if not key or not _valid_exact_keyword(key):
        return False
    items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
    existing = {_keyword_key(item.get("keyword") if isinstance(item, dict) else item) for item in items}
    if key in existing:
        return False
    items.append(
        {
            "keyword": key,
            "match_type": "exact",
            "source": "lane_conflict_resolver",
            "reason": reason,
            "owner": owner,
        }
    )
    assets["negative_keywords"] = items
    return True


def _append_negative_url(assets: dict[str, Any], url: str, reason: str, owner: str) -> bool:
    key = _webpage_url_key(url)
    if not key:
        return False
    values = assets.get("negative_page_targets") if isinstance(assets.get("negative_page_targets"), list) else []
    existing = {_webpage_url_key(item) for item in values}
    if key in existing:
        return False
    values.append(key)
    assets["negative_page_targets"] = values
    governance = assets.get("lane_conflict_url_exclusions") if isinstance(assets.get("lane_conflict_url_exclusions"), list) else []
    governance.append({"url": key, "reason": reason, "owner": owner, "source": "lane_conflict_resolver"})
    assets["lane_conflict_url_exclusions"] = governance[-500:]
    return True


def _drafts_for_account(session: Session, account: GoogleAdsAccount, *, limit: int = 200) -> list[AdDraft]:
    rows = list(
        session.scalars(
            select(AdDraft)
            .options(
                load_only(
                    AdDraft.id,
                    AdDraft.account_id,
                    AdDraft.ad_type,
                    AdDraft.status,
                    AdDraft.website_url,
                    AdDraft.final_url,
                    AdDraft.business_name,
                    AdDraft.generated_assets,
                    AdDraft.created_at,
                )
            )
            .where(
                AdDraft.account_id == account.id,
                AdDraft.status.in_(ACTIVE_DRAFT_STATUSES),
            )
            .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
            .limit(max(10, min(int(limit or 200), 1000)))
        ).all()
    )
    return [draft for draft in rows if _automation_source_key(draft).startswith("automation:")]


def resolve_account_lane_conflicts(
    session: Session,
    account: GoogleAdsAccount,
    *,
    source_job_id: Optional[int] = None,
    max_drafts: int = 200,
) -> dict[str, Any]:
    drafts = _drafts_for_account(session, account, limit=max_drafts)
    if not drafts:
        return {"status": "skipped_no_drafts", "account_id": account.id, "draft_count": 0}

    term_rows: dict[str, list[dict[str, Any]]] = {}
    url_rows: dict[str, list[dict[str, Any]]] = {}
    draft_state: dict[int, dict[str, Any]] = {}
    for draft in drafts:
        lane_class = _lane_class(draft)
        label = _draft_label(draft)
        positives = _positive_search_terms(draft)
        urls = _positive_urls(draft)
        negatives = _negative_intent_terms(draft)
        negative_urls = _negative_intent_urls(draft)
        draft_state[draft.id] = {
            "draft": draft,
            "lane_class": lane_class,
            "label": label,
            "positive_terms": positives,
            "positive_urls": urls,
            "negative_terms": negatives,
            "negative_urls": negative_urls,
        }
        for term in positives:
            term_rows.setdefault(term, []).append(draft_state[draft.id])
        for url in urls:
            url_rows.setdefault(url, []).append(draft_state[draft.id])

    changed_drafts: set[int] = set()
    keyword_conflicts: list[dict[str, Any]] = []
    url_conflicts: list[dict[str, Any]] = []
    negative_intent_terms = set()
    negative_intent_urls = set()
    for state in draft_state.values():
        negative_intent_terms.update(state["negative_terms"])
        negative_intent_urls.update(state["negative_urls"])

    for state in draft_state.values():
        assets = dict(state["draft"].generated_assets or {})
        if _prune_stale_resolver_negative_intent(assets, negative_intent_terms, negative_intent_urls):
            state["draft"].generated_assets = assets
            changed_drafts.add(state["draft"].id)

    for term, rows in sorted(term_rows.items()):
        if len(rows) < 2:
            continue
        owner = _winner(rows)
        owner_label = owner["label"]
        for row in rows:
            if row["draft"].id == owner["draft"].id:
                continue
            assets = dict(row["draft"].generated_assets or {})
            reason = f"Lane conflict: owned by {owner_label}; exclude from {row['label']} to prevent internal competition."
            if _append_negative_keyword(assets, term, reason, owner_label):
                row["draft"].generated_assets = assets
                changed_drafts.add(row["draft"].id)
                keyword_conflicts.append(
                    {
                        "keyword": term,
                        "owner_draft_id": owner["draft"].id,
                        "owner_lane": owner["lane_class"],
                        "loser_draft_id": row["draft"].id,
                        "loser_lane": row["lane_class"],
                    }
                )

    for term in sorted(negative_intent_terms):
        for row in term_rows.get(term, []):
            if row["lane_class"] == "waste":
                continue
            assets = dict(row["draft"].generated_assets or {})
            reason = f"Negative intent conflict: {term} is planned as a negative elsewhere; exclude from {row['label']}."
            if _append_negative_keyword(assets, term, reason, "negative_intent_bank"):
                row["draft"].generated_assets = assets
                changed_drafts.add(row["draft"].id)
                keyword_conflicts.append(
                    {
                        "keyword": term,
                        "owner_draft_id": None,
                        "owner_lane": "negative_intent_bank",
                        "loser_draft_id": row["draft"].id,
                        "loser_lane": row["lane_class"],
                    }
                )

    for url, rows in sorted(url_rows.items()):
        if len(rows) < 2:
            continue
        owner = _winner(rows)
        owner_label = owner["label"]
        for row in rows:
            if row["draft"].id == owner["draft"].id:
                continue
            assets = dict(row["draft"].generated_assets or {})
            reason = f"Lane conflict: URL owned by {owner_label}; exclude from {row['label']} to prevent internal competition."
            if _append_negative_url(assets, url, reason, owner_label):
                row["draft"].generated_assets = assets
                changed_drafts.add(row["draft"].id)
                url_conflicts.append(
                    {
                        "url": url,
                        "owner_draft_id": owner["draft"].id,
                        "owner_lane": owner["lane_class"],
                        "loser_draft_id": row["draft"].id,
                        "loser_lane": row["lane_class"],
                    }
                )

    for url in sorted(negative_intent_urls):
        for row in url_rows.get(url, []):
            if row["lane_class"] == "waste":
                continue
            assets = dict(row["draft"].generated_assets or {})
            reason = f"Negative URL intent conflict: {url} is planned as an exclusion elsewhere; exclude from {row['label']}."
            if _append_negative_url(assets, url, reason, "negative_intent_bank"):
                row["draft"].generated_assets = assets
                changed_drafts.add(row["draft"].id)
                url_conflicts.append(
                    {
                        "url": url,
                        "owner_draft_id": None,
                        "owner_lane": "negative_intent_bank",
                        "loser_draft_id": row["draft"].id,
                        "loser_lane": row["lane_class"],
                    }
                )

    for draft_id in changed_drafts:
        draft = draft_state[draft_id]["draft"]
        assets = dict(draft.generated_assets or {})
        state = assets.get("lane_conflict_resolver") if isinstance(assets.get("lane_conflict_resolver"), dict) else {}
        assets["lane_conflict_resolver"] = {
            **state,
            "last_resolved_at": utcnow().isoformat(),
            "source_job_id": source_job_id,
        }
        draft.generated_assets = assets

    result = {
        "status": "updated" if changed_drafts else "clean",
        "account_id": account.id,
        "customer_id": account.customer_id,
        "draft_count": len(drafts),
        "changed_draft_count": len(changed_drafts),
        "keyword_conflict_count": len(keyword_conflicts),
        "url_conflict_count": len(url_conflicts),
        "keyword_conflicts": keyword_conflicts[:200],
        "url_conflicts": url_conflicts[:200],
        "api_calls": 0,
        "basis": "DB draft intent only; no direct Google Ads reads or writes are performed by this resolver.",
    }
    if changed_drafts or keyword_conflicts or url_conflicts:
        session.add(
            AutoPilotEvent(
                account_id=account.id,
                campaign_id=None,
                campaign_name=None,
                action_type="google_ads_lane_conflict_resolver",
                status="planned" if changed_drafts else "clean",
                summary=(
                    f"Resolved {len(keyword_conflicts)} keyword and {len(url_conflicts)} URL lane conflicts "
                    f"across {len(changed_drafts)} draft(s) using DB draft intent."
                ),
                evidence={
                    "source_job_id": source_job_id,
                    "draft_count": len(drafts),
                    "api_calls": 0,
                    "basis": result["basis"],
                    "keyword_conflicts": keyword_conflicts[:50],
                    "url_conflicts": url_conflicts[:50],
                },
                result_json=result,
            )
        )
    session.commit()
    return result
