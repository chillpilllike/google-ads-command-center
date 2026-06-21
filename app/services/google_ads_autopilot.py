from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.app_settings import get_sync_setting_map, parse_bool
from app.models import AppSetting, AutoPilotEvent, GoogleAdsAccount
from app.services.currency_rates import convert_amount, get_latest_rate_snapshot_sync, snapshot_payload
from app.services.google_ads_api_errors import record_google_ads_api_error, record_google_ads_generic_error
from app.services.google_ads_goals import get_or_fetch_conversion_goal_rows
from app.services.google_ads_snapshot_store import DATASET_HOURLY_DELIVERY, get_fresh_snapshot, upsert_snapshot
from app.services.google_ads_sync import build_client, enum_name
from app.services.spend_guard import account_spend_guard_from_cost


HOURLY_DELIVERY_SNAPSHOT_TTL_MINUTES = 45


@dataclass
class AutoPilotAction:
    action_type: str
    summary: str
    campaign_id: Optional[int] = None
    campaign_name: Optional[str] = None
    resource_name: Optional[str] = None
    old_value: Any = None
    new_value: Any = None
    field: str = ""
    evidence: Optional[dict[str, Any]] = None
    applyable: bool = True


def currency_to_micros(value: float) -> int:
    return int(float(value) * 1_000_000)


def micros_to_currency(value: int) -> float:
    return int(value or 0) / 1_000_000


def _num(values: dict[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _state_key(account: GoogleAdsAccount, campaign_id: int) -> str:
    return f"autopilot_state.{account.customer_id}.{campaign_id}"


def _load_state(session: Session, account: GoogleAdsAccount, campaign_id: int) -> dict[str, Any]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == _state_key(account, campaign_id)))
    if row is None or not isinstance(row.value, dict):
        return {}
    return row.value


def _save_state(session: Session, account: GoogleAdsAccount, campaign_id: int, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(
        key=_state_key(account, campaign_id),
        value=state,
        category="Autopilot state",
        label=f"{account.name} campaign {campaign_id} autopilot state",
        help_text="Machine state for no-impression delivery rescue.",
        input_type="json",
        sensitive=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value},
    )
    session.execute(stmt)


def _parse_hour(date_text: str, hour: int) -> datetime:
    return datetime.fromisoformat(f"{date_text}T{int(hour):02d}:00:00").replace(tzinfo=timezone.utc)


def _target_budget(settings: dict[str, Any], currency: str) -> int:
    currency = (currency or "").upper()
    if currency == "INR":
        return currency_to_micros(_num(settings, "autopilot.inr_rescue_budget", 50000))
    if currency == "AUD":
        return currency_to_micros(_num(settings, "autopilot.aud_rescue_budget", 500))
    return currency_to_micros(_num(settings, "autopilot.default_rescue_budget", 500))


def _initial_cpc(settings: dict[str, Any], currency: str) -> int:
    currency = (currency or "").upper()
    if currency == "INR":
        return currency_to_micros(_num(settings, "autopilot.maximize_clicks_cpc_inr", 300))
    if currency == "AUD":
        return currency_to_micros(_num(settings, "autopilot.maximize_clicks_cpc_aud", 20))
    return currency_to_micros(_num(settings, "autopilot.maximize_clicks_cpc_aud", 20))


def _cpc_step(settings: dict[str, Any], currency: str) -> int:
    currency = (currency or "").upper()
    if currency == "INR":
        return currency_to_micros(_num(settings, "autopilot.cpc_step_inr", 200))
    if currency == "AUD":
        return currency_to_micros(_num(settings, "autopilot.cpc_step_aud", 10))
    return currency_to_micros(_num(settings, "autopilot.cpc_step_aud", 10))


def _restore_spend_threshold(session: Session, settings: dict[str, Any], currency: str) -> int:
    currency = (currency or "").upper()
    if currency == "INR":
        return currency_to_micros(_num(settings, "autopilot.restore_spend_inr", 5000))
    rates = snapshot_payload(get_latest_rate_snapshot_sync(session)).get("rates", {})
    usd_threshold = _num(settings, "autopilot.restore_spend_usd", 50)
    converted = convert_amount(usd_threshold, "USD", currency or "USD", rates)
    return currency_to_micros(converted if converted is not None else usd_threshold)


