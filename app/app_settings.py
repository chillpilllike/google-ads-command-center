from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import ROOT_DIR, Settings, get_settings
from app.models import AppSetting, GoogleAdsConnection


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    category: str
    input_type: str
    default: Any
    help_text: str
    sensitive: bool = False
    env_name: Optional[str] = None


RESTRICTED_PAGE_FEED_TITLE_TERMS_DEFAULT = """5 Day Forecast 1600mg
CDP-choline
Citicoline
DMAE
DOPA Mucuna
Extreme Male Pills
Fat Burner HCA
Fatzorb
Ghost Legend
Gluta Lipo
Graviola Leaf
Horny Goat Weed
Jetfuel Pyro
L-DOPA
Longjack Complex
Maximize Intense
Mucuna
Mucuna Pruriens
Mucuna Pruriens Extract
NOREPINEPHRINE
Phen375
Phenemine
Phenterdrine
Phentermin
Phentramine
Phentremin
Provigor
Tongkat Ali
Tongkat Ali Extract
Tongkat Ali Root
VIGRA
Virility Max
Xiang Sha Liu Jun Zi Tang
Xiao Chai Hu Tang
Yohimbe
DHEA
Slim-30
Alteril All Natural Sleep Aid
Deca 200
Yoni Detox Pearls
Shilajit Capsules
Diindolylmethane DIM
3XT-PUMP
Tums Ultra Strength 1000
Artemisinin
NAD+ Gold
Qing Qi Hua Tan Wan
Halovar
Promina Ginseng Pearl Cream
patanjali and thyrogrit
Red Eye Relief
Natural Dieta
HGH for Men
Testrol Gold ES
3XT-PUMP
Dermatitis Cream
Gastro-Soothe
Organic Germanium
5-HTP 50 mg
5-HTP
LipoRUSH
5-HTP Plus
dolotrex
Calcifediol
Core Fury
Triptófano
DMAA
Pe Min Kan Wan
Westside Barbell Smelling Salts
vRox
Shotgun 5X
Sumatra Slim Belly Tonic
Time Release 5-HTP
Pure Caffeine Powder
Vitamin B17
LOMOTIL
DIPHENOXYLATE
Libido-Max Pink
Neuro Support Complex
Thyro-Boost
Nutrabol
Methyl Andro Hardcore
HerSolution
Ban Xia
Dang Gui Si Ni
OXYCODONE
HYDROCODONE
Xiang Sha Liu Jun Wan
VIGORA 100
1-Testosterone
Ammonia Smelling Salt
Endorush
Chuan Xiong Cha Tiao Wan
Super Slimming
Hemorrhoid Cream
BOTOX and Botulinum toxin
Sarcosine
Forebrain Focus
LeanFire PM
Zeolite Sorbolit Detox
Myostine
Insane Pump
Cholestene
Double Potency 5-HTP Extra
Steel-Libido RED
Isa-Test GF
Turkey Tail Mushroom Capsule
Motiv-8 Muscle
Prime HGH Secretion Activator
Shilajit+
Similasan Kids Allergy Eye Relief
OxyElite Pro
Bottled Insanity XL
TRAMADOL
CARBIDOPA
BUPRENORPHINE
MORPHINE
Organic Germanium
3XT-PUMPwhjygrtophht"""


