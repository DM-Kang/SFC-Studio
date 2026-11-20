"""Cross-platform app-data path selection tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shortform_studio.config import get_app_home, is_allowed_request_origin


class ConfigPathTests(unittest.TestCase):
    """Verify local data paths honor explicit and operating-system defaults."""

    def test_explicit_app_home_takes_precedence(self) -> None:
        """@brief Let users select the same data location in direct and container runs."""
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": temporary, "LOCALAPPDATA": "/ignored"}):
            self.assertEqual(get_app_home(), Path(temporary).resolve())

    def test_windows_uses_local_app_data_when_present(self) -> None:
        """@brief Preserve the established Windows per-user data location."""
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": "", "LOCALAPPDATA": "C:/Users/test/AppData/Local"}):
            self.assertEqual(get_app_home(), Path("C:/Users/test/AppData/Local") / "ShortformContentStudio")

    def test_posix_uses_xdg_data_home_or_user_local_share(self) -> None:
        """@brief Follow the standard POSIX data directory and XDG override."""
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": "", "LOCALAPPDATA": "", "XDG_DATA_HOME": "/tmp/studio-xdg"}):
            self.assertEqual(get_app_home(), Path("/tmp/studio-xdg/shortform-content-studio"))
        with patch.dict(os.environ, {"SHORTFORM_STUDIO_HOME": "", "LOCALAPPDATA": "", "XDG_DATA_HOME": ""}), patch("shortform_studio.config.Path.home", return_value=Path("/tmp/studio-user")):
            self.assertEqual(get_app_home(), Path("/tmp/studio-user/.local/share/shortform-content-studio"))

    def test_allowed_request_origin_honors_explicit_tailscale_hosts(self) -> None:
        """@brief Allow Tailscale Serve HTTPS hosts only when explicitly configured."""
        self.assertEqual(is_allowed_request_origin("127.0.0.1:8765", "http://127.0.0.1:8765"), (True, ""))
        self.assertFalse(is_allowed_request_origin("dmkang-mbp16.tail835374.ts.net", None)[0])
        self.assertEqual(
            is_allowed_request_origin(
                "dmkang-mbp16.tail835374.ts.net",
                "https://dmkang-mbp16.tail835374.ts.net",
                "dmkang-mbp16.tail835374.ts.net",
            ),
            (True, ""),
        )
        self.assertFalse(
            is_allowed_request_origin(
                "dmkang-mbp16.tail835374.ts.net",
                "https://evil.example.com",
                "dmkang-mbp16.tail835374.ts.net",
            )[0]
        )


if __name__ == "__main__":
    unittest.main()
