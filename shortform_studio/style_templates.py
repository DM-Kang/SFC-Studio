"""Continuous discovery and style-template generation from 1M+ view Korean YouTube Shorts."""

from __future__ import annotations

import base64
import json
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .ai import ai_ready, selected_model
from .ai_providers import generate_multimodal_description
from .config import get_secret
from .media import KOREAN_SHORTS_FONT_FAMILIES, normalize_subtitle_style
from .shorts_learner import compute_pacing_metrics, fetch_youtube_shorts_data
from .store import Store

STYLE_TEMPLATE_SETTINGS_KEY = "style_templates_state"
DEFAULT_SYNC_INTERVAL_SECONDS = 6 * 3600  # 6 hours
MIN_VIRAL_VIEW_COUNT = 1_000_000

DEFAULT_DISCOVERY_QUERIES = [
    "#shorts 꿀팁",
    "#shorts 추천템",
    "#shorts 살림노하우",
    "#shorts 리뷰",
    "#shorts 요리레시피",
    "#shorts 건강상식",
    "#shorts 심리",
    "#shorts 직장인",
    "#shorts 가성비템",
    "#shorts 생활꿀팁",
]

_VIEW_COUNT_TEXT_RE = re.compile(r"조회수\s*([0-9]+(?:\.[0-9]+)?)\s*(억|만|천)?\s*회")
_REEL_BLOCK_RE = re.compile(
    r'"reelWatchEndpoint":\{"videoId":"([A-Za-z0-9_-]{11})".*?"accessibilityText":"([^"]{1,300})"',
    re.DOTALL,
)
_ASS_COLOR_RE = re.compile(r"^&H[0-9A-Fa-f]{8}$")

STYLE_TEMPLATE_VISION_PROMPT = f"""Analyze this viral Korean YouTube Shorts (1M+ views) visual frame, pacing metrics, and transcript.
Design a reusable visual & editing style template inspired by this video's look and rhythm.
Return ONLY one JSON object with these keys:
- template_name: concise Korean style name (8-22 chars, e.g. "생활꿀팁 볼드 노랑배너", "홈쇼핑 하이라이트 카드형")
- category: one of ("정보·꿀팁", "상품·리뷰", "스토리·감성", "랭킹·비교", "라이프·브이로그")
- description: 1 concise Korean sentence describing the font weight, color contrast, and cut rhythm
- banner_font_name: MUST be exactly one of {list(KOREAN_SHORTS_FONT_FAMILIES)}
- banner_font_size: integer 60..92
- banner_primary_colour: ASS &HAABBGGRR hex (note BBGGRR byte order! e.g. Yellow "&H0000E5FF", White "&H00FFFFFF", Mint "&H00C8FF78")
- banner_back_colour: ASS &HAABBGGRR hex (e.g. "&HCC101520")
- banner_border_style: integer 1 (clean thick outline+shadow) or 3 (solid/translucent pill backdrop box)
- banner_margin_v: integer 130..230
- use_hook_banner: boolean
- font_name: MUST be exactly one of {list(KOREAN_SHORTS_FONT_FAMILIES)}
- font_size: integer 54..78
- primary_colour: ASS &HAABBGGRR hex (e.g. White "&H00FFFFFF", Yellow "&H0000E5FF")
- outline_colour: ASS &HAABBGGRR hex (usually "&H00000000")
- back_colour: ASS &HAABBGGRR hex (e.g. "&H99000000" or "&HCC101520")
- border_style: integer 1 (clean bold outline) or 3 (card backdrop box)
- outline: integer 4..9
- shadow: integer 1..4
- alignment: integer 2 (lower third above Shorts UI) or 5 (center impact)
- margin_v: integer 260..420 when alignment=2, or 80..180 when alignment=5
- animation: one of ("pop", "fade", "static")
- transition: one of ("cut", "fade")
- bgm_mood: one of ("upbeat", "calm")
- recommended_scenes: integer 4..6
- hook_style: short Korean description of opening hook technique
- script_rules: array of 3 concise Korean writing/editing rules based on this 1M+ video
"""


def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", str(text or "")))


def parse_korean_view_count(label: str) -> int:
    """@brief Parse Korean accessibility text view counts like '조회수 368만회' or '조회수 1.2억회'."""
    match = _VIEW_COUNT_TEXT_RE.search(str(label or ""))
    if not match:
        return 0
    num = float(match.group(1))
    unit = match.group(2) or ""
    if unit == "억":
        return int(num * 100_000_000)
    if unit == "만":
        return int(num * 10_000)
    if unit == "천":
        return int(num * 1_000)
    return int(num)


