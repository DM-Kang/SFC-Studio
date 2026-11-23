"""Attachment ingestion, document text extraction, and multimodal visual summarization."""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ai import describe_media_with_ai
from .media import extract_video_frame

MAX_ATTACHMENTS_PER_ITEM = 12
MAX_ATTACHMENT_TEXT_CHARS = 12_000
MAX_DOC_IMAGE_BYTES = 50_000_000
MAX_VIDEO_BYTES = 250_000_000

PLAIN_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".srt",
    ".vtt",
    ".html",
    ".htm",
}
OOXML_AND_DOC_EXTS = {
    ".docx",
    ".xlsx",
    ".xlsm",
    ".pptx",
    ".hwpx",
    ".odt",
    ".ods",
    ".odp",
    ".pdf",
    ".doc",
    ".xls",
    ".ppt",
}
DOCUMENT_EXTS = PLAIN_TEXT_EXTS | OOXML_AND_DOC_EXTS
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac"}
SUPPORTED_ATTACHMENT_EXTS = DOCUMENT_EXTS | IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,:/()\-_%+#@]{3,}")
_UTF16_HANGUL_RE = re.compile(r"[\uac00-\ud7a3A-Za-z0-9 .,:/()\-_%+#@?!]{4,}")
_INDEX_SUFFIX_RE = re.compile(r"(\d+)\.xml$")


def classify_attachment_ext(ext: str) -> str | None:
    """@brief Classify a normalized file suffix into document, image, video, audio, or None."""
    lowered = ext.lower()
    if lowered in DOCUMENT_EXTS:
        return "document"
    if lowered in IMAGE_EXTS:
        return "image"
    if lowered in VIDEO_EXTS:
        return "video"
    if lowered in AUDIO_EXTS:
        return "audio"
    return None


def _numeric_xml_sort_key(name: str) -> tuple[int, str]:
    match = _INDEX_SUFFIX_RE.search(name)
    return (int(match.group(1)) if match else 0, name)


def _decode_text_bytes(raw: bytes) -> str:
    """@brief Decode plain-text/CSV files across UTF-8, UTF-16, and Korean CP949/EUC-KR encodings."""
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp949", "euc-kr", "latin-1"):
        try:
            text = raw.decode(encoding)
            if "\x00" not in text:
                return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _read_zip_xml(archive: zipfile.ZipFile, member: str) -> ET.Element | None:
    try:
        return ET.fromstring(archive.read(member))
    except (KeyError, ET.ParseError, OSError):
        return None


