from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import AdDraft, AppSetting, AutoPilotEvent, BrowserAutomationTask, GoogleAdsAccount, GoogleAdsGeneratedAsset
from app.services.google_ads_editor_export import _iter_dynamic_search_editor_rows, _iter_editor_rows


BROWSER_AUTOMATION_TOKEN_KEY = "browser_automation.extension_token"
BROWSER_TASK_FINAL_STATUSES = {"done", "skipped", "cancelled"}
BROWSER_TASK_CLAIMABLE_STATUSES = {"queued", "retry"}
BROWSER_TASK_STALE_CLAIM_MINUTES = 15
BROWSER_TASK_BATCHABLE_ACTIONS = {
    "add_keyword",
    "add_negative_keyword",
    "add_url_inclusion",
    "add_url_exclusion",
    "add_pmax_search_theme",
}


def _clean(value: Any, *, max_len: int = 255) -> str:
    return " ".join(str(value or "").strip().split())[:max_len]


def _row_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(row.get(key))
        if value:
            return value
    return ""


def _hash_payload(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value or "").encode("utf-8", "ignore"))
        digest.update(b"\0")
    return digest.hexdigest()[:64]


def _payload_main_value(payload: dict[str, Any]) -> str:
    for key in ("keyword", "search_theme", "url"):
        value = _clean(payload.get(key), max_len=500)
        if value:
            return value
    return ""


def _task_batch_item(task: BrowserAutomationTask) -> dict[str, Any]:
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    return {
        "task_id": int(task.id),
        "action_type": task.action_type,
        "entity_type": task.entity_type,
        "campaign": task.campaign_name,
        "ad_group": task.ad_group_name,
        "asset_group": task.asset_group_name,
        "value": _payload_main_value(payload),
        "keyword": _clean(payload.get("keyword"), max_len=120),
        "match_type": _clean(payload.get("match_type"), max_len=80),
        "search_theme": _clean(payload.get("search_theme"), max_len=120),
        "url": _clean(payload.get("url"), max_len=500),
        "payload": payload,
    }


def _ads_url(account: GoogleAdsAccount, path: str = "overview") -> str:
    customer_id = "".join(ch for ch in str(account.customer_id or "") if ch.isdigit())
    return f"https://ads.google.com/aw/{path}?ocid={customer_id}" if customer_id else f"https://ads.google.com/aw/{path}"


def get_or_create_browser_automation_token(session: Session) -> str:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == BROWSER_AUTOMATION_TOKEN_KEY))
    if setting and isinstance(setting.value, dict) and str(setting.value.get("token") or "").strip():
        return str(setting.value["token"])
    token = secrets.token_urlsafe(32)
    if setting is None:
        setting = AppSetting(
            key=BROWSER_AUTOMATION_TOKEN_KEY,
            value={"token": token, "created_at": datetime.utcnow().isoformat() + "Z"},
            category="Browser automation",
            label="Chrome extension token",
            help_text="Bearer token used by the Google Ads browser worker extension.",
            input_type="password",
            sensitive=True,
        )
        session.add(setting)
    else:
        setting.value = {"token": token, "created_at": datetime.utcnow().isoformat() + "Z"}
        setting.sensitive = True
    session.flush()
    return token


def rotate_browser_automation_token(session: Session) -> str:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == BROWSER_AUTOMATION_TOKEN_KEY))
    token = secrets.token_urlsafe(32)
    payload = {"token": token, "created_at": datetime.utcnow().isoformat() + "Z", "rotated": True}
    if setting is None:
        setting = AppSetting(
            key=BROWSER_AUTOMATION_TOKEN_KEY,
            value=payload,
            category="Browser automation",
            label="Chrome extension token",
            help_text="Bearer token used by the Google Ads browser worker extension.",
            input_type="password",
            sensitive=True,
        )
        session.add(setting)
    else:
        setting.value = payload
        setting.sensitive = True
    session.flush()
    return token


def verify_browser_automation_token(session: Session, token: str) -> bool:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == BROWSER_AUTOMATION_TOKEN_KEY))
    saved = ""
    if setting and isinstance(setting.value, dict):
        saved = str(setting.value.get("token") or "")
    return bool(saved and hmac.compare_digest(saved, str(token or "")))