SETTING_DEFINITIONS = [
    SettingDefinition(
        "google_ads.developer_token",
        "Developer token",
        "Google Ads credentials",
        "password",
        "",
        "Google Ads API developer token.",
        sensitive=True,
        env_name="GOOGLE_ADS_DEVELOPER_TOKEN",
    ),
    SettingDefinition(
        "google_ads.client_id",
        "OAuth client ID",
        "Google Ads credentials",
        "text",
        "",
        "OAuth client ID for Google Ads.",
        sensitive=True,
        env_name="GOOGLE_ADS_CLIENT_ID",
    ),
    SettingDefinition(
        "google_ads.client_secret",
        "OAuth client secret",
        "Google Ads credentials",
        "password",
        "",
        "OAuth client secret for Google Ads.",
        sensitive=True,
        env_name="GOOGLE_ADS_CLIENT_SECRET",
    ),
    SettingDefinition(
        "google_ads.refresh_token",
        "Refresh token",
        "Google Ads credentials",
        "password",
        "",
        "Refresh token used by the optimizer worker.",
        sensitive=True,
        env_name="GOOGLE_ADS_REFRESH_TOKEN",
    ),
    SettingDefinition(
        "google_ads.api_version",
        "API version",
        "Google Ads credentials",
        "text",
        "",
        "Leave empty to use the version bundled with the installed google-ads package.",
        env_name="GOOGLE_ADS_API_VERSION",
    ),
    SettingDefinition(
        "optimizer.allow_mutations",
        "Allow mutations",
        "Safety controls",
        "checkbox",
        False,
        "Master switch. Keep off until you are ready to mutate Google Ads campaigns.",
        env_name="GOOGLE_ADS_ALLOW_MUTATIONS",
    ),
    SettingDefinition(
        "optimizer.dry_run",
        "Dry run",
        "Safety controls",
        "checkbox",
        True,
        "When enabled, apply mode will not make live Google Ads changes.",
        env_name="GOOGLE_ADS_DRY_RUN",
    ),
    SettingDefinition(
        "optimizer.allow_total_budget_increase",
        "Allow total budget increase",
        "Safety controls",
        "checkbox",
        False,
        "Allows winning-campaign increases even when not offset by reductions elsewhere.",
        env_name="GOOGLE_ADS_ALLOW_TOTAL_BUDGET_INCREASE",
    ),
    SettingDefinition(
        "live_campaign_creator.criteria_daily_item_limit",
        "Daily criteria item limit",
        "Safety controls",
        "number",
        1000,
        "Maximum new keyword, URL inclusion, URL exclusion, negative keyword, and PMax search-theme items the live publisher may add per account per UTC day.",
    ),
    SettingDefinition(
        "live_campaign_creator.primary_criteria_daily_item_limit",
        "Primary account criteria limit",
        "Safety controls",
        "number",
        15000,
        "Daily keyword, URL inclusion, URL exclusion, negative keyword, and PMax search-theme additions allowed for primary Google Ads accounts.",
    ),
    SettingDefinition(
        "live_campaign_creator.secondary_criteria_daily_item_limit",
        "Secondary account criteria limit",
        "Safety controls",
        "number",
        10000,
        "Daily keyword, URL inclusion, URL exclusion, negative keyword, and PMax search-theme additions allowed for secondary Google Ads accounts.",
    ),
    SettingDefinition(
        "live_campaign_creator.other_criteria_daily_item_limit",
        "Other account criteria limit",
        "Safety controls",
        "number",
        5000,
        "Daily keyword, URL inclusion, URL exclusion, negative keyword, and PMax search-theme additions allowed for other enabled Google Ads accounts.",
    ),
    SettingDefinition(
        "automation.universal_garbage_negative_bank_limit",
        "Universal garbage negative bank limit",
        "Safety controls",
        "number",
        1500,
        "Maximum account negative-keyword bank terms the dedicated universal garbage negative worker can push into the shared negative list per account run.",
    ),
    SettingDefinition(
        "automation.universal_garbage_negative_sync_interval_hours",
        "Universal garbage negative sync interval",
        "Safety controls",
        "number",
        4,
        "How often the scheduler queues the dedicated shared negative keyword protection worker.",
    ),
    SettingDefinition(
        "automation.universal_garbage_negative_sync_max_accounts",
        "Universal garbage negative sync accounts",
        "Safety controls",
        "number",
        100,
        "Maximum enabled Google Ads accounts checked per dedicated universal garbage negative sync run.",
    ),
    SettingDefinition(
        "automation.basic_access_daily_operation_budget",
        "Daily Google Ads operations budget",
        "Safety controls",
        "number",
        15000,
        "Developer-token-level daily operations budget while the token has Google Ads API Basic Access.",
    ),
    SettingDefinition(
        "automation.basic_access_budget_reserve",
        "Daily operations reserve",
        "Safety controls",
        "number",
        3000,
        "Operations kept unused for budget up/down, monitoring, retries, and urgent fixes. Heavy campaign/asset work stops before this reserve is consumed.",
    ),
    SettingDefinition(
        "automation.cold_start_minimum_budget_until_conversion_threshold",
        "Cold-start minimum budget",
        "Automation",
        "checkbox",
        True,
        "Allows Testing / Discovery campaign creation at the account minimum budget for cold accounts until they reach the configured conversion threshold.",
    ),
    SettingDefinition(
        "automation.cold_start_conversion_threshold",
        "Cold-start conversion threshold",
        "Automation",
        "number",
        15,
        "Number of recent conversions after which cold accounts return to the normal Odoo sales/spend budget guard.",
    ),
    SettingDefinition(
        "automation.scheduler_primary_customer_ids",
        "Primary automation accounts",
        "Safety controls",
        "textarea",
        "8976162539\n3495463031\n5820365427",
        "Primary Google Ads customer IDs processed first while API access is Basic.",
    ),
    SettingDefinition(
        "automation.scheduler_secondary_customer_ids",
        "Secondary automation accounts",
        "Safety controls",
        "textarea",
        "2830748821\n9856905758\n1685325188\n6013079244\n7237928790",
        "Secondary Google Ads customer IDs processed after primary accounts for campaign/ad creation while API access is Basic.",
    ),
    SettingDefinition(
        "automation.scheduler_include_unlisted_accounts",
        "Process unlisted accounts",
        "Safety controls",
        "checkbox",
        False,
        "When off, unlisted accounts are skipped for heavy campaign/ad work while Basic Access quota is protected.",
    ),
    SettingDefinition(
        "optimizer.change_cooldown_hours",
        "Change cooldown hours",
        "Safety controls",
        "number",
        24,
        "Prevents repeating the same campaign action too soon.",
        env_name="GOOGLE_ADS_CHANGE_COOLDOWN_HOURS",
    ),
    SettingDefinition(
        "optimizer.optimization_date_range",
        "Optimization date range",
        "Optimization windows",
        "text",
        "LAST_7_DAYS",
        "Google Ads date range used for current performance.",
        env_name="GOOGLE_ADS_OPTIMIZATION_DATE_RANGE",
    ),
    SettingDefinition(
        "optimizer.baseline_date_range",
        "Baseline date range",
        "Optimization windows",
        "text",
        "LAST_30_DAYS",
        "Google Ads date range used for baseline comparison.",
        env_name="GOOGLE_ADS_BASELINE_DATE_RANGE",
    ),
    SettingDefinition(
        "optimizer.min_conversions_for_bid_change",
        "Minimum conversions for bid change",
        "Thresholds",
        "number",
        5,
        "Minimum conversion volume before bid target changes are considered.",
        env_name="GOOGLE_ADS_MIN_CONVERSIONS_FOR_BID_CHANGE",
    ),
    SettingDefinition(
        "optimizer.min_conversions_for_value_bidding",
        "Minimum conversions for value bidding",
        "Thresholds",
        "number",
        15,
        "Minimum 30-day conversion volume before Target ROAS or value-bidding target changes are treated as ready.",
    ),
    SettingDefinition(
        "optimizer.min_cost_for_action",
        "Minimum cost for action",
        "Thresholds",
        "number",
        25,
        "Minimum spend before waste or reduction actions are considered.",
        env_name="GOOGLE_ADS_MIN_COST_FOR_ACTION",
    ),
    SettingDefinition(
        "optimizer.zero_conversion_min_clicks",
        "Zero-conversion minimum clicks",
        "Thresholds",
        "number",
        10,
        "Minimum clicks before zero-conversion campaigns are flagged.",
        env_name="GOOGLE_ADS_ZERO_CONVERSION_MIN_CLICKS",
    ),
    SettingDefinition(
        "optimizer.max_budget_change_pct",
        "Maximum budget change percentage",
        "Guardrails",
        "number",
        0.20,
        "Maximum budget adjustment as a decimal. Example: 0.20 means 20%.",
        env_name="GOOGLE_ADS_MAX_BUDGET_CHANGE_PCT",
    ),
    SettingDefinition(
        "optimizer.max_target_roas_change_pct",
        "Maximum target ROAS change percentage",
        "Guardrails",
        "number",
        0.10,
        "Maximum target ROAS adjustment as a decimal.",
        env_name="GOOGLE_ADS_MAX_TARGET_ROAS_CHANGE_PCT",
    ),
    SettingDefinition(
        "optimizer.min_daily_budget",
        "Minimum daily budget",
        "Guardrails",
        "number",
        1,
        "Budget floor used when reducing campaign budgets.",
        env_name="GOOGLE_ADS_MIN_DAILY_BUDGET",
    ),
    SettingDefinition(
        "optimizer.ai_enabled",
        "Use OpenAI for optimization review",
        "AI optimizer",
        "checkbox",
        True,
        "Lets the campaign optimization button ask OpenAI to review the rule-engine plan. The rule guardrails still cap risky changes.",
    ),
    SettingDefinition(
        "optimizer.apply_local_page_feed_labels",
        "Apply local page-feed label updates",
        "AI optimizer",
        "checkbox",
        True,
        "Allows optimization runs to promote strong product URLs to winner or exclude proven waste URLs in local Odoo page-feed signals without calling Google Ads.",
    ),
    SettingDefinition(
        "optimizer.default_optimization_days",
        "Default campaign optimization days",
        "AI optimizer",
        "number",
        30,
        "Default lookback window used by the Optimize campaign button.",
    ),
    SettingDefinition(
        "optimizer.optimization_max_rows",
        "Optimization max rows",
        "AI optimizer",
        "number",
        3000,
        "Maximum rows per targeted Google Ads optimizer dataset. Lower values save API response size and processing time.",
    ),
    SettingDefinition(
        "storage.persist_optimizer_reports",
        "Save optimizer reports in Postgres",
        "Storage",
        "checkbox",
        True,
        "Saves generated report JSON into strategy run rows.",
    ),
    SettingDefinition(
        "storage.use_temporary_local_artifacts",
        "Use temporary local artifacts only",
        "Storage",
        "checkbox",
        True,
        "Uses temporary worker directories and removes them after each account run.",
    ),
    SettingDefinition(
        "page_feed.public_base_url",
        "Public frontend URL",
        "Page feeds",
        "text",
        "",
        "Public app URL Google Ads can fetch page-feed CSV files from. Leave blank to use the current browser domain, and set this after hosting the app.",
    ),
    SettingDefinition(
        "page_feed.default_max_urls",
        "Default page feed rows",
        "Page feeds",
        "number",
        5000,
        "Maximum URLs included when generating each hosted Google Ads page-feed CSV.",
    ),
    SettingDefinition(
        "automation.page_feed_publish_max_accounts_per_run",
        "Daily page-feed publish accounts",
        "Page feeds",
        "number",
        30,
        "Maximum mapped automation accounts included in each recurring Google Ads page-feed URL publish sweep.",
    ),
    SettingDefinition(
        "automation.page_feed_publish_max_urls_per_account",
        "Daily page-feed publish URLs",
        "Page feeds",
        "number",
        1000,
        "Maximum page-feed URLs published per mapped account in the recurring Google Ads page-feed URL publish sweep.",
    ),
    SettingDefinition(
        "page_feed.restricted_title_terms",
        "Restricted product title words",
        "Page feeds",
        "textarea",
        RESTRICTED_PAGE_FEED_TITLE_TERMS_DEFAULT,
        "One restricted term per line. Products whose Odoo title contains any term are excluded from all Google page feeds.",
    ),
    SettingDefinition(
        "razorpay.key_id",
        "Razorpay key ID",
        "Razorpay",
        "text",
        "",
        "API key ID used by the app worker to sync Razorpay payments.",
        sensitive=True,
    ),
    SettingDefinition(
        "razorpay.key_secret",
        "Razorpay key secret",
        "Razorpay",
        "password",
        "",
        "API key secret used by the app worker to sync Razorpay payments.",
        sensitive=True,
    ),
    SettingDefinition(
        "razorpay.sync_days",
        "Razorpay sync window",
        "Razorpay",
        "number",
        30,
        "Number of recent days to sync into the cost dashboard.",
    ),
    SettingDefinition(
        "currency.openexchange_app_id",
        "OpenExchange App ID",
        "Currency conversion",
        "password",
        "",
        "Open Exchange Rates App ID used to refresh USD, INR, and AUD conversion rates once per day. Stored in Postgres.",
        sensitive=True,
    ),
    SettingDefinition(
        "google_ads.performance_sync_days",
        "Google Ads sync window",
        "Google Ads reporting",
        "number",
        30,
        "Number of recent days of campaign cost and conversion value to sync.",
    ),
    SettingDefinition(
        "openai.api_key",
        "OpenAI API key",
        "OpenAI",
        "password",
        "",
        "API key used only by the Ad Factory worker to generate ad copy. Stored in Postgres.",
        sensitive=True,
    ),
    SettingDefinition(
        "openai.model",
        "OpenAI model",
        "OpenAI",
        "text",
        "gpt-5.2",
        "Model used for ad copy generation through the Responses API.",
    ),
    SettingDefinition(
        "openai.negative_keyword_decisions_enabled",
        "AI negative keyword decisions",
        "OpenAI",
        "checkbox",
        True,
        "When an OpenAI key is configured, ask the model to approve, reject, or hold top negative keyword candidates after hard conversion/brand guards run.",
    ),
    SettingDefinition(
        "openai.negative_keyword_decision_limit",
        "AI negative decision limit",
        "OpenAI",
        "number",
        25,
        "Maximum negative keyword candidates reviewed by OpenAI per account refresh. Results are stored with the candidate so repeat runs do not re-spend calls within the normal cache window.",
    ),
    SettingDefinition(
        "ad_factory.ai_mode_default",
        "Enable Google AI mode by default",
        "Ad Factory",
        "checkbox",
        True,
        "Marks generated ad drafts for Google AI features such as final URL expansion/text automation where the campaign type supports them.",
    ),
    SettingDefinition(
        "autopilot.enabled",
        "Autopilot enabled",
        "Autopilot",
        "checkbox",
        False,
        "When on, autopilot strategies can apply live Google Ads mutations if Allow mutations is on and Dry run is off.",
    ),
    SettingDefinition(
        "autopilot.purchase_goal_enabled",
        "Purchase goal auto-mode",
        "Autopilot",
        "checkbox",
        True,
        "Sets PURCHASE goals biddable and ADD_TO_CART goals not biddable wherever those goals exist.",
    ),
    SettingDefinition(
        "autopilot.lookback_hours",
        "No-impression lookback hours",
        "Autopilot",
        "number",
        12,
        "Hourly delivery window used to decide if a campaign has no or low impressions.",
    ),
    SettingDefinition(
        "autopilot.low_impression_threshold",
        "Low impression threshold",
        "Autopilot",
        "number",
        1,
        "Campaigns at or below this many impressions in the lookback window enter delivery rescue.",
    ),
    SettingDefinition(
        "autopilot.inr_rescue_budget",
        "INR rescue daily budget",
        "Autopilot budgets",
        "number",
        50000,
        "Daily budget target for INR campaigns in delivery rescue.",
    ),
    SettingDefinition(
        "autopilot.aud_rescue_budget",
        "AUD rescue daily budget",
        "Autopilot budgets",
        "number",
        500,
        "Daily budget target for AUD campaigns in delivery rescue.",
    ),
    SettingDefinition(
        "autopilot.default_rescue_budget",
        "Default non-INR rescue daily budget",
        "Autopilot budgets",
        "number",
        500,
        "Daily budget target for other non-INR currencies in delivery rescue.",
    ),
    SettingDefinition(
        "autopilot.target_roas_start",
        "Starting Target ROAS",
        "Autopilot ROAS",
        "number",
        3.5,
        "First rescue Target ROAS. 3.5 equals 350%.",
    ),
    SettingDefinition(
        "autopilot.target_roas_step",
        "Target ROAS lowering step",
        "Autopilot ROAS",
        "number",
        0.5,
        "Amount to lower Target ROAS each rescue pass while impressions remain absent.",
    ),
    SettingDefinition(
        "autopilot.target_roas_floor",
        "Target ROAS floor",
        "Autopilot ROAS",
        "number",
        1,
        "Lowest Target ROAS applied before the campaign is treated as needing target removal.",
    ),
    SettingDefinition(
        "autopilot.disable_after_hours",
        "Open-delivery observation after hours",
        "Autopilot ROAS",
        "number",
        72,
        "After this many no-impression hours, remove delivery caps where possible and observe instead of pausing.",
    ),
    SettingDefinition(
        "autopilot.observation_interval_hours",
        "Observation interval hours",
        "Autopilot observation",
        "number",
        3,
        "Minimum hours between open-delivery observation checks.",
    ),
    SettingDefinition(
        "autopilot.restore_spend_inr",
        "INR restore spend threshold",
        "Autopilot observation",
        "number",
        5000,
        "After open-delivery impressions return, restore regular ROAS/CPC once spend reaches this INR amount.",
    ),
    SettingDefinition(
        "autopilot.restore_spend_usd",
        "USD restore spend threshold",
        "Autopilot observation",
        "number",
        50,
        "After open-delivery impressions return, restore regular ROAS/CPC once spend reaches this USD-equivalent amount for non-INR accounts.",
    ),
    SettingDefinition(
        "spend_guard.window_hours",
        "Sales guard window hours",
        "Spend Guard",
        "number",
        12,
        "Confirmed Odoo sales and Google Ads spend window used for spend guard decisions.",
    ),
    SettingDefinition(
        "spend_guard.target_ratio",
        "Target ad cost to sales ratio",
        "Spend Guard",
        "number",
        0.15,
        "Green guard target. 0.15 means ad cost should stay within 15% of confirmed sales.",
    ),
    SettingDefinition(
        "spend_guard.exploration_margin",
        "Exploration margin",
        "Spend Guard",
        "number",
        0.05,
        "Allowed extra exploration above the target ratio. 0.05 means red starts above 20% when target is 15%.",
    ),
    SettingDefinition(
        "spend_guard.red_budget_cut_pct",
        "Red guard budget reduction",
        "Spend Guard",
        "number",
        0.20,
        "When ad cost is above the red threshold, reduce weak non-shared campaign budgets by this percentage instead of pausing campaigns.",
    ),
    SettingDefinition(
        "spend_guard.margin_warning_ratio",
        "Margin warning ratio",
        "Spend Guard",
        "number",
        0.16,
        "When synced Odoo margin exists, amber starts when ad cost reaches this share of order margin. 0.16 warns before the 20% cap.",
    ),
    SettingDefinition(
        "spend_guard.margin_hard_cap_ratio",
        "Margin hard cap ratio",
        "Spend Guard",
        "number",
        0.20,
        "When synced Odoo margin exists, ordinary accounts turn red above this share of margin. 0.20 means ad spend must stay below 20% of profit margin.",
    ),
    SettingDefinition(
        "spend_guard.priority_margin_extra_ratio",
        "Priority extra margin ratio",
        "Spend Guard",
        "number",
        0.10,
        "Extra margin share allowed only for priority accounts with a revenue signal. 0.10 means a 20% cap can become 30% for proven performers.",
    ),
    SettingDefinition(
        "spend_guard.priority_customer_ids",
        "Priority Google Ads accounts",
        "Spend Guard",
        "textarea",
        "3495463031\n5820365427\n8976162539",
        "One Google Ads customer ID per line. These accounts are treated as important sales drivers in guard evidence.",
    ),
]

