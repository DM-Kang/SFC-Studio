"""Local FastAPI application and approval-gated content workflow endpoints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .config import (
    PROJECT_ROOT,
    get_app_home,
    get_database_path,
    get_managed_secrets_status,
    get_projects_dir,
    get_secret,
    is_allowed_request_origin,
    load_local_environment,
    update_local_env_secrets,
)
from .analytics import build_analytics_dashboard, export_analytics_csv, parse_analytics_csv, summarize_analytics, summarize_experiment
from .ai import ai_status
from .ai_config import save_ai_settings
from .ai_providers import (
    clear_model_list_cache,
    disconnect_chatgpt_oauth,
    disconnect_gemini_oauth,
    list_all_provider_models,
    list_provider_models,
    start_oauth_login,
    sync_ready_provider_models,
)
from .brand import default_brand_settings, find_series_template, normalize_brand_settings
from .oauth_callbacks import get_authorization_urls
from .preflight import build_publish_preflight, combine_review_status, rights_assets_fingerprint
from .backups import MAX_BACKUP_BYTES, create_backup_archive, restore_backup_archive
from .publishing import PublicationError, instagram_ready, tiktok_ready, youtube_ready, youtube_scheduling_ready
from .platform_content import copy_fingerprint, platform_plan
from .resources import test_provider_connection, validate_external_url
from .store import Store
from .workflow import (
    add_direct_url_research,
    can_reuse_video_for_plan,
    change_youtube_privacy,
    cleanup_instagram_r2_object,
    confirm_item_rights_for_approval,
    delete_item_media,
    download_stock_material_clip,
    get_subtitles_state,
    item_directory,
    record_rejection_feedback,
    revise_plan,
    run_autopilot_pipeline,
    run_content_review,
    run_hook_suggestions,
    run_plan_autofix,
    run_planning,
    run_postprocess_video,
    run_publication,
    run_scene_assembly,
    run_stock_material_search,
    run_video_generation,
    run_web_research,
    replace_final_video_rights_record,
    save_subtitles_and_rerender,
    save_thumbnail_from_video,
    sha256_file,
    toggle_research_source_inclusion,
)

load_local_environment()
get_projects_dir().mkdir(parents=True, exist_ok=True)
store = Store(get_database_path())
store.recover_interrupted_jobs()
store.recover_interrupted_schedules()
_worker_lock = threading.Lock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None
_MODEL_SYNC_INTERVAL_SECONDS = 300
_last_model_sync_at = 0.0

app = FastAPI(title="Shortform Content Studio", version="0.1.0")
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "shortform_studio" / "static"), name="static")


class NewItem(BaseModel):
    """Validate a new brief before it is persisted."""

    topic: str = Field(min_length=3, max_length=500)
    audience: str = Field(default="General audience", max_length=300)
    tone: str = Field(default="Clear and engaging", max_length=200)
    language: str = Field(default="Korean", max_length=50)
    platforms: list[str] = Field(default_factory=lambda: ["youtube"], min_length=1, max_length=3)
    duration_seconds: int = Field(default=30, ge=4, le=30)
    source_notes: str = Field(default="", max_length=12000)
    synthetic_media: bool = True
    series_template_id: str | None = Field(default=None, max_length=36)
    workflow_mode: Literal["manual", "autopilot"] = "manual"


class SceneEditInput(BaseModel):
    """Validate one editable chronological scene inside a plan."""

    id: str = Field(default="", max_length=24)
    narration: str = Field(default="", max_length=800)
    visual_query: str = Field(default="", max_length=200)
    duration_seconds: float | None = Field(default=None, ge=1.0, le=30.0)


class PlanEdit(BaseModel):
    """Validate human edits to generated text before they invalidate review."""

    title: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=240)
    script: str = Field(min_length=1, max_length=6000)
    caption: str = Field(min_length=1, max_length=5000)
    video_prompt: str = Field(min_length=1, max_length=1000)
    hashtags: list[str] = Field(default_factory=list, max_length=20)
    scenes: list[SceneEditInput] | None = Field(default=None, max_length=8)


class SceneAssemblyClipInput(BaseModel):
    """Validate one scene clip assignment for FFmpeg multi-scene assembly."""

    source_kind: Literal["stock", "pexels", "pixabay", "uploaded", "source"]
    provider_id: str = Field(default="", max_length=80)
    filename: str = Field(default="", max_length=180)
    duration_seconds: float = Field(default=5.0, ge=1.0, le=30.0)


class SceneAssemblyRequest(BaseModel):
    """Validate multi-scene assembly options."""

    scenes: list[SceneAssemblyClipInput] = Field(min_length=1, max_length=8)
    transition: Literal["cut", "fade"] = "cut"
    tts_provider: Literal["auto", "elevenlabs", "openai_tts", "none"] = "auto"
    tts_voice: str = Field(default="", max_length=80)
    bgm_volume: float = Field(default=0.18, ge=0.02, le=1.0)
    ducking: bool = True
    loudnorm: bool = False
    subtitle_style: SubtitleStyleInput | None = None


class PostprocessRequest(BaseModel):
    """Validate post-processing options for an uploaded or generated MP4."""

    apply_subtitles: bool = True
    apply_bgm: bool = True
    bgm_volume: float = Field(default=0.18, ge=0.02, le=1.0)
    ducking: bool = True
    loudnorm: bool = True
    tts_provider: Literal["none", "auto", "elevenlabs", "openai_tts"] = "none"
    tts_voice: str = Field(default="", max_length=80)
    subtitle_style: SubtitleStyleInput | None = None


class ThumbnailExtractRequest(BaseModel):
    """Validate extracting a cover frame timestamp from the current video."""

    timestamp_seconds: float = Field(default=0.5, ge=0.0, le=180.0)


class ApprovalRequest(BaseModel):
    """Require explicit human confirmation before the approved hash is saved."""

    confirm: bool
    human_reviewed: bool
    confirm_rights: bool = False


class RejectRequest(BaseModel):
    """Capture operator rejection feedback and optionally trigger automatic regeneration."""

    feedback: str = Field(min_length=2, max_length=2000)
    auto_reproduce: bool = True


class PublishRequest(BaseModel):
    """Validate the publication target and visibility choice."""

    platform: str
    confirm: bool
    privacy_status: str = "private"
    publish_at: datetime | None = None


class YouTubePrivacyUpdateRequest(BaseModel):
    """Validate updating a published YouTube video's visibility."""

    privacy_status: Literal["private", "unlisted", "public"]


class SecretsUpdateRequest(BaseModel):
    """Validate updating local .env secrets without returning raw secret values."""

    updates: dict[str, str] = Field(default_factory=dict)
    clear_keys: list[str] = Field(default_factory=list)


class DirectUrlResearchRequest(BaseModel):
    """Validate a public HTTPS URL for direct evidence extraction."""

    url: str = Field(min_length=10, max_length=2048)


class SourceInclusionRequest(BaseModel):
    """Validate toggling whether a collected source is included in planning prompts."""

    included: bool


class AIStageSelection(BaseModel):
    """Validate one AI provider and model pairing."""

    provider: Literal["openai_api", "chatgpt_oauth", "gemini_api", "gemini_oauth", "anthropic_api", "ollama"]
    model: str = Field(min_length=1, max_length=100)


class AISettingsUpdate(BaseModel):
    """Validate per-stage model selections and non-secret local provider settings."""

    planning: AIStageSelection
    review: AIStageSelection
    gemini_client_secrets_file: str = Field(default="", max_length=2048)
    gemini_google_cloud_project: str = Field(default="", max_length=128)
    ollama_base_url: str = Field(default="http://127.0.0.1:11434", max_length=300)


class BrandProfileInput(BaseModel):
    """Validate concise local brand guidance before it reaches planning prompts."""

    brand_name: str = Field(default="", max_length=100)
    brand_description: str = Field(default="", max_length=1000)
    target_audience: str = Field(default="", max_length=300)
    voice_and_tone: str = Field(default="", max_length=500)
    language: str = Field(default="Korean", max_length=50)
    preferred_terms: list[str] = Field(default_factory=list, max_length=20)
    prohibited_terms: list[str] = Field(default_factory=list, max_length=30)
    required_disclosures: list[str] = Field(default_factory=list, max_length=20)


class SeriesTemplateInput(BaseModel):
    """Validate one reusable format and its optional brief defaults."""

    id: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=80)
    format: str = Field(default="", max_length=1000)
    audience: str = Field(default="", max_length=300)
    tone: str = Field(default="", max_length=200)
    language: str = Field(default="", max_length=50)
    duration_seconds: int = Field(default=30, ge=4, le=30)
    platforms: list[Literal["youtube", "instagram", "tiktok"]] = Field(default_factory=lambda: ["youtube"], min_length=1, max_length=3)
    call_to_action: str = Field(default="", max_length=300)
    key_points: list[str] = Field(default_factory=list, max_length=15)