def _draft_id_for_campaign(drafts: Iterable[AdDraft], campaign_name: str) -> Optional[int]:
    for draft in drafts:
        assets = draft.generated_assets if isinstance(draft.generated_assets, dict) else {}
        identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
        name = _clean(identity.get("campaign_name") or assets.get("campaign_name"))
        if name == campaign_name:
            return int(draft.id)
    return None


def _task_classification(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    criterion = _row_value(row, "Criterion Type", "Criterion Type#Original")
    if _row_value(row, "Campaign Type"):
        return {"action_type": "upsert_campaign", "entity_type": "campaign", "priority": 10, "target_path": "campaigns"}
    if _row_value(row, "Asset Group") and any(_row_value(row, f"Headline {idx}") for idx in range(1, 16)):
        return {"action_type": "upsert_pmax_asset_group", "entity_type": "pmax_asset_group", "priority": 25, "target_path": "asset-groups"}
    if _row_value(row, "Ad Group Type"):
        return {"action_type": "upsert_ad_group", "entity_type": "ad_group", "priority": 20, "target_path": "adgroups"}
    if _row_value(row, "Ad type"):
        return {"action_type": "upsert_ad", "entity_type": "ad", "priority": 30, "target_path": "ads"}
    if _row_value(row, "Keyword") and criterion.startswith("Campaign Negative"):
        return {"action_type": "add_negative_keyword", "entity_type": "negative_keyword", "priority": 70, "target_path": "negativekeywords"}
    if _row_value(row, "Keyword"):
        return {"action_type": "add_keyword", "entity_type": "keyword", "priority": 40, "target_path": "keywords"}
    if _row_value(row, "Search theme"):
        return {"action_type": "add_pmax_search_theme", "entity_type": "pmax_search_theme", "priority": 45, "target_path": "asset-groups"}
    if _row_value(row, "URL rule value 1") and criterion.startswith("Campaign Negative"):
        return {"action_type": "add_url_exclusion", "entity_type": "url_exclusion", "priority": 75, "target_path": "dynamic-search-ad-targets"}
    if _row_value(row, "URL rule value 1"):
        return {"action_type": "add_url_inclusion", "entity_type": "url_inclusion", "priority": 50, "target_path": "dynamic-search-ad-targets"}
    if any(_row_value(row, key) for key in ("Link Text", "Callout text", "Header", "Snippet Values", "Type", "Business name#Original")):
        return {"action_type": "upsert_asset", "entity_type": "asset", "priority": 60, "target_path": "assets"}
    return None


def _task_payload(account: GoogleAdsAccount, row: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    campaign = _row_value(row, "Campaign")
    ad_group = _row_value(row, "Ad Group")
    asset_group = _row_value(row, "Asset Group")
    target_path = str(classification.get("target_path") or "overview")
    return {
        "schema_version": 1,
        "account": {
            "id": account.id,
            "name": account.name,
            "customer_id": account.customer_id,
            "manager_customer_id": account.manager_customer_id,
            "currency_code": account.currency_code,
        },
        "target_url": _ads_url(account, target_path),
        "campaign": campaign,
        "ad_group": ad_group,
        "asset_group": asset_group,
        "action_type": classification["action_type"],
        "entity_type": classification["entity_type"],
        "keyword": _row_value(row, "Keyword"),
        "match_type": _row_value(row, "Criterion Type", "Criterion Type#Original"),
        "search_theme": _row_value(row, "Search theme"),
        "url": _row_value(row, "URL rule value 1", "Final URL"),
        "ad_type": _row_value(row, "Ad type"),
        "business_name": _row_value(row, "Business name"),
        "budget": _row_value(row, "Budget"),
        "bid_strategy": _row_value(row, "Bid Strategy Type"),
        "target_roas": _row_value(row, "Target ROAS"),
        "max_cpc": _row_value(row, "Maximum CPC bid limit", "Max CPC"),
        "headlines": [_row_value(row, f"Headline {idx}") for idx in range(1, 16) if _row_value(row, f"Headline {idx}")],
        "descriptions": [_row_value(row, f"Description {idx}") for idx in range(1, 9) if _row_value(row, f"Description {idx}")],
        "raw_editor_row": {key: value for key, value in row.items() if value not in ("", None)},
    }


def _upsert_task(
    session: Session,
    account: GoogleAdsAccount,
    *,
    draft_id: Optional[int],
    row: dict[str, Any],
    classification: dict[str, Any],
    step_order: int,
) -> tuple[BrowserAutomationTask, bool]:
    payload = _task_payload(account, row, classification)
    dedupe_key = _hash_payload(
        classification["action_type"],
        payload.get("campaign"),
        payload.get("ad_group"),
        payload.get("asset_group"),
        payload.get("keyword"),
        payload.get("search_theme"),
        payload.get("url"),
        payload.get("ad_type"),
    )
    task = session.scalar(
        select(BrowserAutomationTask).where(
            BrowserAutomationTask.account_id == account.id,
            BrowserAutomationTask.dedupe_key == dedupe_key,
        )
    )
    created = False
    if task is None:
        task = BrowserAutomationTask(account_id=account.id, dedupe_key=dedupe_key)
        session.add(task)
        created = True
    if task.status not in BROWSER_TASK_FINAL_STATUSES:
        task.draft_id = draft_id
        task.action_type = classification["action_type"]
        task.entity_type = classification["entity_type"]
        task.campaign_name = _clean(payload.get("campaign"))
        task.ad_group_name = _clean(payload.get("ad_group"))
        task.asset_group_name = _clean(payload.get("asset_group"))
        task.priority = int(classification.get("priority") or 100)
        task.step_order = step_order
        task.payload_json = payload
        task.result_json = {**(task.result_json or {}), "last_generated_at": datetime.utcnow().isoformat() + "Z"}
        if task.status not in {"running", "claimed"}:
            task.status = "queued"
    return task, created


def generate_browser_automation_tasks(session: Session, account: GoogleAdsAccount) -> dict[str, Any]:
    drafts = list(
        session.scalars(
            select(AdDraft)
            .where(AdDraft.account_id == account.id, AdDraft.generated_assets != {})
            .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        ).all()
    )
    generated_assets = list(
        session.scalars(
            select(GoogleAdsGeneratedAsset)
            .where(GoogleAdsGeneratedAsset.account_id == account.id)
            .order_by(GoogleAdsGeneratedAsset.asset_type, GoogleAdsGeneratedAsset.id)
        ).all()
    )
    editor_rows = list(_iter_editor_rows(drafts, generated_assets))
    dynamic_rows = list(_iter_dynamic_search_editor_rows(session, account, drafts))
    rows = editor_rows + dynamic_rows
    created = 0
    updated = 0
    skipped = 0
    counts: Counter[str] = Counter()
    for step_order, row in enumerate(rows, start=1):
        classification = _task_classification(row)
        if classification is None:
            skipped += 1
            continue
        task, was_created = _upsert_task(
            session,
            account,
            draft_id=_draft_id_for_campaign(drafts, _row_value(row, "Campaign")),
            row=row,
            classification=classification,
            step_order=step_order,
        )
        counts[task.action_type] += 1
        created += int(was_created)
        updated += int(not was_created)
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=None,
            campaign_name=None,
            action_type="browser_automation_queue_generated",
            status="planned",
            summary=f"Generated {created} new and refreshed {updated} browser automation tasks.",
            evidence={"created": created, "updated": updated, "skipped": skipped, "counts": dict(counts)},
            result_json={},
        )
    )
    session.flush()
    return {"created": created, "updated": updated, "skipped": skipped, "counts": dict(counts)}


