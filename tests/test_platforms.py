import unittest

from media_bot.platforms import (
    extract_supported_urls,
    is_supported_url,
    is_tiktok_photo_url,
    normalize_tiktok_profile,
)


class SupportedUrlTests(unittest.TestCase):
    def test_normalizes_tiktok_profile_inputs(self):
        expected = "https://www.tiktok.com/@creator.name"
        for value in (
            "creator.name", "@creator.name", "tiktok.com/creator.name",
            "www.tiktok.com/@creator.name/", "https://www.tiktok.com/@creator.name/posts",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_tiktok_profile(value), expected)

    def test_rejects_non_profile_tiktok_input(self):
        self.assertIsNone(normalize_tiktok_profile("https://example.com/@creator"))
        self.assertIsNone(normalize_tiktok_profile("https://www.tiktok.com/@creator/video/123"))

    def test_all_supported_platforms(self):
        for url in (
            "https://www.youtube.com/watch?v=abc", "https://youtube.com/shorts/abc", "https://youtu.be/abc",
            "https://www.instagram.com/reel/abc/", "https://www.tiktok.com/@a/video/1",
            "https://www.facebook.com/reel/abc", "https://fb.watch/abc/",
        ):
            self.assertTrue(is_supported_url(url), url)

    def test_rejects_lookalikes_and_non_urls(self):
        for url in (
            "http://youtube.com/watch?v=abc",
            "https://youtube.com.evil.test/x",
            "file:///tmp/a",
            "https://example.com/youtube.com",
            "yt-dlp --help",
        ):
            self.assertFalse(is_supported_url(url), url)

    def test_extracts_links_from_normal_message_text(self):
        text = "Try this (https://www.instagram.com/reel/abc/), then https://example.com/nope."
        self.assertEqual(extract_supported_urls(text), ["https://www.instagram.com/reel/abc/"])

    def test_identifies_tiktok_photo_posts(self):
        self.assertTrue(is_tiktok_photo_url("https://www.tiktok.com/@someone/photo/123456789"))
        self.assertFalse(is_tiktok_photo_url("https://www.tiktok.com/@someone/video/123456789"))

    def test_rejects_insecure_tiktok_profile_url(self):
        self.assertIsNone(normalize_tiktok_profile("http://www.tiktok.com/@creator"))


if __name__ == "__main__":
    unittest.main()
