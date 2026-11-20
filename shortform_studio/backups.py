"""Validated local backup creation and guarded project-data restoration."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_FORMAT = "shortform-studio-backup-v1"
MAX_BACKUP_BYTES = 10_000_000_000
MAX_RESTORED_BYTES = 20_000_000_000
MAX_ARCHIVE_ENTRIES = 50_000


def create_backup_archive(database_path: Path, projects_dir: Path, archive_path: Path) -> Path:
    """@brief Create a consistent SQLite and media archive without provider secrets.

    @param database_path Active application SQLite database.
    @param projects_dir User-generated project media directory.
    @param archive_path Destination ZIP archive.
    @returns The created archive path.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if projects_dir.is_symlink():
        raise ValueError("Backup cannot include a symbolic-link projects root.")
    with tempfile.TemporaryDirectory(prefix="studio-backup-", dir=archive_path.parent) as temporary:
        snapshot_path = Path(temporary) / "studio.sqlite3"
        source = sqlite3.connect(database_path, timeout=30)
        destination = sqlite3.connect(snapshot_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "includes": ["studio.sqlite3", "projects/"],
            "excludes": [".env", "OAuth tokens", "API credentials", "provider secret files"],
        }
        with zipfile.ZipFile(archive_path, "w", allowZip64=True) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2), compress_type=zipfile.ZIP_DEFLATED)
            archive.write(snapshot_path, "studio.sqlite3", compress_type=zipfile.ZIP_DEFLATED)
            if projects_dir.is_dir():
                for source_path in sorted(projects_dir.rglob("*")):
                    if source_path.is_symlink():
                        raise ValueError("Backup cannot include symbolic links in the projects folder.")
                    if source_path.is_file():
                        relative = source_path.relative_to(projects_dir).as_posix()
                        archive_name = f"projects/{relative}"
                        compression = zipfile.ZIP_DEFLATED if source_path.suffix.lower() in {".json", ".txt", ".srt", ".vtt"} else zipfile.ZIP_STORED
                        archive.write(source_path, archive_name, compress_type=compression)
    if archive_path.stat().st_size > MAX_BACKUP_BYTES:
        archive_path.unlink(missing_ok=True)
        raise ValueError("Created backup exceeds the 10 GB restore limit.")
    return archive_path


def _safe_archive_name(name: str) -> PurePosixPath:
    """@brief Reject archive paths that could escape the restore staging folder."""
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Backup contains an unsafe archive path.")
    if path.parts[0] not in {"manifest.json", "studio.sqlite3", "projects"}:
        raise ValueError("Backup contains an unsupported file.")
    if path.parts[0] == "projects" and len(path.parts) < 2:
        raise ValueError("Backup contains an invalid project path.")
    if len(path.parts) > 1 and any(":" in part for part in path.parts):
        raise ValueError("Backup contains an invalid project path.")
    return path


