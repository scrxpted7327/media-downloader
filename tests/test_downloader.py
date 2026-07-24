import unittest

from media_bot.downloader import download_progress


class DownloadProgressTests(unittest.TestCase):
    def test_parses_ytdlp_progress(self):
        self.assertEqual(download_progress(b"[download]  42.7% of 10.00MiB at 1.00MiB/s"), 42)

    def test_ignores_non_progress_output(self):
        self.assertIsNone(download_progress(b"[info] Extracting URL"))


if __name__ == "__main__":
    unittest.main()
