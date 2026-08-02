from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from .storage import (
    consume_download_token,
    get_edit_job,
    get_job,
    lookup_download_token,
    open_database,
)

LOGGER = logging.getLogger(__name__)
DB_PATH_KEY = web.AppKey("db_path", Path)
STORAGE_DIR_KEY = web.AppKey("storage_dir", Path)


def resolve_contained_file(storage_dir: Path, file_path: Path) -> Path | None:
    """Return a readable regular file under storage_dir, or None if unsafe/missing.

    Uses resolve-based containment so symlink escapes are rejected. Symlinked
    media paths are forbidden even when the ultimate target is inside the root.
    """
    try:
        root = storage_dir.resolve(strict=True)
    except OSError:
        return None
    if file_path.is_symlink():
        return None
    try:
        resolved = file_path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


async def _load_token_resource(db_path: Path, token_data) -> Path | None:
    resource = (
        await get_edit_job(db_path, token_data.edit_job_id)
        if token_data.edit_job_id is not None
        else await get_job(db_path, token_data.job_id)
    )
    if resource is None or resource.file_path is None:
        return None
    return Path(resource.file_path)


async def handle_download(request: web.Request) -> web.Response:
    if request.method == "HEAD":
        return web.Response(status=405, text="Method Not Allowed")

    token = request.match_info.get("token", "")
    if not token:
        return web.Response(status=404, text="Not found")

    db_path = request.app[DB_PATH_KEY]
    storage_dir = request.app[STORAGE_DIR_KEY]

    token_data = await lookup_download_token(db_path, token)
    if token_data is None:
        return web.Response(status=403, text="Invalid or expired link")

    file_path = await _load_token_resource(db_path, token_data)
    if file_path is None:
        return web.Response(status=404, text="File not found")

    resolved = resolve_contained_file(storage_dir, file_path)
    if resolved is None:
        if not file_path.exists():
            return web.Response(status=404, text="File not found")
        return web.Response(status=403, text="Access denied")

    claimed = await consume_download_token(db_path, token)
    if claimed is None:
        return web.Response(status=403, text="Invalid or expired link")

    LOGGER.info(
        "Serving download for %s %s to user %s",
        "edit" if claimed.edit_job_id is not None else "job",
        claimed.edit_job_id if claimed.edit_job_id is not None else claimed.job_id,
        claimed.user_id,
    )

    headers = {
        "Content-Disposition": f'attachment; filename="{resolved.name}"',
        "X-Content-Type-Options": "nosniff",
    }
    resp = web.FileResponse(resolved, headers=headers)
    resp.content_type = "application/octet-stream"
    return resp


async def handle_health(request: web.Request) -> web.Response:
    """Return minimal readiness without exposing paths, schema, or exceptions."""
    db_path = request.app[DB_PATH_KEY]
    storage_dir = request.app[STORAGE_DIR_KEY]
    healthy = False
    try:
        if not storage_dir.is_dir() or not os.access(storage_dir, os.R_OK | os.W_OK):
            raise OSError("storage is unavailable")
        if not db_path.is_file():
            raise OSError("database is unavailable")
        async with asyncio.timeout(2):
            async with open_database(db_path) as db:
                async with db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
                ) as cursor:
                    healthy = await cursor.fetchone() is not None
    except Exception as exc:
        LOGGER.warning("Health check failed (%s)", type(exc).__name__)

    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if not healthy:
        return web.json_response({"status": "unhealthy"}, status=503, headers=headers)
    return web.json_response({"status": "ok"}, headers=headers)


def create_download_app(db_path: Path, storage_dir: Path) -> web.Application:
    app = web.Application()
    app[DB_PATH_KEY] = db_path
    app[STORAGE_DIR_KEY] = storage_dir
    app.router.add_get("/healthz", handle_health)
    # Register GET only so aiohttp does not auto-map HEAD through the handler.
    app.router.add_route("GET", "/download/{token}", handle_download)
    app.router.add_route("HEAD", "/download/{token}", handle_download)
    return app