def _age_hours(timestamp: str | None, now: datetime) -> float:
    if not timestamp:
        return 999999.0
    try:
        return (now - datetime.fromisoformat(timestamp)).total_seconds() / 3600
    except ValueError:
        return 999999.0


def _hourly_delivery_query(start_date: str, end_date: str) -> str:
    return f"""
        SELECT
          customer.currency_code,
          segments.date,
          segments.hour,
          campaign.id,
          campaign.name,
          campaign.resource_name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversion_value.target_roas,
          campaign.maximize_clicks.cpc_bid_ceiling_micros,
          campaign_budget.resource_name,
          campaign_budget.amount_micros,
          campaign_budget.explicitly_shared,
          metrics.cost_micros,
          metrics.impressions,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND segments.date BETWEEN '{start_date}' AND '{end_date}'
    """


def _hourly_delivery_scope(lookback_hours: int, start_date: str, end_date: str, end_dt: datetime) -> str:
    return f"lookback:{max(int(lookback_hours), 1)}:{start_date}:{end_date}:hour:{end_dt.strftime('%Y-%m-%dT%H')}"


def _campaigns_from_snapshot(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    campaigns = payload.get("campaigns") or {}
    if not isinstance(campaigns, dict):
        return {}
    parsed: dict[int, dict[str, Any]] = {}
    for campaign_id, campaign in campaigns.items():
        if not isinstance(campaign, dict):
            continue
        parsed[int(campaign_id)] = campaign
    return parsed


def _hourly_campaign_rows(
    session: Session,
    client,
    account: GoogleAdsAccount,
    lookback_hours: int,
    source_job_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    service = client.get_service("GoogleAdsService")
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=max(lookback_hours, 1))
    start_date = start_dt.date().isoformat()
    end_date = end_dt.date().isoformat()
    query = _hourly_delivery_query(start_date, end_date)
    scope_key = _hourly_delivery_scope(lookback_hours, start_date, end_date, end_dt)
    snapshot = get_fresh_snapshot(
        session,
        dataset_key=DATASET_HOURLY_DELIVERY,
        account=account,
        scope_key=scope_key,
        query=query,
    )
    if snapshot is not None:
        return _campaigns_from_snapshot(snapshot.payload_json or {})

    campaigns: dict[int, dict[str, Any]] = {}
    for batch in service.search_stream(customer_id=account.customer_id, query=query):
        for row in batch.results:
            hour_dt = _parse_hour(str(row.segments.date), int(row.segments.hour))
            if hour_dt < start_dt:
                continue
            campaign_id = int(row.campaign.id)
            current = campaigns.setdefault(
                campaign_id,
                {
                    "campaign_id": campaign_id,
                    "campaign_name": row.campaign.name,
                    "campaign_resource_name": row.campaign.resource_name,
                    "currency_code": row.customer.currency_code or account.currency_code or "",
                    "channel_type": enum_name(row.campaign.advertising_channel_type),
                    "bidding_strategy_type": enum_name(row.campaign.bidding_strategy_type),
                    "target_roas": float(row.campaign.maximize_conversion_value.target_roas or 0) or None,
                    "cpc_bid_ceiling_micros": int(row.campaign.maximize_clicks.cpc_bid_ceiling_micros or 0),
                    "budget_resource_name": row.campaign_budget.resource_name,
                    "budget_amount_micros": int(row.campaign_budget.amount_micros or 0),
                    "budget_explicitly_shared": bool(row.campaign_budget.explicitly_shared),
                    "cost_micros": 0,
                    "impressions": 0,
                    "conversions": 0.0,
                    "conversion_value": 0.0,
                },
            )
            current["cost_micros"] += int(row.metrics.cost_micros or 0)
            current["impressions"] += int(row.metrics.impressions or 0)
            current["conversions"] += float(row.metrics.conversions or 0)
            current["conversion_value"] += float(row.metrics.conversions_value or 0)
    upsert_snapshot(
        session,
        dataset_key=DATASET_HOURLY_DELIVERY,
        account=account,
        scope_key=scope_key,
        query=query,
        payload={
            "customer_id": account.customer_id,
            "manager_customer_id": account.manager_customer_id,
            "lookback_hours": max(int(lookback_hours), 1),
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
            "campaigns": {str(campaign_id): campaign for campaign_id, campaign in campaigns.items()},
        },
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=HOURLY_DELIVERY_SNAPSHOT_TTL_MINUTES),
        source_job_id=source_job_id,
        row_count=len(campaigns),
    )
    return campaigns


