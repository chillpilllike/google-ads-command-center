from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import GoogleAdsAccount, OdooStore, Strategy, User
from app.app_settings import seed_app_settings
from app.security import hash_password
from app.services.google_ads_connection import ensure_default_google_ads_connection
from app.services.google_ads_script_importer import import_default_google_ads_script_sources


DEFAULT_STRATEGIES = [
    {
        "key": "recommend_guarded_changes",
        "name": "Recommend guarded changes",
        "mode": "recommend",
        "description": "Read-only optimizer pass that builds account and campaign recommendations for review.",
        "details": {
            "overview": "Runs the guarded optimizer in recommendation mode for the selected accounts. It reads Google Ads data, creates an evidence-backed report, and saves the output in Postgres without changing campaigns.",
            "covers": [
                "Recent spend, clicks, impressions, conversions, conversion value, and ROAS.",
                "Campaigns with spend but no conversion value or weak value tracking.",
                "Campaigns that look ready for controlled value scaling.",
                "Risk warnings such as zero conversions, ROAS drops, daily spend spikes, and missing primary conversion value.",
            ],
            "mechanism": [
                "Builds a one-account Google Ads config for the worker run.",
                "Runs the optimizer in read-only recommend mode.",
                "Stores report JSON, output text, and optimizer state in Postgres for audit.",
                "Records Google Ads API failures in the global API error alert table.",
            ],
            "safety": [
                "Does not mutate Google Ads campaigns.",
                "Safe first step before validation or apply runs.",
                "Keeps recommendations reviewable before any account is changed.",
            ],
            "settings": [
                "Uses the saved Google Ads connection for each selected account.",
                "Uses optimizer thresholds from the Settings page.",
                "Uses Postgres for reports, state, and error visibility.",
            ],
        },
        "is_destructive": False,
    },
    {
        "key": "validate_guardrails",
        "name": "Validate before apply",
        "mode": "validate",
        "description": "Checks that planned changes pass safety rules without mutating campaigns.",
        "details": {
            "overview": "Runs the same guarded optimizer path in validation mode. It checks whether planned changes are acceptable before the app is allowed to apply them live.",
            "covers": [
                "Account eligibility and Google Ads credentials.",
                "Campaign-level safety gates before bid, budget, or goal-related changes.",
                "Google Ads API validation responses and blocked operations.",
                "Report and state persistence for operator review.",
            ],
            "mechanism": [
                "Uses the selected account and strategy settings from Postgres.",
                "Runs validation with stop-on-error behavior so failed checks surface clearly.",
                "Saves validation output and report JSON to the strategy run.",
                "Pushes any API or limit errors into the top-right Google Ads API alert.",
            ],
            "safety": [
                "Does not apply live campaign mutations.",
                "Should be run before any destructive apply strategy.",
                "Keeps failed validation visible in background jobs and run detail pages.",
            ],
            "settings": [
                "Controlled by optimizer guardrail settings.",
                "Still respects account connection and API credential settings.",
                "Works best after the latest Google Ads sync snapshot is saved.",
            ],
        },
        "is_destructive": False,
    },
    {
        "key": "apply_guarded_optimizations",
        "name": "Apply guarded optimizations",
        "mode": "apply",
        "description": "Applies bounded optimizer changes after guardrail checks pass.",
        "details": {
            "overview": "Runs the guarded optimizer in apply mode. This is the controlled live-change path for selected accounts after recommendations and validation are acceptable.",
            "covers": [
                "Bounded optimizer actions produced by the guarded strategy engine.",
                "Campaign changes that pass safety rules and mutation settings.",
                "Run-level output, report JSON, and optimizer state persistence.",
                "Failure visibility through background jobs and Google Ads API alerts.",
            ],
            "mechanism": [
                "Builds a temporary account config from Postgres, not local storage.",
                "Runs the optimizer apply command for each selected account.",
                "Saves stdout, stderr, report JSON, and state back into strategy run rows.",
                "Records nonzero optimizer exits and API failures into the shared error table.",
            ],
            "safety": [
                "Marked as an apply strategy because it can mutate Google Ads.",
                "Requires mutation settings to allow live changes.",
                "Keeps every account run isolated, so one failed account does not hide the rest.",
            ],
            "settings": [
                "optimizer.allow_mutations controls whether live changes are permitted.",
                "optimizer.dry_run keeps apply paths non-mutating when enabled.",
                "storage.persist_optimizer_reports controls report JSON persistence.",
            ],
        },
        "is_destructive": True,
    },
    {
        "key": "autopilot_delivery_rescue",
        "name": "Autopilot delivery rescue",
        "mode": "autopilot",
        "description": "Postgres-audited autopilot for purchase-goal alignment, no-impression budget unlocks, Target ROAS lowering, and open-delivery observation.",
        "details": {
            "overview": "Automates delivery rescue for accounts where campaigns are not getting impressions while keeping every planned, validated, applied, skipped, or failed action in Postgres.",
            "covers": [
                "Purchase goal alignment: PURCHASE is made biddable and ADD_TO_CART is made non-biddable where those goals exist.",
                "Hourly delivery checks using the configured no-impression lookback window.",
                "Low or zero-impression campaigns that need budget and bidding rescue.",
                "Three-day no-impression open-delivery observation without pausing campaigns.",
                "Odoo-confirmed margin guard so ordinary accounts stay under 20% of profit margin, with a 10% extra runway only for priority accounts that show revenue.",
            ],
            "mechanism": [
                "Reads customer and campaign conversion goals from Google Ads.",
                "Reads enabled campaign delivery over the configured lookback hours.",
                "Raises non-shared budgets toward the configured INR, AUD, or default rescue budget.",
                "Lowers Target ROAS toward the configured floor while delivery is absent.",
                "After the configured no-impression window, removes Target ROAS where the API allows it, then observes every configured interval.",
                "When impressions return and controlled spend reaches the restore threshold, returns regular Target ROAS.",
                "Uses confirmed Odoo margin mapped to each Google Ads account to reduce weak budgets by 20% instead of pausing when spend exceeds the red guard.",
            ],
            "safety": [
                "Runs in validate-only mode unless autopilot is enabled, mutations are allowed, and dry run is off.",
                "Does not create Maximize Clicks rescue campaigns for any automation lane.",
                "Skips shared budget raises to avoid affecting unrelated campaigns through a shared budget.",
                "Never pauses campaigns in the red spend guard or after no-impression observation; it uses budget reductions and cap removal/restoration instead.",
                "Records every autopilot action in the Autopilot events table.",
            ],
            "settings": [
                "autopilot.enabled gates whether autopilot can apply live mutations.",
                "autopilot.lookback_hours controls the delivery window.",
                "autopilot.inr_rescue_budget, autopilot.aud_rescue_budget, and autopilot.default_rescue_budget control budget targets.",
                "autopilot.target_roas_start, target_roas_step, and target_roas_floor control ROAS lowering.",
                "autopilot.disable_after_hours controls open-delivery observation after Target ROAS lowering is exhausted.",
                "spend_guard.margin_hard_cap_ratio, priority_margin_extra_ratio, and red_budget_cut_pct control the margin-protected budget guard.",
            ],
        },
        "is_destructive": True,
    },
]

