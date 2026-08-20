"""Deep interface for the authenticated shared media library.

The module deliberately owns policy around shared assets while leaving channel
adapters and media transformation engines outside its seam.  The existing
SQLite schema is used as-is; this module only composes the storage primitives.
"""

from __future__ import annotations

import asyncio
import inspect
import mimetypes
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Iterable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .library import PRESET_SPECS, preset_extension, probe_media, sha256_file
from .storage import (
    MediaAsset,
    MediaVariant,
    create_or_get_media_asset,
    create_or_get_media_variant,
    get_media_asset,
    get_media_variant,
    list_media_assets,
    list_media_variants,
    media_library_storage_bytes,
    open_database,
    update_media_asset,
    update_media_variant,
)


TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class LibraryError(Exception):
    """Base error with a stable machine-readable code."""

    code = "library_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AuthorizationError(LibraryError):
    code = "forbidden"


class InvalidPresetError(LibraryError):
    code = "unsupported_preset"


class AssetNotFoundError(LibraryError):
    code = "asset_not_found"


class VariantNotFoundError(LibraryError):
    code = "variant_not_found"


class UnsafeLibraryPathError(LibraryError):
    code = "unsafe_path"


class CapacityError(LibraryError):
    code = "capacity_exceeded"


class PromotionError(LibraryError):
    code = "promotion_failed"


class VariantPendingError(LibraryError):
    code = "variant_pending"


@dataclass(frozen=True)
class LibraryPrincipal:
    """Minimal principal accepted by the default authorization policy."""

    principal_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def can(self, operation: str) -> bool:
        aliases = {
            "read": ("read", "media.library.read"),
            "variant_request": ("variant_request", "media.library.variant_request"),
            "promote": (
                "promote",
                "media.library.promote",
                "media.library.manage",
            ),
            "delete": ("delete", "media.library.manage"),
        }
        return bool(self.capabilities.intersection(aliases.get(operation, (operation,))))


class Authorizer(Protocol):
    def __call__(
        self, principal: LibraryPrincipal, operation: str, asset: MediaAsset | None
    ) -> bool | Awaitable[bool]: ...


@dataclass(frozen=True)
class SourceIdentity:
    source_key: str
    canonical_url: str
    platform: str | None
    media_id: str | None


@dataclass(frozen=True)
class SafeMediaFile:
    """A caller-facing file reference proven to remain below the library root."""

    path: Path
    size: int
    mime_type: str | None

    def open(self, mode: str = "rb"):
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            raise ValueError("library files are read-only through this handle")
        return self.path.open(mode)


@dataclass(frozen=True)
class PromotionResult:
    asset: MediaAsset
    variant: MediaVariant
    created_asset: bool
    created_variant: bool
    idempotent: bool


@dataclass(frozen=True)
class LibraryRead:
    asset: MediaAsset
    variants: tuple[MediaVariant, ...]


@dataclass(frozen=True)
class VariantRequestResult:
    variant: MediaVariant
    created: bool
    source_variant: MediaVariant | None


def canonical_source_url(value: str) -> str:
    """Return stable URL identity, excluding known tracking parameters."""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip()
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/",
         urlencode(query), "")
    )


def source_identity(
    source_url: str, *, platform: str | None = None, media_id: str | None = None
) -> SourceIdentity:
    canonical = canonical_source_url(source_url)
    clean_platform = platform.strip().lower() if platform and platform.strip() else None
    clean_id = media_id.strip() if media_id and media_id.strip() else None
    key = f"native:{clean_platform or 'unknown'}:{clean_id}" if clean_id else f"url:{canonical}"
    return SourceIdentity(key, canonical, clean_platform, clean_id)


def validate_preset(preset_key: str) -> dict[str, object]:
    spec = PRESET_SPECS.get(preset_key)
    if spec is None:
        raise InvalidPresetError(f"unsupported media library preset: {preset_key}")
    return dict(spec)


def _contained(root: Path, candidate: Path) -> Path:
    resolved_root = root.expanduser().resolve(strict=False)
    resolved = candidate.expanduser().resolve(strict=False)
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise UnsafeLibraryPathError(f"path is outside shared library root: {resolved}")
    return resolved


