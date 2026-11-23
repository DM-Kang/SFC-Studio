"""Provider adapters and OAuth session management for text generation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .ai_config import load_ai_settings
from .oauth_callbacks import clear_authorization_url, oauth_callback_server, set_authorization_url, wait_for_oauth_callback

GEMINI_OAUTH_SCOPES = ["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/generative-language.retriever"]
CHATGPT_AUTHORIZATION_URL = "https://auth.openai.com/api/accounts/authorize"
CHATGPT_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
CHATGPT_JWKS_URL = "https://auth.openai.com/.well-known/jwks.json"
CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_RESOURCE = "https://api.openai.com/v1"
CHATGPT_REQUIRED_SCOPES = {"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}
CHATGPT_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_CHATGPT_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MODEL_LIST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_LOCK = threading.RLock()
_LOGIN_LOCK = threading.RLock()
_CHATGPT_REFRESH_LOCK = threading.RLock()
_LOGIN_STATE: dict[str, dict[str, str]] = {}


def _gemini_token_ready() -> bool:
    """@brief Check for a locally stored refreshable Gemini OAuth token."""
    try:
        token = json.loads(_unprotect_token(gemini_oauth_token_path().read_bytes()).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        return False
    return isinstance(token, dict) and bool(token.get("refresh_token"))


def chatgpt_oauth_token_path() -> Path:
    """@brief Return the app-owned ChatGPT OAuth credential location."""
    from .config import get_app_home

    return get_app_home() / "chatgpt-oauth-token.bin"


def chatgpt_profile_path() -> Path:
    """@brief Return the local profile that keeps the ChatGPT host and issued client IDs stable."""
    from .config import get_app_home

    return get_app_home() / "chatgpt-profile.json"


def _chatgpt_token_ready() -> bool:
    """@brief Check that a stored ChatGPT token includes direct-plan permission and renewal data."""
    try:
        token = _load_chatgpt_tokens()
    except RuntimeError:
        return False
    return bool(token.get("access_token") and token.get("refresh_token"))


def gemini_oauth_token_path() -> Path:
    """@brief Return the separate app-owned Gemini OAuth token location."""
    from .config import get_app_home

    return get_app_home() / "gemini-oauth-token.bin"


def _protect_token(payload: bytes) -> bytes:
    """@brief Encrypt saved provider credentials with Windows user-scope DPAPI when available."""
    if os.name != "nt":
        return payload
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        """@brief Represent a Windows DPAPI byte buffer."""

        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(payload)
    source = DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    destination = DataBlob()
    protect_data = ctypes.windll.crypt32.CryptProtectData
    protect_data.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    protect_data.restype = wintypes.BOOL
    if not protect_data(ctypes.byref(source), "Shortform Content Studio", None, None, None, 0x1, ctypes.byref(destination)):
        raise RuntimeError("Windows could not protect the local OAuth credential.")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(destination.pbData)


def _unprotect_token(payload: bytes) -> bytes:
    """@brief Decrypt this app's DPAPI credential only for the current Windows user."""
    if os.name != "nt":
        return payload
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        """@brief Represent a Windows DPAPI byte buffer."""

        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(payload)
    source = DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    destination = DataBlob()
    unprotect_data = ctypes.windll.crypt32.CryptUnprotectData
    unprotect_data.argtypes = [ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DataBlob), wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    unprotect_data.restype = wintypes.BOOL
    if not unprotect_data(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(destination)):
        raise RuntimeError("Windows could not decrypt the local OAuth credential.")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(destination.pbData)


