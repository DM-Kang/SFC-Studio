"""Bounded web research and stock-footage discovery adapters."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"
MAX_PROVIDER_RESPONSE_BYTES = 4_000_000
MAX_RESEARCH_SOURCES = 5
MAX_EXTRACTED_SOURCES = 4
MAX_SOURCE_CONTENT_CHARS = 8_000
MAX_SCENE_SEARCHES = 5
MAX_VIDEOS_PER_SCENE = 3
MAX_RESEARCH_IMAGES = 16
MAX_NEWS_IMAGE_BYTES = 10_000_000
_BAD_IMAGE_URL_TOKENS = (
    "/ad/",
    "/ads/",
    "doubleclick",
    "facebook.com/tr",
    "pixel",
    "favicon",
    "sprite",
    "spacer",
    "logo_small",
    "share.",
    "icon",
    "avatar",
    "emoji",
    ".svg",
    ".gif",
)


def _request_json(url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    """@brief Send a bounded JSON request to a fixed external provider endpoint."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            encoded = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Resource provider returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Resource provider request failed: {type(error).__name__}.") from None
    decoded = _decode_json_response(encoded)
    from .usage import record_tavily_usage

    if url == TAVILY_SEARCH_URL:
        record_tavily_usage("search", depth=str(body.get("search_depth") or "basic"))
    elif url == TAVILY_EXTRACT_URL:
        res_list = decoded.get("results") if isinstance(decoded.get("results"), list) else []
        ok_count = max(1, len(res_list)) if res_list else max(1, len(body.get("urls") or []))
        record_tavily_usage("extract", depth=str(body.get("extract_depth") or "basic"), urls_count=ok_count)
    return decoded


