"""Tests for Hangul ElevenLabs subtitle preservation, English stock query cleanup, and YouTube Shorts learning."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.brand import normalize_brand_settings
from shortform_studio.media import generate_voiceover
from shortform_studio.planning import _build_plan_payload
from shortform_studio.shorts_learner import (
    analyze_shorts_reference,
    parse_youtube_video_id,
    reference_to_series_template,
    summarize_shorts_references_for_planning,
)


class _FakeUrlResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, *_args: object) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeUrlResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class ShortsLearnerAndQualityTests(unittest.TestCase):
    """Verify Korean caption alignment selection, CJK query cleanup, and YouTube Shorts benchmarking."""

    def test_elevenlabs_voiceover_uses_hangul_alignment_instead_of_romanized_normalized_alignment(self) -> None:
        """@brief Keep Hangul captions instead of ElevenLabs romanized normalized_alignment."""
        fake_response = {
            "audio_base64": base64.b64encode(b"ID3fakemp3").decode("ascii"),
            "normalized_alignment": {
                "characters": list("annyeong"),
                "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
                "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            },
            "alignment": {
                "characters": list("안녕하세요"),
                "character_start_times_seconds": [0.0, 0.15, 0.3, 0.45, 0.6],
                "character_end_times_seconds": [0.15, 0.3, 0.45, 0.6, 0.8],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": {"ELEVENLABS_API_KEY": "xi", "ELEVENLABS_VOICE_ID": "v1"}.get(k, d)),
                patch("urllib.request.urlopen", return_value=_FakeUrlResponse(json.dumps(fake_response).encode("utf-8"))),
            ):
                vo = generate_voiceover("안녕하세요. 콜드브루 팁입니다.", Path(tmp), tts_provider="elevenlabs")
            srt = Path(vo["captions_path"]).read_text(encoding="utf-8")
        self.assertIn("안녕하세요", srt)
        self.assertNotIn("annyeong", srt)

    def test_elevenlabs_falls_back_to_source_hangul_if_alignment_contains_only_latin(self) -> None:
        """@brief Fall back to spoken Korean text timings if ElevenLabs omits Hangul in alignment."""
        fake_response = {
            "audio_base64": base64.b64encode(b"ID3fakemp3").decode("ascii"),
            "normalized_alignment": {
                "characters": list("hello"),
                "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3, 0.4],
                "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": {"ELEVENLABS_API_KEY": "xi", "ELEVENLABS_VOICE_ID": "v1"}.get(k, d)),
                patch("urllib.request.urlopen", return_value=_FakeUrlResponse(json.dumps(fake_response).encode("utf-8"))),
                patch("shortform_studio.media.probe_audio_duration", return_value=4.0),
            ):
                vo = generate_voiceover("집에서 실패 없는 콜드브루 황금 비율", Path(tmp), tts_provider="elevenlabs")
            srt = Path(vo["captions_path"]).read_text(encoding="utf-8")
        self.assertIn("콜드브루", srt)
        self.assertNotIn("hello", srt)

    def test_planner_cleans_chinese_visual_queries_and_hanzi_hashtags_for_korean_brief(self) -> None:
        """@brief Replace Chinese scene visual_query strings with English fallback and drop Hanzi tags."""
        raw = {
            "title": "콜드브루 1:8 황금비율",
            "hook": "카페보다 맛있는 콜드브루, 집에서 5분 만에 준비됩니다.",
            "script": "원두 50g에 찬물 450ml만 기억하세요.",
            "caption": "집에서 만드는 콜드브루",
            "hashtags": ["#콜드브루", "#홈카페", "#冷水萃取咖啡"],
            "video_prompt": "pouring cold brew coffee glass ice vertical closeup",
            "duration_seconds": 20,
            "claims": [],
            "scenes": [
                {"narration": "원두를 굵게 갈아주세요", "visual_query": "准备研磨的咖啡豆"},
                {"narration": "찬물을 부어주세요", "visual_query": "pouring water cold brew glass"},
            ],
        }
        payload = _build_plan_payload(raw, "ollama:qwen2.5:7b", {"topic": "콜드브루", "language": "Korean", "duration_seconds": 20}, [])
        self.assertEqual(payload["hashtags"], ["#콜드브루", "#홈카페"])
        self.assertTrue(payload["scenes"][0]["visual_query"].isascii())
        self.assertIn("coffee", payload["scenes"][0]["visual_query"])
        self.assertEqual(payload["scenes"][1]["visual_query"], "pouring water cold brew glass")

    def test_youtube_shorts_parsing_extraction_and_template_conversion(self) -> None:
        """@brief Parse Shorts URLs, extract caption pacing metrics, and convert to valid series templates."""
        self.assertEqual(parse_youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(parse_youtube_video_id("https://youtu.be/dQw4w9WgXcQ?si=abc"), "dQw4w9WgXcQ")
        with self.assertRaises(ValueError):
            parse_youtube_video_id("http://127.0.0.1:8080/shorts/dQw4w9WgXcQ")

        player_json = json.dumps(
            {
                "videoDetails": {
                    "title": "집에서 카페급 콜드브루 15초 정리",
                    "author": "홈카페연구소",
                    "lengthSeconds": "22",
                    "shortDescription": "1:9 비율만 기억하세요",
                    "keywords": ["콜드브루", "홈카페"],
                },
                "captions": {
                    "playerCaptionsTracklistRenderer": {
                        "captionTracks": [
                            {"baseUrl": "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ", "languageCode": "ko", "kind": "asr"}
                        ]
                    }
                },
            }
        )
        fake_html = f"<html><script>var ytInitialPlayerResponse = {player_json};</script></html>"
        fake_xml = '<transcript><text start="0.0" dur="2.1">카페에서 6천원 내는 콜드브루, 집에선 500원이면 충분해요?</text><text start="2.2" dur="3.0">원두 50g에 물 450ml만 부어 14시간 재우세요.</text></transcript>'

        def fake_get(url: str, **_kwargs: object) -> str:
            if "oembed" in url:
                return json.dumps({"title": "집에서 카페급 콜드브루 15초 정리", "author_name": "홈카페연구소"})
            if "timedtext" in url:
                return fake_xml
            return fake_html

        with patch("shortform_studio.shorts_learner._http_get_text", side_effect=fake_get):
            ref = analyze_shorts_reference("https://www.youtube.com/shorts/dQw4w9WgXcQ", notes="빠른 컷 참고", use_ai=False)

        self.assertEqual(ref["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(ref["duration_seconds"], 22)
        self.assertGreater(ref["metrics"]["chars_per_second"], 0)
        self.assertIn("콜드브루", ref["metrics"]["opening_hook"])
        patterns = summarize_shorts_references_for_planning([ref])
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["hook_type"], "호기심·문제제기 훅")

        template = reference_to_series_template(ref)
        validated = normalize_brand_settings({"profile": {}, "series_templates": [template]})
        self.assertEqual(len(validated["series_templates"]), 1)
        self.assertEqual(validated["series_templates"][0]["duration_seconds"], 22)


if __name__ == "__main__":
    unittest.main()
