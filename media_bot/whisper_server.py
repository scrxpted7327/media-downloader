#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import tempfile
from pathlib import Path

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("whisper_server")

_WHISPER_MODEL = None
_WHISPER_LOCK = asyncio.Lock()
_transcribe_semaphore = asyncio.Semaphore(1)

DEFAULT_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_AUTH_TOKEN_KEY = web.AppKey("whisper_auth_token", str)
_MAX_REQUEST_BYTES_KEY = web.AppKey("whisper_max_request_bytes", int)


async def _get_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    async with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        from faster_whisper import WhisperModel
        LOGGER.info("Loading faster-whisper model...")
        loop = asyncio.get_running_loop()
        _WHISPER_MODEL = await loop.run_in_executor(
            None,
            lambda: WhisperModel("tiny", device="cpu", compute_type="int8"),
        )
        LOGGER.info("Model loaded")
    return _WHISPER_MODEL


async def handle_transcribe(request: web.Request) -> web.Response:
    auth_error = _authorize_request(request)
    if auth_error is not None:
        return auth_error

    max_request_bytes = request.app.get(
        _MAX_REQUEST_BYTES_KEY, DEFAULT_MAX_REQUEST_BYTES
    )
    if request.content_length is not None and request.content_length > max_request_bytes:
        return web.json_response({"error": "request too large"}, status=413)
    try:
        reader = await request.multipart()
        field = await reader.next()
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"error": "request too large"}, status=413)
    except (AssertionError, ValueError):
        return web.json_response({"error": "multipart form required"}, status=400)
    if field is None or field.name != "audio":
        return web.json_response({"error": "missing audio field"}, status=400)

    try:
        with tempfile.TemporaryDirectory(prefix="whisper-srv-") as temporary:
            wav_path = Path(temporary) / "audio.wav"
            received = 0
            with wav_path.open("xb") as audio_file:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_request_bytes:
                        return web.json_response(
                            {"error": "request too large"}, status=413
                        )
                    audio_file.write(chunk)

            if received == 0:
                return web.json_response({"error": "empty audio"}, status=400)

            model = await _get_model()
            async with _transcribe_semaphore:
                loop = asyncio.get_running_loop()
                segments_gen, info = await loop.run_in_executor(
                    None,
                    lambda: model.transcribe(str(wav_path), beam_size=1),
                )
                segments = await loop.run_in_executor(
                    None, lambda: list(segments_gen)
                )

            result = [
                {
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "text": segment.text.strip(),
                }
                for segment in segments
            ]
            LOGGER.info(
                "Transcribed: %d segments, language %s", len(result), info.language
            )
            return web.json_response(
                {
                    "segments": result,
                    "language": info.language,
                    "duration": info.duration,
                }
            )
    except web.HTTPRequestEntityTooLarge:
        return web.json_response({"error": "request too large"}, status=413)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("Transcription failed")
        return web.json_response({"error": "transcription failed"}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    auth_error = _authorize_request(request)
    if auth_error is not None:
        return auth_error
    return web.json_response({"status": "ok", "model_loaded": _WHISPER_MODEL is not None})


def _authorize_request(request: web.Request) -> web.Response | None:
    expected = request.app.get(_AUTH_TOKEN_KEY)
    if expected is None:
        expected = os.getenv("WHISPER_AUTH_TOKEN", "").strip()
    if not expected:
        LOGGER.error("Rejecting request because WHISPER_AUTH_TOKEN is not configured")
        return web.json_response({"error": "service unavailable"}, status=503)
    supplied = request.headers.get("X-Whisper-Token", "")
    if not hmac.compare_digest(supplied, expected):
        return web.json_response({"error": "unauthorized"}, status=403)
    return None


def create_app(
    auth_token: str | None = None,
    *,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> web.Application:
    token = (
        os.getenv("WHISPER_AUTH_TOKEN", "") if auth_token is None else auth_token
    ).strip()
    if not token:
        raise RuntimeError(
            "WHISPER_AUTH_TOKEN is required; refusing to start an unauthenticated service"
        )
    if max_request_bytes <= 0:
        raise ValueError("Whisper request-size limit must be positive")
    app = web.Application(client_max_size=max_request_bytes)
    app[_AUTH_TOKEN_KEY] = token
    app[_MAX_REQUEST_BYTES_KEY] = max_request_bytes
    app.router.add_post("/transcribe", handle_transcribe)
    app.router.add_get("/health", handle_health)
    return app


def main() -> None:
    token = os.getenv("WHISPER_AUTH_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "WHISPER_AUTH_TOKEN is required; refusing to start an unauthenticated service"
        )
    host = os.getenv("WHISPER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WHISPER_PORT", "8765"))
    max_request_bytes = int(
        os.getenv("WHISPER_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES))
    )
    app = create_app(token, max_request_bytes=max_request_bytes)
    LOGGER.info("Whisper server starting on %s:%s", host, port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
