from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models import (
    AppSetting,
    GoogleAdsAccount,
    GoogleAdsAutomationPreference,
    GoogleAdsConnection,
    GoogleAdsDataSnapshot,
    GoogleAdsKeywordCandidate,
    GoogleAdsLandingPageCandidate,
    GoogleAdsNegativeKeywordCandidate,
)
from app.services.google_ads_automation import (
    _selected_peak_budget_operations,
    _google_ads_api_quota_retry_state,
    automation_live_creation_control,
    automation_campaign_identity,
    automation_strategy_summary,
    budget_guard_due_now,
    campaign_planning_data_freshness,
    cap_budget_increase_operations_by_sales_room,
    daily_insight_api_window_days,
    local_peak_window,
    landing_page_category_governance_plan,
    low_traffic_schedule_decision,
    peak_conversion_schedule_decision,
    peak_budget_due_now,
    peak_budget_restore_due_now,
    claim_keyword_terms,
    core_owned_keyword_rows_for_scale_migration,
    pmax_search_theme_plan,
    preference_due_now,
    refresh_peak_budget_decision,
    run_account_automation_monitor,
    run_odoo_sales_budget_guard,
    run_peak_budget_transition,
    run_testing_campaign_automation,
    scale_landing_page_plan,
    testing_scale_page_exclusion_plan,
    testing_scale_negative_keyword_plan,
    testing_daily_budget_from_sales,
    underperforming_budget_reduction_operations,
    waste_category_plan,
)
from app.services.google_ads_snapshot_store import (
    DATASET_CAMPAIGN_INSIGHTS,
    DATASET_LANDING_PAGES,
    DATASET_SEARCH_TERMS,
    DATASET_TIME_SEGMENTS,
)


