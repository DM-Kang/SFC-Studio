"""Regression coverage for plan revisions and approval-safe content review."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.store import Store
from shortform_studio.workflow import replace_final_video_rights_record, revise_plan, run_content_review, run_planning, run_stock_material_search, run_web_research


class WorkflowTests(unittest.TestCase):
    """Exercise review transitions using local files and an isolated store."""

    def setUp(self) -> None:
        """@brief Create a temporary store and draft for each workflow test."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = Store(self.root / "studio.sqlite3")
        self.item = self.store.create_item({"topic": "A test topic", "platforms": ["youtube"]})

    def tearDown(self) -> None:
        """@brief Remove the isolated test database and media fixtures."""
        self.temporary_directory.cleanup()

    def test_editing_plan_advances_revision_and_invalidates_derived_state(self) -> None:
        """@brief Prevent an old video review or approval from matching edited text."""
        self.store.update_payload(
            self.item["id"],
            {
                "plan_revision": 1,
                "plan": {"revision": 1, "title": "Old", "generated_by": "local_template"},
                "review": {"status": "passed"},
                "approved_at": "2026-01-01T00:00:00+00:00",
                "approved_asset_sha256": "old-hash",
                "approval_record": {"asset_sha256": "old-hash"},
                "materials": [{"scene_id": "scene-1", "provider_id": "42"}],
                "materials_collected_at": "2026-01-01T00:00:00+00:00",
            },
        )

        updated = revise_plan(self.store, self.item["id"], {"title": "New title"})

        self.assertEqual(updated["plan_revision"], 2)
        self.assertEqual(updated["plan"]["revision"], 2)
        self.assertEqual(updated["plan"]["title"], "New title")
        self.assertIsNone(updated["review"])
        self.assertIsNone(updated["approved_at"])
        self.assertIsNone(updated["approved_asset_sha256"])
        self.assertIsNone(updated["approval_record"])
        self.assertEqual(updated["materials"], [])
        self.assertIsNone(updated["materials_collected_at"])

    def test_script_edit_replaces_scene_terms_from_the_old_narration(self) -> None:
        """@brief Keep stock searches aligned after an operator changes the script."""
        self.store.update_payload(
            self.item["id"],
            {"plan_revision": 1, "plan": {"revision": 1, "script": "Old narration", "scenes": [{"id": "scene-1", "visual_query": "old scene"}]}},
        )

        updated = revise_plan(self.store, self.item["id"], {"script": "New narration"})

        self.assertEqual(updated["plan"]["scenes"][0]["narration"], "New narration")
        self.assertEqual(updated["plan"]["scenes"][0]["visual_query"], "A test topic")

    def test_planning_uses_only_saved_research_sources(self) -> None:
        """@brief Attach the extracted research snapshot to the generated plan."""
        self.store.update_payload(
            self.item["id"],
            {
                "language": "English",
                "research": {
                    "sources": [
                        {"id": "S1", "url": "https://example.com/a", "content": "Extracted evidence", "status": "fetched"},
                        {"id": "S2", "url": "https://example.com/b", "content": "Search snippet", "status": "snippet_only"},
                    ]
                }
            },
        )
        with patch("shortform_studio.workflow.ai_ready", return_value=False):
            run_planning(self.store, self.item["id"])

        plan = self.store.get_item(self.item["id"])["plan"]
        self.assertEqual([source["id"] for source in plan["sources"]], ["S1"])

    def test_research_and_stock_search_persist_provider_results(self) -> None:
        """@brief Persist bounded provider results and return to the prior stage."""
        research_result = {"status": "complete", "sources": [{"id": "S1", "status": "fetched"}]}
        material_result = [{"scene_id": "scene-1", "provider_id": "42"}]
        self.store.update_payload(self.item["id"], {"plan": {"revision": 1, "scenes": [{"id": "scene-1", "visual_query": "coffee"}]}})
        with (
            patch("shortform_studio.workflow.get_secret", return_value="test-key"),
            patch("shortform_studio.workflow.collect_web_research", return_value=research_result),
            patch("shortform_studio.workflow.search_pexels_videos", return_value=material_result),
        ):
            self.assertEqual(run_web_research(self.store, self.item["id"], "draft"), "draft")
            self.assertEqual(run_stock_material_search(self.store, self.item["id"], "planned"), "planned")

        saved = self.store.get_item(self.item["id"])
        self.assertEqual(saved["research"], research_result)
        self.assertEqual(saved["materials"], material_result)
        self.assertIsNotNone(saved["materials_collected_at"])

    def test_failed_technical_check_cannot_be_downgraded_to_manual_required(self) -> None:
        """@brief Keep a definite media failure blocking even when AI review is absent."""
        video_path = self.root / "draft.mp4"
        video_path.write_bytes(b"local-fixture")
        self.store.update_payload(
            self.item["id"],
            {
                "plan_revision": 1,
                "plan": {"revision": 1},
                "assets": {"final_video": str(video_path), "generated_for_plan_revision": 1},
            },
        )
        failed_probe = {"available": True, "passed": False, "message": "Invalid dimensions."}
        with patch("shortform_studio.workflow.ai_ready", return_value=False), patch("shortform_studio.workflow.probe_video", return_value=failed_probe):
            run_content_review(self.store, self.item["id"])

        reviewed = self.store.get_item(self.item["id"])
        self.assertEqual(reviewed["review"]["status"], "needs_changes")
        self.assertEqual(reviewed["review"]["technical"]["message"], "Invalid dimensions.")

    def test_independent_review_receives_platform_copy_variants(self) -> None:
        """@brief Include the exact channel-specific copy in independent editorial review."""
        video_path = self.root / "draft.mp4"
        video_path.write_bytes(b"local-fixture")
        variants = {"youtube": {"title": "Shorts title", "caption": "Shorts caption", "hashtags": ["#shorts"]}}
        plan = {"revision": 1, "title": "Shared title", "script": "Narration", "claims": [], "sources": []}
        self.store.update_payload(self.item["id"], {"plan_revision": 1, "plan": plan, "platform_variants": variants, "assets": {"final_video": str(video_path), "generated_for_plan_revision": 1}})
        passed_review = {"status": "passed", "summary": "No issues.", "issues": [], "factual_claims": [], "rights_check": "not_verified", "safety_check": "not_verified", "reviewed_by": "test"}
        with patch("shortform_studio.workflow.ai_ready", return_value=True), patch("shortform_studio.workflow.probe_video", return_value={"available": True, "passed": True}), patch("shortform_studio.workflow.review_plan", return_value=passed_review) as review_call:
            run_content_review(self.store, self.item["id"])

        self.assertEqual(review_call.call_args.args[1]["platform_publishing_variants"], variants)

    def test_stale_video_is_rejected_for_review_after_plan_edit(self) -> None:
        """@brief Require media generated for the current plan revision."""
        video_path = self.root / "draft.mp4"
        video_path.write_bytes(b"old-plan-video")
        self.store.update_payload(
            self.item["id"],
            {
                "plan_revision": 1,
                "plan": {"revision": 2},
                "assets": {"final_video": str(video_path), "generated_for_plan_revision": 1},
            },
        )

        with self.assertRaisesRegex(RuntimeError, "current plan"):
            run_content_review(self.store, self.item["id"])

    def test_replacing_video_preserves_supporting_asset_records_but_resets_final_rights(self) -> None:
        """@brief Keep independent music and footage provenance when final MP4 changes."""
        supporting = {"id": "source-asset", "asset_type": "music", "asset_name": "Music", "rights_status": "cleared"}
        previous_video = {"id": "old-video", "asset_type": "final_video", "asset_name": "Previous MP4", "rights_status": "cleared"}

        updated = replace_final_video_rights_record([supporting, previous_video], "operator upload")

        self.assertEqual(updated[0], supporting)
        self.assertEqual(len(updated), 2)
        self.assertEqual(updated[1]["asset_type"], "final_video")
        self.assertEqual(updated[1]["rights_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
