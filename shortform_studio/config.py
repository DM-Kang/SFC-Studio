"""Runtime configuration and user-local storage paths."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")

MANAGED_ENV_KEYS = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "RUNWAY_API_KEY",
    "RUNWAY_MODEL",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "YOUTUBE_CLIENT_SECRETS_FILE",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_USER_ID",
    "INSTAGRAM_API_HOST",
    "INSTAGRAM_API_VERSION",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
    "R2_AUTO_CLEANUP",
    "TIKTOK_ACCESS_TOKEN",
)


def load_local_environment() -> None:
    """@brief Load the per-user environment file without overriding process values."""
    app_home = get_app_home()
    env_path = app_home / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key.replace("_", "").isalnum():
            # Treat empty env placeholders (e.g. from Compose env_file templates) as unset so saved local secrets persist across restarts.
            if value and not os.environ.get(key):
                os.environ[key] = value


def get_app_home() -> Path:
    """@brief Return the writable per-user data directory for the application."""
    configured = os.environ.get("SHORTFORM_STUDIO_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ShortformContentStudio"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "shortform-content-studio"
    return Path.home() / ".local" / "share" / "shortform-content-studio"


def get_projects_dir() -> Path:
    """@brief Return the directory that owns generated media and project artifacts."""
    return get_app_home() / "projects"


def get_database_path() -> Path:
    """@brief Return the SQLite database path in the per-user data directory."""
    return get_app_home() / "studio.sqlite3"


def get_secret(name: str, default: str = "") -> str:
    """@brief Read a setting without copying it into project data."""
    return os.environ.get(name, default).strip()


def get_managed_secrets_status() -> dict[str, bool]:
    """@brief Report whether each managed environment key is configured without returning secret values."""
    return {key: bool(get_secret(key)) for key in MANAGED_ENV_KEYS}


def update_local_env_secrets(updates: dict[str, str], *, clear_keys: list[str] | None = None) -> dict[str, bool]:
    """@brief Atomically merge updated credentials into the user-local .env file."""
    allowed = set(MANAGED_ENV_KEYS)
    cleaned_updates: dict[str, str] = {}
    for key, value in updates.items():
        if key not in allowed:
            raise ValueError(f"Unsupported environment setting: {key}")
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError(f"Invalid value for {key}.")
        trimmed = value.strip()
        if trimmed:
            cleaned_updates[key] = trimmed
    keys_to_clear = {key for key in (clear_keys or []) if key in allowed}

    app_home = get_app_home()
    app_home.mkdir(parents=True, exist_ok=True)
    env_path = app_home / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []

    seen: set[str] = set()
    output_lines: list[str] = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(raw_line)
            continue
        key, _ = stripped.split("=", 1)
        key = key.strip()
        if key in keys_to_clear:
            seen.add(key)
            output_lines.append(f"{key}=")
            os.environ.pop(key, None)
        elif key in cleaned_updates:
            seen.add(key)
            val = cleaned_updates[key]
            safe_val = f'"{val}"' if any(ch in val for ch in (' ', '#', '"')) else val
            output_lines.append(f"{key}={safe_val}")
            os.environ[key] = val
        else:
            output_lines.append(raw_line)

    for key in MANAGED_ENV_KEYS:
        if key in seen:
            continue
        if key in cleaned_updates:
            val = cleaned_updates[key]
            safe_val = f'"{val}"' if any(ch in val for ch in (' ', '#', '"')) else val
            output_lines.append(f"{key}={safe_val}")
            os.environ[key] = val
        elif key in keys_to_clear:
            os.environ.pop(key, None)

    temp_path = env_path.with_name(".env.tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write("\n".join(output_lines) + ("\n" if output_lines else ""))
        temp_path.replace(env_path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        raise
    return get_managed_secrets_status()


def is_allowed_request_origin(host_header: str, origin_header: str | None, extra_allowed_hosts: str = "") -> tuple[bool, str]:
    """@brief Validate Host and Origin headers against loopback and opt-in Tailscale/proxy hosts."""
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    configured_hosts = {
        candidate
        for raw in (extra_allowed_hosts or "").split(",")
        if (candidate := raw.strip().rstrip(".").lower()) and _HOSTNAME_PATTERN.fullmatch(candidate)
    }
    host = (urlsplit(f"//{host_header or ''}").hostname or "").rstrip(".").lower()
    if host not in local_hosts and host not in configured_hosts:
        return False, "This application is available only on the local computer."
    if origin_header:
        parsed = urlsplit(origin_header)
        origin_host = (parsed.hostname or "").rstrip(".").lower()
        try:
            origin_port = parsed.port
        except ValueError:
            return False, "Cross-origin browser requests are not allowed."
        if origin_host in local_hosts:
            if parsed.scheme != "http" or origin_port not in {None, 8765}:
                return False, "Cross-origin browser requests are not allowed."
        elif origin_host in configured_hosts:
            if parsed.scheme not in {"http", "https"} or origin_port not in {None, 443, 8443, 10000, 8765}:
                return False, "Cross-origin browser requests are not allowed."
        else:
            return False, "Cross-origin browser requests are not allowed."
    return True, ""
