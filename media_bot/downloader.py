from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


class DownloadError(RuntimeError):
    pass


async def download_media(ytdlp: Path, url: str, max_filesize_mb: int, timeout_seconds: int) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="media-bot-")
    directory = Path(temporary.name)
    command = [
        str(ytdlp), "--no-playlist", "--no-config", "--restrict-filenames", "--max-filesize", f"{max_filesize_mb}M",
        "--socket-timeout", "30", "--retries", "2", "--output", str(directory / "%(title).120B-%(id)s.%(ext)s"),
        "--print", "after_move:filepath", "--", url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (OSError, asyncio.TimeoutError) as exc:
        temporary.cleanup()
        raise DownloadError("downloader failed or timed out") from exc
    if process.returncode != 0:
        temporary.cleanup()
        detail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown downloader error"]
        raise DownloadError(detail[0][:400])
    paths = [Path(line) for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    result = next((path for path in reversed(paths) if path.is_file() and path.parent == directory), None)
    if result is None:
        temporary.cleanup()
        raise DownloadError("downloader produced no uploadable file")
    return temporary, result
