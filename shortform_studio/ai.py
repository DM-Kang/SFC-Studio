"""Structured text generation across API, OAuth CLI, and local AI providers."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .ai_config import load_ai_settings
from .ai_providers import generate_text, get_provider_statuses, provider_ready
from .config import get_secret

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def request_json(url: str, *, method: str = "POST", headers: dict[str, str], body: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    """@brief Send a JSON request and return its decoded object response."""
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Provider response bodies may echo request data, so only expose the status.
        raise RuntimeError(f"Provider request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Provider request failed: {type(error).__name__}.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Provider returned a response with an unexpected shape.")
    return result


def extract_response_text(response: dict[str, Any]) -> str:
    """@brief Extract text output from a Responses API response object."""
    direct_text = response.get("output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()
    collected: list[str] = []
    for output_item in response.get("output", []):
        if not isinstance(output_item, dict) or output_item.get("type") != "message":
            continue
        for content_item in output_item.get("content", []):
            if isinstance(content_item, dict) and content_item.get("type") in {"output_text", "text"}:
                text_value = content_item.get("text")
                if isinstance(text_value, str):
                    collected.append(text_value)
    text = "\n".join(collected).strip()
    if not text:
        raise RuntimeError("The text model returned no usable text output.")
    return text


def selected_model(stage: str) -> tuple[str, str]:
    """@brief Return the provider and model configured for planning or review."""
    if stage not in {"planning", "review"}:
        raise ValueError("AI stage must be planning or review.")
    selection = load_ai_settings()[stage]
    return str(selection["provider"]), str(selection["model"])


def ai_ready(stage: str) -> bool:
    """@brief Check whether a configured provider can serve the requested stage."""
    provider, model = selected_model(stage)
    return provider_ready(provider, model)


def ai_status() -> dict[str, Any]:
    """@brief Return model selections and credential-free provider readiness state."""
    config = load_ai_settings()
    providers = get_provider_statuses()
    stages: dict[str, dict[str, Any]] = {}
    for stage in ("planning", "review"):
        selection = config[stage]
        provider = str(selection["provider"])
        status = providers.get(provider, {})
        ready = bool(status.get("ready"))
        if provider == "ollama":
            models = status.get("models", [])
            ready = ready and (selection["model"] in models or f"{selection['model']}:latest" in models)
        stages[stage] = {"provider": provider, "model": selection["model"], "ready": ready}
    return {"config": config, "providers": providers, "stages": stages}


def generate_json(system_prompt: str, user_prompt: str, stage: str = "planning") -> tuple[dict[str, Any], str]:
    """@brief Generate one JSON object using the model selected for a workflow stage."""
    provider, model = selected_model(stage)
    if not provider_ready(provider, model):
        raise RuntimeError(f"The configured {provider} provider is not connected or ready.")
    if provider == "openai_api":
        api_key = get_secret("OPENAI_API_KEY")
        response = request_json(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            body={"model": model, "instructions": system_prompt, "input": user_prompt},
        )
        text = extract_response_text(response)
    else:
        text = generate_text(provider, model, system_prompt, user_prompt)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"The text model did not return valid JSON at character {error.pos}.") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("The text model response must be a JSON object.")
    return parsed, f"{provider}:{model}"
