"""User-local, non-secret model selection and provider configuration."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .config import get_app_home, get_secret

AI_PROVIDERS = {"openai_api", "chatgpt_oauth", "gemini_oauth", "anthropic_api", "ollama"}
DEFAULT_MODELS = {
    "openai_api": "gpt-4.1-mini",
    "chatgpt_oauth": "gpt-6.1-sol",
    "gemini_oauth": "gemini-2.5-flash",
    "anthropic_api": "claude-sonnet-4-5",
    "ollama": "qwen3:8b",
}
_LEGACY_PROVIDERS = {"codex_cli": "chatgpt_oauth", "claude_code": "anthropic_api"}
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}\Z")
_CONFIG_LOCK = threading.RLock()


def ai_settings_path() -> Path:
    """@brief Return the location of the local, non-secret AI settings file."""
    return get_app_home() / "ai-settings.json"


def _default_stage(provider: str, model: str) -> dict[str, str]:
    """@brief Build one stage's default provider selection."""
    return {"provider": provider, "model": model}


def default_ai_settings() -> dict[str, object]:
    """@brief Return provider defaults while preserving the legacy OpenAI model setting."""
    openai_model = get_secret("OPENAI_MODEL") or DEFAULT_MODELS["openai_api"]
    return {
        "planning": _default_stage("openai_api", openai_model),
        "review": _default_stage("openai_api", openai_model),
        "gemini_client_secrets_file": "",
        "gemini_google_cloud_project": "",
        "ollama_base_url": get_secret("SHORTFORM_STUDIO_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
    }


def _normalize_settings(value: object) -> dict[str, object]:
    """@brief Normalize persisted settings and discard malformed or unknown values."""
    defaults = default_ai_settings()
    if not isinstance(value, dict):
        return defaults
    normalized: dict[str, object] = dict(defaults)
    for stage in ("planning", "review"):
        candidate = value.get(stage)
        if not isinstance(candidate, dict):
            continue
        provider = candidate.get("provider")
        model = candidate.get("model")
        provider = _LEGACY_PROVIDERS.get(provider, provider)
        if provider == "anthropic_api" and model == "sonnet":
            model = DEFAULT_MODELS["anthropic_api"]
        if provider in AI_PROVIDERS and isinstance(model, str) and _MODEL_PATTERN.fullmatch(model.strip()):
            normalized[stage] = {"provider": provider, "model": model.strip()}
    for key in ("gemini_client_secrets_file", "gemini_google_cloud_project", "ollama_base_url"):
        candidate = value.get(key)
        if isinstance(candidate, str) and len(candidate) <= 2048:
            try:
                normalized[key] = validate_ollama_url(candidate) if key == "ollama_base_url" else candidate.strip()
            except ValueError:
                normalized[key] = defaults[key]
    return normalized


def load_ai_settings() -> dict[str, object]:
    """@brief Load local AI settings without exposing OAuth tokens or API keys."""
    with _CONFIG_LOCK:
        path = ai_settings_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_ai_settings()
        return _normalize_settings(payload)


def validate_ollama_url(value: str) -> str:
    """@brief Restrict local-model requests to loopback or the explicitly enabled Docker host gateway."""
    parsed = urlsplit(value.strip())
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    # Compose opts into this single hostname so the container can reach host Ollama without permitting arbitrary remote endpoints.
    if os.environ.get("SHORTFORM_STUDIO_ALLOW_HOST_OLLAMA") == "1":
        allowed_hosts.add("host.docker.internal")
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in allowed_hosts:
        raise ValueError("Ollama must use a localhost URL, or host.docker.internal in the Docker Compose runtime.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Ollama URL must not include credentials, a path, query, or fragment.")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Ollama URL has an invalid port.") from None
    return value.strip().rstrip("/")


def save_ai_settings(payload: dict[str, object]) -> dict[str, object]:
    """@brief Validate and atomically save model selections and non-secret OAuth setup."""
    defaults = default_ai_settings()
    normalized: dict[str, object] = {}
    for stage in ("planning", "review"):
        candidate = payload.get(stage)
        if not isinstance(candidate, dict):
            raise ValueError(f"The {stage} model selection is required.")
        provider = candidate.get("provider")
        model = candidate.get("model")
        if provider not in AI_PROVIDERS:
            raise ValueError(f"Unsupported provider for {stage}.")
        if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model.strip()):
            raise ValueError(f"Enter a valid model ID for {stage}.")
        normalized[stage] = {"provider": provider, "model": model.strip()}
    client_file = payload.get("gemini_client_secrets_file", "")
    project_id = payload.get("gemini_google_cloud_project", "")
    if not isinstance(client_file, str) or len(client_file) > 2048 or "\n" in client_file or "\r" in client_file:
        raise ValueError("Gemini OAuth client file path is invalid.")
    if not isinstance(project_id, str) or len(project_id) > 128 or "\n" in project_id or "\r" in project_id:
        raise ValueError("Google Cloud project ID is invalid.")
    ollama_url = payload.get("ollama_base_url", defaults["ollama_base_url"])
    if not isinstance(ollama_url, str):
        raise ValueError("Ollama URL is invalid.")
    normalized["gemini_client_secrets_file"] = client_file.strip()
    normalized["gemini_google_cloud_project"] = project_id.strip()
    normalized["ollama_base_url"] = validate_ollama_url(ollama_url)
    with _CONFIG_LOCK:
        path = ai_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(temporary, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                json.dump(normalized, output, ensure_ascii=False, indent=2)
                output.write("\n")
            temporary.replace(path)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise
    return normalized
