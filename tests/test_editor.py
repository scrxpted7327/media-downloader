import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_bot.editor import (
    _caption_chunks,
    _image_difference,
    _get_video_dimensions,
    _segments_to_srt,
    _transcribe_ssh,
    render_captions,
    render_edit,
    render_voice_over,
)
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

    def test_splits_long_segments_into_one_or_two_word_beats(self):
        segments = [{
            "start": 0.0,
            "end": 5.0,
            "text": "One two three four five",
        }]
        chunks = _caption_chunks(segments)

        self.assertEqual(
            [chunk["text"] for chunk in chunks],
            ["One two", "three four", "five"],
        )
        self.assertTrue(all(len(chunk["text"].split()) <= 2 for chunk in chunks))
        self.assertEqual(chunks[0]["start"], 0.0)
        self.assertEqual(chunks[-1]["end"], 5.0)

    def test_uses_whisper_word_timestamps(self):
        segments = [{
            "start": 0.0,
            "end": 4.0,
            "text": "Timed words stay accurate",
            "words": [
                {"start": 0.2, "end": 0.8, "word": "Timed"},
                {"start": 0.9, "end": 1.4, "word": "words"},
                {"start": 2.0, "end": 2.5, "word": "stay"},
                {"start": 2.6, "end": 3.2, "word": "accurate"},
            ],
        }]
        chunks = _caption_chunks(segments)

        self.assertEqual(chunks, [
            {"start": 0.2, "end": 1.4, "text": "Timed words"},
            {"start": 2.0, "end": 3.2, "text": "stay accurate"},
        ])


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
    def test_auto_caption_burn_uses_short_beats_and_native_canvas(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"video")
            captured_ass = []

            async def fake_ffmpeg(command, *_args, **_kwargs):
                ass_argument = command[command.index("-vf") + 1]
                ass_path = Path(ass_argument.removeprefix("ass="))
                captured_ass.append(ass_path.read_text(encoding="utf-8"))
                output.write_bytes(b"rendered")

            segments = [{
                "start": 0.0, "end": 2.0, "text": "One two three four",
                "words": [
                    {"start": 0.0, "end": .4, "word": "One"},
                    {"start": .5, "end": .9, "word": "two"},
                    {"start": 1.0, "end": 1.4, "word": "three"},
                    {"start": 1.5, "end": 2.0, "word": "four"},
                ],
            }]
            with (
                patch("media_bot.editor.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("media_bot.editor.transcribe_audio", return_value=segments),
                patch("media_bot.editor._get_video_dimensions", return_value=(720, 1280)),
                patch("media_bot.editor._run_ffmpeg_with_progress", side_effect=fake_ffmpeg),
            ):
                asyncio.run(render_captions(
                    source, output, style="bold", position="high",
                    bottom_safe_area=.17,
                ))

            ass = captured_ass[0]
            self.assertIn("PlayResX: 720", ass)
            self.assertIn("PlayResY: 1280", ass)
            self.assertIn(",One two\n", ass)
            self.assertIn(",three four\n", ass)
            self.assertNotIn("One two three four", ass)
            self.assertIn(",228,1\n", ass)

    def test_auto_captions_preserve_video_when_no_speech_is_found(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"silent-video")
            with (
                patch("media_bot.editor.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("media_bot.editor.transcribe_audio", return_value=[]),
            ):
                asyncio.run(render_captions(source, output, auto_captions=True))
            self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_auto_tts_falls_back_to_espeak_and_does_not_apply_speed_twice(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"video")

            async def fail_edge(*args):
                raise RuntimeError("network unavailable")

            async def make_espeak(_text, path, _voice, _speed, _timeout):
                path.write_bytes(b"audio")

            async def fake_run(command, *_args, **_kwargs):
                Path(command[-1]).write_bytes(b"rendered")
                return b"", b""

            with (
                patch("media_bot.editor._detect_tts_engine", return_value="edge-tts"),
                patch("media_bot.editor._tts_espeak", side_effect=make_espeak),
                patch.dict("media_bot.editor._TTS_ENGINES", {"edge-tts": fail_edge}),
                patch("media_bot.editor._run_checked", side_effect=fake_run) as run_checked,
                patch("media_bot.editor.shutil.which", return_value="/usr/bin/espeak-ng"),
            ):
                asyncio.run(render_voice_over(
                    source, output, "Narration", voice="en-US-GuyNeural", speed=1.5,
                ))

            command = run_checked.await_args.args[0]
            filter_graph = command[command.index("-filter_complex") + 1]
            self.assertNotIn("atempo", filter_graph)
            self.assertTrue(output.is_file())

    def test_render_edit_swaps_manual_watermark_with_text(self):
        if not Path(shutil.which("ffmpeg") or "").is_file():
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            video = root / "input.mp4"
            output = root / "output.mp4"
            _create_test_video(video)

            asyncio.run(render_edit(
                input_path=video,
                output_path=output,
                auto_captions=False,
                watermark_removal=True,
                watermark_mode="swap",
                watermark_text="@replacement",
                watermark_position="top-right",
                timeout_seconds=60,
            ))

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

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

    def test_render_edit_channel_banner_renders_on_portrait(self):
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

            with patch("media_bot.editor._fetch_channel_identity", return_value=("Test Channel", None)):
                asyncio.run(render_edit(
                    input_path=video,
                    output_path=output,
                    auto_captions=False,
                    channel_banner=True,
                    source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    timeout_seconds=60,
                ))

            self.assertTrue(output.is_file(), "channel_banner on portrait produced no output")
            self.assertNotEqual(output.read_bytes(), video.read_bytes())
        finally:
            tmpdir.cleanup()


class RemoteWhisperTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_ssh_host(self):
        with (
            patch.dict("os.environ", {"WHISPER_SSH_HOST": ""}, clear=False),
            patch("media_bot.editor.asyncio.create_subprocess_exec") as create_process,
        ):
            with self.assertRaisesRegex(DownloadError, "WHISPER_SSH_HOST not set"):
                await _transcribe_ssh(Path(__file__), None, 10)
        create_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
