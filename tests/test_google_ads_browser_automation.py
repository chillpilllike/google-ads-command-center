from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.google_ads_browser_automation import _task_classification, _task_payload


class GoogleAdsBrowserAutomationTests(unittest.TestCase):
    def test_classifies_campaign_keyword_and_url_inclusion_rows(self) -> None:
        self.assertEqual(
            _task_classification({"Campaign Type": "Search", "Campaign": "AUTO | Core / Scale | RSA"})["action_type"],
            "upsert_campaign",
        )
        self.assertEqual(
            _task_classification({"Keyword": "vitamin c", "Criterion Type": "Exact"})["action_type"],
            "add_keyword",
        )
        self.assertEqual(
            _task_classification({"URL rule value 1": "https://nutricity.ca/shop/vitamin-c", "Ad Group": "DSA"})["action_type"],
            "add_url_inclusion",
        )
        self.assertEqual(
            _task_classification({"Keyword": "free samples", "Criterion Type": "Campaign Negative Exact"})["action_type"],
            "add_negative_keyword",
        )

    def test_payload_preserves_campaign_context_for_extension(self) -> None:
        account = SimpleNamespace(
            id=7,
            name="Nutricity CA",
            customer_id="3495463031",
            manager_customer_id="123",
            currency_code="CAD",
        )
        row = {
            "Campaign": "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-123",
            "Ad Group": "AUTO | Core / Scale | RSA Keywords | AUTO-123",
            "Keyword": "magnesium glycinate",
            "Criterion Type": "Exact",
        }
        classification = _task_classification(row)
        payload = _task_payload(account, row, classification)

        self.assertEqual(payload["account"]["customer_id"], "3495463031")
        self.assertEqual(payload["campaign"], row["Campaign"])
        self.assertEqual(payload["ad_group"], row["Ad Group"])
        self.assertEqual(payload["keyword"], "magnesium glycinate")
        self.assertIn("ads.google.com/aw/keywords", payload["target_url"])


if __name__ == "__main__":
    unittest.main()
