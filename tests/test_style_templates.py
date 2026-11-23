"""Tests for 1M+ Korean YouTube Shorts discovery, Vision style template generation, and ASS styling."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.media import KOREAN_SHORTS_FONT_FAMILIES, normalize_subtitle_style, srt_to_shorts_ass
from shortform_studio.store import Store
from shortform_studio.style_templates import (
    build_style_template_from_shorts,
    discover_and_update_style_templates,
    find_style_template,
    load_style_templates_state,
    parse_korean_view_count,
    search_high_view_korean_shorts,
    style_template_to_production_config,
)


class StyleTemplatesTests(unittest.TestCase):
    """Exercise 1M+ Shorts style template extraction, persistence, and ASS rendering."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "studio.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parses_korean_view_counts_accurately(self) -> None:
        self.assertEqual(parse_korean_view_count("조회수 368만회"), 3_680_000)
        self.assertEqual(parse_korean_view_count("조회수 1.5억회"), 150_000_000)
        self.assertEqual(parse_korean_view_count("조회수 88만회"), 880_000)
        self.assertEqual(parse_korean_view_count("조회수 3.4만회"), 34_000)

    def test_discovers_only_1m_plus_korean_shorts_and_persists_templates(self) -> None:
        mock_search_json = (
            '{"reelWatchEndpoint":{"videoId":"lowViewVid1"},"accessibilityText":"살림 팁, 조회수 12만회"},'
            '{"reelWatchEndpoint":{"videoId":"highViewKor"},"accessibilityText":"자고 일어나면 시원한 살림 치트키, 조회수 268만회"}'
        )

        class FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self, n: int = -1) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

        with patch("urllib.request.urlopen", return_value=FakeResp(mock_search_json.encode("utf-8"))):
            cands = search_high_view_korean_shorts("#shorts 꿀팁", min_views=1_000_000)

        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["video_id"], "highViewKor")

        mock_details = {
            "video_id": "highViewKor",
            "url": "https://www.youtube.com/shorts/highViewKor",
            "title": "자고 일어나면 시원한 살림 치트키",
            "author": "살림연구소",
            "view_count": 2_680_000,
            "duration_seconds": 26,
            "keywords": ["살림"],
            "caption_language": "ko",
            "segments": [{"start": 0.0, "dur": 3.0, "text": "아직도 이것 모르고 쓰셨어요?"}],
            "transcript": "아직도 이것 모르고 쓰셨어요? 단 3초 만에 깔끔해집니다.",
        }
        mock_vision_json = """{
            "template_name": "살림꿀팁 도현·주아 볼드형",
            "category": "정보·꿀팁",
            "description": "상단 도현체 노랑 배너와 굵은 주아체 하단 자막이 돋보이는 스타일",
            "banner_font_name": "Do Hyeon",
            "banner_font_size": 78,
            "banner_primary_colour": "&H0000E5FF",
            "banner_back_colour": "&HCC101520",
            "banner_border_style": 3,
            "banner_margin_v": 160,
            "use_hook_banner": true,
            "font_name": "Jua",
            "font_size": 68,
            "primary_colour": "&H00FFFFFF",
            "outline_colour": "&H00000000",
            "back_colour": "&H99000000",
            "border_style": 1,
            "outline": 6,
            "shadow": 3,
            "alignment": 2,
            "margin_v": 320,
            "animation": "pop",
            "transition": "cut",
            "bgm_mood": "upbeat",
            "recommended_scenes": 5,
            "hook_style": "첫 2초 반전 질문형",
            "script_rules": ["첫 문장 질문형 시작", "3초마다 화면 전환"]
        }"""

        with patch("shortform_studio.style_templates.search_high_view_korean_shorts", return_value=cands), patch(
            "shortform_studio.style_templates.fetch_youtube_shorts_data", return_value=mock_details
        ), patch("shortform_studio.style_templates._fetch_shorts_frame_part", return_value={"mime_type": "image/jpeg", "data_b64": "YWJj"}), patch(
            "shortform_studio.style_templates.ai_ready", return_value=True
        ), patch("shortform_studio.style_templates.generate_multimodal_description", return_value=mock_vision_json):
            res = discover_and_update_style_templates(self.store, max_new=2, custom_query="#shorts 살림")

        self.assertEqual(res["last_added_count"], 1)
        state = load_style_templates_state(self.store)
        auto_tpls = [t for t in state["templates"] if t["origin"] == "auto_1m_youtube"]
        self.assertEqual(len(auto_tpls), 1)
        tpl = auto_tpls[0]
        self.assertEqual(tpl["source_video"]["view_count"], 2_680_000)
        self.assertIn(tpl["visual"]["font_name"], (*KOREAN_SHORTS_FONT_FAMILIES, "Apple SD Gothic Neo", "Malgun Gothic"))

        # Check ASS rendering uses the upgraded banner size and font configuration
        cfg = style_template_to_production_config(find_style_template(self.store, tpl["id"]))
        ass_path = srt_to_shorts_ass(
            "1\n00:00:00,000 --> 00:00:02,000\n테스트 자막입니다\n",
            self.root / "out.ass",
            style=cfg["subtitle_style"],
            hook_title="할머니 댁 메밀베개",
        )
        ass_text = ass_path.read_text(encoding="utf-8")
        self.assertIn("Style: HookBanner,", ass_text)
        self.assertIn(",78,", ass_text)
        self.assertIn(",68,", ass_text)
        self.assertIn("&H0000E5FF", ass_text)

    def test_builtin_style_templates_render_cleanly(self) -> None:
        state = load_style_templates_state(self.store)
        self.assertGreaterEqual(len(state["templates"]), 4)
        for tpl in state["templates"]:
            norm = normalize_subtitle_style(tpl["visual"])
            self.assertGreaterEqual(norm["banner_font_size"], 60)
            self.assertGreaterEqual(norm["font_size"], 58)


if __name__ == "__main__":
    unittest.main()
