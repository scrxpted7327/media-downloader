from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Sequence

from .downloader import DownloadError, _run_checked

LOGGER = logging.getLogger(__name__)

_VOICE_PRESETS = {
    "basic": {"ar": "22050", "ac": "1", "codec": "aac"},
    "premium": {"ar": "44100", "ac": "2", "codec": "aac"},
}


async def render_captions(
    input_path: Path,
    output_path: Path,
    caption_text: str,
    color: str = "white",
    style: str = "basic",
    position: str = "bottom",
    timeout_seconds: int = 600,
) -> Path:
    if not input_path.is_file():
        raise DownloadError("input file not found for caption rendering")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for caption rendering")

    color_map = {
        "white": "white",
        "black": "black",
        "yellow": "yellow",
        "red": "red",
        "blue": "blue",
        "green": "green",
    }
    font_color = color_map.get(color.lower(), "white")

    if style == "bold":
        fontsize, borderw, shadow = "24", "3", "2"
    elif style == "bubble":
        fontsize, borderw, shadow = "20", "4", "3"
    else:
        fontsize, borderw, shadow = "18", "2", "1"

    position_map = {
        "low": "h-text_h-20",
        "middle": "(h-text_h)/2",
        "high": "20",
    }
    y_expr = position_map.get(position.lower(), "h-text_h-20")

    safe_text = caption_text.replace("'", "\\'").replace(":", "\\:")

    drawtext = (
        f"drawtext=text='{safe_text}':fontcolor={font_color}:fontsize={fontsize}:"
        f"borderw={borderw}:bordercolor=black:shadowx=2:shadowy={shadow}:"
        f"x=(w-text_w)/2:y={y_expr}:enable='between(t,0,t+86400)'"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-vf", drawtext,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", str(output_path),
    ]
    try:
        await _run_checked(cmd, timeout_seconds, "caption rendering failed")
    except DownloadError as exc:
        raise DownloadError(f"Caption render failed: {exc}") from exc

    if not output_path.is_file():
        raise DownloadError("caption rendering produced no output file")
    return output_path


async def render_voice_over(
    input_video: Path,
    output_path: Path,
    voice_text: str,
    tts_engine: str = "say",
    voice: str = "default",
    quality: str = "basic",
    speed: float = 1.0,
    timeout_seconds: int = 600,
) -> Path:
    if not input_video.is_file():
        raise DownloadError("input video not found for voice-over")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for voice-over")

    preset = _VOICE_PRESETS.get(quality.lower(), _VOICE_PRESETS["basic"])
    tmp_audio = input_video.with_suffix(".tts-tmp.wav")

    if tts_engine == "say":
        rate = int((speed - 1.0) * 500)
        cmd = ["say", "-o", str(tmp_audio), "-r", "175"]
        if voice and voice != "default":
            cmd.extend(["-v", voice])
        if rate:
            cmd.extend(["-r", f"{175 + rate}"])
        cmd.append(voice_text)
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            if proc.returncode != 0:
                raise DownloadError(f"TTS failed: {stderr.decode()[:200]}")
        except (OSError, asyncio.TimeoutError) as exc:
            raise DownloadError("TTS generation failed or timed out") from exc
    else:
        raise DownloadError(f"Unsupported TTS engine: {tts_engine}")

    if not tmp_audio.is_file():
        raise DownloadError("TTS produced no audio file")

    try:
        await _run_checked(
            [
                "ffmpeg", "-y",
                "-i", str(input_video),
                "-i", str(tmp_audio),
                "-filter_complex",
                f"[1:a]asetrate={preset['ar']},aresample={preset['ar']}:filter_type=kaiser,"
                f"atempo={speed:.2f}[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", preset["codec"],
                "-ac", preset["ac"],
                "-ar", preset["ar"],
                "-shortest",
                str(output_path),
            ],
            timeout_seconds,
            "voice-over merge failed",
        )
    except DownloadError as exc:
        raise DownloadError(f"Voice-over merge failed: {exc}") from exc
    finally:
        tmp_audio.unlink(missing_ok=True)

    if not output_path.is_file():
        raise DownloadError("voice-over produced no output file")
    return output_path


async def render_edit(
    input_path: Path,
    output_path: Path,
    caption_text: str | None = None,
    caption_color: str = "white",
    caption_style: str = "basic",
    caption_position: str = "bottom",
    voice_text: str | None = None,
    voice: str = "default",
    voice_quality: str = "basic",
    voice_speed: float = 1.0,
    timeout_seconds: int = 600,
) -> Path:
    current = input_path
    if caption_text:
        current = await render_captions(
            current, output_path.with_name(f"{output_path.stem}_cap{output_path.suffix}"),
            caption_text, caption_color, caption_style, caption_position, timeout_seconds,
        )
    if voice_text:
        current = await render_voice_over(
            current, output_path, voice_text, "say", voice, voice_quality, voice_speed, timeout_seconds,
        )
    if current != input_path and current != output_path:
        current.replace(output_path)
    elif current != output_path:
        shutil.move(current, output_path)
    return output_path
