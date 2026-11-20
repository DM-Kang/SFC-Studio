"""Unit tests for Phase 1 UX and editorial workflow features."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.media import (
    build_subtitles_filter,
    normalize_subtitle_style,
    render_video,
    validate_and_normalize_srt,
)
from shortform_studio.planning import estimate_speaking_duration
from shortform_studio.store import Store
from shortform_studio.workflow import (
    delete_item_media,
    get_subtitles_state,
    save_subtitles_and_rerender,
)


class Phase1StoreTests(unittest.TestCase):
    """Verify item filtering, duplication, archiving, and guarded deletion."""

    def setUp(self) -> None:
        """@brief Create an isolated SQLite store per test."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = Store(self.root / "studio.sqlite3")

    def tearDown(self) -> None:
        """@brief Clean up temporary SQLite files."""
        self.temporary_directory.cleanup()

    def test_list_items_filters_by_query_stage_platform_and_archive(self) -> None:
        """@brief Filter workspace items cleanly without breaking full-list queries."""
        first = self.store.create_item({"topic": "Cold brew guide", "platforms": ["youtube"]})
        second = self.store.create_item({"topic": "Latte art tips", "platforms": ["instagram"]})
        self.store.update_payload(second["id"], {"plan": {"title": "Perfect Latte Foam"}})
        self.store.set_stage(second["id"], "planned")
        self.store.set_item_archived(first["id"], True)

        self.assertEqual([item["id"] for item in self.store.list_items(include_archived=False)], [second["id"]])
        self.assertEqual([item["id"] for item in self.store.list_items(query="foam")], [second["id"]])
        self.assertEqual([item["id"] for item in self.store.list_items(stage="planned")], [second["id"]])
        self.assertEqual([item["id"] for item in self.store.list_items(platform="youtube")], [first["id"]])
        self.assertEqual(len(self.store.list_items()), 2)

    def test_duplicate_item_clones_brief_and_plan_without_approval_or_media(self) -> None:
        """@brief Duplicate editorial inputs cleanly while resetting approval and publication state."""
        original = self.store.create_item({"topic": "Morning routine", "platforms": ["youtube", "instagram"], "duration_seconds": 20})
        self.store.update_payload(
            original["id"],
            {
                "plan_revision": 4,
                "plan": {"revision": 4, "title": "5AM Routine", "script": "Wake up early.", "generated_by": "openai"},
                "platform_variants": {"youtube": {"title": "YT Routine", "caption": "Desc", "hashtags": ["#shorts"]}},
                "assets": {"final_video": "/tmp/fake.mp4", "sha256": "deadbeef"},
                "review": {"status": "passed"},
            },
        )
        self.store.record_approval(original["id"], "2026-01-01T00:00:00+00:00", {"asset_sha256": "deadbeef", "plan_revision": 4})

        clone = self.store.duplicate_item(original["id"])

        self.assertNotEqual(clone["id"], original["id"])
        self.assertTrue(clone["topic"].endswith("(복사본)"))
        self.assertEqual(clone["stage"], "planned")
        self.assertEqual(clone["plan"]["revision"], 1)
        self.assertEqual(clone["plan"]["title"], "5AM Routine")
        self.assertEqual(clone["platform_variants"]["youtube"]["title"], "YT Routine")
        self.assertEqual(clone["assets"], {})
        self.assertIsNone(clone["review"])
        self.assertIsNone(clone["approved_at"])
        self.assertEqual(clone["approval_history"], [])

    def test_delete_item_blocks_active_and_published_items_but_removes_drafts(self) -> None:
        """@brief Protect audit trails on published or active items while allowing draft cleanup."""
        draft = self.store.create_item({"topic": "Scratch idea", "platforms": ["youtube"]})
        self.store.schedule_publication(draft["id"], "youtube", "2026-12-01T00:00:00+00:00", "UTC", "private")
        self.assertTrue(self.store.delete_item(draft["id"]))
        self.assertIsNone(self.store.get_item(draft["id"]))
        self.assertEqual(self.store.list_schedules(), [])
        self.assertFalse(self.store.delete_item(draft["id"]))

        published = self.store.create_item({"topic": "Published item", "platforms": ["youtube"]})
        self.store.reserve_publication(published["id"], "youtube")
        self.store.finish_publication(published["id"], "youtube", "published", remote_id="yt-123")
        with self.assertRaisesRegex(RuntimeError, "Published or uncertain"):
            self.store.delete_item(published["id"])

        busy = self.store.create_item({"topic": "Busy item", "platforms": ["youtube"]})
        self.store.claim_job(busy["id"], "planning")
        with self.assertRaisesRegex(RuntimeError, "active workflow"):
            self.store.delete_item(busy["id"])

    def test_delete_item_media_removes_only_item_subdirectory(self) -> None:
        """@brief Remove local media for a deleted item and reject malformed identifiers."""
        projects_dir = self.root / "projects"
        item_id = "11111111-2222-3333-4444-555555555555"
        media_dir = projects_dir / item_id
        media_dir.mkdir(parents=True)
        (media_dir / "final.mp4").write_bytes(b"video")

        with patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir):
            delete_item_media(item_id)
            self.assertFalse(media_dir.exists())
            with self.assertRaises(ValueError):
                delete_item_media("../escape")