def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> dict[str, Any]:
    """@brief Read a bounded JSON response from a fixed provider API endpoint."""
    merged_headers = {"User-Agent": "ShortformContentStudio/0.1", "Accept": "application/json", **(headers or {})}
    request = urllib.request.Request(url, headers=merged_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            encoded = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Resource provider returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Resource provider request failed: {type(error).__name__}.") from None
    decoded = _decode_json_response(encoded)
    from .usage import record_tavily_usage

    if url == TAVILY_SEARCH_URL:
        record_tavily_usage("search", depth=str(body.get("search_depth") or "basic"))
    elif url == TAVILY_EXTRACT_URL:
        res_list = decoded.get("results") if isinstance(decoded.get("results"), list) else []
        ok_count = max(1, len(res_list)) if res_list else max(1, len(body.get("urls") or []))
        record_tavily_usage("extract", depth=str(body.get("extract_depth") or "basic"), urls_count=ok_count)
    return decoded


def _decode_json_response(encoded: bytes) -> dict[str, Any]:
    """@brief Enforce response-size and JSON-object contracts for provider results."""
    if len(encoded) > MAX_PROVIDER_RESPONSE_BYTES:
        raise RuntimeError("Resource provider response exceeded the local size limit.")
    try:
        result = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Resource provider returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Resource provider returned an unexpected response shape.")
    return result


def _safe_public_url(value: Any) -> str | None:
    """@brief Accept public HTTPS links without credentials or local-network hosts."""
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        hostname = parsed.hostname
        if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
            return None
        normalized_host = hostname.rstrip(".").lower()
        if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            address = None
        if address and (not address.is_global):
            return None
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def validate_external_url(value: Any) -> str | None:
    """@brief Validate an optional public HTTPS source or license URL for display."""
    return _safe_public_url(value)


def _urls_in_notes(notes: str) -> list[str]:
    """@brief Extract a small set of explicitly supplied HTTPS source URLs."""
    candidates = re.findall(r"https?://[^\s<>\"'`]+", notes or "", flags=re.IGNORECASE)
    urls: list[str] = []
    for candidate in candidates:
        safe_url = _safe_public_url(candidate.rstrip(".,;:!?)]}"))
        if safe_url and safe_url not in urls:
            urls.append(safe_url)
        if len(urls) >= 2:
            break
    return urls


def _is_usable_editorial_image_url(url: str | None) -> bool:
    """@brief Reject tracker pixels, SVGs, share icons, and non-editorial thumbnail URLs."""
    if not url:
        return False
    lowered = url.lower()
    if any(tok in lowered for tok in _BAD_IMAGE_URL_TOKENS):
        return False
    return True


def collect_web_research(brief: dict[str, Any], api_key: str) -> dict[str, Any]:
    """@brief Search a topic and extract source pages plus editorial candidate images for visual grounding."""
    if not api_key:
        raise ValueError("Configure TAVILY_API_KEY before collecting web research.")
    topic = str(brief.get("topic", "")).strip()[:500]
    if len(topic) < 3:
        raise ValueError("A topic with at least three characters is required for web research.")
    language = "ko" if str(brief.get("language", "")).lower() in {"ko", "korean", "한국어"} else "en"
    headers = {"Authorization": f"Bearer {api_key}"}
    search = _request_json(
        TAVILY_SEARCH_URL,
        headers=headers,
        body={
            "query": topic,
            "search_depth": "basic",
            "max_results": MAX_RESEARCH_SOURCES,
            "topic": "general",
            "language": language,
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
            "include_images": True,
            "include_image_descriptions": True,
        },
    )
    candidates: list[dict[str, Any]] = []
    for result in search.get("results", []) if isinstance(search.get("results"), list) else []:
        if not isinstance(result, dict):
            continue
        url = _safe_public_url(result.get("url"))
        if not url:
            continue
        candidates.append(
            {
                "url": url,
                "title": str(result.get("title") or url)[:300],
                "snippet": str(result.get("content") or "")[:1_200],
                "published_at": str(result.get("published_date") or "")[:80],
                "score": result.get("score") if isinstance(result.get("score"), (float, int)) else None,
            }
        )
    by_url = {entry["url"]: entry for entry in candidates}
    supplied_urls = _urls_in_notes(str(brief.get("source_notes", "")))
    ordered_urls = list(supplied_urls)
    ordered_urls.extend(url for url in by_url if url not in ordered_urls)
    ordered_urls = ordered_urls[:MAX_RESEARCH_SOURCES]
    if not ordered_urls:
        raise RuntimeError("The web search returned no usable public HTTPS sources.")

    extracted_by_url: dict[str, str] = {}
    extraction_error: str | None = None
    extraction_urls = ordered_urls[:MAX_EXTRACTED_SOURCES]
    try:
        extracted = _request_json(
            TAVILY_EXTRACT_URL,
            headers=headers,
            body={"urls": extraction_urls, "extract_depth": "basic", "format": "markdown", "include_published_date": True},
        )
        for result in extracted.get("results", []) if isinstance(extracted.get("results"), list) else []:
            if not isinstance(result, dict):
                continue
            url = _safe_public_url(result.get("url"))
            content = result.get("raw_content")
            if url in extraction_urls and isinstance(content, str) and content.strip():
                extracted_by_url[url] = content.strip()[:MAX_SOURCE_CONTENT_CHARS]
    except RuntimeError as error:
        # Search results remain visible, but snippets are intentionally not promoted to evidence.
        extraction_error = str(error)

    image_candidates: list[dict[str, str]] = []
    seen_img_urls: set[str] = set()
    for raw_img in search.get("images", []) if isinstance(search.get("images"), list) else []:
        if isinstance(raw_img, dict):
            img_url = _safe_public_url(raw_img.get("url"))
            desc = str(raw_img.get("description") or "").strip()[:240]
        elif isinstance(raw_img, str):
            img_url = _safe_public_url(raw_img)
            desc = ""
        else:
            continue
        if img_url and _is_usable_editorial_image_url(img_url) and img_url not in seen_img_urls:
            seen_img_urls.add(img_url)
            image_candidates.append({"url": img_url, "description": desc})
            if len(image_candidates) >= MAX_RESEARCH_IMAGES:
                break

    if len(image_candidates) < MAX_RESEARCH_IMAGES:
        for src_url, md_text in extracted_by_url.items():
            for match in re.finditer(r"!\[([^\]]{0,140})\]\((https://[^)\s]+)\)", md_text):
                alt, raw_link = match.group(1).strip(), match.group(2).strip()
                img_url = _safe_public_url(raw_link)
                if img_url and _is_usable_editorial_image_url(img_url) and img_url not in seen_img_urls:
                    seen_img_urls.add(img_url)
                    image_candidates.append({"url": img_url, "description": alt[:240], "source_url": src_url})
                    if len(image_candidates) >= MAX_RESEARCH_IMAGES:
                        break
            if len(image_candidates) >= MAX_RESEARCH_IMAGES:
                break

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources: list[dict[str, Any]] = []
    for index, url in enumerate(ordered_urls, 1):
        candidate = by_url.get(url, {"title": url, "snippet": "", "published_at": "", "score": None})
        content = extracted_by_url.get(url, "")
        hostname = urllib.parse.urlsplit(url).hostname or ""
        sources.append(
            {
                "id": f"S{index}",
                "title": candidate["title"],
                "url": url,
                "domain": hostname,
                "snippet": candidate["snippet"],
                "content": content,
                "published_at": candidate["published_at"],
                "score": candidate["score"],
                "provider": "Tavily",
                "status": "fetched" if content else "snippet_only",
                "fetched_at": now if content else None,
            }
        )
    return {
        "status": "complete" if sources and all(source["status"] == "fetched" for source in sources) else "partial",
        "provider": "Tavily",
        "query": topic,
        "collected_at": now,
        "sources": sources,
        "images": image_candidates,
        "extraction_error": extraction_error,
        "evidence_source_ids": [source["id"] for source in sources if source["status"] == "fetched"],
    }


def supplement_research_images(brief: dict[str, Any], research: dict[str, Any] | None, api_key: str) -> dict[str, Any]:
    """@brief Fetch additional English/international editorial news images when initial Korean search yields few photos."""
    base = dict(research) if isinstance(research, dict) else {"sources": [], "images": []}
    existing = list(base.get("images") or [])
    if len(existing) >= 4 or not api_key:
        return base
    raw_query = f"{brief.get('topic') or ''} {brief.get('source_notes') or ''}"[:400]
    tokens = [w for w in re.findall(r"[A-Za-z0-9\-]{2,}", raw_query) if not w.isdigit()][:6]
    # Also include 2-word core Hangul tokens translated/kept if ASCII scarce
    img_query = " ".join(tokens) if len(tokens) >= 2 else str(brief.get("topic") or "").strip()[:120]
    if not img_query:
        return base
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        search = _request_json(
            TAVILY_SEARCH_URL,
            headers=headers,
            body={
                "query": img_query,
                "search_depth": "basic",
                "max_results": 5,
                "topic": "general",
                "include_answer": False,
                "include_images": True,
                "include_image_descriptions": True,
            },
            timeout=20,
        )
    except Exception:
        return base
    seen = {str(e.get("url") or "") for e in existing if isinstance(e, dict)}
    for raw_img in search.get("images", []) if isinstance(search.get("images"), list) else []:
        if isinstance(raw_img, dict):
            img_url = _safe_public_url(raw_img.get("url"))
            desc = str(raw_img.get("description") or "").strip()[:240]
        elif isinstance(raw_img, str):
            img_url = _safe_public_url(raw_img)
            desc = ""
        else:
            continue
        if img_url and _is_usable_editorial_image_url(img_url) and img_url not in seen:
            seen.add(img_url)
            existing.append({"url": img_url, "description": desc})
            if len(existing) >= MAX_RESEARCH_IMAGES:
                break
    base["images"] = existing
    return base


def search_targeted_editorial_images(
    queries: list[str],
    research: dict[str, Any] | None,
    api_key: str,
    *,
    include_sources: bool = False,
    max_total_images: int = MAX_RESEARCH_IMAGES,
) -> dict[str, Any]:
    """@brief Fetch targeted editorial photos and optional citable articles per missing scene entity/person."""
    base = dict(research) if isinstance(research, dict) else {"sources": [], "images": []}
    if not api_key or not queries:
        return base
    images = list(base.get("images") or [])
    sources = list(base.get("sources") or [])
    seen_img_urls = {str(e.get("url") or "") for e in images if isinstance(e, dict)}
    seen_src_urls = {str(s.get("url") or "") for s in sources if isinstance(s, dict)}
    headers = {"Authorization": f"Bearer {api_key}"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cleaned_queries: list[str] = []
    seen_q: set[str] = set()
    for raw_q in queries:
        q = re.sub(r"\s+", " ", str(raw_q or "")).strip()[:120]
        if q and q.lower() not in seen_q:
            seen_q.add(q.lower())
            cleaned_queries.append(q)
        if len(cleaned_queries) >= 4:
            break

    for q in cleaned_queries:
        if len(images) >= max_total_images and not include_sources:
            break
        try:
            search = _request_json(
                TAVILY_SEARCH_URL,
                headers=headers,
                body={
                    "query": q,
                    "search_depth": "basic",
                    "max_results": 4,
                    "topic": "general",
                    "include_answer": False,
                    "include_images": True,
                    "include_image_descriptions": True,
                },
                timeout=20,
            )
        except Exception:
            continue
        results = search.get("results", []) if isinstance(search.get("results"), list) else []
        top_page_url = ""
        for item in results:
            if not isinstance(item, dict):
                continue
            cand_url = _safe_public_url(item.get("url"))
            if not cand_url:
                continue
            if not top_page_url:
                top_page_url = cand_url
            if include_sources and cand_url not in seen_src_urls and len(sources) < 10:
                seen_src_urls.add(cand_url)
                hostname = urllib.parse.urlsplit(cand_url).hostname or ""
                snippet = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:1200]
                sources.append(
                    {
                        "id": f"S{len(sources) + 1}",
                        "title": str(item.get("title") or cand_url)[:240],
                        "url": cand_url,
                        "domain": hostname,
                        "snippet": snippet[:400],
                        "content": snippet,
                        "published_at": str(item.get("published_date") or ""),
                        "score": item.get("score"),
                        "provider": "Tavily",
                        "status": "fetched" if snippet else "snippet_only",
                        "fetched_at": now if snippet else None,
                    }
                )
        for raw_img in search.get("images", []) if isinstance(search.get("images"), list) else []:
            if len(images) >= max_total_images:
                break
            if isinstance(raw_img, dict):
                img_url = _safe_public_url(raw_img.get("url"))
                desc = str(raw_img.get("description") or "").strip()[:240]
            elif isinstance(raw_img, str):
                img_url = _safe_public_url(raw_img)
                desc = ""
            else:
                continue
            if img_url and _is_usable_editorial_image_url(img_url) and img_url not in seen_img_urls:
                seen_img_urls.add(img_url)
                entry: dict[str, Any] = {"url": img_url, "description": desc or q}
                if top_page_url:
                    entry["source_url"] = top_page_url
                images.append(entry)

    base["images"] = images
    base["sources"] = sources
    return base


def collect_community_reactions(
    topic: str,
    feedback: str,
    research: dict[str, Any] | None,
    api_key: str,
) -> dict[str, Any]:
    """@brief Search indexed community discussions and fan reaction summaries via Tavily without anti-bot bypass."""
    base = dict(research) if isinstance(research, dict) else {"sources": [], "images": []}
    if not api_key:
        return base
    sources = list(base.get("sources") or [])
    seen_urls = {str(s.get("url") or "") for s in sources if isinstance(s, dict)}
    clean_topic = re.sub(r"[!?,.;:]+", " ", str(topic or "")).strip()
    core_terms = " ".join(clean_topic.split()[:6])
    queries = [f"{core_terms} 에펨코리아 팬 반응 댓글", f"{core_terms} 팬 반응 네티즌 댓글"]
    headers = {"Authorization": f"Bearer {api_key}"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for q in queries:
        if not core_terms:
            break
        try:
            search = _request_json(
                TAVILY_SEARCH_URL,
                headers=headers,
                body={
                    "query": q,
                    "search_depth": "basic",
                    "max_results": 4,
                    "topic": "general",
                    "include_answer": True,
                    "include_images": False,
                },
                timeout=20,
            )
        except Exception:
            continue
        for item in search.get("results", []) if isinstance(search.get("results"), list) else []:
            if not isinstance(item, dict) or len(sources) >= 12:
                break
            cand_url = _safe_public_url(item.get("url"))
            if not cand_url or cand_url in seen_urls:
                continue
            seen_urls.add(cand_url)
            hostname = urllib.parse.urlsplit(cand_url).hostname or ""
            snippet = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:1200]
            sources.append(
                {
                    "id": f"S{len(sources) + 1}",
                    "title": str(item.get("title") or cand_url)[:240],
                    "url": cand_url,
                    "domain": hostname,
                    "snippet": snippet[:400],
                    "content": snippet,
                    "published_at": str(item.get("published_date") or ""),
                    "score": item.get("score"),
                    "provider": "Tavily (community)",
                    "status": "fetched" if snippet else "snippet_only",
                    "fetched_at": now if snippet else None,
                }
            )
        answer = re.sub(r"\s+", " ", str(search.get("answer") or "")).strip()[:1000]
        if answer and not any(str(s.get("provider") or "") == "Tavily (reaction summary)" for s in sources):
            sources.append(
                {
                    "id": f"S{len(sources) + 1}",
                    "title": f"{core_terms} 커뮤니티 반응 요약",
                    "url": "",
                    "domain": "tavily-reaction-summary",
                    "snippet": answer[:400],
                    "content": answer,
                    "published_at": "",
                    "score": None,
                    "provider": "Tavily (reaction summary)",
                    "status": "fetched",
                    "fetched_at": now,
                }
            )
    base["sources"] = sources
    return base


def download_news_image(image_url: str, destination: Path, *, max_bytes: int = MAX_NEWS_IMAGE_BYTES) -> bool:
    """@brief Safely download a real JPEG/PNG/WEBP editorial image via verified public HTTPS host."""
    try:
        safe_url = _resolve_and_verify_public_host(image_url)
    except (ValueError, RuntimeError):
        return False
    request = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ShortformContentStudio/0.1)", "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            ctype = str(response.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
                return False
            raw = response.read(max_bytes + 1)
    except Exception:
        return False
    if len(raw) < 5_000 or len(raw) > max_bytes:
        return False
    is_valid_magic = (
        raw.startswith(b"\xff\xd8\xff")
        or raw.startswith(b"\x89PNG\r\n\x1a\n")
        or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    )
    if not is_valid_magic:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f"{destination.stem}.tmp{destination.suffix}")
    try:
        staging.write_bytes(raw)
        staging.replace(destination)
        return True
    except Exception:
        staging.unlink(missing_ok=True)
        return False