def _atomic_write_private_file(path: Path, payload: bytes) -> None:
    """@brief Replace an app-owned credential file atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
        temporary.replace(path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _load_chatgpt_profile() -> dict[str, str]:
    """@brief Load or initialize the persistent opaque host ID and issued ChatGPT client ID."""
    path = chatgpt_profile_path()
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        token_path = chatgpt_oauth_token_path()
        if token_path.is_file():
            try:
                saved = json.loads(_unprotect_token(token_path.read_bytes()).decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
                raise RuntimeError("The local ChatGPT profile is missing and its credentials cannot restore it; reconnect this app.") from None
            if not isinstance(saved, dict) or not isinstance(saved.get("host_id"), str) or not isinstance(saved.get("client_id"), str):
                raise RuntimeError("The local ChatGPT profile is missing and its credentials cannot restore it; reconnect this app.")
            profile = {"host_id": saved["host_id"], "client_id": saved["client_id"]}
        else:
            profile = {"host_id": f"urn:uuid:{uuid.uuid4()}", "client_id": ""}
            _save_chatgpt_profile(profile)
            return profile
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("The local ChatGPT profile is unreadable; restore or remove chatgpt-profile.json and reconnect.") from None
    if not isinstance(profile, dict) or not isinstance(profile.get("host_id"), str) or not profile["host_id"].startswith("urn:uuid:"):
        raise RuntimeError("The local ChatGPT profile is invalid; restore or remove chatgpt-profile.json and reconnect.")
    try:
        uuid.UUID(profile["host_id"][9:])
    except ValueError:
        raise RuntimeError("The local ChatGPT host ID is invalid; restore or remove chatgpt-profile.json and reconnect.") from None
    client_id = profile.get("client_id", "")
    if not isinstance(client_id, str) or (client_id and not _CHATGPT_CLIENT_ID_PATTERN.fullmatch(client_id)) or client_id == "dynamic_agent_client":
        raise RuntimeError("The saved ChatGPT client registration is invalid; reconnect ChatGPT.")
    if not path.exists():
        _save_chatgpt_profile({"host_id": profile["host_id"], "client_id": client_id})
    return {"host_id": profile["host_id"], "client_id": client_id}


def _save_chatgpt_profile(profile: dict[str, str]) -> None:
    """@brief Persist the non-secret ChatGPT host identity and issued client registration."""
    encoded = json.dumps(profile, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_private_file(chatgpt_profile_path(), encoded)


def _ollama_models(base_url: str) -> list[str]:
    """@brief Read model names from the configured loopback-only Ollama endpoint."""
    now = time.monotonic()
    cache_key = f"ollama:{base_url}"
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return list(cached[1].get("models", []))
    request = urllib.request.Request(f"{base_url}/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        models = []
    else:
        entries = payload.get("models", []) if isinstance(payload, dict) else []
        models = [entry["name"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    with _STATUS_LOCK:
        _STATUS_CACHE[cache_key] = (now + 5, {"models": models})
    return models


def get_provider_statuses() -> dict[str, dict[str, Any]]:
    """@brief Return safe readiness indicators for configured AI providers."""
    settings = load_ai_settings()
    gemini_file = Path(str(settings["gemini_client_secrets_file"])).expanduser()
    gemini_ready = bool(gemini_file.is_file() and settings["gemini_google_cloud_project"] and _gemini_token_ready())
    try:
        from .config import get_secret

        gemini_api_ready = bool(get_secret("GEMINI_API_KEY"))
        openai_ready = bool(get_secret("OPENAI_API_KEY"))
        anthropic_ready = bool(get_secret("ANTHROPIC_API_KEY"))
    except ImportError:
        gemini_api_ready = False
        openai_ready = False
        anthropic_ready = False
    models = _ollama_models(str(settings["ollama_base_url"]))
    chatgpt_ready = _chatgpt_token_ready()
    with _LOGIN_LOCK:
        login_states = {provider: dict(value) for provider, value in _LOGIN_STATE.items()}
    statuses: dict[str, dict[str, Any]] = {
        "openai_api": {"ready": openai_ready, "installed": True, "message": "API 키 연결됨." if openai_ready else "OPENAI_API_KEY 설정이 필요합니다."},
        "gemini_api": {"ready": gemini_api_ready, "installed": True, "message": "Gemini API 키 연결됨." if gemini_api_ready else "GEMINI_API_KEY 설정이 필요합니다."},
        "gemini_oauth": {"ready": gemini_ready, "installed": True, "connecting": False, "message": "Google OAuth 연결됨." if gemini_ready else "OAuth 클라이언트 파일, Cloud 프로젝트와 로그인이 필요합니다."},
        "chatgpt_oauth": {"ready": chatgpt_ready, "installed": True, "connecting": False, "message": "ChatGPT OAuth 연결됨." if chatgpt_ready else "Sign in with ChatGPT 연결이 필요합니다."},
        "anthropic_api": {"ready": anthropic_ready, "installed": True, "message": "Anthropic API 키 연결됨." if anthropic_ready else "ANTHROPIC_API_KEY 설정이 필요합니다."},
        "ollama": {"ready": bool(models), "installed": True, "models": models, "message": "로컬 Ollama 연결됨." if models else "localhost Ollama 서버를 찾을 수 없습니다."},
    }
    statuses["gemini_oauth"]["connecting"] = login_states.get("gemini", {}).get("state") == "connecting"
    for name, state in login_states.items():
        target = {"gemini": "gemini_oauth", "chatgpt_oauth": "chatgpt_oauth"}.get(name)
        if target in statuses and state.get("state") in {"connecting", "error"}:
            statuses[target]["connecting"] = state.get("state") == "connecting"
            if state.get("state") == "error":
                statuses[target]["message"] = state.get("message", "OAuth 연결에 실패했습니다.")
    return statuses


def _is_openai_text_model(model_id: str) -> bool:
    """@brief Filter OpenAI /v1/models entries down to text/reasoning generation models."""
    lowered = model_id.lower()
    excluded = ("whisper", "tts", "dall-e", "embedding", "moderation", "sora", "image", "realtime", "audio", "transcribe", "babbage", "davinci")
    if any(token in lowered for token in excluded):
        return False
    return lowered.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4", "o5", "codex"))


def _fetch_openai_models_with_bearer(token: str) -> list[dict[str, str]]:
    """@brief Fetch live text-capable model IDs from OpenAI's /v1/models endpoint."""
    payload = _request_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=15,
    )
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    filtered = [
        entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and _is_openai_text_model(entry["id"])
    ]
    filtered.sort(key=lambda item: (int(item.get("created") or 0), item["id"]), reverse=True)
    return [{"id": entry["id"], "label": entry["id"]} for entry in filtered]


