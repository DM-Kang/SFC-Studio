"""Tests for Korean display numeral formatting vs natural spoken Korean TTS reading."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.korean_numbers import (
    korean_text_to_tts_speech,
    normalize_korean_display_text,
)
from shortform_studio.media import (
    _build_tts_audio_filter,
    _prepare_tts_and_caption_texts,
    format_shorts_banner_text,
    generate_voiceover,
    scale_srt_timestamps,
)
from shortform_studio.planning import generate_plan


class KoreanNumbersTests(unittest.TestCase):
    """Ensure subtitles render clean Arabic numerals while TTS reads them natively."""

    def test_native_vs_sino_korean_units_and_ranges(self) -> None:
        self.assertEqual(korean_text_to_tts_speech("12시간"), "열두 시간")
        self.assertEqual(korean_text_to_tts_speech("16시간"), "열여섯 시간")
        self.assertEqual(korean_text_to_tts_speech("24시간"), "스물네 시간")
        self.assertEqual(korean_text_to_tts_speech("1시간"), "한 시간")
        self.assertEqual(korean_text_to_tts_speech("12~16시간"), "열두 시간에서 열여섯 시간")
        self.assertEqual(korean_text_to_tts_speech("3가지 팁"), "세 가지 팁")
        self.assertEqual(korean_text_to_tts_speech("2잔"), "두 잔")
        self.assertEqual(korean_text_to_tts_speech("20살"), "스무 살")

        # Sino-Korean measurements, ratios, decimals, calendar exceptions
        self.assertEqual(korean_text_to_tts_speech("1:9 비율"), "일 대 구 비율")
        self.assertEqual(korean_text_to_tts_speech("원두 50g에 찬물 450ml"), "원두 오십 그램에 찬물 사백오십 밀리리터")
        self.assertEqual(korean_text_to_tts_speech("1.5시간 동안 90% 추출"), "일 점 오 시간 동안 구십 퍼센트 추출")
        self.assertEqual(korean_text_to_tts_speech("3~5초 만에 완성"), "삼 초에서 오 초 만에 완성")
        self.assertEqual(korean_text_to_tts_speech("1번째와 2번째"), "첫 번째와 두 번째")
        self.assertEqual(korean_text_to_tts_speech("6월과 10월"), "유월과 시월")
        self.assertEqual(korean_text_to_tts_speech("$10 또는 8,000원"), "십 달러 또는 팔천원")

        # Do not mangle English model/format identifiers
        self.assertEqual(korean_text_to_tts_speech("4K 영상과 MP4 포맷"), "4K 영상과 MP4 포맷")
        self.assertIn("MP4", korean_text_to_tts_speech("MP4 파일 2개"))
        self.assertIn("두 개", korean_text_to_tts_speech("MP4 파일 2개"))

    def test_normalize_korean_display_text_converts_spelled_units_without_false_positives(self) -> None:
        spelled = "비율은 일 대 구! 원두 오십 그램에 물 사백오십 밀리리터를 붓고 열두 시간에서 열여섯 시간 우립니다."
        cleaned = normalize_korean_display_text(spelled)
        self.assertIn("1:9", cleaned)
        self.assertIn("50g", cleaned)
        self.assertIn("450ml", cleaned)
        self.assertIn("12시간", cleaned)
        self.assertIn("16시간", cleaned)

        # Idiomatic prose stays untouched
        idiom = "세상에서 가장 좋은 이유 한 가지를 이 분에게 한 번 말해보세요."
        self.assertEqual(normalize_korean_display_text(idiom), idiom)

    def test_tts_speaks_korean_words_while_captions_show_digits(self) -> None:
        raw = "비율은 1:9! 50g에 450ml 붓고 12~16시간 냉장 보관하세요."
        spoken, display = _prepare_tts_and_caption_texts(raw)
        self.assertEqual(display, raw)
        self.assertEqual(spoken, "비율은 일 대 구! 오십 그램에 사백오십 밀리리터 붓고 열두 시간에서 열여섯 시간 냉장 보관하세요.")

        captured: dict[str, str] = {}

        class FakeResponse:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

        def fake_urlopen(req, timeout=60):
            import json

            payload = json.loads(req.data.decode("utf-8"))
            captured["tts_input"] = payload["contents"][0]["parts"][0]["text"]
            return FakeResponse(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "audio/wav",
                                                "data": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=",
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            with patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": "fake-key" if k == "GEMINI_API_KEY" else d), patch(
                "shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"
            ), patch("shortform_studio.media.urllib.request.urlopen", side_effect=fake_urlopen), patch(
                "shortform_studio.media._run_ffmpeg",
                side_effect=lambda cmd, cwd=None: ((cwd / "voiceover.mp3").write_bytes(b"mp3"), (0, ""))[1],
            ), patch("shortform_studio.media.probe_audio_duration", return_value=6.0):
                res = generate_voiceover(
                    "원두 오십 그램에 물 사백오십 밀리리터 붓고 열두 시간 우리세요.",
                    out_dir,
                    tts_provider="gemini_tts",
                )

            srt_text = Path(res["captions_path"]).read_text(encoding="utf-8")
            # TTS receives natural spoken words ("열두 시간", "오십 그램")
            self.assertIn("열두 시간", captured["tts_input"])
            self.assertIn("오십 그램", captured["tts_input"])
            self.assertIn("사백오십 밀리리터", captured["tts_input"])
            # Burned captions show clean numerals ("50g", "450ml", "12시간")
            self.assertIn("50g", srt_text)
            self.assertIn("450ml", srt_text)
            self.assertIn("12시간", srt_text)

    def test_plan_payload_normalizes_spelled_numbers_to_digits(self) -> None:
        mock_ai = (
            {
                "title": "오십 그램 콜드브루",
                "hook": "일 대 구 황금비율!",
                "script": "원두 오십 그램에 물 사백오십 밀리리터를 붓고 열두 시간 우리세요.",
                "caption": "열두 시간 냉침 꿀팁",
                "hashtags": ["#콜드브루"],
                "video_prompt": "Vertical iced coffee extraction",
                "duration_seconds": 15,
                "claims": [],
                "scenes": [
                    {"id": "scene-1", "narration": "오십 그램에 사백오십 밀리리터 붓고 열두 시간 우리세요.", "visual_query": "pouring cold brew coffee"}
                ],
            },
            "gemini_api:gemini-4-hydrogen",
        )
        brief = {"topic": "콜드브루", "audience": "일반", "tone": "친근", "language": "Korean", "platforms": ["youtube"], "duration_seconds": 15}
        with patch("shortform_studio.planning.generate_json", return_value=mock_ai):
            plan = generate_plan(brief, use_ai=True, research_sources=[])
        self.assertIn("50g", plan["script"])
        self.assertIn("450ml", plan["script"])
        self.assertIn("12시간", plan["script"])
        self.assertIn("1:9", plan["hook"])
        self.assertIn("12시간", plan["scenes"][0]["narration"])

    def test_tts_pace_filter_and_srt_scaling(self) -> None:
        flt = _build_tts_audio_filter(1.5, trim_silence=True)
        self.assertIn("silenceremove=", flt)
        self.assertIn("atempo=1.500", flt)

        base_srt = "1\n00:00:00,000 --> 00:00:03,000\n첫 자막\n\n2\n00:00:03,000 --> 00:00:06,000\n둘째 자막\n"
        scaled = scale_srt_timestamps(base_srt, 1.5)
        self.assertIn("00:00:00,000 --> 00:00:02,000", scaled)
        self.assertIn("00:00:02,000 --> 00:00:04,000", scaled)

    def test_banner_text_keeps_korean_compound_nouns_intact(self) -> None:
        self.assertEqual(format_shorts_banner_text("목 뻐근할 땐 초승달 메밀베개"), r"목 뻐근할 땐\N초승달 메밀베개")
        self.assertEqual(format_shorts_banner_text("아침마다 뻐근한 목 초승달 경추베개"), r"아침마다 뻐근한 목\N초승달 경추베개")
        self.assertEqual(format_shorts_banner_text("텁텁함 없는 콜드브루 황금 비율"), r"텁텁함 없는\N콜드브루 황금 비율")
        self.assertEqual(format_shorts_banner_text("한 줄 제목"), "한 줄 제목")

    def test_two_tier_banner_and_keyword_highlighted_subtitles(self) -> None:
        from shortform_studio.media import srt_to_shorts_ass

        with tempfile.TemporaryDirectory() as tmp:
            ass_path = srt_to_shorts_ass(
                "1\n00:00:00,000 --> 00:00:01,500\n60cm 크기 천연 메밀 100% 국내 제조!\n",
                Path(tmp) / "captions.ass",
                style={"preset": "shorts_punch"},
                hook_title="목 뻐근할 땐 초승달 메밀베개",
            )
            ass_text = ass_path.read_text(encoding="utf-8")
        self.assertIn(r"{\fs55\c&H00FFFFFF&}목 뻐근할 땐\N{\fs85\c&H0000E5FF}", ass_text)
        self.assertIn(r"{\c&H0000E5FF&}60cm{\c&H00FFFFFF&}", ass_text)
        self.assertIn(r"{\c&H0000E5FF&}100%{\c&H00FFFFFF&}", ass_text)
        self.assertIn(r"{\c&H0000E5FF&}국내 제조{\c&H00FFFFFF&}", ass_text)

    def test_multiscene_blurred_card_and_varied_motions(self) -> None:
        from shortform_studio.media import build_multiscene_ffmpeg_command

        cmd = build_multiscene_ffmpeg_command(
            "ffmpeg",
            [
                {"relative_path": "clips/img1.jpg", "duration_seconds": 4.0, "media_type": "image", "aspect_fit": "card"},
                {"relative_path": "clips/img2.png", "duration_seconds": 4.0, "media_type": "image"},
                {"relative_path": "clips/img3.png", "duration_seconds": 4.0, "media_type": "image"},
                {"relative_path": "clips/img4.png", "duration_seconds": 4.0, "media_type": "image"},
            ],
            "out.mp4",
            transition="fade",
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("boxblur=24:4", fc)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", fc)
        # Distinct camera motions across scenes (zoom-in/pan L->R/zoom-out/pan R->L)
        self.assertIn("if(eq(on,0),0", fc)
        self.assertIn("1.12-0.12*on/", fc)
        self.assertIn("if(eq(on,0),iw-iw/zoom", fc)

    def test_generate_ai_scene_image_attaches_reference_product_photos(self) -> None:
        import base64 as _base64
        import json as _json
        from shortform_studio.media import generate_ai_scene_image

        captured_bodies: list[dict] = []

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

        def _fake_urlopen(req, timeout=60):
            captured_bodies.append(_json.loads(req.data.decode("utf-8")))
            return _FakeResp(
                _json.dumps(
                    {
                        "candidates": [
                            {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _base64.b64encode(b"fake-png").decode("ascii")}}]}}
                        ]
                    }
                ).encode("utf-8")
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref_img = root / "product.jpg"
            ref_img.write_bytes(b"\xff\xd8\xff\xe0dummy-jpeg")
            out_img = root / "ai_scene.png"
            with patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": "fake-gem-key" if k == "GEMINI_API_KEY" else d), patch(
                "shortform_studio.media.urllib.request.urlopen", side_effect=_fake_urlopen
            ):
                saved = generate_ai_scene_image("bedroom pillow scene", out_img, reference_image_paths=[ref_img])

            self.assertEqual(saved, out_img)
            self.assertEqual(out_img.read_bytes(), b"fake-png")
            parts = captured_bodies[0]["contents"][0]["parts"]
            self.assertTrue(any("inlineData" in part for part in parts))
            text_part = next(part["text"] for part in parts if "text" in part)
            self.assertIn("reference photo", text_part)

    def test_synthesize_sfx_and_mix_bgm_builds_amix_command(self) -> None:
        from shortform_studio.media import synthesize_sfx_and_mix_bgm

        commands: list[list[str]] = []

        def _fake_ff(cmd: list[str], cwd=None):
            commands.append(cmd)
            Path(cmd[-1]).write_bytes(b"audio")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bgm = root / "bgm_auto.mp3"
            bgm.write_bytes(b"mp3")
            out_mix = root / "bgm_sfx.mp3"
            with patch("shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                "shortform_studio.media._run_ffmpeg", side_effect=_fake_ff
            ):
                res = synthesize_sfx_and_mix_bgm(bgm, [3.0, 4.0, 3.5], out_mix)
            self.assertEqual(res, out_mix)
            self.assertTrue(out_mix.is_file())
            mix_cmd = commands[-1]
            fc = mix_cmd[mix_cmd.index("-filter_complex") + 1]
            self.assertIn("adelay=40|40", fc)
            self.assertIn("adelay=3000|3000", fc)
            self.assertIn("adelay=7000|7000", fc)
            self.assertIn("normalize=0", fc)

    def test_rejection_feedback_enforces_7_scenes_and_banned_phrases(self) -> None:
        from shortform_studio.planning import revise_plan_with_feedback

        # Simulate AI still returning 5 scenes AND accidentally keeping the forbidden ending phrase
        mock_ai = (
            {
                "title": "초승달 전통 메밀베개",
                "hook": "자고 일어나도 목이 뻐근한 이유?",
                "script": "자고 일어나도 목이 뻐근한 이유! 100% 천연 메밀이 목 곡선을 잡아줍니다. 지금 특별한 가격으로 만나보세요.",
                "caption": "초승달 메밀베개 소개. 지금 특별한 가격으로 만나보세요.",
                "hashtags": ["#메밀베개"],
                "video_prompt": "Vertical buckwheat pillow showcase",
                "duration_seconds": 30,
                "claims": [],
                "scenes": [
                    {"narration": "자고 일어나도 목이 뻐근한 이유?", "visual_query": "neck pain morning bedroom"},
                    {"narration": "100% 천연 메밀 충전재로 통기성이 좋습니다.", "visual_query": "buckwheat hulls pouring bowl"},
                    {"narration": "전통 자수 면 겉커버로 촉감이 부드럽습니다.", "visual_query": "embroidered cotton pillow fabric"},
                    {"narration": "초승달 설계가 목 곡선을 받쳐줍니다.", "visual_query": "crescent neck pillow ergonomic"},
                    {"narration": "지퍼 분리 세탁이 간편합니다. 지금 특별한 가격으로 만나보세요.", "visual_query": "zipper pillow cover washing"},
                ],
            },
            "gemini_api:gemini-4-hydrogen",
        )
        brief = {
            "topic": "전통 메밀베개",
            "audience": "일반",
            "tone": "홈쇼핑",
            "language": "Korean",
            "platforms": ["youtube"],
            "duration_seconds": 30,
            "rejection_history": ["마지막에 특별한 가격으로 만나보세요 이부분은 빼줘"],
        }
        prev_plan = {"duration_seconds": 30, "sources": []}
        with patch("shortform_studio.planning.generate_json", return_value=mock_ai):
            revised = revise_plan_with_feedback(
                brief,
                prev_plan,
                "씬 7개로 만들어 주고 씬 순서대로 장면을 나눠줘",
                use_ai=True,
                research_sources=[],
            )
        self.assertEqual(len(revised["scenes"]), 7)
        self.assertNotIn("특별한 가격", revised["script"])
        self.assertNotIn("특별한 가격", revised["caption"])
        for sc in revised["scenes"]:
            self.assertNotIn("특별한 가격", sc["narration"])

    def test_generate_ai_scene_video_omni_and_lyria_bgm_and_clean_pill(self) -> None:
        import base64 as _base64
        import json as _json
        from shortform_studio.media import (
            ensure_ambient_bgm,
            generate_ai_scene_video_omni,
            srt_to_shorts_ass,
        )
        from shortform_studio.style_templates import _builtin_style_templates, style_template_to_production_config

        flow_tpl = next((t for t in _builtin_style_templates() if t["id"] == "builtin_flow_cinematic_pill"), None)
        self.assertIsNotNone(flow_tpl)
        cfg = style_template_to_production_config(flow_tpl)
        self.assertFalse(cfg["use_hook_banner"])
        self.assertEqual(cfg["subtitle_style"]["preset"], "clean_pill")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ass_path = srt_to_shorts_ass(
                "1\n00:00:00,000 --> 00:00:02,000\n할머니 방에 있던 100% 천연 베개\n",
                root / "pill.ass",
                style={"preset": "clean_pill"},
                hook_title="",
            )
            ass_text = ass_path.read_text(encoding="utf-8")
            self.assertIn("&H00202632", ass_text)
            self.assertIn("&H00FFFFFF", ass_text)
            self.assertIn(r"{\c&H00D84E1D&}100%{\c&H00202632&}", ass_text)

            ref_img = root / "pillow.jpg"
            ref_img.write_bytes(b"\xff\xd8\xff\xe0dummy-jpeg")
            captured_omni: dict = {}
            fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 5000

            class _FakeResp:
                def __init__(self, body: bytes) -> None:
                    self._body = body

                def read(self) -> bytes:
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, *args) -> None:
                    return None

            def _fake_urlopen(req, timeout=60):
                payload = _json.loads(req.data.decode("utf-8"))
                if "interactions" in req.full_url:
                    captured_omni["body"] = payload
                    return _FakeResp(
                        _json.dumps(
                            {
                                "id": "i1",
                                "status": "completed",
                                "steps": [
                                    {
                                        "type": "model_output",
                                        "content": [{"type": "video", "mime_type": "video/mp4", "data": _base64.b64encode(fake_mp4).decode("ascii")}],
                                    }
                                ],
                            }
                        ).encode("utf-8")
                    )
                fake_mp3 = b"ID3" + b"1" * 10000
                return _FakeResp(
                    _json.dumps(
                        {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/mpeg", "data": _base64.b64encode(fake_mp3).decode("ascii")}}]}}]}
                    ).encode("utf-8")
                )

            with patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": "fake-key" if k == "GEMINI_API_KEY" else d), patch(
                "shortform_studio.media.urllib.request.urlopen", side_effect=_fake_urlopen
            ):
                omni_out = generate_ai_scene_video_omni("hand stroking pillow", root / "omni.mp4", reference_image_paths=[ref_img])
                self.assertEqual(omni_out, root / "omni.mp4")
                self.assertEqual((root / "omni.mp4").read_bytes(), fake_mp4)
                self.assertEqual(captured_omni["body"]["model"], "gemini-omni-1.2-flash")
                self.assertEqual(captured_omni["body"]["input"][0]["type"], "image")

                with patch("shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                    "shortform_studio.media._run_ffmpeg",
                    side_effect=lambda cmd, cwd=None: ((cwd / cmd[-1]).write_bytes(b"lyria-out"), (0, ""))[1],
                ):
                    bgm_out = ensure_ambient_bgm(root / "bgm_auto.mp3", 12.0, mood="calm")
                self.assertEqual(bgm_out, root / "bgm_auto.mp3")
                self.assertEqual(bgm_out.read_bytes(), b"lyria-out")


if __name__ == "__main__":
    unittest.main()
