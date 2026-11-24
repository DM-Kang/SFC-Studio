"""Pydantic request validation models for the Shortform Content Studio FastAPI endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class SubtitleStyleInput(BaseModel):
    """Validate subtitle rendering style options."""

    preset: Literal["clean_bottom", "bold_center", "yellow_pop", "top_news", "shorts_punch", "clean_pill"] = "clean_bottom"
    font_name: str = Field(default="", max_length=64)
    font_size: int = Field(default=42, ge=18, le=96)
    margin_v: int = Field(default=170, ge=10, le=600)
    outline: int = Field(default=3, ge=0, le=10)
    shadow: int = Field(default=1, ge=0, le=6)
    alignment: Literal[2, 5, 8] = 2
    primary_colour: str = Field(default="&H00FFFFFF", max_length=10)
    outline_colour: str = Field(default="&H00000000", max_length=10)


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
    style_template_id: str | None = Field(default=None, max_length=64)
    workflow_mode: Literal["manual", "autopilot"] = "manual"
    visual_mode: Literal["hybrid", "ai_images", "omni_video", "news_photos", "stock"] = "hybrid"
    use_attached_media: bool = True
    tts_speed: float = Field(default=1.35, ge=0.75, le=2.0)
    tts_voice: str = Field(default="", max_length=64)


class RetimeVideoRequest(BaseModel):
    """Adjust finished video playback/narration speed while keeping subtitles and audio synced."""

    speed_multiplier: float = Field(default=1.5, ge=0.75, le=2.0)


class StyleTemplateDiscoverRequest(BaseModel):
    """Trigger immediate discovery of 1M+ Korean YouTube Shorts style templates."""

    query: str = Field(default="", max_length=120)
    max_new: int = Field(default=2, ge=1, le=5)


class StyleTemplateSettingsRequest(BaseModel):
    """Configure continuous background 1M+ YouTube Shorts style discovery."""

    auto_sync_enabled: bool = True
    sync_interval_hours: int = Field(default=6, ge=1, le=72)


class ItemStyleTemplateRequest(BaseModel):
    """Assign or clear the style template snapshot on an existing item."""

    style_template_id: str | None = Field(default=None, max_length=64)


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
    tts_provider: Literal["auto", "gemini_tts", "elevenlabs", "openai_tts", "none"] = "auto"
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
    tts_provider: Literal["none", "auto", "gemini_tts", "elevenlabs", "openai_tts"] = "none"
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
    visual_mode: Literal["hybrid", "ai_images", "omni_video", "news_photos", "stock"] | None = None
    style_template_id: str | None = Field(default=None, max_length=64)
    tts_voice: str | None = Field(default=None, max_length=64)
    reuse_visuals: bool = False
    reuse_bgm: bool = False


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


class ShortsReferenceCreate(BaseModel):
    """Validate a YouTube Shorts URL and optional operator notes for style learning."""

    url: str = Field(min_length=15, max_length=512)
    notes: str = Field(default="", max_length=500)
    manual_transcript: str = Field(default="", max_length=6000)


class ShortsReferenceToggle(BaseModel):
    """Toggle whether a learned YouTube Shorts reference guides new plans."""

    active_for_planning: bool


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


class SubtitleUpdateRequest(BaseModel):
    """Validate edited SRT captions and optional FFmpeg rerendering."""

    srt: str = Field(min_length=1, max_length=200000)
    style: SubtitleStyleInput = Field(default_factory=SubtitleStyleInput)
    rerender: bool = True
