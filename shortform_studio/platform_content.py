"""Platform-specific copy normalization and approval fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def platform_plan(plan: dict[str, Any], variants: dict[str, Any], platform: str) -> dict[str, Any]:
    """@brief Overlay one platform's approved copy on the shared production plan.

    @param plan Shared script and production plan.
    @param variants Saved platform-specific copy keyed by platform.
    @param platform Target adapter identifier.
    @returns A detached plan containing the selected platform's copy.
    """
    result = dict(plan)
    variant = variants.get(platform) if isinstance(variants, dict) else None
    if isinstance(variant, dict):
        for key in ("title", "caption", "hashtags"):
            if key in variant:
                result[key] = list(variant[key]) if key == "hashtags" and isinstance(variant[key], list) else variant[key]
    return result


def instagram_caption(plan: dict[str, Any]) -> str:
    """@brief Compose Instagram's actual caption with the selected hashtags.

    @param plan Shared plan overlaid with Instagram-specific copy.
    @returns Caption text ready for the Instagram adapter.
    """
    caption = str(plan.get("caption") or "").strip()
    existing = {token.casefold().strip(".,!?;:()[]{}") for token in caption.split()}
    tags: list[str] = []
    for raw_tag in plan.get("hashtags", []) if isinstance(plan.get("hashtags"), list) else []:
        value = str(raw_tag).strip().lstrip("#")
        tag = f"#{value}" if value else ""
        if tag and tag.casefold() not in existing and tag.casefold() not in {entry.casefold() for entry in tags}:
            tags.append(tag)
    return f"{caption}\n\n{' '.join(tags)}".strip()


def tiktok_title(plan: dict[str, Any]) -> str:
    """@brief Compose TikTok post title/caption with selected hashtags."""
    base = str(plan.get("caption") or plan.get("title") or "").strip()
    existing = {token.casefold().strip(".,!?;:()[]{}") for token in base.split()}
    tags: list[str] = []
    for raw_tag in plan.get("hashtags", []) if isinstance(plan.get("hashtags"), list) else []:
        value = str(raw_tag).strip().lstrip("#")
        tag = f"#{value}" if value else ""
        if tag and tag.casefold() not in existing and tag.casefold() not in {entry.casefold() for entry in tags}:
            tags.append(tag)
    return f"{base} {' '.join(tags)}".strip()


def copy_fingerprint(plan: dict[str, Any], variants: dict[str, Any]) -> str:
    """@brief Hash shared and platform-specific publishing text for approval binding.

    @param plan Shared content plan.
    @param variants Platform-specific titles, captions, and hashtags.
    @returns SHA-256 digest of canonical publishing-copy JSON.
    """
    encoded = json.dumps(
        {"plan": {key: plan.get(key) for key in ("title", "hook", "script", "caption", "hashtags")}, "variants": variants},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