DEFINITIONS_BY_KEY = {definition.key: definition for definition in SETTING_DEFINITIONS}
LEGACY_DEFAULT_REPLACEMENTS = {
    "autopilot.inr_rescue_budget": {120000: 50000, 120000.0: 50000},
    "autopilot.aud_rescue_budget": {2000: 500, 2000.0: 500},
    "autopilot.default_rescue_budget": {2000: 500, 2000.0: 500},
    "spend_guard.margin_warning_ratio": {0.8: 0.16, 0.80: 0.16},
    "spend_guard.margin_hard_cap_ratio": {1.0: 0.20, 1: 0.20},
}


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def coerce_form_value(definition: SettingDefinition, raw: Optional[str]) -> Any:
    if definition.input_type == "checkbox":
        return raw == "on"
    if raw is None:
        return definition.default
    value = raw.strip()
    if definition.input_type == "number":
        if value == "":
            return definition.default
        number = float(value)
        return int(number) if number.is_integer() else number
    return value


def coerce_env_value(definition: SettingDefinition, raw: str) -> Any:
    if definition.input_type == "checkbox":
        return parse_bool(raw)
    if definition.input_type == "number":
        number = float(raw)
        return int(number) if number.is_integer() else number
    return raw


async def seed_app_settings(session: AsyncSession, settings: Settings) -> None:
    env_values = {
        **parse_env_file(ROOT_DIR / ".env"),
        **parse_env_file(settings.optimizer_env_file),
    }
    for definition in SETTING_DEFINITIONS:
        value = definition.default
        has_env_value = definition.env_name and env_values.get(definition.env_name) not in {None, ""}
        if definition.env_name and env_values.get(definition.env_name) not in {None, ""}:
            value = coerce_env_value(definition, env_values[definition.env_name])
        stmt = insert(AppSetting).values(
            key=definition.key,
            value=value,
            category=definition.category,
            label=definition.label,
            help_text=definition.help_text,
            input_type=definition.input_type,
            sensitive=definition.sensitive,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[AppSetting.key])
        await session.execute(stmt)
        existing = await session.scalar(select(AppSetting).where(AppSetting.key == definition.key))
        if existing is not None:
            if has_env_value and existing.value in {None, ""}:
                existing.value = value
            existing.value = LEGACY_DEFAULT_REPLACEMENTS.get(definition.key, {}).get(existing.value, existing.value)
            existing.category = definition.category
            existing.label = definition.label
            existing.help_text = definition.help_text
            existing.input_type = definition.input_type
            existing.sensitive = definition.sensitive


