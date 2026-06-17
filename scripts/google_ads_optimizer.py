#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as dt
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


ROOT = Path(__file__).resolve().parents[1]


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def clean_customer_id(value: str) -> str:
    return re.sub(r"[^\d]", "", str(value or ""))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def micros_to_currency(value: int) -> float:
    return value / 1_000_000


def currency_to_micros(value: float) -> int:
    return int(round(value * 1_000_000))


@dataclasses.dataclass(frozen=True)
class Settings:
    customer_id: str
    login_customer_id: Optional[str]
    api_version: Optional[str]
    legacy_config_path: Optional[Path]
    config_source: str
    allow_mutations: bool
    dry_run: bool
    cooldown_hours: int
    optimization_date_range: str
    baseline_date_range: str
    min_conversions_for_bid_change: float
    min_cost_for_action: float
    zero_conversion_min_clicks: int
    max_budget_change_pct: float
    max_target_roas_change_pct: float
    min_daily_budget: float
    allow_total_budget_increase: bool
    report_dir: Path
    state_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        legacy_config_path_raw = os.getenv("GOOGLE_ADS_CONFIG_PY", "").strip()
        legacy_config_path = (
            Path(legacy_config_path_raw).expanduser()
            if legacy_config_path_raw
            else None
        )
        legacy_config: Dict[str, Any] = {}
        legacy_customer_id: Optional[str] = None
        if legacy_config_path:
            legacy_config, legacy_customer_id = load_legacy_google_ads_config(
                legacy_config_path
            )

        required = [
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_CLIENT_SECRET",
            "GOOGLE_ADS_REFRESH_TOKEN",
        ]
        legacy_key_map = {
            "GOOGLE_ADS_DEVELOPER_TOKEN": "developer_token",
            "GOOGLE_ADS_CLIENT_ID": "client_id",
            "GOOGLE_ADS_CLIENT_SECRET": "client_secret",
            "GOOGLE_ADS_REFRESH_TOKEN": "refresh_token",
        }
        missing = [
            name
            for name in required
            if not os.getenv(name) and not legacy_config.get(legacy_key_map[name])
        ]
        customer_id_raw = os.getenv("GOOGLE_ADS_CUSTOMER_ID") or legacy_customer_id
        if not customer_id_raw:
            missing.append("GOOGLE_ADS_CUSTOMER_ID")
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(
                f"Missing required environment values: {joined}. "
                f"Copy .env.example to .env and fill them locally, or set "
                f"GOOGLE_ADS_CONFIG_PY to a Python file containing GOOGLE_ADS_CONFIG "
                f"and CUSTOMER_ID."
            )

        login_customer_id = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or str(
            legacy_config.get("login_customer_id") or ""
        )
        api_version = os.getenv("GOOGLE_ADS_API_VERSION", "").strip() or None
        return cls(
            customer_id=clean_customer_id(customer_id_raw),
            login_customer_id=clean_customer_id(login_customer_id)
            if login_customer_id
            else None,
            api_version=api_version,
            legacy_config_path=legacy_config_path,
            config_source="GOOGLE_ADS_CONFIG_PY" if legacy_config_path else "environment",
            allow_mutations=env_bool("GOOGLE_ADS_ALLOW_MUTATIONS", False),
            dry_run=env_bool("GOOGLE_ADS_DRY_RUN", True),
            cooldown_hours=int(env_float("GOOGLE_ADS_CHANGE_COOLDOWN_HOURS", 24)),
            optimization_date_range=os.getenv(
                "GOOGLE_ADS_OPTIMIZATION_DATE_RANGE", "LAST_7_DAYS"
            ),
            baseline_date_range=os.getenv(
                "GOOGLE_ADS_BASELINE_DATE_RANGE", "LAST_30_DAYS"
            ),
            min_conversions_for_bid_change=env_float(
                "GOOGLE_ADS_MIN_CONVERSIONS_FOR_BID_CHANGE", 5.0
            ),
            min_cost_for_action=env_float("GOOGLE_ADS_MIN_COST_FOR_ACTION", 25.0),
            zero_conversion_min_clicks=int(
                env_float("GOOGLE_ADS_ZERO_CONVERSION_MIN_CLICKS", 10)
            ),
            max_budget_change_pct=env_float("GOOGLE_ADS_MAX_BUDGET_CHANGE_PCT", 0.20),
            max_target_roas_change_pct=env_float(
                "GOOGLE_ADS_MAX_TARGET_ROAS_CHANGE_PCT", 0.10
            ),
            min_daily_budget=env_float("GOOGLE_ADS_MIN_DAILY_BUDGET", 1.0),
            allow_total_budget_increase=env_bool(
                "GOOGLE_ADS_ALLOW_TOTAL_BUDGET_INCREASE", False
            ),
            report_dir=ROOT / os.getenv("GOOGLE_ADS_REPORT_DIR", "reports"),
            state_dir=ROOT / os.getenv("GOOGLE_ADS_STATE_DIR", "state"),
        )


@dataclasses.dataclass
class CampaignSnapshot:
    campaign_id: int
    campaign_resource_name: str
    name: str
    status: str
    channel_type: str
    bidding_strategy_type: str
    budget_resource_name: str
    budget_name: str
    budget_amount_micros: int
    budget_explicitly_shared: bool
    target_roas: Optional[float]
    target_cpa_micros: Optional[int]
    impressions: int
    clicks: int
    cost_micros: int
    conversions: float
    conversions_value: float
    all_conversions: float
    all_conversions_value: float

    @property
    def cost(self) -> float:
        return micros_to_currency(self.cost_micros)

    @property
    def roas(self) -> Optional[float]:
        if self.cost_micros <= 0:
            return None
        return self.conversions_value / self.cost

    @property
    def cpa(self) -> Optional[float]:
        if self.conversions <= 0:
            return None
        return self.cost / self.conversions

    @property
    def conversion_rate(self) -> Optional[float]:
        if self.clicks <= 0:
            return None
        return self.conversions / self.clicks


