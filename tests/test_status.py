import asyncio
import time
import unittest

from media_bot.__main__ import DownloadReporter, _ProgressReporter, _format_eta_line


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


if __name__ == "__main__":
    unittest.main()
