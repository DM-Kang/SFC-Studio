"""Durable workflow-state and duplicate-publication regression tests."""

import tempfile
import unittest
from pathlib import Path

from shortform_studio.store import Store


class StoreTests(unittest.TestCase):
    """Exercise state ownership with isolated temporary SQLite databases."""

    def setUp(self) -> None:
        """@brief Create a fresh temporary store before each test."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary_directory.name) / "test.sqlite3")
        self.item = self.store.create_item({"topic": "Test topic", "platforms": ["youtube"]})

    def tearDown(self) -> None:
        """@brief Remove only this test's temporary database directory."""
        self.temporary_directory.cleanup()

    def test_job_claim_is_exclusive_and_owner_can_finish(self) -> None:
        """@brief Reject a concurrent claim and preserve the first job's result."""
        job_id = self.store.claim_job(self.item["id"], "planning")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.store.claim_job(self.item["id"], "generating")
        self.store.finish_job(self.item["id"], job_id, "planned")
        self.assertEqual(self.store.get_item(self.item["id"])["stage"], "planned")

    def test_restart_marks_interrupted_operation_without_retry(self) -> None:
        """@brief Convert a durable active job into a visible interrupted error."""
        self.store.claim_job(self.item["id"], "generating")
        self.assertEqual(self.store.recover_interrupted_jobs(), 1)
        recovered = self.store.get_item(self.item["id"])
        self.assertEqual(recovered["stage"], "failed")
        self.assertIsNone(recovered["active_job"])
        self.assertIn("inspect the saved artifacts", recovered["error"])

    def test_publication_reservation_blocks_duplicate_and_uncertain_retry(self) -> None:
        """@brief Prevent public duplicate posts after a pending or uncertain call."""
        self.store.reserve_publication(self.item["id"], "youtube")
        with self.assertRaisesRegex(RuntimeError, "already publishing"):
            self.store.reserve_publication(self.item["id"], "youtube")
        self.store.finish_publication(self.item["id"], "youtube", "uncertain", error="timeout")
        with self.assertRaisesRegex(RuntimeError, "already uncertain"):
            self.store.reserve_publication(self.item["id"], "youtube")

    def test_failed_publication_can_be_retried_after_known_failure(self) -> None:
        """@brief Allow retry only when the adapter recorded a definite failure."""
        self.store.reserve_publication(self.item["id"], "youtube")
        self.store.finish_publication(self.item["id"], "youtube", "failed", error="OAuth setup failed")
        self.store.reserve_publication(self.item["id"], "youtube")
        self.assertEqual(self.store.get_item(self.item["id"])["publications"]["youtube"]["status"], "publishing")

    def test_analytics_requires_successful_publication_and_persists_snapshots(self) -> None:
        """@brief Persist manual analytics only after the platform reports success."""
        with self.assertRaisesRegex(RuntimeError, "only for a successfully published"):
            self.store.append_analytics_snapshot(self.item["id"], "youtube", {"views": 12})

        self.store.reserve_publication(self.item["id"], "youtube")
        self.store.finish_publication(self.item["id"], "youtube", "published", remote_id="video-1")
        updated = self.store.append_analytics_snapshot(self.item["id"], "youtube", {"views": 12})

        self.assertEqual(updated["analytics"]["youtube"], [{"views": 12}])

    def test_application_settings_round_trip_json_values(self) -> None:
        """@brief Store reusable non-secret settings independently of work items."""
        value = {"profile": {"brand_name": "Studio"}, "series_templates": []}

        self.store.set_setting("brand_settings", value)

        self.assertEqual(self.store.get_setting("brand_settings"), value)


if __name__ == "__main__":
    unittest.main()
