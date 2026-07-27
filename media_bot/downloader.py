from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from collections.abc import Awaitable, Callable

from .tools import prefer_ffmpeg_full

prefer_ffmpeg_full()


class DownloadError(RuntimeError):
    pass


ProgressCallback = Callable[[int], Awaitable[None]]
_PROGRESS_PATTERN = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_IMAGE_SUFFIXES = {".avif", ".jpeg", ".jpg", ".png", ".webp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv"}
LOGGER = logging.getLogger(__name__)

_VIDEO_ENCODER: str | None = None

def _detect_video_encoder() -> str:
    global _VIDEO_ENCODER
    if _VIDEO_ENCODER is not None:
        return _VIDEO_ENCODER
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=64x64",
             "-c:v", "libx264", "-t", "1", "-f", "null", "-"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0 or b"Unknown encoder" not in result.stderr:
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


def download_progress(line: bytes) -> int | None:
    """Extract an integer percentage from a yt-dlp progress line."""
    match = _PROGRESS_PATTERN.search(line.decode("utf-8", "replace"))
    if match is None:
        return None
    return min(100, max(0, int(float(match.group(1)))))


async def _read_stream(
    stream: asyncio.StreamReader | None,
    output: list[bytes],
    progress_callback: ProgressCallback | None = None,
) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        output.append(line)
        progress = download_progress(line)
        if progress is not None and progress_callback is not None:
            await progress_callback(progress)


async def download_media(
    ytdlp: Path,
    url: str,
    max_filesize_mb: int,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="media-bot-")
    directory = Path(temporary.name)
    process = None
    readers: tuple[asyncio.Task[None], ...] = ()
    command = [
        str(ytdlp), "--no-playlist", "--no-config", "--restrict-filenames", "--max-filesize", f"{max_filesize_mb}M",
        "--socket-timeout", "30", "--retries", "2", "--concurrent-fragments", "4", "--newline",
        "--format", "bestvideo[ext!=webm]+bestaudio/best[ext!=webm]/best",
        "--output", str(directory / "%(title).120B-%(id)s.%(ext)s"),
        "--print", "after_move:filepath", "--", url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout_lines: list[bytes] = []
        stderr_lines: list[bytes] = []
        readers = (
            asyncio.create_task(_read_stream(process.stdout, stdout_lines)),
            asyncio.create_task(_read_stream(process.stderr, stderr_lines, progress_callback)),
        )
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        await asyncio.gather(*readers)
    except (OSError, asyncio.TimeoutError) as exc:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.gather(*readers, return_exceptions=True)
        temporary.cleanup()
        raise DownloadError(
            f"downloader failed or timed out for {url[:100]} (timeout={timeout_seconds}s, max_size={max_filesize_mb}MB)"
        ) from exc
    if process.returncode != 0:
        temporary.cleanup()
        stderr_text = b"".join(stderr_lines).decode("utf-8", "replace").strip()
        detail = stderr_text.splitlines()[-3:] or ["unknown downloader error"]
        raise DownloadError(f"download failed for {url[:100]}: {'; '.join(detail)[:600]}")
    paths = [Path(line) for line in b"".join(stdout_lines).decode("utf-8", "replace").splitlines() if line.strip()]
    result = next((path for path in reversed(paths) if path.is_file() and path.parent == directory), None)
    if result is None:
        temporary.cleanup()
        raise DownloadError("downloader produced no uploadable file")
    if result.suffix.lower() == ".webm":
        LOGGER.info("Only .webm available; converting %s to .mp4", result.name)
        try:
            result = await _convert_to_mp4(result, timeout_seconds)
        except DownloadError:
            temporary.cleanup()
            raise
    return temporary, result


async def _run_checked(command: list[str], timeout_seconds: int, error: str) -> tuple[bytes, bytes]:
    cmd_preview = " ".join(str(c) for c in command[:4]) + " …"
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (OSError, asyncio.TimeoutError) as exc:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        raise DownloadError(f"{error} (timeout={timeout_seconds}s, cmd={cmd_preview})") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:] or [error]
        msg = "; ".join(detail)[:600]
        raise DownloadError(f"{error}: {msg} (cmd={cmd_preview})")
    return stdout, stderr


