"""Private watchMyWallet-facing media API.

This app is intentionally separate from the legacy token download app but is
started in the same Telegram process and shares its database, storage, and
bounded download queue.
"""

from __future__ import annotations

import asyncio
import hmac
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .acting_context import ActingContextError, ActingUserContext, validate_acting_context
from .download_server import resolve_contained_file
from .pwa_service import PwaMediaService
from .storage import claim_internal_request_id, open_database


CONTEXT_HEADER = "X-WatchMyWallet-Acting-User"
SIGNATURE_HEADER = "X-WatchMyWallet-Acting-Signature"
REQUEST_ID_HEADER = "X-WatchMyWallet-Request-ID"
CLIENT_HEADER = "X-WatchMyWallet-Client"
API_KEY_HEADER = "X-Media-Api-Key"
MAX_BODY_BYTES = 64 * 1024
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "deleted"})


@dataclass(frozen=True)
class MediaApiRuntime:
    service: PwaMediaService
    api_key: str | None
    signing_secret: str | None
    acting_context_max_age_seconds: int = 60
    acting_context_clock_skew_seconds: int = 5


RUNTIME_KEY = web.AppKey("media_api_runtime", MediaApiRuntime)


def _error(status: int, detail: str, *, code: str | None = None) -> web.Response:
    payload: dict[str, str] = {"detail": detail}
    if code:
        payload["code"] = code
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _job_payload(job) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "source": job.url,
        "source_channel": job.source_channel,
        "requested_format": job.requested_format,
        "requested_quality": job.requested_quality,
        "status": job.status,
        "phase": job.phase,
        "progress_percent": job.progress_percent,
        "bytes_downloaded": job.bytes_downloaded,
        "bytes_total": job.bytes_total,
        "speed": job.speed,
        "eta_seconds": job.eta_seconds,
        "title": job.title,
        "file_size": job.file_size,
        "output_filename": job.output_filename,
        "output_mime_type": job.output_mime_type,
        "has_result": bool(job.file_path and job.status == "completed"),
        "error_code": job.error_code,
        "error": job.error_message,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "failed_at": _iso(job.failed_at),
    }


async def _request_json(request: web.Request) -> dict[str, Any] | web.Response:
    length = request.headers.get("Content-Length")
    if length:
        try:
            if int(length) > MAX_BODY_BYTES:
                return _error(413, "request body is too large")
        except ValueError:
            return _error(400, "invalid content length")
    if not request.content_type.lower().startswith("application/json"):
        return _error(415, "application/json is required")
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return _error(400, "request body must be valid JSON")
    if not isinstance(payload, dict):
        return _error(400, "request body must be a JSON object")
    return payload


def _service_authenticated(request: web.Request, runtime: MediaApiRuntime) -> web.Response | None:
    if not runtime.api_key:
        return _error(503, "media service authentication is not configured", code="not_configured")
    supplied = request.headers.get(API_KEY_HEADER, "")
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, runtime.api_key):
        return _error(401, "media service authentication failed", code="service_authentication_failed")
    return None


def _acting_context(
    request: web.Request,
    runtime: MediaApiRuntime,
) -> ActingUserContext | web.Response:
    auth_error = _service_authenticated(request, runtime)
    if auth_error is not None:
        return auth_error
    if request.headers.get(CLIENT_HEADER) != "pwa-bff":
        return _error(403, "trusted caller marker is required", code="caller_not_trusted")
    try:
        return validate_acting_context(
            encoded_payload=request.headers.get(CONTEXT_HEADER),
            signature=request.headers.get(SIGNATURE_HEADER),
            request_id_header=request.headers.get(REQUEST_ID_HEADER),
            signing_secret=runtime.signing_secret,
            max_age_seconds=runtime.acting_context_max_age_seconds,
            clock_skew_seconds=runtime.acting_context_clock_skew_seconds,
        )
    except ActingContextError as exc:
        status = 503 if exc.code == "signing_not_configured" else 401
        return _error(status, "acting-user context was rejected", code=exc.code)


async def _claim_mutation(request: web.Request, runtime: MediaApiRuntime, context: ActingUserContext) -> web.Response | None:
    claimed = await claim_internal_request_id(
        runtime.service.db_path,
        context.request_id,
        context.user_id,
        runtime.acting_context_max_age_seconds + runtime.acting_context_clock_skew_seconds,
    )
    if not claimed:
        return _error(409, "request has already been used", code="replayed_request")
    return None


def _job_id(request: web.Request) -> int | web.Response:
    raw = request.match_info.get("job_id", "")
    try:
        value = int(raw)
    except ValueError:
        return _error(404, "media job not found")
    if value <= 0:
        return _error(404, "media job not found")
    return value


