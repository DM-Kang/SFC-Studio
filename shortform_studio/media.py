"""Video generation, narration, captions, and deterministic local rendering."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .ai import request_json
from .config import get_secret
from .korean_numbers import korean_text_to_tts_speech, normalize_korean_display_text


def _prepare_tts_and_caption_texts(text: str) -> tuple[str, str]:
    """@brief Return (spoken_text_for_tts, display_text_for_captions) with context-aware Korean numerals."""
    raw = str(text or "").strip()
    if _contains_hangul(raw):
        display_text = normalize_korean_display_text(raw)
        spoken_text = korean_text_to_tts_speech(display_text)
        return spoken_text, display_text
    return raw, raw

RUNWAY_BASE_URL = "https://api.dev.runwayml.com/v1"
RUNWAY_API_VERSION = "2024-11-06"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
MAX_VIDEO_BYTES = 1_000_000_000
_FONT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9가-힣 _-]{1,64}$")
_ASS_COLOR_PATTERN = re.compile(r"^&H[0-9A-Fa-f]{8}$")
_SRT_TIME_PATTERN = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})$")

KOREAN_SHORTS_FONT_FAMILIES = (
    "Pretendard",
    "Black Han Sans",
    "Do Hyeon",
    "Jua",
    "Gothic A1",
    "Sunflower",
    "Noto Sans CJK KR",
    "Noto Serif CJK KR",
)

SUBTITLE_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "clean_bottom": {
        "label": "깔끔한 모던 하단 자막 (Pretendard)",
        "font_name": "",
        "font_size": 58,
        "margin_v": 310,
        "outline": 5,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&H99000000",
        "border_style": 1,
        "banner_font_name": "Black Han Sans",
        "banner_font_size": 72,
        "banner_primary_colour": "&H0000E5FF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&H990B0E14",
        "banner_border_style": 1,
        "banner_outline": 7,
        "banner_margin_v": 165,
        "animation": "pop",
    },
    "shorts_punch": {
        "label": "바이럴 쇼츠 볼드 강조 자막",
        "font_name": "",
        "font_size": 66,
        "margin_v": 325,
        "outline": 6,
        "shadow": 3,
        "alignment": 2,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&H99000000",
        "border_style": 1,
        "banner_font_name": "Black Han Sans",
        "banner_font_size": 76,
        "banner_primary_colour": "&H0000E5FF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&HB00D1117",
        "banner_border_style": 1,
        "banner_outline": 8,
        "banner_margin_v": 165,
        "animation": "pop",
    },
    "bold_center": {
        "label": "임팩트 중앙 자막",
        "font_name": "Do Hyeon",
        "font_size": 70,
        "margin_v": 140,
        "outline": 7,
        "shadow": 3,
        "alignment": 5,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "border_style": 1,
        "banner_font_name": "Black Han Sans",
        "banner_font_size": 74,
        "banner_primary_colour": "&H0000E5FF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&H80000000",
        "banner_border_style": 1,
        "banner_outline": 7,
        "banner_margin_v": 165,
        "animation": "pop",
    },
    "yellow_pop": {
        "label": "노란색 포인트 자막 (꿀팁/리뷰)",
        "font_name": "Jua",
        "font_size": 64,
        "margin_v": 315,
        "outline": 6,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H0000E5FF",
        "outline_colour": "&H00000000",
        "back_colour": "&H80000000",
        "border_style": 1,
        "banner_font_name": "Do Hyeon",
        "banner_font_size": 70,
        "banner_primary_colour": "&H00FFFFFF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&HC815202E",
        "banner_border_style": 3,
        "banner_outline": 12,
        "banner_margin_v": 165,
        "animation": "pop",
    },
    "top_news": {
        "label": "상단 카드형 뉴스·정보 자막",
        "font_name": "Gothic A1",
        "font_size": 58,
        "margin_v": 300,
        "outline": 5,
        "shadow": 2,
        "alignment": 2,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&HCC101520",
        "border_style": 3,
        "banner_font_name": "Do Hyeon",
        "banner_font_size": 68,
        "banner_primary_colour": "&H0000E5FF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&HCC101520",
        "banner_border_style": 3,
        "banner_outline": 12,
        "banner_margin_v": 165,
        "animation": "fade",
    },
    "clean_pill": {
        "label": "감성 캡슐 카드형 자막 (Google Flow 스타일)",
        "font_name": "Pretendard",
        "font_size": 58,
        "margin_v": 290,
        "outline": 14,
        "shadow": 0,
        "alignment": 2,
        "primary_colour": "&H00202632",
        "outline_colour": "&H00FFFFFF",
        "back_colour": "&H00FFFFFF",
        "border_style": 3,
        "banner_font_name": "Pretendard",
        "banner_font_size": 64,
        "banner_primary_colour": "&H00FFFFFF",
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": "&H88000000",
        "banner_border_style": 1,
        "banner_outline": 5,
        "banner_margin_v": 165,
        "animation": "fade",
    },
}


def _contains_hangul(text: str) -> bool:
    """@brief Detect whether text contains Korean Hangul syllables or Jamo characters."""
    return bool(re.search(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", str(text or "")))


def default_subtitle_font() -> str:
    """@brief Choose a crisp Korean Shorts font installed in the runtime environment."""
    import sys

    if sys.platform == "win32":
        return "Malgun Gothic"
    if sys.platform == "darwin":
        return "Apple SD Gothic Neo"
    if Path("/usr/share/fonts/truetype/korean-shorts/Pretendard-Bold.otf").is_file():
        return "Pretendard"
    return "Noto Sans CJK KR"


def _resolve_banner_font(requested: str, fallback: str) -> str:
    """@brief Resolve banner font cleanly against installed Linux/container fonts."""
    import sys

    clean = str(requested or "").strip()
    if sys.platform.startswith("linux") and clean == "Black Han Sans" and not Path("/usr/share/fonts/truetype/korean-shorts/BlackHanSans-Regular.ttf").is_file():
        return fallback
    if sys.platform.startswith("linux") and clean in {"Do Hyeon", "Jua", "Gothic A1", "Sunflower", "Pretendard"}:
        font_files = {
            "Do Hyeon": "DoHyeon-Regular.ttf",
            "Jua": "Jua-Regular.ttf",
            "Gothic A1": "GothicA1-ExtraBold.ttf",
            "Sunflower": "Sunflower-Bold.ttf",
            "Pretendard": "Pretendard-Bold.otf",
        }
        if not Path(f"/usr/share/fonts/truetype/korean-shorts/{font_files[clean]}").is_file():
            return fallback
    return clean or fallback


def normalize_subtitle_style(style: dict[str, Any] | None = None) -> dict[str, Any]:
    """@brief Validate subtitle and headline banner parameters and merge with a named preset."""
    raw = style if isinstance(style, dict) else {}
    preset_id = str(raw.get("preset") or "clean_bottom").strip()
    if preset_id not in SUBTITLE_STYLE_PRESETS:
        raise ValueError("Unsupported subtitle preset.")
    merged = dict(SUBTITLE_STYLE_PRESETS[preset_id])
    merged["preset"] = preset_id

    font_name = str(raw.get("font_name") if raw.get("font_name") is not None else merged["font_name"]).strip()
    if not font_name:
        font_name = default_subtitle_font()
    if not _FONT_NAME_PATTERN.fullmatch(font_name):
        raise ValueError("Subtitle font name contains unsupported characters.")
    merged["font_name"] = _resolve_banner_font(font_name, default_subtitle_font())

    banner_font = str(raw.get("banner_font_name") if raw.get("banner_font_name") is not None else merged.get("banner_font_name") or "").strip()
    if banner_font and not _FONT_NAME_PATTERN.fullmatch(banner_font):
        raise ValueError("Banner font name contains unsupported characters.")
    merged["banner_font_name"] = _resolve_banner_font(banner_font, merged["font_name"])

    def _bounded_int(key: str, minimum: int, maximum: int) -> int:
        value = raw.get(key, merged[key])
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Subtitle {key} must be an integer between {minimum} and {maximum}.")
        return value

    merged["font_size"] = _bounded_int("font_size", 18, 110)
    merged["margin_v"] = _bounded_int("margin_v", 10, 700)
    merged["outline"] = _bounded_int("outline", 0, 14)
    merged["shadow"] = _bounded_int("shadow", 0, 8)
    merged["border_style"] = _bounded_int("border_style", 1, 3)
    if merged["border_style"] not in {1, 3}:
        merged["border_style"] = 1
    merged["banner_font_size"] = _bounded_int("banner_font_size", 32, 116)
    merged["banner_outline"] = _bounded_int("banner_outline", 0, 20)
    merged["banner_margin_v"] = _bounded_int("banner_margin_v", 40, 420)
    merged["banner_border_style"] = _bounded_int("banner_border_style", 1, 3)
    if merged["banner_border_style"] not in {1, 3}:
        merged["banner_border_style"] = 1

    alignment = raw.get("alignment", merged["alignment"])
    if alignment not in {2, 5, 8}:
        raise ValueError("Subtitle alignment must be bottom (2), center (5), or top (8).")
    merged["alignment"] = alignment

    anim = str(raw.get("animation") or merged.get("animation") or "pop").strip().lower()
    merged["animation"] = anim if anim in {"pop", "fade", "static"} else "pop"

    for color_key in ("primary_colour", "outline_colour", "back_colour", "banner_primary_colour", "banner_outline_colour", "banner_back_colour"):
        color_val = str(raw.get(color_key) or merged[color_key]).strip()
        if not _ASS_COLOR_PATTERN.fullmatch(color_val):
            raise ValueError(f"Subtitle {color_key} must use ASS &HAABBGGRR format.")
        merged[color_key] = color_val.upper()
    return merged


def _srt_timestamp_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    """@brief Convert parsed SRT timestamp groups to non-negative milliseconds."""
    h, m, s, ms = int(hours), int(minutes), int(seconds), int(millis)
    if m >= 60 or s >= 60:
        raise ValueError("Invalid SRT timestamp minutes or seconds.")
    return ((h * 3600) + (m * 60) + s) * 1000 + ms


def validate_and_normalize_srt(raw_srt: str) -> str:
    """@brief Parse, validate, and renumber SRT subtitle cues."""
    if not isinstance(raw_srt, str) or not raw_srt.strip():
        raise ValueError("Subtitle SRT content cannot be empty.")
    if len(raw_srt) > 200_000:
        raise ValueError("Subtitle SRT content exceeds the 200,000-character limit.")
    normalized_text = raw_srt.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n{2,}", normalized_text)
    if len(blocks) > 1000:
        raise ValueError("Subtitle SRT exceeds the 1,000-cue limit.")
    cues: list[str] = []
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n") if line.strip() != ""]
        if len(lines) < 2:
            raise ValueError("Each SRT cue must include a timestamp range and text.")
        time_line_index = 1 if lines[0].strip().isdigit() else 0
        if time_line_index >= len(lines):
            raise ValueError("Missing SRT timestamp line.")
        match = _SRT_TIME_PATTERN.fullmatch(lines[time_line_index].strip())
        if not match:
            raise ValueError(f"Invalid SRT timestamp line: {lines[time_line_index][:40]}")
        start_ms = _srt_timestamp_ms(*match.groups()[:4])
        end_ms = _srt_timestamp_ms(*match.groups()[4:])
        if end_ms <= start_ms:
            raise ValueError("Each SRT cue end time must be greater than its start time.")
        cue_lines = lines[time_line_index + 1 :]
        if not cue_lines:
            raise ValueError("Each SRT cue must contain subtitle text.")
        cues.append(
            "\n".join(
                [
                    str(len(cues) + 1),
                    f"{_srt_time(start_ms / 1000.0)} --> {_srt_time(end_ms / 1000.0)}",
                    *cue_lines,
                ]
            )
        )
    if not cues:
        raise ValueError("Subtitle SRT must contain at least one valid cue.")
    return "\n\n".join(cues) + "\n"


def build_subtitles_filter(captions_filename: str, style: dict[str, Any] | None = None) -> str:
    """@brief Build a safe FFmpeg subtitles filter expression supporting styled SRT or dynamic ASS."""
    safe_filename = captions_filename.replace("\\", "/").replace("'", "\\'").replace(":", "\\:")
    if captions_filename.lower().endswith(".ass"):
        return f"ass={safe_filename}"
    normalized = normalize_subtitle_style(style)
    force_style = (
        f"FontName={normalized['font_name']},"
        f"FontSize={normalized['font_size']},"
        f"Outline={normalized['outline']},"
        f"Shadow={normalized['shadow']},"
        f"Alignment={normalized['alignment']},"
        f"MarginV={normalized['margin_v']},"
        f"PrimaryColour={normalized['primary_colour']},"
        f"OutlineColour={normalized['outline_colour']}"
    )
    return f"subtitles={safe_filename}:force_style='{force_style}'"


def _ass_time(seconds: float) -> str:
    """@brief Format non-negative seconds as H:MM:SS.CC for ASS dialogue events."""
    cs = max(0, int(round(seconds * 100)))
    hours, rem = divmod(cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    whole_seconds, centis = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centis:02d}"


def _parse_srt_cues(raw_srt: str) -> list[tuple[float, float, str]]:
    """@brief Extract normalized (start_sec, end_sec, text) tuples from SRT text."""
    normalized_text = validate_and_normalize_srt(raw_srt)
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\n{2,}", normalized_text.strip()):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            continue
        match = _SRT_TIME_PATTERN.fullmatch(lines[1])
        if not match:
            continue
        start_s = _srt_timestamp_ms(*match.groups()[:4]) / 1000.0
        end_s = _srt_timestamp_ms(*match.groups()[4:]) / 1000.0
        text = " ".join(lines[2:]).strip()
        if text:
            cues.append((start_s, end_s, text))
    return cues


_BANNER_CLAUSE_ENDINGS = (
    "!",
    "?",
    ",",
    ":",
    "·",
    "땐",
    "때",
    "면",
    "요",
    "까",
    "죠",
    "지",
    "고",
    "서",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "로",
    "도",
    "만",
    "와",
    "과",
)
_BANNER_DEPENDENT_STARTS = {"땐", "때", "수", "것", "거", "법", "중", "분", "명", "개", "가지", "배", "등", "전", "후"}


def format_shorts_banner_text(title: str, hook: str = "", *, max_line_chars: int = 15, max_lines: int = 2) -> str:
    """@brief Wrap a Shorts headline cleanly into balanced Korean phrases without breaking emphasized compound nouns."""
    raw = str(title or "").strip() or str(hook or "").strip()
    if not raw:
        return ""
    if "\n" in raw:
        explicit = [re.sub(r"[\r{}]+", " ", part).strip() for part in raw.splitlines() if part.strip()]
        if explicit:
            return r"\N".join(explicit[:max_lines])
    cleaned = re.sub(r"[\r\n{}]+", " ", raw).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > max_line_chars * max_lines + 4:
        clause = re.split(r"[!?.]\s+", cleaned)[0].strip()
        if len(clause) >= 12:
            cleaned = clause
    words = [w for w in cleaned.split(" ") if w]
    if not words:
        return ""
    if len(cleaned) <= max_line_chars or len(words) == 1 or max_lines <= 1:
        return cleaned[: max_line_chars + 4]

    # Pick a balanced 2-line split that respects Korean clause/particle endings and keeps compound noun phrases intact.
    if max_lines == 2:
        best_pair: tuple[str, str] | None = None
        best_score = float("inf")
        for split_idx in range(1, len(words)):
            line1 = " ".join(words[:split_idx])
            line2 = " ".join(words[split_idx:])
            if len(line1) > max_line_chars + 2 or len(line2) > max_line_chars + 2:
                continue
            balance_penalty = abs(len(line1) - len(line2)) * 2.2
            overflow_penalty = max(0, len(line1) - max_line_chars) * 8 + max(0, len(line2) - max_line_chars) * 8
            orphan_penalty = 12 if (len(line2.replace(" ", "")) <= 3 or (len(line1.replace(" ", "")) <= 4 and len(line2) >= 12)) else 0
            dependent_penalty = 14 if words[split_idx].rstrip("!?.") in _BANNER_DEPENDENT_STARTS else (4.0 if len(words[split_idx]) == 1 else 0.0)
            last_w1 = words[split_idx - 1]
            # Prefer breaking right after punctuation, clause endings ("~땐", "~면"), or particles ("~은/는/가/을/를/에").
            if last_w1.endswith(_BANNER_CLAUSE_ENDINGS):
                phrase_bonus = -9.0
            else:
                # Avoid cutting two adjacent noun phrases like "초승달 | 메밀베개"
                phrase_bonus = 6.0
            score = balance_penalty + overflow_penalty + orphan_penalty + dependent_penalty + phrase_bonus
            if score < best_score:
                best_score = score
                best_pair = (line1, line2)
        if best_pair is not None:
            return rf"{best_pair[0]}\N{best_pair[1]}"

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_line_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                current = ""
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return r"\N".join(lines)


_CAPTION_HIGHLIGHT_RE = re.compile(
    r"(?<!\\)(?<![A-Za-z0-9])\d+(?:[.,:~×x\-]\d+)*(?:\s?(?:%|cm|mm|km|ml|mL|g|kg|mg|시간|분|초|원|만|억|배속|배|가지|종|개))?"
    r"|(?<![A-Za-z0-9가-힣\\])(?:100%|무료|특가|할인|국내산|국내 제조|국내 제작|천연|핵심|비결|주의|실수|반드시|지금 바로)(?![A-Za-z0-9가-힣])"
)


def _format_hook_banner_ass(clean_banner: str, banner_size: int, banner_primary: str) -> str:
    """@brief Style 2-line hook banners with a crisp sub-hook line and larger hero emphasis line."""
    if r"\N" not in clean_banner:
        return clean_banner
    line1, line2 = clean_banner.split(r"\N", 1)
    line1 = line1.strip()
    line2 = line2.strip()
    if not line1 or not line2:
        return clean_banner
    sub_fs = max(46, int(round(banner_size * 0.72)))
    hero_fs = max(banner_size, int(round(banner_size * 1.12)))
    hero_col = "&H0000E5FF" if banner_primary.upper() == "&H00FFFFFF" else banner_primary
    return rf"{{\fs{sub_fs}\c&H00FFFFFF&}}{line1}\N{{\fs{hero_fs}\c{hero_col}}}{line2}"


def _highlight_caption_keywords(text: str, primary_colour: str) -> str:
    """@brief Highlight numeric specs, ratios, and key commerce emphasis words in ASS captions."""
    if not text:
        return text
    upper_pri = primary_colour.upper()
    if "202632" in upper_pri:
        # Dark text on bright frosted-white pill uses crisp deep cobalt highlight.
        accent = "&H00D84E1D"
    elif "00E5FF" in upper_pri:
        accent = "&H0080FF9E"
    else:
        accent = "&H0000E5FF"

    def _wrap(match: re.Match[str]) -> str:
        token = match.group(0)
        return rf"{{\c{accent}&}}{token}{{\c{primary_colour}&}}"

    return _CAPTION_HIGHLIGHT_RE.sub(_wrap, text)


def srt_to_shorts_ass(
    raw_srt: str,
    output_path: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    style: dict[str, Any] | None = None,
    hook_title: str = "",
    source_attributions: list[dict[str, Any]] | None = None,
) -> Path:
    """@brief Build a styled 9:16 ASS caption file with pop-in text, headline banner, and optional per-scene source credits."""
    normalized = normalize_subtitle_style(style)
    cues = _parse_srt_cues(raw_srt)
    font_name = normalized["font_name"] or default_subtitle_font()
    font_size = max(50, int(normalized["font_size"]))
    margin_v = int(normalized["margin_v"])
    outline = max(3, int(normalized["outline"]))
    shadow = max(1, int(normalized["shadow"]))
    alignment = int(normalized["alignment"])
    primary = normalized["primary_colour"]
    outline_col = normalized["outline_colour"]
    back_col = normalized["back_colour"]
    border_style = int(normalized["border_style"])

    banner_font = normalized["banner_font_name"] or font_name
    banner_size = int(normalized["banner_font_size"])
    banner_primary = normalized["banner_primary_colour"]
    banner_outline_col = normalized["banner_outline_colour"]
    banner_back_col = normalized["banner_back_colour"]
    banner_border_style = int(normalized["banner_border_style"])
    banner_outline = int(normalized["banner_outline"])
    banner_margin_v = int(normalized["banner_margin_v"])
    anim_mode = normalized.get("animation", "pop")

    total_end = max((end for _, end, _ in cues), default=15.0)
    events: list[str] = []
    clean_banner = re.sub(r"[\r\n{}]+", " ", str(hook_title or "")).strip()
    if clean_banner and r"\N" not in clean_banner:
        clean_banner = format_shorts_banner_text(clean_banner)
    if clean_banner:
        banner_ass = _format_hook_banner_ass(clean_banner, banner_size, banner_primary)
        events.append(f"Dialogue: 1,0:00:00.00,{_ass_time(total_end)},HookBanner,,0,0,0,,{banner_ass}")
    for start_s, end_s, cue_text in cues:
        safe_text = cue_text.replace("{", "(").replace("}", ")").replace("\n", r"\N")
        safe_text = _highlight_caption_keywords(safe_text, primary)
        if anim_mode == "pop":
            anim = r"{\fad(30,30)\fscx108\fscy108\t(0,85,\fscx100\fscy100)}"
        elif anim_mode == "fade":
            anim = r"{\fad(60,60)}"
        else:
            anim = ""
        events.append(f"Dialogue: 0,{_ass_time(start_s)},{_ass_time(end_s)},Default,,0,0,0,,{anim}{safe_text}")

    extra_styles: list[str] = []
    if source_attributions:
        extra_styles.append(
            f"Style: SourceCredit,{font_name},26,&H18FFFFFF,&H000000FF,&H00000000,&H60000000,-1,0,0,0,100,100,0,0,1,3,1,3,56,56,210,1"
        )
        for attr in source_attributions:
            if not isinstance(attr, dict):
                continue
            label = re.sub(r"[\r\n{}]+", " ", str(attr.get("label") or "")).strip()
            if not label:
                continue
            try:
                st = max(0.0, float(attr.get("start") or 0.0))
                en = max(st + 0.3, float(attr.get("end") or (st + 3.0)))
            except (TypeError, ValueError):
                continue
            credit_line = label if label.startswith("출처") else f"출처: {label}"
            events.append(f"Dialogue: 2,{_ass_time(st)},{_ass_time(en)},SourceCredit,,0,0,0,,{credit_line}")

    ass_doc = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font_name},{font_size},{primary},&H000000FF,{outline_col},{back_col},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},64,64,{margin_v},1",
            f"Style: HookBanner,{banner_font},{banner_size},{banner_primary},&H000000FF,{banner_outline_col},{banner_back_col},-1,0,0,0,100,100,0,0,{banner_border_style},{banner_outline},3,8,56,56,{banner_margin_v},1",
            *extra_styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ass_doc, encoding="utf-8")
    return output_path


def _wrap_card_comment(text: str, max_chars: int = 24, max_lines: int = 2) -> str:
    """@brief Wrap Korean fan comment text into at most max_lines readable lines for 9:16 reaction cards."""
    cleaned = re.sub(r"[\r\n{}]+", " ", str(text or "")).strip()
    if not cleaned:
        return ""
    words = cleaned.split()
    lines: list[str] = []
    current = ""
    for word in words:
        cand = f"{current} {word}".strip() if current else word
        if len(cand) <= max_chars or not current:
            current = cand
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines - 1:
                break
    remaining = words[sum(len(ln.split()) for ln in lines):]
    if remaining and len(lines) < max_lines:
        tail = " ".join(remaining)
        if len(tail) > max_chars + 4:
            tail = tail[: max_chars + 1].rstrip() + "..."
        lines.append(tail)
    elif current and len(lines) < max_lines and current not in lines:
        lines.append(current[: max_chars + 4])
    return r"\N".join(lines[:max_lines])


def _build_fan_reaction_card_ass(
    *,
    title: str,
    badge: str,
    comments: list[dict[str, Any]],
    source_footer: str = "",
    width: int = 1080,
    height: int = 1920,
    font_name: str | None = None,
) -> str:
    """@brief Build a 9:16 ASS layout rendering 3 community comment cards inside the safe center frame."""
    resolved_font = font_name or default_subtitle_font()
    safe_badge = re.sub(r"[\r\n{}]+", " ", str(badge or "커뮤니티 팬 반응")).strip()[:40]
    safe_title = re.sub(r"[\r\n{}]+", " ", str(title or "경기 후 팬 반응")).strip()[:52]
    safe_footer = re.sub(r"[\r\n{}]+", " ", str(source_footer or "검색 기반 팬 반응 요약")).strip()[:64]

    def _rect(x1: int, y1: int, x2: int, y2: int, r: int = 24) -> str:
        k = int(round(r * 0.55))
        return (
            f"m {x1 + r} {y1} "
            f"l {x2 - r} {y1} b {x2 - r + k} {y1} {x2} {y1 + r - k} {x2} {y1 + r} "
            f"l {x2} {y2 - r} b {x2} {y2 - r + k} {x2 - r + k} {y2} {x2 - r} {y2} "
            f"l {x1 + r} {y2} b {x1 + r - k} {y2} {x1} {y2 - r + k} {x1} {y2 - r} "
            f"l {x1} {y1 + r} b {x1} {y1 + r - k} {x1 + r - k} {y1} {x1 + r} {y1}"
        )

    events: list[str] = [
        # Soft radial backdrop card container inside safe center zone (y=410..1360)
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H291C14&\\1a&H10&\\bord0\\shad0\\p1}}{_rect(76, 405, 1004, 1345, 34)}{{\\p0}}",
        f"Dialogue: 1,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H523523&\\1a&H00&\\bord0\\shad0\\p1}}{_rect(108, 434, 460, 490, 24)}{{\\p0}}",
        f"Dialogue: 2,0:00:00.00,0:00:05.00,CardBadge,,0,0,0,,{{\\an4\\pos(130,462)}}{safe_badge}",
        f"Dialogue: 2,0:00:00.00,0:00:05.00,CardTitle,,0,0,0,,{{\\an7\\pos(108,508)}}{safe_title}",
    ]

    normalized_comments = [c for c in comments if isinstance(c, dict) and str(c.get("text") or "").strip()][:3]
    y_cursor = 588
    card_h = 206
    card_gap = 22
    for idx, cmt in enumerate(normalized_comments, 1):
        author = re.sub(r"[\r\n{}]+", " ", str(cmt.get("author") or f"팬 {idx}")).strip()[:20]
        likes = re.sub(r"[\r\n{}]+", " ", str(cmt.get("likes") or "추천")).strip()[:16]
        body = _wrap_card_comment(str(cmt.get("text") or ""), max_chars=27, max_lines=2)
        y2 = y_cursor + card_h
        events.extend(
            [
                f"Dialogue: 1,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H3B2A1E&\\1a&H00&\\bord2\\3c&H694B33&\\shad0\\p1}}{_rect(104, y_cursor, 976, y2, 24)}{{\\p0}}",
                f"Dialogue: 2,0:00:00.00,0:00:05.00,CardAuthor,,0,0,0,,{{\\an7\\pos(134,{y_cursor + 24})}}{author}",
                f"Dialogue: 2,0:00:00.00,0:00:05.00,CardLikes,,0,0,0,,{{\\an9\\pos(946,{y_cursor + 24})}}★ {likes}",
                f"Dialogue: 2,0:00:00.00,0:00:05.00,CardBody,,0,0,0,,{{\\an7\\pos(134,{y_cursor + 74})}}{body}",
            ]
        )
        y_cursor = y2 + card_gap

    events.append(f"Dialogue: 2,0:00:00.00,0:00:05.00,CardFooter,,0,0,0,,{{\\an8\\pos({width // 2},1292)}}{safe_footer}")

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Shape,{resolved_font},20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: CardBadge,{resolved_font},28,&H00FFDB93,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1",
            f"Style: CardTitle,{resolved_font},42,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: CardAuthor,{resolved_font},28,&H00FFD59E,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: CardLikes,{resolved_font},26,&H0047C9FF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,9,0,0,0,1",
            f"Style: CardBody,{resolved_font},38,&H00F8FAFC,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: CardFooter,{resolved_font},24,&H00B8A394,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,0,0,0,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


def render_fan_reaction_card_image(
    output_path: Path,
    *,
    title: str,
    comments: list[dict[str, Any]],
    badge: str = "커뮤니티 · 팬 반응 TOP 3",
    source_footer: str = "검색 기반 팬 반응 요약",
    width: int = 1080,
    height: int = 1920,
) -> Path | None:
    """@brief Render a clean 9:16 PNG card showing up to 3 summarized fan comments via FFmpeg + libass."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not comments:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.with_suffix(".ass")
    ass_doc = _build_fan_reaction_card_ass(
        title=title,
        badge=badge,
        comments=comments,
        source_footer=source_footer,
        width=width,
        height=height,
    )
    ass_path.write_text(ass_doc, encoding="utf-8")
    command = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x0E131E:s={width}x{height}:d=0.2",
        "-vf",
        build_subtitles_filter(ass_path.name),
        "-frames:v",
        "1",
        output_path.name,
    ]
    try:
        res = subprocess.run(command, cwd=output_path.parent, capture_output=True, text=True, timeout=30, check=False)
        if res.returncode != 0 or not output_path.is_file():
            return None
        return output_path
    finally:
        ass_path.unlink(missing_ok=True)


