"""Local OAuth callback handling shared by browser and container runtimes."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

_CALLBACK_LOCK = threading.Lock()
_AUTH_URL_LOCK = threading.RLock()
_AUTH_URLS: dict[str, tuple[float, str]] = {}
_AUTH_URL_TTL_SECONDS = 330
_AUTH_HOSTS = {"accounts.google.com", "auth.openai.com"}


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Accept one state-checked OAuth redirect without logging its query string."""

    def do_GET(self) -> None:
        """@brief Validate and capture one callback for the active local sign-in."""
        request_url = urlsplit(self.path)
        if request_url.path != getattr(self.server, "oauth_callback_path", "/oauth2callback"):
            self.send_error(404)
            return
        if len(self.path) > 8_192:
            self.send_error(414)
            return
        try:
            query = parse_qs(request_url.query, max_num_fields=12)
        except ValueError:
            self.send_error(400, "Invalid OAuth callback.")
            return
        values = {key: entries[0][:4_096] for key, entries in query.items() if entries}
        if values.get("state") != getattr(self.server, "oauth_expected_state", ""):
            self.send_error(400, "OAuth state validation failed.")
            return
        self.server.oauth_callback = values
        self.server.oauth_event.set()
        page = b"<!doctype html><html><body>Sign-in response received. You can close this tab.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:
        """@brief Suppress callback paths that may contain authorization codes."""
        return


@contextmanager
def oauth_callback_server(path: str) -> Iterator[tuple[HTTPServer, str]]:
    """@brief Own the single local callback listener and yield it with its redirect URI.

    @param path Fixed callback path registered with the OAuth provider.
    @returns Context-managed server and loopback redirect URI.
    """
    bind_host = os.environ.get("SHORTFORM_STUDIO_OAUTH_BIND_HOST", "127.0.0.1").strip()
    redirect_host = os.environ.get("SHORTFORM_STUDIO_OAUTH_REDIRECT_HOST", "127.0.0.1").strip()
    configured_port = os.environ.get("SHORTFORM_STUDIO_OAUTH_PORT", "0").strip()
    if bind_host not in {"127.0.0.1", "0.0.0.0"} or redirect_host != "127.0.0.1":
        raise RuntimeError("OAuth callbacks must advertise the IPv4 loopback address.")
    try:
        port = int(configured_port)
    except ValueError:
        raise RuntimeError("OAuth callback port must be an integer from 0 to 65535.") from None
    if not 0 <= port <= 65_535:
        raise RuntimeError("OAuth callback port must be an integer from 0 to 65535.")
    if not _CALLBACK_LOCK.acquire(timeout=1):
        raise RuntimeError("Another OAuth callback is already active; finish it before starting another.")
    server: HTTPServer | None = None
    try:
        try:
            server = HTTPServer((bind_host, port), OAuthCallbackHandler)
        except OSError as error:
            raise RuntimeError(f"Could not start the local OAuth callback listener ({type(error).__name__}).") from None
        server.timeout = 0.5
        server.oauth_event = threading.Event()
        server.oauth_callback = {}
        server.oauth_callback_path = path
        yield server, f"http://{redirect_host}:{server.server_port}{path}"
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            _CALLBACK_LOCK.release()


def wait_for_oauth_callback(server: HTTPServer, expected_state: str, timeout_seconds: int = 300) -> dict[str, str]:
    """@brief Wait a bounded time for a callback matching the OAuth state.

    @param server Active callback server created by oauth_callback_server.
    @param expected_state Random state generated for this authorization request.
    @param timeout_seconds Maximum callback wait in seconds.
    @returns Validated callback query values.
    """
    server.oauth_expected_state = expected_state
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not server.oauth_event.is_set():
        server.handle_request()
    if not server.oauth_event.is_set():
        raise RuntimeError("OAuth sign-in timed out after five minutes.")
    return dict(server.oauth_callback)


def set_authorization_url(provider: str, url: str) -> None:
    """@brief Publish a short-lived provider authorization URL to the local browser UI.

    @param provider Stable provider key used by the UI.
    @param url HTTPS authorization URL from Google or OpenAI.
    @returns None.
    """
    parsed = urlsplit(url)
    try:
        invalid_authority = bool(parsed.username or parsed.password or parsed.port)
    except ValueError:
        invalid_authority = True
    if provider not in {"gemini", "chatgpt_oauth", "youtube"} or parsed.scheme != "https" or parsed.hostname not in _AUTH_HOSTS or invalid_authority:
        raise RuntimeError("OAuth authorization URL did not match an approved provider.")
    with _AUTH_URL_LOCK:
        _AUTH_URLS[provider] = (time.monotonic() + _AUTH_URL_TTL_SECONDS, url)


def clear_authorization_url(provider: str) -> None:
    """@brief Remove an OAuth authorization URL after callback or failure.

    @param provider Stable provider key used by the UI.
    @returns None.
    """
    with _AUTH_URL_LOCK:
        _AUTH_URLS.pop(provider, None)


def get_authorization_urls() -> dict[str, str]:
    """@brief Return only unexpired browser authorization URLs for active sign-ins.

    @returns A provider-to-URL map without OAuth tokens or callback codes.
    """
    now = time.monotonic()
    with _AUTH_URL_LOCK:
        expired = [provider for provider, (deadline, _) in _AUTH_URLS.items() if deadline <= now]
        for provider in expired:
            _AUTH_URLS.pop(provider, None)
        return {provider: url for provider, (_, url) in _AUTH_URLS.items()}
