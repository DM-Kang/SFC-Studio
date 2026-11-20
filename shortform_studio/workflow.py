"""Business workflow for creating, reviewing, and publishing content items."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ai import ai_ready
from .analytics import summarize_analytics
from .config import get_projects_dir, get_secret
from .media import (
    SUBTITLE_STYLE_PRESETS,
    assemble_multiscene_video,
    extract_video_frame,
    generate_voiceover,
    normalize_subtitle_style,
    probe_video,
    render_video,
    runway_generate_video,
    validate_and_normalize_srt,
)
from .planning import (
    autofix_plan_with_review,
    generate_plan,
    normalize_editable_scenes,
    review_plan,
    suggest_hooks,
)
from .preflight import build_publish_preflight, combine_review_status, rights_assets_fingerprint
from .platform_content import copy_fingerprint, instagram_caption, platform_plan
from .publishing import (
    PublicationError,
    delete_r2_object,
    publish_instagram,
    publish_tiktok,
    publish_youtube,
    update_youtube_privacy,
)
from .resources import collect_web_research, download_pexels_clip, fetch_direct_url_source, search_pexels_videos
from .store import Store


def item_directory(item_id: str) -> Path:
    """@brief Resolve a UUID item to its private local media directory."""
    if len(item_id) != 36 or any(char not in "0123456789abcdef-" for char in item_id.lower()):
        raise ValueError("Invalid content item identifier.")
    root = get_projects_dir().resolve()
    destination = (root / item_id).resolve()
    if destination.parent != root:
        raise ValueError("Content item path is outside the application media directory.")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def delete_item_media(item_id: str) -> None:
    """@brief Remove a deleted item's private local media directory safely."""
    if len(item_id) != 36 or any(char not in "0123456789abcdef-" for char in item_id.lower()):
        raise ValueError("Invalid content item identifier.")
    root = get_projects_dir().resolve()
    destination = (root / item_id).resolve()
    if destination.parent != root:
        raise ValueError("Content item path is outside the application media directory.")
    if destination.exists():
        shutil.rmtree(destination)


def sha256_file(path: Path) -> str:
    """@brief Hash a saved media artifact for approval and publication binding."""
    digest = hashlib.sha256()
    with path.open("rb") as media_file:
        while chunk := media_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def can_reuse_video_for_plan(previous_plan: dict[str, Any], restored_plan: dict[str, Any], assets: dict[str, Any]) -> bool:
    """@brief Determine whether an existing video still matches restored production inputs.

    @param previous_plan Plan that was active when the saved artifact was reviewed.
    @param restored_plan Historical plan selected for restoration.
    @param assets Current media-artifact metadata.
    @returns True only when the media-affecting plan fields match and artifact revision is current.
    """
    media_fields = ("script", "video_prompt", "duration_seconds")
    return all(previous_plan.get(field) == restored_plan.get(field) for field in media_fields) and assets.get("generated_for_plan_revision") == previous_plan.get("revision")


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    """@brief Select only planning inputs from a stored project item."""
    return {
        **{key: item.get(key) for key in ("topic", "audience", "tone", "language", "platforms", "duration_seconds", "source_notes")},
        "brand_profile": item.get("brand_profile_snapshot") or {},
        "series_template": item.get("series_template_snapshot") or {},
    }


def final_video_rights_record(source: str) -> dict[str, Any]:
    """@brief Create an unresolved rights record for a newly replaced final video."""
    return {
        "id": str(uuid4()),
        "asset_name": "Final video and all included audio, visuals, and voices",
        "asset_type": "final_video",
        "source_description": f"Generated or uploaded via {source}"[:300],
        "source_url": "",
        "license_name": "",
        "license_url": "",
        "rights_basis": "",
        "attribution": "",
        "included": True,
        "rights_status": "unverified",
    }


def replace_final_video_rights_record(existing: Any, source: str) -> list[dict[str, Any]]:
    """@brief Preserve supporting-source records while resetting the replaced final video's rights."""
    supporting_assets = []
    if isinstance(existing, list):
        supporting_assets = [
            dict(asset)
            for asset in existing
            if isinstance(asset, dict) and asset.get("asset_type") != "final_video"
        ]
    return [*supporting_assets, final_video_rights_record(source)]


