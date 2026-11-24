"""Visual scene matching, editorial photo scoring, and AI prompt construction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_NEWS_SPORTS_KEYWORDS = (
    "뉴스",
    "속보",
    "시사",
    "경기",
    "축구",
    "야구",
    "농구",
    "프리미어",
    "챔피언스",
    "epl",
    "맨유",
    "맨체스터",
    "리버풀",
    "아스날",
    "토트넘",
    "레알",
    "바르셀로나",
    "인터뷰",
    "감독",
    "선수",
    "스코어",
    "하이라이트",
    "news",
    "match",
    "premier league",
    "interview",
    "manager",
    "football",
    "soccer",
)

_FAN_REACTION_TOKENS = (
    "팬 반응",
    "팬들 반응",
    "커뮤니티",
    "댓글",
    "베스트글",
    "베스트 댓글",
    "에펨",
    "fmkorea",
    "네티즌",
    "여론",
    "fan reaction",
    "comments",
)


def _is_news_or_sports_topic(item: dict[str, Any]) -> bool:
    """@brief Detect news, current events, or sports-match topics that benefit from real editorial photos."""
    haystack = " ".join(
        [
            str(item.get("topic") or ""),
            str(item.get("audience") or ""),
            str(item.get("tone") or ""),
            str(item.get("source_notes") or "")[:600],
            str((item.get("style_template_snapshot") or {}).get("category") or ""),
        ]
    ).lower()
    return any(tok in haystack for tok in _NEWS_SPORTS_KEYWORDS)


def _is_fan_reaction_scene(scene: dict[str, Any] | Any) -> bool:
    """@brief Detect whether a planned scene depicts fan or community comments."""
    if not isinstance(scene, dict):
        return False
    text = f"{scene.get('narration') or ''} {scene.get('visual_query') or ''}".lower()
    return any(tok in text for tok in _FAN_REACTION_TOKENS)


def _editorial_image_priority_score(cand: Any) -> int:
    """@brief Rank editorial candidate images higher when clean full-frame photos and lower when small thumbnails/watermarks."""
    if isinstance(cand, dict):
        url = str(cand.get("url") or "").lower()
        desc = str(cand.get("description") or "").lower()
    else:
        url = str(cand or "").lower()
        desc = ""
    score = 0
    for clean_host in ("reuters", "bbc", "aljazeera", "apnews", "hani.co.kr", "donga", "joongang", "theanalyst", "premierleague", "skysports", "espn"):
        if clean_host in url:
            score += 4
    if "1200" in url or "original" in url or "large" in url:
        score += 2
    for noisy_tok in ("cdn-cgi/image", "w=460", "w=320", "w=400", "getty", "watermark", "thumb", "f=avif"):
        if noisy_tok in url or noisy_tok in desc:
            score -= 5
    return score


def _build_english_ai_visual_prompt(
    scene: dict[str, Any],
    *,
    visual_bible: str = "",
    fallback_topic: str = "",
) -> str:
    """@brief Construct a purely English AI visual prompt combining scene choreography with cross-scene continuity."""
    raw_scene_prompt = str(scene.get("ai_visual_prompt") or scene.get("visual_query") or fallback_topic or "").strip()
    ascii_scene = re.sub(r"\s{2,}", " ", re.sub(r"[^\x20-\x7e]+", " ", raw_scene_prompt)).strip()
    ascii_bible = re.sub(r"\s{2,}", " ", re.sub(r"[^\x20-\x7e]+", " ", str(visual_bible or ""))).strip()
    parts: list[str] = []
    if ascii_bible:
        parts.append(f"Visual continuity: {ascii_bible[:320]}")
    if ascii_scene:
        parts.append(f"Scene framing: {ascii_scene[:320]}")
    else:
        parts.append("Vertical 9:16 editorial photography with cinematic lighting")
    parts.append("Strict exclusions: NO random unrelated league or K-League/FC Seoul crests, NO text overlays, keep kit colors uniform.")
    return ". ".join(parts)


def _collect_reference_product_images(folder: Path, attachments: list[dict[str, Any]] | None) -> list[Path]:
    """@brief Collect up to 2 included image attachment paths to anchor AI-generated scenes to the real product."""
    if not isinstance(attachments, list):
        return []
    clips_dir = (folder / "clips").resolve()
    refs: list[Path] = []
    for att in attachments:
        if (
            isinstance(att, dict)
            and att.get("included", True) is not False
            and att.get("kind") == "image"
            and att.get("stored_name")
        ):
            candidate = (clips_dir / Path(str(att["stored_name"])).name).resolve()
            if candidate.parent == clips_dir and candidate.is_file():
                refs.append(candidate)
                if len(refs) >= 2:
                    break
    return refs


def _keyword_overlap_score(scene: dict[str, Any], material: dict[str, Any]) -> int:
    """@brief Measure token overlap between scene visual query and candidate clip metadata."""
    q_tokens = {tok.lower() for tok in str(scene.get("visual_query") or "").replace(",", " ").split() if len(tok) >= 2 and tok.isascii()}
    if not q_tokens:
        return 0
    haystack = " ".join(str(material.get(field) or "") for field in ("tags", "title")).lower().replace(",", " ").replace("·", " ")
    m_tokens = {tok for tok in haystack.split() if len(tok) >= 2}
    return len(q_tokens & m_tokens)


def _score_material_for_scene(scene: dict[str, Any], material: dict[str, Any], target_dur: float) -> tuple[int, int]:
    """@brief Return (overlap_count, total_quality_score) for ranking a stock clip candidate."""
    overlap = _keyword_overlap_score(scene, material)
    w = int(material.get("width") or 0)
    h = int(material.get("height") or 0)
    portrait = 50 if h > w > 0 else 0
    hd = 20 if h >= 1080 else (10 if h >= 720 else 0)
    dur_ok = 10 if int(material.get("duration_seconds") or 0) >= target_dur else 0
    scene_match = 100 if str(material.get("scene_id") or "") == str(scene.get("id") or "") else 0
    return overlap, (overlap * 200) + scene_match + portrait + hd + dur_ok


def _auto_select_scene_clips(
    scenes: list[dict[str, Any]],
    materials: list[dict[str, Any]],
    target_duration: int | float,
) -> list[dict[str, Any]]:
    """@brief Automatically match the highest-scoring unused stock video clip to each planned scene."""
    downloadable = [m for m in materials if isinstance(m, dict) and m.get("provider_id") and m.get("download_url")]
    if not downloadable:
        return []
    bounded_scenes = [s for s in scenes if isinstance(s, dict)][:8] or [{"id": "scene-1"}]
    total = max(4.0, min(30.0, float(target_duration or 20)))
    default_dur = round(max(2.0, min(30.0, total / len(bounded_scenes))), 1)

    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for scene in bounded_scenes:
        sid = str(scene.get("id") or "")
        raw_dur = scene.get("duration_seconds")
        dur = round(max(1.0, min(30.0, float(raw_dur))), 1) if isinstance(raw_dur, (int, float)) and not isinstance(raw_dur, bool) else default_dur
        scene_matches = [m for m in downloadable if str(m.get("scene_id") or "") == sid]
        pool = (
            [m for m in scene_matches if str(m["provider_id"]) not in used_ids]
            or scene_matches
            or [m for m in downloadable if str(m["provider_id"]) not in used_ids]
            or downloadable
        )
        ranked = sorted(pool, key=lambda cand: _score_material_for_scene(scene, cand, dur)[1], reverse=True)
        chosen = ranked[0]
        pid = str(chosen["provider_id"])
        used_ids.add(pid)
        selected.append({"source_kind": "stock", "provider_id": pid, "duration_seconds": dur})
    return selected