class GoogleAdsAutomationTests(unittest.TestCase):
    class FakeSession:
        def __init__(self) -> None:
            self.added = []
            self.commits = 0

        def add(self, item) -> None:
            self.added.append(item)

        def commit(self) -> None:
            self.commits += 1

    class FakeScalarSession(FakeSession):
        def __init__(self, scalar_result=None) -> None:
            super().__init__()
            self.scalar_result = scalar_result

        def scalar(self, *_args, **_kwargs):
            return self.scalar_result

    class FakeQuotaSession(FakeSession):
        def __init__(self, exact_result=None, fallback_results=None) -> None:
            super().__init__()
            self.exact_result = exact_result
            self.fallback_results = fallback_results or []

        def scalar(self, *_args, **_kwargs):
            return self.exact_result

        def scalars(self, *_args, **_kwargs):
            class Result:
                def __init__(self, rows):
                    self.rows = rows

                def all(self):
                    return self.rows

            return Result(self.fallback_results)

    def test_low_traffic_schedule_uses_lowest_impression_hour(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        snapshot = GoogleAdsDataSnapshot(
            id=22,
            account_id=637,
            dataset_key=DATASET_TIME_SEGMENTS,
            scope_key="last_365d:2025-06-13:2026-06-12",
            row_count=4,
            fetched_at=datetime(2026, 6, 12, 14, 55, tzinfo=timezone.utc),
            payload_json={
                "rows": [
                    {"hour": 0, "impressions": 100, "clicks": 10, "cost": 20, "conversions": 1},
                    {"hour": 1, "impressions": 10, "clicks": 2, "cost": 1, "conversions": 0},
                    {"hour": 2, "impressions": 400, "clicks": 30, "cost": 80, "conversions": 4},
                    {"hour": 1, "impressions": 5, "clicks": 1, "cost": 1, "conversions": 0},
                ]
            },
        )
        with patch("app.services.google_ads_automation.latest_time_segments_snapshot", return_value=snapshot):
            decision = low_traffic_schedule_decision(
                None,
                account,
                fallback_hour=4,
                fallback_minute=20,
                time_zone="America/Toronto",
            )

        self.assertEqual(decision["status"], "decided")
        self.assertEqual(decision["recommended_hour"], 3)
        self.assertEqual(decision["recommended_time"], "03:20")
        self.assertEqual(decision["peak_hour"], 2)
        self.assertEqual(decision["source"], "time_segments:22")

    def test_preference_due_now_honors_schedule_window_and_last_run(self) -> None:
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            automation_enabled=True,
            schedule_mode="dynamic_low_traffic",
            scheduled_hour=4,
            scheduled_minute=20,
            schedule_timezone="UTC",
        )
        self.assertTrue(preference_due_now(preference, now=datetime(2026, 6, 12, 4, 45, tzinfo=timezone.utc)))
        self.assertFalse(preference_due_now(preference, now=datetime(2026, 6, 12, 5, 21, tzinfo=timezone.utc)))
        preference.last_run_at = datetime(2026, 6, 12, 4, 30, tzinfo=timezone.utc)
        self.assertFalse(preference_due_now(preference, now=datetime(2026, 6, 12, 4, 45, tzinfo=timezone.utc)))

    def test_monitor_blocks_only_google_ads_calls_during_api_quota_retry(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = GoogleAdsAutomationPreference(
            account=account,
            account_id=637,
            automation_enabled=True,
            monitor_only=False,
            auto_create_campaigns_enabled=True,
        )
        retry_not_before = datetime(2026, 6, 15, 17, 3, tzinfo=timezone.utc)
        setting = AppSetting(
            key="google_ads_asset_publish_quota.3495463031",
            value={
                "retry_not_before": retry_not_before.isoformat(),
                "reason": "Google Ads API basic-access operations quota exhausted during live asset repair",
            },
            category="google_ads_automation",
            label="quota",
            help_text="quota",
            input_type="json",
            sensitive=False,
        )
        session = self.FakeScalarSession(setting)

        with patch("app.services.google_ads_automation.utcnow", return_value=datetime(2026, 6, 14, 22, 0, tzinfo=timezone.utc)), patch(
            "app.services.google_ads_automation.refresh_low_traffic_schedule",
            return_value={"status": "decided", "recommended_time": "03:20", "source": "cached"},
        ) as refresh_schedule, patch(
            "app.services.google_ads_automation.automation_budget_bootstrap_state",
            return_value={"name": "budget_bootstrap", "status": "inactive", "active": False},
        ), patch(
            "app.services.google_ads_automation.reserve_automation_quota_units"
        ) as reserve_quota, patch(
            "app.services.google_ads_automation.ensure_account_ga4_mapping",
            return_value={"name": "ga4_connection", "status": "mapped"},
        ), patch(
            "app.services.google_ads_automation.maybe_refresh_ga4_ecommerce_for_account",
            return_value={"name": "ga4_ecommerce_pull", "status": "done"},
        ), patch(
            "app.services.google_ads_automation.ga4_ads_signal_matrix",
            return_value={"generated_at": "2026-06-14T22:00:00+00:00", "summary": {}, "snapshot_ids": [], "basis": "test"},
        ), patch(
            "app.services.google_ads_automation.ensure_account_search_console_mapping",
            return_value={"name": "search_console_connection", "status": "mapped"},
        ), patch(
            "app.services.google_ads_automation.sync_search_console_search_analytics",
            return_value={"scope_key": "recent", "target_count": 1, "dataset_count": 1, "snapshot_count": 1, "query_candidates_imported": 3, "errors": []},
        ), patch(
            "app.services.google_ads_automation.search_console_ads_signal_matrix",
            return_value={"generated_at": "2026-06-14T22:00:00+00:00", "summary": {}, "snapshot_ids": [], "basis": "test"},
        ), patch(
            "app.services.google_ads_automation.campaign_planning_data_freshness",
            return_value={"ok": False, "reason": "Google Ads quota blocked current metrics."},
        ), patch(
            "app.services.google_ads_automation.block_automation_campaign_drafts_for_data_freshness",
            return_value={"blocked": 0},
        ), patch(
            "app.services.google_ads_automation.get_sync_setting_map",
            return_value={"optimizer.allow_mutations": True, "optimizer.dry_run": False},
        ):
            summary = run_account_automation_monitor(session, preference)

        self.assertEqual(summary["status"], "blocked_by_google_quota")
        self.assertTrue(summary["google_ads_api_blocked"])
        self.assertEqual(summary["steps"][0]["name"], "google_ads_api_quota")
        self.assertGreater(summary["steps"][0]["retry_after_seconds"], 0)
        self.assertIn("search_console_ads_signal_matrix", summary)
        self.assertIn("ga4_ads_signal_matrix", summary)
        self.assertTrue(
            any(step["name"] == "live_campaign_creation" and step["status"] == "blocked_by_google_quota" for step in summary["steps"])
        )
        self.assertEqual(session.commits, 1)
        refresh_schedule.assert_called_once()
        self.assertFalse(refresh_schedule.call_args.kwargs["fetch_time_zone"])
        reserve_quota.assert_not_called()

    def test_google_ads_quota_retry_is_scoped_to_matching_account_or_connection(self) -> None:
        retry_not_before = datetime(2026, 6, 15, 17, 3, tzinfo=timezone.utc)
        ca_quota = AppSetting(
            key="google_ads_asset_publish_quota.3495463031",
            value={
                "retry_not_before": retry_not_before.isoformat(),
                "reason": "CA token quota exhausted",
            },
            category="google_ads_automation",
            label="quota",
            help_text="quota",
            input_type="json",
            sensitive=False,
        )
        aud_account = GoogleAdsAccount(
            id=633,
            name="NutriCity - AUD Account",
            customer_id="8976162539",
            connection_id=7,
            connection=GoogleAdsConnection(id=7, developer_token="separate-token"),
        )

        with patch("app.services.google_ads_automation.utcnow", return_value=datetime(2026, 6, 14, 22, 0, tzinfo=timezone.utc)):
            retry = _google_ads_api_quota_retry_state(self.FakeQuotaSession(fallback_results=[ca_quota]), aud_account)

        self.assertIsNone(retry)

        ca_quota.value = {**ca_quota.value, "connection_id": 7}
        with patch("app.services.google_ads_automation.utcnow", return_value=datetime(2026, 6, 14, 22, 0, tzinfo=timezone.utc)):
            retry = _google_ads_api_quota_retry_state(self.FakeQuotaSession(fallback_results=[ca_quota]), aud_account)

        self.assertIsNotNone(retry)
        self.assertEqual(retry["quota_key"], "google_ads_asset_publish_quota.3495463031")

    def test_budget_guard_due_every_configured_interval(self) -> None:
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            automation_enabled=True,
            odoo_sales_guard_enabled=True,
            budget_guard_check_interval_hours=6,
        )
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(budget_guard_due_now(preference, now=now))
        preference.last_budget_guard_run_at = datetime(2026, 6, 12, 7, 0, tzinfo=timezone.utc)
        self.assertFalse(budget_guard_due_now(preference, now=now))
        preference.last_budget_guard_run_at = datetime(2026, 6, 12, 6, 0, tzinfo=timezone.utc)
        self.assertTrue(budget_guard_due_now(preference, now=now))

    def test_strategy_summary_uses_rolling_sales_window_and_fix_watch_roas(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031", currency_code="CAD")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            automation_enabled=True,
            auto_peak_budget_enabled=True,
            odoo_sales_guard_enabled=True,
            odoo_sales_max_spend_ratio=0.15,
            odoo_sales_guard_window_days=7,
            peak_budget_extra_spend_ratio=0.05,
            peak_budget_check_interval_minutes=60,
            scheduled_hour=4,
            scheduled_minute=20,
            schedule_timezone="UTC",
            testing_bootstrap_enabled=True,
            testing_bootstrap_days=15,
            pmax_min_7d_conversions=15,
            testing_sales_budget_ratio=0.05,
            peak_budget_decision_json={"peak_time": "02:00"},
        )

        summary = automation_strategy_summary(preference)
        categories = {item["name"]: item for item in summary["campaign_categories"]}
        intervals = {item["name"]: item for item in summary["intervals"]}

        self.assertEqual(summary["quota"]["sales_guard_window_type"], "rolling")
        self.assertEqual(summary["quota"]["fix_watch_target_roas"], 3.5)
        self.assertEqual(summary["quota"]["testing_bootstrap_days"], 15)
        self.assertEqual(summary["quota"]["pmax_min_7d_conversions"], 15)
        self.assertEqual(summary["quota"]["testing_sales_budget_ratio"], 5)
        self.assertEqual(categories["Fix / Watch"]["target_roas_pct"], 350)
        self.assertEqual(categories["Waste / Recovery"]["target_roas_pct"], 200)
        self.assertEqual(categories["Waste / Recovery"]["badge"], "Fixed 50 CAD")
        self.assertEqual(summary["quota"]["waste_fixed_daily_budget"], 50.0)
        self.assertIn("PMax drafts are prepared", categories["Testing / Discovery"]["campaign_types"])
        self.assertIn("20.0%", intervals["Peak conversion budget"]["detail"])
        self.assertIn("rolling 7-day window", intervals["Odoo sales guard"]["detail"])
        self.assertIn("Target ROAS RSA", intervals["Testing campaign bootstrap"]["detail"])
        self.assertNotIn("Maximize Clicks", intervals["Testing campaign bootstrap"]["detail"])

    def test_inr_waste_budget_uses_currency_specific_fixed_amount(self) -> None:
        account = GoogleAdsAccount(id=640, name="Nutricity India", customer_id="7373005276", currency_code="INR")
        preference = GoogleAdsAutomationPreference(account_id=640, account=account, automation_enabled=True)

        summary = automation_strategy_summary(preference)
        categories = {item["name"]: item for item in summary["campaign_categories"]}

        self.assertEqual(categories["Waste / Recovery"]["badge"], "Fixed 4,000 INR")
        self.assertEqual(summary["quota"]["waste_fixed_daily_budget"], 4000.0)

    def test_live_creation_control_holds_only_pmax_until_search_conversion_gate(self) -> None:
        thin_decision = {
            "pmax_allowed": False,
            "mode": "testing_no_pmax",
            "pmax_gate": {"threshold": 15, "conversions": 4, "reason": "PMax waits for Search conversions."},
        }
        allowed_decision = {"pmax_allowed": True, "mode": "pmax_allowed"}

        testing_rsa = automation_live_creation_control(thin_decision, category="Testing / Discovery", ad_type="rsa")
        core_rsa = automation_live_creation_control(thin_decision, category="Core / Scale", ad_type="rsa")
        testing_pmax = automation_live_creation_control(thin_decision, category="Testing / Discovery", ad_type="pmax")
        allowed_pmax = automation_live_creation_control(allowed_decision, category="Core / Scale", ad_type="pmax")

        self.assertTrue(testing_rsa["enabled"])
        self.assertEqual(testing_rsa["phase"], "full_closed_loop")
        self.assertTrue(core_rsa["enabled"])
        self.assertFalse(testing_pmax["enabled"])
        self.assertEqual(testing_pmax["phase"], "pmax_search_conversion_gate")
        self.assertEqual(testing_pmax["pmax_gate"]["threshold"], 15)
        self.assertTrue(allowed_pmax["enabled"])

    def test_campaign_planning_freshness_allows_fresh_zero_metrics(self) -> None:
        account = GoogleAdsAccount(id=637, name="New Account", customer_id="3495463031")
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        research = {
            "datasets": [
                {"dataset_key": DATASET_CAMPAIGN_INSIGHTS, "status": "fetched", "rows": 0},
                {"dataset_key": DATASET_LANDING_PAGES, "status": "fetched", "rows": 0},
                {"dataset_key": DATASET_SEARCH_TERMS, "status": "fetched", "rows": 0},
            ],
            "errors": [],
        }

        with patch(
            "app.services.google_ads_automation.recent_account_metric_totals",
            return_value={
                "latest_metric_date": None,
                "latest_synced_at": None,
                "metric_day_count": 0,
                "impressions": 0,
                "conversions": 0,
            },
        ):
            freshness = campaign_planning_data_freshness(
                None,
                account,
                research=research,
                metrics_refresh_ok=True,
                now=now,
            )

        self.assertTrue(freshness["ok"])
        self.assertTrue(freshness["fresh_zero_metrics"])

    def test_campaign_planning_freshness_blocks_stale_metric_data(self) -> None:
        account = GoogleAdsAccount(id=637, name="Stale Account", customer_id="3495463031")
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        research = {
            "datasets": [
                {"dataset_key": DATASET_CAMPAIGN_INSIGHTS, "status": "cached", "rows": 5, "fetched_at": now.isoformat()},
                {"dataset_key": DATASET_LANDING_PAGES, "status": "cached", "rows": 5, "fetched_at": now.isoformat()},
                {"dataset_key": DATASET_SEARCH_TERMS, "status": "cached", "rows": 5, "fetched_at": now.isoformat()},
            ],
            "errors": [],
        }

        with patch(
            "app.services.google_ads_automation.recent_account_metric_totals",
            return_value={
                "latest_metric_date": "2026-06-09",
                "latest_synced_at": "2026-06-09T12:00:00+00:00",
                "metric_day_count": 7,
                "impressions": 10,
                "conversions": 0,
            },
        ):
            freshness = campaign_planning_data_freshness(
                None,
                account,
                research=research,
                metrics_refresh_ok=True,
                now=now,
            )

        self.assertFalse(freshness["ok"])
        self.assertIn("7-day campaign metrics", freshness["reason"])

    def test_direct_campaign_planner_requires_freshness_token(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            testing_bootstrap_enabled=True,
        )

        result = run_testing_campaign_automation(None, preference)

        self.assertEqual(result["status"], "deferred_stale_data")
        self.assertIn("Fresh data token", result["reason"])

    def test_testing_budget_blocks_when_odoo_spend_room_is_zero(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031", currency_code="CAD")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            testing_sales_budget_ratio=0.05,
            odoo_sales_guard_window_days=7,
            minimum_daily_budget_amount=10,
        )

        with patch(
            "app.services.google_ads_automation.odoo_sales_budget_guard_for_account",
            return_value={
                "status": "red",
                "sales_inr": 100000,
                "remaining_spend_room_inr": 0,
                "daily_spend_room_inr": 0,
            },
        ), patch("app.services.google_ads_automation.get_latest_rate_snapshot_sync", return_value=None):
            budget = testing_daily_budget_from_sales(None, preference)

        self.assertTrue(budget["budget_blocked"])
        self.assertEqual(budget["daily_budget"], 0)
        self.assertIn("no remaining spend room", budget["budget_block_reason"])

    def test_testing_budget_bootstrap_uses_sales_before_spend_room_guard(self) -> None:
        account = GoogleAdsAccount(id=633, name="NutriCity - AUD Account", customer_id="8976162539", currency_code="INR")
        preference = GoogleAdsAutomationPreference(
            account_id=633,
            account=account,
            auto_create_campaigns_enabled=True,
            monitor_only=False,
            testing_sales_budget_ratio=0.05,
            odoo_sales_guard_window_days=7,
            minimum_daily_budget_amount=10,
        )

        with patch(
            "app.services.google_ads_automation.odoo_sales_budget_guard_for_account",
            return_value={
                "status": "red",
                "sales_inr": 100000,
                "remaining_spend_room_inr": 0,
                "daily_spend_room_inr": 0,
            },
        ), patch(
            "app.services.google_ads_automation.automation_budget_bootstrap_state",
            return_value={"status": "active", "active": True, "days": 3},
        ), patch("app.services.google_ads_automation.get_latest_rate_snapshot_sync", return_value=None):
            budget = testing_daily_budget_from_sales(None, preference)

        self.assertFalse(budget["budget_blocked"])
        self.assertEqual(budget["budget_basis"], "last_7_day_sales_bootstrap")
        self.assertAlmostEqual(budget["daily_budget"], 714.2857, places=3)

    def test_automation_campaign_identity_is_stable_for_reattached_account(self) -> None:
        original = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="349-546-3031")
        reattached = GoogleAdsAccount(id=999, name="Nutricity Canada", customer_id="3495463031")

        first = automation_campaign_identity(
            original,
            category="Testing / Discovery",
            campaign_intent="maximize_clicks_dsa_all_pages",
            channel_label="DSA Target ROAS Page Discovery",
            website_url="https://nutricity.com.au/collections",
        )
        second = automation_campaign_identity(
            reattached,
            category="Testing / Discovery",
            campaign_intent="maximize_clicks_dsa_all_pages",
            channel_label="DSA Target ROAS Page Discovery",
            website_url="https://www.nutricity.com.au/",
        )
        rsa = automation_campaign_identity(
            original,
            category="Testing / Discovery",
            campaign_intent="maximize_clicks_rsa_best_keywords",
            channel_label="RSA Target ROAS Keywords",
            website_url="https://nutricity.com.au/",
        )

        self.assertEqual(first["campaign_code"], second["campaign_code"])
        self.assertNotEqual(first["campaign_code"], rsa["campaign_code"])
        self.assertIn("Testing / Discovery", first["campaign_name"])
        self.assertIn(first["campaign_code"], first["campaign_name"])

    def test_peak_conversion_schedule_uses_highest_conversion_hour(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        snapshot = GoogleAdsDataSnapshot(
            id=25,
            account_id=637,
            dataset_key=DATASET_TIME_SEGMENTS,
            scope_key="last_365d:2025-06-13:2026-06-12",
            row_count=4,
            fetched_at=datetime(2026, 6, 12, 14, 55, tzinfo=timezone.utc),
            payload_json={
                "rows": [
                    {"campaign_id": 1, "campaign_name": "Low", "hour": 0, "impressions": 1000, "clicks": 90, "cost": 50, "conversions": 4, "conversion_value": 100},
                    {"campaign_id": 2, "campaign_name": "Peak A", "hour": 2, "impressions": 400, "clicks": 30, "cost": 30, "conversions": 7, "conversion_value": 150},
                    {"campaign_id": 3, "campaign_name": "Peak B", "hour": 2, "impressions": 300, "clicks": 20, "cost": 25, "conversions": 2, "conversion_value": 80},
                    {"campaign_id": 4, "campaign_name": "Tie loser", "hour": 9, "impressions": 500, "clicks": 50, "cost": 45, "conversions": 7, "conversion_value": 120},
                ]
            },
        )
        with patch("app.services.google_ads_automation.latest_time_segments_snapshot", return_value=snapshot):
            decision = peak_conversion_schedule_decision(
                None,
                account,
                warmup_minutes=60,
                restore_delay_minutes=0,
                increase_pct=0.5,
                time_zone="Australia/Perth",
            )

        self.assertEqual(decision["status"], "decided")
        self.assertEqual(decision["peak_hour"], 2)
        self.assertEqual(decision["peak_time"], "02:00")
        self.assertEqual(decision["boost_start_time"], "01:00")
        self.assertEqual(decision["restore_time"], "03:00")
        self.assertEqual(decision["campaign_ids"], [2, 3])

    def test_peak_conversion_schedule_blocks_stale_snapshot_when_required(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        snapshot = GoogleAdsDataSnapshot(
            id=25,
            account_id=637,
            dataset_key=DATASET_TIME_SEGMENTS,
            fetched_at=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            payload_json={"rows": [{"hour": 2, "impressions": 400, "clicks": 30, "conversions": 7}]},
        )

        with patch("app.services.google_ads_automation.latest_time_segments_snapshot", return_value=snapshot):
            decision = peak_conversion_schedule_decision(
                None,
                account,
                require_fresh_snapshot=True,
            )

        self.assertEqual(decision["status"], "stale")
        self.assertIsNone(decision["peak_hour"])

    def test_pmax_search_theme_plan_caps_at_50_and_adds_asset_groups(self) -> None:
        rows = [
            GoogleAdsKeywordCandidate(
                id=index,
                account_id=637,
                keyword=("very long supplement search theme " * 5) if index == 1 else f"theme {index}",
                normalized_keyword=f"theme-{index}",
                quality_label="converting",
                score=100 - index,
                clicks=index,
                impressions=index * 10,
                conversions=30 - index,
                conversion_value=1000 - index,
            )
            for index in range(1, 56)
        ]

        plan = pmax_search_theme_plan(rows)

        self.assertEqual(plan["limit"], 50)
        self.assertEqual(len(plan["active_terms"]), 50)
        self.assertEqual(plan["additional_asset_group_term_count"], 5)
        self.assertEqual(plan["pending_partial_asset_group_term_count"], 5)
        self.assertEqual(plan["overflow_count"], 0)
        self.assertEqual(plan["asset_group_count"], 1)
        self.assertEqual(plan["campaign_count"], 1)
        self.assertLessEqual(max(len(term) for term in plan["active_terms"]), 80)
        self.assertTrue(plan["additional_asset_group_candidates"])

    def test_keyword_terms_are_claimed_once_across_campaign_lanes(self) -> None:
        rows = [
            GoogleAdsKeywordCandidate(
                id=index,
                account_id=637,
                keyword=f"theme {index}",
                normalized_keyword=f"theme {index}",
                quality_label="converting",
                score=100 - index,
                clicks=index,
                impressions=index * 10,
            )
            for index in range(1, 8)
        ]
        used = {"theme 1", "theme 2"}

        first_terms, _first_rows = claim_keyword_terms(rows, used, limit=3)
        second_terms, _second_rows = claim_keyword_terms(rows, used, limit=3)

        self.assertEqual(first_terms, ["theme 3", "theme 4", "theme 5"])
        self.assertEqual(second_terms, ["theme 6", "theme 7"])
        self.assertEqual(len(set(first_terms + second_terms)), 5)

    def test_scale_migration_uses_same_account_keywords_only(self) -> None:
        account = GoogleAdsAccount(id=637, customer_id="3495463031", name="Nutricity CA")
        rows = [
            GoogleAdsKeywordCandidate(
                id=1,
                account_id=637,
                keyword="vitamin c canada",
                normalized_keyword="vitamin c canada",
                quality_label="converting",
                score=100,
            ),
            GoogleAdsKeywordCandidate(
                id=2,
                account_id=999,
                keyword="cross account winner",
                normalized_keyword="cross account winner",
                quality_label="converting",
                score=500,
            ),
        ]

        owned = core_owned_keyword_rows_for_scale_migration(rows, account)

        self.assertEqual([row.keyword for row in owned], ["vitamin c canada"])

    def test_pmax_search_theme_plan_starts_new_campaign_after_100_asset_groups(self) -> None:
        rows = [
            GoogleAdsKeywordCandidate(
                id=index,
                account_id=637,
                keyword=f"scale theme {index}",
                normalized_keyword=f"scale-theme-{index}",
                quality_label="converting",
                score=10000 - index,
                clicks=index,
                impressions=index * 10,
                conversions=1,
                conversion_value=100,
            )
            for index in range(1, 5052)
        ]

        plan = pmax_search_theme_plan(rows, overflow_limit=6000)

        self.assertEqual(plan["asset_group_count"], 101)
        self.assertEqual(plan["campaign_count"], 2)
        self.assertEqual(plan["campaign_plans"][0]["asset_group_count"], 100)
        self.assertEqual(plan["campaign_plans"][1]["asset_group_count"], 1)
        self.assertEqual(plan["pending_partial_asset_group_term_count"], 1)
        self.assertTrue(plan["new_campaign_required"])

    def test_testing_negative_plan_holds_new_scale_terms_pending(self) -> None:
        plan = {
            "campaign_plans": [
                {
                    "asset_groups": [
                        {
                            "candidates": [
                                {
                                    "search_theme": "vitamin c canada",
                                    "theme_key": "vitamin c canada",
                                    "scale_evidence": {"scale_conversions": 1, "scale_conversion_value": 100},
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        migration = {
            "terms": {
                "vitamin c canada": {
                    "promoted_at_utc": (now - timedelta(days=1)).isoformat(),
                    "scale_conversions": 1,
                }
            }
        }

        negatives = testing_scale_negative_keyword_plan(plan, migration_state=migration, now=now)

        self.assertFalse(negatives["enabled"])
        self.assertEqual(negatives["negative_keyword_count"], 0)
        self.assertEqual(negatives["pending_keyword_count"], 1)
        self.assertIn("3-day migration hold", negatives["pending_keywords"][0]["reason"])

    def test_testing_negative_plan_reserves_only_after_hold_and_scale_conversion(self) -> None:
        plan = {
            "campaign_plans": [
                {
                    "asset_groups": [
                        {
                            "candidates": [
                                {
                                    "search_theme": "vitamin c canada",
                                    "theme_key": "vitamin c canada",
                                    "scale_evidence": {
                                        "scale_conversions": 2,
                                        "scale_conversion_value": 200,
                                        "scale_campaign_names": ["AUTO | Core / Scale | PMax Target ROAS Scale S001"],
                                    },
                                },
                                {
                                    "search_theme": "magnesium glycinate",
                                    "theme_key": "magnesium glycinate",
                                    "scale_evidence": {"scale_conversions": 0, "scale_conversion_value": 0},
                                },
                            ]
                        }
                    ]
                }
            ]
        }
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        migration = {
            "terms": {
                "vitamin c canada": {
                    "promoted_at_utc": (now - timedelta(days=8)).isoformat(),
                    "scale_conversions": 2,
                },
                "magnesium glycinate": {
                    "promoted_at_utc": (now - timedelta(days=8)).isoformat(),
                    "scale_conversions": 0,
                },
            }
        }

        negatives = testing_scale_negative_keyword_plan(plan, migration_state=migration, now=now)

        self.assertTrue(negatives["enabled"])
        self.assertEqual(negatives["negative_keyword_count"], 1)
        self.assertEqual(negatives["negative_keywords"][0]["keyword"], "vitamin c canada")
        self.assertEqual(negatives["pending_keyword_count"], 1)
        self.assertIn("testing_dsa", negatives["applies_to"])

    def test_testing_page_exclusion_plan_holds_new_scale_pages_pending(self) -> None:
        plan = {
            "active_pages": [
                {
                    "url": "https://nutricity.ca/shop/vitamin-c",
                    "page_key": "https://nutricity.ca/shop/vitamin-c",
                    "scale_evidence": {"scale_conversions": 1, "scale_conversion_value": 100},
                }
            ]
        }
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        migration = {
            "pages": {
                "https://nutricity.ca/shop/vitamin-c": {
                    "promoted_at_utc": (now - timedelta(days=1)).isoformat(),
                    "scale_conversions": 1,
                }
            }
        }

        exclusions = testing_scale_page_exclusion_plan(plan, migration_state=migration, now=now)

        self.assertFalse(exclusions["enabled"])
        self.assertEqual(exclusions["page_exclusion_count"], 0)
        self.assertEqual(exclusions["pending_page_count"], 1)
        self.assertIn("3-day migration hold", exclusions["pending_pages"][0]["reason"])

    def test_testing_page_exclusion_plan_excludes_only_after_hold_and_scale_conversion(self) -> None:
        plan = {
            "active_pages": [
                {
                    "url": "https://nutricity.ca/shop/vitamin-c",
                    "page_key": "https://nutricity.ca/shop/vitamin-c",
                    "scale_evidence": {
                        "scale_conversions": 2,
                        "scale_conversion_value": 200,
                        "scale_campaign_names": ["AUTO | Core / Scale | PMax Target ROAS Scale S001"],
                    },
                },
                {
                    "url": "https://nutricity.ca/shop/magnesium",
                    "page_key": "https://nutricity.ca/shop/magnesium",
                    "scale_evidence": {"scale_conversions": 0, "scale_conversion_value": 0},
                },
            ]
        }
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        migration = {
            "pages": {
                "https://nutricity.ca/shop/vitamin-c": {
                    "promoted_at_utc": (now - timedelta(days=8)).isoformat(),
                    "scale_conversions": 2,
                },
                "https://nutricity.ca/shop/magnesium": {
                    "promoted_at_utc": (now - timedelta(days=8)).isoformat(),
                    "scale_conversions": 0,
                },
            }
        }

        exclusions = testing_scale_page_exclusion_plan(plan, migration_state=migration, now=now)

        self.assertTrue(exclusions["enabled"])
        self.assertEqual(exclusions["page_exclusion_count"], 1)
        self.assertEqual(exclusions["page_exclusions"][0]["url"], "https://nutricity.ca/shop/vitamin-c")
        self.assertEqual(exclusions["pending_page_count"], 1)
        self.assertIn("testing_pmax", exclusions["applies_to"])

    def test_scale_landing_page_plan_dedupes_urls(self) -> None:
        rows = [
            GoogleAdsLandingPageCandidate(
                id=1,
                account_id=637,
                url="https://nutricity.ca/shop/vitamin-c?utm_source=x",
                normalized_url="https://nutricity.ca/shop/vitamin-c",
                normalized_url_hash="a",
                quality_label="converting",
                score=10,
                clicks=10,
                impressions=100,
                conversions=1,
                conversion_value=50,
            ),
            GoogleAdsLandingPageCandidate(
                id=2,
                account_id=637,
                url="https://nutricity.ca/shop/vitamin-c",
                normalized_url="https://nutricity.ca/shop/vitamin-c",
                normalized_url_hash="a",
                quality_label="clicked",
                score=5,
                clicks=10,
                impressions=100,
            ),
        ]

        plan = scale_landing_page_plan(rows)

        self.assertEqual(plan["active_page_count"], 1)
        self.assertEqual(plan["active_pages"][0]["url"], "https://nutricity.ca/shop/vitamin-c")

    def test_scale_landing_page_plan_includes_ga4_cart_and_search_console_pages(self) -> None:
        plan = scale_landing_page_plan(
            [],
            ga4_matrix={
                "scale_landing_pages": [],
                "testing_landing_pages": [
                    {
                        "url": "https://nutricity.ca/shop/cart-interest",
                        "add_to_carts": 5,
                        "checkouts": 1,
                        "score": 900,
                    }
                ],
            },
            search_console_matrix={
                "top_pages": [
                    {
                        "page_url": "https://nutricity.ca/shop/organic-traffic",
                        "clicks": 12,
                        "impressions": 800,
                        "score": 500,
                    }
                ]
            },
        )

        urls = {item["url"] for item in plan["active_pages"]}

        self.assertIn("https://nutricity.ca/shop/cart-interest", urls)
        self.assertIn("https://nutricity.ca/shop/organic-traffic", urls)
        cart_page = next(item for item in plan["active_pages"] if item["url"].endswith("/cart-interest"))
        self.assertEqual(cart_page["add_to_carts"], 5)

    def test_waste_category_plan_quarantines_negative_terms(self) -> None:
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        negative = GoogleAdsNegativeKeywordCandidate(
            id=1,
            account_id=637,
            keyword="free vitamin samples",
            normalized_keyword="free vitamin samples",
            match_type="exact",
            reason_label="free_discount_intent",
            confidence=0.82,
            impressions=200,
            clicks=3,
            cost=4.5,
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now - timedelta(days=1),
            last_pulled_at=now - timedelta(days=1),
        )

        plan = waste_category_plan([negative], [], [], now=now)

        keyword_plan = plan["negative_keyword_plan"]
        self.assertTrue(plan["enabled"])
        self.assertEqual(plan["budget"], 50.0)
        self.assertEqual(plan["budget_policy"], "fixed_no_peak_boost_no_sales_guard_reduction")
        self.assertEqual(keyword_plan["active_negative_keyword_count"], 1)
        self.assertEqual(keyword_plan["active_negative_keywords"][0]["keyword"], "free vitamin samples")
        self.assertIn("core_scale_pmax", keyword_plan["applies_to"])
        self.assertIn("testing_dsa", keyword_plan["applies_to"])

    def test_waste_category_plan_releases_keyword_after_fresh_conversion_hold(self) -> None:
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        negative = GoogleAdsNegativeKeywordCandidate(
            id=1,
            account_id=637,
            keyword="omega capsules canada",
            normalized_keyword="omega capsules canada",
            match_type="exact",
            reason_label="zero_conversion_click_waste",
            confidence=0.76,
            clicks=12,
            cost=31,
            first_seen_at=now - timedelta(days=20),
            last_seen_at=now - timedelta(days=10),
            last_pulled_at=now - timedelta(days=10),
        )
        positive = GoogleAdsKeywordCandidate(
            id=10,
            account_id=637,
            keyword="omega capsules canada",
            normalized_keyword="omega capsules canada",
            quality_label="converting",
            clicks=18,
            conversions=1,
            conversion_value=80,
            last_seen_at=now,
            last_pulled_at=now,
        )
        state = {
            "terms": {
                "omega capsules canada": {
                    "quarantined_at_utc": (now - timedelta(days=20)).isoformat(),
                    "recovery_started_at_utc": (now - timedelta(days=8)).isoformat(),
                }
            }
        }

        plan = waste_category_plan([negative], [positive], [], recovery_state=state, now=now)

        keyword_plan = plan["negative_keyword_plan"]
        self.assertEqual(keyword_plan["active_negative_keyword_count"], 0)
        self.assertEqual(keyword_plan["scale_recovery_keyword_count"], 1)
        self.assertTrue(keyword_plan["scale_recovery_keywords"][0]["remove_negative_before_promotion"])

    def test_waste_category_plan_quarantines_and_recovers_landing_pages(self) -> None:
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        waste_page = GoogleAdsLandingPageCandidate(
            id=1,
            account_id=637,
            url="https://nutricity.ca/shop/free-samples",
            normalized_url="https://nutricity.ca/shop/free-samples",
            normalized_url_hash="waste",
            quality_label="clicked",
            clicks=25,
            impressions=400,
            cost=35,
            first_seen_at=now - timedelta(days=4),
            last_seen_at=now - timedelta(days=1),
            last_pulled_at=now - timedelta(days=1),
        )
        plan = waste_category_plan([], [], [waste_page], now=now)
        self.assertEqual(plan["page_exclusion_plan"]["active_page_exclusion_count"], 1)

        recovered_page = GoogleAdsLandingPageCandidate(
            id=1,
            account_id=637,
            url="https://nutricity.ca/shop/free-samples",
            normalized_url="https://nutricity.ca/shop/free-samples",
            normalized_url_hash="waste",
            quality_label="converting",
            clicks=40,
            impressions=500,
            conversions=1,
            conversion_value=75,
            first_seen_at=now - timedelta(days=14),
            last_seen_at=now,
            last_pulled_at=now,
        )
        state = {
            "pages": {
                "https://nutricity.ca/shop/free-samples": {
                    "quarantined_at_utc": (now - timedelta(days=14)).isoformat(),
                    "recovery_started_at_utc": (now - timedelta(days=8)).isoformat(),
                }
            }
        }

        recovered = waste_category_plan([], [], [recovered_page], recovery_state=state, now=now)

        page_plan = recovered["page_exclusion_plan"]
        self.assertEqual(page_plan["active_page_exclusion_count"], 0)
        self.assertEqual(page_plan["scale_recovery_page_count"], 1)
        self.assertTrue(page_plan["scale_recovery_pages"][0]["remove_page_exclusion_before_promotion"])

    def test_landing_page_governance_assigns_pages_by_category(self) -> None:
        now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        scale_page = GoogleAdsLandingPageCandidate(
            id=1,
            account_id=637,
            url="https://nutricity.ca/shop/vitamin-c",
            normalized_url="https://nutricity.ca/shop/vitamin-c",
            normalized_url_hash="scale",
            quality_label="converting",
            score=100,
            clicks=20,
            impressions=500,
            conversions=2,
            conversion_value=160,
            campaign_names=["AUTO | Core / Scale | PMax Target ROAS Scale S001"],
        )
        testing_page = GoogleAdsLandingPageCandidate(
            id=2,
            account_id=637,
            url="https://nutricity.ca/shop/new-greens",
            normalized_url="https://nutricity.ca/shop/new-greens",
            normalized_url_hash="testing",
            quality_label="clicked",
            score=20,
            clicks=6,
            impressions=200,
        )
        fix_page = GoogleAdsLandingPageCandidate(
            id=3,
            account_id=637,
            url="https://nutricity.ca/shop/low-impression",
            normalized_url="https://nutricity.ca/shop/low-impression",
            normalized_url_hash="fix",
            quality_label="watch",
            score=5,
            clicks=1,
            impressions=90,
        )
        waste_page = GoogleAdsLandingPageCandidate(
            id=4,
            account_id=637,
            url="https://nutricity.ca/shop/free-samples",
            normalized_url="https://nutricity.ca/shop/free-samples",
            normalized_url_hash="waste",
            quality_label="clicked",
            score=1,
            clicks=25,
            impressions=500,
            cost=40,
        )
        scale_plan = scale_landing_page_plan([scale_page])
        migration = {
            "pages": {
                "https://nutricity.ca/shop/vitamin-c": {
                    "promoted_at_utc": (now - timedelta(days=8)).isoformat(),
                    "scale_conversions": 2,
                    "scale_conversion_value": 160,
                }
            }
        }
        scale_exclusions = testing_scale_page_exclusion_plan(scale_plan, migration_state=migration, now=now)
        waste_plan = waste_category_plan([], [], [waste_page], now=now)

        governance = landing_page_category_governance_plan(
            [scale_page, testing_page, fix_page, waste_page],
            website_url="https://nutricity.ca",
            scale_page_plan=scale_plan,
            scale_page_exclusion_plan=scale_exclusions,
            waste_plan=waste_plan,
            all_page_rows=[scale_page, testing_page, fix_page, waste_page],
        )

        categories = {item["name"]: item for item in governance["categories"]}
        self.assertEqual(categories["Core / Scale"]["included_page_count"], 1)
        self.assertEqual(categories["Waste / Recovery"]["active_excluded_page_count"], 1)
        testing_urls = {item["url"] for item in categories["Testing / Discovery"]["candidate_pages"]}
        testing_excluded_urls = {item["url"] for item in categories["Testing / Discovery"]["excluded_pages"]}
        self.assertNotIn("https://nutricity.ca/shop/vitamin-c", testing_urls)
        self.assertIn("https://nutricity.ca/shop/vitamin-c", testing_excluded_urls)
        self.assertIn("https://nutricity.ca/shop/free-samples", testing_excluded_urls)
        fix_urls = {item["url"] for item in categories["Fix / Watch"]["candidate_pages"]}
        self.assertIn("https://nutricity.ca/shop/low-impression", fix_urls)
        self.assertNotIn("https://nutricity.ca/shop/free-samples", fix_urls)

    def test_peak_timezone_fetch_respects_quota(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            schedule_timezone="UTC",
            peak_budget_warmup_minutes=60,
            peak_budget_restore_delay_minutes=0,
            peak_budget_increase_pct=0.5,
        )
        quota = {"allowed": False, "status": "quota_deferred", "reason": "quota full"}

        with patch("app.services.google_ads_automation.reserve_automation_quota_units", return_value=quota), patch(
            "app.services.google_ads_automation.fetch_account_time_zone"
        ) as fetch_timezone, patch(
            "app.services.google_ads_automation.peak_conversion_schedule_decision",
            return_value={"status": "fallback", "peak_hour": None},
        ):
            decision = refresh_peak_budget_decision(self.FakeSession(), preference, fetch_time_zone=True)

        fetch_timezone.assert_not_called()
        self.assertEqual(decision["timezone_quota"], quota)

    def test_daily_insight_window_becomes_incremental_after_all_time_baseline(self) -> None:
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            daily_keyword_lookback_days=60,
            last_all_time_pull_at=datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc),
            last_keyword_pull_at=datetime(2026, 6, 13, 3, 0, tzinfo=timezone.utc),
        )

        days, mode = daily_insight_api_window_days(
            preference,
            now=datetime(2026, 6, 15, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(days, 3)
        self.assertEqual(mode, "incremental_since_last_daily_pull")

    def test_daily_insight_window_uses_configured_window_before_baseline(self) -> None:
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            daily_keyword_lookback_days=45,
            last_all_time_pull_at=None,
        )

        days, mode = daily_insight_api_window_days(preference, now=datetime(2026, 6, 15, tzinfo=timezone.utc))

        self.assertEqual(days, 45)
        self.assertEqual(mode, "baseline_recent_window_before_all_time_import")

    def test_local_peak_window_covers_warmup_through_restore(self) -> None:
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            automation_enabled=True,
            auto_peak_budget_enabled=True,
            schedule_timezone="UTC",
            peak_budget_warmup_minutes=60,
            peak_budget_restore_delay_minutes=0,
            peak_budget_decision_json={"peak_hour": 2},
        )
        window = local_peak_window(preference, now=datetime(2026, 6, 12, 1, 30, tzinfo=timezone.utc))

        self.assertEqual(window["status"], "ready")
        self.assertTrue(window["in_boost_window"])
        self.assertFalse(window["after_restore"])
        self.assertEqual(window["boost_start"].hour, 1)
        self.assertEqual(window["peak_start"].hour, 2)
        self.assertEqual(window["restore_at"].hour, 3)

    def test_peak_budget_due_honors_hourly_check_interval(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            automation_enabled=True,
            auto_peak_budget_enabled=True,
            schedule_timezone="UTC",
            peak_budget_warmup_minutes=60,
            peak_budget_restore_delay_minutes=0,
            peak_budget_check_interval_minutes=60,
            peak_budget_decision_json={"peak_hour": 2, "peak_hour_conversions": 3},
        )
        now = datetime(2026, 6, 12, 1, 30, tzinfo=timezone.utc)
        with patch("app.services.google_ads_automation.load_peak_budget_state", return_value={}):
            self.assertTrue(peak_budget_due_now(None, preference, now=now))
            preference.last_peak_budget_check_at = datetime(2026, 6, 12, 1, 0, tzinfo=timezone.utc)
            self.assertFalse(peak_budget_due_now(None, preference, now=now))
            preference.last_peak_budget_check_at = datetime(2026, 6, 12, 0, 30, tzinfo=timezone.utc)
            self.assertTrue(peak_budget_due_now(None, preference, now=now))

    def test_peak_budget_restore_due_detects_active_restore_window(self) -> None:
        account = GoogleAdsAccount(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            automation_enabled=True,
            auto_peak_budget_enabled=True,
        )
        state = {"active": True, "restore_at_utc": "2026-06-12T03:00:00+00:00"}

        with patch("app.services.google_ads_automation.load_peak_budget_state", return_value=state):
            self.assertFalse(
                peak_budget_restore_due_now(
                    None,
                    preference,
                    now=datetime(2026, 6, 12, 2, 59, tzinfo=timezone.utc),
                )
            )
            self.assertTrue(
                peak_budget_restore_due_now(
                    None,
                    preference,
                    now=datetime(2026, 6, 12, 3, 0, tzinfo=timezone.utc),
                )
            )

    def test_sales_room_caps_peak_budget_increase(self) -> None:
        operations = [
            {
                "budget_resource_name": "customers/1/campaignBudgets/1",
                "currency_code": "INR",
                "old_amount_micros": 100_000_000,
                "new_amount_micros": 150_000_000,
                "old_amount": 100,
                "new_amount": 150,
            }
        ]
        guard = {"status": "amber", "daily_spend_room_inr": 25, "remaining_spend_room_inr": 175}

        capped, skipped = cap_budget_increase_operations_by_sales_room(
            operations,
            guard,
            rates={"INR": 1.0, "USD": 83.0},
        )

        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0]["new_amount_micros"], 125_000_000)
        self.assertTrue(capped[0]["odoo_sales_room_limited"])
        self.assertIn("odoo_sales_room_scaled", {item["reason"] for item in skipped})

    def test_sales_room_red_blocks_peak_budget_increase(self) -> None:
        capped, skipped = cap_budget_increase_operations_by_sales_room(
            [{"budget_resource_name": "budget/1", "currency_code": "INR", "old_amount_micros": 100, "new_amount_micros": 150}],
            {"status": "red", "spend_ratio": 0.18, "max_ratio": 0.15},
            rates={"INR": 1.0},
        )

        self.assertEqual(capped, [])
        self.assertEqual(skipped[0]["reason"], "odoo_sales_cap_red")

    def test_budget_reduction_targets_only_underperforming_campaigns(self) -> None:
        budget_rows = [
            {
                "campaign_id": 10,
                "campaign_name": "No Sales",
                "budget_resource_name": "customers/1/campaignBudgets/10",
                "budget_name": "No Sales budget",
                "currency_code": "AUD",
                "amount_micros": 20_000_000,
                "explicitly_shared": False,
            },
            {
                "campaign_id": 20,
                "campaign_name": "Winner",
                "budget_resource_name": "customers/1/campaignBudgets/20",
                "budget_name": "Winner budget",
                "currency_code": "AUD",
                "amount_micros": 20_000_000,
                "explicitly_shared": False,
            },
        ]
        performance_rows = [
            {"campaign_id": 10, "cost": 40, "clicks": 12, "conversions": 0, "all_conversions": 0},
            {"campaign_id": 20, "cost": 50, "clicks": 20, "conversions": 2, "all_conversions": 0},
        ]

        operations, skipped = underperforming_budget_reduction_operations(
            budget_rows,
            performance_rows,
            reduce_pct=0.20,
            minimum_daily_budget_amount=5,
        )

        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["campaign_ids"], [10])
        self.assertEqual(operations[0]["new_amount_micros"], 16_000_000)
        self.assertIn("has_purchase_conversion_signal", {item["reason"] for item in skipped})

    def test_budget_reduction_skips_fixed_waste_budget_campaigns(self) -> None:
        budget_rows = [
            {
                "campaign_id": 10,
                "campaign_name": "AUTO | Waste / Recovery | Validation | AUTO-WASTE123",
                "budget_resource_name": "customers/1/campaignBudgets/10",
                "budget_name": "Waste fixed budget",
                "currency_code": "CAD",
                "amount_micros": 50_000_000,
                "explicitly_shared": False,
            }
        ]
        performance_rows = [
            {"campaign_id": 10, "cost": 40, "clicks": 20, "conversions": 0, "all_conversions": 0},
        ]

        operations, skipped = underperforming_budget_reduction_operations(
            budget_rows,
            performance_rows,
            reduce_pct=0.20,
            minimum_daily_budget_amount=5,
        )

        self.assertEqual(operations, [])
        self.assertEqual(skipped[0]["reason"], "fixed_waste_budget_protected")

    def test_peak_budget_selection_skips_fixed_waste_budget_campaigns(self) -> None:
        budget_rows = [
            {
                "campaign_id": 10,
                "campaign_name": "AUTO | Waste / Recovery | Validation | AUTO-WASTE123",
                "budget_resource_name": "customers/1/campaignBudgets/10",
                "budget_name": "Waste fixed budget",
                "currency_code": "CAD",
                "amount_micros": 50_000_000,
                "explicitly_shared": False,
            }
        ]
        decision = {"campaign_ids": [10]}

        operations, skipped = _selected_peak_budget_operations(budget_rows, decision, 0.5)

        self.assertEqual(operations, [])
        self.assertEqual(skipped[0]["reason"], "fixed_waste_budget_protected")

    def test_budget_reduction_fallback_targets_weak_conversion_efficiency(self) -> None:
        budget_rows = [
            {
                "campaign_id": 10,
                "campaign_name": "Weak Value",
                "budget_resource_name": "customers/1/campaignBudgets/10",
                "budget_name": "Weak Value budget",
                "currency_code": "AUD",
                "amount_micros": 20_000_000,
                "explicitly_shared": False,
            },
            {
                "campaign_id": 20,
                "campaign_name": "Efficient Value",
                "budget_resource_name": "customers/1/campaignBudgets/20",
                "budget_name": "Efficient Value budget",
                "currency_code": "AUD",
                "amount_micros": 20_000_000,
                "explicitly_shared": False,
            },
        ]
        performance_rows = [
            {"campaign_id": 10, "cost": 100, "clicks": 20, "conversions": 2, "all_conversions": 0, "conversion_value": 200},
            {"campaign_id": 20, "cost": 100, "clicks": 20, "conversions": 2, "all_conversions": 0, "conversion_value": 800},
        ]

        operations, skipped = underperforming_budget_reduction_operations(
            budget_rows,
            performance_rows,
            reduce_pct=0.20,
            minimum_daily_budget_amount=5,
            allow_conversion_efficiency_reductions=True,
            required_roas=6.67,
        )

        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["campaign_ids"], [10])
        self.assertEqual(operations[0]["reduction_reason"], "weak_conversion_efficiency_against_odoo_sales_cap")
        self.assertIn("efficient_google_value_signal", {item["reason"] for item in skipped})

    def test_budget_reduction_force_trims_when_account_cap_is_red(self) -> None:
        budget_rows = [
            {
                "campaign_id": 10,
                "campaign_name": "Google Efficient But Sales Cap Red",
                "budget_resource_name": "customers/1/campaignBudgets/10",
                "budget_name": "Cap budget",
                "currency_code": "AUD",
                "amount_micros": 20_000_000,
                "explicitly_shared": False,
            }
        ]
        performance_rows = [
            {"campaign_id": 10, "cost": 100, "clicks": 20, "conversions": 2, "all_conversions": 0, "conversion_value": 900},
        ]

        operations, skipped = underperforming_budget_reduction_operations(
            budget_rows,
            performance_rows,
            reduce_pct=0.20,
            minimum_daily_budget_amount=5,
            allow_conversion_efficiency_reductions=True,
            required_roas=6.67,
            force_account_cap_trim=True,
        )

        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["reduction_reason"], "account_over_odoo_sales_cap_proportional_trim")
        self.assertIn("efficient_google_value_signal", {item["reason"] for item in skipped})

    def test_budget_guard_quota_defers_before_google_budget_fetch(self) -> None:
        account = GoogleAdsAccount(
            id=637,
            name="Nutricity CA",
            customer_id="3495463031",
            manager_customer_id="1234567890",
        )
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            odoo_sales_guard_enabled=True,
            odoo_sales_max_spend_ratio=0.15,
            odoo_sales_guard_window_days=7,
            minimum_daily_budget_amount=5,
            underperforming_budget_reduce_pct=0.2,
        )
        quota = {"allowed": False, "status": "quota_deferred", "reason": "quota full"}

        with patch(
            "app.services.google_ads_automation.odoo_sales_budget_guard_for_account",
            return_value={"status": "red", "sales_inr": 1000, "ad_cost_inr": 200},
        ), patch("app.services.google_ads_automation.reserve_automation_quota_units", return_value=quota), patch(
            "app.services.google_ads_automation.build_client"
        ) as build_client:
            result = run_odoo_sales_budget_guard(self.FakeSession(), preference)

        build_client.assert_not_called()
        self.assertEqual(result["status"], "deferred_quota")
        self.assertEqual(result["quota"], quota)

    def test_peak_budget_boost_quota_defers_before_google_budget_fetch(self) -> None:
        account = GoogleAdsAccount(
            id=637,
            name="Nutricity CA",
            customer_id="3495463031",
            manager_customer_id="1234567890",
        )
        preference = GoogleAdsAutomationPreference(
            account_id=637,
            account=account,
            automation_enabled=True,
            auto_peak_budget_enabled=True,
            schedule_timezone="UTC",
        )
        now = datetime(2026, 6, 12, 1, 30, tzinfo=timezone.utc)
        window = {
            "status": "ready",
            "in_boost_window": True,
            "window_key": "2026-06-12T02:00:00+00:00",
            "boost_start": datetime(2026, 6, 12, 1, 0, tzinfo=timezone.utc),
            "peak_start": datetime(2026, 6, 12, 2, 0, tzinfo=timezone.utc),
            "restore_at": datetime(2026, 6, 12, 3, 0, tzinfo=timezone.utc),
            "time_zone": "UTC",
        }
        quota = {"allowed": False, "status": "quota_deferred", "reason": "quota full"}

        with patch(
            "app.services.google_ads_automation.refresh_peak_budget_decision",
            return_value={
                "status": "decided",
                "peak_hour": 2,
                "peak_hour_conversions": 3,
                "peak_time": "02:00",
                "boost_start_time": "01:00",
                "restore_time": "03:00",
                "campaign_ids": [10],
            },
        ), patch("app.services.google_ads_automation.local_peak_window", return_value=window), patch(
            "app.services.google_ads_automation.load_peak_budget_state",
            return_value={},
        ), patch(
            "app.services.google_ads_automation.peak_budget_mutation_guard",
            return_value={"can_mutate": False, "validate_only": True},
        ), patch(
            "app.services.google_ads_automation.reserve_automation_quota_units",
            return_value=quota,
        ), patch("app.services.google_ads_automation.build_client") as build_client:
            result = run_peak_budget_transition(self.FakeSession(), preference, now=now)

        build_client.assert_not_called()
        self.assertEqual(result["status"], "deferred_quota")
        self.assertEqual(result["action"], "boost")
        self.assertEqual(result["quota"], quota)


if __name__ == "__main__":
    unittest.main()
