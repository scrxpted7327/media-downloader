import unittest
from pathlib import Path

from media_bot.editor import _image_difference, _get_video_dimensions, _segments_to_srt


class ImageDifferenceTests(unittest.TestCase):
    def test_identical_images_zero_diff(self):
        from PIL import Image
        img = Image.new("RGB", (10, 10), (255, 0, 0))
        self.assertEqual(_image_difference(img, img), 0.0)

    def test_different_images_nonzero_diff(self):
        from PIL import Image
        img1 = Image.new("RGB", (10, 10), (255, 0, 0))
        img2 = Image.new("RGB", (10, 10), (0, 0, 255))
        diff = _image_difference(img1, img2)
        self.assertGreater(diff, 0.0)


class SegmentsToSrtTests(unittest.TestCase):
    def test_converts_segments(self):
        segments = [{"start": 1.0, "end": 2.5, "text": "Hello world"}]
        srt = _segments_to_srt(segments)
        self.assertIn("00:00:01.000 --> 00:00:02.500", srt)
        self.assertIn("Hello world", srt)

    def test_multiple_segments(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "First"},
            {"start": 1.0, "end": 2.0, "text": "Second"},
        ]
        srt = _segments_to_srt(segments)
        self.assertEqual(srt.count("-->"), 2)


if __name__ == "__main__":
    unittest.main()