def _empty_goal_status() -> dict[str, Any]:
    return {
        "customer_purchase_seen": False,
        "customer_purchase_active": False,
        "customer_add_to_cart_seen": False,
        "customer_add_to_cart_biddable": False,
        "campaigns": {},
    }


def _plan_goal_actions(
    session: Session,
    client,
    account: GoogleAdsAccount,
    settings: dict[str, Any],
    source_job_id: int | None = None,
) -> tuple[list[AutoPilotAction], dict[str, Any]]:
    goal_status = _empty_goal_status()
    if not parse_bool(settings.get("autopilot.purchase_goal_enabled", True)):
        return [], goal_status
    actions: list[AutoPilotAction] = []
    for row in get_or_fetch_conversion_goal_rows(session, account, client, source_job_id=source_job_id):
        category = str(row.get("category") or "")
        if category not in {"PURCHASE", "ADD_TO_CART"}:
            continue
        level = str(row.get("level") or "")
        origin = str(row.get("origin") or "")
        biddable = bool(row.get("biddable"))
        new_value = category == "PURCHASE"
        resource_name = str(row.get("resource_name") or "")
        if level == "customer":
            if category == "PURCHASE":
                goal_status["customer_purchase_seen"] = True
                goal_status["customer_purchase_active"] = goal_status["customer_purchase_active"] or biddable
            elif category == "ADD_TO_CART":
                goal_status["customer_add_to_cart_seen"] = True
                goal_status["customer_add_to_cart_biddable"] = goal_status["customer_add_to_cart_biddable"] or biddable
            if biddable == new_value:
                continue
            actions.append(
                AutoPilotAction(
                    action_type="customer_goal_purchase_mode",
                    summary=f"Set customer goal {category}/{origin} biddable={new_value}.",
                    resource_name=resource_name,
                    field="customer_conversion_goal.biddable",
                    old_value=biddable,
                    new_value=new_value,
                    evidence={"category": category, "origin": origin},
                    applyable=bool(resource_name),
                )
            )
        elif level == "campaign":
            campaign_id = int(row.get("campaign_id") or 0)
            campaign_name = str(row.get("campaign_name") or "")
            campaign_status = goal_status["campaigns"].setdefault(
                str(campaign_id),
                {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    "purchase_seen": False,
                    "purchase_active": False,
                    "add_to_cart_seen": False,
                    "add_to_cart_biddable": False,
                },
            )
            if category == "PURCHASE":
                campaign_status["purchase_seen"] = True
                campaign_status["purchase_active"] = campaign_status["purchase_active"] or biddable
            elif category == "ADD_TO_CART":
                campaign_status["add_to_cart_seen"] = True
                campaign_status["add_to_cart_biddable"] = campaign_status["add_to_cart_biddable"] or biddable
            if biddable == new_value:
                continue
            actions.append(
                AutoPilotAction(
                    action_type="campaign_goal_purchase_mode",
                    summary=f"Set campaign goal {category}/{origin} biddable={new_value}.",
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    resource_name=resource_name,
                    field="campaign_conversion_goal.biddable",
                    old_value=biddable,
                    new_value=new_value,
                    evidence={"category": category, "origin": origin},
                    applyable=bool(resource_name),
                )
            )
    return actions, goal_status