def _builtin_style_templates() -> list[dict[str, Any]]:
    """@brief Return curated starter templates modeled on top-performing Korean Shorts styles."""
    now = "2026-11-23T00:00:00+00:00"
    return [
        {
            "id": "builtin_viral_hook_punch",
            "name": "바이럴 훅 임팩트 (Black Han Sans · Pretendard)",
            "origin": "builtin",
            "category": "정보·꿀팁",
            "description": "상단 노란색 대형 헤드라인(76px)과 하단 굵은 모던 고딕 자막(66px)으로 초반 2초 시선을 끄는 가장 대중적인 100만+ 쇼츠 스타일",
            "source_video": None,
            "editing": {
                "transition": "cut",
                "recommended_scenes": 5,
                "cut_interval_sec": 3.5,
                "bgm_mood": "upbeat",
                "hook_style": "첫 2초 질문·반전 결론 후 빠른 컷 전환",
                "script_rules": [
                    "첫 문장은 인사 없이 결론이나 궁금증 유발 질문으로 시작합니다.",
                    "한 장면당 1~1.5문장(3~5초) 속도로 빠르게 전환합니다.",
                    "숫자·비율·핵심 단어는 아라비아 숫자로 선명하게 표기합니다.",
                ],
            },
            "visual": normalize_subtitle_style(
                {
                    "preset": "shorts_punch",
                    "font_name": "Pretendard",
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
                }
            )
            | {"use_hook_banner": True},
            "updated_at": now,
        },
        {
            "id": "builtin_commerce_homeshopping",
            "name": "홈쇼핑 · 리뷰 강조형 (Do Hyeon · Jua)",
            "origin": "builtin",
            "category": "상품·리뷰",
            "description": "상단 카드형 배너(72px)와 친근한 노랑 포인트 자막(64px), 경쾌한 리듬 BGM이 어우러진 커머스·제품 소개 최적화 스타일",
            "source_video": None,
            "editing": {
                "transition": "cut",
                "recommended_scenes": 5,
                "cut_interval_sec": 3.2,
                "bgm_mood": "upbeat",
                "hook_style": "공감 문제 제기 후 실물 특징·스펙·혜택 리듬감 전개",
                "script_rules": [
                    "쇼호스트처럼 짧고 리듬감 있는 구어체 감탄·공감 포인트를 섞습니다.",
                    "규격·소재·특장점은 숫자와 단위로 명확히 짚고 마지막에 구매/행동을 유도합니다.",
                    "배경 음악은 밝고 경쾌한 업비트 리듬을 유지합니다.",
                ],
            },
            "visual": normalize_subtitle_style(
                {
                    "preset": "yellow_pop",
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
                    "banner_font_size": 72,
                    "banner_primary_colour": "&H00FFFFFF",
                    "banner_outline_colour": "&H00000000",
                    "banner_back_colour": "&HCC15202E",
                    "banner_border_style": 3,
                    "banner_outline": 12,
                    "banner_margin_v": 165,
                    "animation": "pop",
                }
            )
            | {"use_hook_banner": True},
            "updated_at": now,
        },
        {
            "id": "builtin_clean_info_modern",
            "name": "모던 정보 · 깔끔 카드형 (Gothic A1 · Pretendard)",
            "origin": "builtin",
            "category": "정보·꿀팁",
            "description": "어두운 반투명 자막 배경과 정갈한 산세리프 폰트로 가독성을 극대화한 뉴스·꿀팁 요약형 스타일",
            "source_video": None,
            "editing": {
                "transition": "cut",
                "recommended_scenes": 5,
                "cut_interval_sec": 4.0,
                "bgm_mood": "upbeat",
                "hook_style": "숫자·핵심 요약형 타이틀 제시",
                "script_rules": [
                    "불필요한 수식어를 줄이고 핵심 사실·단계만 명료하게 전달합니다.",
                    "제목은 15자 이내 한눈에 들어오게 압축합니다.",
                ],
            },
            "visual": normalize_subtitle_style(
                {
                    "preset": "top_news",
                    "font_name": "Pretendard",
                    "font_size": 60,
                    "margin_v": 305,
                    "outline": 5,
                    "shadow": 2,
                    "alignment": 2,
                    "primary_colour": "&H00FFFFFF",
                    "outline_colour": "&H00000000",
                    "back_colour": "&HCC101520",
                    "border_style": 3,
                    "banner_font_name": "Gothic A1",
                    "banner_font_size": 68,
                    "banner_primary_colour": "&H0000E5FF",
                    "banner_outline_colour": "&H00000000",
                    "banner_back_colour": "&HCC101520",
                    "banner_border_style": 3,
                    "banner_outline": 12,
                    "banner_margin_v": 165,
                    "animation": "fade",
                }
            )
            | {"use_hook_banner": True},
            "updated_at": now,
        },
        {
            "id": "builtin_story_emotional_serif",
            "name": "스토리 · 감성 명조형 (Noto Serif · Sunflower)",
            "origin": "builtin",
            "category": "스토리·감성",
            "description": "부드러운 명조체 타이틀과 페이드 전환, 차분한 사운드가 어우러져 브랜드 스토리·추억·감동 콘텐츠에 어울리는 스타일",
            "source_video": None,
            "editing": {
                "transition": "fade",
                "recommended_scenes": 4,
                "cut_interval_sec": 5.0,
                "bgm_mood": "calm",
                "hook_style": "공감과 여운을 남기는 감성 도입",
                "script_rules": [
                    "따뜻하고 서정적인 구어체 호흡을 유지하며 장면 간 페이드 전환을 활용합니다.",
                    "과도한 느낌표 대신 진정성 있는 스토리 흐름을 만듭니다.",
                ],
            },
            "visual": normalize_subtitle_style(
                {
                    "preset": "clean_bottom",
                    "font_name": "Sunflower",
                    "font_size": 60,
                    "margin_v": 310,
                    "outline": 5,
                    "shadow": 2,
                    "alignment": 2,
                    "primary_colour": "&H00E8F6FF",
                    "outline_colour": "&H00121212",
                    "back_colour": "&H88000000",
                    "border_style": 1,
                    "banner_font_name": "Noto Serif CJK KR",
                    "banner_font_size": 66,
                    "banner_primary_colour": "&H00FFFFFF",
                    "banner_outline_colour": "&H00121212",
                    "banner_back_colour": "&H88000000",
                    "banner_border_style": 1,
                    "banner_outline": 7,
                    "banner_margin_v": 170,
                    "animation": "fade",
                }
            )
            | {"use_hook_banner": True},
            "updated_at": now,
        },
    ]


