import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from media_bot import tools


class YtDlpProvisioningTests(unittest.TestCase):
    def _cached_binary(self, root: Path, version: str = "2026.07.01") -> Path:
        binary = root / "yt-dlp_test"
        binary.write_bytes(b"verified binary")
        binary.chmod(binary.stat().st_mode | 0o111)
        (root / "yt-dlp-version").write_text(version + "\n", encoding="utf-8")
        self.assertTrue(os.access(binary, os.X_OK))
        return binary

    def test_default_startup_reuses_verified_cache_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = self._cached_binary(root)
            with (
                patch.object(tools, "_asset_name", return_value=binary.name),
                patch.object(tools, "_get_json") as get_json,
            ):
                result = tools.provision_ytdlp(root)

            self.assertEqual(result, binary)
            get_json.assert_not_called()

    def test_matching_pinned_version_reuses_cache_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = self._cached_binary(root)
            with (
                patch.object(tools, "_asset_name", return_value=binary.name),
                patch.object(tools, "_get_json") as get_json,
            ):
                result = tools.provision_ytdlp(root, "2026.07.01")

            self.assertEqual(result, binary)
            get_json.assert_not_called()

    def test_network_failure_falls_back_to_verified_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = self._cached_binary(root, "2026.06.01")
            with (
                patch.object(tools, "_asset_name", return_value=binary.name),
                patch.object(
                    tools,
                    "_get_json",
                    side_effect=urllib.error.URLError("offline"),
                ),
            ):
                result = tools.provision_ytdlp(root, "2026.07.01")

            self.assertEqual(result, binary)


if __name__ == "__main__":
    unittest.main()