def run_planning(store: Store, item_id: str) -> str:
    """@brief Generate a new plan and invalidate approval for the previous draft."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    use_ai = ai_ready("planning")
    store.set_job_progress(item_id, "참고 자료 및 성과 요약 수집 중", 20)
    research = item.get("research")
    raw_sources = research.get("sources", []) if isinstance(research, dict) else []
    research_sources = [src for src in raw_sources if isinstance(src, dict) and src.get("included", True) is not False]
    brief = _brief(item)
    prior_items = [candidate for candidate in store.list_items() if candidate.get("id") != item_id]
    brief["analytics_feedback"] = summarize_analytics(prior_items)
    if store.is_cancel_requested(item_id):
        raise RuntimeError("Operation cancelled by operator.")
    store.set_job_progress(item_id, "AI 기획안 생성 중", 60)
    plan = generate_plan(brief, use_ai, research_sources)
    plan["revision"] = int(item.get("plan_revision", 0)) + 1
    if isinstance(item.get("plan"), dict):
        store.save_revision_snapshot(item_id, "AI-generated plan replaced the previous draft")
    store.update_payload(
        item_id,
        {"plan": plan, "platform_variants": {}, "plan_revision": plan["revision"], "review": None, "approved_at": None, "approved_asset_sha256": None, "reviewed_asset_sha256": None, "approval_record": None},
    )
    return "planned"


def run_web_research(store: Store, item_id: str, return_stage: str) -> str:
    """@brief Collect and persist bounded web sources before planning a draft."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    research = collect_web_research(_brief(item), get_secret("TAVILY_API_KEY"))
    store.update_payload(item_id, {"research": research})
    return return_stage


def run_stock_material_search(store: Store, item_id: str, return_stage: str) -> str:
    """@brief Search Pexels for scene-matched footage and retain review metadata."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Generate a plan with scene descriptions before searching for video materials.")
    materials = search_pexels_videos(plan.get("scenes", []), get_secret("PEXELS_API_KEY"))
    store.update_payload(item_id, {"materials": materials, "materials_collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return return_stage


def revise_plan(store: Store, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """@brief Persist plan edits as a new revision and invalidate derived review state."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Generate a plan before editing it.")
    store.save_revision_snapshot(item_id, "Shared plan edited")
    revision = int(item.get("plan_revision", plan.get("revision", 0))) + 1
    previous_script = plan.get("script")
    explicit_scenes = changes.pop("scenes", None)
    plan.update(changes)
    if explicit_scenes is not None:
        plan["scenes"] = normalize_editable_scenes(explicit_scenes, str(plan.get("script") or ""), str(item.get("topic") or ""))
    elif changes.get("script") != previous_script:
        # A human-edited narration cannot keep search terms generated for the old scene order.
        plan["scenes"] = [{"id": "scene-1", "narration": str(plan["script"])[:800], "visual_query": str(item.get("topic") or "")[:200]}]
    plan["revision"] = revision
    plan["generated_by"] = f"{plan.get('generated_by', 'unknown')} + human edit"
    return store.update_payload(
        item_id,
        {
            "plan": plan,
            "plan_revision": revision,
            "review": None,
            "approved_at": None,
            "approved_asset_sha256": None,
            "reviewed_asset_sha256": None,
            "approval_record": None,
            "materials": [],
            "materials_collected_at": None,
        },
    )


