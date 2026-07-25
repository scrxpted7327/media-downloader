#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
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
    token = request.headers.get("X-Whisper-Token", "")
    expected = os.getenv("WHISPER_AUTH_TOKEN", "")
    if expected and token != expected:
        return web.json_response({"error": "unauthorized"}, status=403)

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "audio":
        return web.json_response({"error": "missing audio field"}, status=400)

    tmpdir = tempfile.TemporaryDirectory(prefix="whisper-srv-")
    wav_path = Path(tmpdir.name) / "audio.wav"
    try:
        with open(wav_path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)

        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            return web.json_response({"error": "empty audio"}, status=400)

        model = await _get_model()
        async with _transcribe_semaphore:
            loop = asyncio.get_running_loop()
            segments_gen, info = await loop.run_in_executor(
                None,
                lambda: model.transcribe(str(wav_path), beam_size=1),
            )
            segments = await loop.run_in_executor(None, lambda: list(segments_gen))

        result = [
            {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()}
            for seg in segments
        ]
        LOGGER.info("Transcribed: %d segments, language %s", len(result), info.language)
        return web.json_response({
            "segments": result,
            "language": info.language,
            "duration": info.duration,
        })
    except Exception as exc:
        LOGGER.exception("Transcription failed")
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        tmpdir.cleanup()


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "model_loaded": _WHISPER_MODEL is not None})


def main() -> None:
    host = os.getenv("WHISPER_HOST", "0.0.0.0")
    port = int(os.getenv("WHISPER_PORT", "8765"))
    app = web.Application()
    app.router.add_post("/transcribe", handle_transcribe)
    app.router.add_get("/health", handle_health)
    LOGGER.info("Whisper server starting on %s:%s", host, port)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