def validate_and_extract_backup(archive_path: Path, staging_dir: Path) -> tuple[Path, Path]:
    """@brief Validate archive structure, paths, size, manifest, and SQLite integrity.

    @param archive_path Uploaded backup archive to inspect.
    @param staging_dir Empty private directory for extracted data.
    @returns Staged database and projects directory paths.
    """
    if archive_path.stat().st_size > MAX_BACKUP_BYTES:
        raise ValueError("Backup archive exceeds the 10 GB upload limit.")
    staging_dir.mkdir(parents=True, exist_ok=True)
    extracted_bytes = 0
    seen: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Backup contains too many files.")
        for entry in entries:
            raw_name = entry.filename.rstrip("/")
            if entry.is_dir() and raw_name == "projects":
                continue
            path = _safe_archive_name(raw_name)
            if entry.is_dir():
                continue
            if entry.filename in seen:
                raise ValueError("Backup contains duplicate archive paths.")
            seen.add(entry.filename)
            mode = (entry.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("Backup cannot contain symbolic links.")
            extracted_bytes += entry.file_size
            if extracted_bytes > MAX_RESTORED_BYTES:
                raise ValueError("Expanded backup exceeds the 20 GB restore limit.")
            destination = staging_dir.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = destination.parent.resolve()
            resolved_parent.relative_to(staging_dir.resolve())
            remaining = entry.file_size
            with archive.open(entry, "r") as source, destination.open("xb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
                if remaining or source.read(1):
                    raise ValueError("Backup entry expanded beyond its declared size.")
    required = {"manifest.json", "studio.sqlite3"}
    if not required.issubset(seen):
        raise ValueError("Backup is missing its manifest or SQLite database.")
    manifest = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise ValueError("Backup format is unsupported.")
    database_path = staging_dir / "studio.sqlite3"
    with sqlite3.connect(database_path) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not result or result[0] != "ok" or not {"content_items", "publications", "app_settings"}.issubset(tables):
            raise ValueError("Backup database failed integrity or schema validation.")
        if "scheduled_publications" in tables:
            # Restored timers require an operator to review and schedule again before any external post.
            connection.execute("UPDATE scheduled_publications SET status='canceled', error='Restored backup requires explicit rescheduling.' WHERE status='scheduled'")
    projects_path = staging_dir / "projects"
    projects_path.mkdir(exist_ok=True)
    return database_path, projects_path


def restore_backup_archive(database_path: Path, projects_dir: Path, archive_path: Path, recovery_dir: Path) -> Path | None:
    """@brief Replace local project data only after validation and preserve a recovery archive.

    @param database_path Active application SQLite database path.
    @param projects_dir Active project-media directory.
    @param archive_path Validated-format ZIP supplied by the operator.
    @param recovery_dir Destination for a pre-restore safety archive.
    @returns The pre-restore recovery archive path, or None when there was no existing database.
    """
    recovery_dir.mkdir(parents=True, exist_ok=True)
    if database_path.is_symlink() or projects_dir.is_symlink():
        raise ValueError("Restore refuses to replace symbolic-link data paths.")
    recovery_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="studio-restore-", dir=recovery_dir) as temporary:
        staging = Path(temporary) / "staged"
        staged_database, staged_projects = validate_and_extract_backup(archive_path, staging)
        if database_path.exists():
            recovery_path = recovery_dir / f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
            try:
                create_backup_archive(database_path, projects_dir, recovery_path)
            except Exception:
                recovery_path.unlink(missing_ok=True)
                raise
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        old_database = Path(temporary) / "old.sqlite3"
        old_projects = Path(temporary) / "old-projects"
        database_moved = False
        projects_moved = False
        new_database_installed = False
        new_projects_installed = False
        moved_sidecars: list[tuple[Path, Path]] = []
        try:
            if database_path.exists():
                database_path.replace(old_database)
                database_moved = True
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database_path}{suffix}")
                if sidecar.exists():
                    old_sidecar = Path(f"{old_database}{suffix}")
                    sidecar.replace(old_sidecar)
                    moved_sidecars.append((sidecar, old_sidecar))
            if projects_dir.exists():
                projects_dir.replace(old_projects)
                projects_moved = True
            staged_database.replace(database_path)
            new_database_installed = True
            staged_projects.replace(projects_dir)
            new_projects_installed = True
        except Exception:
            if new_projects_installed and projects_dir.exists():
                shutil.rmtree(projects_dir)
            if new_database_installed and database_path.exists():
                database_path.unlink()
            for live_sidecar, _saved_sidecar in moved_sidecars:
                live_sidecar.unlink(missing_ok=True)
            if projects_moved and old_projects.exists():
                old_projects.replace(projects_dir)
            if database_moved and old_database.exists():
                old_database.replace(database_path)
            for live_sidecar, saved_sidecar in moved_sidecars:
                if saved_sidecar.exists():
                    live_sidecar.unlink(missing_ok=True)
                    saved_sidecar.replace(live_sidecar)
            raise
    return recovery_path