def run_video_generation(store: Store, item_id: str) -> str:
    """@brief Generate a vertical video, optional narration, and a final MP4."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Create a plan before generating video.")
    store.cancel_pending_schedules(item_id, "Video generation started; review and schedule the new artifact after approval.")
    folder = item_directory(item_id)
    store.set_job_progress(item_id, "Runway 영상 생성 요청 중", 20)
    source = runway_generate_video(plan["video_prompt"], folder, int(plan["duration_seconds"]))
    if store.is_cancel_requested(item_id):
        raise RuntimeError("Operation cancelled by operator after video clip download.")
    store.set_job_progress(item_id, "내레이션 음성·자막 생성 중", 65)
    narration = generate_voiceover(plan["script"], folder)
    existing_style = (item.get("assets") or {}).get("subtitle_style")
    subtitle_style = normalize_subtitle_style(existing_style) if isinstance(existing_style, dict) else normalize_subtitle_style()
    final_path = folder / "final.mp4"
    store.set_job_progress(item_id, "FFmpeg 최종 MP4 렌더링 중", 85)
    render_video(
        Path(source["path"]),
        final_path,
        audio_path=Path(narration["audio_path"]) if narration else None,
        captions_path=Path(narration["captions_path"]) if narration else None,
        subtitle_style=subtitle_style,
    )
    assets = {
        "source_video": source["path"],
        "final_video": str(final_path),
        "sha256": sha256_file(final_path),
        "generated_for_plan_revision": plan["revision"],
        "video_provider": {key: value for key, value in source.items() if key != "path"},
        "voiceover": narration,
        "captions_path": narration["captions_path"] if narration else None,
        "subtitle_style": subtitle_style,
    }
    rights_assets = replace_final_video_rights_record(item.get("rights_assets"), str(source.get("provider") or "video generation service"))
    store.update_payload(item_id, {"assets": assets, "rights_assets": rights_assets, "review": None, "approved_at": None, "reviewed_asset_sha256": None})
    return "generated"


def get_subtitles_state(store: Store, item_id: str) -> dict[str, Any]:
    """@brief Return saved SRT text, active subtitle style, and rerender readiness."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    assets = item.get("assets") or {}
    voiceover = assets.get("voiceover") if isinstance(assets.get("voiceover"), dict) else {}
    captions_candidate = assets.get("captions_path") or voiceover.get("captions_path") or ""
    srt_text = ""
    if captions_candidate:
        captions_path = Path(captions_candidate)
        if captions_path.is_file():
            srt_text = captions_path.read_text(encoding="utf-8")
    style = normalize_subtitle_style(assets.get("subtitle_style"))
    has_video = bool(assets.get("source_video") or assets.get("final_video"))
    return {
        "srt": srt_text,
        "style": style,
        "presets": {key: value["label"] for key, value in SUBTITLE_STYLE_PRESETS.items()},
        "can_rerender": has_video,
    }


def save_subtitles_and_rerender(
    store: Store,
    item_id: str,
    srt_text: str,
    style: dict[str, Any] | None = None,
    *,
    rerender: bool = True,
) -> dict[str, Any]:
    """@brief Validate edited SRT/style, save captions, optionally rerender MP4, and reset approval."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    normalized_srt = validate_and_normalize_srt(srt_text)
    normalized_style = normalize_subtitle_style(style)
    folder = item_directory(item_id)
    captions_path = folder / "captions.srt"
    captions_path.write_text(normalized_srt, encoding="utf-8")

    assets = dict(item.get("assets") or {})
    assets["captions_path"] = str(captions_path)
    assets["subtitle_style"] = normalized_style
    if isinstance(assets.get("voiceover"), dict):
        assets["voiceover"] = {**assets["voiceover"], "captions_path": str(captions_path)}

    if rerender:
        source_candidate = Path(assets.get("source_video") or assets.get("final_video") or "")
        if not source_candidate.is_file():
            raise RuntimeError("A saved source or uploaded MP4 is required before rendering subtitles.")
        final_path = folder / "final.mp4"
        # Preserve an uploaded final.mp4 as an untouched source copy so repeated subtitle edits never re-burn old captions.
        if source_candidate.resolve() == final_path.resolve():
            preserved_source = folder / "source_original.mp4"
            shutil.copy2(final_path, preserved_source)
            source_candidate = preserved_source
            assets["source_video"] = str(preserved_source)
        voiceover = assets.get("voiceover") if isinstance(assets.get("voiceover"), dict) else {}
        audio_candidate = Path(voiceover.get("audio_path", "")) if voiceover.get("audio_path") else None
        if audio_candidate and not audio_candidate.is_file():
            audio_candidate = None
        render_video(
            source_candidate,
            final_path,
            audio_path=audio_candidate,
            captions_path=captions_path,
            subtitle_style=normalized_style,
        )
        assets["final_video"] = str(final_path)
        assets["sha256"] = sha256_file(final_path)
        if isinstance(item.get("plan"), dict):
            assets["generated_for_plan_revision"] = item["plan"].get("revision")

    store.cancel_pending_schedules(item_id, "Subtitles or render settings changed; review and schedule the updated video after approval.")
    store.update_payload(
        item_id,
        {
            "assets": assets,
            "review": None,
            "approved_at": None,
            "approved_asset_sha256": None,
            "reviewed_asset_sha256": None,
            "approval_record": None,
        },
    )
    if item.get("stage") in {"reviewed", "approved"}:
        store.set_stage(item_id, "generated")
    return store.get_item(item_id)


def run_content_review(store: Store, item_id: str) -> str:
    """@brief Run media checks and an optional independent editorial review."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    assets = item.get("assets") or {}
    video_path = Path(assets.get("final_video", ""))
    if not isinstance(plan, dict) or not video_path.is_file():
        raise RuntimeError("A saved plan and final MP4 are required before review.")
    if assets.get("generated_for_plan_revision") != plan.get("revision"):
        raise RuntimeError("The saved video was not produced for the current plan; upload or generate a matching video first.")
    technical = probe_video(video_path)
    ai_result: dict[str, Any] | None = None
    ai_error: str | None = None
    source_ids = {
        source.get("id")
        for source in plan.get("sources", [])
        if isinstance(source, dict) and source.get("status") == "fetched" and isinstance(source.get("id"), str)
    }
    unverified_claims: list[str] = []
    claims = plan.get("claims", [])
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        cited_sources = claim.get("source_ids", [])
        has_valid_citation = isinstance(cited_sources, list) and any(
            source_id in source_ids for source_id in cited_sources if isinstance(source_id, str)
        )
        if claim.get("evidence_required") or not has_valid_citation:
            unverified_claims.append(str(claim.get("claim") or "Unlabeled factual claim")[:1_000])
    if ai_ready("review"):
        try:
            review_draft = {
                **plan,
                "platform_publishing_variants": item.get("platform_variants", {}),
            }
            ai_result = review_plan(_brief(item), review_draft)
        except Exception as error:
            ai_error = str(error)
    # The text reviewer cannot verify rendered visuals, spoken audio, source rights, or factual evidence.
    if technical["available"] and not technical["passed"]:
        base_status = "needs_changes"
    elif ai_result and ai_result["status"] != "passed":
        base_status = "needs_changes"
    elif unverified_claims:
        # Keep weakly sourced drafts reviewable, but never imply the citations were checked.
        base_status = "manual_required"
    elif not technical["available"] or not ai_result:
        base_status = "manual_required"
    else:
        base_status = "passed"
    preflight = build_publish_preflight(
        plan,
        item.get("platforms", []),
        technical,
        item.get("rights_assets", []),
        item.get("brand_profile_snapshot", {}),
        item.get("platform_variants", {}),
    )
    review = {
        "status": combine_review_status(base_status, preflight),
        "status_without_preflight": base_status,
        "technical": technical,
        "editorial": ai_result,
        "editorial_error": ai_error,
        "preflight": preflight,
        "unverified_claims": unverified_claims,
        "manual_checks_required": ["visual and voiceover match", "factual claims and cited sources", "music, footage, voice, and likeness rights", "brand safety and platform policies"],
        "plan_revision": plan.get("revision"),
        "asset_sha256": sha256_file(video_path),
    }
    store.update_payload(item_id, {"review": review, "reviewed_asset_sha256": review["asset_sha256"], "approved_at": None, "approved_asset_sha256": None, "approval_record": None})
    return "reviewed"