def runway_generate_video(prompt: str, output_dir: Path, duration: int = 10) -> dict[str, Any]:
    """@brief Generate and download one vertical video with Runway."""
    api_key = get_secret("RUNWAY_API_KEY")
    if not api_key:
        raise RuntimeError("RUNWAY_API_KEY is not configured.")
    if not 4 <= duration <= 30:
        raise ValueError("Video duration must be between 4 and 30 seconds.")
    model = get_secret("RUNWAY_MODEL", "seedance2_5")
    task = request_json(
        f"{RUNWAY_BASE_URL}/text_to_video",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Runway-Version": RUNWAY_API_VERSION},
        body={"model": model, "promptText": prompt[:1000], "ratio": "720:1280", "duration": duration},
        timeout=90,
    )
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Runway did not return a generation task ID.")
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = request_json(
            f"{RUNWAY_BASE_URL}/tasks/{urllib.parse.quote(task_id, safe='')}",
            method="GET",
            headers={"Authorization": f"Bearer {api_key}", "X-Runway-Version": RUNWAY_API_VERSION},
            timeout=45,
        )
        state = status.get("status")
        if state == "SUCCEEDED":
            outputs = status.get("output")
            if not isinstance(outputs, list) or not outputs or not isinstance(outputs[0], str):
                raise RuntimeError("Runway completed without a downloadable video URL.")
            output_path = output_dir / "source.mp4"
            download_https_file(outputs[0], output_path)
            return {"path": str(output_path), "provider": "runway", "model": model, "task_id": task_id, "estimated_cost": task.get("estimatedCost")}
        if state in {"FAILED", "CANCELED", "CANCELLED"}:
            raise RuntimeError(f"Runway generation ended with status {state}.")
        time.sleep(5)
    raise TimeoutError("Runway generation did not finish within 30 minutes; inspect provider usage before retrying.")


