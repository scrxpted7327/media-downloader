"""Owner-scoped PWA media jobs backed by the existing downloader queue."""

from __future__ import annotations

import asyncio
import hashlib
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
)
from .acquisition import (
    AcquisitionLifecycle,
    AcquisitionRequest,
    AcquisitionState,
    DownloadedMedia,
    ErrorEvent,
    ProgressEvent,
    PromotionResult as AcquisitionPromotionResult,
    ResultEvent,
)
from .acquisition_storage import AcquisitionStorage
from .library import (
    PRESET_SPECS,
    normalized_source_details,
    preset_extension,
    preset_for_job,
    probe_media,
    sha256_file,
)
from .shared_media_library import (
    AssetNotFoundError,
    LibraryPrincipal,
    SharedMediaLibrary,
    VariantNotFoundError,
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
    delete_job_with_artifacts,
    get_job_for_owner,
    get_job,
    get_media_asset,
    get_media_variant,
    list_jobs_for_owner,
    list_media_variants,
    media_library_storage_bytes,
    open_database,
    update_job,
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


class _PwaAcquisitionDownloader:
    """Downloader adapter that persists one shared claim output."""

    def __init__(self, service: "PwaMediaService") -> None:
        self.service = service

    async def download(self, identity, progress) -> DownloadedMedia:
        context = self.service._acquisition_context.get(
            (identity.source_key, identity.preset)
        )
        if context is None:
            raise DownloadError("acquisition execution context is unavailable")
        job, claim_id = context
        temporary = None
        try:
            if is_tiktok_photo_url(job.url):
                temporary, media = await download_tiktok_slideshow(
                    self.service.gallerydl,
                    job.url,
                    self.service.max_filesize_mb,
                    self.service.timeout_seconds,
                    progress,
                )
            elif is_instagram_url(job.url):
                temporary, media = await download_instagram(
                    self.service.gallerydl,
                    job.url,
                    self.service.max_filesize_mb,
                    self.service.timeout_seconds,
                    progress,
                    ytdlp=self.service.ytdlp,
                )
            else:
                try:
                    temporary, media = await download_media(
                        self.service.ytdlp,
                        job.url,
                        self.service.max_filesize_mb,
                        self.service.timeout_seconds,
                        progress,
                        format_name=job.requested_format or "video",
                        quality=job.requested_quality or "best",
                    )
                except DownloadError:
                    if not is_tiktok_url(job.url):
                        raise
                    temporary, media = await download_tiktok_slideshow(
                        self.service.gallerydl,
                        job.url,
                        self.service.max_filesize_mb,
                        self.service.timeout_seconds,
                        progress,
                    )

            details = normalized_source_details(media.parent, job.url)
            title = str(details.get("title") or "").strip() or None
            source_caption = str(details.get("source_caption") or "").strip() or None
            claim_key = hashlib.sha256(
                f"{identity.source_key}:{identity.preset}".encode("utf-8")
            ).hexdigest()
            acquisition_root = self.service.storage_dir / "acquisitions"
            acquisition_root.mkdir(parents=True, exist_ok=True)
            destination = acquisition_root / f"{claim_key}{media.suffix.lower()}"
            if not destination.exists():
                await asyncio.to_thread(shutil.copy2, media, destination)
            thumbnail = await create_thumbnail(
                destination,
                acquisition_root / f"{claim_key}-thumbnail.jpg",
            )
            metadata: dict[str, Any] = {
                "source_details": details,
                "claim_id": claim_id,
                "job_id": job.id,
                "title": title,
                "source_caption": source_caption,
                "owner_id": job.owner_id or job.owner_kind,
                "thumbnail_path": str(thumbnail) if thumbnail else None,
                "output_filename": destination.name,
                "output_mime_type": mimetypes.guess_type(destination.name)[0]
                or "application/octet-stream",
            }
            return DownloadedMedia(destination, metadata)
        finally:
            if temporary is not None:
                temporary.cleanup()


class _PwaAcquisitionPromoter:
    def __init__(self, service: "PwaMediaService") -> None:
        self.service = service

    async def promote(self, identity, media: DownloadedMedia) -> AcquisitionPromotionResult:
        details = media.metadata.get("source_details")
        if not isinstance(details, dict):
            details = {}
        principal = self.service._library_principal(
            str(media.metadata.get("owner_id") or "pwa-acquisition"),
            "media.library.promote",
        )
        duration = details.get("duration_seconds")
        result = await self.service.shared_library.promote(
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
        return AcquisitionPromotionResult(
            str(result.asset.id),
            str(result.variant.id),
            {"preset_key": result.variant.preset_key},
        )


class _PwaAcquisitionCancellation:
    def __init__(self, service: "PwaMediaService") -> None:
        self.service = service

    async def requested(self, acquisition_job_id: str) -> bool:
        job_id = self.service._acquisition_job_ids.get(acquisition_job_id)
        if job_id is None:
            return False
        job = await get_job(self.service.db_path, job_id)
        return bool(
            job is None
            or job.status == "cancelled"
            or self.service.work.cancellation_requested(self.service._label(job_id))
        )

    async def signal(self, acquisition_job_id: str) -> None:
        job_id = self.service._acquisition_job_ids.get(acquisition_job_id)
        if job_id is not None:
            await update_job(
                self.service.db_path,
                job_id,
                status="cancelled",
                phase="cancelled",
                error_code="cancelled_by_user",
            )


class _PwaAcquisitionProgress:
    def __init__(self, service: "PwaMediaService") -> None:
        self.service = service

    async def emit(self, event: ProgressEvent | ResultEvent | ErrorEvent) -> None:
        job_id = self.service._acquisition_job_ids.get(event.job_id)
        if job_id is None:
            return
        if isinstance(event, ProgressEvent):
            status = {
                AcquisitionState.QUEUED: "queued",
                AcquisitionState.RUNNING: "downloading",
                AcquisitionState.PROCESSING: "processing",
                AcquisitionState.COMPLETED: "processing",
                AcquisitionState.FAILED: "failed",
                AcquisitionState.CANCELLED: "cancelled",
            }[event.state]
            await update_job(
                self.service.db_path,
                job_id,
                status=status,
                phase=event.phase,
                progress_percent=(
                    float(max(0, min(99, event.percent)))
                    if event.percent is not None
                    else None
                ),
            )


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
        self.shared_library = SharedMediaLibrary(
            self.db_path,
            self.library_root,
            max_bytes=(
                library_max_size_mb * 1024 * 1024
                if library_max_size_mb > 0
                else None
            ),
            min_free_bytes=library_min_free_space_mb * 1024 * 1024,
        )
        self.acquisition_storage = AcquisitionStorage(self.db_path)
        self._acquisition_init_lock = asyncio.Lock()
        self._acquisition_initialized = False
        self._acquisition_context: dict[tuple[str, str], tuple[JobRecord, str]] = {}
        self._acquisition_job_ids: dict[str, int] = {}
        self.acquisition_lifecycle = AcquisitionLifecycle(
            persistence=self.acquisition_storage,
            downloader=_PwaAcquisitionDownloader(self),
            promoter=_PwaAcquisitionPromoter(self),
            cancellation=_PwaAcquisitionCancellation(self),
            progress=_PwaAcquisitionProgress(self),
        )

    @staticmethod
    def _library_principal(
        principal_id: str,
        *capabilities: str,
    ) -> LibraryPrincipal:
        return LibraryPrincipal(principal_id, frozenset(capabilities))

    async def _ensure_acquisition_storage(self) -> None:
        if self._acquisition_initialized:
            return
        async with self._acquisition_init_lock:
            if not self._acquisition_initialized:
                await self.acquisition_storage.init()
                self._acquisition_initialized = True

    async def _admit_acquisition(self, job: JobRecord):
        await self._ensure_acquisition_storage()
        admission = await self.acquisition_lifecycle.submit(
            AcquisitionRequest(
                requester_id=f"{job.owner_id or job.owner_kind}:{job.id}",
                source_url=job.url,
                preset=preset_for_job(job.requested_format, job.requested_quality),
            )
        )
        self._acquisition_job_ids[admission.job_id] = job.id
        return admission

    @staticmethod
    def _acquisition_id(job: JobRecord) -> str | None:
        if not job.output_metadata:
            return None
        try:
            metadata = json.loads(job.output_metadata)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        value = metadata.get("acquisition_job_id") if isinstance(metadata, dict) else None
        return str(value) if value else None

    async def resume_acquisition_work(self) -> int:
        """Reconcile and re-admit durable PWA acquisition work after startup."""
        await self._ensure_acquisition_storage()
        await self.acquisition_lifecycle.reconcile()
        async with open_database(self.db_path) as db:
            async with db.execute(
                "SELECT id, owner_id FROM jobs "
                "WHERE owner_kind = ? AND status IN ('queued', 'downloading', 'processing') "
                "ORDER BY created_at, id",
                (PWA_OWNER_KIND,),
            ) as cursor:
                rows = await cursor.fetchall()
        resumed = 0
        for row in rows:
            if not row["owner_id"]:
                continue
            label = self._label(int(row["id"]))
            if self.work.has_label(label):
                continue
            try:
                self.work.submit(
                    user_id=str(row["owner_id"]),
                    label=label,
                    factory=lambda job_id=int(row["id"]), owner=str(row["owner_id"]): self._process_job(job_id, owner),
                )
                resumed += 1
            except WorkRejected as exc:
                LOGGER.warning("Could not resume PWA acquisition %s: %s", row["id"], exc)
        return resumed

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
            admission = await self._admit_acquisition(job)
            output_metadata = {
                "acquisition_job_id": admission.job_id,
                "acquisition_claim_id": admission.claim_id,
            }
            job = await update_job(
                self.db_path,
                job.id,
                output_metadata=json.dumps(
                    output_metadata, sort_keys=True, separators=(",", ":")
                ),
            )
            assert job is not None
            self.work.submit(
                user_id=owner_id,
                label=self._label(job.id),
                factory=lambda job_id=job.id, owner=owner_id: self._process_job(job_id, owner),
            )
        except Exception as exc:
            LOGGER.exception("Could not admit or queue PWA job %s", job.id)
            job = await update_job(
                self.db_path,
                job.id,
                status="failed",
                phase="failed",
                error_code=(
                    "queue_rejected"
                    if isinstance(exc, (WorkRejected, WorkAlreadyQueued))
                    else "acquisition_admission_failed"
                ),
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
        acquisition_job_id = self._acquisition_id(job)
        if acquisition_job_id is not None:
            await self._ensure_acquisition_storage()
            self._acquisition_job_ids[acquisition_job_id] = job.id
            try:
                await self.acquisition_lifecycle.cancel(acquisition_job_id)
            except KeyError:
                LOGGER.debug(
                    "Acquisition requester %s was already removed", acquisition_job_id
                )
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
        self,
        *,
        principal_id: str = "pwa-library",
        limit: int = 50,
        query: str | None = None,
        sort: str | None = None,
    ) -> list[tuple[Any, list[Any]]]:
        bundles = await self.shared_library.list(
            self._library_principal(principal_id, "media.library.read"),
            limit=limit,
            query=query,
            sort=sort,
        )
        return [(bundle.asset, list(bundle.variants)) for bundle in bundles]

    async def get_library_asset(
        self, *, asset_id: int, principal_id: str = "pwa-library"
    ) -> tuple[Any, list[Any]] | None:
        try:
            bundle = await self.shared_library.read(
                self._library_principal(principal_id, "media.library.read"),
                asset_id,
            )
        except AssetNotFoundError:
            return None
        return bundle.asset, list(bundle.variants)

    async def request_variant(
        self, *, requester_id: str, asset_id: int, preset_key: str
    ) -> tuple[Any, bool]:
        result = await self.shared_library.request_variant(
            self._library_principal(
                requester_id,
                "media.library.read",
                "media.library.variant_request",
            ),
            asset_id,
            preset_key,
        )
        variant, created = result.variant, result.created
        if variant.status == "ready":
            return variant, created
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
        destination = self.library_root / "assets" / str(asset.id) / (
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
        self, *, asset_id: int, principal_id: str = "pwa-library-manager"
    ) -> tuple[Any | None, list[Any]]:
        asset = await get_media_asset(self.db_path, asset_id)
        if asset is None or asset.status == "deleted":
            return None, []
        variants = await list_media_variants(self.db_path, asset.id)
        await self.shared_library.delete(
            self._library_principal(
                principal_id, "media.library.manage"
            ),
            asset_id,
        )
        return asset, variants

    async def delete_library_variant(
        self, *, variant_id: int, principal_id: str = "pwa-library-manager"
    ) -> Any | None:
        try:
            return await self.shared_library.delete_variant(
                self._library_principal(
                    principal_id, "media.library.manage"
                ),
                variant_id,
            )
        except (AssetNotFoundError, VariantNotFoundError):
            return None

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

    async def _process_job(self, job_id: int, owner_id: str) -> None:
        """Run one PWA requester through the shared acquisition lifecycle."""
        LOGGER.info("Starting PWA acquisition job %s for %s", job_id, owner_id)
        job = await self.get_job(owner_id=owner_id, job_id=job_id)
        if job is None or job.status == "cancelled":
            return
        acquisition_job_id = self._acquisition_id(job)
        claim = None
        try:
            if acquisition_job_id is None:
                admission = await self._admit_acquisition(job)
                acquisition_job_id = admission.job_id
                job = await update_job(
                    self.db_path,
                    job.id,
                    output_metadata=json.dumps(
                        {
                            "acquisition_job_id": acquisition_job_id,
                            "acquisition_claim_id": admission.claim_id,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                assert job is not None
            self._acquisition_job_ids[acquisition_job_id] = job.id
            requester = await self.acquisition_storage.get_requester(acquisition_job_id)
            if requester is None:
                raise DownloadError("durable acquisition requester is missing")
            claim = await self.acquisition_storage.get_claim(requester.claim_id)
            if claim is None:
                raise DownloadError("durable acquisition claim is missing")
            self._acquisition_context[(claim.identity.source_key, claim.identity.preset)] = (
                job,
                claim.claim_id,
            )
            await update_job(
                self.db_path,
                job.id,
                status="queued",
                phase="queued",
                progress_percent=0.0,
                started_at=_now_iso(),
                error_code=None,
                error_message=None,
            )
            result = await self.acquisition_lifecycle.run(acquisition_job_id)
            claim = await self.acquisition_storage.get_claim(requester.claim_id)
            if result.state == AcquisitionState.CANCELLED:
                await update_job(
                    self.db_path,
                    job.id,
                    status="cancelled",
                    phase="cancelled",
                    error_code="cancelled_by_user",
                )
                return
            if result.state == AcquisitionState.FAILED or claim is None or claim.output is None:
                await update_job(
                    self.db_path,
                    job.id,
                    status="failed",
                    phase="failed",
                    error_code=result.error_code or "download_failed",
                    error_message=result.error_message or "acquisition produced no output",
                    failed_at=_now_iso(),
                )
                return

            output = claim.output
            details = output.metadata.get("source_details")
            if not isinstance(details, dict):
                details = {}
            metadata: dict[str, Any] = {}
            if job.output_metadata:
                try:
                    parsed = json.loads(job.output_metadata)
                    if isinstance(parsed, dict):
                        metadata.update(parsed)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            title = str(details.get("title") or "").strip() or None
            source_caption = str(details.get("source_caption") or "").strip() or None
            if title:
                metadata["title"] = title
            if source_caption:
                metadata["source_caption"] = source_caption
            if result.output is not None:
                metadata.update(
                    {
                        "library_asset_id": result.output.asset_id,
                        "library_variant_id": result.output.variant_id,
                        "library_preset": result.output.metadata.get(
                            "preset_key", claim.identity.preset
                        ),
                    }
                )
            else:
                metadata["library_promotion_status"] = "pending_or_failed"
            await update_job(
                self.db_path,
                job.id,
                status="completed",
                phase="completed",
                progress_percent=100.0,
                file_path=str(output.path),
                file_size=output.path.stat().st_size,
                title=title,
                source_caption=source_caption,
                thumbnail_path=output.metadata.get("thumbnail_path"),
                output_filename=output.metadata.get("output_filename") or output.path.name,
                output_mime_type=output.metadata.get("output_mime_type"),
                output_metadata=json.dumps(
                    metadata, sort_keys=True, separators=(",", ":")
                ),
                completed_at=_now_iso(),
                error_code=None,
                error_message=None,
            )
        except asyncio.CancelledError:
            current = await self.get_job(owner_id=owner_id, job_id=job_id)
            explicitly_cancelled = self.work.cancellation_requested(self._label(job_id))
            if explicitly_cancelled or (current is not None and current.status == "cancelled"):
                await update_job(
                    self.db_path,
                    job_id,
                    status="cancelled",
                    phase="cancelled",
                    error_code="cancelled_by_user",
                )
            else:
                await update_job(
                    self.db_path,
                    job_id,
                    status="queued",
                    phase="queued",
                    error_code=None,
                    error_message=None,
                )
            raise
        except Exception as exc:
            LOGGER.exception("PWA acquisition failed for job %s", job_id)
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
            if acquisition_job_id is not None:
                self._acquisition_job_ids.pop(acquisition_job_id, None)
            if claim is not None:
                self._acquisition_context.pop(
                    (claim.identity.source_key, claim.identity.preset), None
                )

__all__ = [
    "PWA_OWNER_KIND",
    "PWA_SOURCE_CHANNEL",
    "SUPPORTED_FORMATS",
    "SUPPORTED_QUALITIES",
    "PwaMediaService",
]