class BrandSettingsUpdate(BaseModel):
    """Validate the replace-on-save profile and bounded template library."""

    profile: BrandProfileInput
    series_templates: list[SeriesTemplateInput] = Field(default_factory=list, max_length=20)


class RightsAssetInput(BaseModel):
    """Validate an included asset's provenance and operator-reviewed rights."""

    id: str | None = Field(default=None, max_length=36)
    asset_name: str = Field(min_length=1, max_length=160)
    asset_type: Literal["final_video", "video", "image", "audio", "voice", "music", "other"] = "other"
    source_description: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2048)
    license_name: str = Field(default="", max_length=200)
    license_url: str = Field(default="", max_length=2048)
    rights_basis: str = Field(default="", max_length=500)
    attribution: str = Field(default="", max_length=500)
    license_expires_on: str | None = Field(default=None, max_length=10)
    library_asset_id: str | None = Field(default=None, max_length=36)
    included: bool = True
    rights_status: Literal["unverified", "cleared"] = "unverified"


class RightsAssetsUpdate(BaseModel):
    """Validate the complete bounded rights ledger for one unpublished item."""

    assets: list[RightsAssetInput] = Field(default_factory=list, max_length=50)


class AnalyticsSnapshotInput(BaseModel):
    """Validate one manually entered platform-performance snapshot."""

    measured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    views: int | None = Field(default=None, ge=0)
    watch_time_seconds: float | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    saves: int | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=500)


class PlatformVariantInput(BaseModel):
    """Validate the text fields that may differ for a selected platform."""

    title: str = Field(min_length=1, max_length=100)
    caption: str = Field(min_length=1, max_length=5000)
    hashtags: list[str] = Field(default_factory=list, max_length=20)


class AssetLibraryInput(BaseModel):
    """Validate reusable rights and provenance metadata for one media source."""

    id: str | None = Field(default=None, max_length=36)
    asset_name: str = Field(min_length=1, max_length=160)
    asset_type: Literal["video", "image", "audio", "voice", "music", "other"] = "other"
    source_description: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2048)
    license_name: str = Field(default="", max_length=200)
    license_url: str = Field(default="", max_length=2048)
    rights_basis: str = Field(default="", max_length=500)
    attribution: str = Field(default="", max_length=500)
    license_expires_on: str | None = Field(default=None, max_length=10)
    rights_status: Literal["unverified", "cleared"] = "unverified"


class ScheduleRequest(BaseModel):
    """Validate a deliberate platform publication time and visibility choice."""

    scheduled_at: datetime
    timezone_name: str = Field(min_length=1, max_length=100)
    confirm: bool
    privacy_status: Literal["private", "unlisted", "public"] = "private"


class ExperimentInput(BaseModel):
    """Validate a comparison between two already-published content variants."""

    name: str = Field(min_length=1, max_length=160)
    hypothesis: str = Field(default="", max_length=1000)
    platform: Literal["youtube", "instagram", "tiktok"]
    item_a: str = Field(min_length=36, max_length=36)
    item_b: str = Field(min_length=36, max_length=36)
    label_a: str = Field(default="A", min_length=1, max_length=40)
    label_b: str = Field(default="B", min_length=1, max_length=40)


class ExperimentStatusInput(BaseModel):
    """Validate an explicit active/completed state transition for an experiment."""

    status: Literal["active", "completed"]


class ArchiveRequest(BaseModel):
    """Validate an item archive toggle."""

    archived: bool


class SubtitleStyleInput(BaseModel):
    """Validate subtitle rendering style options."""

    preset: Literal["clean_bottom", "bold_center", "yellow_pop", "top_news"] = "clean_bottom"
    font_name: str = Field(default="", max_length=64)
    font_size: int = Field(default=42, ge=18, le=96)
    margin_v: int = Field(default=170, ge=10, le=600)
    outline: int = Field(default=3, ge=0, le=10)
    shadow: int = Field(default=1, ge=0, le=6)
    alignment: Literal[2, 5, 8] = 2
    primary_colour: str = Field(default="&H00FFFFFF", max_length=10)
    outline_colour: str = Field(default="&H00000000", max_length=10)


class SubtitleUpdateRequest(BaseModel):
    """Validate edited SRT captions and optional FFmpeg rerendering."""

    srt: str = Field(min_length=1, max_length=200000)
    style: SubtitleStyleInput = Field(default_factory=SubtitleStyleInput)
    rerender: bool = True


def _get_item(item_id: str) -> dict[str, Any]:
    """@brief Load an item or raise a not-found response."""
    item = store.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Content item not found.")
    return item


def _ensure_mutable(item: dict[str, Any]) -> None:
    """@brief Reject edits after publication so the audit trail stays stable."""
    if any(record.get("status") == "published" for record in item.get("publications", {}).values()):
        raise HTTPException(status_code=409, detail="This item has already been published; create a new item for another version.")


def _preflight_for_item(item: dict[str, Any]) -> dict[str, Any]:
    """@brief Recompute current deterministic publishing checks from saved item state."""
    review = item.get("review") if isinstance(item.get("review"), dict) else {}
    return build_publish_preflight(
        item.get("plan") if isinstance(item.get("plan"), dict) else {},
        item.get("platforms", []),
        review.get("technical") if isinstance(review.get("technical"), dict) else {"available": False, "passed": False},
        item.get("rights_assets") if isinstance(item.get("rights_assets"), list) else [],
        item.get("brand_profile_snapshot") if isinstance(item.get("brand_profile_snapshot"), dict) else {},
        item.get("platform_variants") if isinstance(item.get("platform_variants"), dict) else {},
    )


def _refresh_review_preflight(item: dict[str, Any]) -> dict[str, Any] | None:
    """@brief Persist a fresh preflight and merge its state into an existing review."""
    review = item.get("review")
    if not isinstance(review, dict):
        return None
    updated_review = dict(review)
    preflight = _preflight_for_item(item)
    base_status = str(updated_review.get("status_without_preflight") or updated_review.get("status") or "manual_required")
    updated_review["preflight"] = preflight
    updated_review["status"] = combine_review_status(base_status, preflight)
    store.update_payload(item["id"], {"review": updated_review})
    return updated_review


def _parse_timestamp(value: Any) -> datetime | None:
    """@brief Parse stored publication timestamps as timezone-aware UTC values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_expiry(value: str | None) -> str | None:
    """@brief Validate a license-expiry date while preserving its date-only form."""
    if value is None or value == "":
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(status_code=422, detail="License expiry must use YYYY-MM-DD.") from None


def _validate_publish_candidate(item: dict[str, Any], platform: str) -> Path:
    """@brief Verify that current media, rights, and platform copy still match approval.

    @param item Persisted workflow item.
    @param platform Selected publishing adapter.
    @returns The approved video path after all current-state checks pass.
    """
    if platform not in item.get("platforms", []):
        raise HTTPException(status_code=422, detail="Select this platform in the content brief first.")
    if not item.get("approved_at"):
        raise HTTPException(status_code=409, detail="Approve the reviewed video before publishing or scheduling.")
    assets = item.get("assets") or {}
    video_path = Path(assets.get("final_video", ""))
    if not video_path.is_file() or not item.get("approved_asset_sha256"):
        raise HTTPException(status_code=409, detail="The approved final MP4 is missing.")
    if sha256_file(video_path) != item["approved_asset_sha256"]:
        raise HTTPException(status_code=409, detail="The video changed after approval; review and approve it again.")
    plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
    variants = item.get("platform_variants") if isinstance(item.get("platform_variants"), dict) else {}
    approval = item.get("approval_record") if isinstance(item.get("approval_record"), dict) else {}
    if approval.get("copy_sha256") != copy_fingerprint(plan, variants):
        raise HTTPException(status_code=409, detail="Platform copy changed after approval; review and approve the current copy again.")
    if approval.get("rights_asset_sha256") != rights_assets_fingerprint(item.get("rights_assets", [])):
        raise HTTPException(status_code=409, detail="Asset-rights records changed after approval; review and approve again.")
    preflight = _preflight_for_item(item)
    if preflight["status"] != "ready":
        raise HTTPException(status_code=409, detail="Publishing is blocked by the current platform preflight; resolve its checks and approve again.")
    return video_path


def _scheduler_loop() -> None:
    """@brief Dispatch due approved schedules and periodically sync ready AI provider models."""
    global _last_model_sync_at
    while not _scheduler_stop.is_set():
        try:
            _dispatch_due_schedules()
        except Exception:
            # A single scheduler error must not permanently stop future due-time checks.
            pass
        now = time.monotonic()
        if now - _last_model_sync_at >= _MODEL_SYNC_INTERVAL_SECONDS:
            try:
                sync_ready_provider_models(force_refresh=True)
            except Exception:
                pass
            _last_model_sync_at = now
        _scheduler_stop.wait(10)


def _dispatch_due_schedules() -> None:
    """@brief Claim and dispatch due jobs without bypassing normal approval checks."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for scheduled in store.claim_due_schedules(now):
        item_id = scheduled["item_id"]
        platform = scheduled["platform"]
        try:
            item = _get_item(item_id)
            _validate_publish_candidate(item, platform)
            if platform == "youtube" and not youtube_scheduling_ready():
                raise HTTPException(status_code=503, detail="Scheduled YouTube publishing needs a saved refreshable OAuth session.")
            if platform == "instagram" and not instagram_ready():
                raise HTTPException(status_code=503, detail="Instagram credentials or public media storage are no longer ready.")
            if platform == "tiktok" and not tiktok_ready():
                raise HTTPException(status_code=503, detail="TIKTOK_ACCESS_TOKEN is no longer configured.")
            _schedule(
                item_id,
                "publishing",
                lambda selected_item=item_id, target=platform, privacy=scheduled["privacy_status"]: run_publication(store, selected_item, target, privacy),
                platform=platform,
                scheduled_platform=platform,
            )
        except HTTPException as error:
            if error.status_code == 409 and "Another generation" in str(error.detail):
                store.set_schedule_state(item_id, platform, "scheduled")
            else:
                store.set_schedule_state(item_id, platform, "failed", str(error.detail))
        except Exception as error:
            store.set_schedule_state(item_id, platform, "failed", f"Scheduled dispatch failed: {type(error).__name__}.")


