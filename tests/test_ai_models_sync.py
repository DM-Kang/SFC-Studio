"""Unit tests for dynamic, non-hardcoded AI provider model catalog synchronization."""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from shortform_studio.ai_providers import (
    clear_model_list_cache,
    generate_text,
    list_all_provider_models,
    list_provider_models,
    sync_ready_provider_models,
)


class LiveAIModelCatalogTests(unittest.TestCase):
    """Verify live model discovery, filtering, caching, and error isolation across all providers."""

    def setUp(self) -> None:
        clear_model_list_cache()

    def tearDown(self) -> None:
        clear_model_list_cache()

    def test_openai_models_filters_non_text_models_and_sorts_newest_first(self) -> None:
        """@brief Exclude audio/image/embedding models and keep text/reasoning models."""
        payload = {
            "data": [
                {"id": "whisper-1", "created": 100},
                {"id": "tts-1", "created": 200},
                {"id": "dall-e-3", "created": 300},
                {"id": "text-embedding-3-large", "created": 400},
                {"id": "gpt-image-1", "created": 500},
                {"id": "gpt-4.1-mini", "created": 600},
                {"id": "gpt-5", "created": 900},
                {"id": "o3", "created": 800},
            ]
        }
        with (
            patch("shortform_studio.config.get_secret", side_effect=lambda k, d="": "sk-test" if k == "OPENAI_API_KEY" else d),
            patch("shortform_studio.ai_providers._request_json", return_value=payload) as req,
        ):
            result = list_provider_models("openai_api", force_refresh=True)
            cached = list_provider_models("openai_api", force_refresh=False)

        self.assertIsNone(result["error"])
        self.assertEqual([m["id"] for m in result["models"]], ["gpt-5", "o3", "gpt-4.1-mini"])
        self.assertEqual(cached["models"], result["models"])
        self.assertEqual(req.call_count, 1)

    def test_gemini_oauth_models_filters_generate_content_and_strips_prefix(self) -> None:
        """@brief Query Google Generative Language API live and retain text/JSON generateContent models."""
        payload = {
            "models": [
                {"name": "models/text-embedding-004", "displayName": "Text Embedding", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/lyria-3.5", "displayName": "Lyria 3.5", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/nano-banana-pro-preview", "displayName": "Nano Banana Pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-image", "displayName": "Gemini 2.5 Flash Image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts", "displayName": "Gemini 2.5 Flash TTS", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemma-4-31b-it", "displayName": "Gemma 4 31B IT", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            ]
        }
        fake_creds = types.SimpleNamespace(token="goog-token")
        with (
            patch("shortform_studio.ai_providers.load_ai_settings", return_value={"gemini_google_cloud_project": "proj-1", "ollama_base_url": "http://127.0.0.1:11434"}),
            patch("shortform_studio.ai_providers._load_gemini_credentials", return_value=fake_creds),
            patch("shortform_studio.ai_providers._request_json", return_value=payload) as req,
        ):
            result = list_provider_models("gemini_oauth", force_refresh=True)

        self.assertIsNone(result["error"])
        self.assertEqual([m["id"] for m in result["models"]], ["gemini-2.5-pro", "gemini-2.5-flash", "gemma-4-31b-it"])
        self.assertIn("Gemini 2.5 Flash", result["models"][1]["label"])
        self.assertEqual(req.call_args.kwargs["headers"]["x-goog-user-project"], "proj-1")

    def test_anthropic_models_uses_messages_models_endpoint(self) -> None:
        """@brief Fetch live Claude models with Anthropic API headers."""
        payload = {
            "data": [
                {"id": "claude-sonnet-4-5-20250929", "display_name": "Claude Sonnet 4.5"},
                {"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5"},
            ]
        }
        with (
            patch("shortform_studio.config.get_secret", side_effect=lambda k, d="": "anth-key" if k == "ANTHROPIC_API_KEY" else d),
            patch("shortform_studio.ai_providers._request_json", return_value=payload) as req,
        ):
            result = list_provider_models("anthropic_api", force_refresh=True)

        self.assertIsNone(result["error"])
        self.assertEqual(len(result["models"]), 2)
        self.assertEqual(result["models"][0]["label"], "claude-sonnet-4-5-20250929 (Claude Sonnet 4.5)")
        self.assertEqual(req.call_args.kwargs["headers"]["x-api-key"], "anth-key")

    def test_chatgpt_oauth_and_ollama_models_and_error_isolation(self) -> None:
        """@brief Handle live ChatGPT/Ollama lookups and isolate provider errors gracefully."""
        with (
            patch("shortform_studio.ai_providers._chatgpt_access_token", side_effect=RuntimeError("Connect Sign in with ChatGPT")),
            patch("shortform_studio.ai_providers.load_ai_settings", return_value={"ollama_base_url": "http://127.0.0.1:11434"}),
            patch("shortform_studio.ai_providers._ollama_models", return_value=["qwen3:8b", "llama3.3:latest"]),
            patch("shortform_studio.config.get_secret", return_value=""),
            patch("shortform_studio.ai_providers._load_gemini_credentials", side_effect=RuntimeError("Connect Google OAuth")),
        ):
            all_catalogs = list_all_provider_models(force_refresh=True)

        self.assertEqual(set(all_catalogs.keys()), {"openai_api", "chatgpt_oauth", "gemini_api", "gemini_oauth", "anthropic_api", "ollama"})
        self.assertEqual([m["id"] for m in all_catalogs["ollama"]["models"]], ["qwen3:8b", "llama3.3:latest"])
        self.assertEqual(all_catalogs["chatgpt_oauth"]["models"], [])
        self.assertIn("Connect Sign in with ChatGPT", all_catalogs["chatgpt_oauth"]["error"])
        self.assertIn("OPENAI_API_KEY", all_catalogs["openai_api"]["error"])
        self.assertIn("GEMINI_API_KEY", all_catalogs["gemini_api"]["error"])

    def test_sync_ready_provider_models_only_queries_ready_providers(self) -> None:
        """@brief Periodic server sync only refreshes providers whose credentials are ready."""
        statuses = {
            "openai_api": {"ready": False},
            "chatgpt_oauth": {"ready": False},
            "gemini_oauth": {"ready": True},
            "anthropic_api": {"ready": False},
            "ollama": {"ready": True},
        }
        with (
            patch("shortform_studio.ai_providers.get_provider_statuses", return_value=statuses),
            patch("shortform_studio.ai_providers.list_provider_models", side_effect=lambda p, force_refresh=True: {"provider": p, "models": [], "error": None}) as mock_list,
        ):
            synced = sync_ready_provider_models(force_refresh=True)

        self.assertEqual(set(synced.keys()), {"gemini_oauth", "ollama"})
        self.assertEqual(mock_list.call_count, 2)

    def test_gemini_api_key_models_and_generation_use_api_key_header(self) -> None:
        """@brief Support Google AI Studio GEMINI_API_KEY for both live model discovery and generation."""
        models_payload = {
            "models": [
                {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        gen_payload = {"candidates": [{"content": {"parts": [{"text": "{\"ok\":true}"}]}}]}
        with (
            patch("shortform_studio.config.get_secret", side_effect=lambda k, d="": "ai-studio-key" if k == "GEMINI_API_KEY" else d),
            patch("shortform_studio.ai_providers._request_json", side_effect=[models_payload, gen_payload]) as req,
        ):
            catalog = list_provider_models("gemini_api", force_refresh=True)
            output = generate_text("gemini_api", "gemini-2.5-flash", "sys", "usr")

        self.assertEqual([m["id"] for m in catalog["models"]], ["gemini-2.5-flash"])
        self.assertEqual(req.call_args_list[0].kwargs["headers"]["x-goog-api-key"], "ai-studio-key")
        self.assertEqual(req.call_args_list[1].kwargs["headers"]["x-goog-api-key"], "ai-studio-key")
        self.assertEqual(output, '{"ok":true}')

    def test_gemini_falls_back_on_deprecated_404_or_503_model(self) -> None:
        """@brief Fall back cleanly to a current Gemini flash model when an older ID returns HTTP 404 or 503."""
        gen_payload = {"candidates": [{"content": {"parts": [{"text": "{\"ok\":true}"}]}}]}
        with (
            patch("shortform_studio.config.get_secret", side_effect=lambda k, d="": "ai-studio-key" if k == "GEMINI_API_KEY" else d),
            patch(
                "shortform_studio.ai_providers._request_json",
                side_effect=[RuntimeError("AI provider request failed with HTTP 404 (not found)"), gen_payload],
            ) as req,
        ):
            output = generate_text("gemini_api", "gemini-2.5-flash", "sys", "usr")
        self.assertEqual(output, '{"ok":true}')
        self.assertIn("gemini-3-flash-preview:generateContent", req.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