def run_publication(store: Store, item_id: str, platform: str, privacy_status: str, *, publish_at: str | None = None) -> str:
    """@brief Publish an approved exact media artifact to one selected platform."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan") or {}
    assets = item.get("assets") or {}
    video_path = Path(assets.get("final_video", ""))
    approved_hash = item.get("approved_asset_sha256")
    if not item.get("approved_at") or not video_path.is_file() or not approved_hash:
        raise PublicationError("Approve the current saved video before publishing.")
    if sha256_file(video_path) != approved_hash:
        raise PublicationError("The video changed after approval; review and approve it again.")
    review = item.get("review") or {}
    technical = review.get("technical") or {}
    variants = item.get("platform_variants", {})
    preflight = build_publish_preflight(plan, item.get("platforms", []), technical, item.get("rights_assets", []), item.get("brand_profile_snapshot", {}), variants)
    if preflight["status"] != "ready":
        raise PublicationError("Publishing is blocked by the current platform preflight; resolve the listed checks and approve again.")
    approval_record = item.get("approval_record") or {}
    if approval_record.get("rights_asset_sha256") != rights_assets_fingerprint(item.get("rights_assets", [])):
        raise PublicationError("Asset-rights records changed after approval; review and approve again.")
    if approval_record.get("copy_sha256") != copy_fingerprint(plan, variants):
        raise PublicationError("Platform copy changed after approval; review and approve the current copy again.")
    try:
        thumbnail_candidate = Path(assets.get("thumbnail_path", "")) if assets.get("thumbnail_path") else None
        if thumbnail_candidate and not thumbnail_candidate.is_file():
            thumbnail_candidate = None
        thumb_offset_ms = assets.get("thumbnail_offset_ms")
        store.set_job_progress(item_id, f"{platform} 업로드 진행 중", 50)
        if platform == "youtube":
            result = publish_youtube(
                video_path,
                platform_plan(plan, variants, platform),
                privacy_status,
                bool(item.get("synthetic_media", True)),
                thumbnail_path=thumbnail_candidate,
                publish_at=publish_at,
            )
        elif platform == "instagram":
            result = publish_instagram(
                item_id,
                video_path,
                instagram_caption(platform_plan(plan, variants, platform)),
                thumb_offset_ms=thumb_offset_ms if isinstance(thumb_offset_ms, int) else None,
            )
        elif platform == "tiktok":
            tt_privacy = privacy_status if privacy_status in {"SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"} else "SELF_ONLY"
            result = publish_tiktok(video_path, platform_plan(plan, variants, platform), tt_privacy)
        else:
            raise PublicationError("Unsupported publishing platform.")
    except PublicationError:
        raise
    except Exception as error:
        raise PublicationError(f"Publishing failed: {type(error).__name__}.", uncertain=True) from None
    if platform == "instagram":
        staged_key = result.get("staged_object")
        updated_assets = dict(assets)
        if result.get("r2_cleaned") == "true":
            updated_assets.pop("instagram_staged_object", None)
        elif staged_key:
            updated_assets["instagram_staged_object"] = staged_key
        store.update_payload(item_id, {"assets": updated_assets})
    store.finish_publication(item_id, platform, "published", remote_id=result["remote_id"], permalink=result.get("permalink"))
    publications = store.get_item(item_id)["publications"]
    chosen = set(item.get("platforms", []))
    if chosen and all(publications.get(target, {}).get("status") == "published" for target in chosen):
        return "published"
    return "approved"


def cleanup_instagram_r2_object(store: Store, item_id: str) -> dict[str, Any]:
    """@brief Manually delete a leftover R2 staged object recorded for an Instagram upload."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    assets = dict(item.get("assets") or {})
    object_key = str(assets.get("instagram_staged_object") or "")
    if not object_key:
        raise RuntimeError("No leftover R2 staged object is recorded for this item.")
    cleaned = delete_r2_object(object_key)
    if not cleaned:
        raise RuntimeError("Could not delete the R2 object; verify R2 credentials and bucket permissions.")
    assets.pop("instagram_staged_object", None)
    store.update_payload(item_id, {"assets": assets})
    return {"cleaned": True, "object_key": object_key}