class Phase1SubtitleAndPaceTests(unittest.TestCase):
    """Verify SRT validation, subtitle styling, rerendering, and pace estimation."""

    def test_subtitle_style_normalization_and_filter_generation(self) -> None:
        """@brief Enforce safe ASS style bounds and build FFmpeg subtitles filter strings."""
        style = normalize_subtitle_style({"preset": "yellow_pop", "font_size": 48, "margin_v": 200, "alignment": 2})
        self.assertEqual(style["primary_colour"], "&H0000E5FF")
        self.assertEqual(style["font_size"], 48)

        filter_expr = build_subtitles_filter("captions.srt", style)
        self.assertIn("subtitles=captions.srt:force_style=", filter_expr)
        self.assertIn("FontSize=48", filter_expr)
        self.assertIn("PrimaryColour=&H0000E5FF", filter_expr)

        with self.assertRaises(ValueError):
            normalize_subtitle_style({"preset": "unknown"})
        with self.assertRaises(ValueError):
            normalize_subtitle_style({"font_name": "Bad'Font;rm -rf"})
        with self.assertRaises(ValueError):
            normalize_subtitle_style({"font_size": 200})

    def test_validate_and_normalize_srt_renumbers_and_rejects_invalid_cues(self) -> None:
        """@brief Normalize valid SRT blocks and reject inverted or malformed timestamps."""
        normalized = validate_and_normalize_srt("9\n00:00:00,500 --> 00:00:02,000\n첫 번째 자막\n\n00:00:02,100 --> 00:00:04,000\n두 번째 자막")
        self.assertTrue(normalized.startswith("1\n00:00:00,500 --> 00:00:02,000\n첫 번째 자막"))
        self.assertIn("\n\n2\n00:00:02,100 --> 00:00:04,000\n두 번째 자막\n", normalized)

        with self.assertRaises(ValueError):
            validate_and_normalize_srt("")
        with self.assertRaisesRegex(ValueError, "greater than"):
            validate_and_normalize_srt("1\n00:00:03,000 --> 00:00:02,000\nReversed")
        with self.assertRaisesRegex(ValueError, "Invalid SRT timestamp"):
            validate_and_normalize_srt("1\nbad --> time\nText")

    def test_render_video_passes_custom_subtitle_style_to_ffmpeg(self) -> None:
        """@brief Include selected subtitle style options in the FFmpeg command line."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            output = root / "final.mp4"
            captions = root / "captions.srt"
            source.write_bytes(b"source")
            captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

            captured: list[list[str]] = []

            def fake_run(command, **kwargs):
                captured.append(command)
                (root / "final.rendering.mp4").write_bytes(b"rendered")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with (
                patch("shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("shortform_studio.media.subprocess.run", side_effect=fake_run),
            ):
                render_video(source, output, captions_path=captions, subtitle_style={"preset": "bold_center", "font_size": 50})

            self.assertEqual(len(captured), 1)
            vf_index = captured[0].index("-vf") + 1
            self.assertIn("FontSize=50", captured[0][vf_index])
            self.assertIn("Alignment=5", captured[0][vf_index])

    def test_save_subtitles_and_rerender_preserves_uploaded_source_and_resets_approval(self) -> None:
        """@brief Copy uploaded final.mp4 before burning subtitles and invalidate old approvals."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "studio.sqlite3")
            projects_dir = root / "projects"
            item = store.create_item({"topic": "Subtitles test", "platforms": ["youtube"]})
            item_dir = projects_dir / item["id"]
            item_dir.mkdir(parents=True)
            uploaded_mp4 = item_dir / "final.mp4"
            uploaded_mp4.write_bytes(b"original-upload")
            store.update_payload(
                item["id"],
                {
                    "plan_revision": 1,
                    "plan": {"revision": 1, "title": "Title"},
                    "assets": {"source_video": str(uploaded_mp4), "final_video": str(uploaded_mp4), "sha256": "old"},
                    "review": {"status": "passed"},
                },
            )
            store.record_approval(item["id"], "2026-01-01T00:00:00+00:00", {"asset_sha256": "old", "plan_revision": 1})

            def fake_render(source_path, output_path, **kwargs):
                self.assertNotEqual(source_path.resolve(), output_path.resolve())
                output_path.write_bytes(b"re-rendered-with-subtitles")
                return output_path

            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.workflow.render_video", side_effect=fake_render),
            ):
                updated = save_subtitles_and_rerender(
                    store,
                    item["id"],
                    "1\n00:00:00,000 --> 00:00:02,000\n새 자막\n",
                    {"preset": "yellow_pop", "font_size": 44},
                    rerender=True,
                )
                state = get_subtitles_state(store, item["id"])

            self.assertEqual(updated["stage"], "generated")
            self.assertIsNone(updated["approved_at"])
            self.assertIsNone(updated["review"])
            self.assertTrue((item_dir / "source_original.mp4").is_file())
            self.assertEqual(state["style"]["preset"], "yellow_pop")
            self.assertIn("새 자막", state["srt"])

    def test_estimate_speaking_duration_handles_korean_english_and_empty_text(self) -> None:
        """@brief Estimate speaking duration proportionally for supported languages."""
        ko = estimate_speaking_duration("안녕하세요 오늘은 테스트입니다.", "Korean")
        self.assertGreater(ko["estimated_seconds"], 1.5)
        self.assertEqual(ko["unit_label"], "chars")

        en = estimate_speaking_duration("One two three four five six seven eight nine ten eleven twelve", "English")
        self.assertEqual(en["estimated_seconds"], 5.0)
        self.assertEqual(en["unit_label"], "words")

        empty = estimate_speaking_duration("   ", "Korean")
        self.assertEqual(empty["estimated_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
