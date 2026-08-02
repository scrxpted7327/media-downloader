import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web

from media_bot import whisper_server
from media_bot.fix_agent import _fix_ytdlp, apply_known_fix


class _AudioField:
    name = "audio"

    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)

    async def read_chunk(self) -> bytes:
        return next(self._chunks, b"")


class _MultipartReader:
    def __init__(self, field: _AudioField | None):
        self._field = field

    async def next(self):
        field, self._field = self._field, None
        return field


def _request(
    app: web.Application,
    *,
    token: str | None = None,
    chunks: list[bytes] | None = None,
    content_length: int | None = None,
):
    headers = {"X-Whisper-Token": token} if token is not None else {}
    request = SimpleNamespace(
        app=app,
        headers=headers,
        content_length=content_length,
    )
    field = _AudioField(chunks) if chunks is not None else None
    request.multipart = AsyncMock(return_value=_MultipartReader(field))
    return request


def _json_body(response: web.Response) -> dict:
    return json.loads(response.text)


class WhisperServerHardeningTests(unittest.TestCase):
    def test_main_requires_auth_token(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(whisper_server.web, "run_app") as run_app,
        ):
            with self.assertRaisesRegex(SystemExit, "WHISPER_AUTH_TOKEN is required"):
                whisper_server.main()
        run_app.assert_not_called()

    def test_main_binds_loopback_by_default(self):
        with (
            patch.dict(os.environ, {"WHISPER_AUTH_TOKEN": "test-secret"}, clear=True),
            patch.object(whisper_server.web, "run_app") as run_app,
        ):
            whisper_server.main()

        self.assertEqual(run_app.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run_app.call_args.kwargs["port"], 8765)

    def test_create_app_fails_closed_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "WHISPER_AUTH_TOKEN is required"):
                whisper_server.create_app()

    def test_health_requires_authentication(self):
        async def run():
            app = whisper_server.create_app("test-secret")
            missing = await whisper_server.handle_health(_request(app))
            wrong = await whisper_server.handle_health(_request(app, token="wrong"))
            valid = await whisper_server.handle_health(
                _request(app, token="test-secret")
            )

            self.assertEqual(missing.status, 403)
            self.assertEqual(wrong.status, 403)
            self.assertEqual(valid.status, 200)
            self.assertEqual(_json_body(valid)["status"], "ok")

        asyncio.run(run())

    def test_request_fails_closed_when_app_has_no_auth_configuration(self):
        async def run():
            app = web.Application()
            with patch.dict(os.environ, {}, clear=True):
                response = await whisper_server.handle_health(_request(app))
                self.assertEqual(response.status, 503)
                self.assertEqual(
                    _json_body(response), {"error": "service unavailable"}
                )

        asyncio.run(run())

    def test_transcription_cleans_temporary_artifacts(self):
        async def run():
            segment = SimpleNamespace(start=0.1, end=0.9, text=" hello ")
            info = SimpleNamespace(language="en", duration=1.0)
            observed_paths: list[Path] = []
            test_case = self

            class Model:
                def transcribe(self, path, *, beam_size):
                    audio_path = Path(path)
                    test_case.assertEqual(beam_size, 1)
                    observed_paths.append(audio_path)
                    test_case.assertTrue(audio_path.is_file())
                    return iter([segment]), info

            app = whisper_server.create_app("test-secret", max_request_bytes=4096)
            real_temporary_directory = tempfile.TemporaryDirectory
            with real_temporary_directory() as tracking_dir:
                tracking_root = Path(tracking_dir)

                def tracked_temporary_directory(*args, **kwargs):
                    kwargs["dir"] = tracking_root
                    return real_temporary_directory(*args, **kwargs)

                with (
                    patch.object(
                        whisper_server.tempfile,
                        "TemporaryDirectory",
                        side_effect=tracked_temporary_directory,
                    ),
                    patch.object(
                        whisper_server,
                        "_get_model",
                        AsyncMock(return_value=Model()),
                    ),
                ):
                    response = await whisper_server.handle_transcribe(
                        _request(
                            app,
                            token="test-secret",
                            chunks=[b"audio-data"],
                            content_length=10,
                        )
                    )
                    body = _json_body(response)

                self.assertEqual(response.status, 200)
                self.assertEqual(body["segments"][0]["text"], "hello")
                self.assertEqual(len(observed_paths), 1)
                self.assertFalse(observed_paths[0].exists())
                self.assertEqual(list(tracking_root.iterdir()), [])

        asyncio.run(run())

    def test_request_size_cap_rejects_upload_before_transcription(self):
        async def run():
            app = whisper_server.create_app("test-secret", max_request_bytes=512)
            get_model = AsyncMock()
            with patch.object(whisper_server, "_get_model", get_model):
                declared_large = await whisper_server.handle_transcribe(
                    _request(
                        app,
                        token="test-secret",
                        chunks=[b"x"],
                        content_length=2048,
                    )
                )
                streamed_large = await whisper_server.handle_transcribe(
                    _request(
                        app,
                        token="test-secret",
                        chunks=[b"x" * 300, b"x" * 300],
                        content_length=None,
                    )
                )

            self.assertEqual(app._client_max_size, 512)
            self.assertEqual(declared_large.status, 413)
            self.assertEqual(streamed_large.status, 413)
            get_model.assert_not_awaited()

        asyncio.run(run())

    def test_internal_error_is_generic_and_temp_directory_is_removed(self):
        async def run():
            class BrokenModel:
                def transcribe(self, path, *, beam_size):
                    raise RuntimeError(f"secret temporary path: {path}")

            app = whisper_server.create_app("test-secret", max_request_bytes=4096)
            real_temporary_directory = tempfile.TemporaryDirectory
            with real_temporary_directory() as tracking_dir:
                tracking_root = Path(tracking_dir)

                def tracked_temporary_directory(*args, **kwargs):
                    kwargs["dir"] = tracking_root
                    return real_temporary_directory(*args, **kwargs)

                with (
                    patch.object(
                        whisper_server.tempfile,
                        "TemporaryDirectory",
                        side_effect=tracked_temporary_directory,
                    ),
                    patch.object(
                        whisper_server,
                        "_get_model",
                        AsyncMock(return_value=BrokenModel()),
                    ),
                ):
                    response = await whisper_server.handle_transcribe(
                        _request(
                            app,
                            token="test-secret",
                            chunks=[b"audio-data"],
                            content_length=10,
                        )
                    )
                    body = _json_body(response)

                self.assertEqual(response.status, 500)
                self.assertEqual(body, {"error": "transcription failed"})
                self.assertEqual(list(tracking_root.iterdir()), [])

        asyncio.run(run())


