"""Regression tests for editorial management, schedules, experiments, and recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from shortform_studio.analytics import summarize_experiment
from shortform_studio.backups import create_backup_archive, restore_backup_archive, validate_and_extract_backup
from shortform_studio.platform_content import copy_fingerprint, instagram_caption, platform_plan
from shortform_studio.preflight import build_publish_preflight
from shortform_studio.store import Store
from shortform_studio.workflow import can_reuse_video_for_plan


class ManagementStoreTests(unittest.TestCase):
    """Exercise new management records against isolated SQLite state."""

    def setUp(self) -> None:
        """@brief Create an isolated database and a pair of content items."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = Store(self.root / "studio.sqlite3")
        self.item_a = self.store.create_item({"topic": "Variant A", "platforms": ["youtube"]})
        self.item_b = self.store.create_item({"topic": "Variant B", "platforms": ["youtube"]})

    def tearDown(self) -> None:
        """@brief Remove only the per-test temporary directory."""
        self.temporary_directory.cleanup()

    def test_schedule_claim_cancel_and_interrupted_recovery_are_one_shot(self) -> None:
        """@brief Claim due rows once and mark interrupted work uncertain without retry."""
        self.store.schedule_publication(self.item_a["id"], "youtube", "2026-01-01T00:00:00+00:00", "UTC", "private")
        first_claim = self.store.claim_due_schedules("2026-01-01T00:00:01+00:00")
        self.assertEqual(len(first_claim), 1)
        self.assertEqual(self.store.claim_due_schedules("2026-01-01T00:00:02+00:00"), [])
        self.store.reserve_publication(self.item_a["id"], "youtube", allow_scheduled_dispatch=True)
        self.store.set_schedule_state(self.item_a["id"], "youtube", "running")
        self.store.finish_publication(self.item_a["id"], "youtube", "uncertain", error="timeout")

        self.assertEqual(self.store.recover_interrupted_schedules(), 1)
        schedule = self.store.get_item(self.item_a["id"])["schedules"]["youtube"]
        self.assertEqual(schedule["status"], "uncertain")
        self.assertFalse(self.store.cancel_schedule(self.item_a["id"], "youtube"))

    def test_pending_schedule_blocks_immediate_publication_reservation(self) -> None:
        """@brief Prevent a scheduled post and a separate immediate post from racing."""
        self.store.schedule_publication(self.item_a["id"], "youtube", "2026-12-01T00:00:00+00:00", "UTC", "private")

        with self.assertRaisesRegex(RuntimeError, "Cancel the pending scheduled post"):
            self.store.reserve_publication(self.item_a["id"], "youtube")

        self.assertTrue(self.store.cancel_schedule(self.item_a["id"], "youtube"))
        self.store.reserve_publication(self.item_a["id"], "youtube")

    def test_rights_library_copy_is_independent_and_expiry_persists(self) -> None:
        """@brief Preserve reusable provenance and expiry metadata in local SQLite."""
        saved = self.store.save_asset_library_entry({"asset_name": "Licensed music", "asset_type": "music", "license_expires_on": "2030-01-01", "rights_status": "cleared"})
        self.assertEqual(saved["license_expires_on"], "2030-01-01")
        self.assertTrue(self.store.delete_asset_library_entry(saved["id"]))
        self.assertEqual(self.store.list_asset_library(), [])

    def test_approval_history_and_revision_history_are_bounded_and_atomic(self) -> None:
        """@brief Persist the approval latch and immutable audit record together."""
        approved = self.store.record_approval(self.item_a["id"], "2026-01-01T00:00:00+00:00", {"asset_sha256": "abc", "plan_revision": 3, "manual_reviewed": True})

        self.assertEqual(approved["stage"], "approved")
        self.assertEqual(approved["approved_asset_sha256"], "abc")
        self.assertEqual(approved["approval_history"][0]["plan_revision"], 3)
        snapshot = self.store.save_revision_snapshot(self.item_a["id"], "changed copy")
        self.assertEqual(snapshot["approval_record"]["asset_sha256"], "abc")

    def test_experiment_requires_distinct_published_posts_and_summarizes_manual_metrics(self) -> None:
        """@brief Link only published variants and surface latest recorded observations."""
        for item in (self.item_a, self.item_b):
            self.store.reserve_publication(item["id"], "youtube")
            self.store.finish_publication(item["id"], "youtube", "published", remote_id=f"remote-{item['id']}")
        experiment = self.store.create_experiment({"name": "Hook test", "hypothesis": "Shorter hook may retain viewers", "platform": "youtube", "item_a": self.item_a["id"], "item_b": self.item_b["id"], "label_a": "Short", "label_b": "Long"})
        items = {self.item_a["id"]: {**self.item_a, "publications": {"youtube": {"status": "published", "updated_at": "2026-01-01T00:00:00+00:00"}}, "analytics": {"youtube": [{"measured_at": "2026-01-02T00:00:00+00:00", "views": 100, "likes": 5}]}}, self.item_b["id"]: {**self.item_b, "publications": {"youtube": {"status": "published", "updated_at": "2026-01-01T00:00:00+00:00"}}, "analytics": {"youtube": [{"measured_at": "2026-01-02T00:00:00+00:00", "views": 200, "likes": 4}]}}}

        summary = summarize_experiment(experiment, items)

        self.assertEqual([variant["views"] for variant in summary["variants"]], [100, 200])
        self.assertEqual(summary["variants"][0]["engagement_rate"], 0.05)
        self.assertEqual(summary["measurement_gap_hours"], 0.0)
        with self.assertRaisesRegex(RuntimeError, "already linked"):
            self.store.create_experiment({"name": "Duplicate", "hypothesis": "", "platform": "youtube", "item_a": self.item_b["id"], "item_b": self.item_a["id"], "label_a": "B", "label_b": "A"})