async def handle_health(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    auth_error = _service_authenticated(request, runtime)
    if auth_error is not None:
        return auth_error
    healthy = False
    try:
        if runtime.service.storage_dir.is_dir() and runtime.service.db_path.is_file():
            async with asyncio.timeout(2):
                async with open_database(runtime.service.db_path) as db:
                    async with db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                    ) as cursor:
                        healthy = await cursor.fetchone() is not None
    except Exception:
        healthy = False
    if not healthy:
        return _error(503, "media service is not ready", code="unhealthy")
    return web.json_response(
        {"status": "ok", "job_api": True, "queue": "shared"},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def handle_list_jobs(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    status = request.query.get("status")
    if status and status not in {"queued", "downloading", "processing", "completed", "failed", "cancelled"}:
        return _error(400, "unsupported job status")
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        return _error(400, "limit must be an integer")
    jobs = await runtime.service.list_jobs(owner_id=context.user_id, limit=limit, status=status)
    return web.json_response({"items": [_job_payload(job) for job in jobs]})


async def handle_create_job(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    replay_error = await _claim_mutation(request, runtime, context)
    if replay_error is not None:
        return replay_error
    payload = await _request_json(request)
    if isinstance(payload, web.Response):
        return payload
    url = payload.get("url")
    requested_format = payload.get("format", "video")
    requested_quality = payload.get("quality", "best")
    if not isinstance(url, str) or len(url.strip()) > 4096:
        return _error(422, "url is required and must be bounded")
    if not isinstance(requested_format, str) or not isinstance(requested_quality, str):
        return _error(422, "format and quality must be strings")
    try:
        job = await runtime.service.create_job(
            owner_id=context.user_id,
            url=url,
            requested_format=requested_format,
            requested_quality=requested_quality,
        )
    except ValueError as exc:
        return _error(422, str(exc))
    return web.json_response(_job_payload(job), status=201)


async def handle_get_job(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    job_id = _job_id(request)
    if isinstance(job_id, web.Response):
        return job_id
    job = await runtime.service.get_job(owner_id=context.user_id, job_id=job_id)
    if job is None:
        return _error(404, "media job not found")
    return web.json_response(_job_payload(job))


async def handle_cancel_job(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    replay_error = await _claim_mutation(request, runtime, context)
    if replay_error is not None:
        return replay_error
    job_id = _job_id(request)
    if isinstance(job_id, web.Response):
        return job_id
    job = await runtime.service.cancel_job(owner_id=context.user_id, job_id=job_id)
    if job is None:
        return _error(404, "media job not found")
    return web.json_response(_job_payload(job))


async def handle_delete_job(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    replay_error = await _claim_mutation(request, runtime, context)
    if replay_error is not None:
        return replay_error
    job_id = _job_id(request)
    if isinstance(job_id, web.Response):
        return job_id
    job = await runtime.service.get_job(owner_id=context.user_id, job_id=job_id)
    if job is None:
        return _error(404, "media job not found")
    if job.status not in TERMINAL_STATUSES:
        return _error(409, "only finished, failed, or cancelled jobs can be deleted")
    result = await runtime.service.delete_job(owner_id=context.user_id, job_id=job_id)
    if result is None:
        return _error(404, "media job not found")
    return web.json_response({"deleted": True, "job_id": str(job_id)})


async def handle_result(request: web.Request) -> web.Response:
    runtime: MediaApiRuntime = request.app[RUNTIME_KEY]
    context = _acting_context(request, runtime)
    if isinstance(context, web.Response):
        return context
    job_id = _job_id(request)
    if isinstance(job_id, web.Response):
        return job_id
    job = await runtime.service.get_job(owner_id=context.user_id, job_id=job_id)
    if job is None or job.status != "completed" or not job.file_path:
        return _error(404, "media result not found")
    resolved = resolve_contained_file(runtime.service.storage_dir, Path(job.file_path))
    if resolved is None:
        return _error(404, "media result not found")
    filename = Path(job.output_filename or resolved.name).name.replace('"', "")
    content_type = job.output_mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return web.FileResponse(
        resolved,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


def create_media_api_app(runtime: MediaApiRuntime) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[RUNTIME_KEY] = runtime
    app.router.add_get("/api/media/health", handle_health)
    # Compatibility alias used by the existing watchMyWallet health client.
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/media/jobs", handle_list_jobs)
    app.router.add_post("/api/media/jobs", handle_create_job)
    app.router.add_get("/api/media/jobs/{job_id}", handle_get_job)
    app.router.add_post("/api/media/jobs/{job_id}/cancel", handle_cancel_job)
    app.router.add_get("/api/media/jobs/{job_id}/result", handle_result)
    app.router.add_delete("/api/media/jobs/{job_id}", handle_delete_job)
    return app


__all__ = ["MediaApiRuntime", "create_media_api_app"]
