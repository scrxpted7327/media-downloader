from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from .downloader import DownloadError, _run_checked

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

def _get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        LOGGER.info("Loading faster-whisper tiny model...")
        _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
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
    rate_str = f"+{int((speed - 1.0) * 100)}%" if speed >= 1.0 else f"{int((1.0 - speed) * 100)}%"
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


async def transcribe_audio(
    input_path: Path,
    language: str | None = None,
    timeout_seconds: int = 1800,
) -> list[dict]:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for transcription")

    model = _get_whisper_model()
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

        LOGGER.info("Transcribing %s...", input_path.name)
        segments, info = model.transcribe(
            str(wav_path), beam_size=1, language=language,
        )
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

        ass_style = _build_ass_style(color, style, position)
        ass_header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 0\n"
            "PlayDepth: 0\n"
            "[V4+ Styles]\n"
            f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Arial,{ass_style['fontsize']},&H00{ass_style['color']},&H00000000,&H00000000,&H00000000,{ass_style['bold']},0,0,0,100,100,0,0,{ass_style['borderstyle']},{ass_style['outline']},{ass_style['shadow']},{ass_style['alignment']},10,10,10,1\n"
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
            await _run_checked(
                ["ffmpeg", "-y", "-i", str(input_path),
                 "-vf", f"ass={str(ass_path)}",
                 "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
                 "-c:a", "copy", "-movflags", "+faststart", str(output_path)],
                timeout_seconds,
                f"closed caption burn failed for {input_path.name}",
            )
        finally:
            tmpdir.cleanup()
    else:
        if not caption_text:
            raise DownloadError("caption_text is required when auto_captions is off")
        color_map = {
            "white": "white", "black": "black", "yellow": "yellow",
            "red": "red", "blue": "blue", "green": "green",
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
        safe_text = caption_text.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
        drawtext = (
            f"drawtext=text='{safe_text}':fontcolor={font_color}:fontsize={fontsize}:"
            f"borderw={borderw}:bordercolor=black:shadowx=2:shadowy={shadow}:"
            f"x=(w-text_w)/2:y={y_expr}:enable='between(t,0,t+86400)'"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path), "-vf", drawtext,
            "-c:v", _detect_video_encoder(), "-preset", "fast", "-crf", "23",
            "-c:a", "copy", "-movflags", "+faststart", str(output_path),
        ]
        try:
            await _run_checked(cmd, timeout_seconds, f"caption render failed for {input_path.name}")
        except DownloadError as exc:
            raise DownloadError(f"Caption render failed (text={caption_text[:50]}): {exc}") from exc

    if not output_path.is_file():
        raise DownloadError(f"caption rendering produced no output file ({output_path.name})")
    return output_path


def _build_ass_style(color: str, style: str, position: str) -> dict:
    color_hex = {
        "white": "FFFFFF", "black": "000000", "yellow": "FFFF00",
        "red": "0000FF", "blue": "FF0000", "green": "00FF00",
    }
    style_map = {
        "bold": {"fontsize": 24, "bold": 1, "borderstyle": 1, "outline": 3, "shadow": 2},
        "bubble": {"fontsize": 20, "bold": 0, "borderstyle": 3, "outline": 4, "shadow": 3},
    }
    default = {"fontsize": 18, "bold": 0, "borderstyle": 1, "outline": 2, "shadow": 1}
    s = style_map.get(style.lower(), default)
    align_map = {"low": 2, "middle": 8, "high": 8}
    s["alignment"] = align_map.get(position.lower(), 2)
    s["color"] = color_hex.get(color.lower(), "FFFFFF")
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
        await tts_func(voice_text, tmp_audio, voice, speed, timeout_seconds)

        if not tmp_audio.is_file() or tmp_audio.stat().st_size == 0:
            raise DownloadError(f"TTS engine ({engine}) produced no audio")

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
    scale: str = "fit",
    timeout_seconds: int = 600,
) -> Path:
    if not input_path.is_file():
        raise DownloadError(f"input file not found for banner overlay ({input_path.name})")
    if not banner_path.is_file():
        raise DownloadError(f"banner image not found ({banner_path})")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for banner overlay")

    if scale == "stretch":
        overlay = f"scale=iw:{int(1080 * 0.15)}"
    elif scale == "fill":
        overlay = f"scale=max(iw\\,ih):max(iw\\,ih):force_original_aspect_ratio=increase,crop=iw:{int(1080 * 0.15)}"
    else:
        overlay = f"scale=iw:trunc(oh*a/2)*2:force_original_aspect_ratio=decrease,scale=iw:min(ih\\,{int(1080 * 0.15)})"

    position_map = {
        "top": "0:0",
        "bottom": f"0:ih-overlay_h",
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
        await _run_checked(cmd, timeout_seconds, f"banner overlay failed for {input_path.name}")
    except DownloadError as exc:
        raise DownloadError(f"Banner render failed: {exc}") from exc

    if not output_path.is_file():
        raise DownloadError(f"banner overlay produced no output file ({output_path.name})")
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
    banner_scale: str = "fit",
    timeout_seconds: int = 600,
) -> Path:
    current = input_path
    intermediate = output_path.with_suffix(".intermediate" + output_path.suffix)

    if caption_text or auto_captions:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_cap{output_path.suffix}")
        current = await render_captions(
            current, tmp,
            caption_text, caption_color, caption_style, caption_position, auto_captions, timeout_seconds,
        )

    if voice_text:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_voice{output_path.suffix}")
        current = await render_voice_over(
            current, tmp, voice_text, tts_engine, voice, voice_quality, voice_speed, timeout_seconds,
        )

    if banner_path:
        tmp = intermediate if current == input_path else output_path.with_name(f"{output_path.stem}_banner{output_path.suffix}")
        current = await render_banner(
            current, tmp, banner_path, banner_position, banner_scale, timeout_seconds,
        )

    if current != output_path:
        shutil.move(str(current), str(output_path))

    intermediate.unlink(missing_ok=True)
    return output_path