async def _copy_atomically(source: Path, destination: Path) -> None:
    def copy() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.part"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(copy)


def _remove_file_if_present(path: Path) -> None:
    """Remove a failed destination without following a path through a file."""
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except (FileNotFoundError, NotADirectoryError):
        # A parent may have been replaced by a file while the copy failed.
        return


def _mime(path: Path) -> str | None:
    return mimetypes.guess_type(path.name)[0]


class SharedMediaLibrary:
    """Small interface hiding shared-media identity, files, and policy."""

    def __init__(
        self,
        db_path: Path,
        library_root: Path,
        *,
        authorizer: Authorizer | None = None,
        max_bytes: int | None = None,
        min_free_bytes: int = 0,
    ) -> None:
        self.db_path = Path(db_path)
        self.library_root = Path(library_root).expanduser().resolve(strict=False)
        self.authorizer = authorizer
        self.max_bytes = max_bytes
        self.min_free_bytes = max(0, int(min_free_bytes))
        self._lock = asyncio.Lock()

    async def _allowed(
        self, principal: LibraryPrincipal, operation: str, asset: MediaAsset | None = None
    ) -> None:
        result: bool | Awaitable[bool]
        if self.authorizer is None:
            result = principal.can(operation)
        else:
            result = self.authorizer(principal, operation, asset)
        if inspect.isawaitable(result):
            result = await result
        if not result:
            raise AuthorizationError(f"library {operation} permission is required")

    async def _asset(self, asset_id: int) -> MediaAsset:
        asset = await get_media_asset(self.db_path, int(asset_id))
        if asset is None or asset.status == "deleted":
            raise AssetNotFoundError(f"shared asset {asset_id} was not found")
        return asset

    async def read(self, principal: LibraryPrincipal, asset_id: int) -> LibraryRead:
        asset = await self._asset(asset_id)
        await self._allowed(principal, "read", asset)
        variants = await list_media_variants(self.db_path, asset.id)
        await update_media_asset(self.db_path, asset.id, last_accessed_at=_now())
        return LibraryRead(asset, tuple(variants))

    async def list(
        self,
        principal: LibraryPrincipal,
        *,
        limit: int = 50,
        query: str | None = None,
        sort: str | None = None,
    ) -> tuple[LibraryRead, ...]:
        """List readable shared assets without exposing storage paths."""
        await self._allowed(principal, "read")
        assets = await list_media_assets(
            self.db_path, limit=limit, query=query, sort=sort
        )
        bundles: list[LibraryRead] = []
        for asset in assets:
            bundles.append(
                LibraryRead(
                    asset,
                    tuple(await list_media_variants(self.db_path, asset.id)),
                )
            )
        return tuple(bundles)

    async def request_variant(
        self, principal: LibraryPrincipal, asset_id: int, preset_key: str
    ) -> VariantRequestResult:
        validate_preset(preset_key)
        asset = await self._asset(asset_id)
        await self._allowed(principal, "variant_request", asset)
        async with self._lock:
            source = await self._select_source_variant(asset.id)
            if source is None:
                raise VariantPendingError("no ready source variant is available")
            variant, created = await create_or_get_media_variant(
                self.db_path, asset_id=asset.id, preset_key=preset_key,
                status="queued", source_variant_id=source.id,
            )
            if not created and variant.status == "failed":
                variant = await update_media_variant(
                    self.db_path, variant.id, status="queued", error_code=None,
                    error_message=None, source_variant_id=source.id,
                ) or variant
            return VariantRequestResult(variant, created, source)

    async def promote(
        self,
        principal: LibraryPrincipal,
        source_file: Path,
        source_url: str,
        *,
        preset_key: str = "video_best",
        source_platform: str | None = None,
        source_media_id: str | None = None,
        title: str | None = None,
        uploader: str | None = None,
        duration_seconds: float | None = None,
        upload_date: str | None = None,
        thumbnail_file: Path | None = None,
        created_from_job_id: int | None = None,
    ) -> PromotionResult:
        validate_preset(preset_key)
        await self._allowed(principal, "promote")
        source = Path(source_file).expanduser().resolve(strict=True)
        if not source.is_file():
            raise PromotionError("promotion source is not a regular file", code="source_missing")
        size = source.stat().st_size
        if size <= 0:
            raise PromotionError("promotion source is empty", code="empty_source")
        identity = source_identity(source_url, platform=source_platform, media_id=source_media_id)
        async with self._lock:
            await self._check_capacity(size)
            asset, created_asset = await create_or_get_media_asset(
                self.db_path, source_platform=identity.platform,
                source_media_id=identity.media_id, source_key=identity.source_key,
                source_canonical_url=identity.canonical_url, title=title, uploader=uploader,
                duration_seconds=duration_seconds, upload_date=upload_date,
                thumbnail_path=None, created_from_job_id=created_from_job_id,
                created_by_owner_id=principal.principal_id,
            )
            existing, created_variant = await create_or_get_media_variant(
                self.db_path, asset_id=asset.id, preset_key=preset_key, status="queued"
            )
            if existing.status == "ready" and existing.file_path:
                try:
                    ready_path = _contained(self.library_root, Path(existing.file_path))
                    if ready_path.is_file() and (existing.sha256 or "") == await sha256_file(ready_path):
                        return PromotionResult(asset, existing, created_asset, created_variant, True)
                except (LibraryError, FileNotFoundError):
                    pass
                existing = await update_media_variant(
                    self.db_path, existing.id, status="queued", file_path=None,
                    file_size=None, sha256=None, error_code=None, error_message=None,
                ) or existing
            destination_dir = self.library_root / "assets" / str(asset.id)
            suffix = source.suffix or preset_extension(preset_key)
            destination = _contained(
                self.library_root,
                destination_dir / f"{preset_key}{suffix.lower()}",
            )
            try:
                await _copy_atomically(source, destination)
                if thumbnail_file is not None and thumbnail_file.is_file():
                    thumbnail = _contained(
                        self.library_root, destination_dir / "thumbnail.jpg"
                    )
                    await _copy_atomically(
                        thumbnail_file.expanduser().resolve(strict=True), thumbnail
                    )
                    await update_media_asset(
                        self.db_path, asset.id, thumbnail_path=str(thumbnail)
                    )
                metadata = await probe_media(destination)
                digest = await sha256_file(destination)
                if destination.stat().st_size != size:
                    raise PromotionError("promoted size changed during copy", code="size_mismatch")
                variant = await update_media_variant(
                    self.db_path, existing.id, status="ready", file_path=str(destination),
                    file_size=size, mime_type=_mime(destination),
                    container=metadata.get("container"), video_codec=metadata.get("video_codec"),
                    audio_codec=metadata.get("audio_codec"), width=metadata.get("width"),
                    height=metadata.get("height"), duration_seconds=metadata.get("duration_seconds"),
                    sha256=digest, error_code=None, error_message=None,
                )
                if variant is None:
                    raise PromotionError("variant disappeared during promotion", code="reservation_lost")
                return PromotionResult(asset, variant, created_asset, created_variant, False)
            except LibraryError:
                _remove_file_if_present(destination)
                await update_media_variant(
                    self.db_path, existing.id, status="failed", file_path=None,
                    file_size=None, error_code="promotion_failed", error_message="promotion failed",
                )
                raise
            except Exception as exc:
                _remove_file_if_present(destination)
                await update_media_variant(
                    self.db_path, existing.id, status="failed", file_path=None,
                    file_size=None, error_code="promotion_failed", error_message=str(exc)[:500],
                )
                raise PromotionError("promotion failed", code="promotion_failed") from exc

    async def open_variant(
        self, principal: LibraryPrincipal, asset_id: int, variant_id: int | None = None
    ) -> SafeMediaFile:
        read = await self.read(principal, asset_id)
        variant = await self._select_requested(read.variants, variant_id)
        if variant.status != "ready" or not variant.file_path:
            raise VariantPendingError("requested media variant is not ready")
        path = _contained(self.library_root, Path(variant.file_path))
        if not path.is_file():
            raise VariantNotFoundError("requested media variant file is missing")
        return SafeMediaFile(path, path.stat().st_size, variant.mime_type or _mime(path))

    async def delete(self, principal: LibraryPrincipal, asset_id: int) -> MediaAsset:
        asset = await self._asset(asset_id)
        await self._allowed(principal, "delete", asset)
        variants = await list_media_variants(self.db_path, asset.id)
        await update_media_asset(self.db_path, asset.id, status="deleted")
        for variant in variants:
            await update_media_variant(self.db_path, variant.id, status="deleted")
        return asset

    async def delete_variant(
        self, principal: LibraryPrincipal, variant_id: int
    ) -> MediaVariant:
        variant = await get_media_variant(self.db_path, int(variant_id))
        if variant is None:
            raise VariantNotFoundError(f"shared variant {variant_id} was not found")
        asset = await self._asset(variant.asset_id)
        await self._allowed(principal, "delete", asset)
        updated = await update_media_variant(
            self.db_path, variant.id, status="deleted"
        )
        if updated is None:
            raise VariantNotFoundError(f"shared variant {variant_id} was not found")
        return updated

    async def cleanup_deleted(
        self, principal: LibraryPrincipal, asset_id: int | None = None
    ) -> tuple[Path, ...]:
        """Reclaim only canonical files belonging to logically deleted assets.

        Requester job files are outside this module's records and are never
        considered.  A contained-path check is applied again before unlinking
        so stale database paths cannot turn retention into arbitrary deletion.
        """
        await self._allowed(principal, "delete")
        params: tuple[object, ...] = () if asset_id is None else (int(asset_id),)
        query = (
            "SELECT a.id, v.file_path FROM media_assets a "
            "JOIN media_variants v ON v.asset_id = a.id "
            "WHERE a.status = 'deleted' AND v.status = 'deleted'"
        )
        if asset_id is not None:
            query += " AND a.id = ?"
        async with open_database(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        removed: list[Path] = []
        for row in rows:
            raw_path = row["file_path"]
            if not raw_path:
                continue
            try:
                path = _contained(self.library_root, Path(raw_path))
            except UnsafeLibraryPathError:
                continue
            if path.is_file():
                path.unlink()
                removed.append(path)
        return tuple(removed)

    async def fail_variant(
        self, principal: LibraryPrincipal, variant_id: int, *, code: str, message: str
    ) -> MediaVariant:
        variant = await get_media_variant(self.db_path, variant_id)
        if variant is None:
            raise VariantNotFoundError(f"shared variant {variant_id} was not found")
        asset = await self._asset(variant.asset_id)
        await self._allowed(principal, "variant_request", asset)
        updated = await update_media_variant(
            self.db_path, variant.id, status="failed", error_code=code,
            error_message=message[:500],
        )
        if updated is None:
            raise VariantNotFoundError(f"shared variant {variant_id} was not found")
        return updated

    async def _select_source_variant(self, asset_id: int) -> MediaVariant | None:
        variants = await list_media_variants(self.db_path, asset_id)
        return next((item for item in variants if item.status == "ready" and item.file_path), None)

    @staticmethod
    async def _select_requested(
        variants: Iterable[MediaVariant], variant_id: int | None
    ) -> MediaVariant:
        items = tuple(variants)
        if variant_id is not None:
            for item in items:
                if item.id == int(variant_id):
                    return item
            raise VariantNotFoundError("requested media variant was not found")
        for item in items:
            if item.status == "ready":
                return item
        raise VariantPendingError("no ready media variant is available")

    async def _check_capacity(self, incoming: int) -> None:
        usage = await media_library_storage_bytes(self.db_path)
        if self.max_bytes is not None and usage + incoming > self.max_bytes:
            raise CapacityError("shared media capacity would be exceeded")
        self.library_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.library_root).free
        if free < incoming + self.min_free_bytes:
            raise CapacityError("insufficient free space for shared media promotion")


def _now() -> str:
    # Storage accepts SQLite's ISO-compatible text for last_accessed_at.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AssetNotFoundError", "AuthorizationError", "CapacityError", "InvalidPresetError",
    "LibraryError", "LibraryPrincipal", "LibraryRead", "PromotionError", "PromotionResult",
    "SafeMediaFile", "SharedMediaLibrary", "SourceIdentity", "UnsafeLibraryPathError",
    "VariantNotFoundError", "VariantPendingError", "VariantRequestResult",
    "canonical_source_url", "source_identity", "validate_preset",
]
