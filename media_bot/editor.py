from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

from .downloader import (
    DownloadError,
    _create_subprocess_exec,
    _run_checked,
    _terminate_process,
)
from .tools import prefer_ffmpeg_full

prefer_ffmpeg_full()

ProgressCallback = Callable[[int], Awaitable[None]]

LOGGER = logging.getLogger(__name__)

_VOICE_PRESETS = {
    "basic": {"ar": "22050", "ac": "1", "codec": "aac"},
    "premium": {"ar": "44100", "ac": "2", "codec": "aac"},
}

_CHANNEL_BANNER_HEIGHT_RATIO = .15

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
        if result.returncode == 0:
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
    raise DownloadError("ffmpeg has no supported video encoder available")

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
        model_name = os.getenv("WHISPER_MODEL", "base.en").strip() or "base.en"
        LOGGER.info("Loading faster-whisper %s model in thread...", model_name)
        loop = asyncio.get_running_loop()
        _WHISPER_MODEL = await loop.run_in_executor(
            None, lambda: WhisperModel(model_name, device="cpu", compute_type="int8"),
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
    await _run_checked(cmd, timeout, "say TTS failed or timed out")


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
    # Neural voice names are Edge-specific. Preserve the language when auto
    # mode falls back to eSpeak instead of passing an invalid voice name.
    if engine == "espeak-ng" and voice and voice.endswith("Neural"):
        locale = voice.split("-", 2)
        return "-".join(locale[:2]).lower() if len(locale) >= 2 else "en"
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

    process: asyncio.subprocess.Process | None = None
    readers: tuple[asyncio.Task[None], ...] = ()
    try:
        process = await _create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise DownloadError(f"{label}: process failed") from exc

    last_pct = -1
    stderr_lines: deque[bytes] = deque(maxlen=2000)

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
                    try:
                        await progress_callback(pct)
                    except Exception as exc:
                        # Progress reporting is cosmetic and must never stop pipe
                        # draining or deadlock a long-running FFmpeg process.
                        LOGGER.warning("Could not report %s progress: %s", label, exc)

    readers = (
        asyncio.create_task(_read_progress()),
        asyncio.create_task(_read_stderr()),
    )

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
    except asyncio.CancelledError:
        for reader in readers:
            reader.cancel()
        await _terminate_process(process)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        for reader in readers:
            reader.cancel()
        await _terminate_process(process)
        await asyncio.gather(*readers, return_exceptions=True)
        raise DownloadError(f"{label} timed out (>{timeout}s)") from exc

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
    model = WhisperModel(os.environ.get("WHISPER_MODEL", "base.en"), device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        tmp.name, beam_size=5, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=True,
        language=os.environ.get("WHISPER_LANGUAGE") or None,
    )
    result = [{
        "start": round(s.start, 3),
        "end": round(s.end, 3),
        "text": s.text.strip(),
        "words": [
            {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
            for w in (s.words or [])
        ],
    } for s in segments]
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
    language_env = (
        f"WHISPER_LANGUAGE={shlex.quote(language)} " if language else ""
    )
    # OpenSSH sends the remote command through a shell. Pass one fully quoted
    # command string so the multiline Python program remains one ``-c`` value.
    ssh_args.append(
        f"{language_env}python3 -c {shlex.quote(_SSH_WHISPER_SCRIPT)}"
    )
    LOGGER.info("Transcribing via SSH to %s...", ssh_target)
    try:
        with wav_path.open("rb") as wav_stream:
            proc = await _create_subprocess_exec(
                *ssh_args,
                stdin=wav_stream,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.CancelledError:
                await _terminate_process(proc)
                raise
            except asyncio.TimeoutError as exc:
                await _terminate_process(proc)
                raise DownloadError(
                    f"SSH whisper timed out on {ssh_target}"
                ) from exc
    except OSError as exc:
        raise DownloadError(f"Could not start SSH whisper on {ssh_target}") from exc
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace")[:300]
        raise DownloadError(f"SSH whisper failed on {ssh_target}: {err}")
    try:
        result = json.loads(stdout.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"SSH whisper returned invalid JSON from {ssh_target}") from exc
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

    # FFmpeg reports a fairly opaque mapping error when a perfectly valid video
    # has no audio stream. Treat that as "no speech" so automatic captions can
    # remain enabled for silent/video-only inputs.
    if shutil.which("ffprobe") is not None:
        try:
            stdout, _ = await _run_checked(
                [
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=index", "-of", "csv=p=0",
                    str(input_path),
                ],
                min(timeout_seconds, 30),
                f"audio stream probe failed for {input_path.name}",
            )
        except DownloadError:
            # Let the extraction command provide the definitive error for
            # malformed media or an ffprobe/ffmpeg capability mismatch.
            LOGGER.debug("Could not probe audio stream for %s", input_path.name)
        else:
            if not stdout.strip():
                LOGGER.info("No audio stream found in %s; skipping transcription", input_path.name)
                return []

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
                lambda: model.transcribe(
                    str(wav_path),
                    beam_size=5,
                    language=language,
                    word_timestamps=True,
                    vad_filter=True,
                    condition_on_previous_text=True,
                ),
            )
            segments = await loop.run_in_executor(None, lambda: list(segments_gen))
        result = [{
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [
                {"start": round(word.start, 3), "end": round(word.end, 3), "word": word.word.strip()}
                for word in (seg.words or [])
            ],
        } for seg in segments]
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


def _normalize_caption_text(value: str) -> str:
    """Normalize Whisper punctuation without changing the spoken wording."""
    text = unicodedata.normalize("NFKC", value).translate(str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201a": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u2026": "...",
    }))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)
    return text