async def get_setting_rows(session: AsyncSession) -> list[AppSetting]:
    rows = (await session.scalars(select(AppSetting).order_by(AppSetting.category, AppSetting.id))).all()
    existing = {row.key for row in rows}
    missing = [definition for definition in SETTING_DEFINITIONS if definition.key not in existing]
    for definition in missing:
        row = AppSetting(
            key=definition.key,
            value=definition.default,
            category=definition.category,
            label=definition.label,
            help_text=definition.help_text,
            input_type=definition.input_type,
            sensitive=definition.sensitive,
        )
        session.add(row)
        rows.append(row)
    if missing:
        await session.commit()
    return rows


def get_sync_setting_map(session: Session) -> dict[str, Any]:
    rows = session.scalars(select(AppSetting)).all()
    values = {row.key: row.value for row in rows}
    for definition in SETTING_DEFINITIONS:
        values.setdefault(definition.key, definition.default)
    return apply_runtime_safety_settings(values)


def apply_runtime_safety_settings(values: dict[str, Any]) -> dict[str, Any]:
    safe_values = dict(values)
    runtime_settings = get_settings()
    safe_values["runtime.app_instance_role"] = runtime_settings.app_instance_role
    safe_values["runtime.is_primary_instance"] = runtime_settings.is_primary_instance
    if not runtime_settings.live_google_ads_allowed:
        safe_values["optimizer.allow_mutations"] = False
        safe_values["optimizer.dry_run"] = True
        safe_values["optimizer.allow_total_budget_increase"] = False
    return safe_values