def download_https_file(url: str, destination: Path) -> int:
    """@brief Download a bounded HTTPS media file to a project artifact path."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Generated media must be delivered through an HTTPS URL.")
    request = urllib.request.Request(url, headers={"User-Agent": "ShortformContentStudio/0.1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.download.tmp")
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response, temporary_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise ValueError("Generated media exceeds the 1 GB local file limit.")
                output.write(chunk)
        if total == 0:
            raise RuntimeError("The provider returned an empty media file.")
        temporary_path.replace(destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return total


def _build_tts_audio_filter(speed: float = 1.35, *, trim_silence: bool = True) -> str:
    """@brief Build a pitch-preserving FFmpeg audio filter with dead-air trimming and atempo speedup."""
    filters: list[str] = []
    if trim_silence:
        filters.append("silenceremove=start_periods=1:start_duration=0.03:start_threshold=-42dB:stop_periods=-1:stop_duration=0.14:stop_threshold=-40dB")
    clamped = max(0.75, min(2.0, float(speed or 1.35)))
    if abs(clamped - 1.0) >= 0.02:
        filters.append(f"atempo={clamped:.3f}")
    return ",".join(filters)


def scale_srt_timestamps(raw_srt: str, speed_multiplier: float) -> str:
    """@brief Rescale every SRT cue timestamp by 1 / speed_multiplier while keeping positive durations."""
    factor = max(0.5, min(2.5, float(speed_multiplier or 1.0)))
    cues = _parse_srt_cues(raw_srt)
    lines: list[str] = []
    for idx, (start_s, end_s, text) in enumerate(cues, 1):
        new_start = start_s / factor
        new_end = max(new_start + 0.18, end_s / factor)
        lines.extend((str(idx), f"{_srt_time(new_start)} --> {_srt_time(new_end)}", text, ""))
    return "\n".join(lines)


def retime_video_file(source_path: Path, output_path: Path, speed_multiplier: float, *, cancel_check: Any = None) -> Path:
    """@brief Adjust MP4 video playback speed cleanly using setpts without audio."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to retime video.")
    factor = max(0.75, min(2.0, float(speed_multiplier or 1.0)))
    staging = output_path.with_name(f"{output_path.stem}.retime{output_path.suffix}")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        source_path.name,
        "-filter:v",
        f"setpts=PTS/{factor:.4f}",
        "-r",
        "30",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        staging.name,
    ]
    try:
        rc, detail = _run_ffmpeg(cmd, cwd=source_path.parent, cancel_check=cancel_check)
        if rc != 0 or not staging.is_file():
            raise RuntimeError(f"FFmpeg video retime failed: {detail}")
        staging.replace(output_path)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return output_path