async def _convert_to_mp4(path: Path, timeout_seconds: int) -> Path:
    """Convert a .webm video file to .mp4 using ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise DownloadError(f"ffmpeg is required to convert {path.name}")
    output = path.with_suffix(".mp4")
    await _run_checked(
        ["ffmpeg", "-y", "-i", str(path),
         "-c:v", _detect_video_encoder(), "-c:a", "aac",
         "-movflags", "+faststart", str(output)],
        timeout_seconds,
        f"failed to convert {path.name} to mp4",
    )
    path.unlink(missing_ok=True)
    return output


async def _media_duration(path: Path, timeout_seconds: int) -> float:
    if shutil.which("ffprobe") is None:
        raise DownloadError(f"ffprobe is required to render TikTok slides ({path.name})")
    stdout, _ = await _run_checked(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        timeout_seconds,
        f"could not read audio duration for {path.name}",
    )
    try:
        duration = float(stdout.decode("utf-8", "replace").strip())
    except ValueError as exc:
        raise DownloadError(f"could not parse audio duration from {path.name}") from exc
    if duration <= 0:
        raise DownloadError(f"audio file {path.name} has no usable duration (got {duration}s)")
    return duration


async def download_tiktok_slideshow(
    gallerydl: Path,
    url: str,
    max_filesize_mb: int,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Download a TikTok post via gallery-dl.

    Photo posts are rendered to an MP4 slideshow (or ZIP if audio/ffmpeg is
    unavailable). Video posts — including yt-dlp fallbacks for short links —
    return the downloaded video file directly.
    """
    if not gallerydl.is_file() or not os.access(gallerydl, os.X_OK):
        raise DownloadError(f"gallery-dl is required for TikTok photo posts ({url[:80]})")

    temporary = tempfile.TemporaryDirectory(prefix="media-bot-tiktok-")
    directory = Path(temporary.name)
    try:
        if progress_callback is not None:
            await progress_callback(0)
        await _run_checked(
            [
                str(gallerydl), "--config-ignore", "--no-colors", "--directory", str(directory),
                "--filename", "{type}_{num:03}.{extension}", url,
            ],
            timeout_seconds,
            f"TikTok slide download failed for {url[:80]}",
        )
        images = sorted(path for path in directory.iterdir() if path.suffix.lower() in _IMAGE_SUFFIXES)
        videos = [path for path in directory.iterdir() if path.suffix.lower() in _VIDEO_SUFFIXES]

        if images:
            audio = next((path for path in directory.iterdir() if path.name.startswith("audio_")), None)
            if audio is not None and shutil.which("ffmpeg") is not None:
                try:
                    return await _render_tiktok_video(directory, images, audio, max_filesize_mb, timeout_seconds, temporary)
                except DownloadError:
                    LOGGER.warning("TikTok video render failed, falling back to ZIP for %s", url[:80])

            LOGGER.info("Packaging TikTok slides as ZIP for %s", url[:80])
            return await _package_tiktok_zip(directory, images, max_filesize_mb, temporary)

        if videos:
            result = max(videos, key=lambda path: path.stat().st_size)
            if result.stat().st_size > max_filesize_mb * 1024 * 1024:
                raise DownloadError(f"TikTok download exceeds the configured {max_filesize_mb} MB size limit")
            if result.suffix.lower() == ".webm":
                LOGGER.info("TikTok gallery-dl returned .webm; converting %s to .mp4", result.name)
                result = await _convert_to_mp4(result, timeout_seconds)
            return temporary, result

        raise DownloadError(f"TikTok post did not contain downloadable media ({url[:80]})")
    except Exception:
        temporary.cleanup()
        raise


