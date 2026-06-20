from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.google_ads_live_campaign_creator import (
    _manual_csv_deferred_keyword_result,
    _manual_csv_deferred_url_inclusion_result,
    _manual_first_run_criteria_csv_pending,
)


class GoogleAdsManualFirstRunCsvTests(unittest.TestCase):
    def test_first_run_criteria_csv_waits_until_manual_confirmation(self) -> None:
        preference = SimpleNamespace(manual_first_run_criteria_csv_enabled=True)
        draft = SimpleNamespace(
            generated_assets={
                "keyword_clusters": [{"exact_terms": ["Vitamin C Canada", "vitamin c canada"]}],
                "pmax_asset_groups": [{"search_themes": ["Magnesium Canada"]}],
                "url_inclusion_targets": [
                    {"url": "https://nutricity.ca/shop/vitamin-c?utm_source=ads"},
                    {"url": "https://nutricity.ca/shop/vitamin-c"},
                ],
            }
        )

        self.assertTrue(_manual_first_run_criteria_csv_pending(preference, draft))
        self.assertEqual(_manual_csv_deferred_keyword_result(draft)["keyword_count"], 1)
        self.assertEqual(_manual_csv_deferred_url_inclusion_result(draft)["url_inclusion_count"], 1)

        draft.generated_assets["manual_upload"] = {"status": "confirmed"}
        self.assertFalse(_manual_first_run_criteria_csv_pending(preference, draft))

    def test_first_run_criteria_csv_off_uses_api_path(self) -> None:
        preference = SimpleNamespace(manual_first_run_criteria_csv_enabled=False)
        draft = SimpleNamespace(generated_assets={})

        self.assertFalse(_manual_first_run_criteria_csv_pending(preference, draft))


if __name__ == "__main__":
    unittest.main()