def change_youtube_privacy(store: Store, item_id: str, privacy_status: str) -> dict[str, Any]:
    """@brief Update visibility status of an already-published YouTube video."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    pub = (item.get("publications") or {}).get("youtube") or {}
    if pub.get("status") != "published" or not pub.get("remote_id"):
        raise RuntimeError("YouTube video must be published before changing its visibility.")
    return update_youtube_privacy(str(pub["remote_id"]), privacy_status)


def add_direct_url_research(store: Store, item_id: str, url: str) -> dict[str, Any]:
    """@brief Fetch a single public HTTPS URL directly into the item's citable research snapshot."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    research = dict(item.get("research") or {})
    sources = list(research.get("sources") or [])
    next_index = len(sources) + 1
    source_id = f"S{next_index}"
    fetched = fetch_direct_url_source(url, source_id=source_id)
    if any(isinstance(s, dict) and s.get("url") == fetched["url"] for s in sources):
        raise RuntimeError("This URL is already included in the item's sources.")
    sources.append(fetched)
    research["status"] = "complete"
    research["provider"] = research.get("provider") or "DirectURL"
    research["collected_at"] = fetched["fetched_at"]
    research["sources"] = sources[:10]
    research["evidence_source_ids"] = [s["id"] for s in research["sources"] if s.get("status") == "fetched" and s.get("included", True) is not False]
    return store.update_payload(item_id, {"research": research})


def toggle_research_source_inclusion(store: Store, item_id: str, source_id: str, included: bool) -> dict[str, Any]:
    """@brief Include or exclude a collected research source from future planning prompts."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    research = dict(item.get("research") or {})
    sources = [dict(s) for s in (research.get("sources") or []) if isinstance(s, dict)]
    matched = False
    for source in sources:
        if source.get("id") == source_id:
            source["included"] = bool(included)
            matched = True
            break
    if not matched:
        raise KeyError(source_id)
    research["sources"] = sources
    research["evidence_source_ids"] = [s["id"] for s in sources if s.get("status") == "fetched" and s.get("included", True) is not False]
    return store.update_payload(item_id, {"research": research})


def run_hook_suggestions(store: Store, item_id: str, count: int = 5) -> list[str]:
    """@brief Generate alternative hook candidates for an existing item plan."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Generate a plan before requesting hook variations.")
    hooks = suggest_hooks(_brief(item), plan, ai_ready("planning"), count=count)
    store.update_payload(item_id, {"hook_suggestions": hooks})
    return hooks