DEFAULT_ODOO_STORES = [
    {
        "name": "Nutricity USA",
        "base_url": "https://backend.nutricityusa.com",
        "database": "supplee",
        "username": "admin@nutricityusa.com",
        "website_id": None,
        "is_multisite": True,
    },
    {
        "name": "Secretgreen",
        "base_url": "https://secretgreen.com.au",
        "database": "secretgreen",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "boostgo",
        "base_url": "https://boostgo.com.au",
        "database": "boostgo",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "espot",
        "base_url": "https://espot.com.au",
        "database": "espot",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "nutrihub",
        "base_url": "https://nutrihub.ca",
        "database": "nutrihub",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "suppcity",
        "base_url": "https://suppcity.com.au",
        "database": "suppcity",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "vitagen",
        "base_url": "https://vitagen.com.au",
        "database": "vitagen",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "vitashop",
        "base_url": "https://vitashop.co.nz",
        "database": "vitashop",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
    {
        "name": "wildkart",
        "base_url": "https://wildkart.com.au",
        "database": "wildkart",
        "username": "admin",
        "website_id": None,
        "is_multisite": False,
    },
]


def clean_customer_id(value: str) -> str:
    return re.sub(r"[^\d]", "", str(value or ""))


def load_account_rows(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text())
    groups = data.get("manager_groups") or [{"manager": data["manager"], "sub_accounts": data["sub_accounts"]}]
    rows: list[dict[str, str]] = []
    for group in groups:
        manager = group["manager"]
        for account in group.get("sub_accounts", []):
            rows.append(
                {
                    "manager_name": str(manager["name"]),
                    "manager_customer_id": clean_customer_id(str(manager["customer_id"])),
                    "name": str(account["name"]),
                    "customer_id": clean_customer_id(str(account["customer_id"])),
                    "currency_code": str(account.get("currency_code") or ""),
                    "is_active": True,
                }
            )
    return rows


async def seed_database(session: AsyncSession, settings: Settings) -> None:
    existing_user = await session.scalar(select(User).where(User.email == settings.admin_email.lower()))
    if existing_user is None:
        session.add(
            User(
                email=settings.admin_email.lower(),
                password_hash=hash_password(settings.admin_password),
            )
        )

    for strategy in DEFAULT_STRATEGIES:
        stmt = insert(Strategy).values(**strategy)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Strategy.key],
            set_={
                "name": stmt.excluded.name,
                "mode": stmt.excluded.mode,
                "description": stmt.excluded.description,
                "details": stmt.excluded.details,
                "is_destructive": stmt.excluded.is_destructive,
                "is_active": True,
            },
        )
        await session.execute(stmt)

    for store_data in DEFAULT_ODOO_STORES:
        existing_store = await session.scalar(select(OdooStore).where(OdooStore.name == store_data["name"]))
        if existing_store is None:
            session.add(OdooStore(**store_data, api_key=""))
            continue
        existing_store.base_url = store_data["base_url"]
        existing_store.database = store_data["database"]
        existing_store.username = store_data["username"]
        existing_store.website_id = store_data["website_id"]
        existing_store.is_multisite = store_data["is_multisite"]
        existing_store.is_active = True

    if settings.account_config_path.exists():
        for account in load_account_rows(settings.account_config_path):
            stmt = insert(GoogleAdsAccount).values(**account)
            stmt = stmt.on_conflict_do_update(
                index_elements=[GoogleAdsAccount.customer_id],
                set_={
                    "manager_name": stmt.excluded.manager_name,
                    "manager_customer_id": stmt.excluded.manager_customer_id,
                    "name": stmt.excluded.name,
                    "currency_code": stmt.excluded.currency_code,
                },
            )
            await session.execute(stmt)

    await seed_app_settings(session, settings)
    await import_default_google_ads_script_sources(session)
    await ensure_default_google_ads_connection(session)
    await session.commit()
