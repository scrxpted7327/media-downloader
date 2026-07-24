import asyncio
import unittest

from media_bot.__main__ import DownloadReporter


class FakeMessage:
    def __init__(self):
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class DownloadReporterTests(unittest.TestCase):
    def test_changes_to_downloading_once(self):
        message = FakeMessage()
        reporter = DownloadReporter(message)

        asyncio.run(reporter.progress(0))
        asyncio.run(reporter.progress(50))

        self.assertEqual(message.edits, ["Downloading…"])


if __name__ == "__main__":
    unittest.main()
