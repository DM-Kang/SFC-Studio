"""Regression coverage for brand context, publishing preflight, and analytics."""

import unittest
from datetime import datetime, timezone

from shortform_studio.analytics import summarize_analytics
from shortform_studio.brand import find_series_template, normalize_brand_settings
from shortform_studio.preflight import build_publish_preflight, rights_assets_fingerprint


class BrandSettingsTests(unittest.TestCase):
    """Check stable template identities and bounded profile normalization."""

    def test_template_round_trip_keeps_stable_identity(self) -> None:
        """@brief Keep a reusable template ID stable when settings are normalized."""
        settings = normalize_brand_settings({
            "profile": {"brand_name": "Studio", "preferred_terms": ["Kona"]},
            "series_templates": [{"id": "12345678-1234-1234-1234-123456789abc", "name": "Weekly", "platforms": ["youtube"]}],
        })

        selected = find_series_template(settings, "12345678-1234-1234-1234-123456789abc")

        self.assertEqual(selected["name"], "Weekly")
        self.assertEqual(selected["id"], settings["series_templates"][0]["id"])

    def test_duplicate_brand_terms_are_rejected_case_insensitively(self) -> None:
        """@brief Reject duplicate guidance that makes review behavior ambiguous."""
        with self.assertRaisesRegex(ValueError, "unique"):
            normalize_brand_settings({"profile": {"prohibited_terms": ["Unsafe", "unsafe"]}})

    def test_noncanonical_template_ids_are_rejected(self) -> None:
        """@brief Require template IDs to be canonical UUIDs rather than arbitrary labels."""
        with self.assertRaisesRegex(ValueError, "UUID-shaped"):
            normalize_brand_settings({"profile": {}, "series_templates": [{"id": "not-a-uuid", "name": "Weekly"}]})


class PublishingPreflightTests(unittest.TestCase):
    """Verify rights, platform boundaries, and canonical approval fingerprints."""

    def test_missing_rights_and_utf8_description_overflow_block_publication(self) -> None:
        """@brief Block unverified media rights and YouTube byte-limit overflow."""
        plan = {"title": "A title", "caption": "가" * 1700}
        technical = {"available": True, "passed": True}
        rights = [{"asset_name": "Final video", "included": True, "rights_status": "unverified"}]

        result = build_publish_preflight(plan, ["youtube"], technical, rights, {})

        self.assertEqual(result["status"], "blocked")
        self.assertIn("asset_rights", {check["code"] for check in result["checks"]})
        self.assertIn("description_limit", {check["code"] for check in result["checks"]})

    def test_required_disclosure_must_be_in_script_or_caption(self) -> None:
        """@brief Prevent an internal prompt from satisfying a public disclosure."""
        plan = {"title": "A title", "script": "Narration", "caption": "Caption", "video_prompt": "Include sponsorship disclosure"}
        result = build_publish_preflight(plan, ["instagram"], {"available": False}, [], {"required_disclosures": ["sponsored"]})

        disclosure_check = next(check for check in result["checks"] if check["code"] == "required_disclosure")
        self.assertEqual(disclosure_check["status"], "blocked")

    def test_rights_fingerprint_is_order_independent_but_content_sensitive(self) -> None:
        """@brief Bind approval to canonical rights data regardless of object key order."""
        first = [{"id": "one", "rights_status": "cleared", "rights_basis": "owned"}]
        reordered = [{"rights_basis": "owned", "rights_status": "cleared", "id": "one"}]
        changed = [{"id": "one", "rights_status": "cleared", "rights_basis": "licensed"}]

        self.assertEqual(rights_assets_fingerprint(first), rights_assets_fingerprint(reordered))
        self.assertNotEqual(rights_assets_fingerprint(first), rights_assets_fingerprint(changed))


class AnalyticsSummaryTests(unittest.TestCase):
    """Aggregate the latest valid manual snapshot without inventing engagement."""

    def test_summary_uses_latest_published_snapshot_and_medians(self) -> None:
        """@brief Compute view velocity, engagement, and watch-time medians."""
        now = datetime(2026, 1, 5, tzinfo=timezone.utc)
        item = {
            "topic": "Coffee tips",
            "publications": {"youtube": {"status": "published", "updated_at": "2026-01-01T00:00:00+00:00"}},
            "analytics": {"youtube": [
                {"measured_at": "2026-01-02T00:00:00+00:00", "views": 10, "likes": 1},
                {"measured_at": "2026-01-03T00:00:00+00:00", "views": 1000, "likes": 80, "comments": 10, "shares": 5, "saves": 5, "watch_time_seconds": 3600},
            ]},
        }
        result = summarize_analytics([item], now=now)
        youtube = result["platforms"]["youtube"]

        self.assertEqual(youtube["sample_count"], 1)
        self.assertEqual(youtube["median_views_per_day"], 500.0)
        self.assertEqual(youtube["median_engagement_rate"], 0.1)
        self.assertEqual(youtube["median_average_watch_seconds_per_view"], 3.6)

    def test_views_without_interaction_counts_do_not_imply_zero_rate(self) -> None:
        """@brief Leave engagement unknown when no interaction metrics were entered."""
        item = {
            "topic": "Topic",
            "publications": {"instagram": {"status": "published", "updated_at": "2026-01-01T00:00:00+00:00"}},
            "analytics": {"instagram": [{"measured_at": "2026-01-02T00:00:00+00:00", "views": 500}]},
        }

        result = summarize_analytics([item], now=datetime(2026, 1, 3, tzinfo=timezone.utc))

        self.assertIsNone(result["platforms"]["instagram"]["median_engagement_rate"])


if __name__ == "__main__":
    unittest.main()
