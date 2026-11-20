"""Regression coverage for plan revisions and approval-safe content review."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.store import Store
from shortform_studio.workflow import (
    _auto_select_scene_clips,
    confirm_item_rights_for_approval,
    record_rejection_feedback,
    replace_final_video_rights_record,
    revise_plan,
    run_autopilot_pipeline,
    run_content_review,
    run_planning,
    run_stock_material_search,
    run_web_research,
)


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
            patch("shortform_studio.workflow.search_pixabay_videos", return_value=[]),
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

    def test_autopilot_pipeline_and_rejection_feedback_loop(self) -> None:
        """@brief Run full autopilot from topic through review, reject with feedback, and confirm rights on approval."""
        scenes = [
            {"id": "scene-1", "narration": "Hook line", "visual_query": "wifi cafe"},
            {"id": "scene-2", "narration": "Tip two", "visual_query": "smartphone settings"},
        ]
        materials = [
            {"scene_id": "scene-1", "provider": "Pixabay", "provider_id": "pixabay-1", "download_url": "https://cdn.pixabay.com/1.mp4", "title": "Cafe", "author": "A", "source_url": "https://pixabay.com/videos/1/", "license_url": "https://pixabay.com/service/license-summary/"},
            {"scene_id": "scene-2", "provider": "Pixabay", "provider_id": "pixabay-2", "download_url": "https://cdn.pixabay.com/2.mp4", "title": "Phone", "author": "B", "source_url": "https://pixabay.com/videos/2/", "license_url": "https://pixabay.com/service/license-summary/"},
        ]
        clips = _auto_select_scene_clips(scenes, materials, 20)
        self.assertEqual([c["provider_id"] for c in clips], ["pixabay-1", "pixabay-2"])
        self.assertEqual(clips[0]["source_kind"], "stock")

        fake_plan = {
            "title": "Wi-Fi Security",
            "hook": "Stop connecting blindly!",
            "script": "Hook line. Tip two.",
            "caption": "Keep safe on public Wi-Fi",
            "hashtags": ["#security"],
            "video_prompt": "Vertical cafe smartphone",
            "duration_seconds": 20,
            "claims": [],
            "scenes": scenes,
            "sources": [],
            "generated_by": "gemini_api:gemini-3-flash-preview",
        }
        item = self.store.create_item({"topic": "Public Wi-Fi tips", "platforms": ["youtube"], "workflow_mode": "autopilot"})

        def fake_assemble(store_arg, item_id_arg, scene_inputs, **kwargs):
            target = self.root / "projects" / item_id_arg / "final.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            current = store_arg.get_item(item_id_arg)
            store_arg.update_payload(
                item_id_arg,
                {
                    "assets": {"final_video": str(target), "sha256": "fakehash1", "generated_for_plan_revision": current["plan"]["revision"]},
                    "rights_assets": [{"id": "r1", "asset_name": "Pixabay Cafe", "asset_type": "video", "included": True, "rights_status": "unverified", "license_name": "Pixabay License", "rights_basis": ""}],
                },
            )
            return "generated"

        with (
            patch("shortform_studio.workflow.get_projects_dir", return_value=self.root / "projects"),
            patch("shortform_studio.workflow.get_secret", side_effect=lambda k, d="": "pix-key" if k == "PIXABAY_API_KEY" else ""),
            patch("shortform_studio.workflow.ai_ready", side_effect=lambda s: s == "planning"),
            patch("shortform_studio.workflow.generate_plan", return_value=dict(fake_plan)),
            patch("shortform_studio.workflow.search_pixabay_videos", return_value=materials),
            patch("shortform_studio.workflow.run_scene_assembly", side_effect=fake_assemble),
            patch("shortform_studio.workflow.probe_video", return_value={"available": True, "passed": True, "width": 720, "height": 1280, "duration_seconds": 20.0}),
        ):
            stage = run_autopilot_pipeline(self.store, item["id"])
        self.assertEqual(stage, "reviewed")
        reviewed_item = self.store.get_item(item["id"])
        self.assertEqual(reviewed_item["plan"]["title"], "Wi-Fi Security")
        self.assertIsNotNone(reviewed_item["review"])

        # Reject with feedback and verify rejection history + revision increment.
        rejected = record_rejection_feedback(self.store, item["id"], "Make the hook punchier and shorter")
        self.assertEqual(len(rejected["rejection_history"]), 1)
        self.assertEqual(rejected["rejection_history"][0]["feedback"], "Make the hook punchier and shorter")
        revised_plan = {**fake_plan, "hook": "Never trust airport Wi-Fi!"}
        with (
            patch("shortform_studio.workflow.get_projects_dir", return_value=self.root / "projects"),
            patch("shortform_studio.workflow.get_secret", side_effect=lambda k, d="": "pix-key" if k == "PIXABAY_API_KEY" else ""),
            patch("shortform_studio.workflow.ai_ready", side_effect=lambda s: s == "planning"),
            patch("shortform_studio.workflow.revise_plan_with_feedback", return_value=revised_plan),
            patch("shortform_studio.workflow.search_pixabay_videos", return_value=materials),
            patch("shortform_studio.workflow.run_scene_assembly", side_effect=fake_assemble),
            patch("shortform_studio.workflow.probe_video", return_value={"available": True, "passed": True, "width": 720, "height": 1280, "duration_seconds": 20.0}),
        ):
            stage_v2 = run_autopilot_pipeline(self.store, item["id"], feedback="Make the hook punchier and shorter")
        self.assertEqual(stage_v2, "reviewed")
        v2_item = self.store.get_item(item["id"])
        self.assertEqual(v2_item["plan"]["revision"], 2)
        self.assertEqual(v2_item["plan"]["hook"], "Never trust airport Wi-Fi!")

        confirmed = confirm_item_rights_for_approval(v2_item)
        self.assertEqual(confirmed[0]["rights_status"], "cleared")
        self.assertTrue(confirmed[0]["rights_basis"])

    def test_ollama_plan_wrapper_unwrapping_and_num_ctx_budget(self) -> None:
        """@brief Unwrap nested local-LLM plan wrappers and send adequate num_ctx for multi-source prompts."""
        from shortform_studio.ai_providers import _generate_ollama
        from shortform_studio.planning import _build_plan_payload

        wrapped = {
            "plan": {
                "title": "Wrapped Title",
                "hook": "Hook!",
                "script": "Short script.",
                "caption": "Caption.",
                "hashtags": ["#coffee"],
                "duration_seconds": "25",
                "claims": [],
                "scenes": [{"narration": "Line 1", "visual_query": "cold brew coffee"}],
            }
        }
        payload = _build_plan_payload(wrapped, "ollama:qwen2.5:7b", {"topic": "Coffee", "duration_seconds": 30}, [])
        self.assertEqual(payload["title"], "Wrapped Title")
        self.assertEqual(payload["duration_seconds"], 25)

        with (
            patch("shortform_studio.ai_providers.load_ai_settings", return_value={"ollama_base_url": "http://127.0.0.1:11434"}),
            patch("shortform_studio.ai_providers._request_json", return_value={"message": {"content": "{\"ok\": true}"}}) as req,
        ):
            res = _generate_ollama("qwen2.5:7b", "sys", "usr")
        self.assertEqual(res, '{"ok": true}')
        self.assertGreaterEqual(req.call_args.kwargs["body"]["options"]["num_ctx"], 16384)


if __name__ == "__main__":
    unittest.main()
