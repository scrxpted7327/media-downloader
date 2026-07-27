from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from .storage import consume_download_token, get_job

LOGGER = logging.getLogger(__name__)

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".zip": "application/zip",
    ".srt": "application/x-subrip",
}


def _content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


async def handle_download(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    if not token:
        return web.Response(status=404, text="Not found")

    db_path: Path = request.app["db_path"]
    storage_dir: Path = request.app["storage_dir"]

    token_data = await consume_download_token(db_path, token)
    if token_data is None:
        return web.Response(status=403, text="Invalid or expired link")

    job = await get_job(db_path, token_data.job_id)
    if job is None or job.file_path is None:
        return web.Response(status=404, text="File not found")

    file_path = Path(job.file_path)
    if not file_path.is_file():
        return web.Response(status=404, text="File not found")

    try:
        file_path.relative_to(storage_dir)
    except ValueError:
        return web.Response(status=403, text="Access denied")

    LOGGER.info("Serving download for job %s to user %s", job.id, token_data.user_id)

    headers = {
        "Content-Disposition": f'attachment; filename="{file_path.name}"',
        "X-Content-Type-Options": "nosniff",
    }
    resp = web.FileResponse(file_path, headers=headers)
    resp.content_type = _content_type_for(file_path)
    return resp


def create_download_app(db_path: Path, storage_dir: Path) -> web.Application:
    app = web.Application()
    app["db_path"] = db_path
    app["storage_dir"] = storage_dir
    app.router.add_get("/download/{token}", handle_download)
    return app
