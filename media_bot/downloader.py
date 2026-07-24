from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from collections.abc import Awaitable, Callable


class DownloadError(RuntimeError):
    pass


ProgressCallback = Callable[[int], Awaitable[None]]
_PROGRESS_PATTERN = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_IMAGE_SUFFIXES = {".avif", ".jpeg", ".jpg", ".png", ".webp"}


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
        raise DownloadError("downloader failed or timed out") from exc
    if process.returncode != 0:
        temporary.cleanup()
        detail = b"".join(stderr_lines).decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown downloader error"]
        raise DownloadError(detail[0][:400])
    paths = [Path(line) for line in b"".join(stdout_lines).decode("utf-8", "replace").splitlines() if line.strip()]
    result = next((path for path in reversed(paths) if path.is_file() and path.parent == directory), None)
    if result is None:
        temporary.cleanup()
        raise DownloadError("downloader produced no uploadable file")
    return temporary, result


async def _run_checked(command: list[str], timeout_seconds: int, error: str) -> tuple[bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (OSError, asyncio.TimeoutError) as exc:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        raise DownloadError(error) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [error]
        raise DownloadError(detail[0][:400])
    return stdout, stderr


async def _media_duration(path: Path, timeout_seconds: int) -> float:
    if shutil.which("ffprobe") is None:
        raise DownloadError("ffprobe is required to render TikTok slides")
    stdout, _ = await _run_checked(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        timeout_seconds,
        "could not read TikTok audio duration",
    )
    try:
        duration = float(stdout.decode("utf-8", "replace").strip())
    except ValueError as exc:
        raise DownloadError("could not read TikTok audio duration") from exc
    if duration <= 0:
        raise DownloadError("TikTok audio has no usable duration")
    return duration


async def download_tiktok_slideshow(
    gallerydl: Path,
    url: str,
    max_filesize_mb: int,
    timeout_seconds: int,
    progress_callback: ProgressCallback | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Download a TikTok photo post and render its slides with the post audio."""
    if not gallerydl.is_file() or not os.access(gallerydl, os.X_OK):
        raise DownloadError("gallery-dl is required for TikTok photo posts")
    if shutil.which("ffmpeg") is None:
        raise DownloadError("ffmpeg is required for TikTok photo posts")

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
            "TikTok slide download failed or timed out",
        )
        images = sorted(path for path in directory.iterdir() if path.suffix.lower() in _IMAGE_SUFFIXES)
        audio = next((path for path in directory.iterdir() if path.name.startswith("audio_")), None)
        if not images:
            raise DownloadError("TikTok post did not contain downloadable slides")
        if audio is None:
            raise DownloadError("TikTok post did not provide downloadable music")

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
                "-map", "[v]", "-map", f"{len(images)}:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(output),
            ],
            timeout_seconds,
            "TikTok slide video rendering failed or timed out",
        )
        if not output.is_file():
            raise DownloadError("TikTok slide video rendering produced no file")
        if output.stat().st_size > max_filesize_mb * 1024 * 1024:
            raise DownloadError("rendered TikTok slide video exceeds the configured size limit")
        return temporary, output
    except Exception:
        temporary.cleanup()
        raise


async def persist_download(temp_path: Path, job_id: int, storage_dir: Path) -> Path:
    """Move a downloaded file to persistent job storage."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / f"{job_id}-{temp_path.name}"
    temp_path.replace(dest)
    return dest