def _connection_value(connection: Optional[GoogleAdsConnection], attr: str, fallback: Any) -> Any:
    if connection is None:
        return fallback
    value = getattr(connection, attr, None)
    return value if value not in {None, ""} else fallback


def optimizer_env_from_settings(
    values: dict[str, Any],
    connection: Optional[GoogleAdsConnection] = None,
) -> dict[str, str]:
    values = apply_runtime_safety_settings(values)
    mapping = {
        "GOOGLE_ADS_DEVELOPER_TOKEN": _connection_value(
            connection,
            "developer_token",
            values.get("google_ads.developer_token", ""),
        ),
        "GOOGLE_ADS_CLIENT_ID": _connection_value(connection, "client_id", values.get("google_ads.client_id", "")),
        "GOOGLE_ADS_CLIENT_SECRET": _connection_value(
            connection,
            "client_secret",
            values.get("google_ads.client_secret", ""),
        ),
        "GOOGLE_ADS_REFRESH_TOKEN": _connection_value(
            connection,
            "refresh_token",
            values.get("google_ads.refresh_token", ""),
        ),
        "GOOGLE_ADS_API_VERSION": _connection_value(
            connection,
            "api_version",
            values.get("google_ads.api_version", ""),
        ),
        "GOOGLE_ADS_ALLOW_MUTATIONS": values.get("optimizer.allow_mutations", False),
        "GOOGLE_ADS_DRY_RUN": values.get("optimizer.dry_run", True),
        "GOOGLE_ADS_ALLOW_TOTAL_BUDGET_INCREASE": values.get("optimizer.allow_total_budget_increase", False),
        "GOOGLE_ADS_CHANGE_COOLDOWN_HOURS": values.get("optimizer.change_cooldown_hours", 24),
        "GOOGLE_ADS_OPTIMIZATION_DATE_RANGE": values.get("optimizer.optimization_date_range", "LAST_7_DAYS"),
        "GOOGLE_ADS_BASELINE_DATE_RANGE": values.get("optimizer.baseline_date_range", "LAST_30_DAYS"),
        "GOOGLE_ADS_MIN_CONVERSIONS_FOR_BID_CHANGE": values.get("optimizer.min_conversions_for_bid_change", 5),
        "GOOGLE_ADS_MIN_CONVERSIONS_FOR_VALUE_BIDDING": values.get("optimizer.min_conversions_for_value_bidding", 15),
        "GOOGLE_ADS_MIN_COST_FOR_ACTION": values.get("optimizer.min_cost_for_action", 25),
        "GOOGLE_ADS_ZERO_CONVERSION_MIN_CLICKS": values.get("optimizer.zero_conversion_min_clicks", 10),
        "GOOGLE_ADS_MAX_BUDGET_CHANGE_PCT": values.get("optimizer.max_budget_change_pct", 0.20),
        "GOOGLE_ADS_MAX_TARGET_ROAS_CHANGE_PCT": values.get("optimizer.max_target_roas_change_pct", 0.10),
        "GOOGLE_ADS_MIN_DAILY_BUDGET": values.get("optimizer.min_daily_budget", 1),
    }
    return {
        key: "true" if value is True else "false" if value is False else str(value)
        for key, value in mapping.items()
        if value not in {None, ""}
    }


def grouped_settings(rows: Iterable[AppSetting]) -> dict[str, list[AppSetting]]:
    groups: dict[str, list[AppSetting]] = {}
    for row in rows:
        groups.setdefault(row.category, []).append(row)
    return groups
