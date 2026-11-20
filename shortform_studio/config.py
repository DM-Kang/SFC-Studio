"""Runtime configuration and user-local storage paths."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
            os.environ.setdefault(key, value)


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
