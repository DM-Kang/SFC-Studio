"""Unit tests for Phase 2 multi-scene assembly, audio, thumbnails, and AI plan editing."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from shortform_studio.media import (
    build_multiscene_ffmpeg_command,
    extract_video_frame,
    generate_voiceover,
    render_video,
    text_to_fallback_srt,
)
from shortform_studio.planning import normalize_editable_scenes, suggest_hooks
from shortform_studio.publishing import publish_instagram, publish_youtube
from shortform_studio.resources import download_pexels_clip, search_pexels_videos
from shortform_studio.store import Store
from shortform_studio.workflow import (
    download_stock_material_clip,
    revise_plan,
    run_hook_suggestions,
    run_plan_autofix,
    run_postprocess_video,
    run_scene_assembly,
    save_thumbnail_from_video,
)


class Phase2PlanningTests(unittest.TestCase):
    """Verify scene editing, hook suggestions, and review-driven AI autofix."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = Store(self.root / "studio.sqlite3")
        self.item = self.store.create_item({"topic": "Espresso extraction", "language": "English", "platforms": ["youtube"]})

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_revise_plan_preserves_explicit_scenes_and_durations(self) -> None:
        """@brief Keep operator-edited scenes when explicitly provided in plan updates."""
        self.store.update_payload(self.item["id"], {"plan_revision": 1, "plan": {"revision": 1, "script": "Old", "scenes": []}})
        updated = revise_plan(
            self.store,
            self.item["id"],
            {
                "title": "Updated",
                "script": "New script",
                "scenes": [
                    {"narration": "Scene one line", "visual_query": "espresso shot", "duration_seconds": 4.5},
                    {"narration": "Scene two line", "visual_query": "latte pour", "duration_seconds": 6.0},
                ],
            },
        )
        scenes = updated["plan"]["scenes"]
        self.assertEqual(len(scenes), 2)
        self.assertEqual(scenes[0]["visual_query"], "espresso shot")
        self.assertEqual(scenes[1]["duration_seconds"], 6.0)
        self.assertEqual(normalize_editable_scenes([], "fallback", "topic")[0]["narration"], "fallback")

    def test_hook_suggestions_offline_and_ai_modes(self) -> None:
        """@brief Generate bounded hook candidates both offline and via mocked AI."""
        self.store.update_payload(self.item["id"], {"plan": {"revision": 1, "title": "Espresso", "hook": "Old hook", "script": "Script"}})
        with patch("shortform_studio.workflow.ai_ready", return_value=False):
            offline = run_hook_suggestions(self.store, self.item["id"], count=5)
        self.assertEqual(len(offline), 5)
        self.assertEqual(self.store.get_item(self.item["id"])["hook_suggestions"], offline)

        with patch("shortform_studio.planning.generate_json", return_value=({"hooks": ["Hook A", "Hook B"]}, "openai:gpt-4.1")):
            ai_hooks = suggest_hooks({"topic": "Espresso", "language": "English"}, {"hook": "Old"}, True, count=2)
        self.assertEqual(ai_hooks, ["Hook A", "Hook B"])

    def test_plan_autofix_saves_snapshot_and_resets_approval(self) -> None:
        """@brief Apply review feedback via AI while preserving history and invalidating approval."""
        self.store.update_payload(
            self.item["id"],
            {
                "plan_revision": 1,
                "plan": {
                    "revision": 1,
                    "title": "Old",
                    "hook": "Old hook",
                    "script": "Old claim without source",
                    "caption": "Old caption",
                    "hashtags": [],
                    "video_prompt": "Old prompt",
                    "duration_seconds": 20,
                    "sources": [{"id": "S1", "url": "https://example.com/s1", "status": "fetched"}],
                },
                "review": {"status": "needs_changes", "editorial": {"issues": ["Unsourced claim"]}, "unverified_claims": ["Old claim"]},
            },
        )
        self.store.record_approval(self.item["id"], "2026-01-01T00:00:00+00:00", {"asset_sha256": "abc", "plan_revision": 1})
        ai_payload = {
            "title": "Fixed Title",
            "hook": "Better Hook",
            "script": "Cautious script citing S1.",
            "caption": "Fixed caption",
            "hashtags": ["#coffee"],
            "video_prompt": "Vertical coffee extraction",
            "duration_seconds": 20,
            "claims": [{"claim": "Brewing uses pressure", "source_ids": ["S1"], "evidence": "From S1"}],
            "scenes": [{"narration": "Cautious script", "visual_query": "espresso machine"}],
        }
        with (
            patch("shortform_studio.workflow.ai_ready", return_value=True),
            patch("shortform_studio.planning.generate_json", return_value=(ai_payload, "openai_api:gpt-4.1-mini")),
        ):
            updated = run_plan_autofix(self.store, self.item["id"])

        self.assertEqual(updated["plan_revision"], 2)
        self.assertEqual(updated["plan"]["title"], "Fixed Title")
        self.assertEqual(updated["stage"], "planned")
        self.assertIsNone(updated["review"])
        self.assertIsNone(updated["approved_at"])
        self.assertEqual(len(updated["version_history"]), 1)


