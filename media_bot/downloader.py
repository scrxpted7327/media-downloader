from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from collections.abc import Awaitable, Callable
from collections import deque

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
    output: deque[bytes],
    progress_callback: ProgressCallback | None = None,
) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        output.append(line)
        progress = download_progress(line)
        if progress is not None and progress_callback is not None:
            await progress_callback(progress)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess group, escalating to kill after a short grace."""
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


def _enforce_size(path: Path, max_filesize_mb: int, label: str = "download") -> None:
    if path.stat().st_size > max_filesize_mb * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise DownloadError(
            f"{label} exceeds the configured {max_filesize_mb} MB size limit"
        )


async def _monitor_directory_size(path: Path, maximum_bytes: int) -> None:
    """Abort a producer once its working tree crosses a hard byte ceiling."""
    while True:
        total = 0
        try:
            for candidate in path.rglob("*"):
                if candidate.is_file() and not candidate.is_symlink():
                    total += candidate.stat().st_size
                    if total > maximum_bytes:
                        raise DownloadError(
                            "download working data exceeds the configured size limit"
                        )
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.25)


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
        "--write-info-json", "--write-thumbnail",
        "--output", str(directory / "%(title).120B-%(id)s.%(ext)s"),
        "--print", "after_move:filepath", "--", url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        stdout_lines: deque[bytes] = deque(maxlen=2000)
        stderr_lines: deque[bytes] = deque(maxlen=2000)
        readers = (
            asyncio.create_task(_read_stream(process.stdout, stdout_lines)),
            asyncio.create_task(_read_stream(process.stderr, stderr_lines, progress_callback)),
        )
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        await asyncio.gather(*readers)
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process(process)
        await asyncio.gather(*readers, return_exceptions=True)
        temporary.cleanup()
        raise
    except (OSError, asyncio.TimeoutError) as exc:
        if process is not None:
            await _terminate_process(process)
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
    _enforce_size(result, max_filesize_mb)
    return temporary, result


def read_source_metadata(directory: Path) -> tuple[str | None, str | None]:
    """Read title and source caption from a yt-dlp sidecar when available."""
    info_path = next(directory.glob("*.info.json"), None) or next(directory.glob("*.json"), None)
    if info_path is None:
        return None, None
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    title = str(data.get("title") or "").strip() or None
    caption = str(
        data.get("description")
        or data.get("caption")
        or data.get("content")
        or data.get("text")
        or ""
    ).strip() or None
    return title, caption


async def create_thumbnail(media_path: Path, destination: Path) -> Path | None:
    """Create a representative JPEG frame for video media."""
    if media_path.suffix.lower() not in _VIDEO_SUFFIXES or shutil.which("ffmpeg") is None:
        return None
    try:
        await _run_checked(
            [
                "ffmpeg", "-y", "-ss", "1", "-i", str(media_path),
                "-frames:v", "1", "-vf", "scale=640:-2", str(destination),
            ],
            60,
            "thumbnail extraction failed",
        )
    except DownloadError:
        LOGGER.warning("Could not create thumbnail for %s", media_path.name)
        return None
    return destination if destination.is_file() else None


async def _run_checked(
    command: list[str],
    timeout_seconds: int,
    error: str,
    *,
    working_dir_limit: tuple[Path, int] | None = None,
) -> tuple[bytes, bytes]:
    cmd_preview = " ".join(str(c) for c in command[:4]) + " …"
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        stdout_lines: deque[bytes] = deque(maxlen=2000)
        stderr_lines: deque[bytes] = deque(maxlen=2000)
        readers = (
            asyncio.create_task(_read_stream(process.stdout, stdout_lines)),
            asyncio.create_task(_read_stream(process.stderr, stderr_lines)),
        )
        process_wait = asyncio.create_task(
            asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        )
        monitor = (
            asyncio.create_task(_monitor_directory_size(*working_dir_limit))
            if working_dir_limit is not None else None
        )
        if monitor is None:
            await process_wait
        else:
            done, _ = await asyncio.wait(
                (process_wait, monitor), return_when=asyncio.FIRST_COMPLETED,
            )
            if monitor in done:
                await monitor
            await process_wait
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        await asyncio.gather(*readers)
        stdout, stderr = b"".join(stdout_lines), b"".join(stderr_lines)
    except asyncio.CancelledError:
        if "process" in locals():
            await _terminate_process(process)
        if "process_wait" in locals() and not process_wait.done():
            process_wait.cancel()
        if "monitor" in locals() and monitor is not None and not monitor.done():
            monitor.cancel()
        if "readers" in locals():
            await asyncio.gather(*readers, return_exceptions=True)
        auxiliary = [
            task for task in (
                locals().get("process_wait"),
                locals().get("monitor"),
            )
            if task is not None
        ]
        if auxiliary:
            await asyncio.gather(*auxiliary, return_exceptions=True)
        raise
    except (OSError, asyncio.TimeoutError, DownloadError) as exc:
        if "process" in locals():
            await _terminate_process(process)
        if "process_wait" in locals() and not process_wait.done():
            process_wait.cancel()
        if "monitor" in locals() and monitor is not None and not monitor.done():
            monitor.cancel()
        if "readers" in locals():
            await asyncio.gather(*readers, return_exceptions=True)
        if isinstance(exc, DownloadError):
            raise
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
                "--write-metadata", "--filename", "{type}_{num:03}.{extension}", url,
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
            _enforce_size(result, max_filesize_mb, "TikTok download")
            return temporary, result

        raise DownloadError(f"TikTok post did not contain downloadable media ({url[:80]})")
    except Exception:
        temporary.cleanup()
        raise


async def download_tiktok_account(
    gallerydl: Path,
    profile_url: str,
    max_archive_mb: int,
    timeout_seconds: int,
    post_limit: int | None = 50,
) -> tuple[tempfile.TemporaryDirectory[str], Path, int]:
    """Download a TikTok profile and package its media as one ZIP archive."""
    if not gallerydl.is_file() or not os.access(gallerydl, os.X_OK):
        raise DownloadError("gallery-dl is required for TikTok account downloads")
    if post_limit is not None and not (1 <= post_limit <= 500):
        raise DownloadError("TikTok account limit must be between 1 and 500, or 'all'")

    temporary = tempfile.TemporaryDirectory(prefix="media-bot-tiktok-account-")
    root = Path(temporary.name)
    required_free = max_archive_mb * 2 * 1024 * 1024
    if shutil.disk_usage(root).free < required_free:
        temporary.cleanup()
        raise DownloadError(
            f"insufficient disk space for a {max_archive_mb} MB account archive"
        )
    downloads = root / "media"
    downloads.mkdir()
    command = [
        str(gallerydl), "--config-ignore", "--no-colors",
        "--directory", str(downloads), "--write-metadata",
        "--filename", "{id}_{num:03}.{extension}", "--sleep-request", "0.5",
    ]
    if post_limit is not None:
        command.extend(["--post-range", f"1-{post_limit}"])
    command.append(profile_url)
    try:
        await _run_checked(
            command, timeout_seconds,
            f"TikTok account download failed for {profile_url[:80]}",
            working_dir_limit=(downloads, max_archive_mb * 1024 * 1024),
        )
        media = sorted(
            path for path in downloads.rglob("*")
            if path.is_file() and path.suffix.lower() in (
                _IMAGE_SUFFIXES | _VIDEO_SUFFIXES | {".mp3", ".m4a", ".aac", ".wav"}
            )
        )
        if not media:
            raise DownloadError("TikTok account produced no downloadable media")
        total_bytes = sum(path.stat().st_size for path in media)
        if total_bytes > max_archive_mb * 1024 * 1024:
            raise DownloadError(
                f"TikTok account media exceeds the configured {max_archive_mb} MB archive limit"
            )
        account = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile_url.rstrip("/").split("/")[-1]) or "account"
        archive = root / f"tiktok-{account.lstrip('@')}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for path in media:
                bundle.write(path, path.relative_to(downloads))
        if not archive.is_file() or archive.stat().st_size == 0:
            raise DownloadError("TikTok account archive creation failed")
        _enforce_size(archive, max_archive_mb, "TikTok account archive")
        return temporary, archive, len(media)
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
                "--write-metadata", "--filename", "{title}_{num:03}.{extension}", url,
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
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".json"}
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
    _enforce_size(result, max_filesize_mb, "Instagram download")
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
    if temp_path.is_symlink() or not temp_path.is_file():
        raise DownloadError("download result is not a regular file")
    dest = storage_dir / f"{job_id}-{temp_path.name}"
    if dest.exists():
        raise DownloadError(f"persistent destination already exists for job {job_id}")
    shutil.move(str(temp_path), str(dest))
    return dest