class PlatformCopyTests(unittest.TestCase):
    """Verify platform overlays and deterministic approval invalidation."""

    def test_platform_overlay_does_not_change_shared_narration(self) -> None:
        """@brief Apply platform copy without mutating the common production plan."""
        plan = {"title": "Shared", "script": "Narration", "caption": "Shared caption", "hashtags": ["#shared"]}
        variants = {"instagram": {"title": "Reel title", "caption": "Reel copy", "hashtags": ["#reels"]}}

        result = platform_plan(plan, variants, "instagram")

        self.assertEqual(result["title"], "Reel title")
        self.assertEqual(result["script"], "Narration")
        self.assertEqual(plan["caption"], "Shared caption")
        self.assertNotEqual(copy_fingerprint(plan, variants), copy_fingerprint(plan, {}))

    def test_instagram_caption_sends_channel_hashtags_once(self) -> None:
        """@brief Append Instagram hashtags to the actual caption without duplicating them."""
        result = instagram_caption({"caption": "Useful tip #shorts", "hashtags": ["#shorts", "reels"]})

        self.assertEqual(result, "Useful tip #shorts\n\n#reels")

    def test_version_restore_reuses_video_only_when_media_inputs_and_revision_match(self) -> None:
        """@brief Prevent stale artifacts from being relabeled as matching restored plans."""
        previous = {"revision": 4, "script": "same script", "video_prompt": "same prompt", "duration_seconds": 30}
        restored = {"revision": 2, "script": "same script", "video_prompt": "same prompt", "duration_seconds": 30}
        current_assets = {"generated_for_plan_revision": 4, "final_video": "/unused/test.mp4"}

        self.assertTrue(can_reuse_video_for_plan(previous, restored, current_assets))
        self.assertFalse(can_reuse_video_for_plan(previous, {**restored, "script": "changed"}, current_assets))
        self.assertFalse(can_reuse_video_for_plan(previous, restored, {**current_assets, "generated_for_plan_revision": 3}))

    def test_expired_included_license_blocks_preflight(self) -> None:
        """@brief Prevent publication when a used asset's recorded license has expired."""
        result = build_publish_preflight({"title": "Title", "caption": "Caption"}, ["youtube"], {"available": True, "passed": True}, [{"asset_name": "Music", "included": True, "rights_status": "cleared", "rights_basis": "Licensed", "license_expires_on": "2000-01-01"}], {})

        self.assertEqual(result["status"], "blocked")
        self.assertIn("license_expired", {check["code"] for check in result["checks"]})

    def test_instagram_preflight_counts_appended_hashtags(self) -> None:
        """@brief Enforce the caption limit against the exact text sent to Instagram."""
        result = build_publish_preflight({"title": "Title", "caption": "x" * 2195, "hashtags": []}, ["instagram"], {"available": True, "passed": True}, [], {}, {"instagram": {"title": "Internal", "caption": "x" * 2195, "hashtags": ["#longtag"]}})

        self.assertIn("caption_limit", {check["code"] for check in result["checks"]})