async def _render_tiktok_video(
    directory: Path, images: list[Path], audio: Path,
    max_filesize_mb: int, timeout_seconds: int,
    temporary: tempfile.TemporaryDirectory[str],
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    duration = await _media_duration(audio, timeout_seconds)
    per_slide = duration / len(images)
    inputs: list[str] = []
    filters: list[str] = []
    for index, image in enumerate(images):
        inputs.extend(["-loop", "1", "-framerate", "30", "-t", f"{per_slide:.3f}", "-i", str(image)])
        filters.append(
            f"[{index}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih),setsar=1[v{index}]"
        )
    filters.append("".join(f"[v{index}]" for index in range(len(images))) + f"concat=n={len(images)}:v=1:a=0[v]")
    output = directory / "tiktok-slideshow.mp4"
    await _run_checked(
        [
            "ffmpeg", "-y", *inputs, "-i", str(audio), "-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", f"{len(images)}:a:0", "-c:v", _detect_video_encoder(), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(output),
        ],
        timeout_seconds,
        f"TikTok slide video rendering failed for {directory.name}",
    )
    if not output.is_file():
        raise DownloadError("TikTok slide video rendering produced no file")
    if output.stat().st_size > max_filesize_mb * 1024 * 1024:
        raise DownloadError(f"rendered TikTok slide video exceeds {max_filesize_mb}MB limit")
    return temporary, output


async def _package_tiktok_zip(
    directory: Path, images: list[Path], max_filesize_mb: int,
    temporary: tempfile.TemporaryDirectory[str],
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    zip_path = directory / "tiktok-slides.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            zf.write(img, img.name)
    if not zip_path.is_file():
        raise DownloadError("ZIP creation failed")
    if zip_path.stat().st_size > max_filesize_mb * 1024 * 1024:
        raise DownloadError(f"ZIP archive exceeds {max_filesize_mb}MB limit")
    return temporary, zip_path


async def download_instagram(
    gallerydl: Path,
    url: str,
    max_filesize_mb: int,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Download an Instagram Reel/download using gallery-dl, falling back to yt-dlp."""
    if not gallerydl.is_file() or not os.access(gallerydl, os.X_OK):
        raise DownloadError(f"gallery-dl is required for Instagram downloads ({url[:80]})")

    temporary = tempfile.TemporaryDirectory(prefix="media-bot-instagram-")
    directory = Path(temporary.name)
    try:
        if progress_callback is not None:
            await progress_callback(0)
        await _run_checked(
            [
                str(gallerydl), "--config-ignore", "--no-colors", "--directory", str(directory),
                "--filename", "{title}_{num:03}.{extension}", url,
            ],
            timeout_seconds,
            "Instagram download failed or timed out",
        )
        videos = [p for p in directory.iterdir() if p.suffix.lower() in _VIDEO_SUFFIXES]
        if videos:
            result = max(videos, key=lambda p: p.stat().st_size)
        else:
            all_files = sorted(
                path for path in directory.iterdir()
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif"}
            )
            if not all_files:
                raise DownloadError(
                    f"Instagram download produced no media files in {directory}"
                )
            result = max(all_files, key=lambda p: p.stat().st_size)

        if result.stat().st_size > max_filesize_mb * 1024 * 1024:
            raise DownloadError(
                f"Instagram download exceeds the configured {max_filesize_mb} MB size limit"
            )
    except DownloadError as exc:
        temporary.cleanup()
        err_text = str(exc).lower()
        if "redirect to login" in err_text or "login" in err_text or "authentication" in err_text:
            LOGGER.warning("gallery-dl requires login for Instagram, falling back to yt-dlp for %s", url)
            return await _download_instagram_ytdlp(url, max_filesize_mb, timeout_seconds, progress_callback)
        raise
    except Exception as exc:
        temporary.cleanup()
        raise DownloadError(f"Instagram download failed: {exc}") from exc

    if result.suffix.lower() == ".webm":
        LOGGER.info("Instagram returned .webm; converting %s to .mp4", result.name)
        try:
            result = await _convert_to_mp4(result, timeout_seconds)
        except DownloadError:
            temporary.cleanup()
            raise
    return temporary, result


async def _download_instagram_ytdlp(
    url: str,
    max_filesize_mb: int,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Fallback: download Instagram using yt-dlp."""
    ytdlp = Path(sys.executable).with_name("yt-dlp")
    if not ytdlp.is_file():
        ytdlp = Path(shutil.which("yt-dlp") or sys.executable)
    if not ytdlp.is_file() or not os.access(ytdlp, os.X_OK):
        raise DownloadError(f"yt-dlp is required as fallback for Instagram ({url[:80]})")
    return await download_media(ytdlp, url, max_filesize_mb, timeout_seconds, progress_callback)


async def persist_download(temp_path: Path, job_id: int, storage_dir: Path) -> Path:
    """Move a downloaded file to persistent job storage."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / f"{job_id}-{temp_path.name}"
    shutil.move(str(temp_path), str(dest))
    return dest