def _run_worker(item_id: str, job_id: str, stage: str, action: Any, platform: str | None) -> None:
    """@brief Run one durable workflow action and release its ownership exactly once."""
    final_stage = stage
    error_message: str | None = None
    try:
        final_stage = action()
    except Exception as error:
        error_message = str(error)[:1600]
        final_stage = "approved" if platform else "failed"
        if platform:
            uncertain = isinstance(error, PublicationError) and error.uncertain
            store.finish_publication(item_id, platform, "uncertain" if uncertain else "failed", error=error_message)
    finally:
        try:
            if stage == "publishing" and platform:
                item = store.get_item(item_id) or {}
                publication = item.get("publications", {}).get(platform, {})
                schedule_status = {"published": "completed", "uncertain": "uncertain", "failed": "failed", "publishing": "uncertain"}.get(publication.get("status"))
                if schedule_status:
                    store.set_schedule_state(item_id, platform, schedule_status, publication.get("error"))
            store.finish_job(item_id, job_id, final_stage, error_message)
        finally:
            _worker_lock.release()


def _schedule(item_id: str, stage: str, action: Any, platform: str | None = None, scheduled_platform: str | None = None) -> str:
    """@brief Claim the app-wide worker and launch one non-duplicated operation."""
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another generation, review, or publishing job is running. Wait for it to finish.")
    job_id = ""
    previous_stage: str | None = None
    publication_reserved = False
    try:
        existing_item = store.get_item(item_id)
        if existing_item is None:
            raise KeyError(item_id)
        previous_stage = existing_item["stage"]
        job_id = store.claim_job(item_id, stage)
        if platform:
            store.reserve_publication(item_id, platform, allow_scheduled_dispatch=bool(scheduled_platform))
            publication_reserved = True
        if scheduled_platform:
            store.set_schedule_state(item_id, scheduled_platform, "running")
        worker = threading.Thread(target=_run_worker, args=(item_id, job_id, stage, action, platform), daemon=True, name=f"shortform-{stage}")
        worker.start()
    except KeyError:
        _worker_lock.release()
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except Exception as error:
        try:
            if job_id:
                try:
                    store.finish_job(item_id, job_id, previous_stage or "failed", str(error)[:1600])
                except Exception:
                    pass
            if publication_reserved and platform:
                try:
                    store.finish_publication(item_id, platform, "failed", error=f"Worker could not start: {type(error).__name__}.")
                except Exception:
                    pass
            if scheduled_platform:
                try:
                    store.set_schedule_state(item_id, scheduled_platform, "failed", f"Scheduled worker could not start: {type(error).__name__}.")
                except Exception:
                    pass
        finally:
            _worker_lock.release()
        status_code = 409 if isinstance(error, (RuntimeError, OSError)) else 500
        raise HTTPException(status_code=status_code, detail=str(error)[:1600]) from None
    return job_id


@app.middleware("http")
async def restrict_to_local_browser(request: Any, call_next: Any) -> Any:
    """@brief Restrict host and browser-origin headers to local and configured Tailscale hosts."""
    allowed, detail = is_allowed_request_origin(
        request.headers.get("host", ""),
        request.headers.get("origin"),
        get_secret("SHORTFORM_STUDIO_ALLOWED_HOSTS"),
    )
    if not allowed:
        return JSONResponse(status_code=403, content={"detail": detail})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data: https://images.pexels.com https://cdn.pixabay.com https://i.vimeocdn.com; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.on_event("startup")
def start_publication_scheduler() -> None:
    """@brief Start the single local scheduler thread once for this server process."""
    global _scheduler_thread
    _scheduler_stop.clear()
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="shortform-scheduler")
        _scheduler_thread.start()


