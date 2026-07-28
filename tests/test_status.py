import asyncio
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter

from media_bot.__main__ import (
    DownloadReporter,
    _ProgressReporter,
    _format_eta_line,
    _safe_status_edit,
    _send_document_with_retry,
)


class FakeMessage:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class DownloadReporterTests(unittest.TestCase):
    def test_shows_progress_bar_and_eta(self):
        message = FakeMessage()
        reporter = DownloadReporter(message)
        reporter.start_time = time.monotonic() - 10

        asyncio.run(reporter.progress(50))

        self.assertEqual(len(message.edits), 1)
        self.assertIn("⬇️ Downloading…", message.edits[0])
        self.assertIn("50%", message.edits[0])
        self.assertIn("left", message.edits[0])

    def test_throttles_tiny_updates(self):
        message = FakeMessage()
        reporter = DownloadReporter(message)
        reporter.start_time = time.monotonic() - 10

        async def run():
            await reporter.progress(10)
            await reporter.progress(10)
            await reporter.progress(11)

        asyncio.run(run())
        self.assertEqual(len(message.edits), 1)


class ProgressReporterTests(unittest.TestCase):
    def test_shows_eta_not_elapsed(self):
        message = FakeMessage()
        reporter = _ProgressReporter(message, ["Captions", "Banner"])
        reporter.start_time = time.monotonic() - 20

        asyncio.run(reporter(50))

        self.assertEqual(len(message.edits), 1)
        text = message.edits[0]
        self.assertIn("Step 1/2: Captions", text)
        self.assertIn("left", text)
        self.assertNotIn("⏱ 20s", text)
        self.assertNotIn("(~", text)


class EtaFormatTests(unittest.TestCase):
    def test_format_eta_line(self):
        self.assertEqual(_format_eta_line(0.5, 10), "⏱ calculating…")
        self.assertIn("left", _format_eta_line(10, 50))
        self.assertEqual(_format_eta_line(10, 99), "⏱ almost done")
        self.assertEqual(_format_eta_line(10, 100), "⏱ almost done")

    def test_rate_limited_status_edit_does_not_raise(self):
        message = FakeMessage()
        message.edit_text = AsyncMock(side_effect=RetryAfter(27))
        self.assertFalse(asyncio.run(_safe_status_edit(message, "Uploading…")))

    def test_document_send_retries_after_flood_control(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "video.mp4"
                path.write_bytes(b"video")
                message = AsyncMock()
                message.reply_document.side_effect = [RetryAfter(2), None]
                with patch("media_bot.__main__.asyncio.sleep", new=AsyncMock()) as sleep:
                    await _send_document_with_retry(message, path, "Ready", 60)
                self.assertEqual(message.reply_document.await_count, 2)
                sleep.assert_awaited_once_with(3.0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
