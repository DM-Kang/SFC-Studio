"""Business workflow for creating, reviewing, and publishing content items."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ai import ai_ready
from .analytics import summarize_analytics
from .config import get_projects_dir, get_secret
from .media import generate_voiceover, probe_video, render_video, runway_generate_video
from .planning import generate_plan, review_plan
from .preflight import build_publish_preflight, combine_review_status, rights_assets_fingerprint
from .platform_content import copy_fingerprint, instagram_caption, platform_plan
from .publishing import PublicationError, publish_instagram, publish_youtube
from .resources import collect_web_research, search_pexels_videos
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
    research = item.get("research")
    research_sources = research.get("sources", []) if isinstance(research, dict) else []
    brief = _brief(item)
    prior_items = [candidate for candidate in store.list_items() if candidate.get("id") != item_id]
    brief["analytics_feedback"] = summarize_analytics(prior_items)
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
    plan.update(changes)
    if changes.get("script") != previous_script:
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
    source = runway_generate_video(plan["video_prompt"], folder, int(plan["duration_seconds"]))
    narration = generate_voiceover(plan["script"], folder)
    final_path = folder / "final.mp4"
    render_video(Path(source["path"]), final_path, audio_path=Path(narration["audio_path"]) if narration else None, captions_path=Path(narration["captions_path"]) if narration else None)
    assets = {
        "source_video": source["path"],
        "final_video": str(final_path),
        "sha256": sha256_file(final_path),
        "generated_for_plan_revision": plan["revision"],
        "video_provider": {key: value for key, value in source.items() if key != "path"},
        "voiceover": narration,
    }
    rights_assets = replace_final_video_rights_record(item.get("rights_assets"), str(source.get("provider") or "video generation service"))
    store.update_payload(item_id, {"assets": assets, "rights_assets": rights_assets, "review": None, "approved_at": None, "reviewed_asset_sha256": None})
    return "generated"


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


def run_publication(store: Store, item_id: str, platform: str, privacy_status: str) -> str:
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
        if platform == "youtube":
            result = publish_youtube(video_path, platform_plan(plan, variants, platform), privacy_status, bool(item.get("synthetic_media", True)))
        elif platform == "instagram":
            result = publish_instagram(item_id, video_path, instagram_caption(platform_plan(plan, variants, platform)))
        else:
            raise PublicationError("Unsupported publishing platform.")
    except PublicationError:
        raise
    except Exception as error:
        raise PublicationError(f"Publishing failed: {type(error).__name__}.", uncertain=True) from None
    store.finish_publication(item_id, platform, "published", remote_id=result["remote_id"], permalink=result.get("permalink"))
    publications = store.get_item(item_id)["publications"]
    chosen = set(item.get("platforms", []))
    if chosen and all(publications.get(target, {}).get("status") == "published" for target in chosen):
        return "published"
    return "approved"
