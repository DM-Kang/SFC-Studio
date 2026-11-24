"""Editorial news clipping and fan-reaction 9:16 PNG card renderers."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _wrap_card_comment(text: str, max_chars: int = 24, max_lines: int = 2) -> str:
    """@brief Wrap Korean card text into at most max_lines readable lines for 9:16 cards."""
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


def _rounded_rect_ass(x1: int, y1: int, x2: int, y2: int, r: int = 24) -> str:
    """@brief Generate an ASS vector drawing path for a rounded rectangle."""
    k = int(round(r * 0.55))
    return (
        f"m {x1 + r} {y1} "
        f"l {x2 - r} {y1} b {x2 - r + k} {y1} {x2} {y1 + r - k} {x2} {y1 + r} "
        f"l {x2} {y2 - r} b {x2} {y2 - r + k} {x2 - r + k} {y2} {x2 - r} {y2} "
        f"l {x1 + r} {y2} b {x1 + r - k} {y2} {x1} {y2 - r + k} {x1} {y2 - r} "
        f"l {x1} {y1 + r} b {x1} {y1 + r - k} {x1 + r - k} {y1} {x1 + r} {y1}"
    )


def _render_ass_to_png(output_path: Path, ass_doc: str, *, bg_hex: str, width: int, height: int) -> Path | None:
    """@brief Burn one ASS graphic script over a solid canvas into a single PNG frame via FFmpeg."""
    from ..media import build_subtitles_filter

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path = output_path.with_suffix(".ass")
    ass_path.write_text(ass_doc, encoding="utf-8")
    command = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={bg_hex}:s={width}x{height}:d=0.2",
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
    from ..media import default_subtitle_font

    resolved_font = font_name or default_subtitle_font()
    safe_badge = re.sub(r"[\r\n{}]+", " ", str(badge or "커뮤니티 팬 반응")).strip()[:40]
    safe_title = re.sub(r"[\r\n{}]+", " ", str(title or "경기 후 팬 반응")).strip()[:52]
    safe_footer = re.sub(r"[\r\n{}]+", " ", str(source_footer or "검색 기반 팬 반응 요약")).strip()[:64]

    events: list[str] = [
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H291C14&\\1a&H10&\\bord0\\shad0\\p1}}{_rounded_rect_ass(76, 405, 1004, 1345, 34)}{{\\p0}}",
        f"Dialogue: 1,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H523523&\\1a&H00&\\bord0\\shad0\\p1}}{_rounded_rect_ass(108, 434, 460, 490, 24)}{{\\p0}}",
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
                f"Dialogue: 1,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H3B2A1E&\\1a&H00&\\bord2\\3c&H694B33&\\shad0\\p1}}{_rounded_rect_ass(104, y_cursor, 976, y2, 24)}{{\\p0}}",
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
    if not comments:
        return None
    ass_doc = _build_fan_reaction_card_ass(
        title=title,
        badge=badge,
        comments=comments,
        source_footer=source_footer,
        width=width,
        height=height,
    )
    return _render_ass_to_png(output_path, ass_doc, bg_hex="0x0E131E", width=width, height=height)


def _build_headline_highlight_card_ass(
    *,
    source_domain: str,
    headline: str,
    highlight_quote: str,
    subtitle_hint: str = "",
    width: int = 1080,
    height: int = 1920,
    font_name: str | None = None,
) -> str:
    """@brief Build a 9:16 editorial news clipping card ASS layout with a yellow marker strip behind the key quote."""
    from ..media import default_subtitle_font

    resolved_font = font_name or default_subtitle_font()
    clean_domain = re.sub(r"[\r\n{}]+", " ", str(source_domain or "기사 요약")).strip()[:36]
    clean_headline = _wrap_card_comment(str(headline or "핵심 보도 내용"), max_chars=23, max_lines=2)
    clean_quote = _wrap_card_comment(str(highlight_quote or ""), max_chars=23, max_lines=3)
    clean_hint = re.sub(r"[\r\n{}]+", " ", str(subtitle_hint or "기사 원문 핵심 하이라이트")).strip()[:52]

    quote_lines = clean_quote.split(r"\N") if clean_quote else []
    hl_events: list[str] = []
    q_y = 756
    line_step = 62
    for idx_q, q_line in enumerate(quote_lines[:3]):
        ly = q_y + idx_q * line_step
        bar_w = min(944, max(360, 146 + int(len(q_line) * 32)))
        hl_events.append(
            f"Dialogue: 2,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H7DF0FE&\\1a&H00&\\bord0\\shad0\\p1}}{_rounded_rect_ass(128, ly, bar_w, ly + 48, 8)}{{\\p0}}"
        )
        hl_events.append(
            f"Dialogue: 3,0:00:00.00,0:00:05.00,NewsQuote,,0,0,0,,{{\\an7\\pos(138,{ly + 3})}}{q_line}"
        )

    card_bottom = max(990, q_y + max(1, len(quote_lines[:3])) * line_step + 92)
    events: list[str] = [
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H000000&\\1a&H90&\\bord0\\shad0\\p1}}{_rounded_rect_ass(92, 462, 1004, card_bottom + 15, 28)}{{\\p0}}",
        f"Dialogue: 1,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&HF6F9FC&\\1a&H00&\\bord2\\3c&HD8E0EB&\\shad0\\p1}}{_rounded_rect_ass(80, 448, 992, card_bottom, 28)}{{\\p0}}",
        f"Dialogue: 2,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H422618&\\1a&H00&\\bord0\\shad0\\p1}}{_rounded_rect_ass(124, 488, 520, 540, 18)}{{\\p0}}",
        f"Dialogue: 3,0:00:00.00,0:00:05.00,NewsBadge,,0,0,0,,{{\\an4\\pos(146,514)}}{clean_domain.upper()} · 기사 요약",
        f"Dialogue: 3,0:00:00.00,0:00:05.00,NewsHeadline,,0,0,0,,{{\\an7\\pos(126,568)}}{clean_headline}",
        f"Dialogue: 2,0:00:00.00,0:00:05.00,Shape,,0,0,0,,{{\\an7\\pos(0,0)\\1c&HD0D8E4&\\1a&H00&\\bord0\\shad0\\p1}}m 126 716 l 946 716 l 946 720 l 126 720{{\\p0}}",
        *hl_events,
        f"Dialogue: 3,0:00:00.00,0:00:05.00,NewsFooter,,0,0,0,,{{\\an8\\pos({width // 2},{card_bottom - 52})}}{clean_hint}",
    ]

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
            f"Style: NewsBadge,{resolved_font},26,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1",
            f"Style: NewsHeadline,{resolved_font},44,&H00261810,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: NewsQuote,{resolved_font},38,&H001F1711,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
            f"Style: NewsFooter,{resolved_font},24,&H007A685C,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,0,0,0,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


def render_headline_highlight_card_image(
    output_path: Path,
    *,
    source_domain: str,
    headline: str,
    highlight_quote: str,
    subtitle_hint: str = "기사 원문 핵심 하이라이트",
    width: int = 1080,
    height: int = 1920,
) -> Path | None:
    """@brief Render a clean 9:16 editorial news clipping card PNG with highlighted key text via FFmpeg."""
    if not str(highlight_quote or "").strip():
        return None
    ass_doc = _build_headline_highlight_card_ass(
        source_domain=source_domain,
        headline=headline,
        highlight_quote=highlight_quote,
        subtitle_hint=subtitle_hint,
        width=width,
        height=height,
    )
    return _render_ass_to_png(output_path, ass_doc, bg_hex="0x101724", width=width, height=height)
