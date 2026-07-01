from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import AppSetting, AutoPilotEvent, GoogleAdsAccount
from app.services.google_ads_account_red_flags import account_api_red_flag
from app.services.google_ads_mutation_pacing import paced_google_ads_mutate
from app.services.google_ads_sync import build_client, enum_name


STATE_PREFIX = "google_ads_roas_impression_maintenance"
AUTO_CAMPAIGN_QUERY = """
    SELECT
      campaign.id,
      campaign.name,
      campaign.resource_name,
      campaign.status,
      campaign.advertising_channel_type,
      campaign.bidding_strategy_type,
      campaign.maximize_conversion_value.target_roas,
      campaign_budget.resource_name,
      campaign_budget.amount_micros,
      metrics.impressions
    FROM campaign
    WHERE campaign.name LIKE 'AUTO |%'
      AND campaign.status != REMOVED
      AND segments.date BETWEEN '{start_date}' AND '{end_date}'
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _customer_id_set(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace("\\n", "\n").replace(",", "\n").splitlines()
    return {"".join(ch for ch in item if ch.isdigit()) for item in raw_items if "".join(ch for ch in item if ch.isdigit())}


def account_tier(account: GoogleAdsAccount, settings: dict[str, Any]) -> str:
    customer_id = "".join(ch for ch in str(account.customer_id or "") if ch.isdigit())
    primary_ids = _customer_id_set(settings.get("automation.scheduler_primary_customer_ids"))
    secondary_ids = _customer_id_set(settings.get("automation.scheduler_secondary_customer_ids"))
    if customer_id in primary_ids:
        return "primary"
    if customer_id in secondary_ids:
        return "secondary"
    return "other"


def _state_key(account: GoogleAdsAccount, campaign_id: int) -> str:
    return f"{STATE_PREFIX}.{account.customer_id}.{campaign_id}"


def _load_state(session: Session, account: GoogleAdsAccount, campaign_id: int) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == _state_key(account, campaign_id)))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def _save_state(
    session: Session,
    account: GoogleAdsAccount,
    campaign_id: int,
    campaign_name: str,
    state: dict[str, Any],
) -> None:
    now = _utcnow()
    stmt = insert(AppSetting).values(
        key=_state_key(account, campaign_id),
        value=state,
        category="Google Ads automation state",
        label=f"{account.name} ROAS impression state {campaign_id}",
        help_text=f"Campaign-level ROAS rescue/recovery state for {campaign_name}.",
        input_type="json",
        sensitive=False,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={
            "value": state,
            "category": "Google Ads automation state",
            "label": f"{account.name} ROAS impression state {campaign_id}",
            "help_text": f"Campaign-level ROAS rescue/recovery state for {campaign_name}.",
            "input_type": "json",
            "sensitive": False,
            "updated_at": now,
        },
    )
    session.execute(stmt)


def _target_for_tier(tier: str, state: dict[str, Any], current_roas: float) -> Optional[float]:
    if tier == "secondary":
        return 3.0
    if tier == "other":
        return 3.0 if state.get("graduated_to_300") else 2.0
    if tier == "primary":
        baseline = state.get("baseline_target_roas")
        try:
            baseline_value = float(baseline)
        except (TypeError, ValueError):
            baseline_value = float(current_roas or 0)
        return baseline_value if baseline_value > 0 else None
    return None


def _budget_floor_micros(account: GoogleAdsAccount) -> int:
    currency = str(account.currency_code or "").upper()
    amount = 500 if currency == "INR" else 10
    return int(amount * 1_000_000)


def _eligible_auto_campaign(name: str) -> bool:
    return str(name or "").startswith("AUTO |")


def _build_campaign_rows(client: Any, account: GoogleAdsAccount, *, days: int) -> list[dict[str, Any]]:
    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    query = AUTO_CAMPAIGN_QUERY.format(start_date=start_date.isoformat(), end_date=end_date.isoformat())
    service = client.get_service("GoogleAdsService")
    by_id: dict[int, dict[str, Any]] = {}
    for batch in service.search_stream(customer_id=account.customer_id, query=query):
        for row in batch.results:
            campaign_id = int(row.campaign.id or 0)
            item = by_id.setdefault(
                campaign_id,
                {
                    "campaign_id": campaign_id,
                    "campaign_name": str(row.campaign.name or ""),
                    "campaign_resource_name": str(row.campaign.resource_name or ""),
                    "campaign_status": enum_name(row.campaign.status),
                    "channel_type": enum_name(row.campaign.advertising_channel_type),
                    "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
                    "target_roas": float(row.campaign.maximize_conversion_value.target_roas or 0),
                    "budget_resource_name": str(row.campaign_budget.resource_name or ""),
                    "budget_amount_micros": int(row.campaign_budget.amount_micros or 0),
                    "impressions": 0,
                },
            )
            item["impressions"] += int(row.metrics.impressions or 0)
    return list(by_id.values())


def _campaign_operation(client: Any, resource_name: str, target_roas: Optional[float]) -> Any:
    operation = client.get_type("CampaignOperation")
    operation.update.resource_name = resource_name
    if target_roas is None:
        operation.update.maximize_conversion_value.target_roas = 0.0
        try:
            operation.update._pb.maximize_conversion_value.ClearField("target_roas")
        except Exception:
            pass
    else:
        operation.update.maximize_conversion_value.target_roas = float(target_roas)
    operation.update_mask.paths.append("maximize_conversion_value.target_roas")
    return operation


def _budget_operation(client: Any, resource_name: str, amount_micros: int) -> Any:
    operation = client.get_type("CampaignBudgetOperation")
    operation.update.resource_name = resource_name
    operation.update.amount_micros = int(amount_micros)
    operation.update_mask.paths.append("amount_micros")
    return operation


def _mutate_campaigns(client: Any, account: GoogleAdsAccount, operations: list[Any], validate_only: bool) -> None:
    if not operations:
        return
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = bool(validate_only)
    paced_google_ads_mutate(client.get_service("CampaignService"), "mutate_campaigns", request, timeout=60, attempts=3)


def _mutate_budgets(client: Any, account: GoogleAdsAccount, operations: list[Any], validate_only: bool) -> None:
    if not operations:
        return
    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = account.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = bool(validate_only)
    paced_google_ads_mutate(client.get_service("CampaignBudgetService"), "mutate_campaign_budgets", request, timeout=60, attempts=3)


def _round_roas(value: float) -> float:
    return round(float(value), 4)


def _add_event(
    session: Session,
    account: GoogleAdsAccount,
    campaign: dict[str, Any],
    *,
    action_type: str,
    status: str,
    summary: str,
    evidence: dict[str, Any],
    result: dict[str, Any],
) -> None:
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=int(campaign.get("campaign_id") or 0),
            campaign_name=str(campaign.get("campaign_name") or ""),
            action_type=action_type,
            status=status,
            summary=summary,
            evidence=evidence,
            result_json=result,
            created_at=_utcnow(),
        )
    )


def maintain_account_roas_impression_rescue(
    session: Session,
    account: GoogleAdsAccount,
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    if account_api_red_flag(session, account) is not None:
        return {"status": "blocked_red_flag", "account_id": account.id, "customer_id": account.customer_id}

    settings = get_sync_setting_map(session)
    allow_mutations = parse_bool(settings.get("optimizer.allow_mutations", False))
    dry_run = parse_bool(settings.get("optimizer.dry_run", True))
    if not validate_only and (not allow_mutations or dry_run):
        return {
            "status": "blocked_by_mutation_guard",
            "account_id": account.id,
            "customer_id": account.customer_id,
            "allow_mutations": allow_mutations,
            "dry_run": dry_run,
        }

    tier = account_tier(account, settings)
    client = build_client(settings, account.manager_customer_id, account.connection)
    rows = [row for row in _build_campaign_rows(client, account, days=7) if _eligible_auto_campaign(row.get("campaign_name", ""))]
    now = _utcnow()
    campaign_ops: list[Any] = []
    budget_ops: list[Any] = []
    actions: list[dict[str, Any]] = []
    unchanged = 0

    for campaign in rows:
        campaign_id = int(campaign.get("campaign_id") or 0)
        campaign_name = str(campaign.get("campaign_name") or "")
        current_roas = float(campaign.get("target_roas") or 0)
        impressions_7d = int(campaign.get("impressions") or 0)
        state = _load_state(session, account, campaign_id)
        if current_roas > 0 and not state.get("baseline_target_roas"):
            state["baseline_target_roas"] = current_roas
        state.update(
            {
                "account_tier": tier,
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "last_checked_at": now.isoformat(),
                "last_impressions_7d": impressions_7d,
                "last_seen_target_roas": current_roas,
            }
        )

        desired_roas: Optional[float] = None
        desired_budget: Optional[int] = None
        reason = "no_change"
        mode = str(state.get("mode") or "normal")

        if impressions_7d <= 0:
            no_impression_started = _parse_time(state.get("no_impression_started_at"))
            if no_impression_started is None:
                no_impression_started = now
                state["no_impression_started_at"] = now.isoformat()
                state["rescue_started_at"] = now.isoformat()
            state["mode"] = "rescue"
            state.pop("recovery_started_at", None)
            state.pop("graduated_to_300", None)
            desired_budget = _budget_floor_micros(account)
            target = _target_for_tier(tier, state, current_roas)
            last_lowered = _parse_time(state.get("last_lowered_at"))
            rescue_age = now - no_impression_started
            if current_roas <= 0:
                desired_roas = None
                reason = "no_impressions_budget_floor_no_target_roas"
            elif rescue_age < timedelta(hours=48):
                desired_roas = target if target and current_roas > target else current_roas
                reason = "no_impressions_initial_rescue_budget_floor"
            elif last_lowered is None or last_lowered <= now - timedelta(hours=24):
                reduced = current_roas * 0.8
                if reduced <= 1.0:
                    desired_roas = None
                    state["target_roas_removed_at"] = now.isoformat()
                    reason = "no_impressions_remove_target_roas_floor_reached"
                else:
                    desired_roas = _round_roas(reduced)
                    state["last_lowered_at"] = now.isoformat()
                    reason = "no_impressions_lower_target_roas_20pct"
            else:
                desired_roas = current_roas
                reason = "no_impressions_wait_24h_before_next_lower"
        else:
            state["last_seen_impressions_at"] = now.isoformat()
            if mode == "rescue" or state.get("no_impression_started_at"):
                state["mode"] = "recovery"
                state.pop("no_impression_started_at", None)
                state.pop("rescue_started_at", None)
                state.pop("last_lowered_at", None)
                state.setdefault("recovery_started_at", now.isoformat())
                reason = "impressions_resumed_start_recovery_watch"
            recovery_started = _parse_time(state.get("recovery_started_at"))
            last_raised = _parse_time(state.get("last_raised_at"))
            recovery_ready = recovery_started is not None and recovery_started <= now - timedelta(days=3)
            raise_due = last_raised is None or last_raised <= now - timedelta(hours=24)
            if recovery_ready and raise_due:
                if current_roas <= 0:
                    desired_roas = 1.0
                else:
                    desired_roas = min(3.0, _round_roas(current_roas * 1.1))
                state["last_raised_at"] = now.isoformat()
                reason = "recovery_raise_target_roas_10pct_after_24h"
                if desired_roas >= 3.0:
                    state["graduated_to_300"] = True
                    state["mode"] = "normal"
                    state.pop("recovery_started_at", None)
            elif mode not in {"rescue", "recovery"}:
                target = _target_for_tier(tier, state, current_roas)
                if tier in {"secondary", "other"} and current_roas > 0 and target and abs(current_roas - target) > 0.0001:
                    desired_roas = target
                    reason = f"tier_target_{tier}_{int(target * 100)}pct"

        if desired_budget is not None and campaign.get("budget_resource_name") and int(campaign.get("budget_amount_micros") or 0) != desired_budget:
            budget_ops.append(_budget_operation(client, str(campaign["budget_resource_name"]), desired_budget))
        if desired_roas is not None and current_roas > 0 and abs(current_roas - desired_roas) > 0.0001:
            campaign_ops.append(_campaign_operation(client, str(campaign["campaign_resource_name"]), desired_roas))
        elif desired_roas is None and current_roas > 0 and reason == "no_impressions_remove_target_roas_floor_reached":
            campaign_ops.append(_campaign_operation(client, str(campaign["campaign_resource_name"]), None))

        changed = (
            (desired_budget is not None and int(campaign.get("budget_amount_micros") or 0) != desired_budget)
            or (desired_roas is not None and current_roas > 0 and abs(current_roas - desired_roas) > 0.0001)
            or (desired_roas is None and current_roas > 0 and reason == "no_impressions_remove_target_roas_floor_reached")
        )
        _save_state(session, account, campaign_id, campaign_name, state)
        if changed:
            action = {
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "tier": tier,
                "reason": reason,
                "impressions_7d": impressions_7d,
                "old_target_roas": current_roas,
                "new_target_roas": desired_roas,
                "old_budget_micros": int(campaign.get("budget_amount_micros") or 0),
                "new_budget_micros": desired_budget,
            }
            actions.append(action)
            _add_event(
                session,
                account,
                campaign,
                action_type="roas_impression_maintenance",
                status="validated" if validate_only else "planned",
                summary=f"ROAS impression maintenance planned for {campaign_name}: {reason}.",
                evidence={"tier": tier, "impressions_7d": impressions_7d, "state": state},
                result=action,
            )
        else:
            unchanged += 1

    _mutate_campaigns(client, account, campaign_ops, validate_only)
    _mutate_budgets(client, account, budget_ops, validate_only)
    if actions and not validate_only:
        for action in actions:
            session.add(
                AutoPilotEvent(
                    account_id=account.id,
                    campaign_id=int(action.get("campaign_id") or 0),
                    campaign_name=str(action.get("campaign_name") or ""),
                    action_type="roas_impression_maintenance",
                    status="applied",
                    summary=f"Applied ROAS impression maintenance: {action.get('reason')}.",
                    evidence={"tier": tier, "target_cap": 3.0},
                    result_json=action,
                    created_at=_utcnow(),
                )
            )
    session.commit()
    return {
        "status": "done",
        "account_id": account.id,
        "customer_id": account.customer_id,
        "tier": tier,
        "campaigns_seen": len(rows),
        "actions": len(actions),
        "campaign_mutations": len(campaign_ops),
        "budget_mutations": len(budget_ops),
        "unchanged": unchanged,
        "items": actions,
    }


def maintain_roas_impression_rescue_for_accounts(
    session: Session,
    accounts: list[GoogleAdsAccount],
    *,
    validate_only: bool = False,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for account in accounts:
        try:
            results.append(maintain_account_roas_impression_rescue(session, account, validate_only=validate_only))
        except GoogleAdsException as exc:
            errors.append({"account_id": account.id, "customer_id": account.customer_id, "error": str(exc)[:1000]})
        except Exception as exc:  # noqa: BLE001 - one account must not stop the worker.
            errors.append({"account_id": account.id, "customer_id": account.customer_id, "error": f"{exc.__class__.__name__}: {str(exc)[:1000]}"})
    return {
        "status": "failed" if errors and not results else "partial" if errors else "done",
        "account_count": len(accounts),
        "results": results,
        "errors": errors,
        "action_count": sum(int(item.get("actions") or 0) for item in results),
        "campaign_mutations": sum(int(item.get("campaign_mutations") or 0) for item in results),
        "budget_mutations": sum(int(item.get("budget_mutations") or 0) for item in results),
    }