def search_pexels_videos(scenes: list[dict[str, Any]], api_key: str) -> list[dict[str, Any]]:
    """@brief Find a few portrait-friendly Pexels clips for each bounded scene query."""
    if not api_key:
        raise ValueError("Configure PEXELS_API_KEY before searching stock videos.")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Generate a plan with scenes before searching for video materials.")
    materials: list[dict[str, Any]] = []
    for scene in scenes[:MAX_SCENE_SEARCHES]:
        if not isinstance(scene, dict):
            continue
        query = str(scene.get("visual_query") or "").strip()[:200]
        scene_id = str(scene.get("id") or "")[:20]
        if not query:
            continue
        url = f"{PEXELS_VIDEO_SEARCH_URL}?{urllib.parse.urlencode({'query': query, 'orientation': 'portrait', 'per_page': MAX_VIDEOS_PER_SCENE, 'locale': 'ko-KR'})}"
        response = _get_json(url, headers={"Authorization": api_key})
        videos = response.get("videos", [])
        if not videos:
            ascii_words = [w for w in query.split() if w.isascii() and len(w) > 1]
            if len(ascii_words) > 2:
                retry_url = f"{PEXELS_VIDEO_SEARCH_URL}?{urllib.parse.urlencode({'query': ' '.join(ascii_words[:2]), 'orientation': 'portrait', 'per_page': MAX_VIDEOS_PER_SCENE})}"
                videos = _get_json(retry_url, headers={"Authorization": api_key}).get("videos", [])
        for video in videos[:MAX_VIDEOS_PER_SCENE] if isinstance(videos, list) else []:
            if not isinstance(video, dict):
                continue
            source_url = _safe_public_url(video.get("url"))
            poster_url = _safe_public_url(video.get("image"))
            if not source_url:
                continue
            user = video.get("user") if isinstance(video.get("user"), dict) else {}
            video_files = video.get("video_files") if isinstance(video.get("video_files"), list) else []
            download_url = _select_pexels_download_url(video_files)
            slug_parts = [p for p in urllib.parse.urlsplit(source_url).path.split("/") if p and p != "video"]
            slug_tags = re.sub(r"-\d+$", "", slug_parts[-1]).replace("-", ", ") if slug_parts else ""
            materials.append(
                {
                    "scene_id": scene_id,
                    "query": query,
                    "provider": "Pexels",
                    "provider_id": str(video.get("id") or "")[:80],
                    "title": f"Pexels video {video.get('id', '')}"[:160],
                    "tags": slug_tags[:200],
                    "source_url": source_url,
                    "download_url": download_url,
                    "poster_url": poster_url,
                    "author": str(user.get("name") or "Unknown contributor")[:160],
                    "author_url": _safe_public_url(user.get("url")),
                    "duration_seconds": video.get("duration") if isinstance(video.get("duration"), int) else None,
                    "width": video.get("width") if isinstance(video.get("width"), int) else None,
                    "height": video.get("height") if isinstance(video.get("height"), int) else None,
                    "license_url": "https://www.pexels.com/license/",
                    "rights_status": "human_verification_required",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
    return materials


def _select_pexels_download_url(video_files: list[Any]) -> str | None:
    """@brief Select a bounded portrait or HD MP4 file URL from Pexels metadata."""
    candidates: list[tuple[int, str]] = []
    for entry in video_files:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("file_type") or "").lower() not in {"video/mp4", ""}:
            continue
        link = _safe_public_url(entry.get("link"))
        if not link:
            continue
        width = int(entry.get("width") or 0) if isinstance(entry.get("width"), int) else 0
        height = int(entry.get("height") or 0) if isinstance(entry.get("height"), int) else 0
        portrait_bonus = 10_000 if height > width else 0
        hd_score = min(height, 1920)
        candidates.append((portrait_bonus + hd_score, link))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _select_pixabay_video_entry(videos_obj: Any) -> dict[str, Any] | None:
    """@brief Pick a bounded portrait or HD MP4 rendition from Pixabay video metadata."""
    if not isinstance(videos_obj, dict):
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    fallback_thumbnail: str | None = None
    for size_rank, size_key in enumerate(("medium", "large", "small", "tiny")):
        entry = videos_obj.get(size_key)
        if not isinstance(entry, dict):
            continue
        thumb = _safe_public_url(entry.get("thumbnail"))
        if thumb and not fallback_thumbnail:
            fallback_thumbnail = thumb
        link = _safe_public_url(entry.get("url"))
        if not link:
            continue
        width = int(entry.get("width") or 0) if isinstance(entry.get("width"), int) else 0
        height = int(entry.get("height") or 0) if isinstance(entry.get("height"), int) else 0
        portrait_bonus = 10_000 if height > width else 0
        hd_score = min(height or 720, 1920)
        candidates.append((portrait_bonus + hd_score - size_rank, {"url": link, "width": width, "height": height, "thumbnail": thumb}))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    chosen = candidates[0][1]
    if not chosen.get("thumbnail") and fallback_thumbnail:
        chosen["thumbnail"] = fallback_thumbnail
    return chosen


def search_pixabay_videos(scenes: list[dict[str, Any]], api_key: str) -> list[dict[str, Any]]:
    """@brief Search Pixabay free stock videos per planned scene and retain provenance."""
    if not api_key:
        raise ValueError("Configure PIXABAY_API_KEY before searching stock videos.")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("Generate a plan with scenes before searching for video materials.")
    materials: list[dict[str, Any]] = []
    for scene in scenes[:MAX_SCENE_SEARCHES]:
        if not isinstance(scene, dict):
            continue
        query = str(scene.get("visual_query") or "").strip()[:100]
        scene_id = str(scene.get("id") or "")[:20]
        if not query:
            continue
        url = f"{PIXABAY_VIDEO_SEARCH_URL}?{urllib.parse.urlencode({'key': api_key, 'q': query, 'per_page': max(3, min(10, MAX_VIDEOS_PER_SCENE * 3)), 'safesearch': 'true', 'video_type': 'all'})}"
        response = _get_json(url)
        hits = response.get("hits", [])
        if not hits:
            ascii_words = [w for w in query.split() if w.isascii() and len(w) > 1]
            if len(ascii_words) > 2:
                shorter_query = " ".join(ascii_words[:2])
                retry_url = f"{PIXABAY_VIDEO_SEARCH_URL}?{urllib.parse.urlencode({'key': api_key, 'q': shorter_query, 'per_page': max(3, min(10, MAX_VIDEOS_PER_SCENE * 3)), 'safesearch': 'true', 'video_type': 'all'})}"
                hits = _get_json(retry_url).get("hits", [])
        ranked_hits: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for hit in hits if isinstance(hits, list) else []:
            if not isinstance(hit, dict):
                continue
            best = _select_pixabay_video_entry(hit.get("videos"))
            source_url = _safe_public_url(hit.get("pageURL"))
            if not best or not source_url:
                continue
            portrait_bonus = 10_000 if (best["height"] > best["width"]) else 0
            ranked_hits.append((portrait_bonus + min(best["height"], 1920), hit, best))
        ranked_hits.sort(key=lambda entry: entry[0], reverse=True)
        for _, hit, best in ranked_hits[:MAX_VIDEOS_PER_SCENE]:
            source_url = _safe_public_url(hit.get("pageURL"))
            if not source_url:
                continue
            poster_url = _safe_public_url(best.get("thumbnail"))
            picture_id = str(hit.get("picture_id") or "").strip()
            if not poster_url and picture_id and all(ch.isalnum() or ch in {"-", "_"} for ch in picture_id):
                poster_url = f"https://i.vimeocdn.com/video/{picture_id}_640x360.jpg"
            author_name = str(hit.get("user") or "Unknown contributor")[:160]
            user_id = hit.get("user_id")
            author_url = None
            if hit.get("user") and isinstance(user_id, int):
                author_url = _safe_public_url(f"https://pixabay.com/users/{urllib.parse.quote(str(hit['user']))}-{user_id}/")
            hit_id = str(hit.get("id") or "")
            materials.append(
                {
                    "scene_id": scene_id,
                    "query": query,
                    "provider": "Pixabay",
                    "provider_id": f"pixabay-{hit_id}"[:80],
                    "title": (f"Pixabay · {str(hit.get('tags') or hit_id).split(',')[0].strip()}" if hit.get("tags") else f"Pixabay video {hit_id}")[:160],
                    "tags": str(hit.get("tags") or "")[:200],
                    "source_url": source_url,
                    "download_url": best["url"],
                    "poster_url": poster_url,
                    "author": author_name,
                    "author_url": author_url,
                    "duration_seconds": hit.get("duration") if isinstance(hit.get("duration"), int) else None,
                    "width": best["width"] or None,
                    "height": best["height"] or None,
                    "license_url": "https://pixabay.com/service/license-summary/",
                    "rights_status": "human_verification_required",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
    return materials


def download_stock_clip(download_url: str, destination: Path, *, max_bytes: int = 250_000_000, provider_name: str = "Stock provider") -> int:
    """@brief Safely download a validated HTTPS stock MP4 clip to local storage."""
    safe_url = _safe_public_url(download_url)
    if not safe_url:
        raise ValueError("Clip download URL must be a valid public HTTPS link.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp")
    request = urllib.request.Request(safe_url, headers={"User-Agent": "ShortformContentStudio/0.1"})
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Downloaded stock clip exceeds the 250 MB safety limit.")
                output.write(chunk)
        if total == 0:
            raise RuntimeError(f"{provider_name} returned an empty video clip.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return total


def download_pexels_clip(download_url: str, destination: Path, *, max_bytes: int = 250_000_000) -> int:
    """@brief Backward-compatible wrapper around download_stock_clip for Pexels clips."""
    return download_stock_clip(download_url, destination, max_bytes=max_bytes, provider_name="Pexels")


def _resolve_and_verify_public_host(url: str) -> str:
    """@brief Verify that a public HTTPS URL does not resolve to private or loopback IP addresses."""
    safe_url = _safe_public_url(url)
    if not safe_url:
        raise ValueError("URL must be a public HTTPS address without credentials or local hosts.")
    hostname = urllib.parse.urlsplit(safe_url).hostname or ""
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise RuntimeError(f"Could not resolve hostname: {type(error).__name__}.") from None
    for info in infos:
        ip_raw = info[4][0].split("%", 1)[0]
        address = ipaddress.ip_address(ip_raw)
        if not address.is_global:
            raise ValueError("Resolved address is not a public IP address.")
    return safe_url


def fetch_direct_url_source(url: str, *, source_id: str = "S1") -> dict[str, Any]:
    """@brief Fetch a public HTTPS page directly and extract readable text as citable evidence."""
    safe_url = _resolve_and_verify_public_host(url)
    request = urllib.request.Request(safe_url, headers={"User-Agent": "ShortformContentStudio/0.1", "Accept": "text/html,text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Source URL returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Could not fetch URL: {type(error).__name__}.") from None

    decoded = raw.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, flags=re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip())[:300] if title_match else safe_url
    cleaned = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", decoded, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
    if not cleaned:
        raise RuntimeError("No readable text could be extracted from that URL.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hostname = urllib.parse.urlsplit(safe_url).hostname or ""
    return {
        "id": source_id[:20],
        "title": title or safe_url,
        "url": safe_url,
        "domain": hostname,
        "snippet": cleaned[:400],
        "content": cleaned[:MAX_SOURCE_CONTENT_CHARS],
        "published_at": "",
        "score": None,
        "provider": "DirectURL",
        "status": "fetched",
        "included": True,
        "fetched_at": now,
    }


def test_provider_connection(provider: str) -> dict[str, Any]:
    """@brief Run a lightweight credential/connectivity check for a configured external provider."""
    from .config import get_secret

    name = str(provider or "").strip().lower()
    if name == "tavily":
        key = get_secret("TAVILY_API_KEY")
        if not key:
            return {"ok": False, "message": "TAVILY_API_KEY is not set."}
        _request_json(TAVILY_SEARCH_URL, headers={"Authorization": f"Bearer {key}"}, body={"query": "test", "max_results": 1}, timeout=15)
        return {"ok": True, "message": "Tavily API key verified."}
    if name == "pexels":
        key = get_secret("PEXELS_API_KEY")
        if not key:
            return {"ok": False, "message": "PEXELS_API_KEY is not set."}
        _get_json(f"{PEXELS_VIDEO_SEARCH_URL}?query=nature&per_page=1", headers={"Authorization": key}, timeout=15)
        return {"ok": True, "message": "Pexels API key verified."}
    if name == "pixabay":
        key = get_secret("PIXABAY_API_KEY")
        if not key:
            return {"ok": False, "message": "PIXABAY_API_KEY is not set."}
        _get_json(f"{PIXABAY_VIDEO_SEARCH_URL}?{urllib.parse.urlencode({'key': key, 'q': 'nature', 'per_page': 3})}", timeout=15)
        return {"ok": True, "message": "Pixabay API key verified."}
    if name == "elevenlabs":
        key = get_secret("ELEVENLABS_API_KEY")
        if not key:
            return {"ok": False, "message": "ELEVENLABS_API_KEY is not set."}
        _get_json("https://api.elevenlabs.io/v1/user", headers={"xi-api-key": key}, timeout=15)
        return {"ok": True, "message": "ElevenLabs API key verified."}
    if name == "runway":
        key = get_secret("RUNWAY_API_KEY")
        if not key:
            return {"ok": False, "message": "RUNWAY_API_KEY is not set."}
        return {"ok": True, "message": "RUNWAY_API_KEY is configured (billed per generation)."}
    if name == "openai":
        key = get_secret("OPENAI_API_KEY")
        if not key:
            return {"ok": False, "message": "OPENAI_API_KEY is not set."}
        _get_json("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        return {"ok": True, "message": "OpenAI API key verified."}
    if name == "gemini":
        key = get_secret("GEMINI_API_KEY")
        if not key:
            return {"ok": False, "message": "GEMINI_API_KEY is not set."}
        _get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=5", headers={"x-goog-api-key": key}, timeout=15)
        return {"ok": True, "message": "Gemini API key verified."}
    if name == "anthropic":
        key = get_secret("ANTHROPIC_API_KEY")
        if not key:
            return {"ok": False, "message": "ANTHROPIC_API_KEY is not set."}
        _get_json(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=15,
        )
        return {"ok": True, "message": "Anthropic API key verified."}
    if name == "tiktok":
        return {"ok": bool(get_secret("TIKTOK_ACCESS_TOKEN")), "message": "TIKTOK_ACCESS_TOKEN configured." if get_secret("TIKTOK_ACCESS_TOKEN") else "TIKTOK_ACCESS_TOKEN is not set."}
    raise ValueError("Unsupported provider for connection testing.")