def _extract_docx_text(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        xml_names = [
            name
            for name in archive.namelist()
            if name == "word/document.xml" or name.startswith(("word/header", "word/footer"))
        ]
        xml_names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
        for member in xml_names:
            root = _read_zip_xml(archive, member)
            if root is None:
                continue
            for node in root.iter():
                local = _xml_local_name(node.tag)
                if local == "p":
                    line = "".join(
                        sub.text for sub in node.iter() if _xml_local_name(sub.tag) == "t" and sub.text
                    ).strip()
                    if line:
                        parts.append(line)
    return "\n".join(parts)


def _excel_col_index(cell_ref: str) -> int:
    col = 0
    for ch in cell_ref:
        if "A" <= ch.upper() <= "Z":
            col = col * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(0, col - 1)


def _extract_xlsx_text(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        ss_root = _read_zip_xml(archive, "xl/sharedStrings.xml")
        if ss_root is not None:
            for si in ss_root:
                if _xml_local_name(si.tag) == "si":
                    shared_strings.append("".join(t.text or "" for t in si.iter() if _xml_local_name(t.tag) == "t"))

        sheet_titles: dict[str, str] = {}
        wb_root = _read_zip_xml(archive, "xl/workbook.xml")
        rels_root = _read_zip_xml(archive, "xl/_rels/workbook.xml.rels")
        rid_to_target: dict[str, str] = {}
        if rels_root is not None:
            for rel in rels_root:
                rid = rel.attrib.get("Id")
                target = rel.attrib.get("Target")
                if rid and target:
                    clean_target = target.lstrip("/")
                    if not clean_target.startswith("xl/"):
                        clean_target = f"xl/{clean_target}"
                    rid_to_target[rid] = clean_target
        if wb_root is not None:
            for elem in wb_root.iter():
                if _xml_local_name(elem.tag) == "sheet":
                    name = elem.attrib.get("name") or ""
                    rid = next((v for k, v in elem.attrib.items() if k.endswith("}id") or k == "r:id"), "")
                    if rid and rid in rid_to_target and name:
                        sheet_titles[rid_to_target[rid]] = name

        sheet_files = sorted(
            [n for n in archive.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")],
            key=_numeric_xml_sort_key,
        )
        for sheet_file in sheet_files:
            root = _read_zip_xml(archive, sheet_file)
            if root is None:
                continue
            rows_out: list[str] = []
            for row_node in root.iter():
                if _xml_local_name(row_node.tag) != "row":
                    continue
                row_cells: dict[int, str] = {}
                max_col = -1
                fallback_col = 0
                for cell in row_node:
                    if _xml_local_name(cell.tag) != "c":
                        continue
                    ref = cell.attrib.get("r", "")
                    col_idx = _excel_col_index(ref) if ref else fallback_col
                    fallback_col = col_idx + 1
                    cell_type = cell.attrib.get("t", "")
                    val = ""
                    if cell_type == "inlineStr":
                        val = "".join(t.text or "" for t in cell.iter() if _xml_local_name(t.tag) == "t")
                    else:
                        v_node = next((c for c in cell if _xml_local_name(c.tag) == "v"), None)
                        raw_val = v_node.text if v_node is not None and v_node.text is not None else ""
                        if cell_type == "s" and raw_val.isdigit():
                            idx = int(raw_val)
                            val = shared_strings[idx] if 0 <= idx < len(shared_strings) else raw_val
                        else:
                            val = raw_val
                    val = val.strip()
                    if val:
                        row_cells[col_idx] = val
                        max_col = max(max_col, col_idx)
                if row_cells:
                    row_line = " | ".join(row_cells.get(i, "") for i in range(max_col + 1)).strip(" |")
                    if row_line:
                        rows_out.append(row_line)
            if rows_out:
                label = sheet_titles.get(sheet_file) or Path(sheet_file).stem
                sections.append(f"[시트: {label}]\n" + "\n".join(rows_out))
    return "\n\n".join(sections)


def _extract_pptx_text(path: Path) -> str:
    sections: list[str] = []
    with zipfile.ZipFile(path) as archive:
        slide_files = sorted(
            [n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)],
            key=_numeric_xml_sort_key,
        )
        for slide_idx, slide_file in enumerate(slide_files, start=1):
            root = _read_zip_xml(archive, slide_file)
            if root is None:
                continue
            lines = [
                "".join(t.text or "" for t in p.iter() if _xml_local_name(t.tag) == "t").strip()
                for p in root.iter()
                if _xml_local_name(p.tag) == "p"
            ]
            slide_body = "\n".join(line for line in lines if line)
            note_file = f"ppt/notesSlides/notesSlide{slide_idx}.xml"
            note_root = _read_zip_xml(archive, note_file)
            note_body = ""
            if note_root is not None:
                note_lines = [
                    "".join(t.text or "" for t in p.iter() if _xml_local_name(t.tag) == "t").strip()
                    for p in note_root.iter()
                    if _xml_local_name(p.tag) == "p"
                ]
                note_body = "\n".join(line for line in note_lines if line and not line.isdigit())
            block = f"[슬라이드 {slide_idx}]\n{slide_body}".strip()
            if note_body:
                block = f"{block}\n[발표자 노트] {note_body}"
            if slide_body or note_body:
                sections.append(block)
    return "\n\n".join(sections)


def _extract_odf_or_hwpx_text(path: Path, ext: str) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        if ext == ".hwpx":
            xml_files = sorted(
                [n for n in archive.namelist() if n.startswith("Contents/section") and n.endswith(".xml")],
                key=_numeric_xml_sort_key,
            )
        else:
            xml_files = ["content.xml"]
        for member in xml_files:
            root = _read_zip_xml(archive, member)
            if root is None:
                continue
            for node in root.iter():
                local = _xml_local_name(node.tag)
                if local in {"p", "h", "t"} and node.text and node.text.strip():
                    parts.append(node.text.strip())
                elif local in {"p", "h"}:
                    chunk = "".join(t.strip() for t in node.itertext() if t and t.strip())
                    if chunk:
                        parts.append(chunk)
    deduped = list(dict.fromkeys(parts))
    return "\n".join(deduped)


def _extract_pdf_text(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "pypdf 미설치 상태입니다 (Gemini 멀티모달 분석으로 대체 가능)."
    try:
        reader = PdfReader(str(path))
        pages: list[str] = []
        for idx, page in enumerate(reader.pages[:30], start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pages.append(f"[페이지 {idx}]\n{page_text}")
        combined = "\n\n".join(pages).strip()
        if not combined:
            return "", "텍스트 레이어가 없는 스캔/이미지 PDF입니다."
        return combined, ""
    except Exception as error:
        return "", f"PDF 텍스트 추출 실패 ({type(error).__name__})"


def _extract_legacy_binary_strings(raw: bytes) -> tuple[str, str]:
    utf16_chunks: list[str] = []
    try:
        decoded16 = raw.decode("utf-16-le", errors="ignore")
        utf16_chunks = [m.strip() for m in _UTF16_HANGUL_RE.findall(decoded16) if len(m.strip()) >= 6]
    except Exception:
        utf16_chunks = []
    ascii_chunks = [m.decode("ascii", errors="ignore").strip() for m in re.findall(rb"[A-Za-z0-9 .,:/()\-_%+#@?!]{8,}", raw)]
    combined = "\n".join(dict.fromkeys([*utf16_chunks[:120], *ascii_chunks[:80]])).strip()
    note = "구버전 바이너리 Office 형식(.doc/.xls/.ppt)에서 부분 텍스트만 추출했습니다. 정확한 참조는 .docx/.xlsx/.pptx 또는 PDF 변환을 권장합니다."
    return combined, note


def extract_document_text(path: Path, ext: str) -> tuple[str, str]:
    """@brief Extract readable text from plain text, CSV, Markdown, Word, Excel, PPT, HWPX, ODF, or PDF."""
    lowered = ext.lower()
    if lowered in PLAIN_TEXT_EXTS:
        text = _decode_text_bytes(path.read_bytes())
        if lowered in {".html", ".htm"}:
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
        return cleaned[:MAX_ATTACHMENT_TEXT_CHARS], ""

    if lowered == ".pdf":
        text, note = _extract_pdf_text(path)
        return text[:MAX_ATTACHMENT_TEXT_CHARS], note

    if lowered in {".docx", ".xlsx", ".xlsm", ".pptx", ".hwpx", ".odt", ".ods", ".odp"}:
        try:
            if lowered == ".docx":
                text = _extract_docx_text(path)
            elif lowered in {".xlsx", ".xlsm"}:
                text = _extract_xlsx_text(path)
            elif lowered == ".pptx":
                text = _extract_pptx_text(path)
            else:
                text = _extract_odf_or_hwpx_text(path, lowered)
            cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
            if not cleaned:
                return "", "문서에서 추출 가능한 본문 텍스트를 찾지 못했습니다."
            return cleaned[:MAX_ATTACHMENT_TEXT_CHARS], ""
        except (zipfile.BadZipFile, ET.ParseError, OSError) as error:
            return "", f"문서 구조 분석 실패 ({type(error).__name__}): 암호화되었거나 손상된 파일일 수 있습니다."

    if lowered in {".doc", ".xls", ".ppt"}:
        text, note = _extract_legacy_binary_strings(path.read_bytes()[:2_000_000])
        return text[:MAX_ATTACHMENT_TEXT_CHARS], note

    return "", "지원하지 않는 문서 형식입니다."


def _probe_media_metadata(path: Path, kind: str) -> dict[str, Any]:
    """@brief Probe width, height, and duration when ffprobe is available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return {}
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        data = json.loads(res.stdout)
        streams = data.get("streams") or []
        v_stream = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
        fmt = data.get("format") or {}
        out: dict[str, Any] = {}
        if v_stream:
            if v_stream.get("width"):
                out["width"] = int(v_stream["width"])
            if v_stream.get("height"):
                out["height"] = int(v_stream["height"])
        if kind == "video" and fmt.get("duration"):
            out["duration_seconds"] = round(float(fmt["duration"]), 2)
        return out
    except Exception:
        return {}


def _encode_image_for_vision(path: Path) -> dict[str, str] | None:
    """@brief Prepare a size-bounded JPEG or image payload for Vision AI inspection."""
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    raw_size = path.stat().st_size
    ext = path.suffix.lower()
    if ffmpeg and (raw_size > 1_800_000 or ext in {".bmp", ".gif"}):
        with tempfile.TemporaryDirectory() as tmpdir:
            resized = Path(tmpdir) / "preview.jpg"
            cmd = [ffmpeg, "-y", "-i", str(path), "-vf", "scale='min(1280,iw)':-2", "-frames:v", "1", "-q:v", "4", str(resized)]
            res = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
            if res.returncode == 0 and resized.is_file():
                return {"mime_type": "image/jpeg", "data_b64": base64.b64encode(resized.read_bytes()).decode("ascii")}
    if raw_size > 8_000_000:
        return None
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    return {"mime_type": mime_type, "data_b64": base64.b64encode(path.read_bytes()).decode("ascii")}


def analyze_item_attachments(attachments: list[dict[str, Any]], folder: Path) -> tuple[list[dict[str, Any]], bool]:
    """@brief Summarize uninspected image, video, and low-text PDF attachments with Vision AI."""
    if not isinstance(attachments, list) or not attachments:
        return [], False
    folder_resolved = folder.resolve()
    updated: list[dict[str, Any]] = []
    changed = False
    for entry in attachments:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        rel = str(item.get("relative_path") or "")
        file_path = (folder_resolved / rel).resolve() if rel else None
        if file_path is None or folder_resolved not in file_path.parents or not file_path.is_file():
            updated.append(item)
            continue
        kind = str(item.get("kind") or "")
        already_described = bool(str(item.get("visual_summary") or "").strip())
        if not already_described:
            try:
                if kind == "image":
                    part = _encode_image_for_vision(file_path)
                    if part:
                        desc = describe_media_with_ai(
                            [part],
                            "이 첨부 사진을 유튜브 쇼츠 기획 참고 자료로 분석해 주세요. (1) 화면에 보이는 핵심 피사체·구도·분위기, (2) 이미지 안에 적힌 글자/숫자/제품명/표 내용을 빠짐없이 한국어로 간결하게 정리해 주세요.",
                            stage="planning",
                        )
                        if desc.strip():
                            item["visual_summary"] = desc.strip()[:3000]
                            changed = True
                elif kind == "video":
                    dur = float((item.get("media_info") or {}).get("duration_seconds") or 4.0)
                    marks = [round(max(0.1, dur * 0.2), 2), round(max(0.2, dur * 0.65), 2)] if dur >= 1.5 else [0.1]
                    parts: list[dict[str, str]] = []
                    with tempfile.TemporaryDirectory() as tmpdir:
                        for idx, ts in enumerate(marks):
                            frame_path = Path(tmpdir) / f"frame_{idx}.jpg"
                            try:
                                extract_video_frame(file_path, frame_path, timestamp_seconds=ts)
                                part = _encode_image_for_vision(frame_path)
                                if part:
                                    parts.append(part)
                            except Exception:
                                continue
                    if parts:
                        desc = describe_media_with_ai(
                            parts,
                            f"첨부 동영상('{item.get('original_name', '')}')의 대표 프레임들입니다. 이 영상에 담긴 장면 내용, 분위기, 화면 속 텍스트/제품/행동을 한국어로 간결히 요약해 주세요.",
                            stage="planning",
                        )
                        if desc.strip():
                            item["visual_summary"] = desc.strip()[:3000]
                            changed = True
                elif kind == "document" and str(item.get("ext") or "").lower() == ".pdf":
                    existing_text = str(item.get("extracted_text") or "").strip()
                    if len(existing_text) < 80 and file_path.stat().st_size <= 12_000_000:
                        pdf_part = {"mime_type": "application/pdf", "data_b64": base64.b64encode(file_path.read_bytes()).decode("ascii")}
                        desc = describe_media_with_ai(
                            [pdf_part],
                            "첨부된 PDF 문서의 핵심 내용, 사실 정보, 수치, 표 데이터를 한국어로 빠짐없이 추출·요약해 주세요.",
                            stage="planning",
                        )
                        if desc.strip():
                            item["visual_summary"] = desc.strip()[:6000]
                            changed = True
            except Exception:
                pass
        updated.append(item)
    return updated, changed


def build_attachment_record(
    *,
    original_name: str,
    stored_name: str,
    relative_path: str,
    ext: str,
    kind: str,
    size_bytes: int,
    file_path: Path,
) -> dict[str, Any]:
    """@brief Build a normalized persisted attachment record after writing the file to disk."""
    extracted_text = ""
    extraction_note = ""
    media_info: dict[str, Any] = {}
    if kind == "document":
        extracted_text, extraction_note = extract_document_text(file_path, ext)
    elif kind in {"image", "video"}:
        media_info = _probe_media_metadata(file_path, kind)
    elif kind == "audio":
        media_info = _probe_media_metadata(file_path, kind)
    return {
        "id": str(uuid4()),
        "original_name": original_name[:200],
        "stored_name": stored_name,
        "relative_path": relative_path,
        "ext": ext.lower(),
        "kind": kind,
        "size_bytes": int(size_bytes),
        "included": True,
        "extracted_text": extracted_text,
        "extraction_note": extraction_note[:300],
        "visual_summary": "",
        "media_info": media_info,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def attachments_to_citable_sources(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """@brief Convert included attachment texts and visual summaries into citable planning source entries."""
    sources: list[dict[str, Any]] = []
    if not isinstance(attachments, list):
        return sources
    idx = 0
    for att in attachments:
        if not isinstance(att, dict) or att.get("included", True) is False:
            continue
        text_part = str(att.get("extracted_text") or "").strip()
        vis_part = str(att.get("visual_summary") or "").strip()
        chunks: list[str] = []
        if text_part:
            chunks.append(text_part)
        if vis_part:
            chunks.append(f"[시각·문서 AI 분석]\n{vis_part}")
        if not chunks:
            note = str(att.get("extraction_note") or "").strip()
            media_info = att.get("media_info") or {}
            meta_desc = f"파일명: {att.get('original_name', '')}, 종류: {att.get('kind', '')}"
            if media_info:
                meta_desc += f", 정보: {json.dumps(media_info, ensure_ascii=False)}"
            if note:
                meta_desc += f" ({note})"
            chunks.append(meta_desc)
        idx += 1
        sources.append(
            {
                "id": f"A{idx}",
                "title": f"[첨부파일] {att.get('original_name') or att.get('stored_name') or f'attachment-{idx}'}"[:300],
                "url": "",
                "domain": "로컬 첨부 자료",
                "published_at": "",
                "fetched_at": str(att.get("created_at") or ""),
                "provider": f"attachment:{att.get('kind') or 'file'}",
                "source_kind": "attachment",
                "attachment_id": str(att.get("id") or ""),
                "status": "fetched",
                "content": "\n\n".join(chunks)[:8_000],
            }
        )
    return sources


def summarize_attachments_for_planning(attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """@brief Return compact attachment metadata and excerpts for planning prompts."""
    summary: list[dict[str, Any]] = []
    if not isinstance(attachments, list):
        return summary
    idx = 0
    for att in attachments:
        if not isinstance(att, dict) or att.get("included", True) is False:
            continue
        idx += 1
        entry: dict[str, Any] = {
            "source_id": f"A{idx}",
            "attachment_id": str(att.get("id") or ""),
            "name": str(att.get("original_name") or ""),
            "kind": str(att.get("kind") or ""),
            "stored_filename": str(att.get("stored_name") or ""),
            "usable_as_visual_clip": att.get("kind") in {"image", "video"},
        }
        if att.get("media_info"):
            entry["media_info"] = att["media_info"]
        if att.get("visual_summary"):
            entry["visual_summary"] = str(att["visual_summary"])[:1500]
        if att.get("extracted_text"):
            entry["text_excerpt"] = str(att["extracted_text"])[:2000]
        elif att.get("extraction_note"):
            entry["note"] = str(att["extraction_note"])[:200]
        summary.append(entry)
    return summary
