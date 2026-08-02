import asyncio
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from media_bot.editor import (
    _ass_timestamp,
    _caption_chunks,
    _caption_position_override,
    _build_ass_style,
    _image_difference,
    _run_ffmpeg_with_progress,
    _segments_to_srt,
    _transcribe_ssh,
    render_captions,
    render_edit,
    render_voice_over,
    remove_watermark,
    transcribe_audio,
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
    def test_small_caption_style_and_exact_vertical_position(self):
        style = _build_ass_style("white", "small", "y30", video_height=1280)
        self.assertEqual(style["fontsize"], 21)
        self.assertEqual(style["alignment"], 5)
        self.assertEqual(
            _caption_position_override("y30", 720, 1280),
            "{\\an5\\pos(360,384)}",
        )

    def test_ass_timestamp_uses_centiseconds(self):
        self.assertEqual(_ass_timestamp(40.2), "0:00:40.20")
        self.assertEqual(_ass_timestamp(3661.999), "1:01:02.00")

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

    def test_normalizes_detached_and_unicode_punctuation(self):
        segments = [{
            "start": 0.0,
            "end": 2.0,
            "text": "Hello , world… it’s fine — really",
            "words": [
                {"start": 0.0, "end": .3, "word": "Hello"},
                {"start": .3, "end": .4, "word": ","},
                {"start": .5, "end": .9, "word": "world…"},
                {"start": 1.0, "end": 1.3, "word": "it’s"},
                {"start": 1.4, "end": 2.0, "word": "fine"},
            ],
        }]

        chunks = _caption_chunks(segments)

        self.assertEqual(
            [chunk["text"] for chunk in chunks],
            ["Hello, world...", "it's fine"],
        )
        self.assertEqual(chunks[0]["start"], 0.0)
        self.assertEqual(chunks[0]["end"], .9)


def _create_test_video(path: Path, duration: int = 3) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=red:s=640x480:d={duration}",
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
            self.assertIn("Dialogue: 0,0:00:00.00,0:00:00.90", ass)

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

    def test_video_only_input_skips_whisper_transcription(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe not available")
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            source = Path(directory) / "video-only.mp4"
            _create_test_video(source, duration=1)
            with patch("media_bot.editor._get_whisper_model_async") as get_model:
                segments = asyncio.run(transcribe_audio(source, timeout_seconds=30))
            self.assertEqual(segments, [])
            get_model.assert_not_called()

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
            self.assertIn("apad", filter_graph)
            self.assertIn("-shortest", command)
            self.assertTrue(output.is_file())

    def test_short_voice_over_preserves_full_video_duration(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe not available")
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            _create_test_video(source, duration=2)

            async def make_short_voice(_text, path, _voice, _speed, _timeout):
                with wave.open(str(path), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(16_000)
                    audio.writeframes(b"\0\0" * 4_000)

            with (
                patch("media_bot.editor._detect_tts_engine", return_value="test"),
                patch.dict("media_bot.editor._TTS_ENGINES", {"test": make_short_voice}),
            ):
                asyncio.run(render_voice_over(
                    source, output, "Short narration", tts_engine="test",
                    timeout_seconds=30,
                ))

            duration = float(subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(output),
                ],
                capture_output=True, check=True, text=True,
            ).stdout.strip())
            self.assertGreaterEqual(duration, 1.8)

    def test_macos_say_uses_an_aiff_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"video")
            audio_paths = []

            async def make_say(_text, path, _voice, _speed, _timeout):
                audio_paths.append(path)
                path.write_bytes(b"audio")

            async def fake_run(command, *_args, **_kwargs):
                Path(command[-1]).write_bytes(b"rendered")
                return b"", b""

            with (
                patch("media_bot.editor._detect_tts_engine", return_value="say"),
                patch.dict("media_bot.editor._TTS_ENGINES", {"say": make_say}),
                patch("media_bot.editor._run_checked", side_effect=fake_run),
                patch("media_bot.editor.shutil.which", return_value="/usr/bin/ffmpeg"),
            ):
                asyncio.run(render_voice_over(
                    source, output, "Narration", tts_engine="say",
                ))

            self.assertEqual(audio_paths[0].suffix, ".aiff")
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
            self.assertTrue(video.is_file(), "passthrough must preserve its staged input")
        finally:
            tmpdir.cleanup()

    def test_render_edit_cleans_all_intermediates_after_failure(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"source")

            async def fake_captions(_input, stage, *_args, **_kwargs):
                stage.write_bytes(b"caption-stage")
                return stage

            async def fail_voice(_input, stage, *_args, **_kwargs):
                stage.write_bytes(b"partial-voice-stage")
                raise DownloadError("voice failed")

            with (
                patch("media_bot.editor.render_captions", side_effect=fake_captions),
                patch("media_bot.editor.render_voice_over", side_effect=fail_voice),
            ):
                with self.assertRaisesRegex(DownloadError, "voice failed"):
                    asyncio.run(render_edit(
                        source,
                        output,
                        caption_text="caption",
                        auto_captions=False,
                        voice_text="narration",
                    ))

            self.assertTrue(source.is_file())
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".output_*")), [])


@unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
class ProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ffmpeg_runner_cancellation_kills_descendant_processes(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            pid_file = Path(directory) / "child.pid"
            ready_file = Path(directory) / "child.ready"
            child_script = (
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
            )
            parent_script = (
                "import os,pathlib,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()},{child.pid}'); time.sleep(30)"
            )
            killpg_calls: list[tuple[int, int]] = []
            real_killpg = os.killpg

            def recording_killpg(process_group: int, sig: int) -> None:
                killpg_calls.append((process_group, sig))
                real_killpg(process_group, sig)

            with patch("media_bot.downloader.os.killpg", side_effect=recording_killpg):
                task = asyncio.create_task(_run_ffmpeg_with_progress(
                    [
                        sys.executable, "-c", parent_script, str(pid_file),
                        str(ready_file), child_script,
                    ],
                    30,
                    "test process",
                ))
                parent_pid: int | None = None
                child_pid: int | None = None
                try:
                    for _ in range(100):
                        if pid_file.is_file() and ready_file.is_file():
                            parent_pid, child_pid = (
                                int(value) for value in pid_file.read_text().split(",")
                            )
                            break
                        await asyncio.sleep(.02)
                    self.assertIsNotNone(child_pid, "child process never became ready")
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                    self.assertIn((parent_pid, signal.SIGTERM), killpg_calls)
                    self.assertIn((parent_pid, signal.SIGKILL), killpg_calls)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    if child_pid is not None:
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


class WatermarkCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lama_worker_is_joined_and_partial_output_removed_on_cancel(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            root = Path(directory)
            source = root / "input.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"input")
            worker_started = threading.Event()
            worker_stopped = threading.Event()

            def fake_inpaint(
                _input, partial, _candidates, _model, _timeout, cancel_event,
            ):
                partial.write_bytes(b"partial")
                worker_started.set()
                cancel_event.wait(5)
                worker_stopped.set()
                raise InterruptedError("cancelled")

            candidate = {
                "id": 1,
                "x": 0,
                "y": 0,
                "width": 10,
                "height": 10,
                "confidence": .99,
                "persistence": .99,
                "border_score": .5,
            }
            with (
                patch("media_bot.editor.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("media_bot.watermark.provision_lama_model", return_value=root / "lama.onnx"),
                patch("media_bot.watermark.inpaint_video", side_effect=fake_inpaint),
                patch("media_bot.editor._remove_watermark_regions", new=AsyncMock()) as fallback,
            ):
                task = asyncio.create_task(remove_watermark(
                    source,
                    output,
                    position="auto",
                    candidates=[candidate],
                ))
                self.assertTrue(
                    await asyncio.to_thread(worker_started.wait, 2),
                    "LaMa worker did not start",
                )
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertTrue(worker_stopped.is_set())
            self.assertFalse(output.exists())
            fallback.assert_not_awaited()


class RenderEditChannelIntegrationTests(unittest.TestCase):
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

    async def test_passes_quoted_script_and_language_as_one_remote_command(self):
        with tempfile.TemporaryDirectory(prefix="media-bot-test-") as directory:
            wav_path = Path(directory) / "audio.wav"
            wav_path.write_bytes(b"wav")
            process = AsyncMock()
            process.returncode = 0
            process.communicate.return_value = (
                b'{"segments": [], "language": "fr", "duration": 0}',
                b"",
            )
            with (
                patch.dict(
                    "os.environ",
                    {"WHISPER_SSH_HOST": "whisper-host", "WHISPER_SSH_KEY": ""},
                    clear=False,
                ),
                patch(
                    "media_bot.editor.asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=process),
                ) as create_process,
            ):
                self.assertEqual(await _transcribe_ssh(wav_path, "fr", 10), [])

            arguments = create_process.await_args.args
            self.assertEqual(arguments[-2], "whisper-host")
            self.assertIn("WHISPER_LANGUAGE=fr", arguments[-1])
            self.assertIn("python3 -c ", arguments[-1])
            self.assertIn("from faster_whisper import WhisperModel", arguments[-1])
            self.assertEqual(
                create_process.await_args.kwargs["start_new_session"],
                os.name == "posix",
            )


if __name__ == "__main__":
    unittest.main()