def retime_audio_file(source_path: Path, output_path: Path, speed_multiplier: float, *, cancel_check: Any = None) -> Path:
    """@brief Adjust audio speed cleanly using pitch-preserving FFmpeg atempo."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to retime audio.")
    factor = max(0.75, min(2.0, float(speed_multiplier or 1.0)))
    staging = output_path.with_name(f"{output_path.stem}.retime{output_path.suffix}")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        source_path.name,
        "-af",
        f"atempo={factor:.4f}",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "3",
        staging.name,
    ]
    try:
        rc, detail = _run_ffmpeg(cmd, cwd=source_path.parent, cancel_check=cancel_check)
        if rc != 0 or not staging.is_file():
            raise RuntimeError(f"FFmpeg audio retime failed: {detail}")
        staging.replace(output_path)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return output_path


def generate_voiceover(
    text: str,
    output_dir: Path,
    *,
    tts_provider: str = "auto",
    voice: str = "",
    tts_speed: float = 1.35,
) -> dict[str, Any] | None:
    """@brief Create narration and timing metadata using ElevenLabs or OpenAI TTS."""
    provider = str(tts_provider or "auto").strip().lower()
    if provider == "none":
        return None
    if provider == "auto":
        if get_secret("ELEVENLABS_API_KEY") and get_secret("ELEVENLABS_VOICE_ID"):
            provider = "elevenlabs"
        elif get_secret("GEMINI_API_KEY"):
            provider = "gemini_tts"
        elif get_secret("OPENAI_API_KEY"):
            provider = "openai_tts"
        elif get_secret("ELEVENLABS_API_KEY") or get_secret("ELEVENLABS_VOICE_ID"):
            raise RuntimeError("Both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")
        else:
            return None

    if provider == "gemini_tts":
        return _generate_gemini_tts(text, output_dir, voice=voice, speed=tts_speed)
    if provider == "openai_tts":
        return _generate_openai_tts(text, output_dir, voice=voice, speed=tts_speed)
    if provider != "elevenlabs":
        raise ValueError("Unsupported TTS provider; choose auto, gemini_tts, elevenlabs, openai_tts, or none.")

    api_key = get_secret("ELEVENLABS_API_KEY")
    voice_id = voice.strip() or get_secret("ELEVENLABS_VOICE_ID")
    if not api_key and not voice_id:
        return None
    if not api_key or not voice_id:
        raise RuntimeError("Both ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required.")
    encoded_voice = urllib.parse.quote(voice_id, safe="")
    model_id = get_secret("ELEVENLABS_MODEL", "eleven_multilingual_v2") or "eleven_multilingual_v2"
    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{encoded_voice}/with-timestamps"
    spoken_text, display_text = _prepare_tts_and_caption_texts(text)
    voice_settings: dict[str, Any] = {
        "stability": 0.42,
        "similarity_boost": 0.78,
        "style": 0.35,
        "use_speaker_boost": True,
        "speed": 1.10,
    }
    result: dict[str, Any] | None = None
    for attempt_settings in (voice_settings, {k: v for k, v in voice_settings.items() if k != "speed"}):
        request = urllib.request.Request(
            url,
            data=json.dumps({"text": spoken_text, "model_id": model_id, "voice_settings": attempt_settings}).encode("utf-8"),
            headers={"Content-Type": "application/json", "xi-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code in {400, 422} and "speed" in attempt_settings:
                continue
            detail = ""
            try:
                err_body = json.loads(error.read(4096).decode("utf-8", errors="replace"))
                err_detail = err_body.get("detail") if isinstance(err_body, dict) else None
                if isinstance(err_detail, dict) and isinstance(err_detail.get("message"), str):
                    detail = f": {err_detail['message'][:300]}"
                elif isinstance(err_detail, str):
                    detail = f": {err_detail[:300]}"
            except Exception:
                detail = ""
            raise RuntimeError(f"ElevenLabs request failed with HTTP {error.code}{detail}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"ElevenLabs request failed: {type(error).__name__}.") from None
    assert result is not None
    encoded_audio = result.get("audio_base64")
    raw_alignment = result.get("alignment")
    norm_alignment = result.get("normalized_alignment")
    has_hangul = _contains_hangul(text)
    # ElevenLabs normalized_alignment romanizes Hangul into Latin phonetics ("annyeonghaseyo");
    # always prefer raw alignment (or source text timings) so Korean subtitles stay in Hangul.
    alignment = raw_alignment if (has_hangul and isinstance(raw_alignment, dict)) else (norm_alignment or raw_alignment)
    if not isinstance(encoded_audio, str) or not isinstance(alignment, dict):
        raise RuntimeError("ElevenLabs response is missing audio or character timing data.")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voiceover.mp3"
    audio_path.write_bytes(base64.b64decode(encoded_audio, validate=True))
    captions_path = output_dir / "captions.srt"
    aligned_chars = "".join(str(ch) for ch in (alignment.get("characters") or []) if isinstance(ch, str))
    if (has_hangul and not _contains_hangul(aligned_chars)) or (display_text != spoken_text):
        duration = probe_audio_duration(audio_path)
        if duration is None or duration <= 0:
            end_times = alignment.get("character_end_times_seconds") or []
            duration = float(end_times[-1]) if end_times and isinstance(end_times[-1], (int, float)) else 0.0
        if duration <= 0:
            from .planning import estimate_speaking_duration

            duration = max(2.0, estimate_speaking_duration(spoken_text)["estimated_seconds"])
        srt_text = text_to_fallback_srt(display_text, duration, max_chars=16 if has_hangul else 24)
    else:
        srt_text = alignment_to_srt(alignment, max_chars=16 if has_hangul else 24)
    captions_path.write_text(srt_text, encoding="utf-8")
    from .usage import record_tts_usage

    record_tts_usage("elevenlabs", model_id, char_count=len(spoken_text))
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "elevenlabs", "voice_id": voice_id}


def generate_scene_synced_voiceover(
    scenes: list[dict[str, Any]],
    fallback_script: str,
    output_dir: Path,
    *,
    tts_provider: str = "auto",
    voice: str = "",
    tts_speed: float = 1.35,
    synth_fn: Any = None,
) -> dict[str, Any] | None:
    """@brief Synthesize narration per scene so visual cuts and spoken cues stay 1:1 synced."""
    import inspect

    synthesize = synth_fn or generate_voiceover
    accepts_speed = "tts_speed" in inspect.signature(synthesize).parameters
    ffmpeg = shutil.which("ffmpeg")
    valid_scenes = [
        str(s.get("narration") or "").strip()
        for s in (scenes or [])
        if isinstance(s, dict) and str(s.get("narration") or "").strip()
    ]
    if _contains_hangul(fallback_script) and any(re.search(r"[\u4e00-\u9fff]", line) or not _contains_hangul(line) for line in valid_scenes):
        from .planning import split_script_for_scenes

        valid_scenes = split_script_for_scenes(fallback_script, max(2, len(valid_scenes) or 4))

    def _call_synth(line_text: str, target_dir: Path) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {"tts_provider": tts_provider, "voice": voice}
        if accepts_speed:
            kwargs["tts_speed"] = tts_speed
        return synthesize(line_text, target_dir, **kwargs)

    if len(valid_scenes) < 2 or not ffmpeg or str(tts_provider or "auto").strip().lower() == "none":
        return _call_synth(fallback_script, output_dir)

    parts_dir = output_dir / "tts_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_paths: list[Path] = []
    part_durations: list[float] = []
    merged_cues: list[tuple[float, float, str]] = []
    cursor = 0.0
    first_meta: dict[str, Any] | None = None

    for idx, line in enumerate(valid_scenes[:8]):
        scene_dir = parts_dir / f"scene_{idx}"
        res = _call_synth(line, scene_dir)
        if res is None:
            return None
        if first_meta is None:
            first_meta = res
        audio_file = Path(res["audio_path"])
        dur = probe_audio_duration(audio_file)
        if dur is None or dur <= 0:
            from .planning import estimate_speaking_duration

            dur = max(1.2, estimate_speaking_duration(line)["estimated_seconds"])
        cues = _parse_srt_cues(Path(res["captions_path"]).read_text(encoding="utf-8"))
        for s_off, e_off, cue_txt in cues:
            merged_cues.append((cursor + s_off, cursor + min(dur, max(s_off + 0.2, e_off)), cue_txt))
        part_paths.append(audio_file)
        part_durations.append(round(max(1.2, dur), 2))
        cursor += dur

    output_dir.mkdir(parents=True, exist_ok=True)
    final_audio = output_dir / "voiceover.mp3"
    staging_audio = output_dir / "voiceover.merging.mp3"
    concat_inputs = "".join(f"[{i}:a:0]" for i in range(len(part_paths)))
    cmd = [ffmpeg, "-y"]
    for p in part_paths:
        cmd.extend(["-i", str(p)])
    cmd.extend(["-filter_complex", f"{concat_inputs}concat=n={len(part_paths)}:v=0:a=1[aout]", "-map", "[aout]", "-c:a", "libmp3lame", "-q:a", "3", str(staging_audio)])
    rc, detail = _run_ffmpeg(cmd, cwd=output_dir)
    if rc != 0 or not staging_audio.is_file():
        staging_audio.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg voiceover concatenation failed: {detail}")
    staging_audio.replace(final_audio)

    srt_lines: list[str] = []
    for idx, (start_s, end_s, txt) in enumerate(merged_cues, 1):
        srt_lines.extend((str(idx), f"{_srt_time(start_s)} --> {_srt_time(end_s)}", txt, ""))
    captions_path = output_dir / "captions.srt"
    captions_path.write_text("\n".join(srt_lines), encoding="utf-8")
    return {
        "audio_path": str(final_audio),
        "captions_path": str(captions_path),
        "provider": (first_meta or {}).get("provider", "tts"),
        "voice_id": (first_meta or {}).get("voice_id", ""),
        "scene_durations": part_durations,
    }


def _generate_lyria_bgm(output_path: Path, dur: float, *, mood: str, ffmpeg: str) -> Path | None:
    """@brief Synthesize studio-quality instrumental BGM using Google Lyria 3 when GEMINI_API_KEY is available."""
    gemini_key = get_secret("GEMINI_API_KEY")
    if not gemini_key:
        return None
    if mood == "calm":
        music_prompt = (
            "Warm gentle acoustic piano and soft felt strings instrumental background music, "
            "cozy Korean lifestyle and bedroom wellness vibe, clean studio mix, NO vocals, NO singing, 88 BPM."
        )
    else:
        music_prompt = (
            "Bright modern acoustic pop groove with warm plucked marimba, crisp light beat, and uplifting piano chords, "
            "instrumental Korean short-form lifestyle commerce background music, NO vocals, NO singing, 122 BPM."
        )
    body = {
        "contents": [{"parts": [{"text": music_prompt}]}],
        "generationConfig": {"responseModalities": ["AUDIO"]},
    }
    audio_bytes: bytes | None = None
    used_lyria_model = "lyria-3-clip-preview"
    ext = ".mp3"
    for lyria_model in ("lyria-3-clip-preview", "lyria-3.5"):
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{lyria_model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=55) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for cand in payload.get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict) and isinstance(inline.get("data"), str) and inline["data"]:
                        raw = base64.b64decode(inline["data"])
                        if len(raw) > 8192:
                            audio_bytes = raw
                            used_lyria_model = lyria_model
                            mime = str(inline.get("mimeType") or "").lower()
                            ext = ".wav" if "wav" in mime else ".mp3"
                            break
                if audio_bytes:
                    break
            if audio_bytes:
                break
        except Exception:
            continue
    if not audio_bytes:
        return None
    from .usage import record_bgm_usage

    record_bgm_usage(used_lyria_model)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = output_path.with_name(f"{output_path.stem}.lyria_raw{ext}")
    staging = output_path.with_name(f"{output_path.stem}.lyria{output_path.suffix}")
    fade_out_start = max(0.5, dur - 1.2)
    try:
        raw_path.write_bytes(audio_bytes)
        cmd = [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-t",
            f"{dur:.2f}",
            "-i",
            raw_path.name,
            "-af",
            f"afade=t=in:st=0:d=0.45,afade=t=out:st={fade_out_start:.2f}:d=1.1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "160k",
            staging.name,
        ]
        rc, _detail = _run_ffmpeg(cmd, cwd=output_path.parent)
        if rc == 0 and staging.is_file():
            staging.replace(output_path)
            return output_path
    except Exception:
        pass
    finally:
        raw_path.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
    return None


def ensure_ambient_bgm(output_path: Path, duration_seconds: float, *, mood: str = "upbeat") -> Path | None:
    """@brief Synthesize a clean BGM bed via Google Lyria 3 AI music or deterministic FFmpeg chord synthesis."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dur = max(5.0, min(90.0, float(duration_seconds or 25.0)))
    lyria_track = _generate_lyria_bgm(output_path, dur, mood=mood, ffmpeg=ffmpeg)
    if lyria_track is not None:
        return lyria_track
    if mood != "calm":
        # Bright ~128 BPM C-G-Am-F rhythmic marimba/pop arpeggio + offbeat sparkle pulse for lively commerce/Shorts.
        eighth = "0.234375"
        beat = "0.46875"
        chord = "mod(floor(t/2),4)"
        step = "mod(floor(t/0.234375),4)"
        pluck = f"exp(-7.5*mod(t,{eighth}))"
        pulse = f"exp(-13*mod(t,{beat}))*0.18*sin(2*PI*82.41*t)"
        offbeat_accent = f"eq(mod(floor(t/{eighth}),2),1)*exp(-24*mod(t,{eighth}))*0.09*sin(2*PI*1760*t)"
        root_hz = f"(eq({chord},0)*261.63+eq({chord},1)*196.00+eq({chord},2)*220.00+eq({chord},3)*174.61)"
        arp_hz = (
            f"(eq({chord},0)*(eq({step},0)*523.25+eq({step},1)*659.25+eq({step},2)*783.99+eq({step},3)*1046.50)"
            f"+eq({chord},1)*(eq({step},0)*392.00+eq({step},1)*493.88+eq({step},2)*587.33+eq({step},3)*783.99)"
            f"+eq({chord},2)*(eq({step},0)*440.00+eq({step},1)*523.25+eq({step},2)*659.25+eq({step},3)*880.00)"
            f"+eq({chord},3)*(eq({step},0)*349.23+eq({step},1)*440.00+eq({step},2)*523.25+eq({step},3)*698.46))"
        )
        expr = (
            f"{pulse}+{offbeat_accent}"
            f"+0.24*sin(2*PI*{root_hz}*t)*(0.55+0.45*exp(-3.5*mod(t,{beat})))"
            f"+0.26*{pluck}*(0.75*sin(2*PI*{arp_hz}*t)+0.25*sin(4*PI*{arp_hz}*t))"
        )
        af_filter_base = "lowpass=f=4000"
    else:
        expr = "0.22*sin(2*PI*110.00*t)+0.18*sin(2*PI*164.81*t)+0.15*sin(2*PI*220.00*t)+0.10*sin(2*PI*329.63*t)*(0.6+0.4*sin(2*PI*0.25*t))"
        af_filter_base = "lowpass=f=1200"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_name(f"{output_path.stem}.synth{output_path.suffix}")
    fade_out_start = max(0.5, dur - 1.2)
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expr.replace(',', r'\,')}:s=44100:d={dur:.2f}",
        "-af",
        f"{af_filter_base},afade=t=in:st=0:d=0.45,afade=t=out:st={fade_out_start:.2f}:d=1.1",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(staging),
    ]
    try:
        rc, _detail = _run_ffmpeg(cmd, cwd=output_path.parent)
        if rc == 0 and staging.is_file():
            staging.replace(output_path)
            return output_path
    except Exception:
        pass
    staging.unlink(missing_ok=True)
    return None