@dataclasses.dataclass
class Action:
    key: str
    kind: str
    campaign_id: int
    campaign_name: str
    resource_name: str
    field: str
    old_value: float
    new_value: float
    reason: str
    confidence: str
    applyable: bool = True

    @property
    def pct_change(self) -> Optional[float]:
        if self.old_value == 0:
            return None
        return (self.new_value - self.old_value) / self.old_value


def state_dir_from_env() -> Path:
    load_dotenv(ROOT / ".env")
    return ROOT / os.getenv("GOOGLE_ADS_STATE_DIR", "state")


def run_history_path(state_dir: Path) -> Path:
    return state_dir / "google_ads_optimizer_runs.jsonl"


def load_legacy_google_ads_config(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    if not path.exists():
        raise ConfigError(f"GOOGLE_ADS_CONFIG_PY does not exist: {path}")
    tree = ast.parse(path.read_text(), filename=str(path))
    config: Optional[Dict[str, Any]] = None
    customer_id: Optional[str] = None

    for node in tree.body:
        target_names: List[str] = []
        if isinstance(node, ast.Assign):
            target_names = [
                target.id for target in node.targets if isinstance(target, ast.Name)
            ]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
            value_node = node.value
        else:
            continue

        if "GOOGLE_ADS_CONFIG" in target_names and value_node is not None:
            parsed = ast.literal_eval(value_node)
            if not isinstance(parsed, dict):
                raise ConfigError("GOOGLE_ADS_CONFIG_PY GOOGLE_ADS_CONFIG is not a dict.")
            config = parsed
        elif "CUSTOMER_ID" in target_names and value_node is not None:
            customer_id = str(ast.literal_eval(value_node))

    if config is None:
        raise ConfigError("GOOGLE_ADS_CONFIG_PY did not define GOOGLE_ADS_CONFIG.")
    return config, customer_id


def build_client(settings: Settings) -> GoogleAdsClient:
    config: Dict[str, Any] = {}
    if settings.legacy_config_path:
        legacy_config, _ = load_legacy_google_ads_config(settings.legacy_config_path)
        config.update(
            {
                key: legacy_config[key]
                for key in [
                    "developer_token",
                    "client_id",
                    "client_secret",
                    "refresh_token",
                    "use_proto_plus",
                ]
                if key in legacy_config
            }
        )

    env_key_map = {
        "developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
        "client_id": "GOOGLE_ADS_CLIENT_ID",
        "client_secret": "GOOGLE_ADS_CLIENT_SECRET",
        "refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
    }
    for config_key, env_key in env_key_map.items():
        if os.getenv(env_key):
            config[config_key] = os.environ[env_key]

    config["use_proto_plus"] = bool(config.get("use_proto_plus", True))
    if settings.login_customer_id:
        config["login_customer_id"] = settings.login_customer_id
    if settings.api_version:
        return GoogleAdsClient.load_from_dict(config, version=settings.api_version)
    return GoogleAdsClient.load_from_dict(config)


def check_connection(
    client: GoogleAdsClient, settings: Settings
) -> Dict[str, Any]:
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer.id,
          customer.descriptive_name,
          customer.currency_code,
          customer.time_zone
        FROM customer
        LIMIT 1
    """
    try:
        response = ga_service.search(customer_id=settings.customer_id, query=query)
        customer = None
        for row in response:
            customer = row.customer
            break
        return {
            "ok": True,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "customer_id": settings.customer_id,
            "login_customer_id": settings.login_customer_id,
            "descriptive_name": customer.descriptive_name if customer else None,
            "currency_code": customer.currency_code if customer else None,
            "time_zone": customer.time_zone if customer else None,
        }
    except GoogleAdsException as exc:
        return {
            "ok": False,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "customer_id": settings.customer_id,
            "login_customer_id": settings.login_customer_id,
            "error": summarize_google_ads_exception(exc),
        }


def enum_name(value: Any) -> str:
    name = getattr(value, "name", None)
    if name:
        return name
    return str(value).split(".")[-1]


def optional_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def optional_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0:
        return None
    return parsed


def date_range_days(date_range: str) -> Optional[int]:
    match = re.fullmatch(r"LAST_(\d+)_DAYS", str(date_range or "").strip())
    if not match:
        return None
    return int(match.group(1))


def campaign_snapshot_path_from_env() -> Optional[Path]:
    raw_path = os.getenv("GOOGLE_ADS_CAMPAIGN_SNAPSHOT_JSON", "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    return path if path.exists() else None


def fetch_campaigns_from_snapshot(
    settings: Settings,
    date_range: str,
) -> Optional[List[CampaignSnapshot]]:
    path = campaign_snapshot_path_from_env()
    days = date_range_days(date_range)
    if path is None or days is None:
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if clean_customer_id(payload.get("customer_id", "")) != settings.customer_id:
        return None
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return None

    dated_rows = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("metric_date"):
            continue
        try:
            metric_date = dt.date.fromisoformat(str(row["metric_date"]))
        except ValueError:
            continue
        dated_rows.append((metric_date, row))
    if not dated_rows:
        return None

    end_date = max(metric_date for metric_date, _row in dated_rows)
    start_date = end_date - dt.timedelta(days=days - 1)
    snapshot_start_raw = payload.get("start_date")
    if snapshot_start_raw:
        try:
            if dt.date.fromisoformat(str(snapshot_start_raw)) > start_date:
                return None
        except ValueError:
            return None

    grouped: Dict[int, Dict[str, Any]] = {}
    latest_rows: Dict[int, Tuple[dt.date, Dict[str, Any]]] = {}
    for metric_date, row in dated_rows:
        if metric_date < start_date or metric_date > end_date:
            continue
        if str(row.get("campaign_status") or "") != "ENABLED":
            continue
        campaign_id = int(row.get("campaign_id") or 0)
        if not campaign_id:
            continue
        metrics = grouped.setdefault(
            campaign_id,
            {
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0,
                "conversions_value": 0.0,
                "all_conversions": 0.0,
                "all_conversions_value": 0.0,
            },
        )
        metrics["impressions"] += int(row.get("impressions") or 0)
        metrics["clicks"] += int(row.get("clicks") or 0)
        metrics["cost_micros"] += int(row.get("cost_micros") or 0)
        metrics["conversions"] += float(row.get("conversions") or 0)
        metrics["conversions_value"] += float(row.get("conversions_value") or 0)
        metrics["all_conversions"] += float(row.get("all_conversions") or 0)
        metrics["all_conversions_value"] += float(row.get("all_conversions_value") or 0)
        latest = latest_rows.get(campaign_id)
        if latest is None or metric_date >= latest[0]:
            latest_rows[campaign_id] = (metric_date, row)

    campaigns: List[CampaignSnapshot] = []
    for campaign_id, metrics in grouped.items():
        latest = latest_rows[campaign_id][1]
        campaigns.append(
            CampaignSnapshot(
                campaign_id=campaign_id,
                campaign_resource_name=str(
                    latest.get("campaign_resource_name")
                    or f"customers/{settings.customer_id}/campaigns/{campaign_id}"
                ),
                name=str(latest.get("campaign_name") or f"Campaign {campaign_id}"),
                status=str(latest.get("campaign_status") or "ENABLED"),
                channel_type=str(latest.get("channel_type") or "UNKNOWN"),
                bidding_strategy_type=str(latest.get("bidding_strategy_type") or "UNKNOWN"),
                budget_resource_name=str(latest.get("budget_resource_name") or ""),
                budget_name=str(latest.get("budget_name") or ""),
                budget_amount_micros=int(latest.get("budget_amount_micros") or 0),
                budget_explicitly_shared=bool(latest.get("budget_explicitly_shared")),
                target_roas=optional_float(latest.get("target_roas")),
                target_cpa_micros=optional_int(latest.get("target_cpa_micros")),
                impressions=metrics["impressions"],
                clicks=metrics["clicks"],
                cost_micros=metrics["cost_micros"],
                conversions=metrics["conversions"],
                conversions_value=metrics["conversions_value"],
                all_conversions=metrics["all_conversions"],
                all_conversions_value=metrics["all_conversions_value"],
            )
        )
    return sorted(campaigns, key=lambda campaign: campaign.cost_micros, reverse=True)


def fetch_campaigns(
    client: GoogleAdsClient, settings: Settings, date_range: str
) -> List[CampaignSnapshot]:
    snapshot_campaigns = fetch_campaigns_from_snapshot(settings, date_range)
    if snapshot_campaigns is not None:
        return snapshot_campaigns

    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          campaign.id,
          campaign.resource_name,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign_budget.resource_name,
          campaign_budget.name,
          campaign_budget.amount_micros,
          campaign_budget.explicitly_shared,
          campaign.maximize_conversion_value.target_roas,
          campaign.maximize_conversions.target_cpa_micros,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND segments.date DURING {date_range}
    """
    rows = ga_service.search_stream(customer_id=settings.customer_id, query=query)
    campaigns: List[CampaignSnapshot] = []
    for batch in rows:
        for row in batch.results:
            campaign = row.campaign
            budget = row.campaign_budget
            metrics = row.metrics
            campaigns.append(
                CampaignSnapshot(
                    campaign_id=int(campaign.id),
                    campaign_resource_name=campaign.resource_name,
                    name=campaign.name,
                    status=enum_name(campaign.status),
                    channel_type=enum_name(campaign.advertising_channel_type),
                    bidding_strategy_type=enum_name(campaign.bidding_strategy_type),
                    budget_resource_name=budget.resource_name,
                    budget_name=budget.name,
                    budget_amount_micros=int(budget.amount_micros),
                    budget_explicitly_shared=bool(budget.explicitly_shared),
                    target_roas=optional_float(
                        campaign.maximize_conversion_value.target_roas
                    ),
                    target_cpa_micros=optional_int(
                        campaign.maximize_conversions.target_cpa_micros
                    ),
                    impressions=int(metrics.impressions),
                    clicks=int(metrics.clicks),
                    cost_micros=int(metrics.cost_micros),
                    conversions=float(metrics.conversions),
                    conversions_value=float(metrics.conversions_value),
                    all_conversions=float(metrics.all_conversions),
                    all_conversions_value=float(metrics.all_conversions_value),
                )
            )
    return campaigns


def fetch_recommendations(
    client: GoogleAdsClient, settings: Settings, limit: int = 30
) -> List[Dict[str, Any]]:
    ga_service = client.get_service("GoogleAdsService")
    query = f"""
        SELECT
          recommendation.resource_name,
          recommendation.type,
          recommendation.campaign,
          recommendation.dismissed
        FROM recommendation
        WHERE recommendation.dismissed = false
        LIMIT {limit}
    """
    recommendations: List[Dict[str, Any]] = []
    try:
        rows = ga_service.search_stream(customer_id=settings.customer_id, query=query)
        for batch in rows:
            for row in batch.results:
                rec = row.recommendation
                recommendations.append(
                    {
                        "resource_name": rec.resource_name,
                        "type": enum_name(rec.type),
                        "campaign": rec.campaign,
                        "optimization_score_uplift": None,
                    }
                )
    except GoogleAdsException as exc:
        recommendations.append({"error": summarize_google_ads_exception(exc)})
    return recommendations


def fetch_search_term_waste(
    client: GoogleAdsClient, settings: Settings, min_cost: float, date_range: str
) -> List[Dict[str, Any]]:
    ga_service = client.get_service("GoogleAdsService")
    min_cost_micros = currency_to_micros(min_cost)
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          search_term_view.search_term,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM search_term_view
        WHERE segments.date DURING {date_range}
          AND metrics.cost_micros >= {min_cost_micros}
          AND metrics.conversions = 0
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """
    terms: List[Dict[str, Any]] = []
    try:
        rows = ga_service.search_stream(customer_id=settings.customer_id, query=query)
        for batch in rows:
            for row in batch.results:
                terms.append(
                    {
                        "campaign_id": int(row.campaign.id),
                        "campaign_name": row.campaign.name,
                        "search_term": row.search_term_view.search_term,
                        "clicks": int(row.metrics.clicks),
                        "cost": micros_to_currency(int(row.metrics.cost_micros)),
                        "conversions": float(row.metrics.conversions),
                        "conversions_value": float(row.metrics.conversions_value),
                    }
                )
    except GoogleAdsException as exc:
        terms.append({"error": summarize_google_ads_exception(exc)})
    return terms


def indexed_by_id(campaigns: Iterable[CampaignSnapshot]) -> Dict[int, CampaignSnapshot]:
    return {campaign.campaign_id: campaign for campaign in campaigns}


def clamp_pct(old_value: float, new_value: float, max_change_pct: float) -> float:
    lower = old_value * (1 - max_change_pct)
    upper = old_value * (1 + max_change_pct)
    return min(max(new_value, lower), upper)


def weekly_projection(campaign: CampaignSnapshot, baseline_days: int = 30) -> Dict[str, float]:
    factor = 7 / baseline_days
    return {
        "cost": campaign.cost * factor,
        "conversions": campaign.conversions * factor,
        "conversion_value": campaign.conversions_value * factor,
    }


def merge_duplicate_actions(actions: List[Action]) -> List[Action]:
    merged: Dict[str, Action] = {}
    passthrough: List[Action] = []
    for action in actions:
        if not action.applyable or action.kind == "note":
            passthrough.append(action)
            continue
        existing = merged.get(action.key)
        if existing is None:
            merged[action.key] = action
            continue

        if action.field == "amount_micros":
            if action.new_value < action.old_value or existing.new_value < existing.old_value:
                existing.new_value = min(existing.new_value, action.new_value)
            else:
                existing.new_value = max(existing.new_value, action.new_value)
        elif action.field == "maximize_conversion_value.target_roas":
            if action.new_value > action.old_value or existing.new_value > existing.old_value:
                existing.new_value = max(existing.new_value, action.new_value)
            else:
                existing.new_value = min(existing.new_value, action.new_value)
        elif action.field == "maximize_conversions.target_cpa_micros":
            if action.new_value < action.old_value or existing.new_value < existing.old_value:
                existing.new_value = min(existing.new_value, action.new_value)
            else:
                existing.new_value = max(existing.new_value, action.new_value)
        existing.reason = f"{existing.reason} Also: {action.reason}"
        if action.confidence == "high":
            existing.confidence = "high"
    return list(merged.values()) + passthrough


def add_budget_action(
    actions: List[Action],
    campaign: CampaignSnapshot,
    new_budget_micros: int,
    reason: str,
    confidence: str,
    settings: Settings,
) -> None:
    if campaign.budget_explicitly_shared:
        return
    old_budget = campaign.budget_amount_micros
    min_budget = currency_to_micros(settings.min_daily_budget)
    bounded = max(new_budget_micros, min_budget)
    bounded = int(clamp_pct(old_budget, bounded, settings.max_budget_change_pct))
    if abs(bounded - old_budget) < 1:
        return
    actions.append(
        Action(
            key=f"budget:{campaign.budget_resource_name}",
            kind="budget",
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.name,
            resource_name=campaign.budget_resource_name,
            field="amount_micros",
            old_value=float(old_budget),
            new_value=float(bounded),
            reason=reason,
            confidence=confidence,
        )
    )


def add_campaign_target_action(
    actions: List[Action],
    campaign: CampaignSnapshot,
    field: str,
    old_value: float,
    new_value: float,
    max_change_pct: float,
    reason: str,
    confidence: str,
) -> None:
    bounded = clamp_pct(old_value, new_value, max_change_pct)
    if math.isclose(bounded, old_value, rel_tol=0.0001, abs_tol=0.0001):
        return
    actions.append(
        Action(
            key=f"{field}:{campaign.campaign_resource_name}",
            kind="campaign",
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.name,
            resource_name=campaign.campaign_resource_name,
            field=field,
            old_value=old_value,
            new_value=bounded,
            reason=reason,
            confidence=confidence,
        )
    )


def add_exact_campaign_target_action(
    actions: List[Action],
    campaign: CampaignSnapshot,
    field: str,
    old_value: float,
    new_value: float,
    reason: str,
    confidence: str,
) -> None:
    if math.isclose(new_value, old_value, rel_tol=0.0001, abs_tol=0.0001):
        return
    actions.append(
        Action(
            key=f"{field}:{campaign.campaign_resource_name}",
            kind="campaign",
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.name,
            resource_name=campaign.campaign_resource_name,
            field=field,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            confidence=confidence,
        )
    )


def plan_actions(
    optimization_window: List[CampaignSnapshot],
    baseline_window: List[CampaignSnapshot],
    settings: Settings,
) -> List[Action]:
    baseline = indexed_by_id(baseline_window)
    actions: List[Action] = []

    for campaign in optimization_window:
        baseline_campaign = baseline.get(campaign.campaign_id)
        baseline_roas = baseline_campaign.roas if baseline_campaign else None
        baseline_week = (
            weekly_projection(baseline_campaign) if baseline_campaign else None
        )
        roas = campaign.roas
        cost = campaign.cost
        conversions = campaign.conversions
        budget = campaign.budget_amount_micros
        spend_not_down = (
            baseline_week is not None and cost >= baseline_week["cost"] * 0.90
        )
        conversion_drop = (
            baseline_week is not None
            and baseline_week["conversions"] >= 1
            and conversions <= baseline_week["conversions"] * 0.60
            and spend_not_down
        )
        value_drop = (
            baseline_week is not None
            and baseline_week["conversion_value"] > 0
            and campaign.conversions_value <= baseline_week["conversion_value"] * 0.60
            and spend_not_down
        )

        if campaign.impressions <= 0 and campaign.target_roas and campaign.target_roas > 1.0:
            add_exact_campaign_target_action(
                actions,
                campaign,
                "maximize_conversion_value.target_roas",
                campaign.target_roas,
                1.0,
                "Last 7 days had 0 impressions; reducing target ROAS to 100% unlocks delivery for low-traffic campaigns.",
                "high",
            )
            continue

        if (
            cost >= settings.min_cost_for_action
            and campaign.clicks >= settings.zero_conversion_min_clicks
            and conversions <= 0
        ):
            if campaign.target_roas:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversion_value.target_roas",
                    campaign.target_roas,
                    campaign.target_roas * 1.08,
                    settings.max_target_roas_change_pct,
                    "Last 7 days spent without conversions; raising target ROAS asks bidding to buy only higher-value traffic.",
                    "high",
                )
            elif campaign.target_cpa_micros:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversions.target_cpa_micros",
                    float(campaign.target_cpa_micros),
                    float(campaign.target_cpa_micros * 0.92),
                    settings.max_target_roas_change_pct,
                    "Last 7 days spent without conversions; tightening target CPA reduces inefficient traffic.",
                    "high",
                )
            add_budget_action(
                actions,
                campaign,
                int(budget * 0.80),
                "Last 7 days spent meaningfully with clicks but no conversions; budget cut protects spend immediately.",
                "high",
                settings,
            )
            continue

        if conversion_drop or value_drop:
            if campaign.target_roas:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversion_value.target_roas",
                    campaign.target_roas,
                    campaign.target_roas * 1.05,
                    settings.max_target_roas_change_pct,
                    "Last 7 days show sales/value dropping while spend did not fall; tightening ROAS target prioritizes efficiency.",
                    "medium",
                )
            elif campaign.target_cpa_micros:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversions.target_cpa_micros",
                    float(campaign.target_cpa_micros),
                    float(campaign.target_cpa_micros * 0.95),
                    settings.max_target_roas_change_pct,
                    "Last 7 days show sales dropping while spend did not fall; tightening CPA target prioritizes efficiency.",
                    "medium",
                )
            add_budget_action(
                actions,
                campaign,
                int(budget * 0.90),
                "Weekly performance deteriorated versus the 30-day baseline, so spend is reduced until conversion quality recovers.",
                "medium",
                settings,
            )

        if roas is None:
            continue

        if campaign.target_roas:
            target = campaign.target_roas
            enough_volume = conversions >= settings.min_conversions_for_bid_change
            baseline_ok = baseline_roas is None or baseline_roas >= target * 0.95
            if enough_volume and roas >= target * 1.30 and baseline_ok:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversion_value.target_roas",
                    target,
                    max(1.01, target * 0.94),
                    settings.max_target_roas_change_pct,
                    "Last 7 days are materially above target ROAS; lowering target ROAS can unlock more volume while staying efficiency-led.",
                    "medium",
                )
                add_budget_action(
                    actions,
                    campaign,
                    int(budget * 1.10),
                    "Last 7 days show strong ROAS and enough conversion volume; budget increase is capped and funded by reductions elsewhere.",
                    "medium",
                    settings,
                )
            elif cost >= settings.min_cost_for_action and roas <= target * 0.75:
                add_campaign_target_action(
                    actions,
                    campaign,
                    "maximize_conversion_value.target_roas",
                    target,
                    target * 1.06,
                    settings.max_target_roas_change_pct,
                    "Last 7 days are below target ROAS; raising target ROAS asks Smart Bidding to prioritize efficiency.",
                    "medium",
                )
                add_budget_action(
                    actions,
                    campaign,
                    int(budget * 0.90),
                    "Last 7 days are below target ROAS with meaningful spend; reducing budget protects spend while bidding tightens.",
                    "medium",
                    settings,
                )
        elif campaign.target_cpa_micros:
            target_cpa = campaign.target_cpa_micros
            cpa = campaign.cpa
            if cpa and conversions >= settings.min_conversions_for_bid_change:
                if cpa <= micros_to_currency(target_cpa) * 0.75:
                    add_campaign_target_action(
                        actions,
                        campaign,
                        "maximize_conversions.target_cpa_micros",
                        float(target_cpa),
                        float(target_cpa * 1.06),
                        settings.max_target_roas_change_pct,
                        "Last 7 days CPA is comfortably below target; relaxing target CPA can unlock more volume.",
                        "medium",
                    )
                elif cpa >= micros_to_currency(target_cpa) * 1.30:
                    add_campaign_target_action(
                        actions,
                        campaign,
                        "maximize_conversions.target_cpa_micros",
                        float(target_cpa),
                        float(target_cpa * 0.94),
                        settings.max_target_roas_change_pct,
                        "Last 7 days CPA is above target; tightening target CPA should reduce inefficient spend.",
                        "medium",
                    )
                    add_budget_action(
                        actions,
                        campaign,
                        int(budget * 0.90),
                        "Last 7 days CPA is above target with enough conversion data; reducing budget limits waste.",
                        "medium",
                        settings,
                    )
        elif (
            conversions >= settings.min_conversions_for_bid_change
            and roas >= 3.0
            and campaign.channel_type in {"PERFORMANCE_MAX", "SEARCH", "SHOPPING"}
        ):
            actions.append(
                Action(
                    key=f"note:{campaign.campaign_resource_name}",
                    kind="note",
                    campaign_id=campaign.campaign_id,
                    campaign_name=campaign.name,
                    resource_name=campaign.campaign_resource_name,
                    field="bidding_strategy",
                    old_value=0,
                    new_value=0,
                    reason="Last 7 days show conversion value but no target ROAS. Consider testing Maximize Conversion Value with a conservative target.",
                    confidence="low",
                    applyable=False,
                )
            )

    return constrain_budget_increases(merge_duplicate_actions(actions), settings)


def constrain_budget_increases(actions: List[Action], settings: Settings) -> List[Action]:
    if settings.allow_total_budget_increase:
        return actions

    budget_actions = [
        action
        for action in actions
        if action.kind == "budget" and action.applyable and action.field == "amount_micros"
    ]
    decreases = sum(
        action.old_value - action.new_value
        for action in budget_actions
        if action.new_value < action.old_value
    )
    increases = [
        action for action in budget_actions if action.new_value > action.old_value
    ]
    total_increase = sum(action.new_value - action.old_value for action in increases)
    if total_increase <= decreases or total_increase == 0:
        return actions

    if decreases <= 0:
        for action in increases:
            action.applyable = False
            action.reason += " Skipped by guardrail: no offsetting budget reductions available."
        return actions

    scale = decreases / total_increase
    for action in increases:
        delta = action.new_value - action.old_value
        action.new_value = action.old_value + int(delta * scale)
        action.reason += " Scaled by guardrail so total daily budget does not increase."
    return actions


def load_state(settings: Settings) -> Dict[str, Any]:
    state_file = settings.state_dir / "google_ads_optimizer_state.json"
    if not state_file.exists():
        return {"actions": {}}
    return json.loads(state_file.read_text())


def save_state(settings: Settings, state: Dict[str, Any]) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = settings.state_dir / "google_ads_optimizer_state.json"
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def filter_cooldown(actions: List[Action], settings: Settings, state: Dict[str, Any]) -> List[Action]:
    now = dt.datetime.now(dt.timezone.utc)
    filtered: List[Action] = []
    for action in actions:
        if not action.applyable:
            filtered.append(action)
            continue
        last_raw = state.get("actions", {}).get(action.key)
        if last_raw:
            last = dt.datetime.fromisoformat(last_raw)
            age = now - last
            if age < dt.timedelta(hours=settings.cooldown_hours):
                action.applyable = False
                action.reason += f" Skipped by cooldown; last changed {round(age.total_seconds() / 3600, 1)} hours ago."
        filtered.append(action)
    return filtered


def record_applied(actions: Iterable[Action], settings: Settings, state: Dict[str, Any]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    action_state = state.setdefault("actions", {})
    for action in actions:
        if action.applyable:
            action_state[action.key] = now
    save_state(settings, state)


def apply_budget_actions(
    client: GoogleAdsClient,
    settings: Settings,
    actions: List[Action],
    validate_only: bool,
) -> List[str]:
    budget_service = client.get_service("CampaignBudgetService")
    operations = []
    for action in actions:
        operation = client.get_type("CampaignBudgetOperation")
        budget = operation.update
        budget.resource_name = action.resource_name
        budget.amount_micros = int(action.new_value)
        operation.update_mask.paths.append("amount_micros")
        operations.append(operation)

    if not operations:
        return []
    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = settings.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = budget_service.mutate_campaign_budgets(request=request)
    return [result.resource_name for result in response.results]


def apply_campaign_actions(
    client: GoogleAdsClient,
    settings: Settings,
    actions: List[Action],
    validate_only: bool,
) -> List[str]:
    campaign_service = client.get_service("CampaignService")
    operations = []
    for action in actions:
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = action.resource_name
        if action.field == "maximize_conversion_value.target_roas":
            campaign.maximize_conversion_value.target_roas = float(action.new_value)
            operation.update_mask.paths.append("maximize_conversion_value.target_roas")
        elif action.field == "maximize_conversions.target_cpa_micros":
            campaign.maximize_conversions.target_cpa_micros = int(action.new_value)
            operation.update_mask.paths.append("maximize_conversions.target_cpa_micros")
        else:
            continue
        operations.append(operation)

    if not operations:
        return []
    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = settings.customer_id
    request.operations.extend(operations)
    request.partial_failure = True
    request.validate_only = validate_only
    response = campaign_service.mutate_campaigns(request=request)
    return [result.resource_name for result in response.results]


def execute_actions(
    client: GoogleAdsClient,
    settings: Settings,
    actions: List[Action],
    validate_only: bool,
) -> Dict[str, Any]:
    applyable = [action for action in actions if action.applyable]
    if not validate_only and (settings.dry_run or not settings.allow_mutations):
        return {
            "mode": "dry_run",
            "message": "Mutations skipped. Set GOOGLE_ADS_ALLOW_MUTATIONS=true and GOOGLE_ADS_DRY_RUN=false to apply.",
            "applied": [],
        }

    budget_actions = [action for action in applyable if action.kind == "budget"]
    campaign_actions = [action for action in applyable if action.kind == "campaign"]
    applied_budget = apply_budget_actions(client, settings, budget_actions, validate_only)
    applied_campaign = apply_campaign_actions(
        client, settings, campaign_actions, validate_only
    )
    return {
        "mode": "validate_only" if validate_only else "apply",
        "applied": applied_budget + applied_campaign,
    }


def action_change_summary(action: Action) -> Dict[str, Any]:
    data = {
        "campaign_id": action.campaign_id,
        "campaign_name": action.campaign_name,
        "kind": action.kind,
        "field": action.field,
        "status": "planned" if action.applyable else "skipped_or_review",
        "old_value": action.old_value,
        "new_value": action.new_value,
        "reason": action.reason,
        "confidence": action.confidence,
    }
    if action.field == "amount_micros":
        data["old_display"] = micros_to_currency(int(action.old_value))
        data["new_display"] = micros_to_currency(int(action.new_value))
        data["display_unit"] = "daily_budget"
    elif action.field.endswith("target_cpa_micros"):
        data["old_display"] = micros_to_currency(int(action.old_value))
        data["new_display"] = micros_to_currency(int(action.new_value))
        data["display_unit"] = "target_cpa"
    else:
        data["old_display"] = action.old_value
        data["new_display"] = action.new_value
        data["display_unit"] = "raw"
    return data


def action_to_dict(action: Action) -> Dict[str, Any]:
    data = dataclasses.asdict(action)
    if action.field == "amount_micros":
        data["old_daily_budget"] = micros_to_currency(int(action.old_value))
        data["new_daily_budget"] = micros_to_currency(int(action.new_value))
    elif action.field.endswith("target_cpa_micros"):
        data["old_target_cpa"] = micros_to_currency(int(action.old_value))
        data["new_target_cpa"] = micros_to_currency(int(action.new_value))
    return data


def append_run_history(
    settings: Settings,
    mode: str,
    report_path: Optional[Path],
    connection_status: Dict[str, Any],
    optimization_campaigns: List[CampaignSnapshot],
    baseline_campaigns: List[CampaignSnapshot],
    actions: List[Action],
    execution: Dict[str, Any],
) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    applied_resources = set(execution.get("applied", []))
    record = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "report_path": str(report_path) if report_path else None,
        "connection": connection_status,
        "summary": summarize_account(
            optimization_campaigns, settings.optimization_date_range
        ),
        "baseline_summary": summarize_account(
            baseline_campaigns, settings.baseline_date_range
        ),
        "planned_changes": [
            action_change_summary(action) for action in actions if action.applyable
        ],
        "skipped_or_review_changes": [
            action_change_summary(action) for action in actions if not action.applyable
        ],
        "applied_resources": sorted(applied_resources),
        "applied_changes": [
            action_change_summary(action)
            for action in actions
            if action.applyable and action.resource_name in applied_resources
        ],
        "execution": execution,
    }
    with run_history_path(settings.state_dir).open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def load_run_history(limit: int) -> List[Dict[str, Any]]:
    path = run_history_path(state_dir_from_env())
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    records = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def append_config_failure_history(mode: str, error: str) -> None:
    state_dir = state_dir_from_env()
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "report_path": None,
        "connection": {
            "ok": False,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "error": error,
        },
        "summary": {},
        "baseline_summary": {},
        "planned_changes": [],
        "skipped_or_review_changes": [],
        "applied_resources": [],
        "applied_changes": [],
        "execution": {
            "mode": mode,
            "applied": [],
            "message": "Configuration missing; Google Ads connection was not attempted.",
        },
    }
    with run_history_path(state_dir).open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def campaign_to_dict(campaign: CampaignSnapshot) -> Dict[str, Any]:
    return {
        "campaign_id": campaign.campaign_id,
        "name": campaign.name,
        "channel_type": campaign.channel_type,
        "bidding_strategy_type": campaign.bidding_strategy_type,
        "daily_budget": micros_to_currency(campaign.budget_amount_micros),
        "shared_budget": campaign.budget_explicitly_shared,
        "target_roas": campaign.target_roas,
        "target_cpa": micros_to_currency(campaign.target_cpa_micros)
        if campaign.target_cpa_micros
        else None,
        "impressions": campaign.impressions,
        "clicks": campaign.clicks,
        "cost": campaign.cost,
        "conversions": campaign.conversions,
        "conversions_value": campaign.conversions_value,
        "roas": campaign.roas,
        "cpa": campaign.cpa,
        "conversion_rate": campaign.conversion_rate,
    }


def write_report(
    settings: Settings,
    mode: str,
    connection_status: Dict[str, Any],
    optimization_campaigns: List[CampaignSnapshot],
    baseline_campaigns: List[CampaignSnapshot],
    actions: List[Action],
    recommendations: List[Dict[str, Any]],
    search_term_waste: List[Dict[str, Any]],
    execution: Dict[str, Any],
) -> Path:
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.report_dir / f"google_ads_optimizer_{stamp}.json"
    report = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "customer_id": settings.customer_id,
        "connection": connection_status,
        "guardrails": {
            "allow_mutations": settings.allow_mutations,
            "dry_run": settings.dry_run,
            "cooldown_hours": settings.cooldown_hours,
            "config_source": settings.config_source,
            "api_version": settings.api_version or "default_installed",
            "optimization_date_range": settings.optimization_date_range,
            "baseline_date_range": settings.baseline_date_range,
            "max_budget_change_pct": settings.max_budget_change_pct,
            "max_target_roas_change_pct": settings.max_target_roas_change_pct,
            "allow_total_budget_increase": settings.allow_total_budget_increase,
        },
        "summary": summarize_account(
            optimization_campaigns, settings.optimization_date_range
        ),
        "baseline_summary": summarize_account(
            baseline_campaigns, settings.baseline_date_range
        ),
        "optimization_campaigns": [
            campaign_to_dict(campaign) for campaign in optimization_campaigns
        ],
        "baseline_campaigns": [
            campaign_to_dict(campaign) for campaign in baseline_campaigns
        ],
        "actions": [action_to_dict(action) for action in actions],
        "recommendations": recommendations,
        "search_term_waste": search_term_waste,
        "execution": execution,
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def summarize_account(campaigns: List[CampaignSnapshot], date_range: str) -> Dict[str, Any]:
    total_cost_micros = sum(campaign.cost_micros for campaign in campaigns)
    total_value = sum(campaign.conversions_value for campaign in campaigns)
    total_conversions = sum(campaign.conversions for campaign in campaigns)
    total_budget_micros = sum(
        campaign.budget_amount_micros
        for campaign in campaigns
        if not campaign.budget_explicitly_shared
    )
    return {
        "date_range": date_range,
        "active_campaigns": len(campaigns),
        "total_daily_budget_non_shared": micros_to_currency(total_budget_micros),
        "cost": micros_to_currency(total_cost_micros),
        "conversions": total_conversions,
        "conversion_value": total_value,
        "roas": total_value / micros_to_currency(total_cost_micros)
        if total_cost_micros
        else None,
    }


def summarize_google_ads_exception(exc: GoogleAdsException) -> str:
    pieces = [f"request_id={exc.request_id}", f"failure={exc.failure}"]
    return "; ".join(pieces)


def print_console_summary(
    report_path: Path,
    connection_status: Dict[str, Any],
    campaigns: List[CampaignSnapshot],
    date_range: str,
    actions: List[Action],
    execution: Dict[str, Any],
) -> None:
    summary = summarize_account(campaigns, date_range)
    print(
        json.dumps(
            {
                "connection": connection_status,
                "summary": summary,
                "execution": execution,
            },
            indent=2,
        )
    )
    print(f"Report: {report_path}")
    if not actions:
        print("No optimization actions met the current guardrails.")
        return
    print("Planned actions:")
    for action in actions:
        status = "applyable" if action.applyable else "review"
        if action.field == "amount_micros":
            old_display = f"{micros_to_currency(int(action.old_value)):.2f}"
            new_display = f"{micros_to_currency(int(action.new_value)):.2f}"
        elif action.field.endswith("target_cpa_micros"):
            old_display = f"{micros_to_currency(int(action.old_value)):.2f}"
            new_display = f"{micros_to_currency(int(action.new_value)):.2f}"
        else:
            old_display = f"{action.old_value:.4g}"
            new_display = f"{action.new_value:.4g}"
        print(
            f"- [{status}] {action.campaign_name}: {action.field} "
            f"{old_display} -> {new_display}. {action.reason}"
        )


def print_run_history(limit: int) -> int:
    records = load_run_history(limit)
    if not records:
        print("No run history found yet.")
        return 0

    for record in records:
        connection = record.get("connection", {})
        ok = "successful" if connection.get("ok") else "failed"
        print(
            f"{record.get('created_at')} | mode={record.get('mode')} | "
            f"connection={ok} | report={record.get('report_path')}"
        )
        summary = record.get("summary", {})
        if summary:
            print(
                "  "
                f"{summary.get('date_range')}: cost={summary.get('cost')}, "
                f"conversions={summary.get('conversions')}, "
                f"value={summary.get('conversion_value')}, roas={summary.get('roas')}"
            )
        else:
            print("  metrics unavailable for this run")
        planned = record.get("planned_changes", [])
        applied = record.get("applied_changes", [])
        skipped = record.get("skipped_or_review_changes", [])
        print(
            f"  planned_changes={len(planned)} applied_changes={len(applied)} "
            f"skipped_or_review={len(skipped)}"
        )
        for change in applied:
            print(
                "  applied: "
                f"{change.get('campaign_name')} {change.get('field')} "
                f"{change.get('old_display')} -> {change.get('new_display')}"
            )
        if not applied and planned:
            print("  no applied changes in this run; planned changes were not mutated")
        if connection.get("error"):
            print(f"  connection_error={connection.get('error')}")
    return 0


def pmax_clicks_message() -> str:
    return (
        "Performance Max cannot use a Maximize Clicks target in Google Ads API. "
        "Use Performance Max with Maximize Conversions or Maximize Conversion Value, "
        "or create a separate Search campaign with Maximize Clicks."
    )


def run(mode: str) -> int:
    if mode == "history":
        return print_run_history(10)

    load_dotenv(ROOT / ".env")
    settings = Settings.from_env()
    client = build_client(settings)
    connection_status = check_connection(client, settings)

    if mode == "connection":
        execution = {
            "mode": mode,
            "applied": [],
            "message": "Connection successful."
            if connection_status.get("ok")
            else "Connection failed.",
        }
        report_path = write_report(
            settings,
            mode,
            connection_status,
            [],
            [],
            [],
            [],
            [],
            execution,
        )
        append_run_history(
            settings,
            mode,
            report_path,
            connection_status,
            [],
            [],
            [],
            execution,
        )
        print_console_summary(
            report_path,
            connection_status,
            [],
            settings.optimization_date_range,
            [],
            execution,
        )
        return 0 if connection_status.get("ok") else 2

    if not connection_status.get("ok"):
        execution = {
            "mode": mode,
            "applied": [],
            "message": "Google Ads connection failed; optimization skipped.",
        }
        report_path = write_report(
            settings,
            mode,
            connection_status,
            [],
            [],
            [],
            [],
            [],
            execution,
        )
        append_run_history(
            settings,
            mode,
            report_path,
            connection_status,
            [],
            [],
            [],
            execution,
        )
        print_console_summary(
            report_path,
            connection_status,
            [],
            settings.optimization_date_range,
            [],
            execution,
        )
        return 2

    optimization_campaigns = fetch_campaigns(
        client, settings, settings.optimization_date_range
    )
    baseline_campaigns = fetch_campaigns(client, settings, settings.baseline_date_range)
    state = load_state(settings)
    actions = filter_cooldown(
        plan_actions(optimization_campaigns, baseline_campaigns, settings),
        settings,
        state,
    )
    recommendations = fetch_recommendations(client, settings)
    search_term_waste = fetch_search_term_waste(
        client, settings, settings.min_cost_for_action, settings.optimization_date_range
    )

    validate_only = mode == "validate"
    execution = {"mode": mode, "applied": []}
    if mode in {"validate", "apply"}:
        execution = execute_actions(client, settings, actions, validate_only)
        if mode == "apply" and execution.get("mode") == "apply":
            record_applied(actions, settings, state)
    elif mode == "pmax-clicks":
        execution = {"mode": mode, "message": pmax_clicks_message(), "applied": []}

    report_path = write_report(
        settings,
        mode,
        connection_status,
        optimization_campaigns,
        baseline_campaigns,
        actions,
        recommendations,
        search_term_waste,
        execution,
    )
    append_run_history(
        settings,
        mode,
        report_path,
        connection_status,
        optimization_campaigns,
        baseline_campaigns,
        actions,
        execution,
    )
    print_console_summary(
        report_path,
        connection_status,
        optimization_campaigns,
        settings.optimization_date_range,
        actions,
        execution,
    )
    if mode == "pmax-clicks":
        print(pmax_clicks_message())
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guarded Google Ads optimizer")
    parser.add_argument(
        "mode",
        choices=["recommend", "validate", "apply", "connection", "history", "pmax-clicks"],
        help=(
            "recommend writes a report only; validate sends validate_only mutations; "
            "apply mutates only when env guardrails allow it; connection checks "
            "Google Ads access; history shows recent run changes; pmax-clicks "
            "explains why PMax cannot target clicks."
        ),
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args.mode)
    except ConfigError as exc:
        append_config_failure_history(args.mode, str(exc))
        print(str(exc), file=sys.stderr)
        return 1
    except GoogleAdsException as exc:
        print(summarize_google_ads_exception(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
