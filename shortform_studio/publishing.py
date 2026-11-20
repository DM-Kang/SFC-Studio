"""Approval-gated YouTube and Instagram publishing adapters."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import get_app_home, get_secret
from .oauth_callbacks import clear_authorization_url, oauth_callback_server, set_authorization_url, wait_for_oauth_callback


class PublicationError(RuntimeError):
    """Represent a publishing failure and whether the platform may have accepted it."""

    def __init__(self, message: str, uncertain: bool = False):
        """@brief Create an error with an explicit duplicate-retry safety classification."""
        super().__init__(message)
        self.uncertain = uncertain


def youtube_ready() -> bool:
    """@brief Report whether local YouTube OAuth credentials are configured."""
    path = get_secret("YOUTUBE_CLIENT_SECRETS_FILE")
    return bool(path and Path(path).is_file())


def youtube_scheduling_ready() -> bool:
    """@brief Require a saved refreshable YouTube session before unattended scheduling."""
    token_path = get_app_home() / "youtube-token.json"
    if not youtube_ready() or not token_path.is_file():
        return False
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(token, dict) and bool(token.get("refresh_token"))


def instagram_ready() -> bool:
    """@brief Report whether Instagram and public R2 storage settings are present."""
    names = (
        "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID", "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL",
    )
    return all(get_secret(name) for name in names)


def _authorize_youtube_flow(flow: Any) -> Any:
    """@brief Complete YouTube Desktop OAuth through the shared local browser callback.

    @param flow Google OAuth flow created for the YouTube upload scope.
    @returns Authorized Google credentials.
    """
    with oauth_callback_server("/oauth2callback") as (server, redirect_uri):
        flow.redirect_uri = redirect_uri
        authorization_url, expected_state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        set_authorization_url("youtube", authorization_url)
        try:
            callback = wait_for_oauth_callback(server, expected_state)
        finally:
            clear_authorization_url("youtube")
    if callback.get("error"):
        raise PublicationError("YouTube sign-in was cancelled or denied.")
    code = callback.get("code")
    if not code:
        raise PublicationError("YouTube sign-in did not return an authorization code.")
    try:
        flow.fetch_token(code=code)
    except Exception as error:
        raise PublicationError(f"YouTube OAuth token exchange failed: {type(error).__name__}.") from None
    return flow.credentials


def _save_youtube_credentials(path: Path, credentials: Any) -> None:
    """@brief Atomically save YouTube OAuth credentials with owner-only POSIX access.

    @param path User-local credential file.
    @param credentials Google authorized-user credentials.
    @returns None.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(credentials.to_json())
            output.write("\n")
        temporary.replace(path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def upload_to_r2(item_id: str, video_path: Path) -> tuple[str, str]:
    """@brief Upload one generated video to the configured public R2 bucket."""
    if not instagram_ready():
        raise PublicationError("Instagram publishing needs account credentials and public R2 storage settings.")
    try:
        import boto3
    except ImportError as error:
        raise PublicationError("The boto3 package is not installed; rerun setup.ps1.") from error
    account_id = get_secret("R2_ACCOUNT_ID")
    object_key = f"shortform/{item_id}/{uuid4()}.mp4"
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=get_secret("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=get_secret("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    try:
        client.upload_file(str(video_path), get_secret("R2_BUCKET"), object_key, ExtraArgs={"ContentType": "video/mp4"})
    except Exception as error:
        raise PublicationError(f"R2 upload failed: {type(error).__name__}.") from None
    base_url = get_secret("R2_PUBLIC_BASE_URL").rstrip("/")
    return object_key, f"{base_url}/{urllib.parse.quote(object_key, safe='/')}"


def publish_youtube(video_path: Path, plan: dict[str, Any], privacy_status: str, synthetic_media: bool = True) -> dict[str, str]:
    """@brief Upload an approved video with resumable YouTube OAuth publishing."""
    if privacy_status not in {"private", "unlisted", "public"}:
        raise PublicationError("YouTube privacy must be private, unlisted, or public.")
    if not youtube_ready():
        raise PublicationError("YOUTUBE_CLIENT_SECRETS_FILE is not configured or does not exist.")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise PublicationError("YouTube OAuth dependencies are missing; rerun setup.ps1.") from error
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    token_path = get_app_home() / "youtube-token.json"
    credentials = Credentials.from_authorized_user_file(str(token_path), scopes) if token_path.is_file() else None
    try:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(get_secret("YOUTUBE_CLIENT_SECRETS_FILE"), scopes)
            credentials = _authorize_youtube_flow(flow)
        _save_youtube_credentials(token_path, credentials)
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        tags = [str(tag).lstrip("#")[:80] for tag in plan.get("hashtags", [])[:20]]
        metadata = {
            "snippet": {"title": plan["title"][:100], "description": plan["caption"][:5000], "tags": tags, "categoryId": "22"},
            "status": {"privacyStatus": privacy_status, "containsSyntheticMedia": synthetic_media},
        }
        upload = service.videos().insert(
            part="snippet,status",
            body=metadata,
            media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True),
        )
    except PublicationError:
        raise
    except Exception as error:
        raise PublicationError(f"YouTube authorization or upload setup failed: {type(error).__name__}.") from None
    response: dict[str, Any] | None = None
    try:
        while response is None:
            _, response = upload.next_chunk()
    except Exception as error:
        raise PublicationError(f"YouTube upload failed after request start: {type(error).__name__}.", uncertain=True) from None
    video_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(video_id, str) or not video_id:
        raise PublicationError("YouTube returned no video ID after accepting the upload.", uncertain=True)
    return {"remote_id": video_id, "permalink": f"https://www.youtube.com/watch?v={video_id}"}


def _instagram_request(url: str, *, method: str, fields: dict[str, str], token: str, timeout: int = 45) -> dict[str, Any]:
    """@brief Make a token-authenticated Instagram Graph API request."""
    encoded = urllib.parse.urlencode({**fields, "access_token": token}).encode("utf-8")
    request = urllib.request.Request(url, data=encoded if method == "POST" else None, headers={"Content-Type": "application/x-www-form-urlencoded"}, method=method)
    if method == "GET":
        request = urllib.request.Request(f"{url}?{encoded.decode('ascii')}", headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise PublicationError(f"Instagram API returned HTTP {error.code}.", uncertain=method == "POST") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PublicationError(f"Instagram API request failed: {type(error).__name__}.", uncertain=method == "POST") from None
    if not isinstance(payload, dict):
        raise PublicationError("Instagram API returned an unexpected response.", uncertain=method == "POST")
    return payload


def publish_instagram(item_id: str, video_path: Path, caption: str) -> dict[str, str]:
    """@brief Stage an MP4 publicly, wait for its Reel container, and publish it."""
    token = get_secret("INSTAGRAM_ACCESS_TOKEN")
    user_id = urllib.parse.quote(get_secret("INSTAGRAM_USER_ID"), safe="")
    host = get_secret("INSTAGRAM_API_HOST", "graph.instagram.com")
    version = get_secret("INSTAGRAM_API_VERSION", "v27.0")
    if host not in {"graph.instagram.com", "graph.facebook.com"} or not re.fullmatch(r"v\d+\.\d+", version):
        raise PublicationError("Instagram API host or version is invalid; use an official Meta Graph API endpoint.")
    object_key, video_url = upload_to_r2(item_id, video_path)
    base = f"https://{host}/{version}"
    container = _instagram_request(
        f"{base}/{user_id}/media",
        method="POST",
        token=token,
        fields={"media_type": "REELS", "video_url": video_url, "caption": caption[:2200], "share_to_feed": "true"},
    )
    creation_id = container.get("id")
    if not isinstance(creation_id, str) or not creation_id:
        raise PublicationError("Instagram did not return a Reel container ID.")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status = _instagram_request(
            f"{base}/{urllib.parse.quote(creation_id, safe='')}",
            method="GET",
            token=token,
            fields={"fields": "status_code,status"},
        )
        status_code = status.get("status_code")
        if status_code == "FINISHED":
            break
        if status_code in {"ERROR", "EXPIRED"}:
            raise PublicationError(f"Instagram Reel container ended with status {status_code}; staged object {object_key} remains in R2.")
        time.sleep(5)
    else:
        raise PublicationError(f"Instagram processing timed out; staged object {object_key} remains in R2.")
    try:
        published = _instagram_request(
            f"{base}/{user_id}/media_publish",
            method="POST",
            token=token,
            fields={"creation_id": creation_id},
        )
    except PublicationError as error:
        error.uncertain = True
        raise
    media_id = published.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise PublicationError("Instagram returned no media ID after publish request; inspect the account before retrying.", uncertain=True)
    return {"remote_id": media_id, "permalink": "", "staged_object": object_key}
