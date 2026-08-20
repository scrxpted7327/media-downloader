"""Telegram adapter for the shared durable acquisition lifecycle.

The lifecycle and SQLite records remain channel-neutral.  This module only
connects the existing downloader and shared-library seams to Telegram-owned
requesters; message delivery is deliberately left to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .acquisition import (
    AcquisitionLifecycle,
    AcquisitionRequest,
    AcquisitionState,
    ClaimRecord,
    DownloadedMedia,
    ErrorEvent,
    ProgressEvent,
    PromotionResult,
    RequesterRecord,
    ResultEvent,
    SourceIdentity,
)
from .acquisition_storage import AcquisitionStorage
from .downloader import (
    DownloadError,
    create_thumbnail,
    download_instagram,
    download_media,
    download_tiktok_slideshow,
)
from .library import normalized_source_details
from .platforms import (
    is_instagram_url,
    is_tiktok_photo_url,
    is_tiktok_url,
)
from .shared_media_library import LibraryPrincipal, SharedMediaLibrary

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[
    [ProgressEvent | ResultEvent | ErrorEvent], Awaitable[None]
]


@dataclass(frozen=True)
class _DownloadContext:
    requester_id: str
    owner_id: str
    job_id: int | None


class _Cancellation:
    def __init__(self, runtime: "TelegramAcquisitionRuntime") -> None:
        self.runtime = runtime

    async def requested(self, job_id: str) -> bool:
        if job_id in self.runtime._cancelled:
            return True
        requester = await self.runtime.storage.get_requester(job_id)
        return requester is None or requester.state is AcquisitionState.CANCELLED

    async def signal(self, job_id: str) -> None:
        self.runtime._cancelled.add(job_id)


class _Progress:
    def __init__(self, runtime: "TelegramAcquisitionRuntime") -> None:
        self.runtime = runtime

    async def emit(self, event: ProgressEvent | ResultEvent | ErrorEvent) -> None:
        callback = self.runtime._progress.get(event.job_id)
        if callback is None:
            return
        try:
            await callback(event)
        except Exception:
            # Status-message delivery is advisory; it must not turn a durable
            # acquisition into a failed download.
            LOGGER.exception("Telegram acquisition progress callback failed")


class _Downloader:
    def __init__(self, runtime: "TelegramAcquisitionRuntime") -> None:
        self.runtime = runtime

    async def download(self, identity: SourceIdentity, progress) -> DownloadedMedia:
        context = self.runtime._contexts.get((identity.source_key, identity.preset))
        if context is None:
            raise DownloadError("acquisition execution context is unavailable")
        temporary = None
        try:
            if is_tiktok_photo_url(identity.source_url):
                temporary, media = await download_tiktok_slideshow(
                    self.runtime.gallerydl,
                    identity.source_url,
                    self.runtime.max_filesize_mb,
                    self.runtime.timeout_seconds,
                    progress,
                )
            elif is_instagram_url(identity.source_url):
                temporary, media = await download_instagram(
                    self.runtime.gallerydl,
                    identity.source_url,
                    self.runtime.max_filesize_mb,
                    self.runtime.timeout_seconds,
                    progress,
                    ytdlp=self.runtime.ytdlp,
                )
            else:
                format_name = "audio" if identity.preset.startswith("audio_") else "video"
                quality = (
                    "best"
                    if identity.preset == "video_best"
                    else identity.preset.removeprefix("video_")
                )
                try:
                    temporary, media = await download_media(
                        self.runtime.ytdlp,
                        identity.source_url,
                        self.runtime.max_filesize_mb,
                        self.runtime.timeout_seconds,
                        progress,
                        format_name=format_name,
                        quality=quality,
                    )
                except DownloadError:
                    if not is_tiktok_url(identity.source_url):
                        raise
                    temporary, media = await download_tiktok_slideshow(
                        self.runtime.gallerydl,
                        identity.source_url,
                        self.runtime.max_filesize_mb,
                        self.runtime.timeout_seconds,
                        progress,
                    )

            details = normalized_source_details(media.parent, identity.source_url)
            claim_key = hashlib.sha256(
                f"{identity.source_key}:{identity.preset}".encode("utf-8")
            ).hexdigest()
            acquisition_root = self.runtime.storage_dir / "acquisitions"
            acquisition_root.mkdir(parents=True, exist_ok=True)
            destination = acquisition_root / f"{claim_key}{media.suffix.lower()}"
            if not destination.exists():
                temporary_destination = destination.with_name(
                    f".{destination.name}.{os.getpid()}.part"
                )

                def copy_output() -> None:
                    try:
                        shutil.copy2(media, temporary_destination)
                        os.replace(temporary_destination, destination)
                    finally:
                        temporary_destination.unlink(missing_ok=True)

                await asyncio.to_thread(copy_output)
            thumbnail = await create_thumbnail(
                destination,
                acquisition_root / f"{claim_key}-thumbnail.jpg",
            )
            title = str(details.get("title") or "").strip() or None
            caption = str(details.get("source_caption") or "").strip() or None
            metadata: dict[str, Any] = {
                "source_details": details,
                "requester_id": context.requester_id,
                "owner_id": context.owner_id,
                "job_id": context.job_id,
                "title": title,
                "source_caption": caption,
                "thumbnail_path": str(thumbnail) if thumbnail else None,
                "output_filename": destination.name,
                "output_mime_type": mimetypes.guess_type(destination.name)[0]
                or "application/octet-stream",
            }
            return DownloadedMedia(destination, metadata)
        finally:
            if temporary is not None:
                temporary.cleanup()


class _Promoter:
    def __init__(self, runtime: "TelegramAcquisitionRuntime") -> None:
        self.runtime = runtime

    async def promote(
        self, identity: SourceIdentity, media: DownloadedMedia
    ) -> PromotionResult:
        details = media.metadata.get("source_details")
        if not isinstance(details, dict):
            details = {}
        owner_id = str(media.metadata.get("owner_id") or "telegram-acquisition")
        principal = LibraryPrincipal(
            owner_id,
            frozenset({"media.library.promote"}),
        )
        duration = details.get("duration_seconds")
        result = await self.runtime.library.promote(
            principal,
            media.path,
            str(details.get("source_canonical_url") or identity.source_url),
            preset_key=identity.preset,
            source_platform=str(details.get("source_platform") or "") or None,
            source_media_id=str(details.get("source_media_id") or "") or None,
            title=str(details.get("title") or "") or None,
            uploader=str(details.get("uploader") or "") or None,
            duration_seconds=float(duration) if duration not in (None, "") else None,
            upload_date=str(details.get("upload_date") or "") or None,
            thumbnail_file=(
                Path(str(media.metadata["thumbnail_path"]))
                if media.metadata.get("thumbnail_path")
                else None
            ),
            created_from_job_id=(
                int(media.metadata["job_id"])
                if media.metadata.get("job_id") is not None
                else None
            ),
        )
        return PromotionResult(
            str(result.asset.id),
            str(result.variant.id),
            {"preset_key": result.variant.preset_key},
        )


class TelegramAcquisitionRuntime:
    """Durable Telegram requester adapter over one shared acquisition store."""

    def __init__(
        self,
        *,
        db_path: Path,
        storage_dir: Path,
        ytdlp: Path,
        gallerydl: Path,
        max_filesize_mb: int,
        timeout_seconds: int,
        library_max_size_mb: int = 0,
        library_min_free_space_mb: int = 1024,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_dir = Path(storage_dir)
        self.ytdlp = Path(ytdlp)
        self.gallerydl = Path(gallerydl)
        self.max_filesize_mb = max_filesize_mb
        self.timeout_seconds = timeout_seconds
        self.storage = AcquisitionStorage(self.db_path)
        self.library = SharedMediaLibrary(
            self.db_path,
            self.storage_dir / "library",
            max_bytes=(library_max_size_mb * 1024 * 1024 if library_max_size_mb > 0 else None),
            min_free_bytes=library_min_free_space_mb * 1024 * 1024,
        )
        self._init_lock = asyncio.Lock()
        self._initialized = False
        self._cancelled: set[str] = set()
        self._contexts: dict[tuple[str, str], _DownloadContext] = {}
        self._progress: dict[str, ProgressCallback] = {}
        self.lifecycle = AcquisitionLifecycle(
            persistence=self.storage,
            downloader=_Downloader(self),
            promoter=_Promoter(self),
            cancellation=_Cancellation(self),
            progress=_Progress(self),
        )

    async def init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await self.storage.init()
                self._initialized = True

    async def submit(
        self,
        requester_id: str,
        source_url: str,
        *,
        preset: str = "video_best",
        owner_id: str = "",
        job_id: int | None = None,
    ) -> RequesterRecord:
        await self.init()
        request = AcquisitionRequest(
            requester_id=requester_id,
            source_url=source_url,
            preset=preset,
            metadata={"owner_id": owner_id, "job_id": job_id},
        )
        identity = SourceIdentity.from_request(request)
        self._contexts.setdefault(
            (identity.source_key, identity.preset),
            _DownloadContext(requester_id, owner_id, job_id),
        )
        return await self.lifecycle.submit(request)

    async def run(self, requester_id: str) -> RequesterRecord:
        await self.init()
        return await self.lifecycle.run(requester_id)

    async def cancel(self, requester_id: str) -> RequesterRecord:
        await self.init()
        return await self.lifecycle.cancel(requester_id)

    async def record_delivery(
        self, requester_id: str, *, success: bool, error: str | None = None
    ) -> RequesterRecord:
        await self.init()
        return await self.lifecycle.record_delivery(
            requester_id, success=success, error=error
        )

    async def reconcile(self) -> int:
        await self.init()
        return await self.lifecycle.reconcile()

    async def get_requester(self, requester_id: str) -> RequesterRecord | None:
        await self.init()
        return await self.storage.get_requester(requester_id)

    async def get_claim(self, claim_id: str) -> ClaimRecord | None:
        await self.init()
        return await self.storage.get_claim(claim_id)

    def bind_progress(self, requester_id: str, callback: ProgressCallback) -> None:
        self._progress[requester_id] = callback

    def unbind_progress(self, requester_id: str) -> None:
        self._progress.pop(requester_id, None)


__all__ = ["TelegramAcquisitionRuntime"]
