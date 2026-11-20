"""Offline tests for template planning, speech captions, and media boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.media import alignment_to_srt, probe_video, render_video
from shortform_studio.planning import generate_plan, make_template_plan


class ContentTests(unittest.TestCase):
    """Verify offline functionality without contacting paid provider APIs."""

    def test_template_plan_is_explicitly_not_an_ai_result(self) -> None:
        """@brief Identify the local fallback so it cannot be mistaken for a model call."""
        plan = generate_plan({"topic": "회의 준비", "language": "Korean"}, use_ai=False)
        self.assertEqual(plan["generated_by"], "local_template")
        self.assertIn("회의 준비", plan["title"])

    def test_template_plan_has_default_short_duration(self) -> None:
        """@brief Keep the template result within the supported vertical-video range."""
        plan = make_template_plan({"topic": "Coffee", "language": "English"})
        self.assertGreaterEqual(plan["duration_seconds"], 4)
        self.assertLessEqual(plan["duration_seconds"], 30)

    def test_local_template_preview_applies_series_call_to_action(self) -> None:
        """@brief Preserve a template call to action in offline planning previews."""
        plan = make_template_plan({"topic": "Coffee", "language": "English", "series_template": {"call_to_action": "Follow for more."}})

        self.assertTrue(plan["caption"].endswith("Follow for more."))

    def test_alignment_becomes_subtitle_cues_with_timing(self) -> None:
        """@brief Preserve speech timestamps while generating numbered SRT cues."""
        alignment = {
            "characters": list("Hello."),
            "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4, 0.5],
            "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.7],
        }
        subtitles = alignment_to_srt(alignment)
        self.assertIn("00:00:00,000 --> 00:00:00,700", subtitles)
        self.assertIn("Hello.", subtitles)

    def test_alignment_rejects_mismatched_timing_arrays(self) -> None:
        """@brief Reject incomplete provider timestamps instead of misaligning captions."""
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            alignment_to_srt({"characters": ["a"], "character_start_times_seconds": [], "character_end_times_seconds": []})

    def test_ffmpeg_optional_copy_path_is_safe_and_explicit(self) -> None:
        """@brief Allow unrendered media import while clearly separating FFmpeg checks."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            output = Path(temporary) / "final.mp4"
            source.write_bytes(b"test-media")
            with patch("shortform_studio.media.shutil.which", return_value=None):
                render_video(source, output)
            self.assertEqual(output.read_bytes(), b"test-media")

    def test_voiceover_render_requires_ffmpeg(self) -> None:
        """@brief Refuse to claim narration was combined when FFmpeg is unavailable."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            output = Path(temporary) / "final.mp4"
            audio = Path(temporary) / "voice.mp3"
            source.touch()
            audio.touch()
            with patch("shortform_studio.media.shutil.which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "FFmpeg is required"):
                    render_video(source, output, audio_path=audio)

    def test_ffprobe_absence_is_reported_as_unverified(self) -> None:
        """@brief Do not convert a missing media utility into a passing QA result."""
        with tempfile.TemporaryDirectory() as temporary:
            with patch("shortform_studio.media.shutil.which", return_value=None):
                result = probe_video(Path(temporary) / "draft.mp4")
            self.assertFalse(result["available"])
            self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
