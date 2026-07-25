import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from media_bot.editor import _image_difference, _get_video_dimensions, _segments_to_srt, render_edit
from media_bot.downloader import DownloadError


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


def _create_test_video(path: Path, duration: int = 3) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=640x480:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, check=True,
    )


def _create_test_image(path: Path) -> None:
    from PIL import Image
    img = Image.new("RGBA", (200, 100), (0, 255, 0))
    img.save(path)


class RenderEditIntegrationTests(unittest.TestCase):
    def test_render_edit_banner_and_watermark(self):
        if not Path(shutil.which("ffmpeg") or "").is_file():
            self.skipTest("ffmpeg not available")
        tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-test-")
        try:
            tmp = Path(tmpdir.name)
            video = tmp / "input.mp4"
            banner = tmp / "banner.png"
            output = tmp / "output.mp4"
            _create_test_video(video)
            _create_test_image(banner)

            asyncio.run(render_edit(
                input_path=video,
                output_path=output,
                caption_text="Test Caption",
                auto_captions=False,
                banner_path=banner,
                banner_position="bottom",
                watermark_removal=True,
                watermark_position="top-right",
                timeout_seconds=60,
            ))

            self.assertTrue(output.is_file(), "render_edit produced no output file")
            self.assertGreater(output.stat().st_size, 0, "render_edit output is empty")
        finally:
            tmpdir.cleanup()

    def test_render_edit_passthrough(self):
        if not Path(shutil.which("ffmpeg") or "").is_file():
            self.skipTest("ffmpeg not available")
        tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-test-")
        try:
            tmp = Path(tmpdir.name)
            video = tmp / "input.mp4"
            output = tmp / "output.mp4"
            _create_test_video(video)

            asyncio.run(render_edit(
                input_path=video,
                output_path=output,
                auto_captions=False,
                timeout_seconds=60,
            ))

            self.assertTrue(output.is_file(), "passthrough render_edit produced no output file")
        finally:
            tmpdir.cleanup()

    def test_render_edit_channel_banner_skips_portrait(self):
        if not Path(shutil.which("ffmpeg") or "").is_file():
            self.skipTest("ffmpeg not available")
        tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-test-")
        try:
            tmp = Path(tmpdir.name)
            video = tmp / "input.mp4"
            output = tmp / "output.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=480x640:d=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                capture_output=True, check=True,
            )

            asyncio.run(render_edit(
                input_path=video,
                output_path=output,
                auto_captions=False,
                channel_banner=True,
                source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                timeout_seconds=60,
            ))

            self.assertTrue(output.is_file(), "channel_banner on portrait produced no output")
        finally:
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
