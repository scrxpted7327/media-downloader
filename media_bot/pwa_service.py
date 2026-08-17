"""Owner-scoped PWA media jobs backed by the existing downloader queue."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
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
)
from .library import (
    PRESET_SPECS,
    normalized_source_details,
    preset_extension,
    preset_for_job,
    probe_media,
    sha256_file,
)
from .platforms import (
    is_instagram_url,
    is_supported_url,
    is_tiktok_photo_url,
    is_tiktok_url,
)
from .storage import (
    CleanupResult,
    JobRecord,
    create_external_job,
    create_or_get_media_asset,
    create_or_get_media_variant,
    delete_job_with_artifacts,
    delete_media_asset,
    delete_media_variant,
    get_job_for_owner,
    get_media_asset,
    get_media_variant,
    list_jobs_for_owner,
    list_media_assets,
    list_media_variants,
    media_library_storage_bytes,
    update_job,
    update_media_asset,
    update_media_variant,
)
from .work_queue import WorkAlreadyQueued, WorkQueue, WorkRejected

PWA_OWNER_KIND = "watchmywallet"
PWA_SOURCE_CHANNEL = "PWA"
LOGGER = logging.getLogger(__name__)
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
        variant_work: WorkQueue | None = None,
        library_max_filesize_mb: int = 2048,
        library_min_free_space_mb: int = 1024,
        library_max_size_mb: int = 0,
    ) -> None:
        self.db_path = db_path
        self.storage_dir = storage_dir
        self.ytdlp = ytdlp
        self.gallerydl = gallerydl
        self.work = work
        self.max_filesize_mb = max_filesize_mb
        self.timeout_seconds = timeout_seconds
        self.variant_work = variant_work
        self.library_root = self.storage_dir / "library"
        self.library_max_filesize_mb = library_max_filesize_mb
        self.library_min_free_space_mb = library_min_free_space_mb
        self.library_max_size_mb = library_max_size_mb

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

    async def list_library(
        self, *, limit: int = 50, query: str | None = None, sort: str | None = None
    ) -> list[tuple[Any, list[Any]]]:
        assets = await list_media_assets(
            self.db_path, limit=limit, query=query, sort=sort
        )
        return [(asset, await list_media_variants(self.db_path, asset.id)) for asset in assets]

    async def get_library_asset(self, *, asset_id: int) -> tuple[Any, list[Any]] | None:
        asset = await get_media_asset(self.db_path, asset_id)
        if asset is None or asset.scope != "shared" or asset.status == "deleted":
            return None
        return asset, await list_media_variants(self.db_path, asset.id)

    async def request_variant(
        self, *, requester_id: str, asset_id: int, preset_key: str
    ) -> tuple[Any, bool]:
        if preset_key not in PRESET_SPECS:
            raise ValueError("unsupported media library preset")
        asset_bundle = await self.get_library_asset(asset_id=asset_id)
        if asset_bundle is None:
            raise LookupError("media asset not found")
        asset, variants = asset_bundle
        existing = next((item for item in variants if item.preset_key == preset_key), None)
        if existing is not None:
            if existing.status == "ready":
                return existing, False
            if self.variant_work is not None and not self.variant_work.has_label(
                self._variant_label(existing.id)
            ):
                self._submit_variant(existing.id, requester_id)
            return existing, False
        await self._check_library_capacity_async(0)
        variant, created = await create_or_get_media_variant(
            self.db_path, asset_id=asset.id, preset_key=preset_key
        )
        if self.variant_work is None:
            await update_media_variant(
                self.db_path,
                variant.id,
                status="failed",
                error_code="variant_queue_unavailable",
                error_message="variant generation is not available",
            )
            return (await get_media_variant(self.db_path, variant.id)), created  # type: ignore[return-value]
        self._submit_variant(variant.id, requester_id)
        return variant, created

    def _submit_variant(self, variant_id: int, requester_id: str) -> None:
        if self.variant_work is None or self.variant_work.has_label(self._variant_label(variant_id)):
            return
        try:
            self.variant_work.submit(
                user_id=requester_id,
                label=self._variant_label(variant_id),
                factory=lambda: self._process_variant(variant_id),
            )
        except WorkAlreadyQueued:
            return
        except WorkRejected as exc:
            asyncio.create_task(
                update_media_variant(
                    self.db_path,
                    variant_id,
                    status="failed",
                    error_code="queue_rejected",
                    error_message=str(exc)[:500],
                )
            )

    @staticmethod
    def _variant_label(variant_id: int) -> str:
        return f"library:variant:{variant_id}"

    async def _check_library_capacity_async(self, additional_bytes: int) -> None:
        usage = shutil.disk_usage(self.library_root.parent)
        if usage.free < self.library_min_free_space_mb * 1024 * 1024:
            raise ValueError("shared media storage is temporarily low on free space")
        if self.library_max_size_mb > 0:
            used = await media_library_storage_bytes(self.db_path)
            if used + additional_bytes > self.library_max_size_mb * 1024 * 1024:
                raise ValueError("shared media library storage limit reached")

    async def _copy_library_file(self, source: Path, destination: Path) -> Path:
        if not source.is_file() or source.is_symlink():
            raise ValueError("media source file is unavailable")
        if not source.resolve(strict=False).is_relative_to(
            self.storage_dir.resolve(strict=False)
        ):
            raise ValueError("media source is outside the configured storage root")
        if destination.exists():
            return destination
        await self._check_library_capacity_async(source.stat().st_size)

        def _copy() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
            try:
                try:
                    os.link(source, destination, follow_symlinks=False)
                except (OSError, FileExistsError):
                    shutil.copy2(source, temporary)
                    os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(_copy)
        return destination

    async def _promote_completed_job(
        self, job: JobRecord, details: dict[str, object]
    ) -> tuple[str, str] | None:
        if (
            job.owner_kind != PWA_OWNER_KIND
            or job.source_channel != PWA_SOURCE_CHANNEL
            or not job.file_path
        ):
            return None
        source = Path(job.file_path)
        source_key = str(details.get("source_key") or f"url:{job.url}")
        asset, _ = await create_or_get_media_asset(
            self.db_path,
            source_platform=str(details.get("source_platform") or "") or None,
            source_media_id=str(details.get("source_media_id") or "") or None,
            source_key=source_key,
            source_canonical_url=str(details.get("source_canonical_url") or job.url),
            title=str(details.get("title") or job.title or "") or None,
            uploader=str(details.get("uploader") or "") or None,
            duration_seconds=(
                float(details["duration_seconds"])
                if details.get("duration_seconds") not in (None, "")
                else None
            ),
            upload_date=str(details.get("upload_date") or "") or None,
            thumbnail_path=None,
            created_from_job_id=job.id,
            created_by_owner_id=job.owner_id,
        )
        asset_dir = self.library_root / str(asset.id)
        if job.thumbnail_path and Path(job.thumbnail_path).is_file() and not asset.thumbnail_path:
            thumbnail = await self._copy_library_file(
                Path(job.thumbnail_path), asset_dir / "thumbnail.jpg"
            )
            await update_media_asset(self.db_path, asset.id, thumbnail_path=str(thumbnail))
        preset_key = preset_for_job(job.requested_format, job.requested_quality)
        variant, created = await create_or_get_media_variant(
            self.db_path, asset_id=asset.id, preset_key=preset_key, status="processing"
        )
        if created or variant.status != "ready":
            destination = asset_dir / f"{preset_key}{preset_extension(preset_key, source.suffix)}"
            copied = await self._copy_library_file(source, destination)
            probe = await probe_media(copied)
            digest = await sha256_file(copied)
            await update_media_variant(
                self.db_path,
                variant.id,
                status="ready",
                file_path=str(copied),
                file_size=int(probe.get("file_size") or copied.stat().st_size),
                mime_type=mimetypes.guess_type(copied.name)[0] or "application/octet-stream",
                container=probe.get("container"),
                video_codec=probe.get("video_codec"),
                audio_codec=probe.get("audio_codec"),
                width=probe.get("width"),
                height=probe.get("height"),
                duration_seconds=probe.get("duration_seconds"),
                sha256=digest,
                error_code=None,
                error_message=None,
            )
        return str(asset.id), preset_key

    async def _process_variant(self, variant_id: int) -> None:
        variant = await get_media_variant(self.db_path, variant_id)
        if variant is None:
            return
        bundle = await self.get_library_asset(asset_id=variant.asset_id)
        if bundle is None:
            return
        asset, variants = bundle
        source = next(
            (item for item in variants if item.status == "ready" and item.file_path), None
        )
        if source is None or source.file_path is None:
            await update_media_variant(
                self.db_path, variant_id, status="failed", error_code="source_unavailable",
                error_message="no durable source variant is available",
            )
            return
        await update_media_variant(self.db_path, variant_id, status="processing")
        destination = self.library_root / str(asset.id) / (
            f"{variant.preset_key}{preset_extension(variant.preset_key)}"
        )
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        spec = PRESET_SPECS[variant.preset_key]
        try:
            if variant.preset_key == source.preset_key:
                output = await self._copy_library_file(Path(source.file_path), destination)
            else:
                output = await self._render_variant(
                    Path(source.file_path), temporary, variant.preset_key, int(spec.get("height") or 0)
                )
                output = await self._copy_library_file(output, destination)
                temporary.unlink(missing_ok=True)
            probe = await probe_media(output)
            if output.stat().st_size > self.library_max_filesize_mb * 1024 * 1024:
                raise ValueError("generated variant exceeds the shared library size limit")
            await update_media_variant(
                self.db_path,
                variant_id,
                status="ready",
                file_path=str(output),
                file_size=int(probe.get("file_size") or output.stat().st_size),
                mime_type=mimetypes.guess_type(output.name)[0] or "application/octet-stream",
                container=probe.get("container"),
                video_codec=probe.get("video_codec"),
                audio_codec=probe.get("audio_codec"),
                width=probe.get("width"),
                height=probe.get("height"),
                duration_seconds=probe.get("duration_seconds"),
                sha256=await sha256_file(output),
                source_variant_id=source.id,
                error_code=None,
                error_message=None,
            )
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            await update_media_variant(
                self.db_path,
                variant_id,
                status="failed",
                error_code="variant_generation_failed",
                error_message=str(exc)[:500],
            )

    async def delete_library_asset(
        self, *, asset_id: int
    ) -> tuple[Any | None, list[Any]]:
        bundle = await self.get_library_asset(asset_id=asset_id)
        if bundle is None:
            return None, []
        asset, variants = await delete_media_asset(self.db_path, asset_id)
        paths = [Path(item.file_path) for item in variants if item.file_path]
        if asset is not None and asset.thumbnail_path:
            paths.append(Path(asset.thumbnail_path))
        for path in paths:
            try:
                if path.resolve(strict=False).is_relative_to(
                    self.library_root.resolve(strict=False)
                ):
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        return asset, variants

    async def delete_library_variant(self, *, variant_id: int) -> Any | None:
        variant = await get_media_variant(self.db_path, variant_id)
        if variant is None:
            return None
        deleted = await delete_media_variant(self.db_path, variant_id)
        if variant.file_path:
            path = Path(variant.file_path)
            try:
                if path.resolve(strict=False).is_relative_to(
                    self.library_root.resolve(strict=False)
                ):
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        return deleted

    async def _render_variant(
        self, source: Path, destination: Path, preset_key: str, height: int
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if preset_key.startswith("audio_"):
            codec = "libmp3lame" if preset_key == "audio_mp3" else "aac"
            command = ["ffmpeg", "-y", "-i", str(source), "-vn", "-c:a", codec]
            if codec == "libmp3lame":
                command += ["-q:a", "2"]
            else:
                command += ["-b:a", "192k"]
            command += ["-movflags", "+faststart", str(destination)]
        else:
            encoder = "libx264"
            command = [
                "ffmpeg", "-y", "-i", str(source), "-vf", f"scale=-2:min({height},ih)",
                "-c:v", encoder, "-preset", "medium", "-crf", "23", "-c:a", "aac",
                "-movflags", "+faststart", str(destination),
            ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        del stdout
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").splitlines()[-2:]
            raise DownloadError("ffmpeg variant generation failed: " + "; ".join(detail)[:500])
        if not destination.is_file():
            raise DownloadError("ffmpeg produced no variant")
        return destination

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
            details = normalized_source_details(media.parent, job.url)
            title = str(details.get("title") or "").strip() or None
            source_caption = str(details.get("source_caption") or "").strip() or None
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
                status="processing",
                phase="processing",
                progress_percent=99.0,
                file_path=str(persisted),
                file_size=persisted.stat().st_size,
                title=title,
                source_caption=source_caption,
                thumbnail_path=str(thumbnail) if thumbnail else None,
                output_filename=persisted.name,
                output_mime_type=mime_type,
                output_metadata=json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            )
            processing = await self.get_job(owner_id=owner_id, job_id=job_id)
            if processing is not None:
                try:
                    promoted = await self._promote_completed_job(processing, details)
                    if promoted is not None:
                        metadata.update(
                            {"library_asset_id": promoted[0], "library_preset": promoted[1]}
                        )
                except Exception as exc:
                    # The private job remains a valid completed result even if
                    # shared-library promotion is temporarily blocked by disk,
                    # probing, or a transient SQLite failure.
                    LOGGER.warning("Could not promote PWA job %s to library: %s", job_id, exc)
            await update_job(
                self.db_path,
                job_id,
                status="completed",
                phase="completed",
                progress_percent=100.0,
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
    "SUPPORTED_FORMATS",
    "SUPPORTED_QUALITIES",
    "PwaMediaService",
]