def run_plan_autofix(store: Store, item_id: str) -> dict[str, Any]:
    """@brief Use the planning model to revise a draft based on the latest review findings."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    review = item.get("review")
    if not isinstance(plan, dict) or not isinstance(review, dict):
        raise RuntimeError("A saved plan and review result are required before auto-fixing.")
    if not ai_ready("planning"):
        raise RuntimeError("Configure a planning AI model before auto-fixing a reviewed draft.")
    store.save_revision_snapshot(item_id, "AI autofix applied from review feedback")
    revised_plan, _model = autofix_plan_with_review(_brief(item), plan, review)
    revision = int(item.get("plan_revision", plan.get("revision", 0))) + 1
    revised_plan["revision"] = revision
    store.update_payload(
        item_id,
        {
            "plan": revised_plan,
            "plan_revision": revision,
            "review": None,
            "approved_at": None,
            "approved_asset_sha256": None,
            "reviewed_asset_sha256": None,
            "approval_record": None,
        },
    )
    if item.get("stage") in {"reviewed", "approved", "failed"}:
        store.set_stage(item_id, "planned")
    return store.get_item(item_id)


def _merge_supporting_rights_record(existing: Any, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """@brief Append a supporting asset rights record once based on source URL or asset name."""
    records = [dict(entry) for entry in existing if isinstance(entry, dict)] if isinstance(existing, list) else []
    source_url = str(candidate.get("source_url") or "")
    name = str(candidate.get("asset_name") or "")
    for entry in records:
        if source_url and entry.get("source_url") == source_url:
            return records
        if not source_url and name and entry.get("asset_name") == name and entry.get("asset_type") == candidate.get("asset_type"):
            return records
    records.append({"id": str(uuid4()), **candidate})
    return records


def download_stock_material_clip(store: Store, item_id: str, provider_id: str) -> dict[str, Any]:
    """@brief Download a Pexels candidate video locally for multi-scene assembly."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    materials = list(item.get("materials") or [])
    candidate = next((entry for entry in materials if isinstance(entry, dict) and str(entry.get("provider_id")) == str(provider_id)), None)
    if not candidate:
        raise KeyError(provider_id)
    download_url = candidate.get("download_url")
    if not isinstance(download_url, str) or not download_url:
        raise RuntimeError("This Pexels result does not include a downloadable MP4 link.")
    safe_id = "".join(ch for ch in str(provider_id) if ch.isalnum() or ch in {"-", "_"})[:40] or str(uuid4())[:8]
    folder = item_directory(item_id) / "clips"
    destination = folder / f"pexels_{safe_id}.mp4"
    download_pexels_clip(download_url, destination)
    for entry in materials:
        if isinstance(entry, dict) and str(entry.get("provider_id")) == str(provider_id):
            entry["local_path"] = str(destination)
    rights_assets = _merge_supporting_rights_record(
        item.get("rights_assets"),
        {
            "asset_name": candidate.get("title") or f"Pexels {safe_id}",
            "asset_type": "video",
            "source_description": f"{candidate.get('author') or 'Unknown'} · Pexels",
            "source_url": candidate.get("source_url") or "",
            "license_name": "Pexels License · verify current terms",
            "license_url": candidate.get("license_url") or "https://www.pexels.com/license/",
            "rights_basis": "",
            "attribution": candidate.get("author") or "",
            "included": True,
            "rights_status": "unverified",
        },
    )
    return store.update_payload(item_id, {"materials": materials, "rights_assets": rights_assets})


