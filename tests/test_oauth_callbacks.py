"""Security and runtime tests for browser-mediated local OAuth callbacks."""

from __future__ import annotations

import os
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import patch

from shortform_studio.oauth_callbacks import (
    clear_authorization_url,
    get_authorization_urls,
    oauth_callback_server,
    set_authorization_url,
    wait_for_oauth_callback,
)


class OAuthCallbackTests(unittest.TestCase):
    """Verify loopback redirects, callback state checks, and safe login links."""

    def test_callback_rejects_wrong_state_then_accepts_matching_state(self) -> None:
        """@brief Ignore unsolicited redirects without consuming the active sign-in."""
        result: dict[str, str] = {}
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_OAUTH_BIND_HOST": "127.0.0.1", "SHORTFORM_STUDIO_OAUTH_REDIRECT_HOST": "127.0.0.1", "SHORTFORM_STUDIO_OAUTH_PORT": "0"}):
            with oauth_callback_server("/oauth2callback") as (server, redirect_uri):
                waiter = threading.Thread(target=lambda: result.update(wait_for_oauth_callback(server, "expected-state", timeout_seconds=3)))
                waiter.start()
                deadline = threading.Event()
                for _ in range(100):
                    if getattr(server, "oauth_expected_state", None) == "expected-state":
                        break
                    deadline.wait(0.01)
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(f"{redirect_uri}?{urlencode({'state': 'wrong-state', 'code': 'bad'})}", timeout=2)
                self.assertEqual(rejected.exception.code, 400)
                with urlopen(f"{redirect_uri}?{urlencode({'state': 'expected-state', 'code': 'good'})}", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                waiter.join(timeout=3)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, {"state": "expected-state", "code": "good"})

    def test_docker_listener_advertises_only_loopback_redirect(self) -> None:
        """@brief Bind inside Compose while directing the provider to host loopback."""
        environment = {"SHORTFORM_STUDIO_OAUTH_BIND_HOST": "0.0.0.0", "SHORTFORM_STUDIO_OAUTH_REDIRECT_HOST": "127.0.0.1", "SHORTFORM_STUDIO_OAUTH_PORT": "0"}
        with patch.dict(os.environ, environment):
            with oauth_callback_server("/auth/callback") as (server, redirect_uri):
                self.assertEqual(server.server_address[0], "0.0.0.0")
                self.assertTrue(redirect_uri.startswith("http://127.0.0.1:"))

    def test_authorization_links_are_provider_limited_and_removable(self) -> None:
        """@brief Expose only HTTPS sign-in URLs from the supported identity providers."""
        set_authorization_url("gemini", "https://accounts.google.com/o/oauth2/v2/auth?state=abc")
        self.assertEqual(get_authorization_urls()["gemini"], "https://accounts.google.com/o/oauth2/v2/auth?state=abc")
        with self.assertRaisesRegex(RuntimeError, "approved provider"):
            set_authorization_url("gemini", "https://example.com/auth")
        clear_authorization_url("gemini")
        self.assertNotIn("gemini", get_authorization_urls())

    def test_invalid_oauth_port_is_rejected(self) -> None:
        """@brief Reject malformed callback configuration before binding a socket."""
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_OAUTH_PORT": "70000"}):
            with self.assertRaisesRegex(RuntimeError, "0 to 65535"):
                with oauth_callback_server("/oauth2callback"):
                    self.fail("Invalid ports must never create a callback server.")


if __name__ == "__main__":
    unittest.main()
