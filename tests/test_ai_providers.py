"""Tests for provider OAuth, token handling, and structured API requests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shortform_studio.ai_providers import (
    _chatgpt_access_token,
    _ensure_chatgpt_identity_matches,
    _generate_anthropic,
    _generate_chatgpt,
    _perform_gemini_oauth,
    _perform_chatgpt_oauth,
    _save_chatgpt_tokens,
    _verify_chatgpt_id_token,
    chatgpt_oauth_token_path,
)


class AIProviderTests(unittest.TestCase):
    """Verify safe local OAuth storage and provider-specific request contracts."""

    def test_gemini_oauth_uses_matching_ipv4_loopback_callback_and_state(self) -> None:
        """@brief Validate Google's installed-app callback without contacting Google."""
        instances: list[object] = []

        class FakeFlow:
            """@brief Supply a deterministic OAuth client for the loopback test."""

            @classmethod
            def from_client_secrets_file(cls, client_file: str, **kwargs: object) -> object:
                """@brief Capture the client file and create the fake credential flow."""
                flow = cls()
                flow.client_file = client_file
                flow.options = kwargs
                flow.credentials = SimpleNamespace(to_json=lambda: "{}")
                instances.append(flow)
                return flow

            def authorization_url(self, **kwargs: object) -> tuple[str, str]:
                """@brief Return a stable state value for the simulated browser callback."""
                self.authorization_options = kwargs
                return "https://accounts.google.com/o/oauth2/v2/auth", "google-state"

            def fetch_token(self, *, code: str) -> None:
                """@brief Record the authorization code accepted by the client library."""
                self.authorization_code = code

        oauth_package = ModuleType("google_auth_oauthlib")
        oauth_package.__path__ = []
        flow_module = ModuleType("google_auth_oauthlib.flow")
        flow_module.InstalledAppFlow = FakeFlow
        with tempfile.TemporaryDirectory() as temporary:
            client_file = Path(temporary) / "client.json"
            client_file.write_text("{}", encoding="utf-8")
            with patch("shortform_studio.ai_providers.load_ai_settings", return_value={"gemini_client_secrets_file": str(client_file), "gemini_google_cloud_project": "test-project"}), patch.dict("sys.modules", {"google_auth_oauthlib": oauth_package, "google_auth_oauthlib.flow": flow_module}), patch("shortform_studio.ai_providers._save_gemini_token") as save_token:
                with patch("shortform_studio.ai_providers.wait_for_oauth_callback", return_value={"code": "google-code", "state": "google-state"}) as wait_callback:
                    _perform_gemini_oauth()

        self.assertEqual(instances[0].authorization_code, "google-code")
        self.assertIn("http://127.0.0.1:", instances[0].redirect_uri)
        self.assertEqual(wait_callback.call_args.args[1], "google-state")
        self.assertTrue(instances[0].options["autogenerate_code_verifier"])
        save_token.assert_called_once_with(instances[0].credentials)

    def test_oauth_flow_uses_state_nonce_pkce_and_persists_issued_registration(self) -> None:
        """@brief Validate the ChatGPT loopback flow shape without contacting OpenAI."""
        with patch("shortform_studio.ai_providers._load_chatgpt_profile", return_value={"host_id": "urn:uuid:11111111-2222-4333-8444-555555555555", "client_id": ""}), patch("shortform_studio.ai_providers._save_chatgpt_profile") as save_profile, patch("shortform_studio.ai_providers._request_form", return_value={"access_token": "access", "refresh_token": "refresh", "id_token": "identity", "expires_in": 3600, "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"}) as exchange, patch("shortform_studio.ai_providers._verify_chatgpt_id_token", return_value={"sub": "subject-1"}), patch("shortform_studio.ai_providers._save_chatgpt_tokens") as save_tokens:
            authorization_parameters: dict[str, list[str]] = {}

            def show_authorization_url(provider: str, url: str) -> None:
                """@brief Capture the browser URL emitted for the desktop OAuth handoff."""
                parameters = parse_qs(urlsplit(url).query)
                authorization_parameters.update(parameters)
                self.assertEqual(provider, "chatgpt_oauth")
                self.assertEqual(parameters["client_id"], ["dynamic_agent_client"])
                self.assertEqual(parameters["code_challenge_method"], ["S256"])
                self.assertEqual(parameters["agent_name_hint"], ["Shortform Content Studio"])
                self.assertIn("chatgpt.tokens.use.direct", parameters["scope"][0].split())

            def return_callback(server: object, expected_state: str, timeout_seconds: int = 300) -> dict[str, str]:
                """@brief Simulate a state-matched authorization callback without provider access."""
                self.assertEqual(authorization_parameters["state"], [expected_state])
                return {"code": "auth-code", "state": expected_state, "client_id": "oaiapp_test123"}

            with patch("shortform_studio.ai_providers.set_authorization_url", side_effect=show_authorization_url), patch("shortform_studio.ai_providers.wait_for_oauth_callback", side_effect=return_callback):
                _perform_chatgpt_oauth()

        save_profile.assert_called_once_with({"host_id": "urn:uuid:11111111-2222-4333-8444-555555555555", "client_id": "oaiapp_test123"})
        verifier = exchange.call_args.args[1]["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertEqual(authorization_parameters["code_challenge"], [challenge])
        self.assertEqual(exchange.call_args.args[1]["code"], "auth-code")
        self.assertEqual(exchange.call_args.args[1]["grant_type"], "authorization_code")
        self.assertEqual(save_tokens.call_args.args[0]["subject"], "subject-1")
        self.assertEqual(save_tokens.call_args.args[0]["client_id"], "oaiapp_test123")

    def test_chatgpt_identity_token_checks_signature_issuer_audience_and_nonce(self) -> None:
        """@brief Reject an ID token unless JWT and transaction identity checks match."""
        fake_jwt = ModuleType("jwt")
        fake_jwt.PyJWKClient = lambda *args, **kwargs: SimpleNamespace(get_signing_key_from_jwt=lambda value: SimpleNamespace(key="public-key"))
        fake_jwt.decode = lambda *args, **kwargs: {"sub": "user", "nonce": "expected"}
        with patch.dict("sys.modules", {"jwt": fake_jwt}):
            claims = _verify_chatgpt_id_token("signed-token", "oaiapp_client", "expected")
            with self.assertRaisesRegex(RuntimeError, "nonce"):
                _verify_chatgpt_id_token("signed-token", "oaiapp_client", "different")

        self.assertEqual(claims["sub"], "user")

    def test_chatgpt_refresh_rotates_tokens_and_keeps_session_ready(self) -> None:
        """@brief Refresh an expiring token and persist any replacement refresh token."""
        token = {"client_id": "oaiapp_client", "host_id": "urn:uuid:host", "refresh_token": "old-refresh", "access_token": "old-access", "expires_at": 0, "scopes": ["chatgpt.tokens.use.direct"]}
        with patch("shortform_studio.ai_providers._load_chatgpt_tokens", return_value=token), patch("shortform_studio.ai_providers._request_form", return_value={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1800}) as refresh, patch("shortform_studio.ai_providers._save_chatgpt_tokens") as save_tokens:
            access_token = _chatgpt_access_token()

        self.assertEqual(access_token, "new-access")
        self.assertEqual(refresh.call_args.args[1]["refresh_token"], "old-refresh")
        self.assertEqual(save_tokens.call_args.args[0]["refresh_token"], "new-refresh")

    def test_chatgpt_refresh_rejects_removed_plan_permission(self) -> None:
        """@brief Remove stored credentials when refresh revokes direct plan usage."""
        token = {"client_id": "oaiapp_client", "host_id": "urn:uuid:host", "refresh_token": "refresh", "access_token": "old-access", "expires_at": 0, "scopes": ["chatgpt.tokens.use.direct"]}
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "chatgpt-oauth-token.bin"
            token_path.write_bytes(b"stale credential")
            with patch("shortform_studio.ai_providers._load_chatgpt_tokens", return_value=token), patch("shortform_studio.ai_providers._request_form", return_value={"access_token": "new-access", "expires_in": 1800, "scope": "openid profile"}), patch("shortform_studio.ai_providers.chatgpt_oauth_token_path", return_value=token_path):
                with self.assertRaisesRegex(RuntimeError, "no longer grants plan usage"):
                    _chatgpt_access_token()
            self.assertFalse(token_path.exists())

    def test_chatgpt_reauthorization_rejects_a_different_saved_identity(self) -> None:
        """@brief Keep one issued local registration bound to its validated ChatGPT subject."""
        existing = {"client_id": "oaiapp_client", "subject": "saved-subject"}
        with patch("shortform_studio.ai_providers._load_chatgpt_tokens", return_value=existing):
            with self.assertRaisesRegex(RuntimeError, "different account"):
                _ensure_chatgpt_identity_matches("oaiapp_client", "other-subject")
        with patch("shortform_studio.ai_providers._load_chatgpt_tokens", return_value=existing):
            _ensure_chatgpt_identity_matches("oaiapp_client", "saved-subject")

    def test_chatgpt_response_is_streamed_and_not_persisted(self) -> None:
        """@brief Require a completed non-stored Responses API stream before returning text."""
        events = [
            {"type": "response.output_text.delta", "delta": '{"title":'},
            {"type": "response.output_text.delta", "delta": '"ok"}'},
            {"type": "response.completed"},
        ]
        stream = io.BytesIO(b"".join(f"data: {json.dumps(event)}\n\n".encode("utf-8") for event in events))
        captured: list[object] = []

        def open_stream(request: object, timeout: int) -> io.BytesIO:
            """@brief Capture the request and return a finite SSE response fixture."""
            captured.append(request)
            self.assertEqual(timeout, 10)
            return stream

        with patch("shortform_studio.ai_providers._chatgpt_access_token", return_value="oauth-access"), patch("shortform_studio.ai_providers.urllib.request.urlopen", side_effect=open_stream):
            result = _generate_chatgpt("gpt-6.1-sol", "system", "user")

        request = captured[0]
        body = json.loads(request.data)
        self.assertEqual(result, '{"title":"ok"}')
        self.assertEqual(request.get_header("Authorization"), "Bearer oauth-access")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])

    def test_chatgpt_stream_without_completion_is_not_success(self) -> None:
        """@brief Treat a truncated stream as failure even when partial text arrived."""
        event = {"type": "response.output_text.delta", "delta": '{"partial":true}'}
        stream = io.BytesIO(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
        with patch("shortform_studio.ai_providers._chatgpt_access_token", return_value="oauth-access"), patch("shortform_studio.ai_providers.urllib.request.urlopen", return_value=stream):
            with self.assertRaisesRegex(RuntimeError, "response.completed"):
                _generate_chatgpt("gpt-6.1-sol", "system", "user")

    def test_anthropic_uses_api_key_messages_endpoint(self) -> None:
        """@brief Route Claude requests through the supported API-key interface."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-secret"}), patch("shortform_studio.ai_providers._request_json", return_value={"content": [{"type": "text", "text": '{"ok":true}'}]}) as request:
            result = _generate_anthropic("claude-sonnet-4-5", "system", "user")

        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(request.call_args.args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.call_args.kwargs["headers"]["x-api-key"], "anthropic-secret")

    def test_chatgpt_credentials_are_private_and_app_local(self) -> None:
        """@brief Save credentials outside the project tree with owner-only POSIX access."""
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": temporary}):
            with patch("shortform_studio.ai_providers._protect_token", side_effect=lambda value: value):
                _save_chatgpt_tokens({"client_id": "oaiapp_client", "access_token": "access", "refresh_token": "refresh", "scopes": ["chatgpt.tokens.use.direct"]})
            token_path = chatgpt_oauth_token_path()
            self.assertEqual(token_path.read_bytes(), b'{"client_id":"oaiapp_client","access_token":"access","refresh_token":"refresh","scopes":["chatgpt.tokens.use.direct"]}')
            if os.name != "nt":
                self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
