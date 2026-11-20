"""Tests for safe, user-local AI provider settings."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.ai_config import load_ai_settings, save_ai_settings, validate_ollama_url


class AIConfigTests(unittest.TestCase):
    """Verify AI settings persistence and local endpoint restrictions."""

    def test_stage_models_are_saved_without_credentials(self) -> None:
        """@brief Persist different planning and review providers without token fields."""
        with tempfile.TemporaryDirectory() as temporary:
            payload = {
                "planning": {"provider": "gemini_oauth", "model": "gemini-2.5-flash"},
                "review": {"provider": "ollama", "model": "qwen3:8b"},
                "gemini_client_secrets_file": "C:/Users/test/client.json",
                "gemini_google_cloud_project": "shortform-test",
                "ollama_base_url": "http://localhost:11434",
            }
            with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": temporary}):
                saved = save_ai_settings(payload)
                loaded = load_ai_settings()
                stored_text = (Path(temporary) / "ai-settings.json").read_text(encoding="utf-8")

        self.assertEqual(saved, loaded)
        self.assertNotIn("access_token", json.loads(stored_text))
        self.assertEqual(loaded["planning"]["provider"], "gemini_oauth")
        self.assertEqual(loaded["review"]["provider"], "ollama")

    def test_rejects_non_loopback_ollama_endpoints(self) -> None:
        """@brief Prevent the configurable local-model URL from targeting remote hosts."""
        with self.assertRaisesRegex(ValueError, "localhost"):
            validate_ollama_url("http://example.com:11434")
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_ollama_url("http://user:pass@127.0.0.1:11434")

    def test_docker_host_gateway_is_allowed_only_when_compose_enables_it(self) -> None:
        """@brief Permit the exact Docker host gateway only in the explicit Compose runtime."""
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_ALLOW_HOST_OLLAMA": ""}):
            with self.assertRaisesRegex(ValueError, "host.docker.internal"):
                validate_ollama_url("http://host.docker.internal:11434")
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_ALLOW_HOST_OLLAMA": "1"}):
            self.assertEqual(validate_ollama_url("http://host.docker.internal:11434"), "http://host.docker.internal:11434")

    def test_invalid_persisted_ollama_url_falls_back_to_loopback(self) -> None:
        """@brief Ignore a tampered settings file that points Ollama off-machine."""
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "ai-settings.json"
            settings_path.write_text(json.dumps({"ollama_base_url": "http://169.254.169.254"}), encoding="utf-8")
            with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": temporary}):
                settings = load_ai_settings()

        self.assertEqual(settings["ollama_base_url"], "http://127.0.0.1:11434")

    def test_legacy_cli_provider_settings_migrate_to_supported_interfaces(self) -> None:
        """@brief Keep older provider choices readable after switching to supported API flows."""
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "ai-settings.json"
            settings_path.write_text(json.dumps({"planning": {"provider": "codex_cli", "model": "gpt-6.1-sol"}, "review": {"provider": "claude_code", "model": "sonnet"}}), encoding="utf-8")
            with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": temporary}):
                settings = load_ai_settings()

        self.assertEqual(settings["planning"], {"provider": "chatgpt_oauth", "model": "gpt-6.1-sol"})
        self.assertEqual(settings["review"], {"provider": "anthropic_api", "model": "claude-sonnet-4-5"})


if __name__ == "__main__":
    unittest.main()
