"""Unit tests for Phase 3 TikTok publishing, R2 cleanup, analytics CSV, .env secrets, and direct research."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from shortform_studio.analytics import (
    build_analytics_dashboard,
    export_analytics_csv,
    parse_analytics_csv,
)
from shortform_studio.brand import normalize_brand_settings
from shortform_studio.config import get_managed_secrets_status, update_local_env_secrets
from shortform_studio.platform_content import tiktok_title
from shortform_studio.preflight import build_publish_preflight
from shortform_studio.publishing import (
    PublicationError,
    delete_r2_object,
    publish_instagram,
    publish_tiktok,
    publish_youtube,
    update_youtube_privacy,
)
from shortform_studio.resources import fetch_direct_url_source
from shortform_studio.store import Store
from shortform_studio.workflow import (
    add_direct_url_research,
    cleanup_instagram_r2_object,
    run_planning,
    run_publication,
    toggle_research_source_inclusion,
)


class Phase3PublishingTests(unittest.TestCase):
    """Verify TikTok adapter, R2 cleanup, and YouTube publishAt / privacy updates."""

    def test_tiktok_preflight_and_publish_upload(self) -> None:
        """@brief Validate TikTok metadata preflight and two-step Content Posting upload."""
        preflight_ok = build_publish_preflight(
            {"title": "Hook", "caption": "Quick tip", "hashtags": ["#tips"]},
            ["tiktok"],
            {"available": True, "passed": True},
            [{"asset_name": "Final", "included": True, "rights_status": "cleared", "rights_basis": "Original"}],
            {},
        )
        self.assertEqual(preflight_ok["status"], "ready")
        self.assertEqual(tiktok_title({"caption": "Quick tip #tips", "hashtags": ["#tips", "fyp"]}), "Quick tip #tips #fyp")

        preflight_bad = build_publish_preflight(
            {"title": "T", "caption": "x" * 2201, "hashtags": []},
            ["tiktok"],
            {"available": True, "passed": True},
            [{"asset_name": "Final", "included": True, "rights_status": "cleared", "rights_basis": "Original"}],
            {},
        )
        self.assertEqual(preflight_bad["status"], "blocked")

        brand = normalize_brand_settings(
            {
                "profile": {"language": "Korean"},
                "series_templates": [{"name": "TT Series", "platforms": ["youtube", "tiktok"], "duration_seconds": 15}],
            }
        )
        self.assertEqual(brand["series_templates"][0]["platforms"], ["youtube", "tiktok"])

        with tempfile.TemporaryDirectory() as directory:
            mp4 = Path(directory) / "video.mp4"
            mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)
            calls: list[tuple[str, str]] = []

            class _Resp:
                def __init__(self, status: int, body: bytes) -> None:
                    self.status = status
                    self._body = body

                def read(self) -> bytes:
                    return self._body

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

            def fake_urlopen(req, timeout=60):
                calls.append((req.get_method(), req.full_url))
                if "video/init" in req.full_url:
                    return _Resp(200, json.dumps({"data": {"publish_id": "tt-pub-1", "upload_url": "https://upload.tiktokapis.com/chunk/1"}}).encode())
                return _Resp(201, b"")

            with (
                patch("shortform_studio.publishing.get_secret", side_effect=lambda k, d="": "tt-token" if k == "TIKTOK_ACCESS_TOKEN" else d),
                patch("shortform_studio.publishing.urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                res = publish_tiktok(mp4, {"caption": "Hello TikTok", "hashtags": ["#fyp"]}, "SELF_ONLY")

            self.assertEqual(res["remote_id"], "tt-pub-1")
            self.assertEqual(calls[0][0], "POST")
            self.assertEqual(calls[1][0], "PUT")

            with self.assertRaises(PublicationError):
                publish_tiktok(mp4, {"caption": "Hi"}, "INVALID_PRIVACY")

    def test_instagram_auto_cleanup_and_manual_r2_cleanup(self) -> None:
        """@brief Automatically delete staged R2 objects after Instagram upload and support manual cleanup."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mp4 = root / "final.mp4"
            mp4.write_bytes(b"mp4")

            def fake_ig_req(url, *, method, fields, token, timeout=45):
                if method == "POST" and url.endswith("/media"):
                    return {"id": "c1"}
                if method == "GET":
                    return {"status_code": "FINISHED"}
                return {"id": "ig-1"}

            with (
                patch("shortform_studio.publishing.get_secret", side_effect=lambda k, d="": "val" if k in {"INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"} else d),
                patch("shortform_studio.publishing.upload_to_r2", return_value=("shortform/item/1.mp4", "https://cdn.example/1.mp4")),
                patch("shortform_studio.publishing._instagram_request", side_effect=fake_ig_req),
                patch("shortform_studio.publishing.delete_r2_object", return_value=True) as mock_del,
            ):
                res = publish_instagram("item-1", mp4, "Caption")
            self.assertEqual(res["r2_cleaned"], "true")
            mock_del.assert_called_once_with("shortform/item/1.mp4")

            with self.assertRaises(ValueError):
                delete_r2_object("wrong-prefix/file.mp4")

            store = Store(root / "studio.sqlite3")
            item = store.create_item({"topic": "R2 leftover", "platforms": ["instagram"]})
            store.update_payload(item["id"], {"assets": {"instagram_staged_object": "shortform/x/2.mp4"}})
            with patch("shortform_studio.workflow.delete_r2_object", return_value=True):
                cleaned = cleanup_instagram_r2_object(store, item["id"])
            self.assertTrue(cleaned["cleaned"])
            self.assertNotIn("instagram_staged_object", store.get_item(item["id"])["assets"])

    def test_youtube_native_publish_at_and_privacy_update(self) -> None:
        """@brief Force private+publishAt on scheduled YouTube uploads and support post-publish privacy updates."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mp4 = root / "final.mp4"
            mp4.write_bytes(b"mp4")
            (root / "youtube-token.json").write_text('{"refresh_token": "r"}', encoding="utf-8")
            secrets_file = root / "yt.json"
            secrets_file.write_text("{}", encoding="utf-8")

            fake_service = MagicMock()
            fake_service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "yt-99"})
            fake_creds_obj = MagicMock(valid=True, expired=False)
            fake_creds_obj.to_json.return_value = '{"refresh_token": "r"}'

            fake_modules = self._fake_google_modules(fake_service, fake_creds_obj)
            with (
                patch.dict(sys.modules, fake_modules),
                patch("shortform_studio.publishing.get_app_home", return_value=root),
                patch("shortform_studio.publishing.get_secret", side_effect=lambda k, d="": str(secrets_file) if k == "YOUTUBE_CLIENT_SECRETS_FILE" else d),
            ):
                publish_youtube(mp4, {"title": "T", "caption": "C", "hashtags": []}, "public", publish_at="2027-01-01T12:00:00+00:00")
                insert_body = fake_service.videos.return_value.insert.call_args.kwargs["body"]
                self.assertEqual(insert_body["status"]["privacyStatus"], "private")
                self.assertEqual(insert_body["status"]["publishAt"], "2027-01-01T12:00:00+00:00")

                res = update_youtube_privacy("yt-99", "unlisted")
                self.assertEqual(res["privacy_status"], "unlisted")
                fake_service.videos.return_value.update.assert_called_once()

    def _fake_google_modules(self, fake_service, fake_creds_obj):
        fake_google = types.ModuleType("google")
        fake_auth = types.ModuleType("google.auth")
        fake_transport = types.ModuleType("google.auth.transport")
        fake_requests = types.ModuleType("google.auth.transport.requests")
        fake_requests.Request = MagicMock()
        fake_oauth2 = types.ModuleType("google.oauth2")
        fake_creds = types.ModuleType("google.oauth2.credentials")
        fake_creds.Credentials = MagicMock()
        fake_creds.Credentials.from_authorized_user_file.return_value = fake_creds_obj
        fake_oauthlib = types.ModuleType("google_auth_oauthlib")
        fake_flow = types.ModuleType("google_auth_oauthlib.flow")
        fake_flow.InstalledAppFlow = MagicMock()
        fake_apiclient = types.ModuleType("googleapiclient")
        fake_discovery = types.ModuleType("googleapiclient.discovery")
        fake_discovery.build = MagicMock(return_value=fake_service)
        fake_http = types.ModuleType("googleapiclient.http")
        fake_http.MediaFileUpload = MagicMock()
        return {
            "google": fake_google,
            "google.auth": fake_auth,
            "google.auth.transport": fake_transport,
            "google.auth.transport.requests": fake_requests,
            "google.oauth2": fake_oauth2,
            "google.oauth2.credentials": fake_creds,
            "google_auth_oauthlib": fake_oauthlib,
            "google_auth_oauthlib.flow": fake_flow,
            "googleapiclient": fake_apiclient,
            "googleapiclient.discovery": fake_discovery,
            "googleapiclient.http": fake_http,
        }


class Phase3DashboardSecretsAndResearchTests(unittest.TestCase):
    """Verify analytics dashboard/CSV, local .env secrets manager, direct URL fetch, and job cancel."""

    def test_analytics_dashboard_and_csv_roundtrip_with_injection_protection(self) -> None:
        """@brief Aggregate series/platform metrics and neutralize formula prefixes in exported CSV."""
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "studio.sqlite3")
            item = store.create_item(
                {
                    "topic": "=SUM(A1)",
                    "platforms": ["youtube", "tiktok"],
                    "series_template_snapshot": {"name": "Daily Tips"},
                }
            )
            store.reserve_publication(item["id"], "youtube")
            store.finish_publication(item["id"], "youtube", "published", remote_id="yt1")
            store.append_analytics_snapshot(
                item["id"],
                "youtube",
                {"measured_at": "2026-11-01T10:00:00+00:00", "views": 1000, "likes": 50, "watch_time_seconds": 12000, "notes": "+bad"},
            )

            items = store.list_items()
            dashboard = build_analytics_dashboard(items)
            self.assertEqual(dashboard["platforms"]["youtube"]["total_views"], 1000)
            self.assertEqual(dashboard["series"][0]["series_name"], "Daily Tips")

            csv_text = export_analytics_csv(items)
            self.assertIn("'=SUM(A1)", csv_text)
            self.assertIn("'+bad", csv_text)

            parsed = parse_analytics_csv(
                f"item_id,platform,measured_at,views,likes,notes\n{item['id']},tiktok,2026-11-02T12:00:00+00:00,500,25,test\n"
            )
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["views"], 500)
            with self.assertRaises(ValueError):
                parse_analytics_csv("wrong,headers\n1,2\n")

    def test_secrets_update_writes_private_env_without_exposing_values(self) -> None:
        """@brief Persist updated .env keys with owner-only access and expose only boolean flags."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("shortform_studio.config.get_app_home", return_value=root),
                patch.dict(os.environ, {}, clear=False),
            ):
                status = update_local_env_secrets({"PEXELS_API_KEY": "secret-pexels-val", "OPENAI_MODEL": "gpt-4.1-mini"})
                self.assertTrue(status["PEXELS_API_KEY"])
                self.assertTrue(status["OPENAI_MODEL"])
                self.assertNotIn("secret-pexels-val", json.dumps(get_managed_secrets_status()))
                env_file = root / ".env"
                self.assertTrue(env_file.is_file())
                self.assertIn("PEXELS_API_KEY=secret-pexels-val", env_file.read_text(encoding="utf-8"))

                status_after_clear = update_local_env_secrets({}, clear_keys=["PEXELS_API_KEY"])
                self.assertFalse(status_after_clear["PEXELS_API_KEY"])

                with self.assertRaises(ValueError):
                    update_local_env_secrets({"UNSUPPORTED_KEY": "123"})

    def test_load_local_environment_populates_when_process_env_has_empty_placeholders(self) -> None:
        """@brief Fill empty Compose env_file placeholders from local .env without overriding non-empty values."""
        from shortform_studio.config import load_local_environment

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TAVILY_API_KEY=saved-tavily\nOPENAI_API_KEY=saved-openai\n", encoding="utf-8")
            with (
                patch("shortform_studio.config.get_app_home", return_value=root),
                patch.dict(os.environ, {"TAVILY_API_KEY": "", "OPENAI_API_KEY": "explicit-override"}, clear=False),
            ):
                load_local_environment()
                self.assertEqual(os.environ["TAVILY_API_KEY"], "saved-tavily")
                self.assertEqual(os.environ["OPENAI_API_KEY"], "explicit-override")

    def test_direct_url_research_ssrf_guard_source_toggle_and_cancel(self) -> None:
        """@brief Block private IP resolution on URL fetch, allow source inclusion toggles, and cancel jobs cleanly."""
        with patch("shortform_studio.resources.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(ValueError, "not a public IP"):
                fetch_direct_url_source("https://rebind.example.com/secret")

        fake_html = io.BytesIO(b"<html><head><title>Test Source</title></head><body><p>Verified fact here.</p></body></html>")
        fake_html.__enter__ = lambda self: self
        fake_html.__exit__ = lambda self, *args: None

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "studio.sqlite3")
            item = store.create_item({"topic": "Direct URL test", "platforms": ["youtube"]})
            with (
                patch("shortform_studio.resources.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]),
                patch("shortform_studio.resources.urllib.request.urlopen", return_value=fake_html),
            ):
                updated = add_direct_url_research(store, item["id"], "https://example.com/article")
            self.assertEqual(updated["research"]["sources"][0]["title"], "Test Source")

            toggled = toggle_research_source_inclusion(store, item["id"], "S1", False)
            self.assertEqual(toggled["research"]["evidence_source_ids"], [])

            with patch("shortform_studio.workflow.ai_ready", return_value=False):
                run_planning(store, item["id"])
            self.assertEqual(store.get_item(item["id"])["plan"]["sources"], [])

            store.claim_job(item["id"], "planning")
            store.set_job_progress(item["id"], "Running step", 50)
            store.request_job_cancel(item["id"])
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                run_planning(store, item["id"])


if __name__ == "__main__":
    unittest.main()