def _plan_delivery_actions(
    session: Session,
    account: GoogleAdsAccount,
    campaigns: dict[int, dict[str, Any]],
    settings: dict[str, Any],
    *,
    spend_guard: Optional[dict[str, Any]] = None,
    goal_status: Optional[dict[str, Any]] = None,
) -> list[AutoPilotAction]:
    actions: list[AutoPilotAction] = []
    threshold = int(_num(settings, "autopilot.low_impression_threshold", 1))
    now = datetime.now(timezone.utc)
    disable_after = _num(settings, "autopilot.disable_after_hours", 72)
    observation_interval = _num(settings, "autopilot.observation_interval_hours", 3)
    red_budget_cut_pct = min(max(_num(settings, "spend_guard.red_budget_cut_pct", 0.2), 0.01), 0.95)
    roas_start = _num(settings, "autopilot.target_roas_start", 3.5)
    roas_step = _num(settings, "autopilot.target_roas_step", 0.5)
    roas_floor = _num(settings, "autopilot.target_roas_floor", 1)
    spend_guard = spend_guard or {}
    goal_status = goal_status or _empty_goal_status()
    guard_status = str(spend_guard.get("status") or "green")
    guard_priority_extra = bool(spend_guard.get("priority_extra_applied"))
    guard_blocks_budget = guard_status == "red" or (guard_status == "amber" and not guard_priority_extra)
    guard_red = guard_status == "red"
    campaign_goal_status = goal_status.get("campaigns") or {}

    for campaign in campaigns.values():
        campaign_id = int(campaign["campaign_id"])
        state = _load_state(session, account, campaign_id)
        campaign_goal = campaign_goal_status.get(str(campaign_id), {})
        evidence_base = {
            **campaign,
            "purchase_goal_status": {
                "customer_purchase_seen": bool(goal_status.get("customer_purchase_seen")),
                "customer_purchase_active": bool(goal_status.get("customer_purchase_active")),
                "campaign_purchase_seen": bool(campaign_goal.get("purchase_seen")),
                "campaign_purchase_active": bool(campaign_goal.get("purchase_active")),
                "campaign_add_to_cart_biddable": bool(campaign_goal.get("add_to_cart_biddable")),
            },
            "spend_guard": spend_guard,
        }

        if guard_red and not campaign["budget_explicitly_shared"] and int(campaign["budget_amount_micros"]) > 0:
            last_cut = state.get("last_guard_cut_at")
            if _age_hours(last_cut, now) >= observation_interval:
                old_budget = int(campaign["budget_amount_micros"])
                new_budget = max(currency_to_micros(_num(settings, "optimizer.min_daily_budget", 1)), int(old_budget * (1 - red_budget_cut_pct)))
                if new_budget < old_budget:
                    actions.append(
                        AutoPilotAction(
                            action_type="spend_guard_reduce_budget",
                            summary=(
                                f"Red spend guard: reduce daily budget by {red_budget_cut_pct * 100:,.0f}% "
                                f"({micros_to_currency(old_budget):,.2f} -> {micros_to_currency(new_budget):,.2f} {campaign['currency_code']}) instead of pausing."
                            ),
                            campaign_id=campaign_id,
                            campaign_name=campaign["campaign_name"],
                            resource_name=campaign["budget_resource_name"],
                            field="campaign_budget.amount_micros",
                            old_value=old_budget,
                            new_value=new_budget,
                            evidence=evidence_base,
                        )
                    )
                    state["last_guard_cut_at"] = now.isoformat()

        if int(campaign["impressions"]) > threshold:
            if state.get("status") == "observing_open_delivery":
                restore_threshold = _restore_spend_threshold(session, settings, str(campaign["currency_code"]))
                recent_cost = int(campaign.get("cost_micros") or 0)
                state["observed_impression_at"] = state.get("observed_impression_at") or now.isoformat()
                state["observed_recent_cost_micros"] = recent_cost
                evidence = {
                    **evidence_base,
                    "restore_threshold_micros": restore_threshold,
                    "recent_cost_micros": recent_cost,
                }
                if recent_cost >= restore_threshold:
                    actions.append(
                        AutoPilotAction(
                            action_type="delivery_rescue_restore_target_roas",
                            summary=f"Restore regular Target ROAS to {roas_start:,.2f} after open-delivery spend threshold was reached.",
                            campaign_id=campaign_id,
                            campaign_name=campaign["campaign_name"],
                            resource_name=campaign["campaign_resource_name"],
                            field="campaign.maximize_conversion_value.target_roas",
                            old_value=campaign.get("target_roas"),
                            new_value=roas_start,
                            evidence=evidence,
                        )
                    )
                    state["status"] = "restore_ready"
                elif _age_hours(state.get("last_observation_at"), now) >= observation_interval:
                    actions.append(
                        AutoPilotAction(
                            action_type="delivery_rescue_observe_open_delivery",
                            summary=(
                                f"Open-delivery observation active: impressions returned, waiting for "
                                f"{micros_to_currency(restore_threshold):,.2f} {campaign['currency_code']} controlled spend before restoring ROAS/CPC."
                            ),
                            campaign_id=campaign_id,
                            campaign_name=campaign["campaign_name"],
                            resource_name=campaign["campaign_resource_name"],
                            evidence=evidence,
                            applyable=False,
                        )
                    )
                    state["last_observation_at"] = now.isoformat()
                _save_state(session, account, campaign_id, state)
                continue

            if state and state.get("status") != "recovered":
                state["status"] = "recovered"
                state["recovered_at"] = now.isoformat()
                _save_state(session, account, campaign_id, state)
            continue

        first_seen = state.get("first_seen_at")
        if not first_seen:
            first_seen = now.isoformat()
            state["first_seen_at"] = first_seen
        first_seen_dt = datetime.fromisoformat(first_seen)
        age_hours = (now - first_seen_dt).total_seconds() / 3600
        state["last_seen_at"] = now.isoformat()
        state["status"] = "rescuing"
        _save_state(session, account, campaign_id, state)

        evidence = {**evidence_base, "first_seen_at": first_seen, "age_hours": round(age_hours, 2)}
        target_budget = _target_budget(settings, str(campaign["currency_code"]))
        if guard_blocks_budget and not campaign["budget_explicitly_shared"] and int(campaign["budget_amount_micros"]) < target_budget:
            actions.append(
                AutoPilotAction(
                    action_type="spend_guard_blocks_budget_unlock",
                    summary=f"{guard_status.title()} spend guard blocks budget unlock until confirmed Odoo sales support more ad spend.",
                    campaign_id=campaign_id,
                    campaign_name=campaign["campaign_name"],
                    resource_name=campaign["budget_resource_name"],
                    evidence=evidence,
                    applyable=False,
                )
            )
        elif not campaign["budget_explicitly_shared"] and int(campaign["budget_amount_micros"]) < target_budget:
            actions.append(
                AutoPilotAction(
                    action_type="delivery_rescue_budget_unlock",
                    summary=f"Raise daily budget to {micros_to_currency(target_budget):,.2f} {campaign['currency_code']} for low-impression rescue.",
                    campaign_id=campaign_id,
                    campaign_name=campaign["campaign_name"],
                    resource_name=campaign["budget_resource_name"],
                    field="campaign_budget.amount_micros",
                    old_value=int(campaign["budget_amount_micros"]),
                    new_value=target_budget,
                    evidence=evidence,
                )
            )

        if campaign.get("target_roas"):
            current_roas = float(campaign["target_roas"])
            if current_roas > roas_start:
                next_roas = roas_start
            else:
                next_roas = max(roas_floor, current_roas - roas_step)
            actions.append(
                AutoPilotAction(
                    action_type="delivery_rescue_lower_target_roas",
                    summary=f"Lower Target ROAS toward delivery rescue path: {current_roas:,.2f} -> {next_roas:,.2f}.",
                    campaign_id=campaign_id,
                    campaign_name=campaign["campaign_name"],
                    resource_name=campaign["campaign_resource_name"],
                    field="campaign.maximize_conversion_value.target_roas",
                    old_value=current_roas,
                    new_value=next_roas,
                    evidence=evidence,
                )
            )

        if age_hours >= disable_after:
            state["status"] = "observing_open_delivery"
            state["open_delivery_started_at"] = state.get("open_delivery_started_at") or now.isoformat()
            if campaign.get("target_roas"):
                actions.append(
                    AutoPilotAction(
                        action_type="delivery_rescue_remove_target_roas",
                        summary="Remove Target ROAS cap for open-delivery observation after the configured no-impression window.",
                        campaign_id=campaign_id,
                        campaign_name=campaign["campaign_name"],
                        resource_name=campaign["campaign_resource_name"],
                        field="campaign.maximize_conversion_value.target_roas",
                        old_value=campaign.get("target_roas"),
                        new_value=None,
                        evidence=evidence,
                    )
                )
            if not campaign.get("target_roas") and _age_hours(state.get("last_observation_at"), now) >= observation_interval:
                actions.append(
                    AutoPilotAction(
                        action_type="delivery_rescue_observe_open_delivery",
                        summary="Open-delivery observation active. Campaign is not paused; autopilot will check impressions every configured interval.",
                        campaign_id=campaign_id,
                        campaign_name=campaign["campaign_name"],
                        resource_name=campaign["campaign_resource_name"],
                        evidence=evidence,
                        applyable=False,
                    )
                )
                state["last_observation_at"] = now.isoformat()
            _save_state(session, account, campaign_id, state)
            continue
    return actions


