from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.google_ads_lane_conflict_resolver import (
    _negative_intent_terms,
    _negative_intent_urls,
    _prune_stale_resolver_negative_intent,
)


class GoogleAdsLaneConflictResolverTests(unittest.TestCase):
    def test_resolver_generated_negatives_do_not_become_negative_intent(self) -> None:
        draft = SimpleNamespace(
            generated_assets={
                "negative_keywords": [
                    {
                        "keyword": "core owned product keyword",
                        "match_type": "exact",
                        "source": "lane_conflict_resolver",
                        "owner": "AUTO | Core / Scale",
                    },
                    {"keyword": "real garbage keyword", "match_type": "exact", "source": "policy_disapproval_sync"},
                ],
                "negative_page_targets": [
                    "https://example.com/products/core-owned",
                    "https://example.com/products/bad-policy",
                ],
                "lane_conflict_url_exclusions": [
                    {
                        "url": "https://example.com/products/core-owned",
                        "source": "lane_conflict_resolver",
                        "owner": "negative_intent_bank",
                    }
                ],
            }
        )

        self.assertEqual(_negative_intent_terms(draft), {"real garbage keyword"})
        self.assertEqual(_negative_intent_urls(draft), {"https://example.com/products/bad-policy"})

    def test_prune_stale_resolver_negative_intent_keeps_real_bank_negatives(self) -> None:
        assets = {
            "negative_keywords": [
                {
                    "keyword": "winner keyword",
                    "match_type": "exact",
                    "source": "lane_conflict_resolver",
                    "owner": "negative_intent_bank",
                },
                {
                    "keyword": "real bad keyword",
                    "match_type": "exact",
                    "source": "lane_conflict_resolver",
                    "owner": "negative_intent_bank",
                },
            ],
            "negative_page_targets": [
                "https://example.com/products/winner",
                "https://example.com/products/bad",
            ],
            "lane_conflict_url_exclusions": [
                {
                    "url": "https://example.com/products/winner",
                    "source": "lane_conflict_resolver",
                    "owner": "negative_intent_bank",
                },
                {
                    "url": "https://example.com/products/bad",
                    "source": "lane_conflict_resolver",
                    "owner": "negative_intent_bank",
                },
            ],
        }

        changed = _prune_stale_resolver_negative_intent(
            assets,
            {"real bad keyword"},
            {"https://example.com/products/bad"},
        )

        self.assertTrue(changed)
        self.assertEqual([item["keyword"] for item in assets["negative_keywords"]], ["real bad keyword"])
        self.assertEqual(assets["negative_page_targets"], ["https://example.com/products/bad"])
        self.assertEqual([item["url"] for item in assets["lane_conflict_url_exclusions"]], ["https://example.com/products/bad"])


if __name__ == "__main__":
    unittest.main()