def _is_gemini_text_model(model_id: str) -> bool:
    """@brief Keep Gemini/Gemma text-JSON generation models and exclude image/audio/robotics modalities."""
    lowered = model_id.lower()
    excluded = (
        "embedding",
        "lyria",
        "imagen",
        "veo",
        "nano-banana",
        "-image",
        "-tts",
        "transcribe",
        "robotics",
        "computer-use",
        "deep-research",
        "antigravity",
        "aqa",
        "customtools",
    )
    return not any(token in lowered for token in excluded)


def _gemini_auth_headers(*, use_api_key: bool, json_content: bool = False) -> dict[str, str]:
    """@brief Build Google Generative Language API headers for either API-key or OAuth auth."""
    from .config import get_secret

    headers: dict[str, str] = {"Accept": "application/json"}
    if json_content:
        headers["Content-Type"] = "application/json"
    if use_api_key:
        api_key = get_secret("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY 설정이 필요합니다.")
        headers["x-goog-api-key"] = api_key
        return headers
    settings = load_ai_settings()
    project_id = str(settings.get("gemini_google_cloud_project") or "").strip()
    if not project_id:
        raise RuntimeError("Enter the Google Cloud project ID in AI settings before using Gemini OAuth.")
    credentials = _load_gemini_credentials()
    headers["Authorization"] = f"Bearer {credentials.token}"
    headers["x-goog-user-project"] = project_id
    return headers


def _fetch_gemini_models(*, use_api_key: bool = False) -> list[dict[str, str]]:
    """@brief Fetch live generateContent-capable models from Google Generative Language API."""
    payload = _request_json(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers=_gemini_auth_headers(use_api_key=use_api_key),
        timeout=15,
    )
    entries = payload.get("models", []) if isinstance(payload, dict) else []
    models: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        methods = entry.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:
            continue
        raw_name = str(entry.get("name") or "")
        model_id = raw_name.removeprefix("models/").strip()
        if not model_id or not _is_gemini_text_model(model_id):
            continue
        display_name = str(entry.get("displayName") or "").strip()
        label = f"{model_id} ({display_name})" if display_name and display_name != model_id else model_id
        models.append({"id": model_id, "label": label})
    models.sort(key=lambda item: (1 if item["id"].startswith("gemini-") else 0, item["id"]), reverse=True)
    return models


def _fetch_anthropic_models() -> list[dict[str, str]]:
    """@brief Fetch live Claude models from Anthropic's /v1/models endpoint."""
    from .config import get_secret

    api_key = get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 설정이 필요합니다.")
    payload = _request_json(
        "https://api.anthropic.com/v1/models?limit=100",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Accept": "application/json"},
        timeout=15,
    )
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    models: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        model_id = entry["id"].strip()
        display_name = str(entry.get("display_name") or "").strip()
        label = f"{model_id} ({display_name})" if display_name and display_name != model_id else model_id
        models.append({"id": model_id, "label": label})
    return models