def _apply_action(client, account: GoogleAdsAccount, action: AutoPilotAction, validate_only: bool) -> dict[str, Any]:
    if not action.applyable:
        return {"mode": "skipped", "message": "Action was not applyable."}

    if action.action_type == "customer_goal_purchase_mode":
        operation = client.get_type("CustomerConversionGoalOperation")
        operation.update.resource_name = str(action.resource_name)
        operation.update.biddable = bool(action.new_value)
        operation.update_mask.paths.append("biddable")
        request = client.get_type("MutateCustomerConversionGoalsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        response = client.get_service("CustomerConversionGoalService").mutate_customer_conversion_goals(request=request)
        return {"resource_names": [result.resource_name for result in response.results]}

    if action.action_type == "campaign_goal_purchase_mode":
        operation = client.get_type("CampaignConversionGoalOperation")
        operation.update.resource_name = str(action.resource_name)
        operation.update.biddable = bool(action.new_value)
        operation.update_mask.paths.append("biddable")
        request = client.get_type("MutateCampaignConversionGoalsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.validate_only = validate_only
        response = client.get_service("CampaignConversionGoalService").mutate_campaign_conversion_goals(request=request)
        return {"resource_names": [result.resource_name for result in response.results]}

    if action.action_type in {"delivery_rescue_budget_unlock", "spend_guard_reduce_budget"}:
        operation = client.get_type("CampaignBudgetOperation")
        operation.update.resource_name = str(action.resource_name)
        operation.update.amount_micros = int(action.new_value)
        operation.update_mask.paths.append("amount_micros")
        request = client.get_type("MutateCampaignBudgetsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.partial_failure = True
        request.validate_only = validate_only
        response = client.get_service("CampaignBudgetService").mutate_campaign_budgets(request=request)
        return {"resource_names": [result.resource_name for result in response.results]}

    if action.action_type in {
        "delivery_rescue_lower_target_roas",
        "delivery_rescue_remove_target_roas",
        "delivery_rescue_restore_target_roas",
    }:
        operation = client.get_type("CampaignOperation")
        operation.update.resource_name = str(action.resource_name)
        if action.action_type in {"delivery_rescue_lower_target_roas", "delivery_rescue_restore_target_roas"}:
            operation.update.maximize_conversion_value.target_roas = float(action.new_value)
            operation.update_mask.paths.append("maximize_conversion_value.target_roas")
        elif action.action_type == "delivery_rescue_remove_target_roas":
            operation.update.maximize_conversion_value.target_roas = 0.0
            try:
                operation.update._pb.maximize_conversion_value.ClearField("target_roas")
            except Exception:
                pass
            operation.update_mask.paths.append("maximize_conversion_value.target_roas")
        request = client.get_type("MutateCampaignsRequest")
        request.customer_id = account.customer_id
        request.operations.append(operation)
        request.partial_failure = True
        request.validate_only = validate_only
        response = client.get_service("CampaignService").mutate_campaigns(request=request)
        return {"resource_names": [result.resource_name for result in response.results]}

    return {"mode": "skipped", "message": f"No applier for {action.action_type}."}


def _record_event(
    session: Session,
    account: GoogleAdsAccount,
    action: AutoPilotAction,
    status: str,
    result: Optional[dict[str, Any]] = None,
) -> None:
    session.add(
        AutoPilotEvent(
            account_id=account.id,
            campaign_id=action.campaign_id,
            campaign_name=action.campaign_name,
            action_type=action.action_type,
            status=status,
            summary=action.summary,
            evidence=action.evidence or {},
            result_json=result or {},
        )
    )


def run_account_autopilot(
    session: Session,
    account: GoogleAdsAccount,
    source_job_id: int | None = None,
) -> dict[str, Any]:
    settings = get_sync_setting_map(session)
    client = build_client(settings, account.manager_customer_id, account.connection)
    lookback_hours = int(_num(settings, "autopilot.lookback_hours", 12))
    autopilot_enabled = parse_bool(settings.get("autopilot.enabled", False))
    can_mutate = (
        autopilot_enabled
        and parse_bool(settings.get("optimizer.allow_mutations", False))
        and not parse_bool(settings.get("optimizer.dry_run", True))
    )
    validate_only = not can_mutate

    actions: list[AutoPilotAction] = []
    spend_guard: dict[str, Any] = {}
    goal_status: dict[str, Any] = _empty_goal_status()
    try:
        goal_actions, goal_status = _plan_goal_actions(
            session,
            client,
            account,
            settings,
            source_job_id=source_job_id,
        )
        actions.extend(goal_actions)
        campaigns = _hourly_campaign_rows(
            session,
            client,
            account,
            lookback_hours,
            source_job_id=source_job_id,
        )
        cost_by_currency: dict[str, float] = {}
        for campaign in campaigns.values():
            currency_code = str(campaign.get("currency_code") or account.currency_code or "UNKNOWN")
            cost_by_currency[currency_code] = cost_by_currency.get(currency_code, 0.0) + micros_to_currency(int(campaign.get("cost_micros") or 0))
        spend_guard = account_spend_guard_from_cost(
            session,
            account,
            cost_by_currency=cost_by_currency,
            hours=lookback_hours,
        )
        actions.extend(
            _plan_delivery_actions(
                session,
                account,
                campaigns,
                settings,
                spend_guard=spend_guard,
                goal_status=goal_status,
            )
        )
    except GoogleAdsException as exc:
        record_google_ads_api_error(session, exc, account=account, context="autopilot_plan")
        return {"mode": "failed", "error": str(exc), "planned": [], "applied": []}

    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for action in actions:
        status = "planned"
        result: dict[str, Any] = {"validate_only": validate_only}
        if action.applyable:
            try:
                result.update(_apply_action(client, account, action, validate_only))
                status = "validated" if validate_only else "applied"
                applied.append({"action_type": action.action_type, "campaign": action.campaign_name, "status": status})
            except GoogleAdsException as exc:
                session.commit()
                record_google_ads_api_error(
                    session,
                    exc,
                    account=account,
                    context="autopilot_apply",
                    severity="manual_action_required",
                    extra={"action_type": action.action_type, "campaign_id": action.campaign_id},
                )
                status = "failed"
                result["error"] = str(exc)
                failed.append({"action_type": action.action_type, "campaign": action.campaign_name, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - persist every autopilot failure for audit.
                session.commit()
                record_google_ads_generic_error(
                    session,
                    exc,
                    account=account,
                    context="autopilot_apply",
                    severity="manual_action_required",
                    extra={"action_type": action.action_type, "campaign_id": action.campaign_id},
                )
                status = "failed"
                result["error"] = str(exc)
                failed.append({"action_type": action.action_type, "campaign": action.campaign_name, "error": str(exc)})
        else:
            status = "skipped"
        _record_event(session, account, action, status, result)
    session.commit()

    return {
        "mode": "apply" if can_mutate else "validate_only",
        "planned_count": len(actions),
        "applied_or_validated_count": len(applied),
        "failed_count": len(failed),
        "spend_guard": spend_guard,
        "purchase_goal_status": goal_status,
        "planned": [
            {
                "action_type": action.action_type,
                "campaign": action.campaign_name,
                "summary": action.summary,
                "applyable": action.applyable,
            }
            for action in actions
        ],
        "applied_or_validated": applied,
        "failed": failed,
    }