def browser_automation_counts(session: Session, account_id: Optional[int] = None) -> dict[str, Any]:
    statement = select(BrowserAutomationTask.status, func.count(BrowserAutomationTask.id)).group_by(BrowserAutomationTask.status)
    if account_id:
        statement = statement.where(BrowserAutomationTask.account_id == int(account_id))
    counts = {str(status): int(count) for status, count in session.execute(statement).all()}
    action_statement = select(BrowserAutomationTask.action_type, func.count(BrowserAutomationTask.id)).group_by(BrowserAutomationTask.action_type)
    if account_id:
        action_statement = action_statement.where(BrowserAutomationTask.account_id == int(account_id))
    actions = {str(action): int(count) for action, count in session.execute(action_statement).all()}
    return {"statuses": counts, "actions": actions}


def list_browser_automation_tasks(session: Session, account_id: Optional[int] = None, *, limit: int = 200) -> list[BrowserAutomationTask]:
    statement = select(BrowserAutomationTask).order_by(
        BrowserAutomationTask.status.asc(),
        BrowserAutomationTask.priority.asc(),
        BrowserAutomationTask.step_order.asc(),
        BrowserAutomationTask.id.asc(),
    )
    if account_id:
        statement = statement.where(BrowserAutomationTask.account_id == int(account_id))
    return list(session.scalars(statement.limit(max(1, min(int(limit or 200), 1000)))).all())


