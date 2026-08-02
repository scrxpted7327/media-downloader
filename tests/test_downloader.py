import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from media_bot.downloader import (
    DownloadError,
    _enforce_size,
    _download_instagram_ytdlp,
    _run_checked,
    _write_zip_atomic,
    download_progress,
    download_tiktok_account,
    download_tiktok_slideshow,
    persist_download,
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

    def test_final_size_check_removes_oversized_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.bin"
            path.write_bytes(b"x" * 2048)
            with self.assertRaises(DownloadError):
                _enforce_size(path, 0, "test output")
            self.assertFalse(path.exists())

    def test_checked_process_timeout_terminates_promptly(self):
        async def run():
            with self.assertRaises(DownloadError):
                await _run_checked(
                    [
                        __import__("sys").executable,
                        "-c",
                        "import time; print('started', flush=True); time.sleep(30)",
                    ],
                    0.1,
                    "test child timed out",
                )

        asyncio.run(run())

    def test_checked_process_bounds_captured_output(self):
        async def run():
            stdout, _ = await _run_checked(
                [
                    __import__("sys").executable,
                    "-c",
                    "import sys\nfor i in range(5000): print(f'line-{i}')",
                ],
                10,
                "test output failed",
            )
            self.assertNotIn(b"line-0\n", stdout)
            self.assertIn(b"line-4999\n", stdout)
            self.assertLess(len(stdout.splitlines()), 2001)

        asyncio.run(run())

    def test_checked_process_enforces_fast_producer_file_ceiling(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaisesRegex(DownloadError, "file-count"):
                    await _run_checked(
                        [
                            __import__("sys").executable,
                            "-c",
                            (
                                "import pathlib,sys; root=pathlib.Path(sys.argv[1]); "
                                "[(root / f'part-{i}').write_bytes(b'x') for i in range(4)]"
                            ),
                            str(root),
                        ],
                        10,
                        "test producer failed",
                        working_dir_limit=(root, 1024, 3),
                    )

        asyncio.run(run())

    def test_checked_process_enforces_fast_producer_byte_ceiling(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaisesRegex(DownloadError, "size limit"):
                    await _run_checked(
                        [
                            __import__("sys").executable,
                            "-c",
                            "import pathlib,sys; (pathlib.Path(sys.argv[1]) / 'part').write_bytes(b'x' * 2048)",
                            str(root),
                        ],
                        10,
                        "test producer failed",
                        working_dir_limit=(root, 1024, 10),
                    )

        asyncio.run(run())

    def test_persist_download_is_atomic_and_removes_source(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source" / "clip.mp4"
                source.parent.mkdir()
                source.write_bytes(b"complete-video")
                destination = await persist_download(source, 42, root / "storage")
                self.assertEqual(destination.read_bytes(), b"complete-video")
                self.assertFalse(source.exists())
                self.assertFalse(list(destination.parent.glob("*.part")))

        asyncio.run(run())

    def test_persist_download_refuses_to_overwrite_existing_job_file(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "clip.mp4"
                source.write_bytes(b"new")
                storage = root / "storage"
                storage.mkdir()
                existing = storage / "42-clip.mp4"
                existing.write_bytes(b"old")
                with self.assertRaisesRegex(DownloadError, "already exists"):
                    await persist_download(source, 42, storage)
                self.assertEqual(existing.read_bytes(), b"old")
                self.assertEqual(source.read_bytes(), b"new")

        asyncio.run(run())

    def test_atomic_zip_removes_partial_output_when_limit_is_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "image.jpg"
            source.write_bytes(b"not-compressible-enough")
            destination = root / "slides.zip"
            with self.assertRaisesRegex(DownloadError, "size limit"):
                _write_zip_atomic(
                    destination,
                    [(source, source.name)],
                    __import__("zipfile").ZIP_STORED,
                    1,
                    "test ZIP",
                )
            self.assertFalse(destination.exists())
            self.assertFalse((root / ".slides.zip.part").exists())

    def test_instagram_fallback_never_substitutes_python_for_ytdlp(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                fake_python = Path(tmp) / "python"
                fake_python.write_bytes(b"python")
                with (
                    patch("media_bot.downloader.sys.executable", str(fake_python)),
                    patch("media_bot.downloader.shutil.which", return_value=None),
                    patch("media_bot.downloader.download_media", new=AsyncMock()) as download,
                ):
                    with self.assertRaisesRegex(DownloadError, "yt-dlp is required"):
                        await _download_instagram_ytdlp(
                            "https://instagram.com/reel/example", 50, 30,
                        )
                download.assert_not_awaited()

        asyncio.run(run())

    def test_instagram_fallback_uses_provisioned_ytdlp(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                ytdlp = Path(tmp) / "yt-dlp"
                ytdlp.write_text("#!/bin/sh\n")
                ytdlp.chmod(0o755)
                expected = (object(), Path(tmp) / "media.mp4")
                with patch(
                    "media_bot.downloader.download_media",
                    new=AsyncMock(return_value=expected),
                ) as download:
                    result = await _download_instagram_ytdlp(
                        "https://instagram.com/reel/example", 50, 30,
                        ytdlp=ytdlp,
                    )
                self.assertEqual(result, expected)
                self.assertEqual(download.await_args.args[0], ytdlp)

        asyncio.run(run())


class TikTokGalleryDlFallbackTests(unittest.TestCase):
    def test_downloads_tiktok_account_media_into_zip(self):
        async def fake_run(cmd, timeout_seconds, error_prefix, **kwargs):
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

        async def fake_run(cmd, timeout_seconds, error_prefix, **kwargs):
            directory = Path(cmd[cmd.index("--directory") + 1])
            (directory / "video_000.mp4").write_bytes(b"fake-tiktok-video")
            self.assertIn("working_dir_limit", kwargs)

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
        async def fake_run(cmd, timeout_seconds, error_prefix, **kwargs):
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
