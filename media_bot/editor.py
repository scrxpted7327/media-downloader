from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from .downloader import DownloadError, _run_checked
from .tools import prefer_ffmpeg_full

prefer_ffmpeg_full()

ProgressCallback = Callable[[int], Awaitable[None]]

LOGGER = logging.getLogger(__name__)

_VOICE_PRESETS = {
    "basic": {"ar": "22050", "ac": "1", "codec": "aac"},
    "premium": {"ar": "44100", "ac": "2", "codec": "aac"},
}

_VIDEO_ENCODER: str | None = None

def _detect_video_encoder() -> str:
    global _VIDEO_ENCODER
    if _VIDEO_ENCODER is not None:
        return _VIDEO_ENCODER
    import subprocess
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=64x64",
             "-c:v", "libx264", "-t", "1", "-f", "null", "-"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 or "Unknown encoder" not in result.stderr.decode():
            _VIDEO_ENCODER = "libx264"
            return _VIDEO_ENCODER
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in ["h264_v4l2m2m", "mpeg4", "h264_nvenc", "h264_vaapi"]:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=64x64",
                 "-c:v", candidate, "-t", "1", "-f", "null", "-"],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                _VIDEO_ENCODER = candidate
                LOGGER.info("Using video encoder: %s", candidate)
                return _VIDEO_ENCODER
        except (OSError, subprocess.TimeoutExpired):
            pass
    _VIDEO_ENCODER = "mpeg4"
    LOGGER.warning("No hardware encoder found, falling back to mpeg4")
    return _VIDEO_ENCODER

_WHISPER_MODEL = None
_WHISPER_LOCK = asyncio.Lock()
_transcribe_semaphore = asyncio.Semaphore(1)


async def _get_whisper_model_async():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    async with _WHISPER_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL
        from faster_whisper import WhisperModel
        LOGGER.info("Loading faster-whisper tiny model in thread...")
        loop = asyncio.get_running_loop()
        _WHISPER_MODEL = await loop.run_in_executor(
            None, lambda: WhisperModel("tiny", device="cpu", compute_type="int8"),
        )
        LOGGER.info("faster-whisper model loaded")
    return _WHISPER_MODEL


async def _detect_tts_engine(preferred: str | None = None) -> str:
    if preferred and preferred != "auto":
        return preferred
    if shutil.which("edge-tts") or await _async_import("edge_tts"):
        return "edge-tts"
    if shutil.which("espeak-ng"):
        return "espeak-ng"
    if shutil.which("say"):
        return "say"
    raise DownloadError("no TTS engine available (install edge-tts, espeak-ng, or use macOS)")


async def _async_import(name: str) -> bool:
    import importlib
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


async def _tts_edge(
    text: str, output_path: Path, voice: str, speed: float, timeout: int,
) -> None:
    import edge_tts
    voice_name = resolve_voice(voice, "edge-tts")
    rate_pct = int(round((speed - 1.0) * 100))
    rate_str = f"{rate_pct:+d}%"
    communicate = edge_tts.Communicate(text, voice_name, rate=rate_str)
    await asyncio.wait_for(
        communicate.save(str(output_path)),
        timeout=timeout,
    )


async def _tts_espeak(
    text: str, output_path: Path, voice: str, speed: float, timeout: int,
) -> None:
    resolved = resolve_voice(voice, "espeak-ng")
    voice_flag = ["-v", resolved]
    speed_flag = ["-s", str(int(175 * speed))]
    cmd = ["espeak-ng", "-w", str(output_path), *voice_flag, *speed_flag, text]
    await _run_checked(cmd, timeout, "espeak-ng TTS failed")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise DownloadError("espeak-ng produced empty audio")


async def _tts_say(
    text: str, output_path: Path, voice: str, speed: float, timeout: int,
) -> None:
    resolved = resolve_voice(voice, "say")
    cmd = ["say", "-o", str(output_path)]
    if resolved and resolved != "default":
        cmd.extend(["-v", resolved])
    rate = 175 + int((speed - 1.0) * 500)
    cmd.extend(["-r", str(rate), text])
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise DownloadError(f"say TTS failed: {stderr.decode()[:200]}")
    except (OSError, asyncio.TimeoutError) as exc:
        raise DownloadError("say TTS failed or timed out") from exc


