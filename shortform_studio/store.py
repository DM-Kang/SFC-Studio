"""Small SQLite repository for durable workflow state and publication guards."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_INITIALIZE_LOCK = threading.Lock()
BUSY_STATES = {"researching", "planning", "collecting_materials", "generating", "reviewing", "publishing"}


def _now() -> str:
    """@brief Return a timezone-aware timestamp suitable for persistent records."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Own SQLite lifecycle and atomic state transitions for one local app."""

    def __init__(self, database_path: Path):
        """@brief Initialize the store using the supplied per-user database path."""
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """@brief Open a SQLite connection with bounded lock waiting."""
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        """@brief Create durable tables while allowing concurrent app starts."""
        with _INITIALIZE_LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_items (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    active_job TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    item_id TEXT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_id TEXT,
                    permalink TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, platform)
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduled_publications (
                    item_id TEXT NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,
                    privacy_status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (item_id, platform)
                );
                CREATE INDEX IF NOT EXISTS scheduled_publications_due
                    ON scheduled_publications(status, scheduled_at);
                CREATE TABLE IF NOT EXISTS asset_library (
                    id TEXT PRIMARY KEY,
                    asset_name TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    source_description TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    license_name TEXT NOT NULL,
                    license_url TEXT NOT NULL,
                    rights_basis TEXT NOT NULL,
                    attribution TEXT NOT NULL,
                    license_expires_on TEXT,
                    rights_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    item_a TEXT NOT NULL REFERENCES content_items(id),
                    item_b TEXT NOT NULL REFERENCES content_items(id),
                    label_a TEXT NOT NULL,
                    label_b TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, item_a, item_b)
                );
                """
            )

    def create_item(self, values: dict[str, Any]) -> dict[str, Any]:
        """@brief Create a draft item and return its display representation."""
        item_id = str(uuid4())
        now = _now()
        payload = {**values, "publications": {}, "assets": {}, "review": None, "plan": None, "research": None, "materials": [], "approved_at": None, "platform_variants": {}, "version_history": [], "approval_history": []}
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO content_items(id, topic, payload, stage, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (item_id, values["topic"], json.dumps(payload, ensure_ascii=False), "draft", now, now),
            )
        return self.get_item(item_id)

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        """@brief Load one item and its publication records, or return None."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM content_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            item = dict(row)
            item.update(json.loads(item.pop("payload")))
            item["publications"] = {
                publication["platform"]: dict(publication)
                for publication in connection.execute("SELECT * FROM publications WHERE item_id = ?", (item_id,))
            }
            item["schedules"] = {
                schedule["platform"]: dict(schedule)
                for schedule in connection.execute("SELECT * FROM scheduled_publications WHERE item_id = ?", (item_id,))
            }
            return item

    def list_items(self) -> list[dict[str, Any]]:
        """@brief Return recent items in reverse creation order for the dashboard."""
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM content_items ORDER BY created_at DESC").fetchall()
        return [item for row in rows if (item := self.get_item(row["id"])) is not None]

    def update_payload(self, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """@brief Merge project data and return the updated item."""
        with self._connect() as connection:
            # Acquire the write reservation before reading to prevent concurrent JSON-payload updates from overwriting each other.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM content_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            payload = json.loads(row["payload"])
            payload.update(changes)
            connection.execute(
                "UPDATE content_items SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), _now(), item_id),
            )
        return self.get_item(item_id)

    def get_setting(self, key: str, default: Any = None) -> Any:
        """@brief Load one JSON-encoded application setting or return its default."""
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def save_revision_snapshot(self, item_id: str, reason: str) -> dict[str, Any]:
        """@brief Preserve an immutable bounded snapshot before an editorial change.

        @param item_id Existing content item identifier.
        @param reason Short operator-facing explanation for the snapshot.
        @returns The saved revision entry.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM content_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            payload = json.loads(row["payload"])
            history = payload.get("version_history") if isinstance(payload.get("version_history"), list) else []
            revision = {
                "id": str(uuid4()),
                "created_at": _now(),
                "reason": str(reason)[:120],
                "plan": payload.get("plan"),
                "platform_variants": payload.get("platform_variants", {}),
                "rights_assets": payload.get("rights_assets", []),
                "review": payload.get("review"),
                "approved_at": payload.get("approved_at"),
                "approval_record": payload.get("approval_record"),
            }
            payload["version_history"] = [*history, revision][-50:]
            connection.execute(
                "UPDATE content_items SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), _now(), item_id),
            )
            connection.execute(
                "UPDATE scheduled_publications SET status='canceled', error='Content changed after scheduling; review and schedule again.', updated_at=? WHERE item_id=? AND status='scheduled'",
                (_now(), item_id),
            )
        return revision

    def record_approval(self, item_id: str, approved_at: str, approval_record: dict[str, Any]) -> dict[str, Any]:
        """@brief Atomically save current approval and append its bounded audit event.

        @param item_id Existing content item identifier.
        @param approved_at Time at which the operator approved the reviewed artifact.
        @param approval_record Exact plan, media, copy, and rights evidence approved.
        @returns Updated content item with the new approval history entry.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM content_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            payload = json.loads(row["payload"])
            history = payload.get("approval_history") if isinstance(payload.get("approval_history"), list) else []
            history.append({"id": str(uuid4()), "approved_at": approved_at, **approval_record})
            payload.update({"approved_at": approved_at, "approved_asset_sha256": approval_record.get("asset_sha256"), "approval_record": approval_record, "approval_history": history[-50:]})
            connection.execute(
                "UPDATE content_items SET payload=?, stage='approved', updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), _now(), item_id),
            )
        return self.get_item(item_id)

    def cancel_pending_schedules(self, item_id: str, reason: str) -> int:
        """@brief Cancel all not-yet-dispatched schedules after media or approval changes."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_publications SET status='canceled', error=?, updated_at=? WHERE item_id=? AND status='scheduled'",
                (reason[:1600], _now(), item_id),
            )
        return cursor.rowcount

    def save_asset_library_entry(self, asset: dict[str, Any]) -> dict[str, Any]:
        """@brief Create or update one reusable rights-reviewed asset record."""
        asset_id = str(asset.get("id") or uuid4())
        now = _now()
        values = (
            asset_id, asset["asset_name"], asset["asset_type"], asset.get("source_description", ""),
            asset.get("source_url", ""), asset.get("license_name", ""), asset.get("license_url", ""),
            asset.get("rights_basis", ""), asset.get("attribution", ""), asset.get("license_expires_on"),
            asset.get("rights_status", "unverified"), now, now,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO asset_library(id, asset_name, asset_type, source_description, source_url, license_name, license_url, rights_basis, attribution, license_expires_on, rights_status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET asset_name=excluded.asset_name, asset_type=excluded.asset_type, source_description=excluded.source_description, source_url=excluded.source_url, license_name=excluded.license_name, license_url=excluded.license_url, rights_basis=excluded.rights_basis, attribution=excluded.attribution, license_expires_on=excluded.license_expires_on, rights_status=excluded.rights_status, updated_at=excluded.updated_at",
                values,
            )
        return self.get_asset_library_entry(asset_id)

    def get_asset_library_entry(self, asset_id: str) -> dict[str, Any] | None:
        """@brief Load one reusable asset record by its stable identifier."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM asset_library WHERE id = ?", (asset_id,)).fetchone()
        return dict(row) if row else None

    def list_asset_library(self) -> list[dict[str, Any]]:
        """@brief Return reusable asset records in recently updated order."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM asset_library ORDER BY updated_at DESC, asset_name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def delete_asset_library_entry(self, asset_id: str) -> bool:
        """@brief Remove a reusable library record without changing copied item ledgers."""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM asset_library WHERE id = ?", (asset_id,))
        return cursor.rowcount == 1

    def schedule_publication(self, item_id: str, platform: str, scheduled_at: str, timezone_name: str, privacy_status: str) -> dict[str, Any]:
        """@brief Atomically create or replace a future publication reservation."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM content_items WHERE id = ?", (item_id,)).fetchone():
                raise KeyError(item_id)
            existing = connection.execute("SELECT status FROM scheduled_publications WHERE item_id = ? AND platform = ?", (item_id, platform)).fetchone()
            publication = connection.execute("SELECT status FROM publications WHERE item_id = ? AND platform = ?", (item_id, platform)).fetchone()
            if existing and existing["status"] in {"dispatching", "running"}:
                raise RuntimeError("This scheduled publication is already being processed.")
            if publication and publication["status"] in {"publishing", "published", "uncertain"}:
                raise RuntimeError(f"Publication is already {publication['status']}; scheduling is blocked to prevent duplicates.")
            connection.execute(
                "INSERT INTO scheduled_publications(item_id, platform, status, scheduled_at, timezone_name, privacy_status, error, created_at, updated_at) VALUES(?, ?, 'scheduled', ?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(item_id, platform) DO UPDATE SET status='scheduled', scheduled_at=excluded.scheduled_at, timezone_name=excluded.timezone_name, privacy_status=excluded.privacy_status, error=NULL, updated_at=excluded.updated_at",
                (item_id, platform, scheduled_at, timezone_name, privacy_status, now, now),
            )
        return next(row for row in self.list_schedules() if row["item_id"] == item_id and row["platform"] == platform)

    def list_schedules(self) -> list[dict[str, Any]]:
        """@brief Return scheduled and historical publication jobs with item titles."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.*, i.topic FROM scheduled_publications s JOIN content_items i ON i.id=s.item_id ORDER BY s.scheduled_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_due_schedules(self, now: str) -> list[dict[str, Any]]:
        """@brief Claim due rows once across app processes before publication dispatch."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT item_id, platform FROM scheduled_publications WHERE status='scheduled' AND scheduled_at <= ? ORDER BY scheduled_at LIMIT 20",
                (now,),
            ).fetchall()
            claimed: list[tuple[str, str]] = [(row["item_id"], row["platform"]) for row in rows]
            connection.executemany(
                "UPDATE scheduled_publications SET status='dispatching', updated_at=? WHERE item_id=? AND platform=? AND status='scheduled'",
                [(_now(), item_id, platform) for item_id, platform in claimed],
            )
        return [row for row in self.list_schedules() if (row["item_id"], row["platform"]) in claimed and row["status"] == "dispatching"]

    def set_schedule_state(self, item_id: str, platform: str, status: str, error: str | None = None) -> None:
        """@brief Persist a scheduled publication lifecycle transition and safe error."""
        allowed = {"scheduled", "dispatching", "running", "completed", "failed", "uncertain", "canceled"}
        if status not in allowed:
            raise ValueError("Unsupported scheduled publication state.")
        with self._connect() as connection:
            connection.execute(
                "UPDATE scheduled_publications SET status=?, error=?, updated_at=? WHERE item_id=? AND platform=?",
                (status, error[:1600] if error else None, _now(), item_id, platform),
            )

    def cancel_schedule(self, item_id: str, platform: str) -> bool:
        """@brief Cancel a schedule only before dispatch has reserved publication."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scheduled_publications SET status='canceled', updated_at=? WHERE item_id=? AND platform=? AND status='scheduled'",
                (_now(), item_id, platform),
            )
        return cursor.rowcount == 1

    def recover_interrupted_schedules(self) -> int:
        """@brief Fail interrupted schedules without retrying possibly accepted posts."""
        message = "App restarted during scheduled publication; inspect the platform before retrying."
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE scheduled_publications SET status=CASE "
                "WHEN EXISTS (SELECT 1 FROM publications p WHERE p.item_id=scheduled_publications.item_id AND p.platform=scheduled_publications.platform AND p.status='published') THEN 'completed' "
                "WHEN EXISTS (SELECT 1 FROM publications p WHERE p.item_id=scheduled_publications.item_id AND p.platform=scheduled_publications.platform AND p.status IN ('publishing','uncertain')) THEN 'uncertain' ELSE 'failed' END, "
                "error=CASE WHEN status IN ('dispatching','running') THEN ? ELSE error END, updated_at=? WHERE status IN ('dispatching','running')",
                (message, _now()),
            )
        return cursor.rowcount

    def create_experiment(self, values: dict[str, Any]) -> dict[str, Any]:
        """@brief Link two published items as manually measured A/B variants."""
        experiment_id = str(uuid4())
        now = _now()
        item_a, item_b = values["item_a"], values["item_b"]
        if item_a == item_b:
            raise ValueError("An experiment requires two different content items.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item_id in (item_a, item_b):
                if not connection.execute("SELECT 1 FROM content_items WHERE id=?", (item_id,)).fetchone():
                    raise KeyError(item_id)
                publication = connection.execute("SELECT status FROM publications WHERE item_id=? AND platform=?", (item_id, values["platform"])).fetchone()
                if not publication or publication["status"] != "published":
                    raise RuntimeError("Both experiment items must already be published to the selected platform.")
            duplicate = connection.execute(
                "SELECT 1 FROM experiments WHERE platform=? AND ((item_a=? AND item_b=?) OR (item_a=? AND item_b=?))",
                (values["platform"], item_a, item_b, item_b, item_a),
            ).fetchone()
            if duplicate:
                raise RuntimeError("These two posts are already linked in an experiment.")
            connection.execute(
                "INSERT INTO experiments(id, name, hypothesis, platform, item_a, item_b, label_a, label_b, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (experiment_id, values["name"], values["hypothesis"], values["platform"], item_a, item_b, values["label_a"], values["label_b"], now, now),
            )
        return self.get_experiment(experiment_id)

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        """@brief Return one experiment joined to its two item titles."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT e.*, a.topic AS topic_a, b.topic AS topic_b FROM experiments e JOIN content_items a ON a.id=e.item_a JOIN content_items b ON b.id=e.item_b WHERE e.id=?",
                (experiment_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_experiments(self) -> list[dict[str, Any]]:
        """@brief Return experiments newest first with their current comparison records."""
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM experiments ORDER BY created_at DESC").fetchall()
        return [experiment for row in rows if (experiment := self.get_experiment(row["id"])) is not None]

    def set_experiment_status(self, experiment_id: str, status: str) -> dict[str, Any] | None:
        """@brief Mark a comparison active or complete without altering its evidence."""
        if status not in {"active", "completed"}:
            raise ValueError("Experiment status must be active or completed.")
        with self._connect() as connection:
            connection.execute("UPDATE experiments SET status=?, updated_at=? WHERE id=?", (status, _now(), experiment_id))
        return self.get_experiment(experiment_id)

    def set_setting(self, key: str, value: Any) -> None:
        """@brief Atomically persist one non-secret application setting as JSON."""
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, encoded, _now()),
            )

    def append_analytics_snapshot(self, item_id: str, platform: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """@brief Append a bounded metrics snapshot only to a successfully published target."""
        if platform not in {"youtube", "instagram"}:
            raise ValueError("Unsupported analytics platform.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM content_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            publication = connection.execute(
                "SELECT status FROM publications WHERE item_id = ? AND platform = ?",
                (item_id, platform),
            ).fetchone()
            if not publication or publication["status"] != "published":
                raise RuntimeError("Metrics can be recorded only for a successfully published platform item.")
            payload = json.loads(row["payload"])
            analytics = payload.get("analytics") if isinstance(payload.get("analytics"), dict) else {}
            records = analytics.get(platform) if isinstance(analytics.get(platform), list) else []
            records.append(snapshot)
            analytics[platform] = records[-100:]
            payload["analytics"] = analytics
            connection.execute(
                "UPDATE content_items SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), _now(), item_id),
            )
        return self.get_item(item_id)

    def set_stage(self, item_id: str, stage: str, error: str | None = None) -> None:
        """@brief Persist a stage transition and clear completed worker ownership."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE content_items SET stage = ?, active_job = NULL, error = ?, updated_at = ? WHERE id = ?",
                (stage, error, _now(), item_id),
            )

    def claim_job(self, item_id: str, stage: str) -> str:
        """@brief Atomically claim an item for a costly or long-running stage."""
        job_id = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT stage, active_job FROM content_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            if row["active_job"] or row["stage"] in BUSY_STATES:
                raise RuntimeError("Another workflow operation is already running for this item.")
            connection.execute(
                "UPDATE content_items SET stage = ?, active_job = ?, error = NULL, updated_at = ? WHERE id = ?",
                (stage, job_id, _now(), item_id),
            )
        return job_id

    def finish_job(self, item_id: str, job_id: str, stage: str, error: str | None = None) -> None:
        """@brief Finish only the job that still owns the item's active state."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE content_items SET stage = ?, active_job = NULL, error = ?, updated_at = ? WHERE id = ? AND active_job = ?",
                (stage, error, _now(), item_id, job_id),
            )

    def recover_interrupted_jobs(self) -> int:
        """@brief Mark interrupted jobs without silently retrying provider costs."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE content_items SET stage = 'failed', active_job = NULL, error = 'App restarted during this operation; inspect the saved artifacts before retrying.', updated_at = ? WHERE active_job IS NOT NULL",
                (_now(),),
            )
            return cursor.rowcount

    def reserve_publication(self, item_id: str, platform: str, *, allow_scheduled_dispatch: bool = False) -> None:
        """@brief Reserve publication and reject duplicate or uncertain retries."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM publications WHERE item_id = ? AND platform = ?",
                (item_id, platform),
            ).fetchone()
            if row and row["status"] in {"publishing", "published", "uncertain"}:
                raise RuntimeError(f"Publication is already {row['status']}; automatic retry is blocked to prevent duplicates.")
            schedule = connection.execute(
                "SELECT status FROM scheduled_publications WHERE item_id=? AND platform=?",
                (item_id, platform),
            ).fetchone()
            if schedule and schedule["status"] in {"scheduled", "dispatching", "running"}:
                if not (allow_scheduled_dispatch and schedule["status"] == "dispatching"):
                    raise RuntimeError("Cancel the pending scheduled post before publishing immediately.")
            connection.execute(
                "INSERT INTO publications(item_id, platform, status, updated_at) VALUES(?, ?, 'publishing', ?) ON CONFLICT(item_id, platform) DO UPDATE SET status = 'publishing', remote_id = NULL, permalink = NULL, error = NULL, updated_at = excluded.updated_at",
                (item_id, platform, _now()),
            )

    def finish_publication(self, item_id: str, platform: str, status: str, *, remote_id: str | None = None, permalink: str | None = None, error: str | None = None) -> None:
        """@brief Persist a publication result without credentials."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE publications SET status = ?, remote_id = ?, permalink = ?, error = ?, updated_at = ? WHERE item_id = ? AND platform = ?",
                (status, remote_id, permalink, error, _now(), item_id, platform),
            )
