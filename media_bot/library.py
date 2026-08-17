"""Durable authenticated shared-media library primitives.

The library is deliberately separate from Telegram's Pool.  Pool rows remain
user/Telegram-owned; these rows represent authenticated shared assets created
from completed watchMyWallet PWA jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .downloader import read_source_details
from .storage import MediaAsset, MediaVariant

TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
PRESET_SPECS: dict[str, dict[str, object]] = {
    "video_best": {"kind": "video", "label": "Best available", "extension": ".mp4"},
    "video_1080p": {"kind": "video", "label": "1080p", "height": 1080, "extension": ".mp4"},
    "video_720p": {"kind": "video", "label": "720p", "height": 720, "extension": ".mp4"},
    "video_480p": {"kind": "video", "label": "480p", "height": 480, "extension": ".mp4"},
    "audio_mp3": {"kind": "audio", "label": "MP3", "codec": "mp3", "extension": ".mp3"},
    "audio_m4a": {"kind": "audio", "label": "M4A (AAC)", "codec": "aac", "extension": ".m4a"},
}


def canonical_source_url(value: str) -> str:
    """Normalize a source URL without treating raw submitted text as identity."""
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
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def normalized_source_details(directory: Path, fallback_url: str) -> dict[str, object]:
    details = read_source_details(directory)
    canonical = canonical_source_url(
        str(details.get("source_canonical_url") or fallback_url)
    )
    platform = str(details.get("source_platform") or "").strip().lower() or None
    media_id = str(details.get("source_media_id") or "").strip() or None
    details["source_canonical_url"] = canonical
    details["source_platform"] = platform
    details["source_media_id"] = media_id
    if not media_id:
        details["source_key"] = f"url:{canonical}"
    else:
        details["source_key"] = f"native:{platform or 'unknown'}:{media_id}"
    return details


def preset_for_job(requested_format: str | None, requested_quality: str | None) -> str:
    if requested_format == "audio":
        return "audio_mp3"
    quality = requested_quality or "best"
    return "video_best" if quality == "best" else f"video_{quality}"


def preset_extension(preset_key: str, source_suffix: str = ".mp4") -> str:
    spec = PRESET_SPECS.get(preset_key)
    if spec is None:
        raise ValueError("unsupported media library preset")
    if preset_key == "video_best" and source_suffix:
        return source_suffix.lower()
    return str(spec.get("extension") or source_suffix or ".media")


async def sha256_file(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)


async def probe_media(path: Path) -> dict[str, object]:
    """Probe media with ffprobe when present; absence is a valid partial state."""
    if shutil.which("ffprobe") is None:
        return {"file_size": path.stat().st_size}
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"file_size": path.stat().st_size}
    if process.returncode != 0:
        return {"file_size": path.stat().st_size}
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"file_size": path.stat().st_size}
    streams = payload.get("streams") if isinstance(payload, dict) else []
    format_data = payload.get("format") if isinstance(payload, dict) else {}
    video = next((item for item in streams or [] if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams or [] if item.get("codec_type") == "audio"), {})
    result: dict[str, object] = {
        "file_size": path.stat().st_size,
        "container": str(format_data.get("format_name") or "").split(",")[0] or None,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
    }
    duration = format_data.get("duration")
    try:
        result["duration_seconds"] = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        result["duration_seconds"] = None
    return result


def asset_payload(asset: MediaAsset, variants: list[MediaVariant]) -> dict[str, object]:
    """Return fields safe for all authenticated library readers."""
    return {
        "asset_id": str(asset.id),
        "scope": asset.scope,
        "status": asset.status,
        "source_platform": asset.source_platform,
        "source_canonical_url": asset.source_canonical_url,
        "has_thumbnail": bool(asset.thumbnail_path),
        "title": asset.title or asset.source_canonical_url,
        "uploader": asset.uploader,
        "duration_seconds": asset.duration_seconds,
        "upload_date": asset.upload_date,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
        "variants": [variant_payload(item) for item in variants],
    }


def variant_payload(variant: MediaVariant) -> dict[str, object]:
    return {
        "variant_id": str(variant.id),
        "preset_key": variant.preset_key,
        "label": PRESET_SPECS.get(variant.preset_key, {}).get("label", variant.preset_key),
        "status": variant.status,
        "file_size": variant.file_size,
        "mime_type": variant.mime_type,
        "container": variant.container,
        "width": variant.width,
        "height": variant.height,
        "duration_seconds": variant.duration_seconds,
        "created_at": variant.created_at.isoformat(),
        "updated_at": variant.updated_at.isoformat(),
        "error_code": variant.error_code,
    }


__all__ = [
    "PRESET_SPECS",
    "asset_payload",
    "canonical_source_url",
    "normalized_source_details",
    "preset_extension",
    "preset_for_job",
    "probe_media",
    "sha256_file",
    "variant_payload",
]