@app.on_event("shutdown")
def stop_publication_scheduler() -> None:
    """@brief Stop due-time polling cleanly when the local web server exits."""
    _scheduler_stop.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=2)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """@brief Provide a credential-free liveness response for local and Compose runtimes."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    """@brief Serve the local content-studio interface."""
    return FileResponse(PROJECT_ROOT / "shortform_studio" / "static" / "index.html")


@app.get("/api/settings")
def settings() -> dict[str, Any]:
    """@brief Expose provider readiness without returning credentials."""
    return {
        "ai": ai_status(),
        "openai_ready": bool(get_secret("OPENAI_API_KEY") and get_secret("OPENAI_MODEL")),
        "openai_model": get_secret("OPENAI_MODEL"),
        "research_ready": bool(get_secret("TAVILY_API_KEY")),
        "stock_video_ready": bool(get_secret("PEXELS_API_KEY") or get_secret("PIXABAY_API_KEY")),
        "pexels_ready": bool(get_secret("PEXELS_API_KEY")),
        "pixabay_ready": bool(get_secret("PIXABAY_API_KEY")),
        "runway_ready": bool(get_secret("RUNWAY_API_KEY")),
        "runway_model": get_secret("RUNWAY_MODEL", "seedance2_5"),
        "elevenlabs_ready": bool(get_secret("ELEVENLABS_API_KEY") and get_secret("ELEVENLABS_VOICE_ID")),
        "openai_tts_ready": bool(get_secret("OPENAI_API_KEY")),
        "youtube_ready": youtube_ready(),
        "instagram_ready": instagram_ready(),
        "tiktok_ready": tiktok_ready(),
        "ffmpeg_ready": bool(shutil.which("ffmpeg")),
        "app_home": str(get_app_home()),
        "oauth_authorization_urls": get_authorization_urls(),
    }


@app.get("/api/settings/secrets")
def get_secrets_status() -> dict[str, bool]:
    """@brief Return configured status flags for managed .env keys without exposing secret values."""
    return get_managed_secrets_status()


@app.put("/api/settings/secrets")
def save_secrets_status(request: SecretsUpdateRequest) -> dict[str, bool]:
    """@brief Atomically merge updated API keys into the local .env file."""
    try:
        result = update_local_env_secrets(request.updates, clear_keys=request.clear_keys)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    clear_model_list_cache()
    return result


@app.post("/api/settings/test/{provider}")
def test_provider_endpoint(provider: str) -> dict[str, Any]:
    """@brief Run a lightweight connectivity check for an external API provider."""
    try:
        return test_provider_connection(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except RuntimeError as error:
        return {"ok": False, "message": str(error)}
    except RuntimeError as error:
        return {"ok": False, "message": str(error)}


@app.get("/api/brand")
def get_brand_settings() -> dict[str, Any]:
    """@brief Return the saved brand profile and reusable series templates."""
    raw = store.get_setting("brand_settings", default_brand_settings())
    try:
        return normalize_brand_settings(raw)
    except ValueError:
        raise HTTPException(status_code=500, detail="Saved brand settings are invalid; replace them in the brand settings dialog.") from None


@app.put("/api/brand")
def update_brand_settings(request: BrandSettingsUpdate) -> dict[str, Any]:
    """@brief Save validated brand guidance for newly created content items."""
    try:
        normalized = normalize_brand_settings(request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    store.set_setting("brand_settings", normalized)
    return normalized


@app.get("/api/analytics/summary")
def get_analytics_summary() -> dict[str, Any]:
    """@brief Aggregate only locally recorded metrics from successfully published items."""
    return summarize_analytics(store.list_items())


@app.get("/api/analytics/dashboard")
def get_analytics_dashboard() -> dict[str, Any]:
    """@brief Return platform totals, per-series summaries, and recent post rows."""
    return build_analytics_dashboard(store.list_items())


@app.get("/api/analytics/export.csv")
def export_analytics_csv_endpoint() -> PlainTextResponse:
    """@brief Export manually recorded platform metrics as a UTF-8 CSV download."""
    csv_body = export_analytics_csv(store.list_items())
    return PlainTextResponse(
        csv_body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="shortform-analytics.csv"'},
    )


@app.post("/api/analytics/import")
async def import_analytics_csv_endpoint(csv_file: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Import validated analytics snapshots from a CSV file for published posts."""
    raw = await csv_file.read(5_000_001)
    await csv_file.close()
    if len(raw) > 5_000_000:
        raise HTTPException(status_code=413, detail="CSV upload exceeds 5 MB limit.")
    try:
        rows = parse_analytics_csv(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    imported = 0
    skipped = 0
    for row in rows:
        item_id = row.pop("item_id")
        platform = row.pop("platform")
        try:
            store.append_analytics_snapshot(item_id, platform, row)
            imported += 1
        except (KeyError, RuntimeError, ValueError):
            skipped += 1
    return {"imported": imported, "skipped": skipped}


@app.get("/api/experiments")
def get_experiments() -> list[dict[str, Any]]:
    """@brief Return A/B comparisons built only from locally recorded metrics."""
    items = {item["id"]: item for item in store.list_items()}
    return [summarize_experiment(experiment, items) for experiment in store.list_experiments()]


@app.post("/api/experiments", status_code=201)
def create_experiment(request: ExperimentInput) -> dict[str, Any]:
    """@brief Link two published posts as explicitly labeled experiment variants."""
    for item_id in (request.item_a, request.item_b):
        try:
            if str(UUID(item_id)) != item_id.lower():
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail="Experiment item identifiers must be UUIDs.") from None
    pair = {request.item_a, request.item_b}
    for existing in store.list_experiments():
        if existing["platform"] == request.platform and {existing["item_a"], existing["item_b"]} == pair:
            raise HTTPException(status_code=409, detail="These two posts are already linked in an experiment.")
    try:
        experiment = store.create_experiment(request.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Experiment content item not found.") from None
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    items = {item["id"]: item for item in store.list_items()}
    return summarize_experiment(experiment, items)


@app.put("/api/experiments/{experiment_id}")
def update_experiment_status(experiment_id: str, request: ExperimentStatusInput) -> dict[str, Any]:
    """@brief Change an experiment's workflow state without rewriting observations."""
    experiment = store.set_experiment_status(experiment_id, request.status)
    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    items = {item["id"]: item for item in store.list_items()}
    return summarize_experiment(experiment, items)


@app.get("/api/assets/library")
def get_asset_library() -> list[dict[str, Any]]:
    """@brief Return reusable local asset provenance records."""
    return store.list_asset_library()


def _save_asset_library_entry(asset_id: str | None, request: AssetLibraryInput) -> dict[str, Any]:
    """@brief Validate identifiers, public URLs, license basis, and expiry for a reusable asset."""
    asset = request.model_dump()
    if asset_id:
        asset["id"] = asset_id
    else:
        asset["id"] = str(asset.get("id") or uuid4())
    try:
        if str(UUID(asset["id"])) != asset["id"].lower():
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Asset library IDs must be UUIDs.") from None
    asset["asset_name"] = asset["asset_name"].strip()
    if not asset["asset_name"]:
        raise HTTPException(status_code=422, detail="Asset name cannot be empty.")
    for field in ("source_url", "license_url"):
        candidate = asset[field].strip()
        if candidate and not validate_external_url(candidate):
            raise HTTPException(status_code=422, detail=f"{field} must be a public HTTPS URL without credentials or local-network hosts.")
        asset[field] = validate_external_url(candidate) if candidate else ""
    asset["license_expires_on"] = _normalize_expiry(asset.get("license_expires_on"))
    if asset["rights_status"] == "cleared" and not asset["rights_basis"].strip():
        raise HTTPException(status_code=422, detail="A cleared asset needs a concise rights basis, not only a license label.")
    return store.save_asset_library_entry(asset)


@app.post("/api/assets/library", status_code=201)
def create_asset_library_entry(request: AssetLibraryInput) -> dict[str, Any]:
    """@brief Save one reusable asset and its operator-reviewed license metadata."""
    return _save_asset_library_entry(None, request)


@app.put("/api/assets/library/{asset_id}")
def update_asset_library_entry(asset_id: str, request: AssetLibraryInput) -> dict[str, Any]:
    """@brief Update an existing reusable rights record without mutating past projects."""
    if not store.get_asset_library_entry(asset_id):
        raise HTTPException(status_code=404, detail="Asset library entry not found.")
    return _save_asset_library_entry(asset_id, request)


@app.delete("/api/assets/library/{asset_id}", status_code=204)
def delete_asset_library_entry(asset_id: str) -> None:
    """@brief Delete one reusable library record while leaving copied ledgers intact."""
    if not store.delete_asset_library_entry(asset_id):
        raise HTTPException(status_code=404, detail="Asset library entry not found.")


@app.post("/api/ai/settings")
def update_ai_settings(request: AISettingsUpdate) -> dict[str, Any]:
    """@brief Save non-secret model selections and return safe provider status."""
    try:
        save_ai_settings(request.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    clear_model_list_cache()
    return ai_status()


@app.get("/api/ai/models")
def get_ai_models(provider: str = "", refresh: bool = False) -> dict[str, Any]:
    """@brief Fetch live supported models from provider APIs without hardcoded catalogs."""
    if provider:
        try:
            return {"providers": {provider: list_provider_models(provider, force_refresh=refresh)}}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    return {"providers": list_all_provider_models(force_refresh=refresh)}


@app.post("/api/ai/auth/{provider}/connect", status_code=202)
def connect_ai_provider(provider: str) -> dict[str, str]:
    """@brief Start an explicit provider-owned OAuth login flow in the background."""
    if provider not in {"gemini", "chatgpt_oauth"}:
        raise HTTPException(status_code=404, detail="OAuth connection is not available for this provider.")
    try:
        return start_oauth_login(provider)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@app.post("/api/ai/auth/gemini/disconnect")
def disconnect_gemini_provider() -> dict[str, Any]:
    """@brief Remove this app's Gemini token without logging out shared CLI sessions."""
    disconnect_gemini_oauth()
    return ai_status()


@app.post("/api/ai/auth/chatgpt_oauth/disconnect")
def disconnect_chatgpt_provider() -> dict[str, Any]:
    """@brief Revoke the ChatGPT refresh session when possible and clear local credentials."""
    revocation_confirmed = disconnect_chatgpt_oauth()
    result = ai_status()
    result["chatgpt_revocation_confirmed"] = revocation_confirmed
    return result


@app.get("/api/items")
def list_items(
    q: str = "",
    stage: str = "",
    platform: str = "",
    include_archived: bool = True,
) -> list[dict[str, Any]]:
    """@brief Return recent content items for the local dashboard with optional filters."""
    return store.list_items(query=q, stage=stage, platform=platform, include_archived=include_archived)


@app.post("/api/items", status_code=201)
def create_item(request: NewItem) -> dict[str, Any]:
    """@brief Validate and persist a new content brief."""
    values = request.model_dump()
    if any(platform not in {"youtube", "instagram", "tiktok"} for platform in values["platforms"]):
        raise HTTPException(status_code=422, detail="Platforms must be youtube, instagram, and/or tiktok.")
    if len(set(values["platforms"])) != len(values["platforms"]):
        raise HTTPException(status_code=422, detail="Duplicate platform selections are not allowed.")
    values["topic"] = values["topic"].strip()
    if len(values["topic"]) < 3:
        raise HTTPException(status_code=422, detail="Topic must contain at least three non-space characters.")
    try:
        brand_settings = normalize_brand_settings(store.get_setting("brand_settings", default_brand_settings()))
        series_template = find_series_template(brand_settings, values.get("series_template_id"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    values["brand_profile_snapshot"] = brand_settings["profile"]
    values["series_template_snapshot"] = series_template
    return store.create_item(values)


@app.get("/api/items/{item_id}")
def get_item(item_id: str) -> dict[str, Any]:
    """@brief Return the full state and artifacts for a content item."""
    return _get_item(item_id)


@app.post("/api/items/{item_id}/duplicate", status_code=201)
def duplicate_item(item_id: str) -> dict[str, Any]:
    """@brief Clone an existing item's brief and plan into a fresh unapproved draft."""
    try:
        return store.duplicate_item(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None


@app.post("/api/items/{item_id}/archive")
def archive_item(item_id: str, request: ArchiveRequest) -> dict[str, Any]:
    """@brief Archive or unarchive an item in the local workspace."""
    try:
        return store.set_item_archived(item_id, request.archived)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None


@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: str) -> None:
    """@brief Delete an unpublished, idle item and remove its local media files."""
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Wait for the active workflow operation before deleting.")
    try:
        try:
            deleted = store.delete_item(item_id)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if not deleted:
            raise HTTPException(status_code=404, detail="Content item not found.")
        delete_item_media(item_id)
    finally:
        _worker_lock.release()


@app.get("/api/items/{item_id}/subtitles")
def get_item_subtitles(item_id: str) -> dict[str, Any]:
    """@brief Return the item's saved SRT captions and subtitle style configuration."""
    try:
        return get_subtitles_state(store, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None


@app.put("/api/items/{item_id}/subtitles")
def update_item_subtitles(item_id: str, request: SubtitleUpdateRequest) -> dict[str, Any]:
    """@brief Save edited SRT subtitles, optionally rerender MP4, and reset approval."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before updating subtitles.")
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Wait for the active workflow operation before rendering subtitles.")
    try:
        return save_subtitles_and_rerender(
            store,
            item_id,
            request.srt,
            request.style.model_dump(),
            rerender=request.rerender,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    finally:
        _worker_lock.release()


@app.put("/api/items/{item_id}/platform-copy/{platform}")
def update_platform_copy(item_id: str, platform: str, request: PlatformVariantInput) -> dict[str, Any]:
    """@brief Save channel-specific copy and invalidate any approval of older text."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if platform not in {"youtube", "instagram", "tiktok"} or platform not in item.get("platforms", []):
        raise HTTPException(status_code=422, detail="Select YouTube, Instagram, or TikTok for this content item first.")
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before editing platform copy.")
    title = request.title.strip()
    caption = request.caption.strip()
    if not title or not caption:
        raise HTTPException(status_code=422, detail="Platform title and caption cannot be blank.")
    hashtags: list[str] = []
    for raw_tag in request.hashtags:
        tag = raw_tag.strip().lstrip("#")
        if not tag:
            continue
        if any(character.isspace() for character in tag):
            raise HTTPException(status_code=422, detail="Hashtags cannot contain whitespace.")
        hashtags.append(f"#{tag}")
    if any(len(tag) > 100 for tag in hashtags):
        raise HTTPException(status_code=422, detail="Each hashtag must be at most 100 characters.")
    candidate = {"title": title, "caption": caption, "hashtags": hashtags}
    current_variants = dict(item.get("platform_variants") or {})
    if current_variants.get(platform) == candidate:
        return item
    store.save_revision_snapshot(item_id, f"{platform} platform copy edited")
    current_variants[platform] = candidate
    variants = current_variants
    store.update_payload(item_id, {"platform_variants": variants, "review": None, "reviewed_asset_sha256": None, "approved_at": None, "approved_asset_sha256": None, "approval_record": None})
    updated = _get_item(item_id)
    _refresh_review_preflight(updated)
    if item.get("stage") in {"approved", "reviewed"}:
        store.set_stage(item_id, "generated")
    return _get_item(item_id)


@app.get("/api/items/{item_id}/versions")
def get_item_versions(item_id: str) -> list[dict[str, Any]]:
    """@brief Return bounded immutable editorial snapshots for one item."""
    item = _get_item(item_id)
    return item.get("version_history", []) if isinstance(item.get("version_history"), list) else []


@app.post("/api/items/{item_id}/versions/{version_id}/restore")
def restore_item_version(item_id: str, version_id: str) -> dict[str, Any]:
    """@brief Restore a prior text/rights snapshot as a new unapproved revision."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before restoring a version.")
    history = item.get("version_history") if isinstance(item.get("version_history"), list) else []
    snapshot = next((entry for entry in history if entry.get("id") == version_id), None)
    if not snapshot or not isinstance(snapshot.get("plan"), dict):
        raise HTTPException(status_code=404, detail="Content version not found.")
    store.save_revision_snapshot(item_id, "Current state saved before version restore")
    previous_plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
    restored_plan = dict(snapshot["plan"])
    plan_revision = int(item.get("plan_revision", previous_plan.get("revision", 0))) + 1
    assets = dict(item.get("assets") or {})
    video_matches = can_reuse_video_for_plan(previous_plan, restored_plan, assets)
    restored_plan["revision"] = plan_revision
    if video_matches and assets.get("final_video"):
        assets["generated_for_plan_revision"] = plan_revision
    store.update_payload(item_id, {
        "plan": restored_plan,
        "plan_revision": plan_revision,
        "platform_variants": snapshot.get("platform_variants") if isinstance(snapshot.get("platform_variants"), dict) else {},
        "rights_assets": snapshot.get("rights_assets") if isinstance(snapshot.get("rights_assets"), list) else [],
        "assets": assets,
        "review": None,
        "approved_at": None,
        "approved_asset_sha256": None,
        "reviewed_asset_sha256": None,
        "approval_record": None,
    })
    store.set_stage(item_id, "generated" if video_matches and assets.get("final_video") else "planned")
    return _get_item(item_id)


@app.put("/api/items/{item_id}/rights")
def update_item_rights(item_id: str, request: RightsAssetsUpdate) -> dict[str, Any]:
    """@brief Save provenance and clear prior approval when rights evidence changes."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before editing asset rights.")
    assets: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for asset_model in request.assets:
        asset = asset_model.model_dump()
        asset_id = str(asset.get("id") or uuid4())
        try:
            canonical_id = str(UUID(asset_id))
        except ValueError:
            canonical_id = ""
        if canonical_id != asset_id.lower() or asset_id.lower() in identifiers:
            raise HTTPException(status_code=422, detail="Asset IDs must be unique UUID-shaped values.")
        asset_id = canonical_id
        identifiers.add(asset_id)
        asset["id"] = asset_id
        asset["license_expires_on"] = _normalize_expiry(asset.get("license_expires_on"))
        library_id = asset.get("library_asset_id")
        if library_id:
            try:
                if str(UUID(library_id)) != library_id.lower():
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail="Library asset references must be UUIDs.") from None
        for field in ("source_url", "license_url"):
            candidate = asset[field].strip()
            if candidate and not validate_external_url(candidate):
                raise HTTPException(status_code=422, detail=f"{field} must be a public HTTPS URL without credentials or local-network hosts.")
            asset[field] = validate_external_url(candidate) if candidate else ""
        if asset["rights_status"] == "cleared" and not asset["rights_basis"].strip():
            raise HTTPException(status_code=422, detail="A cleared asset needs a concise rights basis, not only a license label.")
        assets.append(asset)
    if assets != item.get("rights_assets", []):
        store.save_revision_snapshot(item_id, "Asset-rights ledger edited")
    store.update_payload(
        item_id,
        {"rights_assets": assets, "approved_at": None, "approved_asset_sha256": None, "approval_record": None},
    )
    updated = _get_item(item_id)
    _refresh_review_preflight(updated)
    if item.get("stage") == "approved":
        store.set_stage(item_id, "reviewed")
    return _get_item(item_id)


@app.post("/api/items/{item_id}/analytics/{platform}", status_code=201)
def record_analytics(item_id: str, platform: str, request: AnalyticsSnapshotInput) -> dict[str, Any]:
    """@brief Append a manually measured snapshot for a successfully published target."""
    item = _get_item(item_id)
    if platform not in {"youtube", "instagram", "tiktok"}:
        raise HTTPException(status_code=404, detail="Unsupported analytics platform.")
    publication = item.get("publications", {}).get(platform)
    if not publication or publication.get("status") != "published":
        raise HTTPException(status_code=409, detail="Metrics can be recorded only after this platform publication succeeds.")
    snapshot = request.model_dump()
    measured_at = snapshot["measured_at"]
    if measured_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="Metric measurement time must include a timezone.")
    measured_at = measured_at.astimezone(timezone.utc)
    published_at = _parse_timestamp(publication.get("updated_at"))
    if published_at and measured_at < published_at:
        raise HTTPException(status_code=422, detail="Metric measurement time cannot be earlier than publication.")
    if measured_at > datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="Metric measurement time cannot be more than five minutes in the future.")
    metric_values = {key: snapshot.get(key) for key in ("views", "watch_time_seconds", "likes", "comments", "shares", "saves")}
    if all(value is None for value in metric_values.values()):
        raise HTTPException(status_code=422, detail="Enter at least one platform metric.")
    snapshot["measured_at"] = measured_at.isoformat(timespec="seconds")
    try:
        return store.append_analytics_snapshot(item_id, platform, snapshot)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.post("/api/items/{item_id}/plan", status_code=202)
def start_planning(item_id: str) -> dict[str, str]:
    """@brief Start idea and script planning without blocking the dashboard."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    job_id = _schedule(item_id, "planning", lambda: run_planning(store, item_id))
    return {"job_id": job_id, "status": "planning"}


@app.post("/api/items/{item_id}/research", status_code=202)
def start_web_research(item_id: str) -> dict[str, str]:
    """@brief Start explicit topic research and source extraction for one item."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not get_secret("TAVILY_API_KEY"):
        raise HTTPException(status_code=503, detail="Configure TAVILY_API_KEY before collecting web sources.")
    prior_stage = str(item.get("stage") or "draft")
    job_id = _schedule(item_id, "researching", lambda: run_web_research(store, item_id, prior_stage))
    return {"job_id": job_id, "status": "researching"}


@app.post("/api/items/{item_id}/research/url")
def add_url_to_research(item_id: str, request: DirectUrlResearchRequest) -> dict[str, Any]:
    """@brief Fetch a single public HTTPS URL directly into the item's citable sources."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    try:
        return add_direct_url_research(store, item_id, request.url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.put("/api/items/{item_id}/research/sources/{source_id}")
def update_research_source_inclusion(item_id: str, source_id: str, request: SourceInclusionRequest) -> dict[str, Any]:
    """@brief Include or exclude a collected research source from planning prompts."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    try:
        return toggle_research_source_inclusion(store, item_id, source_id, request.included)
    except KeyError:
        raise HTTPException(status_code=404, detail="Research source not found.") from None


@app.get("/api/items/{item_id}/progress")
def get_item_progress(item_id: str) -> dict[str, Any]:
    """@brief Return active job progress and cooperative cancellation state."""
    item = _get_item(item_id)
    return {
        "stage": item.get("stage"),
        "active_job": item.get("active_job"),
        "progress": item.get("job_progress"),
        "cancel_requested": bool(item.get("cancel_requested")),
    }


@app.post("/api/items/{item_id}/cancel")
def cancel_item_job(item_id: str) -> dict[str, Any]:
    """@brief Request cooperative cancellation for an in-progress workflow job."""
    try:
        updated = store.request_job_cancel(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return {"cancel_requested": bool(updated.get("cancel_requested"))}


@app.post("/api/items/{item_id}/materials", status_code=202)
def start_material_search(item_id: str) -> dict[str, str]:
    """@brief Start explicit stock-footage discovery for generated plan scenes."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not isinstance(item.get("plan"), dict):
        raise HTTPException(status_code=409, detail="Generate a plan with scenes before searching for video materials.")
    if not (get_secret("PIXABAY_API_KEY") or get_secret("PEXELS_API_KEY")):
        raise HTTPException(status_code=503, detail="Configure PIXABAY_API_KEY or PEXELS_API_KEY before searching stock videos.")
    prior_stage = str(item.get("stage") or "planned")
    job_id = _schedule(item_id, "collecting_materials", lambda: run_stock_material_search(store, item_id, prior_stage))
    return {"job_id": job_id, "status": "collecting_materials"}


@app.put("/api/items/{item_id}/plan")
def edit_plan(item_id: str, request: PlanEdit) -> dict[str, Any]:
    """@brief Save human plan edits and invalidate any earlier review or approval."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before editing.")
    if not isinstance(item.get("plan"), dict):
        raise HTTPException(status_code=409, detail="Generate a plan before editing it.")
    try:
        payload = request.model_dump()
        if payload.get("scenes") is None:
            payload.pop("scenes", None)
        revise_plan(store, item_id, payload)
    except (KeyError, RuntimeError):
        raise HTTPException(status_code=409, detail="The plan changed while it was being edited; refresh and try again.") from None
    return _get_item(item_id)


@app.post("/api/items/{item_id}/plan/hooks")
def suggest_item_hooks(item_id: str) -> dict[str, Any]:
    """@brief Generate alternative opening hook suggestions for the current plan."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not isinstance(item.get("plan"), dict):
        raise HTTPException(status_code=409, detail="Generate a plan before requesting hook variations.")
    try:
        hooks = run_hook_suggestions(store, item_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return {"hooks": hooks}


@app.post("/api/items/{item_id}/plan/autofix")
def autofix_item_plan(item_id: str) -> dict[str, Any]:
    """@brief Ask the planning model to revise the draft using latest review feedback."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before auto-fixing.")
    try:
        return run_plan_autofix(store, item_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@app.post("/api/items/{item_id}/materials/{provider_id}/download")
def download_material_clip(item_id: str, provider_id: str) -> dict[str, Any]:
    """@brief Download one Pexels stock video candidate locally for multi-scene assembly."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    try:
        return download_stock_material_clip(store, item_id, provider_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Stock video candidate not found.") from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.post("/api/items/{item_id}/clips", status_code=201)
async def upload_scene_clip(item_id: str, clip: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Upload a local MP4/MOV/image clip for multi-scene FFmpeg assembly."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    raw_name = Path(clip.filename or "clip.mp4").name
    ext = Path(raw_name).suffix.lower()
    if ext not in {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"}:
        await clip.close()
        raise HTTPException(status_code=415, detail="Upload an MP4, MOV, JPG, PNG, or WEBP clip.")
    clips_dir = item_directory(item_id) / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(ch for ch in Path(raw_name).stem if ch.isalnum() or ch in {"-", "_"})[:40] or "clip"
    stored_name = f"{safe_stem}_{uuid4().hex[:8]}{ext}"
    destination = clips_dir / stored_name
    total = 0
    try:
        with destination.open("wb") as output:
            while chunk := await clip.read(1024 * 1024):
                total += len(chunk)
                if total > 250_000_000:
                    raise HTTPException(status_code=413, detail="Clip upload limit is 250 MB.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="Uploaded clip is empty.")
        assets = dict(item.get("assets") or {})
        uploaded_clips = list(assets.get("uploaded_clips") or [])
        uploaded_clips.append({"filename": stored_name, "original_name": raw_name, "media_type": "image" if ext in {".jpg", ".jpeg", ".png", ".webp"} else "video"})
        assets["uploaded_clips"] = uploaded_clips[-30:]
        return store.update_payload(item_id, {"assets": assets})
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await clip.close()


@app.post("/api/items/{item_id}/bgm", status_code=201)
async def upload_bgm(item_id: str, audio: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Upload a background music track and record an unverified rights ledger entry."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    raw_name = Path(audio.filename or "bgm.mp3").name
    ext = Path(raw_name).suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".aac"}:
        await audio.close()
        raise HTTPException(status_code=415, detail="Upload an MP3, WAV, M4A, or AAC audio file.")
    folder = item_directory(item_id)
    destination = folder / f"bgm{ext}"
    total = 0
    try:
        with destination.open("wb") as output:
            while chunk := await audio.read(1024 * 1024):
                total += len(chunk)
                if total > 100_000_000:
                    raise HTTPException(status_code=413, detail="BGM upload limit is 100 MB.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="Uploaded audio file is empty.")
        assets = dict(item.get("assets") or {})
        assets["bgm_path"] = str(destination)
        assets["bgm_name"] = raw_name
        rights_assets = list(item.get("rights_assets") or [])
        rights_assets.append(
            {
                "id": str(uuid4()),
                "asset_name": f"BGM: {raw_name}"[:160],
                "asset_type": "music",
                "source_description": f"Operator-uploaded BGM ({raw_name})"[:300],
                "source_url": "",
                "license_name": "",
                "license_url": "",
                "rights_basis": "",
                "attribution": "",
                "included": True,
                "rights_status": "unverified",
            }
        )
        return store.update_payload(item_id, {"assets": assets, "rights_assets": rights_assets, "approved_at": None, "approval_record": None})
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await audio.close()


@app.post("/api/items/{item_id}/assemble", status_code=202)
def start_scene_assembly(item_id: str, request: SceneAssemblyRequest) -> dict[str, str]:
    """@brief Start FFmpeg multi-scene video assembly across selected clips and audio."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not isinstance(item.get("plan"), dict):
        raise HTTPException(status_code=409, detail="Generate a plan before assembling scenes.")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="FFmpeg is required for multi-scene assembly.")
    scene_dicts = [entry.model_dump() for entry in request.scenes]
    style_dict = request.subtitle_style.model_dump() if request.subtitle_style else None
    job_id = _schedule(
        item_id,
        "generating",
        lambda: run_scene_assembly(
            store,
            item_id,
            scene_dicts,
            transition=request.transition,
            tts_provider=request.tts_provider,
            tts_voice=request.tts_voice,
            bgm_volume=request.bgm_volume,
            ducking=request.ducking,
            loudnorm=request.loudnorm,
            subtitle_style=style_dict,
        ),
    )
    return {"job_id": job_id, "status": "generating"}