class FixYtdlpTests(unittest.TestCase):
    def test_fix_uses_platform_named_verified_binary(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                tools_dir = Path(temporary)
                binary = tools_dir / "yt-dlp_linux"
                binary.write_bytes(b"verified")
                binary.chmod(0o755)
                process = MagicMock(returncode=0)
                process.communicate = AsyncMock(return_value=(b"updated", b""))

                with (
                    patch("media_bot.tools._asset_name", return_value=binary.name),
                    patch(
                        "media_bot.fix_agent.asyncio.create_subprocess_exec",
                        AsyncMock(return_value=process),
                    ) as create_process,
                ):
                    result = await _fix_ytdlp(tools_dir)

                self.assertIsNone(result)
                self.assertEqual(create_process.await_args.args[:2], (str(binary), "--update"))

        asyncio.run(run())

    def test_fix_does_not_fall_back_to_legacy_unverified_name(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                tools_dir = Path(temporary)
                legacy = tools_dir / "yt-dlp"
                legacy.write_bytes(b"legacy")
                legacy.chmod(0o755)

                with (
                    patch("media_bot.tools._asset_name", return_value="yt-dlp_linux"),
                    patch(
                        "media_bot.fix_agent.asyncio.create_subprocess_exec",
                        AsyncMock(),
                    ) as create_process,
                ):
                    result = await _fix_ytdlp(tools_dir)

                self.assertIn("yt-dlp_linux", result)
                create_process.assert_not_awaited()

        asyncio.run(run())

    def test_known_fix_remains_gated(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                with patch(
                    "media_bot.fix_agent.asyncio.create_subprocess_exec", AsyncMock()
                ) as create_process:
                    result = await apply_known_fix(
                        {"category": "ytdlp", "message": "yt-dlp failed"},
                        Path(temporary),
                        repair_enabled=False,
                    )

                self.assertIn("disabled", result)
                create_process.assert_not_awaited()

        asyncio.run(run())

    def test_timed_out_update_is_terminated(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary:
                tools_dir = Path(temporary)
                binary = tools_dir / "yt-dlp_linux"
                binary.write_bytes(b"verified")
                binary.chmod(0o755)
                process = MagicMock()
                process.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
                process.wait = AsyncMock()

                with (
                    patch("media_bot.tools._asset_name", return_value=binary.name),
                    patch(
                        "media_bot.fix_agent.asyncio.create_subprocess_exec",
                        AsyncMock(return_value=process),
                    ),
                ):
                    result = await _fix_ytdlp(tools_dir)

                self.assertIn("timed out", result)
                process.kill.assert_called_once_with()
                process.wait.assert_awaited_once_with()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