def _caption_chunks(segments: list[dict], words_per_caption: int = 2) -> list[dict]:
    """Turn Whisper segments into short, accurately timed caption beats."""
    chunks: list[dict] = []
    for segment in segments:
        timed_words: list[dict] = []
        for raw_word in segment.get("words", []):
            if (
                not raw_word.get("word")
                or raw_word.get("start") is None
                or raw_word.get("end") is None
            ):
                continue
            word = dict(raw_word)
            word["word"] = _normalize_caption_text(str(word["word"]))
            if not word["word"]:
                continue
            # Whisper occasionally emits punctuation as its own timed token.
            # Attach closing punctuation to the prior word so it neither gains
            # a visual space nor consumes one of the words in a short beat.
            if re.fullmatch(r"[,.;:!?%)+\]}]+", word["word"]) and timed_words:
                timed_words[-1]["word"] = _normalize_caption_text(
                    f"{timed_words[-1]['word']}{word['word']}"
                )
                timed_words[-1]["end"] = word["end"]
            else:
                timed_words.append(word)
        if timed_words:
            for offset in range(0, len(timed_words), words_per_caption):
                group = timed_words[offset:offset + words_per_caption]
                chunks.append({
                    "start": float(group[0]["start"]),
                    "end": float(group[-1]["end"]),
                    "text": _normalize_caption_text(
                        " ".join(str(word["word"]).strip() for word in group)
                    ),
                })
            continue

        words = _normalize_caption_text(str(segment.get("text", ""))).split()
        if not words:
            continue
        start = float(segment["start"])
        end = max(start, float(segment["end"]))
        duration = end - start
        groups = [words[offset:offset + words_per_caption] for offset in range(0, len(words), words_per_caption)]
        weights = [max(1, sum(len(word) for word in group)) for group in groups]
        total_weight = sum(weights)
        cursor = start
        for index, (group, weight) in enumerate(zip(groups, weights)):
            chunk_end = end if index == len(groups) - 1 else cursor + duration * weight / total_weight
            chunks.append({
                "start": cursor,
                "end": chunk_end,
                "text": _normalize_caption_text(" ".join(group)),
            })
            cursor = chunk_end
    return chunks


def _segments_to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(_caption_chunks(segments), 1):
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