def _default_state() -> dict[str, Any]:
    return {
        "templates": _builtin_style_templates(),
        "auto_sync_enabled": True,
        "sync_interval_hours": 6,
        "query_cursor": 0,
        "last_sync_at": None,
        "last_sync_status": "아직 자동 수집이 실행되지 않았습니다.",
        "last_checked_query": "",
        "last_added_count": 0,
    }


def load_style_templates_state(store: Store) -> dict[str, Any]:
    """@brief Load style templates state merged cleanly with curated builtin defaults."""
    raw = store.get_setting(STYLE_TEMPLATE_SETTINGS_KEY)
    state = _default_state()
    if not isinstance(raw, dict):
        return state
    saved_templates = raw.get("templates")
    builtins = _builtin_style_templates()
    builtin_by_id = {tpl["id"]: tpl for tpl in builtins}
    if isinstance(saved_templates, list) and saved_templates:
        auto_ones = [t for t in saved_templates if isinstance(t, dict) and t.get("origin") != "builtin"]
        # Always keep refreshed definitions for builtin templates + persisted 1M+ discovered templates.
        merged = [*builtins, *auto_ones[:20]]
    else:
        merged = builtins
    for key in ("auto_sync_enabled", "sync_interval_hours", "query_cursor", "last_sync_at", "last_sync_status", "last_checked_query", "last_added_count"):
        if key in raw:
            state[key] = raw[key]
    # Keep stable lookup integrity for custom edited builtin IDs if none missing
    for idx, entry in enumerate(merged):
        if entry.get("id") in builtin_by_id:
            merged[idx] = builtin_by_id[entry["id"]]
    state["templates"] = merged
    return state


def find_style_template(store: Store, template_id: str | None) -> dict[str, Any] | None:
    """@brief Look up one saved style template by ID."""
    tid = str(template_id or "").strip()
    if not tid:
        return None
    state = load_style_templates_state(store)
    return next((t for t in state["templates"] if t.get("id") == tid), None)


