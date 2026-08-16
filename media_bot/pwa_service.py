"""Owner-scoped PWA media jobs backed by the existing downloader queue."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .downloader import (
    DownloadError,
    create_thumbnail,
    download_instagram,
    download_media,
    download_tiktok_slideshow,
    persist_download,
    read_source_metadata,
)
from .platforms import is_instagram_url, is_supported_url, is_tiktok_photo_url, is_tiktok_url
from .storage import (
    CleanupResult,
    JobRecord,
    create_external_job,
    delete_job_with_artifacts,
    get_job_for_owner,
    list_jobs_for_owner,
    update_job,
)
from .work_queue import WorkAlreadyQueued, WorkQueue, WorkRejected


PWA_OWNER_KIND = "watchmywallet"
PWA_SOURCE_CHANNEL = "PWA"
SUPPORTED_FORMATS = frozenset({"video", "audio"})
SUPPORTED_QUALITIES = frozenset({"best", "2160p", "1440p", "1080p", "720p", "480p"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "deleted"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PwaMediaService:
    """Application service shared by the private aiohttp API and one queue."""

    def __init__(
        self,
        *,
        db_path: Path,
        storage_dir: Path,
        ytdlp: Path,
        gallerydl: Path,
        work: WorkQueue,
        max_filesize_mb: int,
        timeout_seconds: int,
    ) -> None:
        self.db_path = db_path
        self.storage_dir = storage_dir
        self.ytdlp = ytdlp
        self.gallerydl = gallerydl
        self.work = work
        self.max_filesize_mb = max_filesize_mb
        self.timeout_seconds = timeout_seconds

    async def create_job(
        self,
        *,
        owner_id: str,
        url: str,
        requested_format: str = "video",
        requested_quality: str = "best",
    ) -> JobRecord:
        normalized_url = url.strip()
        if not is_supported_url(normalized_url):
            raise ValueError("URL must be an HTTPS link from a supported media site")
        if requested_format not in SUPPORTED_FORMATS:
            raise ValueError("format must be video or audio")
        if requested_quality not in SUPPORTED_QUALITIES:
            raise ValueError("quality is not supported")
        job = await create_external_job(
            self.db_path,
            normalized_url,
            owner_kind=PWA_OWNER_KIND,
            owner_id=owner_id,
            source_channel=PWA_SOURCE_CHANNEL,
            requested_format=requested_format,
            requested_quality=requested_quality,
            requested_options=json.dumps(
                {"format": requested_format, "quality": requested_quality},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        job = await update_job(
            self.db_path,
            job.id,
            status="queued",
            phase="queued",
        )
        assert job is not None
        try:
            self.work.submit(
                user_id=owner_id,
                label=self._label(job.id),
                factory=lambda job_id=job.id, owner=owner_id: self._process_job(job_id, owner),
            )
        except (WorkRejected, WorkAlreadyQueued) as exc:
            job = await update_job(
                self.db_path,
                job.id,
                status="failed",
                phase="failed",
                error_code="queue_rejected",
                error_message=str(exc)[:500],
                failed_at=_now_iso(),
            )
            assert job is not None
        return job

    async def list_jobs(
        self, *, owner_id: str, limit: int = 50, status: str | None = None
    ) -> list[JobRecord]:
        return await list_jobs_for_owner(
            self.db_path,
            owner_kind=PWA_OWNER_KIND,
            owner_id=owner_id,
            limit=limit,
            status=status,
        )

    async def get_job(self, *, owner_id: str, job_id: int) -> JobRecord | None:
        return await get_job_for_owner(
            self.db_path,
            job_id,
            owner_kind=PWA_OWNER_KIND,
            owner_id=owner_id,
        )

    async def cancel_job(self, *, owner_id: str, job_id: int) -> JobRecord | None:
        job = await self.get_job(owner_id=owner_id, job_id=job_id)
        if job is None:
            return None
        if job.status in TERMINAL_STATUSES:
            return job
        self.work.cancel(user_id=owner_id, label=self._label(job.id))
        updated = await update_job(
            self.db_path,
            job.id,
            status="cancelled",
            phase="cancelled",
            error_code="cancelled_by_user",
        )
        return updated

    async def delete_job(self, *, owner_id: str, job_id: int) -> CleanupResult | None:
        if await self.get_job(owner_id=owner_id, job_id=job_id) is None:
            return None
        self.work.cancel(user_id=owner_id, label=self._label(job_id))
        return await delete_job_with_artifacts(
            self.db_path,
            self.storage_dir,
            job_id,
            owner_kind=PWA_OWNER_KIND,
            owner_id=owner_id,
        )

    @staticmethod
    def _label(job_id: int) -> str:
        return f"pwa:download:{job_id}"

    async def _report_progress(self, job_id: int, percent: int) -> None:
        await update_job(
            self.db_path,
            job_id,
            phase="downloading",
            progress_percent=float(max(0, min(99, percent))),
        )

    async def _process_job(self, job_id: int, owner_id: str) -> None:
        job = await self.get_job(owner_id=owner_id, job_id=job_id)
        if job is None or job.status == "cancelled":
            return
        temporary = None
        try:
            started = _now_iso()
            await update_job(
                self.db_path,
                job_id,
                status="downloading",
                phase="downloading",
                started_at=started,
                progress_percent=0.0,
            )
            async def progress(value: int) -> None:
                await self._report_progress(job_id, value)
            if is_tiktok_photo_url(job.url):
                temporary, media = await download_tiktok_slideshow(
                    self.gallerydl,
                    job.url,
                    self.max_filesize_mb,
                    self.timeout_seconds,
                    progress,
                )
            elif is_instagram_url(job.url):
                temporary, media = await download_instagram(
                    self.gallerydl,
                    job.url,
                    self.max_filesize_mb,
                    self.timeout_seconds,
                    progress,
                    ytdlp=self.ytdlp,
                )
            else:
                try:
                    temporary, media = await download_media(
                        self.ytdlp,
                        job.url,
                        self.max_filesize_mb,
                        self.timeout_seconds,
                        progress,
                        format_name=job.requested_format or "video",
                        quality=job.requested_quality or "best",
                    )
                except DownloadError:
                    if is_tiktok_url(job.url):
                        temporary, media = await download_tiktok_slideshow(
                            self.gallerydl,
                            job.url,
                            self.max_filesize_mb,
                            self.timeout_seconds,
                            progress,
                        )
                    else:
                        raise

            await update_job(self.db_path, job_id, status="processing", phase="processing")
            title, source_caption = read_source_metadata(media.parent)
            persisted = await persist_download(media, job_id, self.storage_dir)
            thumbnail = await create_thumbnail(
                persisted,
                self.storage_dir / f"{job_id}-thumbnail.jpg",
            )
            mime_type = mimetypes.guess_type(persisted.name)[0] or "application/octet-stream"
            metadata: dict[str, Any] = {}
            if title:
                metadata["title"] = title
            if source_caption:
                metadata["source_caption"] = source_caption
            await update_job(
                self.db_path,
                job_id,
                status="completed",
                phase="completed",
                progress_percent=100.0,
                file_path=str(persisted),
                file_size=persisted.stat().st_size,
                title=title,
                source_caption=source_caption,
                thumbnail_path=str(thumbnail) if thumbnail else None,
                output_filename=persisted.name,
                output_mime_type=mime_type,
                output_metadata=json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                completed_at=_now_iso(),
                error_code=None,
                error_message=None,
            )
        except asyncio.CancelledError:
            await update_job(
                self.db_path,
                job_id,
                status="cancelled",
                phase="cancelled",
                error_code="cancelled_by_user",
            )
            raise
        except Exception as exc:
            await update_job(
                self.db_path,
                job_id,
                status="failed",
                phase="failed",
                error_code="download_failed",
                error_message=str(exc)[:500],
                failed_at=_now_iso(),
            )
        finally:
            if temporary is not None:
                temporary.cleanup()


__all__ = [
    "PWA_OWNER_KIND",
    "PWA_SOURCE_CHANNEL",
    "PwaMediaService",
    "SUPPORTED_FORMATS",
    "SUPPORTED_QUALITIES",
]