def claim_next_browser_automation_task(
    session: Session,
    *,
    worker_id: str,
    account_id: Optional[int] = None,
    batch_size: int = 50,
) -> Optional[BrowserAutomationTask]:
    stale_before = datetime.utcnow() - timedelta(minutes=BROWSER_TASK_STALE_CLAIM_MINUTES)
    stale_statement = (
        update(BrowserAutomationTask)
        .where(BrowserAutomationTask.status == "claimed")
        .where(BrowserAutomationTask.claimed_at.is_not(None))
        .where(BrowserAutomationTask.claimed_at < stale_before)
        .values(status="retry", claimed_by="", claimed_at=None)
    )
    if account_id:
        stale_statement = stale_statement.where(BrowserAutomationTask.account_id == int(account_id))
    session.execute(stale_statement)

    statement = (
        select(BrowserAutomationTask.id)
        .where(BrowserAutomationTask.status.in_(BROWSER_TASK_CLAIMABLE_STATUSES))
        .order_by(BrowserAutomationTask.priority.asc(), BrowserAutomationTask.step_order.asc(), BrowserAutomationTask.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if account_id:
        statement = statement.where(BrowserAutomationTask.account_id == int(account_id))
    task_id = session.scalar(statement)
    if task_id is None:
        return None
    task = session.get(BrowserAutomationTask, int(task_id))
    if task is None:
        return None
    task.status = "claimed"
    task.claimed_by = _clean(worker_id, max_len=160)
    task.claimed_at = datetime.utcnow()
    task.started_at = task.started_at or datetime.utcnow()
    batch_items = [_task_batch_item(task)]
    if task.action_type in BROWSER_TASK_BATCHABLE_ACTIONS and batch_size > 1:
        sibling_statement = (
            select(BrowserAutomationTask.id)
            .where(BrowserAutomationTask.id != task.id)
            .where(BrowserAutomationTask.account_id == task.account_id)
            .where(BrowserAutomationTask.status.in_(BROWSER_TASK_CLAIMABLE_STATUSES))
            .where(BrowserAutomationTask.action_type == task.action_type)
            .where(BrowserAutomationTask.campaign_name == task.campaign_name)
            .where(BrowserAutomationTask.ad_group_name == task.ad_group_name)
            .where(BrowserAutomationTask.asset_group_name == task.asset_group_name)
            .order_by(BrowserAutomationTask.priority.asc(), BrowserAutomationTask.step_order.asc(), BrowserAutomationTask.id.asc())
            .limit(max(0, min(int(batch_size), 250) - 1))
            .with_for_update(skip_locked=True)
        )
        if account_id:
            sibling_statement = sibling_statement.where(BrowserAutomationTask.account_id == int(account_id))
        sibling_ids = [int(value) for value in session.scalars(sibling_statement).all()]
        if sibling_ids:
            siblings = list(session.scalars(select(BrowserAutomationTask).where(BrowserAutomationTask.id.in_(sibling_ids))).all())
            for sibling in siblings:
                sibling.status = "claimed"
                sibling.claimed_by = _clean(worker_id, max_len=160)
                sibling.claimed_at = task.claimed_at
                sibling.started_at = sibling.started_at or datetime.utcnow()
                batch_items.append(_task_batch_item(sibling))
    task.result_json = {
        **(task.result_json or {}),
        "claimed_batch": batch_items,
        "claimed_batch_size": len(batch_items),
        "claimed_batch_at": datetime.utcnow().isoformat() + "Z",
    }
    session.flush()
    return task


def mark_browser_automation_task_result(
    session: Session,
    task_id: int,
    *,
    status: str,
    worker_id: str,
    result: dict[str, Any],
) -> BrowserAutomationTask:
    task = session.get(BrowserAutomationTask, int(task_id))
    if task is None:
        raise ValueError("Browser automation task was not found.")
    normalized = str(status or "").strip().lower()
    if normalized not in {"done", "failed", "retry", "skipped", "needs_manual_attention", "cancelled"}:
        normalized = "failed"
    task.status = normalized
    task.claimed_by = _clean(worker_id or task.claimed_by, max_len=160)
    task.result_json = {
        **(task.result_json or {}),
        "last_result": result if isinstance(result, dict) else {"value": str(result)},
        "reported_at": datetime.utcnow().isoformat() + "Z",
    }
    if normalized in BROWSER_TASK_FINAL_STATUSES or normalized in {"failed", "needs_manual_attention"}:
        task.finished_at = datetime.utcnow()
    claimed_batch = task.result_json.get("claimed_batch") if isinstance(task.result_json, dict) else []
    result_batch_ids = result.get("batch_task_ids") if isinstance(result, dict) else None
    batch_ids = {
        int(item.get("task_id"))
        for item in claimed_batch
        if isinstance(item, dict) and str(item.get("task_id") or "").isdigit()
    }
    if isinstance(result_batch_ids, list):
        for item in result_batch_ids:
            if str(item).isdigit():
                batch_ids.add(int(item))
    batch_ids.discard(int(task.id))
    if batch_ids:
        siblings = list(session.scalars(select(BrowserAutomationTask).where(BrowserAutomationTask.id.in_(batch_ids))).all())
        for sibling in siblings:
            sibling.status = normalized
            sibling.claimed_by = _clean(worker_id or sibling.claimed_by, max_len=160)
            sibling.result_json = {
                **(sibling.result_json or {}),
                "batch_parent_task_id": int(task.id),
                "last_result": result if isinstance(result, dict) else {"value": str(result)},
                "reported_at": datetime.utcnow().isoformat() + "Z",
            }
            if normalized in BROWSER_TASK_FINAL_STATUSES or normalized in {"failed", "needs_manual_attention"}:
                sibling.finished_at = datetime.utcnow()
    session.add(
        AutoPilotEvent(
            account_id=task.account_id,
            campaign_id=None,
            campaign_name=task.campaign_name or None,
            action_type=f"browser_{task.action_type}",
            status=normalized,
            summary=f"Browser automation step {task.id} reported {normalized}.",
            evidence={"task_id": task.id, "worker_id": worker_id, "entity_type": task.entity_type},
            result_json=task.result_json,
        )
    )
    session.flush()
    return task


def serialize_browser_task(task: BrowserAutomationTask) -> dict[str, Any]:
    payload = dict(task.payload_json or {})
    claimed_batch = task.result_json.get("claimed_batch") if isinstance(task.result_json, dict) else []
    if isinstance(claimed_batch, list) and claimed_batch:
        values = [
            _clean(item.get("value"), max_len=500)
            for item in claimed_batch
            if isinstance(item, dict) and _clean(item.get("value"), max_len=500)
        ]
        payload["batch_items"] = claimed_batch
        payload["batch_values"] = values
        payload["batch_task_ids"] = [
            int(item.get("task_id"))
            for item in claimed_batch
            if isinstance(item, dict) and str(item.get("task_id") or "").isdigit()
        ]
        payload["batch_size"] = len(values)
    return {
        "id": task.id,
        "account_id": task.account_id,
        "draft_id": task.draft_id,
        "action_type": task.action_type,
        "entity_type": task.entity_type,
        "campaign_name": task.campaign_name,
        "ad_group_name": task.ad_group_name,
        "asset_group_name": task.asset_group_name,
        "priority": task.priority,
        "step_order": task.step_order,
        "status": task.status,
        "payload": payload,
        "result": task.result_json or {},
        "claimed_by": task.claimed_by,
        "claimed_at": task.claimed_at.isoformat() if task.claimed_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }
