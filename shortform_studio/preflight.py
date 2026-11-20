"""Deterministic publishing checks that run before human approval and upload."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .platform_content import instagram_caption

YOUTUBE_TITLE_MAX_CHARS = 100
YOUTUBE_DESCRIPTION_MAX_BYTES = 5_000
INSTAGRAM_CAPTION_MAX_CHARS = 2_200


def _check(platform: str, code: str, status: str, message: str) -> dict[str, str]:
    """@brief Create a display-safe preflight result with a stable check code."""
    return {"platform": platform, "code": code, "status": status, "message": message}


def build_publish_preflight(
    plan: dict[str, Any],
    platforms: list[str],
    technical: dict[str, Any],
    rights_assets: list[dict[str, Any]],
    brand_profile: dict[str, Any],
    platform_variants: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """@brief Check media, platform metadata, brand terms, and asset-rights status.

    @param plan Current saved content plan.
    @param platforms Selected platform identifiers.
    @param technical Result from the local media probe.
    @param rights_assets Operator-maintained records for included material.
    @param brand_profile Brand guidance snapshotted when the item was created.
    @returns Timestamped checks and a blocked or ready aggregate status.
    """
    checks: list[dict[str, str]] = []
    variants = platform_variants if isinstance(platform_variants, dict) else {}
    all_copy = [plan]
    all_copy.extend(value for value in variants.values() if isinstance(value, dict))
    content = " ".join(str(value.get(key) or "") for value in all_copy for key in ("title", "hook", "script", "caption"))
    hashtags = " ".join(
        str(tag)
        for value in all_copy
        for tag in (value.get("hashtags") if isinstance(value.get("hashtags"), list) else [])
    )
    content = f"{content} {hashtags}"
    folded_content = content.casefold()
    for term in brand_profile.get("prohibited_terms", []):
        if isinstance(term, str) and term and term.casefold() in folded_content:
            checks.append(_check("all", "brand_prohibited_term", "blocked", f"Draft contains a prohibited brand term: {term[:80]}"))
    for disclosure in brand_profile.get("required_disclosures", []):
        if not isinstance(disclosure, str) or not disclosure:
            continue
        for platform in platforms or ["all"]:
            copy = variants.get(platform) if isinstance(variants.get(platform), dict) else {}
            public_copy = " ".join((str(plan.get("script") or ""), str(copy.get("caption", plan.get("caption")) or ""))).casefold()
            if disclosure.casefold() not in public_copy:
                checks.append(_check(platform, "required_disclosure", "blocked", f"Required disclosure is missing from the script and caption: {disclosure[:150]}"))
    included_assets = [asset for asset in rights_assets if asset.get("included", True)]
    if not included_assets:
        checks.append(_check("all", "rights_records", "blocked", "Register at least one included final-video or source asset and confirm its rights."))
    for asset in included_assets:
        name = str(asset.get("asset_name") or "Unnamed asset")[:100]
        expiry = asset.get("license_expires_on")
        if isinstance(expiry, str) and expiry:
            try:
                expired = datetime.strptime(expiry[:10], "%Y-%m-%d").date() < datetime.now(timezone.utc).date()
            except ValueError:
                expired = True
            if expired:
                checks.append(_check("all", "license_expired", "blocked", f"License expiry is invalid or has passed for: {name}"))
        if asset.get("rights_status") != "cleared":
            checks.append(_check("all", "asset_rights", "blocked", f"Rights are not confirmed for: {name}"))
        elif not str(asset.get("rights_basis") or "").strip():
            checks.append(_check("all", "rights_basis", "blocked", f"Record a rights basis for: {name}"))
    if not technical.get("available"):
        checks.append(_check("all", "media_probe", "warning", "Media properties could not be verified; inspect the video manually."))
    elif not technical.get("passed"):
        checks.append(_check("all", "media_probe", "blocked", str(technical.get("message") or "The video failed a technical check.")))
    else:
        checks.append(_check("all", "media_probe", "passed", "The video passed the configured local format, size, and duration checks."))

    if "youtube" in platforms:
        youtube_copy = variants.get("youtube") if isinstance(variants.get("youtube"), dict) else plan
        title = str(youtube_copy.get("title") or "")
        caption = str(youtube_copy.get("caption") or "")
        checks.append(_check("youtube", "title_required", "passed" if title.strip() else "blocked", "YouTube title is present." if title.strip() else "YouTube title is required."))
        title_valid = len(title) <= YOUTUBE_TITLE_MAX_CHARS and not any(char in title for char in "<>")
        checks.append(_check("youtube", "title_limit", "passed" if title_valid else "blocked", "YouTube title is within the API limit." if title_valid else "YouTube title must be at most 100 characters and cannot contain < or >."))
        description_bytes = len(caption.encode("utf-8"))
        description_valid = description_bytes <= YOUTUBE_DESCRIPTION_MAX_BYTES and not any(char in caption for char in "<>")
        checks.append(_check("youtube", "description_limit", "passed" if description_valid else "blocked", "YouTube description is within the API limit." if description_valid else f"YouTube description is {description_bytes} UTF-8 bytes; maximum is 5000 bytes and < or > are not allowed."))
    if "instagram" in platforms:
        instagram_copy = variants.get("instagram") if isinstance(variants.get("instagram"), dict) else plan
        caption = instagram_caption({**plan, **instagram_copy})
        caption_valid = len(caption) <= INSTAGRAM_CAPTION_MAX_CHARS
        checks.append(_check("instagram", "caption_limit", "passed" if caption_valid else "blocked", "Instagram caption is within this app's 2200-character input limit." if caption_valid else "Instagram caption exceeds this app's 2200-character input limit."))
    valid_platforms = {"youtube", "instagram"}
    for platform in platforms:
        if platform not in valid_platforms:
            checks.append(_check(platform, "platform_supported", "blocked", "This platform has no configured publishing adapter."))
    overall = "blocked" if any(result["status"] == "blocked" for result in checks) else "ready"
    return {"status": overall, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "checks": checks}


def combine_review_status(base_status: str, preflight: dict[str, Any]) -> str:
    """@brief Keep content review blocked while any deterministic preflight fails."""
    if preflight.get("status") == "blocked":
        return "needs_changes"
    return base_status


def rights_assets_fingerprint(rights_assets: list[dict[str, Any]]) -> str:
    """@brief Bind human approval to a canonical snapshot of the reviewed rights ledger."""
    encoded = json.dumps(rights_assets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
