from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.google_ads_browser_automation import _task_classification, _task_payload, serialize_browser_task


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

    def test_serialize_browser_task_includes_claimed_batch_values(self) -> None:
        task = SimpleNamespace(
            id=101,
            account_id=7,
            draft_id=None,
            action_type="add_keyword",
            entity_type="keyword",
            campaign_name="AUTO | Core / Scale | RSA Target ROAS",
            ad_group_name="AUTO | Core / Scale | RSA Keywords",
            asset_group_name="",
            priority=40,
            step_order=9,
            status="claimed",
            payload_json={"keyword": "magnesium glycinate"},
            result_json={
                "claimed_batch": [
                    {"task_id": 101, "value": "magnesium glycinate"},
                    {"task_id": 102, "value": "vitamin c"},
                ]
            },
            claimed_by="chrome-worker-test",
            claimed_at=None,
            finished_at=None,
        )

        payload = serialize_browser_task(task)["payload"]

        self.assertEqual(payload["batch_values"], ["magnesium glycinate", "vitamin c"])
        self.assertEqual(payload["batch_task_ids"], [101, 102])
        self.assertEqual(payload["batch_size"], 2)


if __name__ == "__main__":
    unittest.main()
