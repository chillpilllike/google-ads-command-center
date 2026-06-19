from __future__ import annotations

import io
import unittest
import zipfile
from types import SimpleNamespace

from app.services.google_ads_manual_export import build_manual_automation_export, confirm_manual_automation_upload, manual_export_rows


class GoogleAdsManualExportTests(unittest.TestCase):
    def test_manual_exports_use_latest_automation_draft_payloads(self) -> None:
        account = SimpleNamespace(id=1, name="Nutricity CA", customer_id="3495463031")
        draft = SimpleNamespace(
            id=20,
            ad_type="rsa",
            status="ready_for_review",
            generated_assets={
                "automation": {"source_key": "automation:3495463031:scale_rsa"},
                "campaign_identity": {
                    "campaign_name": "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-123",
                    "campaign_code": "AUTO-123",
                    "category": "Core / Scale",
                },
                "keyword_clusters": [
                    {
                        "ad_group_name": "AUTO | Core / Scale | RSA Keywords | AUTO-123",
                        "exact_terms": ["Vitamin C Canada", "vitamin c canada", "Magnesium Glycinate"],
                    }
                ],
                "negative_keywords": [{"keyword": "free samples", "match_type": "exact", "reason": "waste"}],
                "url_inclusion_targets": [
                    {"url": "https://nutricity.ca/shop/vitamin-c?utm_source=x", "source_label": "scale", "conversions": 2},
                    {"url": "https://nutricity.ca/shop/vitamin-c", "source_label": "dupe"},
                ],
                "negative_page_targets": [{"url": "https://nutricity.ca/shop/no-conversion", "reason": "waste"}],
                "pmax_asset_groups": [
                    {"asset_group_code": "AG001", "search_themes": ["Vitamin D Canada"]},
                ],
            },
        )

        keywords = manual_export_rows(account, [draft], "keywords")
        negatives = manual_export_rows(account, [draft], "negative_keywords")
        inclusions = manual_export_rows(account, [draft], "url_inclusions")
        exclusions = manual_export_rows(account, [draft], "url_exclusions")

        self.assertEqual([row["Keyword"] for row in keywords], ["magnesium glycinate", "vitamin c canada", "vitamin d canada"])
        self.assertEqual(keywords[0]["Campaign"], "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-123")
        self.assertEqual(keywords[2]["Action"], "Add search theme")
        self.assertEqual(keywords[2]["Asset group"], "AUTO | Core / Scale | PMax Asset Group | AUTO-123 | AG001")
        self.assertEqual(negatives[0]["Keyword"], "free samples")
        self.assertEqual(negatives[0]["Google Ads Editor match type"], "Campaign Negative Exact")
        self.assertEqual(len(inclusions), 1)
        self.assertEqual(inclusions[0]["URL"], "https://nutricity.ca/shop/vitamin-c")
        self.assertEqual(exclusions[0]["URL"], "https://nutricity.ca/shop/no-conversion")
        self.assertEqual(keywords[0]["Manual target"], "RSA exact keyword")
        self.assertEqual(keywords[2]["Manual target"], "PMax search theme")
        self.assertEqual(inclusions[0]["Manual target"], "Dynamic/AI Max exact URL inclusion")

    def test_grouped_manual_export_creates_campaign_wise_zip(self) -> None:
        account = SimpleNamespace(id=1, name="Nutricity CA", customer_id="3495463031")
        draft_one = SimpleNamespace(
            id=20,
            ad_type="rsa",
            status="ready_for_review",
            generated_assets={
                "automation": {"source_key": "automation:3495463031:scale_rsa"},
                "campaign_identity": {
                    "campaign_name": "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-123",
                    "campaign_code": "AUTO-123",
                    "category": "Core / Scale",
                },
                "keyword_clusters": [{"ad_group_name": "Core Keywords", "exact_terms": ["Vitamin C Canada"]}],
            },
        )
        draft_two = SimpleNamespace(
            id=21,
            ad_type="dsa",
            status="ready_for_review",
            generated_assets={
                "automation": {"source_key": "automation:3495463031:testing_dsa"},
                "campaign_identity": {
                    "campaign_name": "AUTO | Testing / Discovery | Expanded DSA AI Max Target | AUTO-456",
                    "campaign_code": "AUTO-456",
                    "category": "Testing / Discovery",
                },
                "keyword_clusters": [{"ad_group_name": "Testing Keywords", "exact_terms": ["Magnesium Canada"]}],
            },
        )

        class FakeSession:
            class Scalars:
                def all(self):
                    return [draft_two, draft_one]

            def scalars(self, _statement):
                return self.Scalars()

        export_file = build_manual_automation_export(FakeSession(), account, kind="keywords", grouped=True)

        self.assertTrue(export_file.filename.endswith(".zip"))
        self.assertEqual(export_file.content_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(export_file.content), "r") as archive:
            names = archive.namelist()
            self.assertIn("00-manifest.csv", names)
            self.assertTrue(any(name.endswith("/keywords.csv") and "core-scale-rsa" in name for name in names))
            self.assertTrue(any(name.endswith("/keywords.csv") and "testing-discovery-dsa" in name for name in names))

    def test_confirm_manual_upload_marks_drafts_as_published_enabled(self) -> None:
        account = SimpleNamespace(id=1, name="Nutricity CA", customer_id="3495463031")
        draft = SimpleNamespace(
            id=20,
            ad_type="rsa",
            status="ready_for_review",
            generated_assets={
                "automation": {"source_key": "automation:3495463031:scale_rsa"},
                "campaign_identity": {
                    "campaign_name": "AUTO | Core / Scale | RSA Target ROAS Scale Keywords | AUTO-123",
                    "campaign_code": "AUTO-123",
                    "category": "Core / Scale",
                },
            },
        )

        class FakeSession:
            def __init__(self, row):
                self.row = row
                self.added = []
                self.flushed = False

            class Scalars:
                def __init__(self, row):
                    self.row = row

                def all(self):
                    return [self.row]

            def scalars(self, _statement):
                return self.Scalars(self.row)

            def add(self, row):
                self.added.append(row)

            def flush(self):
                self.flushed = True

        session = FakeSession(draft)
        result = confirm_manual_automation_upload(session, account, user_id=7, note="uploaded in editor")

        self.assertEqual(result["draft_count"], 1)
        self.assertEqual(draft.status, "published_enabled")
        self.assertEqual(draft.generated_assets["manual_upload"]["confirmed_by_user_id"], 7)
        self.assertEqual(draft.generated_assets["live_publish"]["status"], "manual_uploaded_confirmed")
        self.assertTrue(session.flushed)


if __name__ == "__main__":
    unittest.main()
