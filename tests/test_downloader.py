import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from media_bot.downloader import (
    DownloadError,
    download_progress,
    download_tiktok_account,
    download_tiktok_slideshow,
    read_source_metadata,
)


class DownloadProgressTests(unittest.TestCase):
    def test_parses_ytdlp_progress(self):
        self.assertEqual(download_progress(b"[download]  42.7% of 10.00MiB at 1.00MiB/s"), 42)

    def test_ignores_non_progress_output(self):
        self.assertIsNone(download_progress(b"[info] Extracting URL"))

    def test_reads_source_caption_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.info.json"
            path.write_text(json.dumps({"title": "Clip title", "description": "Source caption"}))
            self.assertEqual(
                read_source_metadata(Path(tmp)),
                ("Clip title", "Source caption"),
            )


class TikTokGalleryDlFallbackTests(unittest.TestCase):
    def test_downloads_tiktok_account_media_into_zip(self):
        async def fake_run(cmd, timeout_seconds, error_prefix):
            directory = Path(cmd[cmd.index("--directory") + 1])
            (directory / "one.mp4").write_bytes(b"video")
            (directory / "two.jpg").write_bytes(b"image")
            (directory / "one.json").write_text("{}")

        gallerydl = Path(tempfile.gettempdir()) / "fake-gallery-dl-account"
        gallerydl.write_text("#!/bin/sh\n")
        gallerydl.chmod(0o755)

        async def run():
            with patch("media_bot.downloader._run_checked", new=AsyncMock(side_effect=fake_run)):
                temporary, archive, count = await download_tiktok_account(
                    gallerydl, "https://www.tiktok.com/@creator", 100, 30, 25,
                )
            try:
                self.assertEqual(count, 2)
                import zipfile
                with zipfile.ZipFile(archive) as bundle:
                    self.assertEqual(set(bundle.namelist()), {"one.mp4", "two.jpg"})
            finally:
                temporary.cleanup()

        asyncio.run(run())

    def test_accepts_video_when_gallery_dl_returns_mp4(self):
        """yt-dlp failures fall back to gallery-dl, which may download a video — not slides."""

        async def fake_run(cmd, timeout_seconds, error_prefix):
            directory = Path(cmd[cmd.index("--directory") + 1])
            (directory / "video_000.mp4").write_bytes(b"fake-tiktok-video")

        gallerydl = Path(tempfile.gettempdir()) / "fake-gallery-dl"
        gallerydl.write_text("#!/bin/sh\n")
        gallerydl.chmod(0o755)

        async def run():
            with patch("media_bot.downloader._run_checked", new=AsyncMock(side_effect=fake_run)):
                temporary, media = await download_tiktok_slideshow(
                    gallerydl, "https://vt.tiktok.com/ZS4J4EmqV", max_filesize_mb=50, timeout_seconds=30,
                )
            try:
                self.assertEqual(media.name, "video_000.mp4")
                self.assertTrue(media.is_file())
                self.assertEqual(media.read_bytes(), b"fake-tiktok-video")
            finally:
                temporary.cleanup()

        asyncio.run(run())

    def test_errors_when_gallery_dl_returns_neither_images_nor_video(self):
        async def fake_run(cmd, timeout_seconds, error_prefix):
            return None

        gallerydl = Path(tempfile.gettempdir()) / "fake-gallery-dl"
        gallerydl.write_text("#!/bin/sh\n")
        gallerydl.chmod(0o755)

        async def run():
            with patch("media_bot.downloader._run_checked", new=AsyncMock(side_effect=fake_run)):
                with self.assertRaises(DownloadError) as ctx:
                    await download_tiktok_slideshow(
                        gallerydl, "https://vt.tiktok.com/empty", max_filesize_mb=50, timeout_seconds=30,
                    )
            self.assertIn("downloadable media", str(ctx.exception))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