class Phase2MediaAndAssemblyTests(unittest.TestCase):
    """Verify multi-scene FFmpeg graphs, BGM ducking, OpenAI TTS, and thumbnails."""

    def test_build_multiscene_command_supports_images_xfade_ducking_and_subtitles(self) -> None:
        """@brief Construct valid FFmpeg filter_complex graphs for multi-scene assembly."""
        cmd = build_multiscene_ffmpeg_command(
            "ffmpeg",
            [
                {"relative_path": "clips/one.mp4", "duration_seconds": 4.0, "media_type": "video"},
                {"relative_path": "clips/two.jpg", "duration_seconds": 5.0, "media_type": "image"},
            ],
            "final.assembling.mp4",
            transition="fade",
            audio_name="voiceover.mp3",
            captions_name="captions.srt",
            subtitle_style={"preset": "bold_center", "font_size": 50},
            bgm_name="bgm.mp3",
            bgm_volume=0.15,
            ducking=True,
            loudnorm=True,
        )
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("zoompan=", fc)
        self.assertIn("xfade=transition=fade", fc)
        self.assertIn("subtitles=captions.srt", fc)
        self.assertIn("sidechaincompress=", fc)
        self.assertIn("amix=inputs=2", fc)
        self.assertIn("loudnorm=", fc)

        with self.assertRaises(ValueError):
            build_multiscene_ffmpeg_command("ffmpeg", [], "out.mp4")
        with self.assertRaises(ValueError):
            build_multiscene_ffmpeg_command("ffmpeg", [{"relative_path": "a.mp4"}], "out.mp4", transition="wipe")

    def test_render_video_supports_bgm_ducking_and_loudnorm(self) -> None:
        """@brief Route BGM sidechain ducking and loudness normalization through filter_complex."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            audio = root / "voiceover.mp3"
            bgm = root / "bgm.mp3"
            output = root / "final.mp4"
            source.write_bytes(b"v")
            audio.write_bytes(b"a")
            bgm.write_bytes(b"b")
            captured: list[list[str]] = []

            def fake_run(command, **kwargs):
                captured.append(command)
                (root / "final.rendering.mp4").write_bytes(b"rendered")
                return type("Res", (), {"returncode": 0, "stderr": ""})()

            with (
                patch("shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("shortform_studio.media.subprocess.run", side_effect=fake_run),
            ):
                render_video(source, output, audio_path=audio, bgm_path=bgm, bgm_volume=0.2, ducking=True, loudnorm=True)

            fc = captured[0][captured[0].index("-filter_complex") + 1]
            self.assertIn("sidechaincompress", fc)
            self.assertIn("loudnorm", fc)

    def test_openai_tts_and_fallback_srt_generation(self) -> None:
        """@brief Generate OpenAI TTS audio and proportional SRT cues without ElevenLabs."""
        srt = text_to_fallback_srt("First short sentence. Second slightly longer sentence here.", 6.0)
        self.assertIn("00:00:00,000 -->", srt)
        self.assertIn("First short sentence.", srt)

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            fake_response = io.BytesIO(b"ID3-mp3-bytes")
            fake_response.__enter__ = lambda self: self
            fake_response.__exit__ = lambda self, *args: None
            with (
                patch("shortform_studio.media.get_secret", side_effect=lambda k, d="": "sk-test" if k == "OPENAI_API_KEY" else d),
                patch("shortform_studio.media.urllib.request.urlopen", return_value=fake_response),
                patch("shortform_studio.media.probe_audio_duration", return_value=4.0),
            ):
                vo = generate_voiceover("Hello world. Second line.", out_dir, tts_provider="openai_tts", voice="nova")
            self.assertEqual(vo["provider"], "openai_tts")
            self.assertEqual(vo["voice_id"], "nova")
            self.assertTrue(Path(vo["audio_path"]).is_file())
            self.assertTrue(Path(vo["captions_path"]).is_file())

    def test_pexels_download_and_multiscene_workflow_record_unverified_rights(self) -> None:
        """@brief Download Pexels clips safely and track rights during multi-scene assembly."""
        fake_pexels_api = {
            "videos": [
                {
                    "id": 99,
                    "url": "https://www.pexels.com/video/99/",
                    "image": "https://images.pexels.com/99.jpg",
                    "duration": 8,
                    "width": 1080,
                    "height": 1920,
                    "user": {"name": "Alice", "url": "https://www.pexels.com/@alice"},
                    "video_files": [
                        {"file_type": "video/mp4", "width": 720, "height": 1280, "link": "https://videos.pexels.com/99_hd.mp4"},
                    ],
                }
            ]
        }
        with patch("shortform_studio.resources._get_json", return_value=fake_pexels_api):
            materials = search_pexels_videos([{"id": "scene-1", "visual_query": "coffee"}], "pexels-key")
        self.assertEqual(materials[0]["download_url"], "https://videos.pexels.com/99_hd.mp4")

        with self.assertRaises(ValueError):
            download_pexels_clip("http://127.0.0.1/evil.mp4", Path("/tmp/x.mp4"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "studio.sqlite3")
            projects_dir = root / "projects"
            item = store.create_item({"topic": "Coffee assembly", "platforms": ["youtube"]})
            store.update_payload(
                item["id"],
                {
                    "plan_revision": 1,
                    "plan": {"revision": 1, "script": "Coffee narration", "scenes": [{"id": "scene-1", "visual_query": "coffee"}]},
                    "materials": materials,
                },
            )
            fake_stream = io.BytesIO(b"\x00\x00\x00\x18ftypmp42")
            fake_stream.__enter__ = lambda self: self
            fake_stream.__exit__ = lambda self, *args: None
            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.resources.urllib.request.urlopen", return_value=fake_stream),
            ):
                updated = download_stock_material_clip(store, item["id"], "99")
            self.assertTrue(Path(updated["materials"][0]["local_path"]).is_file())
            self.assertTrue(any(r["rights_status"] == "unverified" for r in updated["rights_assets"]))

            def fake_assemble(work_dir, clips, out_path, **kwargs):
                out_path.write_bytes(b"master-mp4")
                return out_path

            def fake_render(src, out_path, **kwargs):
                out_path.write_bytes(b"final-mp4")
                return out_path

            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.workflow.generate_voiceover", return_value=None),
                patch("shortform_studio.workflow.assemble_multiscene_video", side_effect=fake_assemble),
                patch("shortform_studio.workflow.render_video", side_effect=fake_render),
            ):
                stage = run_scene_assembly(store, item["id"], [{"source_kind": "pexels", "provider_id": "99", "duration_seconds": 5}], transition="fade")
            self.assertEqual(stage, "generated")
            assembled_item = store.get_item(item["id"])
            self.assertEqual(assembled_item["assets"]["video_provider"]["provider"], "multiscene_ffmpeg")
            self.assertEqual(assembled_item["assets"]["video_provider"]["transition"], "fade")

    def test_pixabay_search_download_and_stock_multiscene_assembly(self) -> None:
        """@brief Search Pixabay free stock videos, download locally, and assemble via stock source_kind."""
        from shortform_studio.resources import search_pixabay_videos

        fake_pixabay_api = {
            "hits": [
                {
                    "id": 777,
                    "pageURL": "https://pixabay.com/videos/id-777/",
                    "tags": "coffee, cafe",
                    "duration": 10,
                    "picture_id": "pic-777",
                    "user": "PixCreator",
                    "user_id": 55,
                    "videos": {
                        "large": {"url": "https://cdn.pixabay.com/video/777_large.mp4", "width": 1080, "height": 1920},
                        "medium": {"url": "https://cdn.pixabay.com/video/777_med.mp4", "width": 720, "height": 1280, "thumbnail": "https://cdn.pixabay.com/video/777_640x360.jpg"},
                    },
                }
            ]
        }
        with patch("shortform_studio.resources._get_json", return_value=fake_pixabay_api):
            materials = search_pixabay_videos([{"id": "scene-1", "visual_query": "coffee"}], "pix-key")

        self.assertEqual(materials[0]["provider"], "Pixabay")
        self.assertEqual(materials[0]["provider_id"], "pixabay-777")
        self.assertEqual(materials[0]["download_url"], "https://cdn.pixabay.com/video/777_large.mp4")
        self.assertEqual(materials[0]["poster_url"], "https://cdn.pixabay.com/video/777_640x360.jpg")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "studio.sqlite3")
            projects_dir = root / "projects"
            item = store.create_item({"topic": "Pixabay assembly", "platforms": ["youtube"]})
            store.update_payload(
                item["id"],
                {
                    "plan_revision": 1,
                    "plan": {"revision": 1, "script": "Coffee narration", "scenes": [{"id": "scene-1", "visual_query": "coffee"}]},
                    "materials": materials,
                },
            )
            fake_stream = io.BytesIO(b"\x00\x00\x00\x18ftypmp42")
            fake_stream.__enter__ = lambda self: self
            fake_stream.__exit__ = lambda self, *args: None
            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.resources.urllib.request.urlopen", return_value=fake_stream),
                patch("shortform_studio.workflow.generate_voiceover", return_value=None),
                patch("shortform_studio.workflow.assemble_multiscene_video", side_effect=lambda wd, clips, op, **kw: op.write_bytes(b"m") or op),
                patch("shortform_studio.workflow.render_video", side_effect=lambda s, op, **kw: op.write_bytes(b"f") or op),
            ):
                stage = run_scene_assembly(store, item["id"], [{"source_kind": "stock", "provider_id": "pixabay-777", "duration_seconds": 4}], transition="cut")
            self.assertEqual(stage, "generated")
            assembled = store.get_item(item["id"])
            self.assertTrue(any("Pixabay" in r.get("source_description", "") for r in assembled["rights_assets"]))

    def test_postprocess_and_thumbnail_extract_and_publish_hooks(self) -> None:
        """@brief Extract thumbnails, postprocess uploaded MP4s, and pass thumbnails to publishers."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "studio.sqlite3")
            projects_dir = root / "projects"
            item = store.create_item({"topic": "Postprocess", "platforms": ["youtube", "instagram"]})
            item_dir = projects_dir / item["id"]
            item_dir.mkdir(parents=True)
            mp4 = item_dir / "final.mp4"
            mp4.write_bytes(b"uploaded-mp4")
            store.update_payload(item["id"], {"plan_revision": 1, "plan": {"revision": 1, "script": "Hi"}, "assets": {"source_video": str(mp4), "final_video": str(mp4)}})

            def fake_ff_run(command, **kwargs):
                if "thumbnail.frame.jpg" in command:
                    (item_dir / "thumbnail.frame.jpg").write_bytes(b"jpeg-bytes")
                return type("Res", (), {"returncode": 0, "stderr": ""})()

            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.media.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("shortform_studio.media.subprocess.run", side_effect=fake_ff_run),
            ):
                thumb_item = save_thumbnail_from_video(store, item["id"], timestamp_seconds=1.2)
            self.assertTrue(Path(thumb_item["assets"]["thumbnail_path"]).is_file())
            self.assertEqual(thumb_item["assets"]["thumbnail_offset_ms"], 1200)

            with (
                patch("shortform_studio.workflow.get_projects_dir", return_value=projects_dir),
                patch("shortform_studio.workflow.render_video", side_effect=lambda s, o, **kw: o.write_bytes(b"post") or o),
            ):
                post_item = run_postprocess_video(store, item["id"], apply_subtitles=False, apply_bgm=False, loudnorm=True)
            self.assertTrue((item_dir / "source_original.mp4").is_file())
            self.assertEqual(Path(post_item["assets"]["final_video"]).resolve(), mp4.resolve())

            # Verify YouTube passes thumbnail via thumbnails().set and degrades gracefully if thumbnail fails.
            thumb_file = Path(thumb_item["assets"]["thumbnail_path"])
            fake_service = MagicMock()
            fake_service.videos.return_value.insert.return_value.next_chunk.return_value = (None, {"id": "yt-vid-1"})
            fake_google_auth = types.ModuleType("google")
            fake_auth = types.ModuleType("google.auth")
            fake_transport = types.ModuleType("google.auth.transport")
            fake_requests = types.ModuleType("google.auth.transport.requests")
            fake_requests.Request = MagicMock()
            fake_oauth2 = types.ModuleType("google.oauth2")
            fake_creds = types.ModuleType("google.oauth2.credentials")
            fake_creds.Credentials = MagicMock()
            fake_oauthlib = types.ModuleType("google_auth_oauthlib")
            fake_flow = types.ModuleType("google_auth_oauthlib.flow")
            fake_flow.InstalledAppFlow = MagicMock()
            fake_apiclient = types.ModuleType("googleapiclient")
            fake_discovery = types.ModuleType("googleapiclient.discovery")
            fake_discovery.build = MagicMock(return_value=fake_service)
            fake_http = types.ModuleType("googleapiclient.http")
            fake_http.MediaFileUpload = MagicMock(side_effect=lambda *a, **kw: ("media", a[0]))
            fake_creds_obj = MagicMock(valid=True, expired=False)
            fake_creds_obj.to_json.return_value = '{"refresh_token": "r"}'
            fake_creds.Credentials.from_authorized_user_file.return_value = fake_creds_obj
            token_path = root / "youtube-token.json"
            token_path.write_text('{"refresh_token": "r"}', encoding="utf-8")
            secrets_file = root / "yt-secret.json"
            secrets_file.write_text("{}", encoding="utf-8")
            fake_modules = {
                "google": fake_google_auth,
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
            with (
                patch.dict(sys.modules, fake_modules),
                patch("shortform_studio.publishing.get_app_home", return_value=root),
                patch("shortform_studio.publishing.get_secret", side_effect=lambda k, d="": str(secrets_file) if k == "YOUTUBE_CLIENT_SECRETS_FILE" else d),
            ):
                yt_ok = publish_youtube(mp4, {"title": "T", "caption": "C", "hashtags": []}, "private", thumbnail_path=thumb_file)
                self.assertEqual(yt_ok["thumbnail_uploaded"], "true")
                fake_service.thumbnails.return_value.set.side_effect = RuntimeError("Unverified channel")
                yt_warn = publish_youtube(mp4, {"title": "T", "caption": "C", "hashtags": []}, "private", thumbnail_path=thumb_file)
                self.assertIn("thumbnail_warning", yt_warn)

            # Verify Instagram passes thumb_offset when provided.
            ig_calls: list[dict] = []

            def fake_ig_req(url, *, method, fields, token, timeout=45):
                ig_calls.append({"url": url, "method": method, "fields": fields})
                if method == "POST" and url.endswith("/media"):
                    return {"id": "container-1"}
                if method == "GET":
                    return {"status_code": "FINISHED"}
                return {"id": "media-1"}

            with (
                patch(
                    "shortform_studio.publishing.get_secret",
                    side_effect=lambda k, d="": "val" if k in {"INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"} else d,
                ),
                patch("shortform_studio.publishing.upload_to_r2", return_value=("k", "https://cdn.example/v.mp4")),
                patch("shortform_studio.publishing._instagram_request", side_effect=fake_ig_req),
            ):
                ig_res = publish_instagram(item["id"], mp4, "Caption", thumb_offset_ms=1200)
            self.assertEqual(ig_res["remote_id"], "media-1")
            self.assertEqual(ig_calls[0]["fields"]["thumb_offset"], "1200")


if __name__ == "__main__":
    unittest.main()