def _ass_timestamp(seconds: float) -> str:
    """Format an ASS timestamp using its required centisecond precision."""
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


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
    bottom_safe_area: float = 0.0,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for caption rendering ({input_path.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for caption rendering")

    if auto_captions or not caption_text:
        segments = await transcribe_audio(input_path, timeout_seconds=timeout_seconds)
        if not segments:
            # Silence and music-only clips are valid inputs. Auto captions have
            # nothing to add, so preserve the video and let later edit steps run.
            LOGGER.info("No speech segments found in %s; skipping captions", input_path.name)
            if output_path != input_path:
                await asyncio.to_thread(shutil.copy2, input_path, output_path)
            if progress_callback:
                await progress_callback(100)
            return output_path
        srt_content = _segments_to_srt(segments)
        tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-srt-")
        srt_path = Path(tmpdir.name) / "captions.srt"
        srt_path.write_text(srt_content, encoding="utf-8")
        if srt_output_path:
            srt_output_path.write_text(srt_content, encoding="utf-8")

        video_width, video_height = _get_video_dimensions(input_path)
        ass_style = _build_ass_style(
            color, style, position, video_height=video_height,
            bottom_safe_area=bottom_safe_area,
        )
        position_override = _caption_position_override(
            position, video_width, video_height,
        )
        ass_header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 0\n"
            "PlayDepth: 0\n"
            f"PlayResX: {video_width}\n"
            f"PlayResY: {video_height}\n"
            "[V4+ Styles]\n"
            f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{ass_style['fontsize']},{ass_style['color']},&H00000000,&H00000000,{ass_style['backcolour']},{ass_style['bold']},0,0,0,100,100,0,0,{ass_style['borderstyle']},{ass_style['outline']},{ass_style['shadow']},{ass_style['alignment']},10,10,{ass_style['margin_v']},1\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        for seg in _caption_chunks(segments):
            start = _ass_timestamp(seg["start"])
            end = _ass_timestamp(seg["end"])
            safe_text = (
                seg["text"].replace("\\", "\\\\")
                .replace("{", "\\{").replace("}", "\\}")
                .replace("\n", " ")
            )
            ass_header += (
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,"
                f"{position_override}{safe_text}\n"
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
                progress_callback=(
                    (lambda p: progress_callback(50 + p // 2))
                    if progress_callback else None
                ),
            )
        finally:
            tmpdir.cleanup()
    else:
        if not caption_text:
            raise DownloadError("caption_text is required when auto_captions is off")
        from .colors import resolve_drawtext_color
        font_color = resolve_drawtext_color(color, "white")
        if style == "small":
            fontsize, borderw, shadow = "12", "2", "1"
        elif style == "bold":
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
        position_key = position.lower()
        if len(position_key) == 3 and position_key.startswith("y") and position_key[1:].isdigit():
            y_expr = f"h*{int(position_key[1:]) / 100:.2f}-text_h/2"
        else:
            y_expr = position_map.get(position_key, "h-text_h-20")
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


def _build_ass_style(
    color: str,
    style: str,
    position: str,
    video_height: int = 720,
    bottom_safe_area: float = 0.0,
) -> dict:
    from .colors import resolve_ass_color
    style_map = {
        "small": {"fontsize": 12, "bold": 1, "borderstyle": 1, "outline": 2, "shadow": 1},
        "bold": {"fontsize": 24, "bold": 1, "borderstyle": 1, "outline": 3, "shadow": 2},
        "bubble": {"fontsize": 20, "bold": 0, "borderstyle": 3, "outline": 4, "shadow": 3},
        "border": {"fontsize": 18, "bold": 0, "borderstyle": 1, "outline": 3, "shadow": 1},
        "filled": {"fontsize": 18, "bold": 0, "borderstyle": 3, "outline": 1, "shadow": 0, "backcolour": "&H80000000"},
    }
    default = {"fontsize": 18, "bold": 0, "borderstyle": 1, "outline": 2, "shadow": 1}
    s = dict(style_map.get(style.lower(), default))
    s["fontsize"] = max(12, round(s["fontsize"] * max(.8, video_height / 720)))
    s.setdefault("backcolour", "&H00000000")
    align_map = {"low": 8, "middle": 5, "high": 2}
    position_key = position.lower()
    is_exact = len(position_key) == 3 and position_key.startswith("y") and position_key[1:].isdigit()
    s["alignment"] = 5 if is_exact else align_map.get(position_key, 2)
    s["margin_v"] = (
        max(10, round(video_height * bottom_safe_area) + 10)
        if s["alignment"] in (1, 2, 3) else 10
    )
    s["color"] = resolve_ass_color(color)
    return s


def _caption_position_override(position: str, width: int, height: int) -> str:
    """Return an ASS override for an exact percentage-based caption centerline."""
    key = position.lower()
    if len(key) != 3 or not key.startswith("y") or not key[1:].isdigit():
        return ""
    percent = max(0, min(100, int(key[1:])))
    return f"{{\\an5\\pos({width // 2},{round(height * percent / 100)})}}"


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
    # macOS `say` writes AIFF by default and rejects a `.wav` destination unless
    # a matching data format is supplied. FFmpeg accepts AIFF directly.
    audio_suffix = ".aiff" if engine == "say" else ".wav"
    tmp_audio = Path(tmpdir.name) / f"tts-output{audio_suffix}"

    tts_func = _TTS_ENGINES.get(engine)
    if tts_func is None:
        raise DownloadError(f"unsupported TTS engine: {engine}")

    try:
        if progress_callback:
            await progress_callback(10)
        try:
            await tts_func(voice_text, tmp_audio, voice, speed, timeout_seconds)
        except Exception as exc:
            # Auto mode should remain usable when the network-backed Edge TTS
            # service is unavailable on the bot host.
            if (tts_engine in (None, "auto") and engine == "edge-tts"
                    and shutil.which("espeak-ng")):
                LOGGER.warning("Edge TTS failed; falling back to eSpeak NG: %s", exc)
                engine = "espeak-ng"
                tmp_audio.unlink(missing_ok=True)
                await _tts_espeak(voice_text, tmp_audio, voice, speed, timeout_seconds)
            else:
                raise
        if progress_callback:
            await progress_callback(30)

        if not tmp_audio.is_file() or tmp_audio.stat().st_size == 0:
            raise DownloadError(f"TTS engine ({engine}) produced no audio")

        if progress_callback:
            await progress_callback(50)
        await _run_checked(
            [
                "ffmpeg", "-y",
                "-i", str(input_video),
                "-i", str(tmp_audio),
                "-filter_complex",
                f"[1:a]aresample={preset['ar']}:filter_type=kaiser,apad[aout]",
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
    candidates: list[dict] | None = None,
    tools_dir: Path | None = None,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for watermark removal ({input_path.name})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for watermark removal")

    if position == "auto" and candidates:
        from .diagnostics import append_event
        from .watermark import WatermarkCandidate, inpaint_video, provision_lama_model
        selected = [WatermarkCandidate(**item) for item in candidates]
        started = time.monotonic()
        cancel_event = threading.Event()
        try:
            provision_worker = asyncio.create_task(asyncio.to_thread(
                provision_lama_model,
                tools_dir or Path("runtime/tools"),
                cancel_event=cancel_event,
            ))
            try:
                model = await asyncio.shield(provision_worker)
            except asyncio.CancelledError:
                cancel_event.set()
                await asyncio.gather(provision_worker, return_exceptions=True)
                if output_path != input_path:
                    output_path.unlink(missing_ok=True)
                raise

            async def _inpaint_worker() -> tuple[str, float] | None:
                try:
                    return await asyncio.to_thread(
                        inpaint_video,
                        input_path,
                        output_path,
                        selected,
                        model,
                        timeout_seconds,
                        cancel_event,
                    )
                except Exception:
                    # Once cancellation is requested, the caller is already
                    # joining this worker; consume its cooperative stop error.
                    if cancel_event.is_set():
                        return None
                    raise

            worker = asyncio.create_task(_inpaint_worker())
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # to_thread cannot be force-cancelled. Ask the frame loop/remux
                # to stop, then join it so no worker can outlive its edit job.
                cancel_event.set()
                await asyncio.gather(worker, return_exceptions=True)
                if output_path != input_path:
                    output_path.unlink(missing_ok=True)
                raise
            if result is None:
                raise DownloadError("LaMa watermark removal stopped unexpectedly")
            backend, duration = result
            append_event("watermark_removal", "AI watermark removal completed",
                         confidence=max((item.confidence for item in selected), default=0),
                         masks=[item.box for item in selected], inference_backend=backend,
                         fallback_used=False, duration_seconds=round(duration, 3))
            return output_path
        except Exception as exc:
            LOGGER.warning("LaMa inference unavailable; using adaptive delogo: %s", exc)
            if progress_callback is not None:
                setattr(progress_callback, "watermark_fallback_used", True)
            append_event("watermark_removal", "Adaptive delogo fallback used",
                         confidence=max((item.confidence for item in selected), default=0),
                         masks=[item.box for item in selected], inference_backend=None,
                         fallback_used=True, error=str(exc),
                         duration_seconds=round(time.monotonic() - started, 3))
            return await _remove_watermark_regions(
                input_path, output_path, selected,
                timeout_seconds, progress_callback,
            )

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


async def _remove_watermark_regions(
    input_path: Path,
    output_path: Path,
    regions: list,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None,
) -> Path:
    """Fallback for irregular masks: use one tightly bounded delogo per candidate."""
    vid_w, vid_h = _get_video_dimensions(input_path)
    filters = []
    for region in regions:
        if hasattr(region, "box"):
            x, y, width, height = region.box
            start_seconds = getattr(region, "start_seconds", None)
            end_seconds = getattr(region, "end_seconds", None)
            active_ranges = getattr(region, "active_ranges", ())
        else:
            x, y, width, height = region
            start_seconds = end_seconds = None
            active_ranges = ()
        x = max(1, min(int(x), vid_w - 4))
        y = max(1, min(int(y), vid_h - 4))
        width = max(2, min(int(width), vid_w - x - 2))
        height = max(2, min(int(height), vid_h - y - 2))
        enable = ""
        if active_ranges:
            expressions = [
                f"between(t\\,{float(start):.3f}\\,{float(end):.3f})"
                for start, end in active_ranges
            ]
            enable = f":enable='{'+'.join(expressions)}'"
        elif start_seconds is not None or end_seconds is not None:
            start = max(0.0, float(start_seconds or 0.0))
            end = float(end_seconds if end_seconds is not None else 86400.0)
            enable = f":enable='between(t\\,{start:.3f}\\,{end:.3f})'"
        filters.append(
            f"delogo=x={x}:y={y}:w={width}:h={height}:show=0{enable}"
        )
    if not filters:
        return input_path
    filters.append("format=yuv420p")
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path), "-vf", ",".join(filters),
        "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", str(output_path),
    ]
    await _run_ffmpeg_with_progress(
        cmd, timeout_seconds, f"watermark fallback failed for {input_path.name}",
        total_duration_us=_get_duration_us(input_path), progress_callback=progress_callback,
    )
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
    avatar_path: Path | None = None
    channel_title = ""
    tmpdir = tempfile.TemporaryDirectory(prefix="media-bot-channel-")
    try:
        try:
            metadata_timeout = min(30, max(5, timeout_seconds))
            channel_title, avatar_bytes = await asyncio.wait_for(
                asyncio.to_thread(_fetch_channel_identity, source_url),
                timeout=metadata_timeout,
            )
            if avatar_bytes:
                avatar_path = Path(tmpdir.name) / "avatar.img"
                avatar_path.write_bytes(avatar_bytes)
        except Exception as exc:
            LOGGER.warning("Could not fetch channel info: %s", exc)

        banner_img_path = await _compose_channel_banner_image(
            Path(tmpdir.name), vid_w, vid_h, avatar_path, channel_title, caption_text,
        )

        duration_us = _get_duration_us(input_path)
        banner_height = int(vid_h * _CHANNEL_BANNER_HEIGHT_RATIO)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-i", str(banner_img_path),
            "-filter_complex",
            f"[1:v]scale={vid_w}:{banner_height}[b];"
            f"[0:v][b]overlay=0:{vid_h - banner_height}",
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


def _fetch_channel_identity(source_url: str) -> tuple[str, bytes | None]:
    """Fetch channel text/avatar with bounded network operations."""
    import urllib.request
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
        "retries": 1,
        "extractor_retries": 1,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(source_url, download=False)
    channel_title = info.get("channel", info.get("uploader", info.get("creator", ""))) or ""
    avatar_url = info.get("channel_url") or info.get("uploader_url") or ""
    thumbnails = []
    if avatar_url:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                channel_info = ydl.extract_info(avatar_url, download=False)
            thumbnails = channel_info.get("thumbnails", [])
        except Exception:
            pass
    if not thumbnails:
        thumbnails = info.get("thumbnails", [])
    image_url = thumbnails[-1].get("url", "") if thumbnails else ""
    if not image_url:
        return channel_title, None
    with urllib.request.urlopen(image_url, timeout=10) as response:
        avatar = response.read(10 * 1024 * 1024 + 1)
    if len(avatar) > 10 * 1024 * 1024:
        raise DownloadError("channel avatar exceeds 10 MB")
    return channel_title, avatar


async def _compose_channel_banner_image(
    tmpdir: Path,
    vid_w: int,
    vid_h: int,
    avatar_path: Path | None,
    channel_title: str,
    caption_text: str | None,
) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    banner_h = int(vid_h * _CHANNEL_BANNER_HEIGHT_RATIO)
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


async def overlay_replacement_watermark(
    input_path: Path,
    output_path: Path,
    text: str,
    candidates: list[dict] | None,
    position: str,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    width, height = _get_video_dimensions(input_path)
    regions = [
        (int(item["x"]), int(item["y"]), int(item["width"]), int(item["height"]))
        for item in (candidates or [])
    ]
    if not regions:
        region_width = max(1, int(width * _WATERMARK_REGION_RATIO))
        region_height = max(1, int(height * _WATERMARK_REGION_RATIO * .6))
        positions = {
            "top-left": (1, 1),
            "top-right": (width - region_width - 1, 1),
            "bottom-left": (1, height - region_height - 1),
            "bottom-right": (width - region_width - 1, height - region_height - 1),
            "center": ((width - region_width) // 2, (height - region_height) // 2),
        }
        x, y = positions.get(position, positions["top-right"])
        regions = [(x, y, region_width, region_height)]
    safe_text = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("%", "\\%")
    )
    filters = []
    for x, y, region_width, region_height in regions:
        height_size = region_height * .42
        width_size = region_width / max(1.0, len(text) * .62)
        font_size = max(10, round(min(height_size, width_size)))
        border = max(2, round(font_size * .08))
        padding = max(4, round(font_size * .22))
        filters.append(
            f"drawtext=text='{safe_text}':fontcolor=white:fontsize={font_size}:"
            f"borderw={border}:bordercolor=black@0.9:"
            f"box=1:boxcolor=black@0.58:boxborderw={padding}:"
            f"x={x}+({region_width}-text_w)/2:"
            f"y={y}+({region_height}-text_h)/2"
        )
    filters.append("format=yuv420p")
    command = [
        "ffmpeg", "-y", "-i", str(input_path), "-vf", ",".join(filters),
        "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
        "-c:a", "copy", "-movflags", "+faststart", str(output_path),
    ]
    await _run_ffmpeg_with_progress(
        command,
        timeout_seconds,
        f"replacement watermark overlay failed for {input_path.name}",
        total_duration_us=_get_duration_us(input_path),
        progress_callback=progress_callback,
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise DownloadError("replacement watermark overlay produced no output")
    return output_path


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
    watermark_candidates: list[dict] | None = None,
    tools_dir: Path | None = None,
    watermark_mode: str = "keep",
    watermark_text: str | None = None,
) -> tuple[Path, str | None]:
    current = input_path
    step_idx = 0
    advance = getattr(progress_callback, "set_step", None)
    intermediates: set[Path] = set()
    staged_srt: Path | None = None
    final_srt = output_path.with_suffix(".srt")

    def _stage(label: str) -> Path:
        path = output_path.with_name(
            f".{output_path.stem}_{label}{output_path.suffix}"
        )
        intermediates.add(path)
        return path

    try:
        if watermark_removal:
            tmp = _stage("wm")
            if advance:
                advance(step_idx)
            current = await remove_watermark(
                current, tmp, watermark_position, timeout_seconds,
                progress_callback=progress_callback, candidates=watermark_candidates,
                tools_dir=tools_dir,
            )
            if progress_callback:
                await progress_callback(100)
            step_idx += 1
            if watermark_mode == "swap":
                if not watermark_text or not watermark_text.strip():
                    raise DownloadError("replacement watermark text is required in Swap mode")
                current = await overlay_replacement_watermark(
                    current,
                    _stage("swap"),
                    watermark_text.strip(),
                    watermark_candidates,
                    watermark_position,
                    timeout_seconds,
                    progress_callback=progress_callback,
                )

        if caption_text or auto_captions:
            tmp = _stage("cap")
            if auto_captions:
                staged_srt = output_path.with_name(f".{output_path.stem}_captions.srt")
                intermediates.add(staged_srt)
            if advance:
                advance(step_idx)
            current = await render_captions(
                current, tmp,
                caption_text, caption_color, caption_style, caption_position,
                auto_captions, timeout_seconds,
                srt_output_path=staged_srt,
                progress_callback=progress_callback,
                bottom_safe_area=(
                    .17
                    if channel_banner or (banner_path and banner_position == "bottom")
                    else 0.0
                ),
            )
            if progress_callback:
                await progress_callback(100)
            step_idx += 1

        if voice_text:
            tmp = _stage("voice")
            if advance:
                advance(step_idx)
            current = await render_voice_over(
                current, tmp, voice_text, tts_engine, voice, voice_quality,
                voice_speed, timeout_seconds,
                progress_callback=progress_callback,
            )
            step_idx += 1

        if channel_banner and source_url:
            tmp = _stage("chan")
            if advance:
                advance(step_idx)
            current = await render_channel_banner(
                current, tmp, source_url, caption_text, timeout_seconds,
                progress_callback=progress_callback,
            )
            step_idx += 1

        if banner_path:
            tmp = _stage("banner")
            if advance:
                advance(step_idx)
            current = await render_banner(
                current, tmp, banner_path, banner_position, banner_scale,
                timeout_seconds, progress_callback=progress_callback,
            )

        if current == input_path:
            # Preserve the staged source when editing is a no-op. Copy to a
            # sibling first so replacing an existing output remains atomic.
            passthrough = _stage("passthrough")
            await asyncio.to_thread(shutil.copy2, input_path, passthrough)
            current = passthrough

        if current != output_path:
            await asyncio.to_thread(os.replace, current, output_path)

        subtitles_result: str | None = None
        if staged_srt is not None and staged_srt.is_file():
            await asyncio.to_thread(os.replace, staged_srt, final_srt)
            subtitles_result = str(final_srt)
        return output_path, subtitles_result
    finally:
        # Every edit stage writes next to the destination. Always remove stale
        # products, including partially written files on failure/cancellation.
        for path in intermediates:
            if path not in (input_path, output_path, final_srt):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("Could not remove edit intermediate %s: %s", path, exc)
