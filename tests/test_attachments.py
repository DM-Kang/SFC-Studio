"""Tests for multi-format attachment ingestion, extraction, visual analysis, and planning/assembly integration."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shortform_studio.attachments import (
    analyze_item_attachments,
    attachments_to_citable_sources,
    build_attachment_record,
    extract_document_text,
    summarize_attachments_for_planning,
)
from shortform_studio.planning import generate_plan
from shortform_studio.store import Store
from shortform_studio.workflow import (
    _ai_rerank_and_fill_visuals,
    _assign_attached_media_to_scenes,
    run_planning,
)


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def _write_xlsx(path: Path, sheet_name: str, rows: list[list[str]]) -> None:
    shared: list[str] = []
    sheet_rows: list[str] = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, val in enumerate(row):
            col = chr(ord("A") + c_idx)
            s_idx = len(shared)
            shared.append(val)
            cells.append(f'<c r="{col}{r_idx}" t="s"><v>{s_idx}</v></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    ss_xml = '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + "".join(
        f"<si><t>{v}</t></si>" for v in shared
    ) + "</sst>"
    wb_xml = (
        '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    ws_xml = '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(sheet_rows) + "</sheetData></worksheet>"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", ss_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", ws_xml)


def _write_pptx(path: Path, slides: list[tuple[str, str]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for idx, (slide_text, note_text) in enumerate(slides, start=1):
            slide_xml = (
                '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{slide_text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{idx}.xml", slide_xml)
            if note_text:
                note_xml = (
                    '<?xml version="1.0"?><p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{note_text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
                )
                zf.writestr(f"ppt/notesSlides/notesSlide{idx}.xml", note_xml)


def _write_hwpx(path: Path, lines: list[str]) -> None:
    body = "".join(f"<hp:p><hp:t>{line}</hp:t></hp:p>" for line in lines)
    xml = f'<?xml version="1.0"?><hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">{body}</hs:sec>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Contents/section0.xml", xml)


class AttachmentsTests(unittest.TestCase):
    """Exercise multi-format attachment extraction and workflow usage."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_extracts_plain_text_cp949_and_office_formats(self) -> None:
        txt_file = self.root / "notes.txt"
        txt_file.write_bytes("콜드브루 1대 9 비율 테스트".encode("cp949"))
        txt_out, txt_note = extract_document_text(txt_file, ".txt")
        self.assertIn("콜드브루 1대 9 비율", txt_out)
        self.assertEqual(txt_note, "")

        docx_file = self.root / "brief.docx"
        _write_docx(docx_file, ["제품 핵심 스펙", "배터리 48시간 지속"])
        docx_out, docx_note = extract_document_text(docx_file, ".docx")
        self.assertIn("배터리 48시간 지속", docx_out)
        self.assertEqual(docx_note, "")

        xlsx_file = self.root / "metrics.xlsx"
        _write_xlsx(xlsx_file, "단가표", [["모델", "가격"], ["Hydrogen", "$1.50"]])
        xlsx_out, xlsx_note = extract_document_text(xlsx_file, ".xlsx")
        self.assertIn("[시트: 단가표]", xlsx_out)
        self.assertIn("Hydrogen | $1.50", xlsx_out)
        self.assertEqual(xlsx_note, "")

        pptx_file = self.root / "deck.pptx"
        _write_pptx(pptx_file, [("첫 슬라이드 제목", "오프닝 2초 훅 강조"), ("두번째 슬라이드", "")])
        pptx_out, pptx_note = extract_document_text(pptx_file, ".pptx")
        self.assertIn("[슬라이드 1]", pptx_out)
        self.assertIn("오프닝 2초 훅 강조", pptx_out)
        self.assertIn("두번째 슬라이드", pptx_out)
        self.assertEqual(pptx_note, "")

        hwpx_file = self.root / "guide.hwpx"
        _write_hwpx(hwpx_file, ["한컴 문서 요약", "안전 수칙 준수"])
        hwpx_out, _ = extract_document_text(hwpx_file, ".hwpx")
        self.assertIn("안전 수칙 준수", hwpx_out)

    def test_corrupted_office_archive_returns_safe_note(self) -> None:
        bad_docx = self.root / "corrupt.docx"
        bad_docx.write_bytes(b"not-a-zip-file")
        text, note = extract_document_text(bad_docx, ".docx")
        self.assertEqual(text, "")
        self.assertIn("문서 구조 분석 실패", note)

    def test_attachments_become_citable_sources_and_feed_planning(self) -> None:
        store = Store(self.root / "studio.sqlite3")
        item = store.create_item({"topic": "신제품 안내", "platforms": ["youtube"], "language": "Korean"})
        doc_path = self.root / "spec.docx"
        _write_docx(doc_path, ["무게 120g 초경량 디자인", "방수 IP68 지원"])
        rec = build_attachment_record(
            original_name="spec.docx",
            stored_name="spec_abc123.docx",
            relative_path="attachments/spec_abc123.docx",
            ext=".docx",
            kind="document",
            size_bytes=doc_path.stat().st_size,
            file_path=doc_path,
        )
        excluded = {**rec, "id": "att-excluded", "original_name": "secret.txt", "included": False, "extracted_text": "제외됨"}
        attachments = [rec, excluded]

        sources = attachments_to_citable_sources(attachments)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "A1")
        self.assertEqual(sources[0]["source_kind"], "attachment")
        self.assertIn("IP68", sources[0]["content"])

        summary = summarize_attachments_for_planning(attachments)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["source_id"], "A1")

        captured: dict[str, str] = {}

        def fake_generate_json(sys_prompt: str, user_prompt: str, stage: str = "planning"):
            captured["user_prompt"] = user_prompt
            return (
                {
                    "title": "초경량 120g 방수 신제품",
                    "hook": "120g 무게에 IP68 방수까지 됩니다!",
                    "script": "120g 초경량 디자인에 IP68 방수까지 지원합니다.",
                    "caption": "신제품 핵심 요약",
                    "hashtags": ["#신제품"],
                    "video_prompt": "Minimal studio product showcase vertical",
                    "duration_seconds": 15,
                    "claims": [{"claim": "120g IP68 지원", "source_ids": ["A1"], "evidence": "spec.docx"}],
                    "scenes": [
                        {"id": "scene-1", "narration": "120g 초경량 디자인!", "visual_query": "lightweight gadget closeup"},
                        {"id": "scene-2", "narration": "IP68 방수로 거뜬합니다.", "visual_query": "waterproof splash test"},
                    ],
                },
                "gemini_api:gemini-4-hydrogen",
            )

        with patch("shortform_studio.workflow.get_projects_dir", return_value=self.root / "projects"), patch(
            "shortform_studio.workflow.ai_ready", return_value=True
        ), patch("shortform_studio.planning.generate_json", side_effect=fake_generate_json):
            store.update_payload(item["id"], {"attachments": [rec]})
            run_planning(store, item["id"])

        updated = store.get_item(item["id"])
        self.assertIn("spec.docx", captured["user_prompt"])
        self.assertIn("IP68", captured["user_prompt"])
        self.assertEqual(updated["plan"]["sources"][0]["id"], "A1")
        self.assertEqual(updated["plan"]["claims"][0]["source_ids"], ["A1"])

    def test_analyze_image_attachment_and_preserve_scene_assignment(self) -> None:
        item_dir = self.root / "projects" / "item-1"
        clips_dir = item_dir / "clips"
        clips_dir.mkdir(parents=True)
        img_path = clips_dir / "product_11111111.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0dummy-jpeg")

        att = {
            "id": "att-img-1",
            "original_name": "콜드브루_실물사진.jpg",
            "stored_name": "product_11111111.jpg",
            "relative_path": "clips/product_11111111.jpg",
            "ext": ".jpg",
            "kind": "image",
            "size_bytes": 16,
            "included": True,
            "extracted_text": "",
            "extraction_note": "",
            "visual_summary": "",
            "media_info": {"width": 1080, "height": 1920},
        }
        with patch("shortform_studio.attachments.describe_media_with_ai", return_value="유리병에 담긴 진한 콜드브루 커피와 얼음"):
            enriched, changed = analyze_item_attachments([att], item_dir)
        self.assertTrue(changed)
        self.assertIn("콜드브루 커피", enriched[0]["visual_summary"])

        scenes = [
            {"id": "scene-1", "narration": "첫 장면", "visual_query": "cold brew glass"},
            {"id": "scene-2", "narration": "둘째 장면", "visual_query": "coffee beans"},
        ]
        with patch("shortform_studio.workflow.ai_ready", return_value=True), patch(
            "shortform_studio.workflow.generate_json",
            return_value=({"assignments": {"scene-1": "att-img-1"}}, "gemini_api:gemini-4-hydrogen"),
        ):
            slots = _assign_attached_media_to_scenes(item_dir, scenes, enriched, 15)

        self.assertIsNotNone(slots[0])
        self.assertEqual(slots[0]["filename"], "product_11111111.jpg")
        self.assertTrue(slots[0]["from_attachment"])
        self.assertIsNone(slots[1])

        merged_inputs = [
            slots[0],
            {"source_kind": "stock", "provider_id": "px-1", "duration_seconds": 5.0},
        ]
        with patch("shortform_studio.workflow.get_secret", return_value="dummy-key"), patch(
            "shortform_studio.workflow.generate_ai_scene_image", return_value=clips_dir / "ai_scene_2.png"
        ) as mock_gen:
            (clips_dir / "ai_scene_2.png").write_bytes(b"png")
            result_slots = _ai_rerank_and_fill_visuals(item_dir, scenes, [], merged_inputs, visual_mode="ai_images")

        # Attached photo on scene-1 stays intact; scene-2 uses generated AI image.
        self.assertEqual(result_slots[0]["filename"], "product_11111111.jpg")
        self.assertEqual(result_slots[1]["filename"], "ai_scene_2.png")
        mock_gen.assert_called_once()

    def test_image_zoompan_command_does_not_combine_loop_with_zoompan(self) -> None:
        from shortform_studio.media import build_multiscene_ffmpeg_command

        cmd = build_multiscene_ffmpeg_command(
            "ffmpeg",
            [
                {"relative_path": "clips/ai_scene_1.png", "duration_seconds": 5.0, "media_type": "image"},
                {"relative_path": "clips/ai_scene_2.png", "duration_seconds": 5.0, "media_type": "image"},
            ],
            "out.mp4",
            transition="fade",
        )
        self.assertNotIn("-loop", cmd)
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("zoompan=", fc)
        self.assertIn("trim=duration=5.00", fc)


if __name__ == "__main__":
    unittest.main()