def list_provider_models(provider: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """@brief Fetch live supported models from the provider API without hardcoded model lists."""
    from .ai_config import AI_PROVIDERS
    from .config import get_secret

    if provider not in AI_PROVIDERS:
        raise ValueError(f"Unsupported AI provider: {provider}")
    now = time.monotonic()
    if not force_refresh:
        with _STATUS_LOCK:
            cached = _MODEL_LIST_CACHE.get(provider)
            if cached and cached[0] > now:
                return dict(cached[1])

    models: list[dict[str, str]] = []
    error_message: str | None = None
    try:
        if provider == "openai_api":
            api_key = get_secret("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY 설정이 필요합니다.")
            models = _fetch_openai_models_with_bearer(api_key)
        elif provider == "chatgpt_oauth":
            models = _fetch_openai_models_with_bearer(_chatgpt_access_token())
        elif provider == "gemini_api":
            models = _fetch_gemini_models(use_api_key=True)
        elif provider == "gemini_oauth":
            models = _fetch_gemini_models()
        elif provider == "anthropic_api":
            models = _fetch_anthropic_models()
        elif provider == "ollama":
            settings = load_ai_settings()
            if force_refresh:
                with _STATUS_LOCK:
                    _STATUS_CACHE.pop(f"ollama:{settings['ollama_base_url']}", None)
            raw_models = _ollama_models(str(settings["ollama_base_url"]))
            models = [{"id": name, "label": name} for name in raw_models]
            if not models:
                error_message = "설치된 Ollama 모델을 찾지 못했습니다. Ollama 서버 실행 상태를 확인하세요."
    except Exception as error:
        error_message = str(error) if isinstance(error, RuntimeError) else f"모델 목록 조회 실패 ({type(error).__name__})."

    result = {
        "provider": provider,
        "models": models,
        "error": error_message,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not error_message:
        with _STATUS_LOCK:
            _MODEL_LIST_CACHE[provider] = (now + 300, result)
    return result


def list_all_provider_models(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """@brief Return live model lists across all supported AI providers."""
    from .ai_config import AI_PROVIDERS

    return {provider: list_provider_models(provider, force_refresh=force_refresh) for provider in sorted(AI_PROVIDERS)}


def sync_ready_provider_models(*, force_refresh: bool = True) -> dict[str, dict[str, Any]]:
    """@brief Periodically refresh live model catalogs for every currently ready provider."""
    statuses = get_provider_statuses()
    synced: dict[str, dict[str, Any]] = {}
    for provider, status in statuses.items():
        if isinstance(status, dict) and status.get("ready"):
            synced[provider] = list_provider_models(provider, force_refresh=force_refresh)
    return synced


def clear_model_list_cache(provider: str | None = None) -> None:
    """@brief Invalidate cached live model catalogs after OAuth or settings updates."""
    with _STATUS_LOCK:
        if provider:
            _MODEL_LIST_CACHE.pop(provider, None)
        else:
            _MODEL_LIST_CACHE.clear()


def provider_ready(provider: str, model: str) -> bool:
    """@brief Confirm that one provider is configured and can serve the selected model."""
    status = get_provider_statuses().get(provider, {})
    if not status.get("ready"):
        return False
    if provider == "ollama":
        models = status.get("models", [])
        return model in models or f"{model}:latest" in models
    return True


def _set_login_state(provider: str, state: str, message: str) -> None:
    """@brief Save a credential-free OAuth progress state for the local UI."""
    with _LOGIN_LOCK:
        _LOGIN_STATE[provider] = {"state": state, "message": message}
    with _STATUS_LOCK:
        _STATUS_CACHE.clear()


def _save_gemini_token(credentials: Any) -> None:
    """@brief Atomically save the app's Gemini OAuth token under user-local data."""
    protected = _protect_token(credentials.to_json().encode("utf-8"))
    _atomic_write_private_file(gemini_oauth_token_path(), protected)


def _perform_gemini_oauth() -> None:
    """@brief Run a time-bounded loopback Google OAuth flow for Gemini API access."""
    settings = load_ai_settings()
    client_file = Path(str(settings["gemini_client_secrets_file"])).expanduser().resolve()
    if not client_file.is_file():
        raise RuntimeError("Choose an OAuth Desktop client JSON file first.")
    if not settings["gemini_google_cloud_project"]:
        raise RuntimeError("Enter the Google Cloud project ID first.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise RuntimeError("Google OAuth dependencies are not installed; rerun setup.ps1.") from None
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes=GEMINI_OAUTH_SCOPES, autogenerate_code_verifier=True)
    # Advertise loopback while Docker may bind the callback listener to its private container interface.
    with oauth_callback_server("/oauth2callback") as (server, redirect_uri):
        flow.redirect_uri = redirect_uri
        authorization_url, expected_state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        set_authorization_url("gemini", authorization_url)
        try:
            callback = wait_for_oauth_callback(server, expected_state)
        finally:
            clear_authorization_url("gemini")
    if callback.get("state") != expected_state:
        raise RuntimeError("Google OAuth state validation failed.")
    if callback.get("error"):
        raise RuntimeError("Google sign-in was cancelled or denied.")
    code = callback.get("code")
    if not code:
        raise RuntimeError("Google sign-in did not return an authorization code.")
    flow.fetch_token(code=code)
    _save_gemini_token(flow.credentials)


def _request_form(url: str, values: dict[str, str], timeout: int = 30) -> dict[str, Any]:
    """@brief Exchange OAuth form data while exposing only a provider error code."""
    request = urllib.request.Request(url, data=urlencode(values).encode("utf-8"), headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read(4096).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        code = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(code, str) and code.replace("_", "").isalnum():
            raise RuntimeError(f"OAuth request failed with {code} (HTTP {error.code}).") from None
        raise RuntimeError(f"OAuth request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"OAuth request failed ({type(error).__name__}).") from None
    if not isinstance(payload, dict):
        raise RuntimeError("OAuth provider returned an unexpected response shape.")
    return payload


def _verify_chatgpt_id_token(id_token: str, client_id: str, nonce: str) -> dict[str, Any]:
    """@brief Verify the ChatGPT ID-token signature, issuer, audience, lifetime, and nonce."""
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError:
        raise RuntimeError("ChatGPT OAuth validation dependencies are missing; rerun setup.ps1.") from None
    try:
        signing_key = PyJWKClient(CHATGPT_JWKS_URL, cache_keys=True).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, signing_key, algorithms=["RS256"], audience=client_id, issuer=CHATGPT_ISSUER, options={"require": ["sub", "exp", "iat", "iss", "aud"]}, leeway=5)
    except Exception as error:
        raise RuntimeError(f"ChatGPT identity token validation failed ({type(error).__name__}).") from None
    if claims.get("nonce") != nonce or not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise RuntimeError("ChatGPT identity token nonce or subject validation failed.")
    return claims


def _save_chatgpt_tokens(tokens: dict[str, Any]) -> None:
    """@brief Encrypt and atomically persist the validated ChatGPT OAuth token set."""
    payload = json.dumps(tokens, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_private_file(chatgpt_oauth_token_path(), _protect_token(payload))


def _ensure_chatgpt_identity_matches(client_id: str, subject: str) -> None:
    """@brief Prevent reauthorization from silently replacing a saved ChatGPT account."""
    try:
        existing = _load_chatgpt_tokens()
    except RuntimeError:
        return
    if existing.get("client_id") == client_id and existing.get("subject") and existing["subject"] != subject:
        raise RuntimeError("ChatGPT returned a different account than the saved connection; disconnect it before switching accounts.")


def _load_chatgpt_tokens() -> dict[str, Any]:
    """@brief Load the app-owned ChatGPT token set without returning it to the browser."""
    try:
        payload = json.loads(_unprotect_token(chatgpt_oauth_token_path().read_bytes()).decode("utf-8"))
    except FileNotFoundError:
        raise RuntimeError("Connect Sign in with ChatGPT in AI settings before using this provider.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        raise RuntimeError("The local ChatGPT credentials are unreadable; reconnect this app.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("client_id"), str) or not isinstance(payload.get("refresh_token"), str):
        raise RuntimeError("The local ChatGPT credentials are incomplete; reconnect this app.")
    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or "chatgpt.tokens.use.direct" not in scopes:
        raise RuntimeError("ChatGPT plan usage was not authorized; reconnect and grant the requested permission.")
    profile = _load_chatgpt_profile()
    if payload["client_id"] != profile["client_id"] or payload.get("host_id") != profile["host_id"]:
        raise RuntimeError("The local ChatGPT profile and credentials do not match; reconnect this app.")
    return payload


def _perform_chatgpt_oauth() -> None:
    """@brief Register or authorize this local app with ChatGPT using state, nonce, and PKCE."""
    profile = _load_chatgpt_profile()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    client_id = profile["client_id"] or "dynamic_agent_client"
    with oauth_callback_server("/auth/callback") as (server, redirect_uri):
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": profile["host_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(CHATGPT_REQUIRED_SCOPES)),
            "resource": CHATGPT_RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if not profile["client_id"]:
            parameters["agent_name_hint"] = "Shortform Content Studio"
        set_authorization_url("chatgpt_oauth", f"{CHATGPT_AUTHORIZATION_URL}?{urlencode(parameters)}")
        try:
            callback = wait_for_oauth_callback(server, state)
        finally:
            clear_authorization_url("chatgpt_oauth")
    if callback.get("state") != state:
        raise RuntimeError("ChatGPT OAuth state validation failed.")
    if callback.get("error"):
        if callback["error"] == "access_denied":
            raise RuntimeError("ChatGPT plan permission was declined; no provider requests were made.")
        raise RuntimeError("ChatGPT sign-in was cancelled or denied.")
    code = callback.get("code")
    issued_client_id = callback.get("client_id") or profile["client_id"]
    if not code or not isinstance(issued_client_id, str) or not _CHATGPT_CLIENT_ID_PATTERN.fullmatch(issued_client_id) or issued_client_id == "dynamic_agent_client":
        raise RuntimeError("ChatGPT sign-in did not return a valid authorization code and issued client ID.")
    if profile["client_id"] and issued_client_id != profile["client_id"]:
        raise RuntimeError("ChatGPT returned a different client registration than the one selected.")
    profile["client_id"] = issued_client_id
    _save_chatgpt_profile(profile)
    response = _request_form(CHATGPT_TOKEN_URL, {"grant_type": "authorization_code", "client_id": issued_client_id, "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": CHATGPT_RESOURCE})
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token")
    id_token = response.get("id_token")
    scopes = response.get("scope", "").split() if isinstance(response.get("scope"), str) else []
    if not all(isinstance(value, str) and value for value in (access_token, refresh_token, id_token)):
        raise RuntimeError("ChatGPT OAuth did not return the required access, refresh, and identity tokens.")
    if "chatgpt.tokens.use.direct" not in scopes:
        raise RuntimeError("ChatGPT plan usage permission was not granted; reconnect and approve the requested scope.")
    claims = _verify_chatgpt_id_token(id_token, issued_client_id, nonce)
    _ensure_chatgpt_identity_matches(issued_client_id, claims["sub"])
    expires_in = response.get("expires_in", 3600)
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise RuntimeError("ChatGPT OAuth returned an invalid token lifetime.")
    _save_chatgpt_tokens({"client_id": issued_client_id, "host_id": profile["host_id"], "subject": claims["sub"], "id_token": id_token, "access_token": access_token, "refresh_token": refresh_token, "expires_at": time.time() + expires_in, "scopes": scopes})


def _chatgpt_access_token() -> str:
    """@brief Refresh the rotating ChatGPT token under a process-wide lock before expiry."""
    with _CHATGPT_REFRESH_LOCK:
        tokens = _load_chatgpt_tokens()
        if isinstance(tokens.get("expires_at"), (int, float)) and tokens["expires_at"] > time.time() + 90 and tokens.get("access_token"):
            return tokens["access_token"]
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("ChatGPT session cannot be refreshed; reconnect this app.")
        try:
            response = _request_form(CHATGPT_TOKEN_URL, {"grant_type": "refresh_token", "client_id": tokens["client_id"], "refresh_token": refresh_token, "resource": CHATGPT_RESOURCE})
        except RuntimeError as error:
            if "invalid_grant" in str(error):
                chatgpt_oauth_token_path().unlink(missing_ok=True)
                raise RuntimeError("ChatGPT session expired or was revoked; reconnect this app.") from None
            raise
        access_token = response.get("access_token")
        expires_in = response.get("expires_in", 3600)
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            raise RuntimeError("ChatGPT token refresh returned an invalid response.")
        tokens["access_token"] = access_token
        tokens["expires_at"] = time.time() + expires_in
        if isinstance(response.get("refresh_token"), str) and response["refresh_token"]:
            tokens["refresh_token"] = response["refresh_token"]
        if isinstance(response.get("scope"), str):
            refreshed_scopes = response["scope"].split()
            # A rotating token is not sufficient if the user has revoked the scope required for this feature.
            if "chatgpt.tokens.use.direct" not in refreshed_scopes:
                chatgpt_oauth_token_path().unlink(missing_ok=True)
                raise RuntimeError("ChatGPT refresh no longer grants plan usage; reconnect and approve the requested permission.")
            tokens["scopes"] = refreshed_scopes
        _save_chatgpt_tokens(tokens)
        return access_token


def _login_worker(provider: str) -> None:
    """@brief Run OAuth outside the request thread and publish only sanitized status."""
    try:
        if provider == "gemini":
            _perform_gemini_oauth()
            if not _gemini_token_ready():
                raise RuntimeError("Google OAuth did not create a refreshable Gemini credential.")
        elif provider == "chatgpt_oauth":
            _perform_chatgpt_oauth()
            if not _chatgpt_token_ready():
                raise RuntimeError("ChatGPT OAuth did not create a usable plan-usage credential.")
        else:
            raise RuntimeError("Unsupported OAuth provider.")
        _set_login_state(provider, "connected", "로그인 연결이 완료되었습니다.")
        clear_model_list_cache("gemini_oauth" if provider == "gemini" else "chatgpt_oauth")
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else f"OAuth 처리 실패 ({type(error).__name__})."
        _set_login_state(provider, "error", message[:240])


def start_oauth_login(provider: str) -> dict[str, str]:
    """@brief Start one explicit OAuth login flow for Gemini or Sign in with ChatGPT."""
    if provider not in {"gemini", "chatgpt_oauth"}:
        raise ValueError("This provider does not expose an OAuth connection flow.")
    with _LOGIN_LOCK:
        if _LOGIN_STATE.get(provider, {}).get("state") == "connecting":
            return {"state": "connecting", "message": "로그인 창에서 인증을 완료해 주세요."}
        _LOGIN_STATE[provider] = {"state": "connecting", "message": "로그인 연결 중입니다."}
    with _STATUS_LOCK:
        _STATUS_CACHE.clear()
    worker = threading.Thread(target=_login_worker, args=(provider,), daemon=True, name=f"shortform-oauth-{provider}")
    worker.start()
    return {"state": "connecting", "message": "로그인 연결을 시작했습니다."}


def disconnect_gemini_oauth() -> None:
    """@brief Remove only this app's Gemini OAuth token."""
    gemini_oauth_token_path().unlink(missing_ok=True)
    clear_model_list_cache("gemini_oauth")
    _set_login_state("gemini", "disconnected", "Google OAuth 연결을 해제했습니다.")


def disconnect_chatgpt_oauth() -> bool:
    """@brief Revoke the renewable ChatGPT session when possible and always clear local tokens."""
    token_path = chatgpt_oauth_token_path()
    revocation_confirmed = False
    try:
        tokens = _load_chatgpt_tokens()
        discovery_request = urllib.request.Request("https://auth.openai.com/.well-known/openid-configuration", headers={"Accept": "application/json"})
        with urllib.request.urlopen(discovery_request, timeout=8) as response:
            discovery = json.loads(response.read(4096).decode("utf-8"))
        endpoint = discovery.get("revocation_endpoint") if isinstance(discovery, dict) else None
        parsed_endpoint = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if parsed_endpoint and parsed_endpoint.scheme == "https" and parsed_endpoint.hostname == "auth.openai.com":
            revoke_request = urllib.request.Request(endpoint, data=urlencode({"token": tokens["refresh_token"], "token_type_hint": "refresh_token", "client_id": tokens["client_id"]}).encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with urllib.request.urlopen(revoke_request, timeout=8) as response:
                revocation_confirmed = response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        revocation_confirmed = False
    token_path.unlink(missing_ok=True)
    clear_model_list_cache("chatgpt_oauth")
    message = "ChatGPT 연결을 해제하고 토큰 폐기를 확인했습니다." if revocation_confirmed else "이 앱의 토큰을 삭제했습니다. 원격 폐기는 확인되지 않아 ChatGPT 설정에서도 앱 접근을 해제하세요."
    _set_login_state("chatgpt_oauth", "disconnected", message)
    return revocation_confirmed


def _load_gemini_credentials() -> Any:
    """@brief Load or refresh the dedicated Gemini OAuth token without returning it."""
    token_path = gemini_oauth_token_path()
    if not token_path.is_file():
        raise RuntimeError("Connect Google OAuth in AI settings before using Gemini.")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        token_payload = json.loads(_unprotect_token(token_path.read_bytes()).decode("utf-8"))
        credentials = Credentials.from_authorized_user_info(token_payload, GEMINI_OAUTH_SCOPES)
        if not credentials.valid:
            if not credentials.expired or not credentials.refresh_token:
                raise RuntimeError("Google OAuth credentials need to be reconnected.")
            credentials.refresh(Request())
            _save_gemini_token(credentials)
        return credentials
    except ImportError:
        raise RuntimeError("Google OAuth dependencies are not installed; rerun setup.ps1.") from None
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"Google OAuth credentials could not be refreshed ({type(error).__name__}).") from None


def _request_json(url: str, *, headers: dict[str, str], body: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
    """@brief Send a bounded JSON HTTP request and sanitize provider failures."""
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=encoded, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            err_body = json.loads(error.read(4096).decode("utf-8", errors="replace"))
            err_obj = err_body.get("error") if isinstance(err_body, dict) else None
            msg = err_obj.get("message") if isinstance(err_obj, dict) else None
            if isinstance(msg, str) and msg.strip():
                detail = f" ({msg.strip()[:220]})"
        except Exception:
            detail = ""
        raise RuntimeError(f"AI provider request failed with HTTP {error.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"AI provider request failed ({type(error).__name__}).") from None
    if not isinstance(payload, dict):
        raise RuntimeError("AI provider returned an unexpected response shape.")
    return payload


def _call_gemini_generate(model: str, body: dict[str, Any], *, use_api_key: bool = False) -> str:
    """@brief Execute a Gemini generateContent request with 404/503 fallback and extract candidate text."""
    from urllib.parse import quote

    candidates: list[str] = [model]
    for fallback_model in (
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
    ):
        if fallback_model not in candidates:
            candidates.append(fallback_model)

    headers = _gemini_auth_headers(use_api_key=use_api_key, json_content=True)
    response: dict[str, Any] | None = None
    last_error: RuntimeError | None = None
    for candidate_model in candidates:
        model_path = quote(candidate_model, safe="-_.:")
        try:
            response = _request_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_path}:generateContent",
                headers=headers,
                body=body,
            )
            break
        except RuntimeError as error:
            last_error = error
            msg = str(error)
            if "HTTP 503" in msg:
                time.sleep(1.2)
            if ("HTTP 404" in msg or "HTTP 503" in msg) and candidate_model != candidates[-1]:
                continue
            break

    if response is None and use_api_key and _gemini_token_ready() and last_error and ("HTTP 503" in str(last_error) or "HTTP 404" in str(last_error)):
        try:
            oauth_headers = _gemini_auth_headers(use_api_key=False, json_content=True)
            response = _request_json(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent",
                headers=oauth_headers,
                body=body,
            )
        except RuntimeError:
            pass
    if response is None:
        raise last_error or RuntimeError("Gemini request failed.")
    cand_list = response.get("candidates")
    if not isinstance(cand_list, list) or not cand_list or not isinstance(cand_list[0], dict):
        raise RuntimeError("Gemini returned no usable candidate.")
    content = cand_list[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    text = "\n".join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)).strip()
    if not text:
        raise RuntimeError("Gemini returned no text content.")
    return text


def _generate_gemini(model: str, system_prompt: str, user_prompt: str, *, use_api_key: bool = False) -> str:
    """@brief Generate JSON text through Gemini API with automatic fallback on 404/503 model deprecation or load spikes."""
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    return _call_gemini_generate(model, body, use_api_key=use_api_key)


def _generate_anthropic(model: str, system_prompt: str, user_prompt: str) -> str:
    """@brief Generate text through Anthropic's supported API-key Messages endpoint."""
    from .config import get_secret

    api_key = get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY in the local .env file before using Claude API.")
    response = _request_json(
        ANTHROPIC_MESSAGES_URL,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        body={"model": model, "max_tokens": 4096, "system": system_prompt, "messages": [{"role": "user", "content": user_prompt}]},
    )
    content = response.get("content")
    text = "\n".join(part["text"] for part in content if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)) if isinstance(content, list) else ""
    if not text.strip():
        raise RuntimeError("Anthropic returned no text content.")
    return text.strip()


def _generate_chatgpt(model: str, system_prompt: str, user_prompt: str) -> str:
    """@brief Stream a non-stored Responses API request authorized through ChatGPT plan OAuth."""
    body = json.dumps({"model": model, "instructions": system_prompt, "input": user_prompt, "store": False, "stream": True}).encode("utf-8")
    request = urllib.request.Request(CHATGPT_RESPONSES_URL, data=body, headers={"Authorization": f"Bearer {_chatgpt_access_token()}", "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
    fragments: list[str] = []
    output_length = 0
    completed = False
    response_start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            for raw_line in response:
                if time.monotonic() > response_start + 180:
                    raise RuntimeError("ChatGPT response stream exceeded the three-minute deadline.")
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    raise RuntimeError("ChatGPT returned an invalid response-stream event.") from None
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
                    fragments.append(event["delta"])
                    output_length += len(event["delta"])
                    if output_length > 2_000_000:
                        raise RuntimeError("ChatGPT response exceeded the two-megabyte output limit.")
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    detail = event.get("error", {})
                    code = detail.get("code") if isinstance(detail, dict) else None
                    safe_code = code if isinstance(code, str) and code.replace("_", "").isalnum() else "request_failed"
                    raise RuntimeError(f"ChatGPT response ended with {safe_code}.")
                elif event_type == "response.completed":
                    completed = True
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read(4096).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        detail = payload.get("error") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        safe_code = code if isinstance(code, str) and code.replace("_", "").isalnum() else "request_failed"
        raise RuntimeError(f"ChatGPT Responses API returned HTTP {error.code} ({safe_code}).") from None
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, OSError) as error:
        raise RuntimeError(f"ChatGPT response stream failed ({type(error).__name__}).") from None
    if not completed:
        raise RuntimeError("ChatGPT response stream ended before response.completed.")
    text = "".join(fragments).strip()
    if not text:
        raise RuntimeError("ChatGPT returned no text content.")
    return text


def _generate_ollama(model: str, system_prompt: str, user_prompt: str) -> str:
    """@brief Generate structured local output through a loopback-only Ollama server."""
    settings = load_ai_settings()
    response = _request_json(
        f"{settings['ollama_base_url']}/api/chat",
        headers={"Content-Type": "application/json"},
        body={
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "format": "json",
            "stream": False,
            "options": {"num_ctx": 24576},
        },
    )
    message = response.get("message")
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Ollama returned no text content.")
    return text.strip()


def generate_text(provider: str, model: str, system_prompt: str, user_prompt: str) -> str:
    """@brief Dispatch one structured generation request to the selected provider."""
    if provider == "chatgpt_oauth":
        return _generate_chatgpt(model, system_prompt, user_prompt)
    if provider == "gemini_api":
        return _generate_gemini(model, system_prompt, user_prompt, use_api_key=True)
    if provider == "gemini_oauth":
        return _generate_gemini(model, system_prompt, user_prompt)
    if provider == "anthropic_api":
        return _generate_anthropic(model, system_prompt, user_prompt)
    if provider == "ollama":
        return _generate_ollama(model, system_prompt, user_prompt)
    raise ValueError(f"Unsupported non-OpenAI provider: {provider}.")


def generate_multimodal_description(
    provider: str,
    model: str,
    prompt_text: str,
    media_parts: list[dict[str, str]],
) -> str:
    """@brief Analyze attached images/PDFs with a vision-capable Gemini or Anthropic model.

    @param provider Selected provider identifier.
    @param model Selected model ID.
    @param prompt_text Inspection prompt.
    @param media_parts List of dicts with keys 'mime_type' and 'data_b64'.
    @returns Concise factual visual/OCR description string.
    """
    from .config import get_secret

    valid_parts = [p for p in media_parts if isinstance(p, dict) and p.get("mime_type") and p.get("data_b64")]
    if not valid_parts:
        return ""
    if provider in {"gemini_api", "gemini_oauth"}:
        parts: list[dict[str, Any]] = [{"text": prompt_text}]
        for item in valid_parts[:6]:
            parts.append({"inlineData": {"mimeType": item["mime_type"], "data": item["data_b64"]}})
        body = {"contents": [{"role": "user", "parts": parts}]}
        return _call_gemini_generate(model, body, use_api_key=(provider == "gemini_api"))
    if provider == "anthropic_api":
        api_key = get_secret("ANTHROPIC_API_KEY")
        if not api_key:
            return ""
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for item in valid_parts[:4]:
            if item["mime_type"].startswith("image/"):
                content.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": item["mime_type"], "data": item["data_b64"]},
                    }
                )
        if len(content) <= 1:
            return ""
        response = _request_json(
            ANTHROPIC_MESSAGES_URL,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            body={"model": model, "max_tokens": 1200, "messages": [{"role": "user", "content": content}]},
        )
        blocks = response.get("content")
        return "\n".join(b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)).strip() if isinstance(blocks, list) else ""
    if get_secret("GEMINI_API_KEY"):
        parts = [{"text": prompt_text}]
        for item in valid_parts[:6]:
            parts.append({"inlineData": {"mimeType": item["mime_type"], "data": item["data_b64"]}})
        return _call_gemini_generate("gemini-3-flash-preview", {"contents": [{"role": "user", "parts": parts}]}, use_api_key=True)
    return ""
