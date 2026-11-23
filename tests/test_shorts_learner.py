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

    def test_ass_hook_banner_scene_synced_tts_smart_clip_ranking_and_ai_fallback(self) -> None:
        """@brief Verify ASS banner generation, scene-synced TTS timing merge, smart stock ranking, and AI image fallback."""
        from shortform_studio.media import (
            build_subtitles_filter,
            generate_scene_synced_voiceover,
            srt_to_shorts_ass,
        )
        from shortform_studio.shorts_learner import extract_youtube_urls, resolve_learned_production_style
        from shortform_studio.workflow import _ai_rerank_and_fill_visuals, _auto_select_scene_clips

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ass_path = srt_to_shorts_ass(
                "1\n00:00:00,000 --> 00:00:02,000\n콜드브루 1:9 비율\n",
                root / "captions.ass",
                style={"preset": "shorts_punch"},
                hook_title="집에서 카페급 콜드브루!",
            )
            ass_text = ass_path.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1080", ass_text)
            self.assertIn("Style: HookBanner", ass_text)
            self.assertIn("집에서 카페급 콜드브루!", ass_text)
            self.assertEqual(build_subtitles_filter("captions.ass"), "ass=captions.ass")

            def fake_synth(text: str, out_dir: Path, **_kw: object) -> dict[str, str]:
                out_dir.mkdir(parents=True, exist_ok=True)
                mp3 = out_dir / "voiceover.mp3"
                srt = out_dir / "captions.srt"
                mp3.write_bytes(b"mp3")
                srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
                return {"audio_path": str(mp3), "captions_path": str(srt), "provider": "elevenlabs", "voice_id": "v1"}

            def fake_ff(cmd: list[str], **_kw: object) -> tuple[int, str]:
                Path(cmd[-1]).write_bytes(b"merged-mp3")
                return 0, ""

            with (
                patch("shortform_studio.media.shutil.which", return_value="ffmpeg"),
                patch("shortform_studio.media.probe_audio_duration", side_effect=[2.4, 3.1]),
                patch("shortform_studio.media._run_ffmpeg", side_effect=fake_ff),
            ):
                synced = generate_scene_synced_voiceover(
                    [{"narration": "첫 장면"}, {"narration": "둘째 장면"}],
                    "첫 장면 둘째 장면",
                    root / "vo",
                    synth_fn=fake_synth,
                )
            self.assertEqual(synced["scene_durations"], [2.4, 3.1])
            merged_srt = Path(synced["captions_path"]).read_text(encoding="utf-8")
            self.assertIn("00:00:02,400 --> 00:00:04,400", merged_srt)

            # Smart ranking chooses matching coffee clip over unrelated lemur clip
            scenes = [{"id": "scene-1", "visual_query": "cold brew coffee glass"}]
            mats = [
                {"scene_id": "scene-1", "provider_id": "pixabay-bad", "title": "Pixabay · lemur", "tags": "lemur, animal", "download_url": "https://cdn.pixabay.com/bad.mp4", "width": 720, "height": 1280, "duration_seconds": 10},
                {"scene_id": "scene-1", "provider_id": "pixabay-good", "title": "Pixabay · coffee", "tags": "cold brew, coffee, glass", "download_url": "https://cdn.pixabay.com/good.mp4", "width": 1080, "height": 1920, "duration_seconds": 10},
            ]
            chosen = _auto_select_scene_clips(scenes, mats, 10)
            self.assertEqual(chosen[0]["provider_id"], "pixabay-good")

            # AI image fallback activates when stock overlap is 0
            clips_dir = root / "clips"
            clips_dir.mkdir()
            with (
                patch("shortform_studio.workflow.ai_ready", return_value=False),
                patch("shortform_studio.workflow.get_secret", side_effect=lambda k, d="": "gem-key" if k == "GEMINI_API_KEY" else d),
                patch("shortform_studio.workflow.generate_ai_scene_image", side_effect=lambda p, out: (out.write_bytes(b"png"), out)[1]),
            ):
                filled = _ai_rerank_and_fill_visuals(
                    root,
                    [{"id": "scene-1", "narration": "콜드브루", "visual_query": "espresso extraction"}],
                    [mats[0]],
                    [{"source_kind": "stock", "provider_id": "pixabay-bad", "duration_seconds": 4.0}],
                )
            self.assertEqual(filled[0]["source_kind"], "uploaded")
            self.assertEqual(filled[0]["filename"], "ai_scene_1.png")

        urls = extract_youtube_urls("참고 링크: https://www.youtube.com/shorts/dQw4w9WgXcQ 그리고 https://youtu.be/abcDEF12345")
        self.assertEqual(len(urls), 2)
        style_cfg = resolve_learned_production_style(
            [{"active_for_planning": True, "analysis": {"caption_style_recommendation": "yellow_pop", "bgm_mood": "calm", "has_top_banner": True}, "metrics": {"avg_segment_seconds": 2.5, "recommended_scenes": 6}}]
        )
        self.assertEqual(style_cfg, {"caption_preset": "yellow_pop", "bgm_mood": "calm", "transition": "cut", "use_hook_banner": True})

    def test_korean_plan_and_tts_never_speak_chinese_scene_narrations(self) -> None:
        """@brief Replace leaked Chinese scene narrations with Korean script chunks in planning and TTS."""
        from shortform_studio.media import generate_scene_synced_voiceover

        mixed_plan = {
            "title": "콜드브루 황금 비율",
            "hook": "카페보다 맛있는 비결!",
            "script": "원두 50g을 준비하세요. 차가운 물 450ml를 부으세요. 14시간 냉장 추출하세요.",
            "caption": "집에서 만드는 콜드브루",
            "hashtags": ["#콜드브루"],
            "video_prompt": "pouring iced cold brew coffee",
            "duration_seconds": 18,
            "claims": [],
            "scenes": [
                {"narration": "准备咖啡豆", "visual_query": "coffee beans"},
                {"narration": "准备冷水", "visual_query": "pouring water coffee"},
                {"narration": "放置冰箱冷藏", "visual_query": "cold brew bottle"},
            ],
        }
        payload = _build_plan_payload(mixed_plan, "ollama:qwen2.5:7b", {"topic": "콜드브루", "language": "Korean", "duration_seconds": 18}, [])
        for scene in payload["scenes"]:
            self.assertNotRegex(scene["narration"], r"[\u4e00-\u9fff]")
            self.assertRegex(scene["narration"], r"[\uac00-\ud7a3]")

        spoken_texts: list[str] = []

        def fake_synth(text: str, out_dir: Path, **_kw: object) -> dict[str, str]:
            spoken_texts.append(text)
            out_dir.mkdir(parents=True, exist_ok=True)
            mp3 = out_dir / "voiceover.mp3"
            srt = out_dir / "captions.srt"
            mp3.write_bytes(b"mp3")
            srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
            return {"audio_path": str(mp3), "captions_path": str(srt), "provider": "elevenlabs", "voice_id": "v1"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("shortform_studio.media.shutil.which", return_value="ffmpeg"),
                patch("shortform_studio.media.probe_audio_duration", return_value=2.0),
                patch("shortform_studio.media._run_ffmpeg", side_effect=lambda cmd, **_kw: (Path(cmd[-1]).write_bytes(b"mp3"), (0, ""))[1]),
            ):
                generate_scene_synced_voiceover(
                    [{"narration": "准备咖啡豆"}, {"narration": "准备冷水"}],
                    "원두 50g을 준비하세요. 물 450ml를 부으세요.",
                    root,
                    synth_fn=fake_synth,
                )
        self.assertEqual(len(spoken_texts), 2)
        for line in spoken_texts:
            self.assertNotRegex(line, r"[\u4e00-\u9fff]")
            self.assertRegex(line, r"[\uac00-\ud7a3]")

    def test_banner_wrap_word_boundary_srt_and_gemini_native_tts(self) -> None:
        """@brief Wrap banners on word boundaries, keep Korean SRT words intact, and synthesize Gemini TTS."""
        from shortform_studio.media import alignment_to_srt, format_shorts_banner_text

        banner = format_shorts_banner_text("", "집에서 만든 콜드브루가 텁텁했다면, 비율과 거르는 법을 놓친 겁니다!")
        self.assertNotIn("놓\n", banner)
        self.assertNotIn("겁\\", banner)
        for line in banner.split(r"\N"):
            self.assertLessEqual(len(line), 22)

        phrase = "집에서 만든 콜드브루가 텁텁했다면, 비율과 거르는 법을 놓친 겁니다!"
        alignment = {
            "characters": list(phrase),
            "character_start_times_seconds": [i * 0.1 for i in range(len(phrase))],
            "character_end_times_seconds": [(i + 1) * 0.1 for i in range(len(phrase))],
        }
        srt = alignment_to_srt(alignment, max_chars=16)
        self.assertNotIn("텁텁했\n", srt)
        self.assertNotIn("준\n", srt)
        self.assertIn("텁텁했다면,", srt)

        fake_gemini_audio = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000", "data": base64.b64encode(b"\x00\x00" * 100).decode("ascii")}}]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": "gem-key" if k == "GEMINI_API_KEY" else d),
                patch("shortform_studio.media.shutil.which", return_value="ffmpeg"),
                patch("shortform_studio.media._run_ffmpeg", side_effect=lambda cmd, cwd: ((cwd / cmd[-1]).write_bytes(b"mp3"), (0, ""))[1]),
                patch("shortform_studio.media.probe_audio_duration", return_value=3.2),
                patch("urllib.request.urlopen", return_value=_FakeUrlResponse(json.dumps(fake_gemini_audio).encode("utf-8"))),
            ):
                vo = generate_voiceover("집에서 만든 콜드브루, 오늘 밤 시작해 보세요!", Path(tmp), tts_provider="gemini_tts")
            self.assertEqual(vo["provider"], "gemini_tts")
            self.assertIn("콜드브루,", Path(vo["captions_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