_TTS_ENGINES: dict[str, callable] = {
    "edge-tts": _tts_edge,
    "espeak-ng": _tts_espeak,
    "say": _tts_say,
}

_VOICE_CACHE: dict[str, list[dict[str, str]]] = {}

async def list_tts_voices(engine: str | None = None) -> list[dict[str, str]]:
    engine = await _detect_tts_engine(engine)
    if engine in _VOICE_CACHE:
        return _VOICE_CACHE[engine]

    voices: list[dict[str, str]] = []
    if engine == "edge-tts":
        import edge_tts
        raw = await edge_tts.list_voices()
        for v in raw:
            voices.append({
                "name": v["ShortName"],
                "locale": v["Locale"],
                "gender": v["Gender"],
                "desc": f"{v['Locale']} - {v['ShortName']} ({v['Gender']})",
            })
        voices.sort(key=lambda x: (x["locale"], x["gender"], x["name"]))
    elif engine == "espeak-ng":
        import subprocess
        try:
            result = subprocess.run(["espeak-ng", "--voices"], capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    voices.append({
                        "name": parts[1],
                        "locale": parts[0],
                        "gender": parts[3] if len(parts) > 3 else "?",
                        "desc": line.strip(),
                    })
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif engine == "say":
        import subprocess
        try:
            result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                parts = line.strip().split(None, 2)
                if len(parts) >= 2:
                    name = parts[0]
                    locale = parts[1].strip("()")
                    voices.append({
                        "name": name,
                        "locale": locale,
                        "gender": "?",
                        "desc": line.strip(),
                    })
        except (OSError, subprocess.TimeoutExpired):
            pass

    _VOICE_CACHE[engine] = voices
    return voices


def resolve_voice(voice: str, engine: str | None = None) -> str:
    if voice and voice not in ("default", "male", "female", ""):
        return voice
    if engine == "edge-tts":
        return {"default": "en-US-AriaNeural", "male": "en-US-GuyNeural", "female": "en-US-AriaNeural"}.get(voice, "en-US-AriaNeural")
    if engine == "espeak-ng":
        return {"default": "en", "male": "en", "female": "en+f3"}.get(voice, "en")
    return "default"


async def _run_ffmpeg_with_progress(
    cmd: list[str], timeout: int, label: str, total_duration_us: int | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bytes:
    if cmd and Path(cmd[0]).name.startswith("ffmpeg") and "-progress" not in cmd:
        cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise DownloadError(f"{label}: process failed") from exc

    last_pct = -1
    stderr_lines: list[bytes] = []

    async def _read_stderr() -> None:
        assert process.stderr is not None
        while line := await process.stderr.readline():
            stderr_lines.append(line)

    async def _read_progress() -> None:
        nonlocal last_pct
        assert process.stdout is not None
        if progress_callback is None or not total_duration_us:
            # Drain stdout so the process cannot block on a full pipe.
            while await process.stdout.read(4096):
                pass
            return
        buffer = b""
        while True:
            chunk = await process.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"out_time_us="):
                    continue
                try:
                    current_us = int(line.split(b"=", 1)[1])
                except ValueError:
                    continue
                if total_duration_us <= 0:
                    continue
                pct = min(99, int(current_us * 100 // total_duration_us))
                if pct > last_pct:
                    last_pct = pct
                    await progress_callback(pct)

    readers = (
        asyncio.create_task(_read_progress()),
        asyncio.create_task(_read_stderr()),
    )

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        process.kill()
        await process.wait()
        await asyncio.gather(*readers, return_exceptions=True)
        raise DownloadError(f"{label} timed out (>{timeout}s)") from exc

    await asyncio.gather(*readers)

    if process.returncode != 0:
        details = b"".join(stderr_lines).decode("utf-8", "replace").strip().splitlines()[-3:]
        raise DownloadError(f"{label}: {'; '.join(details)[:500]}")

    return b""


def _get_duration_us(path: Path) -> int | None:
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(float(result.stdout.strip()) * 1_000_000)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


_SSH_WHISPER_SCRIPT = r"""
import sys, json, tempfile, os
from pathlib import Path
data = sys.stdin.buffer.read()
tmp = tempfile.NamedTemporaryFile(prefix='whisper-', suffix='.wav', delete=False)
tmp.write(data)
tmp.close()
try:
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments, info = model.transcribe(tmp.name, beam_size=1)
    result = [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip()} for s in segments]
    print(json.dumps({"segments": result, "language": info.language, "duration": info.duration}))
except Exception as e:
    print(json.dumps({"error": str(e)}), file=sys.stderr)
    sys.exit(1)
finally:
    os.unlink(tmp.name)
"""


async def _transcribe_ssh(wav_path: Path, language: str | None, timeout: int) -> list[dict]:
    ssh_target = os.environ.get("WHISPER_SSH_HOST", "").strip()
    if not ssh_target:
        raise DownloadError("WHISPER_SSH_HOST not set")
    ssh_key = os.environ.get("WHISPER_SSH_KEY", "").strip()
    ssh_args = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if ssh_key:
        ssh_args.extend(["-i", ssh_key])
    ssh_args.append(ssh_target)
    ssh_args.extend(["python3", "-c", _SSH_WHISPER_SCRIPT])
    LOGGER.info("Transcribing via SSH to %s...", ssh_target)
    proc = await asyncio.create_subprocess_exec(
        *ssh_args,
        stdin=wav_path.open("rb"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError(f"SSH whisper timed out on {ssh_target}")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[:300]
        raise DownloadError(f"SSH whisper failed on {ssh_target}: {err}")
    result = json.loads(stdout.decode("utf-8", "replace"))
    if "error" in result:
        raise DownloadError(f"SSH whisper error: {result['error']}")
    segments = result.get("segments", [])
    LOGGER.info("SSH transcription: %d segments, language %s", len(segments), result.get("language", "?"))
    return segments


async def transcribe_audio(
    input_path: Path,
    language: str | None = None,
    timeout_seconds: int = 1800,
) -> list[dict]:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for transcription")

    tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-whisper-")
    wav_path = Path(tmpdir.name) / "audio.wav"

    try:
        await _run_checked(
            ["ffmpeg", "-y", "-i", str(input_path), "-vn", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", str(wav_path)],
            timeout_seconds,
            f"audio extraction failed for {input_path.name}",
        )
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            raise DownloadError("audio extraction produced empty output")

        ssh_host = os.environ.get("WHISPER_SSH_HOST", "")
        if ssh_host:
            return await _transcribe_ssh(wav_path, language, timeout_seconds)

        model = await _get_whisper_model_async()
        LOGGER.info("Transcribing %s locally...", input_path.name)
        async with _transcribe_semaphore:
            loop = asyncio.get_running_loop()
            segments_gen, info = await loop.run_in_executor(
                None,
                lambda: model.transcribe(str(wav_path), beam_size=1, language=language),
            )
            segments = await loop.run_in_executor(None, lambda: list(segments_gen))
        result = [
            {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()}
            for seg in segments
        ]
        LOGGER.info(
            "Transcription complete: %d segments, language %s", len(result), info.language,
        )
        return result
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"transcription failed: {exc}") from exc
    finally:
        tmpdir.cleanup()


def _segments_to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        start_h = int(seg["start"] // 3600)
        start_m = int((seg["start"] % 3600) // 60)
        start_s = seg["start"] % 60
        end_h = int(seg["end"] // 3600)
        end_m = int((seg["end"] % 3600) // 60)
        end_s = seg["end"] % 60
        lines.append(str(i))
        lines.append(f"{start_h:02d}:{start_m:02d}:{start_s:06.3f} --> {end_h:02d}:{end_m:02d}:{end_s:06.3f}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


async def render_captions(
    input_path: Path,
    output_path: Path,
    caption_text: str | None = None,
    color: str = "white",
    style: str = "basic",
    position: str = "bottom",
    auto_captions: bool = True,
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
    srt_output_path: Path | None = None,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for caption rendering ({input_path.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for caption rendering")

    if auto_captions or not caption_text:
        segments = await transcribe_audio(input_path, timeout_seconds=timeout_seconds)
        if not segments:
            raise DownloadError(f"transcription produced no segments for {input_path.name}")
        srt_content = _segments_to_srt(segments)
        tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-srt-")
        srt_path = Path(tmpdir.name) / "captions.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        if srt_output_path:
            srt_output_path.write_text(srt_content, encoding="utf-8")

        ass_style = _build_ass_style(color, style, position)
        ass_header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 0\n"
            "PlayDepth: 0\n"
            "[V4+ Styles]\n"
            f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{ass_style['fontsize']},{ass_style['color']},&H00000000,&H00000000,{ass_style['backcolour']},{ass_style['bold']},0,0,0,100,100,0,0,{ass_style['borderstyle']},{ass_style['outline']},{ass_style['shadow']},{ass_style['alignment']},10,10,10,1\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        for seg in segments:
            start_h = int(seg["start"] // 3600)
            start_m = int((seg["start"] % 3600) // 60)
            start_s = seg["start"] % 60
            end_h = int(seg["end"] // 3600)
            end_m = int((seg["end"] % 3600) // 60)
            end_s = seg["end"] % 60
            safe_text = seg["text"].replace("{", "\\{").replace("}", "\\}")
            ass_header += (
                f"Dialogue: 0,{start_h:02d}:{start_m:02d}:{start_s:06.3f},"
                f"{end_h:02d}:{end_m:02d}:{end_s:06.3f},Default,,0,0,0,,{safe_text}\n"
            )

        ass_path = Path(tmpdir.name) / "captions.ass"
        ass_path.write_text(ass_header, encoding="utf-8")

        try:
            duration_us = _get_duration_us(input_path)
            if progress_callback:
                await progress_callback(50)
            await _run_ffmpeg_with_progress(
                ["ffmpeg", "-y", "-i", str(input_path),
                 "-vf", f"ass={str(ass_path)}",
                 "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
                 "-c:a", "copy", "-movflags", "+faststart", str(output_path)],
                timeout_seconds,
                f"closed caption burn failed for {input_path.name}",
                total_duration_us=duration_us,
                progress_callback=lambda p: progress_callback(50 + p // 2) if progress_callback else None,
            )
        finally:
            tmpdir.cleanup()
    else:
        if not caption_text:
            raise DownloadError("caption_text is required when auto_captions is off")
        from .colors import resolve_drawtext_color
        font_color = resolve_drawtext_color(color, "white")
        if style == "bold":
            fontsize, borderw, shadow = "24", "3", "2"
        elif style == "bubble":
            fontsize, borderw, shadow = "20", "4", "3"
        elif style == "border":
            fontsize, borderw, shadow = "18", "4", "1"
        elif style == "filled":
            fontsize, borderw, shadow = "18", "0", "0"
        else:
            fontsize, borderw, shadow = "18", "2", "1"
        position_map = {
            "low": "20",
            "middle": "(h-text_h)/2",
            "high": "h-text_h-20",
        }
        y_expr = position_map.get(position.lower(), "h-text_h-20")
        safe_text = caption_text.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
        box_param = ":box=1:boxcolor=black@0.5" if style == "filled" else ""
        drawtext = (
            f"drawtext=text='{safe_text}':fontcolor={font_color}:fontsize={fontsize}:"
            f"borderw={borderw}:bordercolor=black:shadowx=2:shadowy={shadow}"
            f"{box_param}:"
            f"x=(w-text_w)/2:y={y_expr}:enable='between(t,0,t+86400)'"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path), "-vf", drawtext,
            "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
            "-c:a", "copy", "-movflags", "+faststart", str(output_path),
        ]
        try:
            duration_us = _get_duration_us(input_path)
            await _run_ffmpeg_with_progress(
                cmd, timeout_seconds, f"caption render failed for {input_path.name}",
                total_duration_us=duration_us,
                progress_callback=progress_callback,
            )
        except DownloadError as exc:
            raise DownloadError(f"Caption render failed (text={caption_text[:50]}): {exc}") from exc

    if not output_path.is_file():
        raise DownloadError(f"caption rendering produced no output file ({output_path.name})")
    return output_path


def _build_ass_style(color: str, style: str, position: str) -> dict:
    from .colors import resolve_ass_color
    style_map = {
        "bold": {"fontsize": 24, "bold": 1, "borderstyle": 1, "outline": 3, "shadow": 2},
        "bubble": {"fontsize": 20, "bold": 0, "borderstyle": 3, "outline": 4, "shadow": 3},
        "border": {"fontsize": 18, "bold": 0, "borderstyle": 1, "outline": 3, "shadow": 1},
        "filled": {"fontsize": 18, "bold": 0, "borderstyle": 3, "outline": 1, "shadow": 0, "backcolour": "&H80000000"},
    }
    default = {"fontsize": 18, "bold": 0, "borderstyle": 1, "outline": 2, "shadow": 1}
    s = style_map.get(style.lower(), default)
    s.setdefault("backcolour", "&H00000000")
    align_map = {"low": 8, "middle": 5, "high": 2}
    s["alignment"] = align_map.get(position.lower(), 2)
    s["color"] = resolve_ass_color(color)
    return s


async def render_voice_over(
    input_video: Path,
    output_path: Path,
    voice_text: str,
    tts_engine: str | None = None,
    voice: str = "default",
    quality: str = "basic",
    speed: float = 1.0,
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    if not input_video.is_file():
        raise DownloadError(f"input video not found for voice-over ({input_video.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for voice-over")
    if not voice_text:
        raise DownloadError("voice_text is empty")

    engine = await _detect_tts_engine(tts_engine)
    preset = _VOICE_PRESETS.get(quality.lower(), _VOICE_PRESETS["basic"])

    tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-tts-")
    tmp_audio = Path(tmpdir.name) / "tts-output.wav"

    tts_func = _TTS_ENGINES.get(engine)
    if tts_func is None:
        raise DownloadError(f"unsupported TTS engine: {engine}")

    try:
        if progress_callback:
            await progress_callback(10)
        await tts_func(voice_text, tmp_audio, voice, speed, timeout_seconds)
        if progress_callback:
            await progress_callback(30)

        if not tmp_audio.is_file() or tmp_audio.stat().st_size == 0:
            raise DownloadError(f"TTS engine ({engine}) produced no audio")

        if progress_callback:
            await progress_callback(50)
        if progress_callback:
            await progress_callback(50)
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
            f"voice-over merge failed for {input_video.name}",
        )
        if progress_callback:
            await progress_callback(100)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"voice-over failed ({engine}): {exc}") from exc
    finally:
        tmpdir.cleanup()

    if not output_path.is_file():
        raise DownloadError(f"voice-over produced no output file ({output_path.name})")
    return output_path


async def render_banner(
    input_path: Path,
    output_path: Path,
    banner_path: Path,
    position: str = "bottom",
    scale: str = "fill",
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for banner overlay ({input_path.name})")
    if not banner_path.is_file():
        raise DownloadError(f"banner image not found ({banner_path})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for banner overlay")

    vid_w, vid_h = _get_video_dimensions(input_path)
    max_h = max(2, int(vid_h * 0.15) // 2 * 2)
    max_w = max(2, vid_w // 2 * 2)

    if scale == "stretch":
        overlay = f"scale={max_w}:{max_h}"
    elif scale == "fill":
        overlay = (
            f"scale={max_w}:{max_h}:force_original_aspect_ratio=increase,"
            f"crop={max_w}:{max_h}"
        )
    else:
        # fit: preserve aspect ratio, fit inside video width × 15% height
        overlay = f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease"

    position_map = {
        "top": "0:0",
        "bottom": "0:main_h-overlay_h",
        "overlay": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
    }
    pos = position_map.get(position.lower(), position_map["bottom"])

    filter_chain = f"[1:v]{overlay}[b];[0:v][b]overlay={pos}"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-i", str(banner_path),
        "-filter_complex", filter_chain,
        "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    try:
        duration_us = _get_duration_us(input_path)
        await _run_ffmpeg_with_progress(
            cmd, timeout_seconds, f"banner overlay failed for {input_path.name}",
            total_duration_us=duration_us,
            progress_callback=progress_callback,
        )
    except DownloadError as exc:
        raise DownloadError(f"Banner render failed: {exc}") from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise DownloadError(f"banner overlay produced no output file ({output_path.name})")
    return output_path


_WATERMARK_POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right", "center"]
_WATERMARK_REGION_RATIO = 0.15


async def remove_watermark(
    input_path: Path,
    output_path: Path,
    position: str = "auto",
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for watermark removal ({input_path.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for watermark removal")

    if position == "auto":
        detected = await _detect_watermark_position(input_path, timeout_seconds)
        if detected is None:
            LOGGER.warning("Could not auto-detect watermark, falling back to top-right")
            position = "top-right"
        else:
            position = detected

    duration_us = _get_duration_us(input_path)
    vid_w, vid_h = _get_video_dimensions(input_path)

    pw = max(1, int(vid_w * _WATERMARK_REGION_RATIO))
    ph = max(1, int(vid_h * _WATERMARK_REGION_RATIO * 0.6))
    pw = min(pw, vid_w - 3)
    ph = min(ph, vid_h - 3)

    position_map = {
        "top-left": (1, 1),
        "top-right": (vid_w - pw - 1, 1),
        "bottom-left": (1, vid_h - ph - 1),
        "bottom-right": (vid_w - pw - 1, vid_h - ph - 1),
        "center": ((vid_w - pw) // 2, (vid_h - ph) // 2),
    }
    x, y = position_map.get(position, (vid_w - pw - 1, 1))
    delogo = f"delogo=x={x}:y={y}:w={pw}:h={ph}"

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", f"{delogo},format=yuv420p",
        "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    try:
        await _run_ffmpeg_with_progress(
            cmd, timeout_seconds, f"watermark removal failed for {input_path.name}",
            total_duration_us=duration_us,
            progress_callback=progress_callback,
        )
    except DownloadError:
        LOGGER.warning("Watermark removal failed for %s, skipping", input_path.name)
        return input_path

    if not output_path.is_file():
        LOGGER.warning("Watermark removal produced no output for %s, skipping", input_path.name)
        return input_path
    return output_path


async def _detect_watermark_position(input_path: Path, timeout_seconds: int) -> str | None:
    if shutil.which("ffprobe") is None:
        return None
    from PIL import Image

    tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-wm-")
    try:
        frame1 = Path(tmpdir.name) / "frame1.png"
        frame2 = Path(tmpdir.name) / "frame2.png"

        for f, ts in [(frame1, "00:00:01"), (frame2, "00:00:03")]:
            await _run_checked(
                ["ffmpeg", "-y", "-ss", ts, "-i", str(input_path),
                 "-vframes", "1", "-q:v", "2", str(f)],
                timeout_seconds, "frame extraction failed",
            )

        img1 = Image.open(frame1).convert("RGB")
        img2 = Image.open(frame2).convert("RGB")
        w, h = img1.size

        pw = int(w * _WATERMARK_REGION_RATIO)
        ph = int(h * _WATERMARK_REGION_RATIO * 0.6)

        regions = {
            "top-left": (0, 0, pw, ph),
            "top-right": (w - pw, 0, w, ph),
            "bottom-left": (0, h - ph, pw, h),
            "bottom-right": (w - pw, h - ph, w, h),
            "center": ((w - pw) // 2, (h - ph) // 2, (w + pw) // 2, (h + ph) // 2),
        }

        best_score = float("inf")
        best_pos: str | None = None

        for pos, (x1, y1, x2, y2) in regions.items():
            crop1 = img1.crop((x1, y1, x2, y2))
            crop2 = img2.crop((x1, y1, x2, y2))
            diff = _image_difference(crop1, crop2)
            if diff < best_score:
                best_score = diff
                best_pos = pos

        if best_score < 15.0:
            return best_pos
        return None
    finally:
        tmpdir.cleanup()


def _image_difference(img1: Image.Image, img2: Image.Image) -> float:
    import math
    pixels1 = [img1.getpixel((x, y)) for y in range(img1.height) for x in range(img1.width)]
    pixels2 = [img2.getpixel((x, y)) for y in range(img2.height) for x in range(img2.width)]
    total = 0.0
    count = len(pixels1)
    for p1, p2 in zip(pixels1, pixels2):
        dr = p1[0] - p2[0]
        dg = p1[1] - p2[1]
        db = p1[2] - p2[2]
        total += math.sqrt(dr * dr + dg * dg + db * db) / 441.67
    return total / count


def _get_video_dimensions(path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return 1920, 1080


async def render_channel_banner(
    input_path: Path,
    output_path: Path,
    source_url: str,
    caption_text: str | None = None,
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for channel banner ({input_path.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for channel banner overlay")

    vid_w, vid_h = _get_video_dimensions(input_path)
    if vid_w <= vid_h:
        LOGGER.info("Video is portrait/square (%dx%d), skipping channel banner", vid_w, vid_h)
        if output_path != input_path:
            shutil.copy2(str(input_path), str(output_path))
        return output_path

    avatar_path: Path | None = None
    channel_title = ""
    tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-channel-")
    try:
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(source_url, download=False)
                channel_title = info.get("channel", info.get("uploader", info.get("creator", ""))) or ""
                avatar_url = (
                    info.get("channel_url") or info.get("uploader_url") or ""
                )
                if avatar_url:
                    try:
                        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl2:
                            chan_info = ydl2.extract_info(avatar_url, download=False)
                            thumb = chan_info.get("thumbnails", [])
                            if thumb:
                                av_url = thumb[-1].get("url", "")
                                if av_url:
                                    import urllib.request
                                    avatar_path = Path(tmpdir.name) / "avatar.png"
                                    urllib.request.urlretrieve(av_url, avatar_path)
                    except Exception:
                        pass
                if not avatar_path:
                    thumbs = info.get("thumbnails", [])
                    if thumbs:
                        av_url = thumbs[-1].get("url", "")
                        if av_url:
                            import urllib.request
                            avatar_path = Path(tmpdir.name) / "avatar.png"
                            urllib.request.urlretrieve(av_url, avatar_path)
        except Exception as exc:
            LOGGER.warning("Could not fetch channel info: %s", exc)

        banner_img_path = await _compose_channel_banner_image(
            Path(tmpdir.name), vid_w, vid_h, avatar_path, channel_title, caption_text,
        )

        duration_us = _get_duration_us(input_path)
        position_map = {
            "bottom": f"0:{vid_h - int(vid_h * 0.18)}",
        }
        pos = position_map["bottom"]

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-i", str(banner_img_path),
            "-filter_complex",
            f"[1:v]scale={vid_w}:{int(vid_h * 0.18)}[b];[0:v][b]overlay=0:{vid_h - int(vid_h * 0.18)}",
            "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
        await _run_ffmpeg_with_progress(
            cmd, timeout_seconds, f"channel banner overlay failed for {input_path.name}",
            total_duration_us=duration_us,
            progress_callback=progress_callback,
        )
    finally:
        tmpdir.cleanup()

    if not output_path.is_file():
        raise DownloadError(f"channel banner overlay produced no output file ({output_path.name})")
    return output_path


async def _compose_channel_banner_image(
    tmpdir: Path,
    vid_w: int,
    vid_h: int,
    avatar_path: Path | None,
    channel_title: str,
    caption_text: str | None,
) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    banner_h = int(vid_h * 0.18)
    img = Image.new("RGBA", (vid_w, banner_h), (0, 0, 0, 180))
    draw = ImageDraw.Draw(img)

    font_large = None
    font_small = None
    for size in (32, 28, 24):
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
            break
        except (OSError, IOError):
            continue
    if font_large is None:
        font_large = ImageFont.load_default()

    for size in (22, 18, 16):
        try:
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
            break
        except (OSError, IOError):
            continue
    if font_small is None:
        font_small = ImageFont.load_default()

    x_offset = 20
    if avatar_path and avatar_path.is_file():
        try:
            av = Image.open(avatar_path).convert("RGBA")
            av_size = int(banner_h * 0.7)
            av = av.resize((av_size, av_size), Image.LANCZOS)

            mask = Image.new("L", (av_size, av_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, av_size, av_size), fill=255)

            av_x = 20
            av_y = (banner_h - av_size) // 2
            img.paste(av, (av_x, av_y), mask)
            x_offset = av_x + av_size + 20
        except Exception as exc:
            LOGGER.warning("Could not process avatar: %s", exc)

    y_pos = banner_h // 2 - 20
    if channel_title:
        draw.text((x_offset, y_pos), channel_title, fill="white", font=font_large)
        y_pos += font_large.size + 4

    if caption_text:
        display_text = caption_text[:100]
        draw.text((x_offset, y_pos), display_text, fill=(220, 220, 220), font=font_small)

    out = tmpdir / "channel_banner.png"
    img.save(out)
    return out


async def render_edit(
    input_path: Path,
    output_path: Path,
    caption_text: str | None = None,
    caption_color: str = "white",
    caption_style: str = "basic",
    caption_position: str = "bottom",
    auto_captions: bool = True,
    voice_text: str | None = None,
    voice: str = "default",
    voice_quality: str = "basic",
    voice_speed: float = 1.0,
    tts_engine: str | None = None,
    banner_path: Path | None = None,
    banner_position: str = "bottom",
    banner_scale: str = "fill",
    watermark_removal: bool = False,
    watermark_position: str = "auto",
    channel_banner: bool = False,
    source_url: str | None = None,
    timeout_seconds: int = 600,
    progress_callback: ProgressCallback | None = None,
) -> tuple[Path, str | None]:
    current = input_path
    intermediate = output_path.with_suffix(".intermediate" + output_path.suffix)
    step_idx = 0
    advance = getattr(progress_callback, "set_step", None)

    if watermark_removal:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_wm{output_path.suffix}")
        if advance:
            advance(step_idx)
        current = await remove_watermark(current, tmp, watermark_position, timeout_seconds, progress_callback=progress_callback)
        if progress_callback:
            await progress_callback(100)
        step_idx += 1

    subtitles_result: str | None = None
    if caption_text or auto_captions:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_cap{output_path.suffix}")
        if advance:
            advance(step_idx)
        current = await render_captions(
            current, tmp,
            caption_text, caption_color, caption_style, caption_position, auto_captions, timeout_seconds,
            srt_output_path=output_path.with_suffix(".srt"),
            progress_callback=progress_callback,
        )
        if auto_captions:
            srt_path = output_path.with_suffix(".srt")
            if srt_path.is_file():
                subtitles_result = str(srt_path)
        if progress_callback:
            await progress_callback(100)
        step_idx += 1

    if voice_text:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_voice{output_path.suffix}")
        if advance:
            advance(step_idx)
        current = await render_voice_over(
            current, tmp, voice_text, tts_engine, voice, voice_quality, voice_speed, timeout_seconds,
            progress_callback=progress_callback,
        )
        step_idx += 1

    if channel_banner and source_url:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_chan{output_path.suffix}")
        if advance:
            advance(step_idx)
        current = await render_channel_banner(
            current, tmp, source_url, caption_text, timeout_seconds,
            progress_callback=progress_callback,
        )
        step_idx += 1

    if banner_path:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_banner{output_path.suffix}")
        if advance:
            advance(step_idx)
        current = await render_banner(
            current, tmp, banner_path, banner_position, banner_scale, timeout_seconds,
            progress_callback=progress_callback,
        )

    if current != output_path:
        shutil.move(str(current), str(output_path))

    intermediate.unlink(missing_ok=True)
    return output_path, subtitles_result
