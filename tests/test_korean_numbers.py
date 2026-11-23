"""Tests for Korean display numeral formatting vs natural spoken Korean TTS reading."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.korean_numbers import (
    korean_text_to_tts_speech,
    normalize_korean_display_text,
)
from shortform_studio.media import _prepare_tts_and_caption_texts, generate_voiceover
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


if __name__ == "__main__":
    unittest.main()
