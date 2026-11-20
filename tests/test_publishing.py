"""Tests for portable OAuth publishing callbacks and local credential storage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.publishing import _authorize_youtube_flow, _save_youtube_credentials


class PublishingOAuthTests(unittest.TestCase):
    """Verify YouTube browser handoff and private credential persistence."""

    def test_youtube_oauth_uses_loopback_callback_and_state(self) -> None:
        """@brief Route a desktop YouTube consent flow through the app browser handoff."""
        class FakeFlow:
            """@brief Provide the minimal Google OAuth flow used by the adapter."""

            credentials = object()

            def authorization_url(self, **kwargs: str) -> tuple[str, str]:
                """@brief Return a stable authorization request for the callback test."""
                self.options = kwargs
                return "https://accounts.google.com/o/oauth2/auth?state=test", "youtube-state"

            def fetch_token(self, *, code: str) -> None:
                """@brief Capture the code delivered after state verification."""
                self.code = code

        flow = FakeFlow()
        with patch("shortform_studio.publishing.set_authorization_url") as publish_url, patch("shortform_studio.publishing.wait_for_oauth_callback", return_value={"state": "youtube-state", "code": "youtube-code"}) as wait_callback:
            credentials = _authorize_youtube_flow(flow)

        self.assertIs(credentials, flow.credentials)
        self.assertEqual(flow.code, "youtube-code")
        self.assertEqual(wait_callback.call_args.args[1], "youtube-state")
        self.assertEqual(publish_url.call_args.args[0], "youtube")

    def test_youtube_credentials_are_written_atomically_with_private_mode(self) -> None:
        """@brief Keep persisted upload credentials private and avoid partial JSON files."""
        class FakeCredentials:
            """@brief Supply deterministic serialized credentials."""

            def to_json(self) -> str:
                """@brief Return a small token fixture for the persistence check."""
                return "{\"token\":\"fixture\"}"

        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "youtube-token.json"
            _save_youtube_credentials(token_path, FakeCredentials())
            self.assertEqual(token_path.read_text(encoding="utf-8"), "{\"token\":\"fixture\"}\n")
            self.assertFalse(token_path.with_name(".youtube-token.json.tmp").exists())
            if os.name != "nt":
                self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