def run_scene_assembly(
    store: Store,
    item_id: str,
    scene_inputs: list[dict[str, Any]],
    *,
    transition: str = "cut",
    tts_provider: str = "auto",
    tts_voice: str = "",
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = False,
    subtitle_style: dict[str, Any] | None = None,
) -> str:
    """@brief Assemble selected clips/images across scenes with optional narration, BGM, and captions."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Create a plan before assembling scenes.")
    store.cancel_pending_schedules(item_id, "Multi-scene assembly started; review and schedule the new artifact after approval.")
    folder = item_directory(item_id)
    folder_resolved = folder.resolve()
    materials = list(item.get("materials") or [])
    rights_assets = list(item.get("rights_assets") or [])
    existing_assets = dict(item.get("assets") or {})

    resolved_clips: list[dict[str, Any]] = []
    for index, scene_req in enumerate(scene_inputs[:8]):
        source_kind = str(scene_req.get("source_kind") or "")
        duration = float(scene_req.get("duration_seconds") or 5.0)
        if source_kind == "pexels":
            provider_id = str(scene_req.get("provider_id") or "")
            candidate = next((m for m in materials if isinstance(m, dict) and str(m.get("provider_id")) == provider_id), None)
            if not candidate:
                raise RuntimeError(f"Scene {index + 1}: Pexels candidate '{provider_id}' was not found.")
            local_path = Path(str(candidate.get("local_path") or ""))
            if not local_path.is_file():
                download_url = candidate.get("download_url")
                if not isinstance(download_url, str) or not download_url:
                    raise RuntimeError(f"Scene {index + 1}: Pexels clip has no download URL.")
                safe_id = "".join(ch for ch in provider_id if ch.isalnum() or ch in {"-", "_"})[:40] or f"clip_{index}"
                local_path = folder / "clips" / f"pexels_{safe_id}.mp4"
                download_pexels_clip(download_url, local_path)
                candidate["local_path"] = str(local_path)
            rights_assets = _merge_supporting_rights_record(
                rights_assets,
                {
                    "asset_name": candidate.get("title") or f"Pexels {provider_id}",
                    "asset_type": "video",
                    "source_description": f"{candidate.get('author') or 'Unknown'} · Pexels",
                    "source_url": candidate.get("source_url") or "",
                    "license_name": "Pexels License · verify current terms",
                    "license_url": candidate.get("license_url") or "https://www.pexels.com/license/",
                    "rights_basis": "",
                    "attribution": candidate.get("author") or "",
                    "included": True,
                    "rights_status": "unverified",
                },
            )
            media_type = "video"
        elif source_kind == "uploaded":
            clip_name = Path(str(scene_req.get("filename") or "")).name
            local_path = (folder / "clips" / clip_name).resolve()
            if local_path.parent != (folder / "clips").resolve() or not local_path.is_file():
                raise RuntimeError(f"Scene {index + 1}: uploaded clip '{clip_name}' was not found.")
            media_type = "image" if local_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "video"
        elif source_kind == "source":
            local_path = Path(str(existing_assets.get("source_video") or ""))
            if not local_path.is_file() or local_path.resolve().parent != folder_resolved:
                raise RuntimeError(f"Scene {index + 1}: saved source video is missing.")
            media_type = "video"
        else:
            raise ValueError(f"Scene {index + 1}: unsupported source_kind '{source_kind}'.")
        rel = local_path.resolve().relative_to(folder_resolved).as_posix()
        resolved_clips.append({"relative_path": rel, "duration_seconds": duration, "media_type": media_type})

    narration = generate_voiceover(plan["script"], folder, tts_provider=tts_provider, voice=tts_voice)
    if narration is None and isinstance(existing_assets.get("voiceover"), dict):
        existing_vo = existing_assets["voiceover"]
        if Path(str(existing_vo.get("audio_path") or "")).is_file():
            narration = existing_vo
    captions_path = Path(narration["captions_path"]) if narration and narration.get("captions_path") else None
    if not captions_path and existing_assets.get("captions_path"):
        candidate_srt = Path(str(existing_assets["captions_path"]))
        if candidate_srt.is_file():
            captions_path = candidate_srt

    bgm_path = Path(str(existing_assets.get("bgm_path") or "")) if existing_assets.get("bgm_path") else None
    if bgm_path and not bgm_path.is_file():
        bgm_path = None

    normalized_style = normalize_subtitle_style(subtitle_style or existing_assets.get("subtitle_style"))
    master_path = folder / "source_assembled.mp4"
    final_path = folder / "final.mp4"
    # Render a clean master without burned captions first so subtitle editing can rerender without re-assembling.
    assemble_multiscene_video(
        folder,
        resolved_clips,
        master_path,
        transition=transition,
        audio_path=None,
        captions_path=None,
        bgm_path=None,
    )
    render_video(
        master_path,
        final_path,
        audio_path=Path(narration["audio_path"]) if narration else None,
        captions_path=captions_path,
        subtitle_style=normalized_style,
        bgm_path=bgm_path,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    new_assets = {
        **existing_assets,
        "source_video": str(master_path),
        "final_video": str(final_path),
        "sha256": sha256_file(final_path),
        "generated_for_plan_revision": plan["revision"],
        "video_provider": {"provider": "multiscene_ffmpeg", "scene_count": len(resolved_clips), "transition": transition},
        "voiceover": narration,
        "captions_path": str(captions_path) if captions_path else None,
        "subtitle_style": normalized_style,
    }
    rights_assets = replace_final_video_rights_record(rights_assets, "multi-scene FFmpeg assembly")
    store.update_payload(
        item_id,
        {
            "materials": materials,
            "assets": new_assets,
            "rights_assets": rights_assets,
            "review": None,
            "approved_at": None,
            "approved_asset_sha256": None,
            "reviewed_asset_sha256": None,
            "approval_record": None,
        },
    )
    return "generated"


def run_postprocess_video(
    store: Store,
    item_id: str,
    *,
    apply_subtitles: bool = True,
    apply_bgm: bool = True,
    bgm_volume: float = 0.18,
    ducking: bool = True,
    loudnorm: bool = True,
    tts_provider: str = "none",
    tts_voice: str = "",
    subtitle_style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """@brief Apply TTS, BGM ducking, loudness normalization, or subtitles to an existing MP4."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    plan = item.get("plan")
    if not isinstance(plan, dict):
        raise RuntimeError("Create a plan before post-processing a video.")
    assets = dict(item.get("assets") or {})
    folder = item_directory(item_id)
    source_candidate = Path(str(assets.get("source_video") or assets.get("final_video") or ""))
    if not source_candidate.is_file():
        raise RuntimeError("Upload or generate a video before post-processing.")
    final_path = folder / "final.mp4"
    if source_candidate.resolve() == final_path.resolve():
        preserved = folder / "source_original.mp4"
        shutil.copy2(final_path, preserved)
        source_candidate = preserved
        assets["source_video"] = str(preserved)

    narration = assets.get("voiceover") if isinstance(assets.get("voiceover"), dict) else None
    if tts_provider != "none":
        narration = generate_voiceover(plan.get("script", ""), folder, tts_provider=tts_provider, voice=tts_voice)
    audio_path = Path(narration["audio_path"]) if narration and Path(str(narration.get("audio_path") or "")).is_file() else None
    captions_candidate = (narration or {}).get("captions_path") or assets.get("captions_path")
    captions_path = Path(str(captions_candidate)) if apply_subtitles and captions_candidate and Path(str(captions_candidate)).is_file() else None
    bgm_path = Path(str(assets.get("bgm_path"))) if apply_bgm and assets.get("bgm_path") and Path(str(assets.get("bgm_path"))).is_file() else None
    normalized_style = normalize_subtitle_style(subtitle_style or assets.get("subtitle_style"))

    render_video(
        source_candidate,
        final_path,
        audio_path=audio_path,
        captions_path=captions_path,
        subtitle_style=normalized_style,
        bgm_path=bgm_path,
        bgm_volume=bgm_volume,
        ducking=ducking,
        loudnorm=loudnorm,
    )
    assets.update(
        {
            "final_video": str(final_path),
            "sha256": sha256_file(final_path),
            "generated_for_plan_revision": plan.get("revision"),
            "voiceover": narration,
            "captions_path": str(captions_path) if captions_path else assets.get("captions_path"),
            "subtitle_style": normalized_style,
        }
    )
    store.cancel_pending_schedules(item_id, "Video post-processed; review and schedule the updated artifact after approval.")
    store.update_payload(
        item_id,
        {
            "assets": assets,
            "review": None,
            "approved_at": None,
            "approved_asset_sha256": None,
            "reviewed_asset_sha256": None,
            "approval_record": None,
        },
    )
    if item.get("stage") in {"reviewed", "approved"}:
        store.set_stage(item_id, "generated")
    return store.get_item(item_id)


def save_thumbnail_from_video(store: Store, item_id: str, timestamp_seconds: float = 0.5) -> dict[str, Any]:
    """@brief Extract a JPEG cover thumbnail frame from the current saved MP4."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(item_id)
    assets = dict(item.get("assets") or {})
    video_path = Path(str(assets.get("final_video") or ""))
    if not video_path.is_file():
        raise RuntimeError("A saved final MP4 is required before extracting a thumbnail.")
    folder = item_directory(item_id)
    thumb_path = folder / "thumbnail.jpg"
    extract_video_frame(video_path, thumb_path, timestamp_seconds=timestamp_seconds)
    assets["thumbnail_path"] = str(thumb_path)
    assets["thumbnail_offset_ms"] = max(0, int(round(float(timestamp_seconds) * 1000)))
    return store.update_payload(item_id, {"assets": assets})