def synthesize_sfx_and_mix_bgm(
    bgm_path: Path | None,
    scene_durations: list[float],
    output_path: Path,
) -> Path | None:
    """@brief Mix subtle opening pop and scene-transition swoop SFX into the BGM bed before sidechain ducking."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not bgm_path or not bgm_path.is_file():
        return bgm_path
    clean_durs = [max(0.5, float(d)) for d in scene_durations if isinstance(d, (int, float)) and d > 0]
    total_dur = max(5.0, min(120.0, sum(clean_durs) + 0.6 if clean_durs else 25.0))
    sfx_dir = output_path.parent / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    pop_wav = sfx_dir / "pop.wav"
    whoosh_wav = sfx_dir / "whoosh.wav"

    # Synthesize crisp opening pop chime (90ms) and gentle transition swoop (240ms) once per item dir.
    try:
        if not pop_wav.is_file():
            rc_p, _ = _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    r"aevalsrc=0.42*exp(-26*t)*(sin(2*PI*784*t)+0.55*sin(2*PI*1175*t)):s=44100:d=0.10",
                    str(pop_wav),
                ],
                cwd=sfx_dir,
            )
            if rc_p != 0:
                return bgm_path
        if not whoosh_wav.is_file():
            rc_w, _ = _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    r"aevalsrc=0.35*exp(-14*abs(t-0.11))*(sin(2*PI*(240+520*t)*t)+0.4*sin(2*PI*(520+900*t)*t)):s=44100:d=0.24",
                    "-af",
                    "lowpass=f=2200,afade=t=in:st=0:d=0.04,afade=t=out:st=0.14:d=0.10",
                    str(whoosh_wav),
                ],
                cwd=sfx_dir,
            )
            if rc_w != 0:
                return bgm_path
    except Exception:
        return bgm_path

    cues: list[tuple[int, str]] = [(40, pop_wav.name)]
    cursor = 0.0
    for idx, dur in enumerate(clean_durs[:-1]):
        cursor += dur
        if cursor >= 0.4 and cursor < total_dur - 0.35 and len(cues) < 8:
            cues.append((int(round(cursor * 1000)), whoosh_wav.name if idx % 2 == 0 else pop_wav.name))

    # Mix BGM + delayed SFX events while preserving base BGM gain (normalize=0).
    staging = output_path.with_name(f"{output_path.stem}.mix{output_path.suffix}")
    cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-t", f"{total_dur:.2f}", "-i", str(bgm_path.resolve())]
    filter_parts: list[str] = ["[0:a:0]volume=1.0[a0]"]
    mix_labels = ["[a0]"]
    for idx, (delay_ms, sfx_name) in enumerate(cues, 1):
        cmd.extend(["-i", str((sfx_dir / sfx_name).resolve())])
        vol = 0.72 if sfx_name == "pop.wav" else 0.58
        filter_parts.append(f"[{idx}:a:0]adelay={delay_ms}|{delay_ms},volume={vol:.2f}[s{idx}]")
        mix_labels.append(f"[s{idx}]")
    filter_parts.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:normalize=0[a_out]")
    cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[a_out]", "-c:a", "libmp3lame", "-b:a", "128k", str(staging)])
    try:
        rc, _detail = _run_ffmpeg(cmd, cwd=output_path.parent)
        if rc == 0 and staging.is_file():
            staging.replace(output_path)
            return output_path
    except Exception:
        pass
    staging.unlink(missing_ok=True)
    return bgm_path


def _extract_omni_video_bytes(payload: dict[str, Any]) -> bytes | None:
    """@brief Extract base64 MP4 bytes from a Gemini Interactions API response."""
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            for item in step.get("content") or []:
                if not isinstance(item, dict):
                    continue
                mime = str(item.get("mime_type") or item.get("mimeType") or "").lower()
                if (item.get("type") == "video" or mime.startswith("video/")) and isinstance(item.get("data"), str) and item["data"]:
                    raw = base64.b64decode(item["data"])
                    if len(raw) > 4096:
                        return raw
    for item in payload.get("outputs") or []:
        if isinstance(item, dict):
            mime = str(item.get("mime_type") or item.get("mimeType") or "").lower()
            if (item.get("type") == "video" or mime.startswith("video/")) and isinstance(item.get("data"), str) and item["data"]:
                raw = base64.b64decode(item["data"])
                if len(raw) > 4096:
                    return raw
    return None


def generate_ai_scene_video_omni(
    prompt: str,
    output_path: Path,
    *,
    reference_image_paths: list[Path] | None = None,
    duration_seconds: float = 4.0,
    timeout: int = 180,
) -> Path | None:
    """@brief Generate a native 9:16 realistic motion video clip via Gemini Omni 1.2 Flash Interactions API."""
    gemini_key = get_secret("GEMINI_API_KEY")
    if not gemini_key:
        return None
    input_blocks: list[dict[str, Any]] = []
    for ref_path in (reference_image_paths or [])[:2]:
        encoded = _encode_reference_image_part(Path(ref_path))
        if encoded and isinstance(encoded.get("inlineData"), dict):
            inl = encoded["inlineData"]
            input_blocks.append(
                {
                    "type": "image",
                    "mime_type": inl.get("mimeType") or "image/jpeg",
                    "data": inl["data"],
                }
            )
    clip_hint = max(3, min(8, int(round(float(duration_seconds or 4.0)))))
    if input_blocks:
        omni_text = (
            f"Create a vertical 9:16 realistic commercial/lifestyle short video clip (~{clip_hint} seconds, "
            f"continuous smooth natural motion, authentic warm home/studio lighting, NO text or subtitles on screen). "
            f"Strictly preserve the exact physical product appearance, shape, colors, pattern, and details from the "
            f"reference image(s). Scene: {str(prompt or '')[:420]}"
        )
    else:
        omni_text = (
            f"Create a vertical 9:16 realistic commercial/lifestyle short video clip (~{clip_hint} seconds, "
            f"continuous smooth natural motion, NO text or subtitles on screen): {str(prompt or '')[:450]}"
        )
    input_blocks.append({"type": "text", "text": omni_text})

    for omni_model in ("gemini-omni-1.2-flash", "gemini-omni-1.1-flash"):
        body = {
            "model": omni_model,
            "input": input_blocks if len(input_blocks) > 1 else omni_text,
        }
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            interaction_id = payload.get("id")
            status = str(payload.get("status") or "").lower()
            deadline = time.monotonic() + timeout
            while status in {"in_progress", "pending", "running"} and interaction_id and time.monotonic() < deadline:
                time.sleep(3)
                poll_req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/interactions/{urllib.parse.quote(str(interaction_id), safe='')}",
                    headers={"x-goog-api-key": gemini_key},
                    method="GET",
                )
                with urllib.request.urlopen(poll_req, timeout=45) as poll_resp:
                    payload = json.loads(poll_resp.read().decode("utf-8"))
                status = str(payload.get("status") or "").lower()
            video_bytes = _extract_omni_video_bytes(payload)
            if video_bytes:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                staging = output_path.with_name(f"{output_path.stem}.omni{output_path.suffix}")
                staging.write_bytes(video_bytes)
                staging.replace(output_path)
                probed = probe_video(output_path)
                actual_dur = float(probed.get("duration_seconds") or clip_hint)
                from .usage import record_omni_video_usage

                record_omni_video_usage(omni_model, payload, duration_seconds=actual_dur)
                return output_path
        except Exception:
            continue
    return None


def _encode_reference_image_part(path: Path) -> dict[str, Any] | None:
    """@brief Encode a local product reference photo as an inlineData multimodal part for Gemini image generation."""
    if not path.is_file():
        return None
    ext = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime_type = mime_map.get(ext)
    raw: bytes | None = None
    try:
        if mime_type and path.stat().st_size <= 2_500_000:
            raw = path.read_bytes()
        else:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    rc, _ = _run_ffmpeg(
                        [ffmpeg, "-y", "-i", str(path), "-vf", "scale=1024:-2:force_original_aspect_ratio=decrease", "-q:v", "4", str(tmp_path)],
                        cwd=path.parent,
                    )
                    if rc == 0 and tmp_path.is_file() and tmp_path.stat().st_size > 0:
                        raw = tmp_path.read_bytes()
                        mime_type = "image/jpeg"
                finally:
                    tmp_path.unlink(missing_ok=True)
    except Exception:
        return None
    if not raw or not mime_type:
        return None
    return {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(raw).decode("ascii")}}


def generate_ai_scene_image(
    prompt: str,
    output_path: Path,
    *,
    reference_image_paths: list[Path] | None = None,
) -> Path | None:
    """@brief Generate a vertical 9:16 keyframe image via Gemini (optionally conditioned on product reference photos) or OpenAI."""
    ref_parts: list[dict[str, Any]] = []
    for ref_path in (reference_image_paths or [])[:2]:
        part = _encode_reference_image_part(Path(ref_path))
        if part is not None:
            ref_parts.append(part)
    if ref_parts:
        clean_prompt = (
            f"Using the exact physical product shape, colors, fabric texture, embroidery, and design shown in the attached "
            f"reference photo(s), generate a vertical 9:16 realistic lifestyle/studio commercial photograph placing that exact "
            f"product naturally into the scene, no text overlays, no watermarks, crisp lighting: {str(prompt or '')[:420]}"
        )
    else:
        clean_prompt = (
            f"Vertical 9:16 realistic photography / editorial visual for short-form video, "
            f"no text overlays, no watermarks, crisp lighting: {str(prompt or '')[:450]}"
        )
    gemini_key = get_secret("GEMINI_API_KEY")
    if gemini_key:
        body = {
            "contents": [{"parts": [*ref_parts, {"text": clean_prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        for image_model in ("gemini-3-pro-image-preview", "gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{image_model}:generateContent"
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                for cand in payload.get("candidates", []):
                    for part in (cand.get("content") or {}).get("parts", []):
                        inline = part.get("inlineData") or part.get("inline_data")
                        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(base64.b64decode(inline["data"]))
                            from .usage import record_image_usage

                            record_image_usage(image_model, 1, payload)
                            return output_path
            except Exception:
                continue

    openai_key = get_secret("OPENAI_API_KEY")
    if openai_key:
        req = urllib.request.Request(
            "https://api.openai.com/v1/images/generations",
            data=json.dumps({"model": "gpt-image-1", "prompt": clean_prompt, "size": "1024x1536"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {openai_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data_list = payload.get("data") or []
            if data_list and isinstance(data_list[0], dict):
                b64 = data_list[0].get("b64_json")
                if isinstance(b64, str) and b64:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(base64.b64decode(b64))
                    from .usage import record_image_usage

                    record_image_usage("gpt-image-1", 1, payload)
                    return output_path
        except Exception:
            pass
    return None


NARRATION_VOICE_PRESETS: dict[str, dict[str, str]] = {
    "": {"label": "여성 · 밝고 또렷한 표준 (기본)", "gemini_voice": "Kore", "openai_voice": "alloy", "style_prompt": ""},
    "female_standard": {"label": "여성 · 밝고 또렷한 표준 (기본)", "gemini_voice": "Kore", "openai_voice": "alloy", "style_prompt": ""},
    "female_warm": {
        "label": "여성 · 따뜻하고 차분한 감성",
        "gemini_voice": "Aoede",
        "openai_voice": "nova",
        "style_prompt": "- Character: Warm, gentle young Korean adult woman\n- Style: Soft, soothing storytelling tone with calm pacing",
    },
    "female_energetic": {
        "label": "여성 · 활기찬 쇼호스트·리뷰",
        "gemini_voice": "Zephyr",
        "openai_voice": "shimmer",
        "style_prompt": "- Character: Lively, bright Korean female host\n- Style: Crisp, energetic, friendly emphasis",
    },
    "male_announcer": {
        "label": "남성 · 힘 있는 스포츠·뉴스 해설",
        "gemini_voice": "Fenrir",
        "openai_voice": "onyx",
        "style_prompt": "- Character: Charismatic Korean male sports caster / anchor\n- Style: Confident, punchy, articulate, dynamic broadcast energy",
    },
    "male_calm": {
        "label": "남성 · 중저음 신뢰감 나레이션",
        "gemini_voice": "Charon",
        "openai_voice": "echo",
        "style_prompt": "- Character: Calm, trustworthy Korean adult male narrator\n- Style: Deep warm baritone, steady documentary narration",
    },
    "male_young": {
        "label": "남성 · 친근하고 밝은 청년",
        "gemini_voice": "Puck",
        "openai_voice": "fable",
        "style_prompt": "- Character: Friendly, upbeat young Korean man\n- Style: Natural conversational rhythm, bright and engaging",
    },
    "child_bright": {
        "label": "아이 · 맑고 명랑한 어린이",
        "gemini_voice": "Leda",
        "openai_voice": "nova",
        "style_prompt": "- Character: Cheerful 8-year-old Korean child\n- Voice Profile: High-pitched, innocent, playful, bright youthful voice in native Seoul Korean",
    },
    "elder_grandfather": {
        "label": "어르신 · 푸근한 할아버지",
        "gemini_voice": "Charon",
        "openai_voice": "onyx",
        "style_prompt": "- Character: 75-year-old warm, gentle Korean grandfather\n- Voice Profile: Deep weathered elder male voice, comforting, nostalgic and wise cadence",
    },
    "elder_grandmother": {
        "label": "어르신 · 다정한 할머니",
        "gemini_voice": "Aoede",
        "openai_voice": "sage",
        "style_prompt": "- Character: 75-year-old affectionate Korean grandmother\n- Voice Profile: Gentle warm elder female voice, soft and caring cadence",
    },
}

_GEMINI_TTS_VOICES = {"Kore", "Aoede", "Charon", "Fenrir", "Puck", "Zephyr", "Leda", "Autonoe", "Erinome"}


def resolve_tts_voice_spec(voice: str, *, provider: str = "gemini_tts") -> tuple[str, str]:
    """@brief Map semantic persona IDs or raw voice names into (provider_voice_name, style_director_prompt)."""
    clean = str(voice or "").strip()
    if clean in NARRATION_VOICE_PRESETS:
        spec = NARRATION_VOICE_PRESETS[clean]
        target = spec["gemini_voice"] if provider == "gemini_tts" else spec["openai_voice"]
        return target, spec["style_prompt"]
    if provider == "gemini_tts":
        default_v = get_secret("GEMINI_TTS_VOICE", "Kore") or "Kore"
        picked = clean if clean in _GEMINI_TTS_VOICES else default_v
        return (picked if picked in _GEMINI_TTS_VOICES else "Kore"), ""
    default_oa = get_secret("OPENAI_TTS_VOICE", "alloy") or "alloy"
    return (clean or default_oa), ""


def _generate_gemini_tts(text: str, output_dir: Path, *, voice: str = "", speed: float = 1.35) -> dict[str, Any]:
    """@brief Synthesize native Korean/multilingual speech using Gemini TTS with persona direction, crisp pace, and word-aligned SRT."""
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for Gemini TTS.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to encode Gemini TTS audio.")
    chosen_voice, style_prompt = resolve_tts_voice_spec(voice, provider="gemini_tts")
    spoken_text, display_text = _prepare_tts_and_caption_texts(text)
    spoken = spoken_text[:3000]
    tts_input = f"# AUDIO PROFILE\n{style_prompt}\n#### TRANSCRIPT\n{spoken}" if style_prompt else spoken
    body = {
        "contents": [{"parts": [{"text": tts_input}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": chosen_voice}}},
        },
    }
    raw_bytes: bytes | None = None
    mime_type = ""
    used_tts_model = "gemini-3.8-flash-tts"
    tts_payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for tts_model in ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-pro-preview-tts"):
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{tts_model}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for cand in payload.get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                        raw_bytes = base64.b64decode(inline["data"])
                        mime_type = str(inline.get("mimeType") or "").lower()
                        used_tts_model = tts_model
                        tts_payload = payload
                        break
                if raw_bytes:
                    break
            if raw_bytes:
                break
        except Exception as err:
            last_error = err
            continue
    if not raw_bytes:
        raise RuntimeError(f"Gemini TTS request failed: {last_error or 'no audio returned'}")

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voiceover.mp3"
    pace_filter = _build_tts_audio_filter(speed, trim_silence=True)
    af_args = ["-af", pace_filter] if pace_filter else []
    if "wav" in mime_type or raw_bytes[:4] == b"RIFF":
        tmp_in = output_dir / "voiceover_raw.wav"
        tmp_in.write_bytes(raw_bytes)
        cmd = [ffmpeg, "-y", "-i", tmp_in.name, *af_args, "-c:a", "libmp3lame", "-q:a", "3", audio_path.name]
    else:
        rate_match = re.search(r"rate=(\d+)", mime_type)
        rate = rate_match.group(1) if rate_match else "24000"
        tmp_in = output_dir / "voiceover_raw.pcm"
        tmp_in.write_bytes(raw_bytes)
        cmd = [ffmpeg, "-y", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", tmp_in.name, *af_args, "-c:a", "libmp3lame", "-q:a", "3", audio_path.name]
    try:
        rc, detail = _run_ffmpeg(cmd, cwd=output_dir)
        if rc != 0 or not audio_path.is_file():
            raise RuntimeError(f"FFmpeg could not encode Gemini TTS audio: {detail}")
    finally:
        tmp_in.unlink(missing_ok=True)

    duration = probe_audio_duration(audio_path)
    if duration is None or duration <= 0:
        from .planning import estimate_speaking_duration

        duration = max(1.5, estimate_speaking_duration(spoken)["estimated_seconds"])
    captions_path = output_dir / "captions.srt"
    captions_path.write_text(
        text_to_fallback_srt(display_text, duration, max_chars=12 if _contains_hangul(display_text) else 22),
        encoding="utf-8",
    )
    from .usage import record_tts_usage

    record_tts_usage("gemini_tts", used_tts_model, payload=tts_payload, duration_seconds=float(duration), char_count=len(spoken))
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "gemini_tts", "voice_id": chosen_voice}


def _generate_openai_tts(text: str, output_dir: Path, *, voice: str = "", speed: float = 1.25) -> dict[str, Any]:
    """@brief Synthesize MP3 narration with OpenAI TTS and produce proportional SRT captions."""
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for OpenAI TTS.")
    chosen_voice, _ = resolve_tts_voice_spec(voice, provider="openai_tts")
    if chosen_voice not in {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"}:
        chosen_voice = "alloy"
    spoken_text, display_text = _prepare_tts_and_caption_texts(text)
    clamped_speed = max(0.75, min(2.0, float(speed or 1.25)))
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps({"model": "tts-1", "voice": chosen_voice, "input": spoken_text[:4096], "response_format": "mp3", "speed": clamped_speed}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            audio_bytes = response.read(50_000_000)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"OpenAI TTS request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"OpenAI TTS request failed: {type(error).__name__}.") from None
    if not audio_bytes:
        raise RuntimeError("OpenAI TTS returned empty audio.")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / "voiceover.mp3"
    audio_path.write_bytes(audio_bytes)
    duration = probe_audio_duration(audio_path)
    if duration is None or duration <= 0:
        from .planning import estimate_speaking_duration

        duration = max(2.0, estimate_speaking_duration(spoken_text)["estimated_seconds"])
    captions_path = output_dir / "captions.srt"
    captions_path.write_text(
        text_to_fallback_srt(display_text, duration, max_chars=12 if _contains_hangul(display_text) else 22),
        encoding="utf-8",
    )
    return {"audio_path": str(audio_path), "captions_path": str(captions_path), "provider": "openai_tts", "voice_id": chosen_voice}


def text_to_fallback_srt(text: str, total_seconds: float, max_chars: int = 26) -> str:
    """@brief Split spoken text proportionally across a known duration into SRT cues."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        raise ValueError("Cannot build SRT cues from empty text.")
    duration = max(0.5, float(total_seconds))
    raw_segments = [seg.strip() for seg in re.split(r"(?<=[.!?。？！,])\s+", cleaned) if seg.strip()]
    segments: list[str] = []
    for seg in raw_segments:
        if len(seg) <= max_chars:
            segments.append(seg)
            continue
        words = seg.split(" ")
        buf = ""
        for word in words:
            candidate = f"{buf} {word}".strip() if buf else word
            if len(candidate) > max_chars and buf:
                segments.append(buf)
                buf = word
            else:
                buf = candidate
        if buf:
            segments.append(buf)
    if not segments:
        segments = [cleaned]
    merged_segments: list[str] = []
    for seg in segments:
        if merged_segments and len(seg.replace(" ", "")) <= 4 and len(merged_segments[-1]) + len(seg) <= max_chars + 6:
            merged_segments[-1] = f"{merged_segments[-1]} {seg}".strip()
        else:
            merged_segments.append(seg)
    segments = merged_segments
    weights = [max(1, len(korean_text_to_tts_speech(seg))) if _contains_hangul(seg) else max(1, len(seg)) for seg in segments]
    total_weight = sum(weights)
    cursor = 0.0
    lines: list[str] = []
    for idx, (seg, weight) in enumerate(zip(segments, weights), 1):
        span = duration * (weight / total_weight)
        end = duration if idx == len(segments) else min(duration, cursor + max(0.25, span))
        lines.extend((str(idx), f"{_srt_time(cursor)} --> {_srt_time(max(end, cursor + 0.2))}", seg, ""))
        cursor = end
    return "\n".join(lines)