def search_high_view_korean_shorts(query: str, *, min_views: int = MIN_VIRAL_VIEW_COUNT, limit: int = 8) -> list[dict[str, Any]]:
    """@brief Search YouTube Korea Shorts and pre-filter candidates with 1M+ views."""
    clean_query = str(query or "#shorts 꿀팁").strip()[:120] or "#shorts 꿀팁"
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
        data=json.dumps(
            {
                "query": clean_query,
                "context": {"client": {"clientName": "WEB", "clientVersion": "2.20241120.01.00", "hl": "ko", "gl": "KR"}},
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "User-Agent": "Mozilla/5.0 (compatible; ShortformContentStudio/0.1)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read(2_500_000).decode("utf-8", errors="replace")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for video_id, acc_text in _REEL_BLOCK_RE.findall(raw):
        if video_id in seen:
            continue
        approx_views = parse_korean_view_count(acc_text)
        if approx_views < min_views:
            continue
        if not _has_hangul(acc_text):
            continue
        seen.add(video_id)
        candidates.append({"video_id": video_id, "accessibility_text": acc_text[:200], "approx_views": approx_views})
        if len(candidates) >= limit:
            break
    return candidates


def _fetch_shorts_frame_part(video_id: str) -> dict[str, str] | None:
    """@brief Download the Shorts vertical frame or cover thumbnail as base64 for Vision analysis."""
    for suffix in ("oar2.jpg", "hqdefault.jpg"):
        url = f"https://i.ytimg.com/vi/{video_id}/{suffix}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ShortformContentStudio/0.1"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read(1_500_000)
            if raw and len(raw) > 2000:
                return {"mime_type": "image/jpeg", "data_b64": base64.b64encode(raw).decode("ascii")}
        except Exception:
            continue
    return None


def _safe_ass_color(value: Any, fallback: str) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if _ASS_COLOR_RE.fullmatch(candidate) else fallback


def _safe_font_choice(value: Any, fallback: str) -> str:
    clean = str(value or "").strip()
    return clean if clean in KOREAN_SHORTS_FONT_FAMILIES else fallback


def build_style_template_from_shorts(video_data: dict[str, Any], *, use_ai: bool = True) -> dict[str, Any]:
    """@brief Convert a verified 1M+ Korean YouTube Shorts video into a reusable rendering & editing template."""
    metrics = compute_pacing_metrics(
        str(video_data.get("transcript") or ""),
        list(video_data.get("segments") or []),
        int(video_data.get("duration_seconds") or 30),
    )
    avg_seg = float(metrics.get("avg_segment_seconds") or 3.2)
    title = str(video_data.get("title") or "").strip()
    view_count = int(video_data.get("view_count") or 0)
    video_id = str(video_data.get("video_id") or "")

    # Defaults grounded in measured pacing metrics
    extracted: dict[str, Any] = {
        "template_name": f"{title[:14].strip()} 스타일" if title else f"100만+ 쇼츠 {video_id[:6]}",
        "category": "정보·꿀팁",
        "description": f"조회수 {max(1, view_count // 10000):,}만회 쇼츠 기반 ({metrics.get('recommended_scenes', 5)}컷, 컷당 ~{avg_seg}초)",
        "banner_font_name": "Black Han Sans",
        "banner_font_size": 74,
        "banner_primary_colour": "&H0000E5FF",
        "banner_back_colour": "&HB00D1117",
        "banner_border_style": 1,
        "banner_margin_v": 165,
        "use_hook_banner": True,
        "font_name": "Pretendard",
        "font_size": 64,
        "primary_colour": "&H00FFFFFF",
        "outline_colour": "&H00000000",
        "back_colour": "&H99000000",
        "border_style": 1,
        "outline": 6,
        "shadow": 2,
        "alignment": 2,
        "margin_v": 320,
        "animation": "pop",
        "transition": "cut" if avg_seg <= 3.8 else "fade",
        "bgm_mood": "calm" if avg_seg > 4.2 else "upbeat",
        "recommended_scenes": int(metrics.get("recommended_scenes") or 5),
        "hook_style": f"초반 2초 훅 ('{str(metrics.get('opening_hook') or title)[:45]}')",
        "script_rules": [
            f"약 {metrics.get('recommended_scenes', 5)}개 장면으로 컷당 {avg_seg}초 템포를 유지합니다.",
            "첫 2초 안에 결론·숫자·질문형 훅을 바로 배치합니다.",
            "짧고 리듬감 있는 한국어 구어체로 한 번에 한 메시지씩 전달합니다.",
        ],
    }

    if use_ai:
        part = _fetch_shorts_frame_part(video_id)
        if part is not None:
            provider, model = selected_model("planning")
            if not ai_ready("planning") and get_secret("GEMINI_API_KEY"):
                provider, model = "gemini_api", "gemini-3-flash-preview"
            prompt = (
                f"{STYLE_TEMPLATE_VISION_PROMPT}\n"
                f"Video Title: {title}\n"
                f"Views: {view_count}\n"
                f"Duration: {video_data.get('duration_seconds')}s\n"
                f"Pacing Metrics: {json.dumps(metrics, ensure_ascii=False)}\n"
                f"Transcript Excerpt: {str(video_data.get('transcript') or '')[:1500]}"
            )
            try:
                raw_json = generate_multimodal_description(provider, model, prompt, [part])
                cleaned = raw_json.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    extracted.update({k: v for k, v in parsed.items() if v is not None})
            except Exception:
                pass

    alignment = extracted.get("alignment") if extracted.get("alignment") in {2, 5} else 2
    border_style = 3 if extracted.get("border_style") == 3 else 1
    banner_border_style = 3 if extracted.get("banner_border_style") == 3 else 1
    visual_raw = {
        "preset": "shorts_punch",
        "font_name": _safe_font_choice(extracted.get("font_name"), "Pretendard"),
        "font_size": max(52, min(86, int(extracted.get("font_size") or 64))),
        "margin_v": max(80 if alignment == 5 else 240, min(440, int(extracted.get("margin_v") or 320))),
        "outline": max(4, min(10, int(extracted.get("outline") or 6))),
        "shadow": max(1, min(5, int(extracted.get("shadow") or 2))),
        "alignment": alignment,
        "primary_colour": _safe_ass_color(extracted.get("primary_colour"), "&H00FFFFFF"),
        "outline_colour": _safe_ass_color(extracted.get("outline_colour"), "&H00000000"),
        "back_colour": _safe_ass_color(extracted.get("back_colour"), "&H99000000"),
        "border_style": border_style,
        "banner_font_name": _safe_font_choice(extracted.get("banner_font_name"), "Black Han Sans"),
        "banner_font_size": max(56, min(96, int(extracted.get("banner_font_size") or 74))),
        "banner_primary_colour": _safe_ass_color(extracted.get("banner_primary_colour"), "&H0000E5FF"),
        "banner_outline_colour": "&H00000000",
        "banner_back_colour": _safe_ass_color(extracted.get("banner_back_colour"), "&HCC101520"),
        "banner_border_style": banner_border_style,
        "banner_outline": 12 if banner_border_style == 3 else 7,
        "banner_margin_v": max(120, min(240, int(extracted.get("banner_margin_v") or 165))),
        "animation": extracted.get("animation") if extracted.get("animation") in {"pop", "fade", "static"} else "pop",
    }
    visual = normalize_subtitle_style(visual_raw)
    visual["use_hook_banner"] = bool(extracted.get("use_hook_banner", True))

    rules = extracted.get("script_rules")
    clean_rules = [str(r).strip()[:220] for r in rules[:4] if isinstance(r, str) and r.strip()] if isinstance(rules, list) else []
    if not clean_rules:
        clean_rules = ["첫 2초 안에 강력한 훅을 제시하고 4~5장면으로 빠르게 전개합니다."]

    return {
        "id": f"yt1m_{video_id}_{uuid4().hex[:6]}",
        "name": str(extracted.get("template_name") or f"100만+ 쇼츠 ({video_id})")[:60],
        "origin": "auto_1m_youtube",
        "category": str(extracted.get("category") or "정보·꿀팁")[:30],
        "description": str(extracted.get("description") or "")[:240],
        "source_video": {
            "video_id": video_id,
            "url": str(video_data.get("url") or f"https://www.youtube.com/shorts/{video_id}"),
            "title": title[:140],
            "author": str(video_data.get("author") or "")[:80],
            "view_count": view_count,
            "duration_seconds": int(video_data.get("duration_seconds") or 30),
        },
        "editing": {
            "transition": "fade" if extracted.get("transition") == "fade" else "cut",
            "recommended_scenes": max(3, min(7, int(extracted.get("recommended_scenes") or 5))),
            "cut_interval_sec": round(avg_seg, 1),
            "bgm_mood": "calm" if extracted.get("bgm_mood") == "calm" else "upbeat",
            "hook_style": str(extracted.get("hook_style") or "")[:160],
            "script_rules": clean_rules,
        },
        "visual": visual,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def discover_and_update_style_templates(
    store: Store,
    *,
    max_new: int = 2,
    min_views: int = MIN_VIRAL_VIEW_COUNT,
    custom_query: str = "",
) -> dict[str, Any]:
    """@brief Discover new 1M+ view Korean Shorts and update persisted reusable style templates."""
    state = load_style_templates_state(store)
    templates = list(state["templates"])
    known_video_ids = {
        str((t.get("source_video") or {}).get("video_id") or "")
        for t in templates
        if isinstance(t.get("source_video"), dict)
    }

    cursor = int(state.get("query_cursor") or 0)
    query = str(custom_query or "").strip() or DEFAULT_DISCOVERY_QUERIES[cursor % len(DEFAULT_DISCOVERY_QUERIES)]
    if not custom_query:
        cursor = (cursor + 1) % len(DEFAULT_DISCOVERY_QUERIES)

    added: list[dict[str, Any]] = []
    use_ai = ai_ready("planning") or bool(get_secret("GEMINI_API_KEY"))
    errors: list[str] = []

    try:
        candidates = search_high_view_korean_shorts(query, min_views=min_views, limit=12)
        for cand in candidates:
            vid = cand["video_id"]
            if vid in known_video_ids:
                continue
            try:
                details = fetch_youtube_shorts_data(f"https://www.youtube.com/shorts/{vid}")
            except Exception as err:
                errors.append(f"{vid}: {err}")
                continue
            exact_views = int(details.get("view_count") or cand["approx_views"] or 0)
            if exact_views < min_views:
                continue
            details["view_count"] = max(exact_views, cand["approx_views"])
            if not _has_hangul(details.get("title", "") + " " + details.get("transcript", "")):
                continue
            tpl = build_style_template_from_shorts(details, use_ai=use_ai)
            templates.append(tpl)
            known_video_ids.add(vid)
            added.append(tpl)
            if len(added) >= max_new:
                break
    except Exception as err:
        errors.append(str(err))

    builtins = [t for t in templates if t.get("origin") == "builtin"]
    auto_ones = [t for t in templates if t.get("origin") != "builtin"][-20:]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if added:
        status_msg = f"'{query}' 검색에서 100만+ 쇼츠 {len(added)}개 스타일 템플릿을 새로 생성했습니다."
    elif errors:
        status_msg = f"'{query}' 조회 중 일부 오류 ({errors[0][:80]})"
    else:
        status_msg = f"'{query}'에서 새로운 미수집 100만+ 쇼츠가 없어 기존 템플릿을 유지했습니다."

    new_state = {
        "templates": [*builtins, *auto_ones],
        "auto_sync_enabled": bool(state.get("auto_sync_enabled", True)),
        "sync_interval_hours": int(state.get("sync_interval_hours") or 6),
        "query_cursor": cursor,
        "last_sync_at": now_iso,
        "last_sync_status": status_msg,
        "last_checked_query": query,
        "last_added_count": len(added),
    }
    store.set_setting(STYLE_TEMPLATE_SETTINGS_KEY, new_state)
    return {**new_state, "added": added}


def style_template_to_production_config(template: dict[str, Any] | None) -> dict[str, Any]:
    """@brief Extract normalized visual subtitle/banner style, editing rules, and transition from a template."""
    if not isinstance(template, dict):
        return {}
    visual = template.get("visual") if isinstance(template.get("visual"), dict) else {}
    editing = template.get("editing") if isinstance(template.get("editing"), dict) else {}
    return {
        "id": str(template.get("id") or ""),
        "name": str(template.get("name") or ""),
        "subtitle_style": normalize_subtitle_style(visual),
        "use_hook_banner": bool(visual.get("use_hook_banner", True)),
        "transition": "fade" if editing.get("transition") == "fade" else "cut",
        "bgm_mood": "calm" if editing.get("bgm_mood") == "calm" else "upbeat",
        "recommended_scenes": int(editing.get("recommended_scenes") or 5),
        "hook_style": str(editing.get("hook_style") or ""),
        "script_rules": list(editing.get("script_rules") or []),
    }
