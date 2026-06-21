from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.ads.googleads.v24.errors.types.errors import GoogleAdsFailure

from app.services.google_ads_live_campaign_creator import (
    _clean_search_theme,
    _create_ad_group_webpage_inclusions,
    _campaign_by_code_or_lane,
    _contains_shipping_offer_text,
    _criteria_daily_reservation_plan,
    _create_search_campaign,
    _create_search_theme_signals,
    _draft_live_creation_control,
    _existing_asset_group_audience_signals,
    _is_replaced_legacy_pmax_draft,
    _live_creation_batch_status,
    _pmax_asset_group_copy_assets,
    _pmax_final_urls_from_assets,
    _restricted_terms_from_policy_strings,
    _successful_resource_names,
    _url_inclusion_urls_from_draft,
    publish_automation_campaigns,
    publish_pmax_draft,
    publish_rsa_draft,
)


class GoogleAdsLiveCampaignCreatorTests(unittest.TestCase):
    class FakeRepeated(list):
        def append(self, item) -> None:
            super().append(item)

        def extend(self, items) -> None:
            super().extend(items)

    class FakeSearchCampaign:
        def __init__(self) -> None:
            self.name = ""
            self.advertising_channel_type = None
            self.status = None
            self.campaign_budget = ""
            self.target_spend = SimpleNamespace(cpc_bid_ceiling_micros=0)
            self.maximize_conversion_value = SimpleNamespace(target_roas=0)
            self.dynamic_search_ads_setting = SimpleNamespace(domain_name="", language_code="")
            self.ai_max_setting = SimpleNamespace(enable_ai_max=False)
            self._pb = SimpleNamespace(
                maximize_conversion_value=SimpleNamespace(CopyFrom=lambda _value: None),
                manual_cpc=SimpleNamespace(CopyFrom=lambda _value: None),
            )

    class FakeCampaignOperation:
        def __init__(self) -> None:
            self.create = GoogleAdsLiveCampaignCreatorTests.FakeSearchCampaign()
            self.update = SimpleNamespace(
                resource_name="",
                network_settings=SimpleNamespace(
                    target_google_search=False,
                    target_search_network=False,
                    target_partner_search_network=False,
                    target_content_network=False,
                ),
                ai_max_setting=SimpleNamespace(enable_ai_max=False),
                maximize_conversion_value=SimpleNamespace(target_roas=0),
                target_spend=SimpleNamespace(cpc_bid_ceiling_micros=0),
                _pb=SimpleNamespace(maximize_conversion_value=SimpleNamespace(CopyFrom=lambda _value: None)),
            )
            self.update_mask = SimpleNamespace(paths=GoogleAdsLiveCampaignCreatorTests.FakeRepeated())

    class FakeMutateRequest:
        def __init__(self) -> None:
            self.customer_id = ""
            self.operations = GoogleAdsLiveCampaignCreatorTests.FakeRepeated()
            self.validate_only = False

    class FakeCampaignService:
        def __init__(self) -> None:
            self.last_request = None

        def mutate_campaigns(self, request):
            self.last_request = request
            return SimpleNamespace(results=[SimpleNamespace(resource_name="customers/3495463031/campaigns/123")])

    class FakeGoogleAdsClient:
        def __init__(self) -> None:
            self.enums = SimpleNamespace(
                AdvertisingChannelTypeEnum=SimpleNamespace(SEARCH="SEARCH"),
                CampaignStatusEnum=SimpleNamespace(ENABLED="ENABLED"),
            )
            self.campaign_service = GoogleAdsLiveCampaignCreatorTests.FakeCampaignService()

        def get_type(self, name):
            if name == "CampaignOperation":
                return GoogleAdsLiveCampaignCreatorTests.FakeCampaignOperation()
            if name == "MutateCampaignsRequest":
                return GoogleAdsLiveCampaignCreatorTests.FakeMutateRequest()
            if name in {"MaximizeConversionValue", "ManualCpc"}:
                return SimpleNamespace(_pb=SimpleNamespace())
            return SimpleNamespace()

        def get_service(self, name):
            if name == "CampaignService":
                return self.campaign_service
            return SimpleNamespace()

    class FakeScalars:
        def __init__(self, rows) -> None:
            self.rows = rows

        def all(self):
            return self.rows

    class FakeSession:
        def __init__(self, rows) -> None:
            self.rows = rows
            self.added = []
            self.flushes = 0

        def scalars(self, *_args, **_kwargs):
            return GoogleAdsLiveCampaignCreatorTests.FakeScalars(self.rows)

        def add(self, item) -> None:
            self.added.append(item)

        def flush(self) -> None:
            self.flushes += 1

    def test_live_creation_respects_global_mutation_guard(self) -> None:
        account = SimpleNamespace(
            id=637,
            name="Nutricity CA",
            customer_id="3495463031",
            manager_customer_id="5748718474",
            connection=None,
        )
        preference = SimpleNamespace(
            account=account,
            account_id=637,
            monitor_only=False,
        )

        with patch(
            "app.services.google_ads_live_campaign_creator.get_sync_setting_map",
            return_value={"optimizer.allow_mutations": False, "optimizer.dry_run": True},
        ), patch("app.services.google_ads_live_campaign_creator.build_client") as build_client:
            result = publish_automation_campaigns(SimpleNamespace(), preference, validate_only=False)

        self.assertEqual(result["status"], "blocked_by_mutation_guard")
        self.assertFalse(result["guard"]["optimizer_allow_mutations"])
        self.assertTrue(result["guard"]["optimizer_dry_run"])
        build_client.assert_not_called()

    def test_live_creation_gate_skips_draft_when_policy_holds_it(self) -> None:
        account = SimpleNamespace(
            id=637,
            name="Nutricity CA",
            customer_id="3495463031",
            manager_customer_id="5748718474",
            connection=None,
        )
        preference = SimpleNamespace(
            account=account,
            account_id=637,
            monitor_only=False,
        )
        draft = SimpleNamespace(
            id=41,
            ad_type="pmax",
            status="publish_ready",
            generated_assets={
                "automation": {"source_key": "automation:637:nutricity.ca:pmax_scale_after_7d_conversions_s001"},
                "campaign_identity": {
                    "campaign_code": "AUTO-PMAX",
                    "campaign_name": "AUTO | Core / Scale | PMax Target ROAS Scale S001 | AUTO-PMAX",
                },
                "live_creation": {
                    "enabled": False,
                    "phase": "draft_until_conversion_gate",
                    "reason": "Held until conversion gate.",
                },
            },
        )
        session = self.FakeSession([draft])

        with patch(
            "app.services.google_ads_live_campaign_creator.get_sync_setting_map",
            return_value={"optimizer.allow_mutations": True, "optimizer.dry_run": False},
        ), patch(
            "app.services.google_ads_live_campaign_creator.build_client",
            return_value=SimpleNamespace(),
        ), patch(
            "app.services.google_ads_live_campaign_creator.sync_restricted_policy_terms",
            return_value={"status": "skipped"},
        ):
            result = publish_automation_campaigns(session, preference, validate_only=False)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["results"][0]["status"], "skipped_live_gate")
        self.assertEqual(result["results"][0]["live_creation"]["phase"], "draft_until_conversion_gate")
        self.assertEqual(_draft_live_creation_control(draft)["enabled"], False)

    def test_rsa_creation_blocks_budget_guarded_draft_before_google_lookup(self) -> None:
        account = SimpleNamespace(id=637, name="Nutricity CA", customer_id="3495463031")
        preference = SimpleNamespace(account=account, account_id=637, minimum_daily_budget_amount=1)
        draft = SimpleNamespace(
            id=19,
            ad_type="rsa",
            website_url="https://nutricity.ca",
            final_url="https://nutricity.ca",
            generated_assets={
                "campaign_identity": {
                    "campaign_code": "AUTO-TST",
                    "campaign_name": "AUTO | Testing / Discovery | RSA Max Clicks Keywords | AUTO-TST",
                },
                "bidding": {
                    "daily_budget": 0,
                    "budget_blocked": True,
                    "budget_block_reason": "Odoo sales guard has no remaining spend room.",
                },
            },
        )

        with patch(
            "app.services.google_ads_live_campaign_creator.get_sync_setting_map",
            return_value={"automation.force_minimum_budget_when_budget_guard_blocked": False},
        ), patch("app.services.google_ads_live_campaign_creator._campaign_by_code") as campaign_by_code:
            result = publish_rsa_draft(SimpleNamespace(), SimpleNamespace(), account, preference, draft)

        self.assertEqual(result["status"], "blocked_by_budget_guard")
        self.assertEqual(result["requested_daily_budget"], 0)
        self.assertIn("no remaining spend room", result["reason"])
        campaign_by_code.assert_not_called()

    def test_batch_status_reports_all_guarded_results_as_blocked(self) -> None:
        status = _live_creation_batch_status(
            [
                {"status": "blocked_by_budget_guard"},
                {"status": "skipped_dsa_disabled"},
            ],
            [],
        )

        self.assertEqual(status, "blocked")
        self.assertEqual(_live_creation_batch_status([{"status": "validated"}], []), "done")
        self.assertEqual(_live_creation_batch_status([{"status": "validated"}], [{"status": "failed"}]), "partial")

    def test_url_inclusion_urls_dedupe_from_draft_assets(self) -> None:
        draft = SimpleNamespace(
            generated_assets={
                "url_inclusion_targets": [
                    {"url": "https://nutricity.ca/collections/vitamins/"},
                    {"url": "https://nutricity.ca/collections/vitamins"},
                    {"final_url": "https://nutricity.ca/products/magnesium"},
                ],
                "landing_page_candidates": [{"url": "https://nutricity.ca/products/ignored"}],
            }
        )

        self.assertEqual(
            _url_inclusion_urls_from_draft(draft),
            [
                "https://nutricity.ca/collections/vitamins",
                "https://nutricity.ca/products/magnesium",
                "https://nutricity.ca/products/ignored",
            ],
        )

    def test_url_inclusion_urls_keeps_full_source_pool_for_progressive_sync(self) -> None:
        draft = SimpleNamespace(
            generated_assets={
                "url_inclusion_targets": [
                    {"url": f"https://nutricity.ca/products/item-{index}"} for index in range(510)
                ]
            }
        )

        urls = _url_inclusion_urls_from_draft(draft)

        self.assertEqual(len(urls), 510)
        self.assertEqual(urls[0], "https://nutricity.ca/products/item-0")
        self.assertEqual(urls[-1], "https://nutricity.ca/products/item-509")

    def test_url_inclusion_urls_drop_private_order_pages(self) -> None:
        draft = SimpleNamespace(
            generated_assets={
                "url_inclusion_targets": [
                    {"url": "https://nutricity.ca/my/orders/5853?access_token=secret&hash=abc&pid=2707"},
                    {"url": "https://nutricity.ca/shop/brswabc-public-product"},
                ]
            }
        )

        self.assertEqual(
            _url_inclusion_urls_from_draft(draft),
            ["https://nutricity.ca/shop/brswabc-public-product"],
        )

    def test_pmax_final_urls_use_stored_core_url_targets(self) -> None:
        urls = _pmax_final_urls_from_assets(
            {
                "url_inclusion_targets": [
                    {"url": "https://nutricity.ca/shop/vitamin-c/"},
                    {"url": "https://nutricity.ca/shop/vitamin-c"},
                    {"url": "https://nutricity.ca/shop/magnesium"},
                ]
            },
            "https://nutricity.ca",
        )

        self.assertEqual(
            urls,
            [
                "https://nutricity.ca/shop/vitamin-c",
                "https://nutricity.ca/shop/magnesium",
            ],
        )

    def test_pmax_final_urls_keep_full_core_target_pool(self) -> None:
        urls = _pmax_final_urls_from_assets(
            {
                "url_inclusion_targets": [
                    {"url": f"https://nutricity.ca/shop/category-{index}"} for index in range(25)
                ]
            },
            "https://nutricity.ca",
        )

        self.assertEqual(len(urls), 25)
        self.assertEqual(urls[0], "https://nutricity.ca/shop/category-0")
        self.assertEqual(urls[-1], "https://nutricity.ca/shop/category-24")

    def test_dynamic_url_inclusions_use_exact_url_operator(self) -> None:
        class FakeAdGroupCriterionOperation:
            def __init__(self) -> None:
                self.create = SimpleNamespace(
                    ad_group="",
                    status=None,
                    webpage=SimpleNamespace(criterion_name="", conditions=GoogleAdsLiveCampaignCreatorTests.FakeRepeated()),
                )

        class FakeCriterionService:
            def __init__(self) -> None:
                self.last_request = None

            def mutate_ad_group_criteria(self, request):
                self.last_request = request
                return SimpleNamespace(
                    results=[
                        SimpleNamespace(resource_name=f"customers/3495463031/adGroupCriteria/{index}")
                        for index, _operation in enumerate(request.operations, start=1)
                    ]
                )

        class FakeClient:
            def __init__(self) -> None:
                self.enums = SimpleNamespace(
                    AdGroupCriterionStatusEnum=SimpleNamespace(ENABLED="ENABLED"),
                    WebpageConditionOperandEnum=SimpleNamespace(URL="URL"),
                    WebpageConditionOperatorEnum=SimpleNamespace(EQUALS="EQUALS", CONTAINS="CONTAINS"),
                )
                self.criterion_service = FakeCriterionService()

            def get_type(self, name):
                if name == "AdGroupCriterionOperation":
                    return FakeAdGroupCriterionOperation()
                if name == "WebpageConditionInfo":
                    return SimpleNamespace(operand=None, operator=None, argument="")
                if name == "MutateAdGroupCriteriaRequest":
                    request = GoogleAdsLiveCampaignCreatorTests.FakeMutateRequest()
                    request.partial_failure = False
                    return request
                return SimpleNamespace()

            def get_service(self, name):
                if name == "AdGroupCriterionService":
                    return self.criterion_service
                return SimpleNamespace()

        client = FakeClient()
        resources = _create_ad_group_webpage_inclusions(
            client,
            SimpleNamespace(customer_id="3495463031"),
            ad_group_resource_name="customers/3495463031/adGroups/99",
            urls=["https://nutricity.ca/shop/vitamins", "https://nutricity.ca/shop/minerals"],
            validate_only=False,
        )

        self.assertEqual(len(resources), 2)
        first_condition = client.criterion_service.last_request.operations[0].create.webpage.conditions[0]
        self.assertEqual(first_condition.operand, "URL")
        self.assertEqual(first_condition.operator, "EQUALS")
        self.assertEqual(first_condition.argument, "https://nutricity.ca/shop/vitamins")

    def test_successful_resource_names_ignores_partial_failure_empty_rows(self) -> None:
        response = SimpleNamespace(
            partial_failure_error=SimpleNamespace(code=1, details=[], message="operation failed"),
            results=[
                SimpleNamespace(resource_name="customers/3495463031/assets/1"),
                SimpleNamespace(resource_name=""),
                SimpleNamespace(resource_name="customers/3495463031/assets/3"),
            ],
        )

        self.assertEqual(
            _successful_resource_names(response),
            ["customers/3495463031/assets/1", "customers/3495463031/assets/3"],
        )

    def test_create_search_campaign_defaults_to_conversion_value_not_max_clicks(self) -> None:
        client = self.FakeGoogleAdsClient()
        account = SimpleNamespace(customer_id="3495463031")

        resource_name = _create_search_campaign(
            client,
            account,
            name="AUTO | Testing / Discovery | RSA Target ROAS Keywords | AUTO-TST",
            budget_resource_name="customers/3495463031/campaignBudgets/1",
            max_cpc_micros=2_500_000,
            bidding={},
            validate_only=False,
        )

        campaign = client.campaign_service.last_request.operations[0].create
        self.assertEqual(resource_name, "customers/3495463031/campaigns/123")
        self.assertEqual(campaign.target_spend.cpc_bid_ceiling_micros, 0)

    def test_create_search_campaign_can_enable_ai_max_for_dsa_lane(self) -> None:
        client = self.FakeGoogleAdsClient()
        account = SimpleNamespace(customer_id="3495463031")

        _create_search_campaign(
            client,
            account,
            name="AUTO | Testing / Discovery | DSA AI Max Target ROAS Page Discovery | AUTO-TST",
            budget_resource_name="customers/3495463031/campaignBudgets/1",
            max_cpc_micros=2_500_000,
            bidding={"strategy": "maximize_conversion_value_target_roas", "target_roas": 5.0},
            domain_name="nutricity.ca",
            enable_ai_max=True,
            validate_only=False,
        )

        campaign = client.campaign_service.last_request.operations[0].create
        self.assertTrue(campaign.ai_max_setting.enable_ai_max)
        self.assertEqual(campaign.dynamic_search_ads_setting.domain_name, "nutricity.ca")
        self.assertEqual(campaign.maximize_conversion_value.target_roas, 5.0)

    def test_campaign_lookup_falls_back_to_live_lane_match(self) -> None:
        client = self.FakeGoogleAdsClient()
        account = SimpleNamespace(customer_id="8976162539")
        desired_name = "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-BBBBBBBBBB"
        rows = [
            SimpleNamespace(
                campaign=SimpleNamespace(
                    id=101,
                    name="AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-AAAAAAAAAA",
                    resource_name="customers/8976162539/campaigns/101",
                    status=SimpleNamespace(name="PAUSED"),
                    advertising_channel_type=SimpleNamespace(name="SEARCH"),
                ),
                campaign_budget=SimpleNamespace(amount_micros=50_000_000),
            ),
            SimpleNamespace(
                campaign=SimpleNamespace(
                    id=202,
                    name="AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-CCCCCCCCCC",
                    resource_name="customers/8976162539/campaigns/202",
                    status=SimpleNamespace(name="ENABLED"),
                    advertising_channel_type=SimpleNamespace(name="SEARCH"),
                ),
                campaign_budget=SimpleNamespace(amount_micros=21_970_000),
            ),
        ]

        with patch("app.services.google_ads_live_campaign_creator._campaign_by_code", return_value=None), patch(
            "app.services.google_ads_live_campaign_creator._google_ads_search",
            return_value=rows,
        ):
            campaign = _campaign_by_code_or_lane(client, account, "AUTO-BBBBBBBBBB", desired_name)

        self.assertIsNotNone(campaign)
        self.assertEqual(campaign["campaign_id"], 202)
        self.assertEqual(campaign["campaign_code"], "AUTO-CCCCCCCCCC")
        self.assertEqual(campaign["matched_by"], "campaign_lane_live_fallback")

    def test_successful_resource_names_ignores_mutate_operation_partial_failures(self) -> None:
        failure = GoogleAdsFailure()
        failure.errors.append({})
        failure.errors[0].message = "failed"
        failure.errors[0].location.field_path_elements.append(
            {"field_name": "mutate_operations", "index": 1}
        )
        response = SimpleNamespace(
            partial_failure_error=SimpleNamespace(
                code=3,
                details=[SimpleNamespace(value=GoogleAdsFailure.serialize(failure))],
            ),
            results=[
                SimpleNamespace(resource_name="customers/3495463031/assetGroupSignals/1"),
                SimpleNamespace(resource_name="customers/3495463031/assetGroupSignals/rejected"),
                SimpleNamespace(resource_name="customers/3495463031/assetGroupSignals/3"),
            ],
        )

        self.assertEqual(
            _successful_resource_names(response),
            [
                "customers/3495463031/assetGroupSignals/1",
                "customers/3495463031/assetGroupSignals/3",
            ],
        )

    def test_search_theme_creation_skips_campaign_reserved_terms(self) -> None:
        account = SimpleNamespace(customer_id="3495463031")
        reserved = {"vitamin c canada"}
        with patch(
            "app.services.google_ads_live_campaign_creator._existing_asset_group_search_themes",
            return_value=set(),
        ), patch(
            "app.services.google_ads_live_campaign_creator._mutate_search_theme_signals_rest",
            return_value=["created"],
        ) as mutate:
            result = _create_search_theme_signals(
                SimpleNamespace(),
                account,
                asset_group_resource_name="customers/3495463031/assetGroups/1",
                search_themes=["Vitamin C Canada", "Magnesium Canada"],
                campaign_theme_keys=reserved,
                validate_only=False,
            )

        self.assertEqual(result, ["created"])
        self.assertIn("magnesium canada", reserved)
        operations = mutate.call_args.kwargs["operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(
            operations[0]["assetGroupSignalOperation"]["create"]["searchTheme"]["text"],
            "Magnesium Canada",
        )

    def test_existing_asset_group_audience_signals_reads_existing_audiences(self) -> None:
        rows = [
            SimpleNamespace(
                asset_group_signal=SimpleNamespace(
                    audience=SimpleNamespace(audience="customers/3495463031/audiences/1")
                )
            ),
            SimpleNamespace(
                asset_group_signal=SimpleNamespace(
                    audience=SimpleNamespace(audience="customers/3495463031/audiences/2")
                )
            ),
            SimpleNamespace(
                asset_group_signal=SimpleNamespace(audience=SimpleNamespace(audience=""))
            ),
        ]
        service = SimpleNamespace(search=lambda customer_id, query: rows)
        client = SimpleNamespace(get_service=lambda name: service)
        account = SimpleNamespace(customer_id="3495463031")

        result = _existing_asset_group_audience_signals(
            client,
            account,
            asset_group_resource_name="customers/3495463031/assetGroups/1",
        )

        self.assertEqual(
            result,
            {"customers/3495463031/audiences/1", "customers/3495463031/audiences/2"},
        )

    def test_clean_search_theme_removes_google_disallowed_symbols(self) -> None:
        self.assertEqual(
            _clean_search_theme("5% Off A&B | Immune-Support (Canada) with Extra Long Product Title"),
            "5 Off A B Immune Support Canada with Extra Long",
        )

    def test_daily_criteria_reservation_spreads_budget_across_scopes(self) -> None:
        allowed_one, state = _criteria_daily_reservation_plan(
            {},
            limit=1000,
            scope_count=4,
            scope_key="exact_keywords:adgroup-1",
            kind="exact_keywords",
            requested=900,
        )
        allowed_two, state = _criteria_daily_reservation_plan(
            state,
            limit=1000,
            scope_count=4,
            scope_key="ad_group_url_inclusions:adgroup-2",
            kind="ad_group_url_inclusions",
            requested=900,
        )
        allowed_one_again, state = _criteria_daily_reservation_plan(
            state,
            limit=1000,
            scope_count=4,
            scope_key="exact_keywords:adgroup-1",
            kind="exact_keywords",
            requested=900,
        )

        self.assertEqual(allowed_one, 250)
        self.assertEqual(allowed_two, 250)
        self.assertEqual(allowed_one_again, 0)
        self.assertEqual(state["used_items"], 500)
        self.assertEqual(state["remaining_items"], 500)

    def test_pmax_group_copy_is_theme_specific_and_filters_shipping_offer(self) -> None:
        account = SimpleNamespace(name="Nutricity CA")
        assets = {
            "headlines": ["Free Delivery Over CA$280", "Supplements Online"],
            "long_headlines": ["Free Delivery Over CA$280 on wellness orders"],
            "descriptions": ["Shop vitamins and supplements online today."],
            "business_name": "Nutricity CA",
        }
        group = {"search_themes": ["magnesium glycinate canada", "made in usa supplements"]}

        copy_assets = _pmax_asset_group_copy_assets(assets, account, group)

        self.assertTrue(any("Magnesium" in headline for headline in copy_assets["headlines"]))
        self.assertIn("Supplements Online", copy_assets["headlines"])
        self.assertFalse(any(_contains_shipping_offer_text(text) for text in copy_assets["headlines"]))
        self.assertFalse(any(_contains_shipping_offer_text(text) for text in copy_assets["long_headlines"]))
        self.assertGreaterEqual(copy_assets["shipping_offer_text_filtered"], 2)

    def test_pmax_group_copy_filters_internal_signal_language(self) -> None:
        account = SimpleNamespace(name="Nutricity CA")
        assets = {
            "headlines": ["Google conversion signal", "Supplements Online"],
            "long_headlines": ["Find wellness products from quality brands based on real demand signals"],
            "descriptions": ["Odoo sales signal", "Shop vitamins and supplements online today."],
            "business_name": "",
        }
        group = {"search_themes": ["vitamin c canada"]}

        copy_assets = _pmax_asset_group_copy_assets(assets, account, group)
        joined = " ".join(copy_assets["headlines"] + copy_assets["long_headlines"] + copy_assets["descriptions"]).lower()

        self.assertNotIn("conversion signal", joined)
        self.assertNotIn("odoo sales signal", joined)
        self.assertNotIn("demand signal", joined)
        self.assertEqual(copy_assets["business_name"], "Nutricity CA")

    def test_restricted_terms_parse_unapproved_substance_policy_text(self) -> None:
        terms = _restricted_terms_from_policy_strings(
            [
                "Unapproved substances",
                "Destination contains: Graviola Leaf, Soursop Graviola Liquid Drops, and DHEA 50 mg",
                "Contains: Zhong Gan Ling",
                "Certificate required in Canada",
                "Remove any references to unapproved substances that are prohibited from ad text.",
            ]
        )

        self.assertEqual(terms, ["Graviola Leaf", "Soursop Graviola Liquid Drops", "DHEA 50 mg", "Zhong Gan Ling"])

    def test_legacy_unsuffixed_pmax_draft_is_replaced_by_s001_source_key(self) -> None:
        legacy = SimpleNamespace(
            id=1,
            ad_type="pmax",
            status="published_enabled",
            generated_assets={
                "automation": {"source_key": "automation:637:nutricity.ca:pmax_scale_after_7d_conversions"}
            },
        )
        replacement = SimpleNamespace(
            id=2,
            ad_type="pmax",
            status="published_enabled",
            generated_assets={
                "automation": {"source_key": "automation:637:nutricity.ca:pmax_scale_after_7d_conversions_s001"}
            },
        )

        self.assertTrue(_is_replaced_legacy_pmax_draft(legacy, [legacy, replacement]))
        self.assertFalse(_is_replaced_legacy_pmax_draft(replacement, [legacy, replacement]))

    def test_rsa_publish_enables_existing_paused_exact_keywords(self) -> None:
        account = SimpleNamespace(id=637, name="Nutricity CA", customer_id="3495463031", currency_code="AUD")
        preference = SimpleNamespace(account=account, account_id=637, minimum_daily_budget_amount=1)
        draft = SimpleNamespace(
            id=21,
            ad_type="rsa",
            website_url="https://nutricity.ca",
            final_url="https://nutricity.ca",
            generated_assets={
                "campaign_identity": {
                    "campaign_code": "AUTO-RSA",
                    "campaign_name": "AUTO | Testing / Discovery | RSA Max Clicks Keywords | AUTO-RSA",
                },
                "bidding": {"daily_budget": 1, "max_cpc_bid_limit": 2.5},
                "final_url": "https://nutricity.ca",
                "keyword_clusters": [
                    {"exact_terms": ["klaire labs probiotics", "juven", "new exact keyword"]},
                ],
            },
        )

        with patch(
            "app.services.google_ads_live_campaign_creator.get_sync_setting_map",
            return_value={"automation.force_minimum_budget_when_budget_guard_blocked": False},
        ), patch(
            "app.services.google_ads_live_campaign_creator._campaign_by_code",
            return_value={
                "campaign_id": 1,
                "campaign_name": "AUTO | Testing / Discovery | RSA Max Clicks Keywords | AUTO-RSA",
                "campaign_resource_name": "customers/3495463031/campaigns/1",
                "campaign_status": "ENABLED",
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._enable_campaign_if_needed",
            return_value=None,
        ), patch(
            "app.services.google_ads_live_campaign_creator._apply_campaign_negative_keywords",
            return_value={
                "negative_keyword_count": 0,
                "existing_negative_keyword_count": 0,
                "new_negative_keyword_count": 0,
                "negative_keyword_resources": [],
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._apply_campaign_negative_webpages",
            return_value={
                "negative_page_count": 0,
                "existing_negative_page_count": 0,
                "new_negative_page_count": 0,
                "negative_page_resources": [],
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._ad_group_by_name",
            return_value={
                "ad_group_id": 2,
                "ad_group_name": "AUTO | Testing / Discovery | RSA Keywords | AUTO-RSA",
                "ad_group_resource_name": "customers/3495463031/adGroups/2",
                "ad_group_status": "ENABLED",
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._enable_ad_group_if_needed",
            return_value=None,
        ), patch(
            "app.services.google_ads_live_campaign_creator._rsa_ad_by_ad_group",
            return_value={
                "ad_group_ad_resource_name": "customers/3495463031/adGroupAds/2~3",
                "ad_status": "ENABLED",
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._enable_ad_group_ad_if_needed",
            return_value=None,
        ), patch(
            "app.services.google_ads_live_campaign_creator._existing_exact_keyword_map",
            return_value={
                "klaire labs probiotics": {"resource_name": "customers/3495463031/adGroupCriteria/2~10", "status": "PAUSED"},
                "juven": {"resource_name": "customers/3495463031/adGroupCriteria/2~11", "status": "ENABLED"},
                "old duplicate keyword": {"resource_name": "customers/3495463031/adGroupCriteria/2~13", "status": "ENABLED"},
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._enable_ad_group_criteria_if_needed",
            return_value=["customers/3495463031/adGroupCriteria/2~10"],
        ) as enable_keywords, patch(
            "app.services.google_ads_live_campaign_creator._pause_ad_group_criteria_if_needed",
            return_value=["customers/3495463031/adGroupCriteria/2~13"],
        ) as pause_keywords, patch(
            "app.services.google_ads_live_campaign_creator._create_exact_keywords",
            return_value=["customers/3495463031/adGroupCriteria/2~12"],
        ) as create_keywords:
            result = publish_rsa_draft(SimpleNamespace(), SimpleNamespace(), account, preference, draft)

        self.assertEqual(result["status"], "published_enabled")
        self.assertEqual(result["existing_keyword_count"], 3)
        self.assertEqual(result["enabled_existing_keyword_count"], 1)
        self.assertEqual(result["paused_stale_keyword_count"], 1)
        self.assertEqual(result["new_keyword_count"], 1)
        enable_keywords.assert_called_once_with(
            SimpleNamespace(),
            account,
            criterion_resource_names=["customers/3495463031/adGroupCriteria/2~10"],
            validate_only=False,
        )
        pause_keywords.assert_called_once_with(
            SimpleNamespace(),
            account,
            criterion_resource_names=["customers/3495463031/adGroupCriteria/2~13"],
            validate_only=False,
        )
        create_keywords.assert_called_once_with(
            SimpleNamespace(),
            account,
            ad_group_resource_name="customers/3495463031/adGroups/2",
            keywords=["new exact keyword"],
            validate_only=False,
        )

    def test_pmax_publish_creates_asset_group_assets_and_search_themes(self) -> None:
        account = SimpleNamespace(id=637, name="Nutricity CA", customer_id="3495463031", currency_code="AUD")
        preference = SimpleNamespace(account=account, account_id=637, minimum_daily_budget_amount=1)
        draft = SimpleNamespace(
            id=20,
            ad_type="pmax",
            website_url="https://nutricity.ca",
            final_url="https://nutricity.ca",
            generated_assets={
                "campaign_identity": {
                    "campaign_code": "AUTO-PMAX",
                    "campaign_name": "AUTO | Core / Scale | PMax Target ROAS Scale S001 | AUTO-PMAX",
                },
                "bidding": {"daily_budget": 1, "target_roas": 6.67},
                "final_url": "https://nutricity.ca",
                "headlines": ["Vitamins Canada", "Supplements Online", "Wellness Store"],
                "long_headlines": ["Shop vitamins and supplements online in Canada"],
                "descriptions": ["Shop vitamins, supplements and wellness products online today."],
                "business_name": "Nutricity CA",
                "negative_keywords": [{"keyword": "free vitamin samples", "match_type": "exact"}],
                "pmax_asset_groups": [
                    {"asset_group_code": "AG001", "search_themes": ["vitamin c canada", "magnesium glycinate"]},
                ],
            },
        )

        with patch(
            "app.services.google_ads_live_campaign_creator.pmax_activation_gate",
            return_value={"allowed": True, "threshold": 15, "conversions": 16, "reason": "allowed"},
        ), patch(
            "app.services.google_ads_live_campaign_creator.get_sync_setting_map",
            return_value={"automation.force_minimum_budget_when_budget_guard_blocked": False},
        ), patch("app.services.google_ads_live_campaign_creator._campaign_by_code", return_value=None), patch(
            "app.services.google_ads_live_campaign_creator._create_campaign_budget",
            return_value="customers/3495463031/campaignBudgets/1",
        ), patch(
            "app.services.google_ads_live_campaign_creator._create_performance_max_campaign",
            return_value="customers/3495463031/campaigns/2",
        ), patch(
            "app.services.google_ads_live_campaign_creator._existing_campaign_negative_exact_keywords",
            return_value=set(),
        ), patch(
            "app.services.google_ads_live_campaign_creator._create_campaign_negative_exact_keywords",
            return_value=["customers/3495463031/campaignCriteria/2~300"],
        ) as create_negatives, patch(
            "app.services.google_ads_live_campaign_creator._create_text_assets",
            return_value={
                "Vitamins Canada": "customers/3495463031/assets/10",
                "Supplements Online": "customers/3495463031/assets/11",
                "Wellness Store": "customers/3495463031/assets/12",
                "Shop vitamins and supplements online in Canada": "customers/3495463031/assets/13",
                "Shop vitamins, supplements and wellness products online today.": "customers/3495463031/assets/14",
                "Nutricity CA": "customers/3495463031/assets/15",
            },
        ), patch(
            "app.services.google_ads_live_campaign_creator._existing_pmax_media_assets",
            return_value={
                "MARKETING_IMAGE": ["customers/3495463031/assets/20"] * 3,
                "SQUARE_MARKETING_IMAGE": ["customers/3495463031/assets/30"] * 3,
                "PORTRAIT_MARKETING_IMAGE": ["customers/3495463031/assets/40"],
                "LOGO": ["customers/3495463031/assets/50"],
                "YOUTUBE_VIDEO": ["customers/3495463031/assets/60"],
            },
        ), patch("app.services.google_ads_live_campaign_creator._asset_group_by_name", return_value=None), patch(
            "app.services.google_ads_live_campaign_creator._create_asset_group_with_assets",
            return_value={
                "asset_group_resource_name": "customers/3495463031/assetGroups/3",
                "asset_link_resources": ["link1", "link2"],
            },
        ) as create_asset_group, patch(
            "app.services.google_ads_live_campaign_creator._link_asset_group_assets",
            return_value=["link1", "link2"],
        ) as link_assets, patch(
            "app.services.google_ads_live_campaign_creator._existing_campaign_search_theme_keys",
            return_value=set(),
        ), patch(
            "app.services.google_ads_live_campaign_creator._create_search_theme_signals",
            return_value=["theme1", "theme2"],
        ) as search_themes:
            result = publish_pmax_draft(SimpleNamespace(), SimpleNamespace(), account, preference, draft)

        self.assertEqual(result["status"], "published_enabled")
        self.assertEqual(result["asset_group_count"], 1)
        self.assertEqual(result["new_negative_keyword_count"], 1)
        create_negatives.assert_called_once()
        create_asset_group.assert_called_once()
        link_assets.assert_not_called()
        search_themes.assert_called_once()

    def test_pmax_publish_blocks_and_pauses_existing_campaign_below_search_conversion_gate(self) -> None:
        account = SimpleNamespace(id=637, name="Nutricity CA", customer_id="3495463031", currency_code="CAD")
        preference = SimpleNamespace(account=account, account_id=637, minimum_daily_budget_amount=1)
        draft = SimpleNamespace(
            id=21,
            ad_type="pmax",
            website_url="https://nutricity.ca",
            final_url="https://nutricity.ca",
            generated_assets={
                "campaign_identity": {
                    "campaign_code": "AUTO-PMAX",
                    "campaign_name": "AUTO | Core / Scale | PMax Target ROAS Scale S001 | AUTO-PMAX",
                },
                "final_url": "https://nutricity.ca",
            },
        )
        campaign = {
            "campaign_id": 2,
            "campaign_name": "AUTO | Core / Scale | PMax Target ROAS Scale S001 | AUTO-PMAX",
            "campaign_resource_name": "customers/3495463031/campaigns/2",
            "campaign_status": "ENABLED",
        }

        with patch(
            "app.services.google_ads_live_campaign_creator.pmax_activation_gate",
            return_value={"allowed": False, "threshold": 15, "conversions": 7, "reason": "PMax waits for Search conversions."},
        ), patch("app.services.google_ads_live_campaign_creator._campaign_by_code", return_value=campaign), patch(
            "app.services.google_ads_live_campaign_creator._pause_campaign_if_needed",
            return_value="customers/3495463031/campaigns/2",
        ) as pause_campaign:
            result = publish_pmax_draft(SimpleNamespace(), SimpleNamespace(), account, preference, draft)

        self.assertEqual(result["status"], "blocked_pmax_conversion_gate")
        self.assertEqual(result["pmax_gate"]["conversions"], 7)
        pause_campaign.assert_called_once()
        self.assertEqual(result["operations"][0]["operation"], "pause_pmax_below_search_conversion_gate")


if __name__ == "__main__":
    unittest.main()