def probe_audio_duration(audio_path: Path) -> float | None:
    """@brief Read audio duration in seconds with ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not audio_path.is_file():
        return None
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    raw_stdout = getattr(result, "stdout", None)
    if not isinstance(raw_stdout, str) or not raw_stdout.strip():
        return None
    try:
        payload = json.loads(raw_stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        return duration if duration > 0 else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def alignment_to_srt(alignment: dict[str, Any], max_chars: int = 26) -> str:
    """@brief Convert character-level speech alignment into readable SRT cues respecting word boundaries."""
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list) or not (len(chars) == len(starts) == len(ends)):
        raise ValueError("Speech alignment arrays must have equal lengths.")
    cues: list[tuple[float, float, str]] = []
    buffer = ""
    cue_start: float | None = None
    cue_end = 0.0
    for index, char in enumerate(chars):
        if not isinstance(char, str) or not char:
            continue
        try:
            start = float(starts[index])
            end = float(ends[index])
        except (TypeError, ValueError):
            continue
        if start < 0 or end < start:
            continue
        if cue_start is None:
            cue_start = start
        buffer += char
        cue_end = end
        stripped_len = len(buffer.strip())
        is_punct = char in {".", "?", "!", "。", "？", "！", ",", "，", "\n"}
        is_space_break = char.isspace() and stripped_len >= max(8, max_chars - 3)
        is_hard_limit = stripped_len >= max_chars + 8
        if is_punct or is_space_break or is_hard_limit:
            clean = buffer.strip()
            if clean:
                if cues and len(clean.replace(" ", "")) <= 4 and len(cues[-1][2]) + len(clean) <= max_chars + 6:
                    prev_start, _prev_end, prev_text = cues[-1]
                    cues[-1] = (prev_start, max(cue_end, prev_start + 0.2), f"{prev_text} {clean}".strip())
                else:
                    cues.append((cue_start, max(cue_end, cue_start + 0.2), clean))
            buffer = ""
            cue_start = None
    if buffer.strip() and cue_start is not None:
        clean = buffer.strip()
        if cues and len(clean.replace(" ", "")) <= 4 and len(cues[-1][2]) + len(clean) <= max_chars + 6:
            prev_start, _prev_end, prev_text = cues[-1]
            cues[-1] = (prev_start, max(cue_end, prev_start + 0.2), f"{prev_text} {clean}".strip())
        else:
            cues.append((cue_start, max(cue_end, cue_start + 0.2), clean))
    if not cues:
        raise ValueError("Speech alignment did not contain any usable captions.")
    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, 1):
        lines.extend((str(index), f"{_srt_time(start)} --> {_srt_time(end)}", text, ""))
    return "\n".join(lines)


def _srt_time(seconds: float) -> str:
    """@brief Format non-negative seconds as an SRT timestamp."""
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _build_audio_mix_graph(
    voice_idx: int | None,
    bgm_idx: int | None,
    *,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
) -> tuple[list[str], str | None]:
    """@brief Build FFmpeg filter_complex audio nodes for narration, ducked BGM, and loudness normalization."""
    filters: list[str] = []
    vol = max(0.02, min(1.0, float(bgm_volume)))
    if voice_idx is not None and bgm_idx is not None:
        filters.append(f"[{bgm_idx}:a:0]volume={vol:.2f}[bgm_scaled]")
        if ducking:
            filters.append(f"[{voice_idx}:a:0]asplit=2[voice_sc][voice_mix]")
            filters.append("[bgm_scaled][voice_sc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=250[bgm_ducked]")
            filters.append("[voice_mix][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[a_mixed]")
        else:
            filters.append(f"[{voice_idx}:a:0][bgm_scaled]amix=inputs=2:duration=first:dropout_transition=2[a_mixed]")
        out_label = "[a_mixed]"
    elif voice_idx is not None:
        out_label = f"[{voice_idx}:a:0]"
    elif bgm_idx is not None:
        filters.append(f"[{bgm_idx}:a:0]volume={vol:.2f}[bgm_only]")
        out_label = "[bgm_only]"
    else:
        return [], None
    if loudnorm:
        filters.append(f"{out_label}loudnorm=I=-14:TP=-1.5:LRA=11[a_norm]")
        out_label = "[a_norm]"
    return filters, out_label


def _run_ffmpeg(
    command: list[str],
    *,
    cwd: Path,
    cancel_check: Any = None,
    timeout: int = 900,
) -> tuple[int, str]:
    """@brief Execute FFmpeg with non-blocking cancellation polling and bounded stderr capture."""
    if cancel_check is None:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (getattr(result, "stderr", "") or "")[-1200:].strip()
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile("w+t", encoding="utf-8", errors="replace") as err_log:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=err_log)
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    err_log.flush()
                    err_log.seek(0)
                    return code, err_log.read()[-1200:].strip()
                if cancel_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    raise RuntimeError("Operation cancelled by operator.")
                if time.monotonic() >= deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                    raise RuntimeError("FFmpeg timed out while rendering video.")
                time.sleep(0.35)
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            raise


def render_video(
    source_path: Path,
    output_path: Path,
    *,
    audio_path: Path | None = None,
    captions_path: Path | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
    cancel_check: Any = None,
) -> Path:
    """@brief Render an MP4 and optionally combine narration, BGM ducking, and burned-in captions."""
    ffmpeg = shutil.which("ffmpeg")
    staging_path = output_path.with_name(f"{output_path.stem}.rendering{output_path.suffix}")
    if not ffmpeg:
        if audio_path or captions_path or bgm_path or loudnorm:
            raise RuntimeError("FFmpeg is required to combine narration, BGM, and captions; install it and add ffmpeg.exe to PATH.")
        try:
            shutil.copy2(source_path, staging_path)
            staging_path.replace(output_path)
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
        return output_path
    command = [ffmpeg, "-y"]
    audio_duration: float | None = None
    if audio_path:
        # Looping the visual lets the full narration finish instead of cutting audio to one generated shot.
        audio_duration = probe_audio_duration(audio_path)
        command.extend(["-stream_loop", "-1", "-i", source_path.name, "-i", audio_path.name])
    else:
        command.extend(["-i", source_path.name])
    if bgm_path:
        command.extend(["-stream_loop", "-1", "-i", bgm_path.name])

    voice_idx = 1 if audio_path else None
    bgm_idx = (2 if audio_path else 1) if bgm_path else None
    target_duration = audio_duration
    if audio_path is None and bgm_path is not None:
        info = probe_video(source_path)
        raw_dur = info.get("duration_seconds") if isinstance(info, dict) else None
        if isinstance(raw_dur, (int, float)) and raw_dur > 0:
            target_duration = float(raw_dur)
    duration_args = ["-t", f"{target_duration + 0.10:.2f}"] if (target_duration is not None and target_duration > 0) else []
    use_complex = bool(bgm_path or (loudnorm and audio_path))
    if use_complex:
        filter_parts: list[str] = []
        v_label = "[0:v:0]"
        if captions_path:
            filter_parts.append(f"[0:v:0]{build_subtitles_filter(captions_path.name, subtitle_style)}[v_out]")
            v_label = "[v_out]"
        audio_parts, a_label = _build_audio_mix_graph(voice_idx, bgm_idx, bgm_volume=bgm_volume, ducking=ducking, loudnorm=loudnorm)
        filter_parts.extend(audio_parts)
        command.extend(["-filter_complex", ";".join(filter_parts), "-map", v_label])
        if a_label:
            command.extend(["-map", a_label, "-shortest", *duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
        else:
            command.extend([*duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    else:
        filters: list[str] = []
        if captions_path:
            filters.append(build_subtitles_filter(captions_path.name, subtitle_style))
        if filters:
            command.extend(["-vf", ",".join(filters)])
        if audio_path:
            command.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest", *duration_args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
        elif captions_path:
            command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"])
        else:
            command.extend(["-c", "copy"])
    command.extend([staging_path.name])
    try:
        returncode, detail = _run_ffmpeg(command, cwd=source_path.parent, cancel_check=cancel_check)
        if returncode != 0:
            raise RuntimeError(f"FFmpeg render failed with exit code {returncode}: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def _probe_image_size(image_path: Path) -> tuple[int, int] | None:
    """@brief Return natural (width, height) pixels for a still image when ffprobe is available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not image_path.is_file():
        return None
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(image_path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if res.returncode != 0 or not res.stdout:
            return None
        streams = json.loads(res.stdout).get("streams") or []
        if streams and isinstance(streams[0], dict):
            w = int(streams[0].get("width") or 0)
            h = int(streams[0].get("height") or 0)
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None


def _build_image_zoompan_expr(idx: int, frames: int, width: int, height: int, *, card_mode: bool) -> str:
    """@brief Build varied per-scene zoompan expressions (push-in, L->R pan, pull-out, R->L pan)."""
    span = max(1, frames - 1)
    if card_mode:
        # Preserve card margins: alternate centered push-in and gentle pull-out so product graphics never clip.
        if idx % 2 == 0:
            z_expr = "min(zoom+0.0005,1.05)"
        else:
            z_expr = f"max(1.0,1.05-0.05*on/{span})"
        return f"zoompan=z='{z_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=30"

    mode = idx % 4
    if mode == 0:
        return f"zoompan=z='min(zoom+0.0011,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=30"
    if mode == 1:
        return f"zoompan=z='1.08':x='if(eq(on,0),0,min(iw-iw/zoom,(iw-iw/zoom)*on/{span}))':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=30"
    if mode == 2:
        return f"zoompan=z='max(1.0,1.12-0.12*on/{span})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=30"
    return f"zoompan=z='1.08':x='if(eq(on,0),iw-iw/zoom,max(0,(iw-iw/zoom)*(1-on/{span})))':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=30"


def build_multiscene_ffmpeg_command(
    ffmpeg_bin: str,
    clips: list[dict[str, Any]],
    output_name: str,
    *,
    width: int = 1080,
    height: int = 1920,
    transition: str = "cut",
    transition_seconds: float = 0.35,
    audio_name: str | None = None,
    captions_name: str | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_name: str | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
) -> list[str]:
    """@brief Construct a deterministic FFmpeg filter_complex command to assemble multiple 9:16 scenes."""
    if not clips or len(clips) > 8:
        raise ValueError("Multi-scene assembly requires between 1 and 8 scene clips.")
    if transition not in {"cut", "fade"}:
        raise ValueError("Transition must be 'cut' or 'fade'.")
    command = [ffmpeg_bin, "-y"]
    filter_parts: list[str] = []
    durations: list[float] = []
    for idx, clip in enumerate(clips):
        dur = max(1.0, min(30.0, float(clip.get("duration_seconds") or 5.0)))
        durations.append(dur)
        rel_path = str(clip["relative_path"]).replace("\\", "/")
        if clip.get("media_type") == "image":
            # Single-frame input only: zoompan already expands 1 input frame into `d` output frames,
            # whereas `-loop 1` causes zoompan to multiply frames (e.g. 150 * 180) and hang FFmpeg.
            command.extend(["-i", rel_path])
            frames = max(30, round(dur * 30))
            card_mode = clip.get("aspect_fit") == "card"
            zp = _build_image_zoompan_expr(idx, frames, width, height, card_mode=card_mode)
            if card_mode:
                card_w = max(320, width - 72)
                card_h = max(480, height - 420)
                filter_parts.append(
                    f"[{idx}:v:0]setsar=1,split=2[bg{idx}][fg{idx}];"
                    f"[bg{idx}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},boxblur=24:4,eq=brightness=-0.14,format=yuv420p[bgb{idx}];"
                    f"[fg{idx}]scale={card_w}:{card_h}:force_original_aspect_ratio=decrease,format=yuva420p[fgs{idx}];"
                    f"[bgb{idx}][fgs{idx}]overlay=(W-w)/2:(H-h)/2,{zp},"
                    f"trim=duration={dur:.2f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
                )
            else:
                filter_parts.append(
                    f"[{idx}:v:0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},{zp},"
                    f"trim=duration={dur:.2f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{idx}]"
                )
        else:
            command.extend(["-stream_loop", "-1", "-t", f"{dur:.2f}", "-i", rel_path])
            filter_parts.append(
                f"[{idx}:v:0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},eq=contrast=1.04:saturation=1.08,setpts=PTS-STARTPTS,setsar=1,fps=30,format=yuv420p[v{idx}]"
            )

    if len(clips) == 1:
        video_label = "[v0]"
    elif transition == "fade":
        fade = max(0.1, min(1.0, float(transition_seconds)))
        current_label = "[v0]"
        timeline = durations[0]
        for idx in range(1, len(clips)):
            offset = max(0.1, timeline - fade)
            next_label = f"[vx{idx}]"
            filter_parts.append(f"{current_label}[v{idx}]xfade=transition=fade:duration={fade:.2f}:offset={offset:.2f}{next_label}")
            current_label = next_label
            timeline = offset + durations[idx]
        video_label = current_label
    else:
        inputs = "".join(f"[v{idx}]" for idx in range(len(clips)))
        filter_parts.append(f"{inputs}concat=n={len(clips)}:v=1:a=0[v_cat]")
        video_label = "[v_cat]"

    if captions_name:
        filter_parts.append(f"{video_label}{build_subtitles_filter(captions_name, subtitle_style)}[v_sub]")
        video_label = "[v_sub]"

    next_input = len(clips)
    voice_idx: int | None = None
    bgm_idx: int | None = None
    if audio_name:
        command.extend(["-i", audio_name])
        voice_idx = next_input
        next_input += 1
    if bgm_name:
        command.extend(["-stream_loop", "-1", "-i", bgm_name])
        bgm_idx = next_input

    audio_parts, audio_label = _build_audio_mix_graph(
        voice_idx,
        bgm_idx,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    filter_parts.extend(audio_parts)
    command.extend(["-filter_complex", ";".join(filter_parts), "-map", video_label])
    if audio_label:
        command.extend(["-map", audio_label, "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"])
    else:
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart"])
    command.append(output_name)
    return command


def assemble_multiscene_video(
    work_dir: Path,
    clips: list[dict[str, Any]],
    output_path: Path,
    *,
    transition: str = "cut",
    audio_path: Path | None = None,
    captions_path: Path | None = None,
    subtitle_style: dict[str, Any] | None = None,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
    cancel_check: Any = None,
) -> Path:
    """@brief Execute FFmpeg multi-scene assembly into a vertical 9:16 MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for multi-scene assembly; install it and add ffmpeg to PATH.")
    prepared_clips: list[dict[str, Any]] = []
    for clip in clips:
        entry = dict(clip)
        if entry.get("media_type") == "image" and not entry.get("aspect_fit"):
            dims = _probe_image_size(work_dir / str(entry.get("relative_path") or ""))
            if dims is not None:
                ratio = dims[0] / dims[1]
                # Auto-switch non-9:16 images (landscape, square, or tall promo detail sheets) to blurred-card layout.
                if ratio > 0.70 or ratio < 0.46:
                    entry["aspect_fit"] = "card"
        prepared_clips.append(entry)
    staging_path = output_path.with_name(f"{output_path.stem}.assembling{output_path.suffix}")
    command = build_multiscene_ffmpeg_command(
        ffmpeg,
        prepared_clips,
        staging_path.name,
        transition=transition,
        audio_name=audio_path.name if audio_path else None,
        captions_name=captions_path.name if captions_path else None,
        subtitle_style=subtitle_style,
        bgm_name=bgm_path.name if bgm_path else None,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    try:
        returncode, detail = _run_ffmpeg(command, cwd=work_dir, cancel_check=cancel_check)
        if returncode != 0:
            raise RuntimeError(f"FFmpeg multi-scene assembly failed with exit code {returncode}: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def extract_video_frame(video_path: Path, output_path: Path, timestamp_seconds: float = 0.5) -> Path:
    """@brief Extract one JPEG thumbnail frame from an MP4 using FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to extract a thumbnail frame from video.")
    if not video_path.is_file():
        raise FileNotFoundError("Source MP4 file was not found.")
    ts = max(0.0, float(timestamp_seconds))
    staging_path = output_path.with_name(f"{output_path.stem}.frame{output_path.suffix}")
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{ts:.2f}",
        "-i",
        video_path.name,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        staging_path.name,
    ]
    try:
        result = subprocess.run(command, cwd=video_path.parent, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0 or not staging_path.is_file():
            detail = result.stderr[-800:].strip()
            raise RuntimeError(f"FFmpeg thumbnail extraction failed: {detail}")
        staging_path.replace(output_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return output_path


def probe_video(video_path: Path) -> dict[str, Any]:
    """@brief Read media streams and dimensions with ffprobe when available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"available": False, "passed": False, "message": "ffprobe is not installed; media properties were not verified."}
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(video_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return {"available": True, "passed": False, "message": "ffprobe could not parse the video file."}
    raw_stdout = getattr(result, "stdout", None)
    if not isinstance(raw_stdout, str) or not raw_stdout.strip():
        return {"available": True, "passed": False, "message": "ffprobe returned empty metadata."}
    try:
        metadata = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return {"available": True, "passed": False, "message": "ffprobe returned malformed metadata."}
    streams = metadata.get("streams")
    format_info = metadata.get("format")
    if not isinstance(streams, list) or not isinstance(format_info, dict):
        return {"available": True, "passed": False, "message": "ffprobe returned incomplete media metadata."}
    video_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"), None)
    try:
        duration = float(format_info.get("duration") or 0)
        width = int(video_stream.get("width") or 0) if video_stream else 0
        height = int(video_stream.get("height") or 0) if video_stream else 0
    except (TypeError, ValueError):
        return {"available": True, "passed": False, "message": "ffprobe returned invalid media dimensions or duration."}
    checks = [bool(video_stream), height > width, 0 < duration <= 180, width >= 480, height >= 640]
    return {
        "available": True,
        "passed": all(checks),
        "has_video": bool(video_stream),
        "has_audio": bool(audio_stream),
        "width": width,
        "height": height,
        "duration_seconds": round(duration, 2),
        "message": "Vertical format, duration, and minimum dimensions checked; human review is still required.",
    }