class BackupRecoveryTests(unittest.TestCase):
    """Validate archive contents, path safety, and reversible data replacement."""

    def test_backup_restore_replaces_database_and_projects_and_keeps_recovery_copy(self) -> None:
        """@brief Restore the chosen snapshot while preserving the prior state in recovery."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "studio.sqlite3"
            projects = root / "projects"
            projects.mkdir()
            store = Store(database)
            original = store.create_item({"topic": "Before", "platforms": ["youtube"]})
            (projects / "old.mp4").write_bytes(b"old")
            archive = root / "backup.zip"
            create_backup_archive(database, projects, archive)
            with zipfile.ZipFile(archive) as saved:
                self.assertEqual(set(saved.namelist()), {"manifest.json", "studio.sqlite3", "projects/old.mp4"})
                self.assertNotIn(".env", saved.namelist())
            changed = store.create_item({"topic": "After", "platforms": ["youtube"]})
            (projects / "new.mp4").write_bytes(b"new")

            recovery = restore_backup_archive(database, projects, archive, root / "recovery")
            restored = Store(database)

            self.assertTrue(recovery.is_file())
            self.assertIsNotNone(restored.get_item(original["id"]))
            self.assertIsNone(restored.get_item(changed["id"]))
            self.assertEqual([path.name for path in projects.iterdir()], ["old.mp4"])

    def test_backup_rejects_traversal_before_extracting(self) -> None:
        """@brief Reject ZIP traversal entries and do not write outside the staging folder."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "studio.sqlite3")
            staged_database = root / "valid.sqlite3"
            staged_database.write_bytes((root / "studio.sqlite3").read_bytes())
            archive_path = root / "malicious.zip"
            manifest = json.dumps({"format": "shortform-studio-backup-v1"})
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("manifest.json", manifest)
                archive.write(staged_database, "studio.sqlite3")
                archive.writestr("projects/../../escaped.txt", "no")
            staging = root / "staging"

            with self.assertRaisesRegex(ValueError, "unsafe archive path"):
                validate_and_extract_backup(archive_path, staging)
            self.assertFalse((root / "escaped.txt").exists())

    def test_invalid_restore_does_not_create_a_recovery_copy_or_replace_data(self) -> None:
        """@brief Validate an incoming backup before writing an unnecessary safety archive."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "studio.sqlite3"
            projects = root / "projects"
            projects.mkdir()
            store = Store(database)
            item = store.create_item({"topic": "Preserve me", "platforms": ["youtube"]})
            invalid = root / "invalid.zip"
            with zipfile.ZipFile(invalid, "w") as archive:
                archive.writestr("manifest.json", "not a valid manifest")
            recovery = root / "recovery"

            with self.assertRaises((ValueError, json.JSONDecodeError)):
                restore_backup_archive(database, projects, invalid, recovery)

            self.assertEqual(Store(database).get_item(item["id"])["topic"], "Preserve me")
            self.assertFalse(list(recovery.glob("pre-restore-*.zip")))


if __name__ == "__main__":
    unittest.main()