@app.post("/api/items/{item_id}/postprocess")
def postprocess_video(item_id: str, request: PostprocessRequest) -> dict[str, Any]:
    """@brief Apply narration, BGM ducking, loudness normalization, and subtitles to an existing MP4."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before post-processing.")
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Wait for the active workflow operation before post-processing.")
    try:
        return run_postprocess_video(
            store,
            item_id,
            apply_subtitles=request.apply_subtitles,
            apply_bgm=request.apply_bgm,
            bgm_volume=request.bgm_volume,
            ducking=request.ducking,
            loudnorm=request.loudnorm,
            tts_provider=request.tts_provider,
            tts_voice=request.tts_voice,
            subtitle_style=request.subtitle_style.model_dump() if request.subtitle_style else None,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    finally:
        _worker_lock.release()


@app.post("/api/items/{item_id}/thumbnail/extract")
def extract_item_thumbnail(item_id: str, request: ThumbnailExtractRequest) -> dict[str, Any]:
    """@brief Extract a cover thumbnail frame from the item's saved MP4."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    try:
        return save_thumbnail_from_video(store, item_id, request.timestamp_seconds)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.post("/api/items/{item_id}/thumbnail", status_code=201)
async def upload_item_thumbnail(item_id: str, image: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Upload a custom JPG or PNG cover thumbnail for the item."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    raw_name = Path(image.filename or "thumbnail.jpg").name
    ext = Path(raw_name).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png"}:
        await image.close()
        raise HTTPException(status_code=415, detail="Upload a JPG or PNG thumbnail image.")
    folder = item_directory(item_id)
    destination = folder / f"thumbnail{'.png' if ext == '.png' else '.jpg'}"
    total = 0
    try:
        with destination.open("wb") as output:
            while chunk := await image.read(1024 * 1024):
                total += len(chunk)
                if total > 10_000_000:
                    raise HTTPException(status_code=413, detail="Thumbnail upload limit is 10 MB.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="Uploaded thumbnail is empty.")
        assets = dict(item.get("assets") or {})
        assets["thumbnail_path"] = str(destination)
        return store.update_payload(item_id, {"assets": assets})
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await image.close()


@app.get("/api/items/{item_id}/thumbnail")
def get_item_thumbnail(item_id: str) -> FileResponse:
    """@brief Serve the saved cover thumbnail image from the item directory."""
    item = _get_item(item_id)
    thumb_path = Path(str((item.get("assets") or {}).get("thumbnail_path") or ""))
    try:
        resolved = thumb_path.resolve(strict=True)
        resolved.relative_to(item_directory(item_id).resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Saved thumbnail not found.") from None
    media_type = "image/png" if resolved.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(resolved, media_type=media_type)


@app.post("/api/items/{item_id}/generate", status_code=202)
def start_generation(item_id: str) -> dict[str, str]:
    """@brief Start explicit paid video and optional narration generation."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not item.get("plan"):
        raise HTTPException(status_code=409, detail="Generate a plan before creating video.")
    if not get_secret("RUNWAY_API_KEY"):
        raise HTTPException(status_code=503, detail="Configure RUNWAY_API_KEY before video generation.")
    job_id = _schedule(item_id, "generating", lambda: run_video_generation(store, item_id))
    return {"job_id": job_id, "status": "generating"}


@app.post("/api/items/{item_id}/video", status_code=201)
async def upload_video(item_id: str, video: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Save a bounded operator-supplied MP4 and invalidate previous review."""
    if not _worker_lock.acquire(blocking=False):
        await video.close()
        raise HTTPException(status_code=409, detail="Wait for the active workflow operation before uploading.")
    temporary: Path | None = None
    try:
        item = _get_item(item_id)
        _ensure_mutable(item)
        if not item.get("plan"):
            raise HTTPException(status_code=409, detail="Generate a plan before uploading its MP4.")
        if item.get("active_job"):
            raise HTTPException(status_code=409, detail="Wait for the active operation before uploading.")
        filename = Path(video.filename or "video.mp4").name
        if Path(filename).suffix.lower() != ".mp4" or video.content_type not in {"video/mp4", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Upload an MP4 video file.")
        folder = item_directory(item_id)
        temporary = folder / "upload.tmp"
        final = folder / "final.mp4"
        total = 0
        with temporary.open("wb") as output:
            while chunk := await video.read(1024 * 1024):
                total += len(chunk)
                if total > 500_000_000:
                    raise HTTPException(status_code=413, detail="Video upload limit is 500 MB.")
                output.write(chunk)
        with temporary.open("rb") as uploaded:
            if b"ftyp" not in uploaded.read(64):
                raise HTTPException(status_code=415, detail="The uploaded file does not look like an MP4 container.")
        with temporary.open("rb") as uploaded:
            if b"ftyp" not in uploaded.read(64):
                raise HTTPException(status_code=415, detail="The uploaded file does not look like an MP4 container.")
        temporary.replace(final)
        assets = {
            "source_video": str(final),
            "final_video": str(final),
            "sha256": sha256_file(final),
            "generated_for_plan_revision": (item.get("plan") or {}).get("revision"),
            "video_provider": {"provider": "operator_upload", "original_filename": filename},
            "voiceover": None,
        }
        rights_assets = replace_final_video_rights_record(item.get("rights_assets"), f"operator-uploaded file: {filename}")
        store.cancel_pending_schedules(item_id, "Video file replaced; review and schedule the new artifact after approval.")
        store.update_payload(item_id, {"assets": assets, "rights_assets": rights_assets, "review": None, "approved_at": None, "approved_asset_sha256": None, "reviewed_asset_sha256": None, "approval_record": None})
        store.set_stage(item_id, "generated")
        return _get_item(item_id)
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        try:
            await video.close()
        finally:
            _worker_lock.release()


@app.post("/api/items/{item_id}/review", status_code=202)
def start_review(item_id: str) -> dict[str, str]:
    """@brief Start technical and optional editorial checks for the saved draft."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if not item.get("plan") or not (item.get("assets") or {}).get("final_video"):
        raise HTTPException(status_code=409, detail="A plan and MP4 are required before review.")
    if (item.get("assets") or {}).get("generated_for_plan_revision") != (item.get("plan") or {}).get("revision"):
        raise HTTPException(status_code=409, detail="The saved video is from an older plan. Generate or upload a video for the current plan first.")
    job_id = _schedule(item_id, "reviewing", lambda: run_content_review(store, item_id))
    return {"job_id": job_id, "status": "reviewing"}


@app.post("/api/items/{item_id}/autopilot", status_code=202)
def start_autopilot(item_id: str) -> dict[str, str]:
    """@brief Run planning, stock/video production, TTS/subtitles, and review automatically."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    store.update_payload(item_id, {"workflow_mode": "autopilot"})
    job_id = _schedule(item_id, "planning", lambda: run_autopilot_pipeline(store, item_id))
    return {"job_id": job_id, "status": "planning"}


@app.post("/api/items/{item_id}/reject", status_code=202)
def reject_item(item_id: str, request: RejectRequest) -> Any:
    """@brief Record operator rejection feedback and optionally re-run the autopilot pipeline."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before rejecting.")
    try:
        updated = record_rejection_feedback(store, item_id, request.feedback)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    if not request.auto_reproduce:
        return JSONResponse(status_code=200, content=updated)
    job_id = _schedule(item_id, "planning", lambda: run_autopilot_pipeline(store, item_id, feedback=request.feedback))
    return {"job_id": job_id, "status": "planning"}


@app.post("/api/items/{item_id}/approve")
def approve_item(item_id: str, request: ApprovalRequest) -> dict[str, Any]:
    """@brief Bind explicit human approval to the current plan revision and file hash."""
    item = _get_item(item_id)
    _ensure_mutable(item)
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before approving.")
    if not request.confirm or not request.human_reviewed:
        raise HTTPException(status_code=422, detail="Explicit confirmation and manual inspection are required.")
    plan = item.get("plan") or {}
    review = item.get("review") or {}
    assets = item.get("assets") or {}
    video_path = Path(assets.get("final_video", ""))
    if not video_path.is_file():
        raise HTTPException(status_code=409, detail="The final MP4 is missing.")
    if review.get("plan_revision") != plan.get("revision") or review.get("asset_sha256") != sha256_file(video_path):
        raise HTTPException(status_code=409, detail="The plan or video changed after review; run review again.")
    if request.confirm_rights:
        confirmed_rights = confirm_item_rights_for_approval(item)
        store.update_payload(item_id, {"rights_assets": confirmed_rights})
        item = _get_item(item_id)
    current_hash = sha256_file(video_path)
    preflight = _preflight_for_item(item)
    review = dict(review)
    review["preflight"] = preflight
    base_status = str(review.get("status_without_preflight") or review.get("status") or "manual_required")
    review["status"] = combine_review_status(base_status, preflight)
    store.update_payload(item_id, {"review": review})
    if preflight["status"] != "ready":
        raise HTTPException(status_code=409, detail="Resolve all blocked platform and rights checks in the review panel before approval.")
    technical = review.get("technical") or {}
    if technical.get("available") and not technical.get("passed"):
        raise HTTPException(status_code=409, detail="The video failed technical media checks; fix media issues before approval.")
    autopilot_override = item.get("workflow_mode") == "autopilot" and request.confirm_rights
    if review.get("status") == "needs_changes" and not autopilot_override:
        raise HTTPException(status_code=409, detail="The review found a failed technical or editorial check. Fix the listed issues and review again before approval.")
    if review.get("status") not in {"passed", "manual_required"} and not autopilot_override:
        raise HTTPException(status_code=409, detail="Run a successful or manual-required review before approval.")
    approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    approval_record = {"plan_revision": plan.get("revision"), "asset_sha256": current_hash, "rights_asset_sha256": rights_assets_fingerprint(item.get("rights_assets", [])), "copy_sha256": copy_fingerprint(plan, item.get("platform_variants", {})), "review_status": review.get("status"), "manual_reviewed": True}
    store.record_approval(item_id, approved_at, approval_record)
    return _get_item(item_id)


@app.post("/api/items/{item_id}/publish", status_code=202)
def start_publish(item_id: str, request: PublishRequest) -> dict[str, str]:
    """@brief Start one user-confirmed platform publication after exact-file approval."""
    item = _get_item(item_id)
    if not request.confirm:
        raise HTTPException(status_code=422, detail="Explicit publication confirmation is required.")
    _validate_publish_candidate(item, request.platform)
    scheduled = item.get("schedules", {}).get(request.platform, {})
    if scheduled.get("status") in {"scheduled", "dispatching", "running"}:
        raise HTTPException(status_code=409, detail="Cancel the pending scheduled post before publishing immediately.")
    existing_publication = item.get("publications", {}).get(request.platform, {})
    if existing_publication.get("status") in {"publishing", "published", "uncertain"}:
        raise HTTPException(status_code=409, detail=f"This platform publication is already {existing_publication['status']}; inspect the channel before taking further action.")
    if request.platform == "youtube" and not youtube_ready():
        raise HTTPException(status_code=503, detail="Configure a YouTube OAuth Desktop client before publishing.")
    if request.platform == "instagram" and not instagram_ready():
        raise HTTPException(status_code=503, detail="Configure Instagram publishing and public R2 storage first.")
    if request.platform == "tiktok" and not tiktok_ready():
        raise HTTPException(status_code=503, detail="Configure TIKTOK_ACCESS_TOKEN before publishing to TikTok.")
    if request.platform == "youtube" and request.privacy_status not in {"private", "unlisted", "public"}:
        raise HTTPException(status_code=422, detail="Invalid YouTube privacy selection.")
    publish_at_iso: str | None = None
    if request.publish_at is not None:
        if request.platform != "youtube":
            raise HTTPException(status_code=422, detail="Native publish_at is supported only for YouTube.")
        if request.publish_at.tzinfo is None or request.publish_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail="YouTube publish_at must include a time zone.")
        publish_at_utc = request.publish_at.astimezone(timezone.utc)
        if publish_at_utc <= datetime.now(timezone.utc) + timedelta(minutes=1):
            raise HTTPException(status_code=422, detail="YouTube publish_at must be in the future.")
        publish_at_iso = publish_at_utc.isoformat(timespec="seconds")
    job_id = _schedule(
        item_id,
        "publishing",
        lambda: run_publication(store, item_id, request.platform, request.privacy_status, publish_at=publish_at_iso),
        platform=request.platform,
    )
    return {"job_id": job_id, "status": "publishing"}


@app.patch("/api/items/{item_id}/youtube/privacy")
def patch_youtube_privacy(item_id: str, request: YouTubePrivacyUpdateRequest) -> dict[str, Any]:
    """@brief Change visibility for an already-published YouTube video."""
    try:
        return change_youtube_privacy(store, item_id, request.privacy_status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except (RuntimeError, PublicationError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.post("/api/items/{item_id}/r2/cleanup")
def cleanup_item_r2(item_id: str) -> dict[str, Any]:
    """@brief Manually remove a staged R2 MP4 left over after Instagram upload."""
    try:
        return cleanup_instagram_r2_object(store, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.get("/api/schedules")
def get_publication_schedules() -> list[dict[str, Any]]:
    """@brief List current and historical platform publication schedule states."""
    return store.list_schedules()


@app.post("/api/items/{item_id}/schedule/{platform}", status_code=201)
def schedule_publication(item_id: str, platform: str, request: ScheduleRequest) -> dict[str, Any]:
    """@brief Schedule a future post only after checking its exact approved state."""
    item = _get_item(item_id)
    if not request.confirm:
        raise HTTPException(status_code=422, detail="Explicit confirmation is required to schedule a post.")
    if item.get("active_job"):
        raise HTTPException(status_code=409, detail="Wait for the active operation before scheduling publication.")
    if platform not in {"youtube", "instagram", "tiktok"}:
        raise HTTPException(status_code=422, detail="Unsupported publishing platform.")
    _validate_publish_candidate(item, platform)
    try:
        ZoneInfo(request.timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Choose a valid IANA time zone.") from None
    scheduled_at = request.scheduled_at
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        raise HTTPException(status_code=422, detail="Scheduled time must include a time zone.")
    scheduled_utc = scheduled_at.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if scheduled_utc < now + timedelta(minutes=1) or scheduled_utc > now + timedelta(days=365):
        raise HTTPException(status_code=422, detail="Choose a schedule between one minute and 365 days from now.")
    if platform == "youtube" and not youtube_scheduling_ready():
        raise HTTPException(status_code=503, detail="Save a refreshable YouTube OAuth session before scheduling unattended publication.")
    if platform == "instagram" and not instagram_ready():
        raise HTTPException(status_code=503, detail="Configure Instagram publishing and public media storage before scheduling.")
    if platform == "tiktok" and not tiktok_ready():
        raise HTTPException(status_code=503, detail="Configure TIKTOK_ACCESS_TOKEN before scheduling TikTok publication.")
    privacy_status = request.privacy_status if platform == "youtube" else "private"
    try:
        return store.schedule_publication(item_id, platform, scheduled_utc.isoformat(timespec="seconds"), request.timezone_name, privacy_status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content item not found.") from None
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None


@app.delete("/api/items/{item_id}/schedule/{platform}", status_code=204)
def cancel_publication_schedule(item_id: str, platform: str) -> None:
    """@brief Cancel only a schedule that has not started publication dispatch."""
    if not store.cancel_schedule(item_id, platform):
        raise HTTPException(status_code=409, detail="The schedule is absent or already being processed.")


@app.get("/api/items/{item_id}/video")
def get_video(item_id: str) -> FileResponse:
    """@brief Stream the current saved MP4 from inside the item media directory."""
    item = _get_item(item_id)
    video_path = Path((item.get("assets") or {}).get("final_video", ""))
    try:
        resolved = video_path.resolve(strict=True)
        resolved.relative_to(item_directory(item_id).resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Saved video not found.") from None
    return FileResponse(resolved, media_type="video/mp4", filename="shortform-draft.mp4")


@app.get("/api/backup")
def download_backup() -> FileResponse:
    """@brief Download a consistent ZIP backup of the database and project media."""
    if not _worker_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Wait for the active workflow operation before creating a backup.")
    archive_path: Path | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix="shortform-backup-", suffix=".zip", dir=get_app_home())
        os.close(descriptor)
        archive_path = Path(temporary_path)
        create_backup_archive(get_database_path(), get_projects_dir(), archive_path)
    except Exception:
        if archive_path:
            archive_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not create the local project backup.") from None
    finally:
        _worker_lock.release()
    if archive_path is None:
        raise HTTPException(status_code=500, detail="Could not create the local project backup.")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"shortform-studio-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.post("/api/backup/restore")
async def restore_backup(backup: UploadFile = File(...)) -> dict[str, Any]:
    """@brief Validate and restore a ZIP backup while preserving a pre-restore recovery copy."""
    filename = Path(backup.filename or "backup.zip").name
    if Path(filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=415, detail="Upload a Shortform Studio ZIP backup.")
    staging_dir = get_app_home() / ".restore-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(prefix="incoming-", suffix=".zip", dir=staging_dir)
    os.close(descriptor)
    archive_path = Path(temporary_path)
    total = 0
    try:
        with archive_path.open("wb") as output:
            while chunk := await backup.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BACKUP_BYTES:
                    raise HTTPException(status_code=413, detail="Backup upload limit is 10 GB.")
                output.write(chunk)
        if not _worker_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="Wait for the active workflow operation before restoring a backup.")
        try:
            if any(item.get("active_job") for item in store.list_items()):
                raise HTTPException(status_code=409, detail="A workflow operation is active; backup restore is blocked.")
            try:
                recovery_path = restore_backup_archive(get_database_path(), get_projects_dir(), archive_path, get_app_home() / "recovery")
            except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, json.JSONDecodeError, sqlite3.Error) as error:
                raise HTTPException(status_code=422, detail=f"Backup restore failed validation or file replacement: {type(error).__name__}.") from None
            store._initialize()
            store.recover_interrupted_jobs()
            store.recover_interrupted_schedules()
            return {"status": "restored", "recovery_archive": recovery_path.name if recovery_path else None, "scheduled_posts": "canceled; review and schedule them again"}
        finally:
            _worker_lock.release()
    finally:
        archive_path.unlink(missing_ok=True)
        await backup.close()
