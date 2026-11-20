"""Validation and defaults for local brand guidance and series templates."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

BRAND_TEXT_LIMITS = {
    "brand_name": 100,
    "brand_description": 1_000,
    "target_audience": 300,
    "voice_and_tone": 500,
    "language": 50,
}
TEMPLATE_TEXT_LIMITS = {
    "name": 80,
    "format": 1_000,
    "audience": 300,
    "tone": 200,
    "language": 50,
    "call_to_action": 300,
}
PROFILE_LIST_FIELD_LIMITS = {"preferred_terms": (20, 80), "prohibited_terms": (30, 80), "required_disclosures": (20, 150)}
TEMPLATE_LIST_FIELD_LIMITS = {"key_points": (15, 180)}
ALLOWED_PLATFORMS = {"youtube", "instagram", "tiktok"}


def default_brand_settings() -> dict[str, Any]:
    """@brief Return a fresh, empty profile and series-template collection."""
    return {
        "profile": {
            "brand_name": "",
            "brand_description": "",
            "target_audience": "",
            "voice_and_tone": "",
            "language": "Korean",
            "preferred_terms": [],
            "prohibited_terms": [],
            "required_disclosures": [],
        },
        "series_templates": [],
    }


def _clean_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    """@brief Trim one bounded text field and reject invalid values."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} is required.")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters.")
    return result


def _clean_string_list(value: Any, field: str, maximum_items: int, maximum_chars: int) -> list[str]:
    """@brief Validate bounded lists used as explicit brand or series guidance."""
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"{field} must be a list with at most {maximum_items} entries.")
    cleaned = [_clean_text(entry, field, maximum_chars, required=True) for entry in value]
    if len({entry.casefold() for entry in cleaned}) != len(cleaned):
        raise ValueError(f"{field} entries must be unique.")
    return cleaned


def normalize_brand_settings(value: Any) -> dict[str, Any]:
    """@brief Normalize and validate settings before durable storage or prompt use.

    @param value Candidate settings received from the local settings form.
    @returns A bounded profile and up to twenty stable series templates.
    """
    if not isinstance(value, dict) or not isinstance(value.get("profile"), dict):
        raise ValueError("Brand settings must contain a profile object.")
    raw_profile = value["profile"]
    profile = {
        key: _clean_text(raw_profile.get(key, "Korean" if key == "language" else ""), key, maximum)
        for key, maximum in BRAND_TEXT_LIMITS.items()
    }
    for key, (maximum_items, maximum_chars) in PROFILE_LIST_FIELD_LIMITS.items():
        profile[key] = _clean_string_list(raw_profile.get(key, []), key, maximum_items, maximum_chars)

    raw_templates = value.get("series_templates", [])
    if not isinstance(raw_templates, list) or len(raw_templates) > 20:
        raise ValueError("At most twenty series templates can be saved.")
    templates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_template in enumerate(raw_templates):
        if not isinstance(raw_template, dict):
            raise ValueError("Each series template must be an object.")
        template_id = _clean_text(raw_template.get("id") or str(uuid4()), "template id", 36, required=True).lower()
        try:
            canonical_id = str(UUID(template_id))
        except ValueError:
            canonical_id = ""
        if canonical_id != template_id or template_id in seen_ids:
            raise ValueError("Series template IDs must be unique UUID-shaped values.")
        seen_ids.add(template_id)
        template = {"id": template_id}
        for key, maximum in TEMPLATE_TEXT_LIMITS.items():
            template[key] = _clean_text(raw_template.get(key, ""), f"series_templates[{index}].{key}", maximum, required=key == "name")
        duration = raw_template.get("duration_seconds", 30)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 4 <= duration <= 30:
            raise ValueError("Series template duration must be from 4 to 30 seconds.")
        template["duration_seconds"] = duration
        platforms = raw_template.get("platforms", ["youtube"])
        if not isinstance(platforms, list) or not platforms or len(platforms) > 3 or any(platform not in ALLOWED_PLATFORMS for platform in platforms) or len(set(platforms)) != len(platforms):
            raise ValueError("Series template platforms must be unique YouTube, Instagram, and/or TikTok selections.")
        template["platforms"] = platforms
        template["key_points"] = _clean_string_list(raw_template.get("key_points", []), f"series_templates[{index}].key_points", *TEMPLATE_LIST_FIELD_LIMITS["key_points"])
        templates.append(template)
    return {"profile": profile, "series_templates": templates}


def find_series_template(settings: dict[str, Any], template_id: str | None) -> dict[str, Any] | None:
    """@brief Find a saved template by stable ID without returning mutable state."""
    if not template_id:
        return None
    for template in settings.get("series_templates", []):
        if template.get("id") == template_id:
            return dict(template)
    raise ValueError("The selected series template no longer exists; refresh the template list.")
