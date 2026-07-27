import tempfile
import unittest
from pathlib import Path

from media_bot.download_server import _content_type_for


class DownloadServerContentTypeTests(unittest.TestCase):
    def test_mp4_uses_video_mp4(self):
        self.assertEqual(_content_type_for(Path("clip.mp4")), "video/mp4")

    def test_srt_and_unknown(self):
        self.assertEqual(_content_type_for(Path("subs.srt")), "application/x-subrip")
        self.assertEqual(_content_type_for(Path("file.bin")), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
