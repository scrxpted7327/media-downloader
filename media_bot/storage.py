from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobRecord:
    id: int
    url: str
    user_id: int
    chat_id: int
    status: str
    file_path: str | None
    file_size: int | None
    local_api_used: bool
    status_message_id: int | None
    title: str | None
    source_caption: str | None
    thumbnail_path: str | None
    created_at: datetime
    updated_at: datetime
    error_message: str | None


@dataclass(frozen=True)
class DownloadToken:
    token_hash: str
    job_id: int
    edit_job_id: int | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    user_id: int


@dataclass(frozen=True)
class UserSettings:
    user_id: int
    preset_name: str | None
    crop_preset: str | None
    caption_text: str | None
    voice_over_voice: str | None
    updated_at: datetime


@dataclass(frozen=True)
class Preset:
    id: int
    user_id: int
    name: str
    crop_preset: str | None
    caption_text: str | None
    voice_over_voice: str | None
    voice_mode: str | None
    caption_color: str | None
    caption_style: str | None
    caption_position: str | None
    auto_captions: bool
    voice_quality: str | None
    voice_speed: float | None
    voice_text: str | None
    tts_engine: str | None
    banner_path: str | None
    banner_position: str | None
    banner_scale: str | None
    watermark_removal: bool
    watermark_position: str | None
    watermark_mode: str | None
    watermark_text: str | None
    channel_banner: bool
    shared: bool
    share_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EditJob:
    id: int
    source_job_id: int
    user_id: int
    preset_id: int | None
    caption_text: str | None
    caption_color: str | None
    caption_style: str | None
    caption_position: str | None
    auto_captions: bool
    voice_text: str | None
    voice_over_voice: str | None
    voice_mode: str | None
    voice_quality: str | None
    voice_speed: float | None
    tts_engine: str | None
    banner_path: str | None
    banner_position: str | None
    banner_scale: str | None
    watermark_removal: bool
    watermark_position: str | None
    watermark_mode: str | None
    watermark_text: str | None
    watermark_analysis: str | None
    watermark_confidence: float | None
    watermark_candidates: str | None
    watermark_preview_path: str | None
    channel_banner: bool
    subtitles_path: str | None
    status: str
    file_path: str | None
    file_size: int | None
    created_at: datetime
    updated_at: datetime
    error_message: str | None
    metadata_status: str
    metadata_description: str | None
    metadata_hashtags: str | None
    metadata_error: str | None
    metadata_attempt_count: int
    metadata_requested_at: datetime | None
    metadata_started_at: datetime | None
    metadata_completed_at: datetime | None
    metadata_model: str | None
    metadata_reasoning_effort: str | None
    metadata_progress_message_id: int | None
    metadata_result_message_id: int | None
    metadata_reply_message_id: int | None
    render_delivery_message_id: int | None
    render_status_message_id: int | None


@dataclass(frozen=True)
class SharedPreset:
    id: int
    preset_id: int
    user_id: int
    share_code: str
    created_at: datetime


@dataclass(frozen=True)
class PoolItem:
    id: int
    user_id: int
    source_job_id: int | None
    edit_job_id: int | None
    file_path: str
    file_size: int | None
    thumbnail_path: str | None
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Classification:
    id: int
    name: str
    description: str | None
    color: str | None
    created_at: datetime


@dataclass(frozen=True)
class PoolTag:
    id: int
    pool_item_id: int
    classification_id: int
    user_id: int
    created_at: datetime


@dataclass(frozen=True)
class Workflow:
    id: int
    user_id: int
    name: str
    trigger_classification_id: int | None
    action_type: str
    action_preset_id: int | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    workflow_id: int
    pool_item_id: int
    user_id: int
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CleanupResult:
    """Summary returned by reference-aware storage cleanup operations."""

    records_deleted: int = 0
    files_deleted: int = 0
    files_preserved: int = 0
    unsafe_paths: tuple[str, ...] = ()

    def merge(self, other: CleanupResult) -> CleanupResult:
        return CleanupResult(
            records_deleted=self.records_deleted + other.records_deleted,
            files_deleted=self.files_deleted + other.files_deleted,
            files_preserved=self.files_preserved + other.files_preserved,
            unsafe_paths=(*self.unsafe_paths, *other.unsafe_paths),
        )


class UnsafeStoragePath(ValueError):
    """Raised when an artifact operation would escape the configured storage root."""


SQLITE_BUSY_TIMEOUT_MS = 5_000
LATEST_SCHEMA_VERSION = 6

_JOB_UPDATE_FIELDS = frozenset({
    "status", "file_path", "file_size", "local_api_used", "status_message_id",
    "title", "source_caption", "thumbnail_path", "error_message",
})
_USER_SETTINGS_UPDATE_FIELDS = frozenset({
    "preset_name", "crop_preset", "caption_text", "voice_over_voice",
})
_PRESET_UPDATE_FIELDS = frozenset({
    "name", "crop_preset", "caption_text", "voice_over_voice", "voice_mode", "caption_color",
    "caption_style", "caption_position", "auto_captions", "voice_quality",
    "voice_speed", "voice_text", "tts_engine", "banner_path", "banner_position",
    "banner_scale", "watermark_removal", "watermark_position", "watermark_mode",
    "watermark_text", "channel_banner",
})
_EDIT_UPDATE_FIELDS = frozenset({
    "preset_id", "caption_text", "caption_color", "caption_style",
    "caption_position", "auto_captions", "voice_text", "voice_over_voice",
    "voice_mode", "voice_quality", "voice_speed", "tts_engine", "banner_path",
    "banner_position", "banner_scale", "watermark_removal",
    "watermark_position", "watermark_mode", "watermark_text",
    "watermark_analysis", "watermark_confidence", "watermark_candidates",
    "watermark_preview_path", "channel_banner", "subtitles_path", "status",
    "file_path", "file_size", "error_message",
    "metadata_status", "metadata_description", "metadata_hashtags",
    "metadata_error", "metadata_attempt_count", "metadata_requested_at",
    "metadata_started_at", "metadata_completed_at", "metadata_model",
    "metadata_reasoning_effort", "metadata_progress_message_id",
    "metadata_result_message_id", "metadata_reply_message_id",
    "render_delivery_message_id", "render_status_message_id",
})
_POOL_UPDATE_FIELDS = frozenset({"title", "status"})
_WORKFLOW_UPDATE_FIELDS = frozenset({
    "name", "trigger_classification_id", "action_type", "action_preset_id", "enabled",
})
_WORKFLOW_RUN_UPDATE_FIELDS = frozenset({"status", "error_message"})


def _validate_update_fields(
    table: str, values: dict[str, object], allowed: frozenset[str],
) -> None:
    invalid = set(values).difference(allowed)
    if invalid:
        names = ", ".join(sorted(invalid))
        raise ValueError(f"unsupported {table} update field(s): {names}")


@asynccontextmanager
async def open_database(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open a consistently configured SQLite connection.

    Foreign-key enforcement is a per-connection SQLite setting, so every storage
    operation must pass through this helper rather than calling
    ``aiosqlite.connect`` directly.
    """

    db = await aiosqlite.connect(db_path)
    try:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        yield db
    finally:
        await db.close()


_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS migration_repairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_version INTEGER NOT NULL,
        table_name TEXT NOT NULL,
        row_id INTEGER,
        detail TEXT NOT NULL,
        repaired_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        file_path TEXT,
        file_size INTEGER,
        local_api_used INTEGER NOT NULL DEFAULT 0,
        status_message_id INTEGER,
        title TEXT,
        source_caption TEXT,
        thumbnail_path TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        error_message TEXT
    );
    CREATE TABLE IF NOT EXISTS edit_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        preset_id INTEGER REFERENCES presets(id) ON DELETE SET NULL,
        caption_text TEXT,
        caption_color TEXT,
        caption_style TEXT,
        caption_position TEXT,
        auto_captions INTEGER NOT NULL DEFAULT 0,
        voice_text TEXT,
        voice_over_voice TEXT,
        voice_mode TEXT,
        voice_quality TEXT,
        voice_speed REAL,
        tts_engine TEXT,
        banner_path TEXT,
        banner_position TEXT,
        banner_scale TEXT,
        watermark_removal INTEGER NOT NULL DEFAULT 0,
        watermark_position TEXT,
        watermark_mode TEXT,
        watermark_text TEXT,
        watermark_analysis TEXT,
        watermark_confidence REAL,
        watermark_candidates TEXT,
        watermark_preview_path TEXT,
        channel_banner INTEGER NOT NULL DEFAULT 0,
        subtitles_path TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        file_path TEXT,
        file_size INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        error_message TEXT,
        metadata_status TEXT NOT NULL DEFAULT 'not_requested',
        metadata_description TEXT,
        metadata_hashtags TEXT,
        metadata_error TEXT,
        metadata_attempt_count INTEGER NOT NULL DEFAULT 0,
        metadata_requested_at TEXT,
        metadata_started_at TEXT,
        metadata_completed_at TEXT,
        metadata_model TEXT,
        metadata_reasoning_effort TEXT,
        metadata_progress_message_id INTEGER,
        metadata_result_message_id INTEGER,
        metadata_reply_message_id INTEGER,
        render_delivery_message_id INTEGER,
        render_status_message_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS download_tokens (
        token_hash TEXT PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        edit_job_id INTEGER REFERENCES edit_jobs(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL,
        used_at TEXT,
        user_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        preset_name TEXT,
        crop_preset TEXT,
        caption_text TEXT,
        voice_over_voice TEXT,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        crop_preset TEXT,
        caption_text TEXT,
        voice_over_voice TEXT,
        caption_color TEXT,
        caption_style TEXT,
        caption_position TEXT,
        auto_captions INTEGER NOT NULL DEFAULT 0,
        voice_quality TEXT,
        voice_speed REAL,
        voice_text TEXT,
        tts_engine TEXT,
        voice_mode TEXT,
        banner_path TEXT,
        banner_position TEXT,
        banner_scale TEXT,
        watermark_removal INTEGER NOT NULL DEFAULT 0,
        watermark_position TEXT,
        watermark_mode TEXT,
        watermark_text TEXT,
        channel_banner INTEGER NOT NULL DEFAULT 0,
        shared INTEGER NOT NULL DEFAULT 0,
        share_code TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, name)
    );
    CREATE TABLE IF NOT EXISTS shared_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        preset_id INTEGER NOT NULL REFERENCES presets(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        share_code TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS pool_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
        edit_job_id INTEGER REFERENCES edit_jobs(id) ON DELETE SET NULL,
        file_path TEXT NOT NULL,
        file_size INTEGER,
        thumbnail_path TEXT,
        title TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS classifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        color TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS pool_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pool_item_id INTEGER NOT NULL REFERENCES pool_items(id) ON DELETE CASCADE,
        classification_id INTEGER NOT NULL REFERENCES classifications(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(pool_item_id, classification_id)
    );
    CREATE TABLE IF NOT EXISTS workflows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        trigger_classification_id INTEGER REFERENCES classifications(id) ON DELETE SET NULL,
        action_type TEXT NOT NULL,
        action_preset_id INTEGER REFERENCES presets(id) ON DELETE SET NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, name)
    );
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
        pool_item_id INTEGER NOT NULL REFERENCES pool_items(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS download_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_tokens_job ON download_tokens(job_id);
    CREATE INDEX IF NOT EXISTS idx_tokens_expires ON download_tokens(expires_at);
    CREATE INDEX IF NOT EXISTS idx_presets_user ON presets(user_id);
    CREATE INDEX IF NOT EXISTS idx_edit_jobs_source ON edit_jobs(source_job_id);
    CREATE INDEX IF NOT EXISTS idx_edit_jobs_user ON edit_jobs(user_id);
    CREATE INDEX IF NOT EXISTS idx_shared_presets_code ON shared_presets(share_code);
    CREATE INDEX IF NOT EXISTS idx_pool_user ON pool_items(user_id);
    CREATE INDEX IF NOT EXISTS idx_pool_status ON pool_items(status);
    CREATE INDEX IF NOT EXISTS idx_tags_item ON pool_tags(pool_item_id);
    CREATE INDEX IF NOT EXISTS idx_tags_class ON pool_tags(classification_id);
    CREATE INDEX IF NOT EXISTS idx_workflows_user ON workflows(user_id);
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_item ON workflow_runs(pool_item_id);
    CREATE INDEX IF NOT EXISTS idx_dlmsg_expires ON download_messages(expires_at);
"""


_LEGACY_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "download_tokens": (
        ("edit_job_id", "INTEGER REFERENCES edit_jobs(id) ON DELETE CASCADE"),
    ),
    "presets": (
        ("auto_captions", "INTEGER NOT NULL DEFAULT 0"),
        ("voice_text", "TEXT"),
        ("tts_engine", "TEXT"),
        ("voice_mode", "TEXT"),
        ("banner_path", "TEXT"),
        ("banner_position", "TEXT"),
        ("banner_scale", "TEXT"),
        ("watermark_removal", "INTEGER NOT NULL DEFAULT 0"),
        ("watermark_position", "TEXT"),
        ("watermark_mode", "TEXT"),
        ("watermark_text", "TEXT"),
        ("channel_banner", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "jobs": (
        ("status_message_id", "INTEGER"),
        ("title", "TEXT"),
        ("source_caption", "TEXT"),
        ("thumbnail_path", "TEXT"),
    ),
    "edit_jobs": (
        ("caption_text", "TEXT"),
        ("caption_color", "TEXT"),
        ("caption_style", "TEXT"),
        ("caption_position", "TEXT"),
        ("auto_captions", "INTEGER NOT NULL DEFAULT 0"),
        ("voice_text", "TEXT"),
        ("voice_over_voice", "TEXT"),
        ("voice_mode", "TEXT"),
        ("voice_quality", "TEXT"),
        ("voice_speed", "REAL"),
        ("tts_engine", "TEXT"),
        ("banner_path", "TEXT"),
        ("banner_position", "TEXT"),
        ("banner_scale", "TEXT"),
        ("watermark_removal", "INTEGER NOT NULL DEFAULT 0"),
        ("watermark_position", "TEXT"),
        ("watermark_mode", "TEXT"),
        ("watermark_text", "TEXT"),
        ("watermark_analysis", "TEXT"),
        ("watermark_confidence", "REAL"),
        ("watermark_candidates", "TEXT"),
        ("watermark_preview_path", "TEXT"),
        ("channel_banner", "INTEGER NOT NULL DEFAULT 0"),
        ("subtitles_path", "TEXT"),
    ),
    "pool_items": (
        ("edit_job_id", "INTEGER REFERENCES edit_jobs(id)"),
    ),
}

_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("metadata_status", "TEXT NOT NULL DEFAULT 'not_requested'"),
    ("metadata_description", "TEXT"),
    ("metadata_hashtags", "TEXT"),
    ("metadata_error", "TEXT"),
    ("metadata_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("metadata_requested_at", "TEXT"),
    ("metadata_started_at", "TEXT"),
    ("metadata_completed_at", "TEXT"),
    ("metadata_model", "TEXT"),
    ("metadata_reasoning_effort", "TEXT"),
    ("metadata_progress_message_id", "INTEGER"),
    ("metadata_result_message_id", "INTEGER"),
    ("metadata_reply_message_id", "INTEGER"),
    ("render_delivery_message_id", "INTEGER"),
)

_RENDER_MESSAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("render_status_message_id", "INTEGER"),
)


async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with open_database(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _apply_migrations(db)


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await db.commit()
    async with db.execute("SELECT version FROM schema_migrations") as cursor:
        applied = {row["version"] for row in await cursor.fetchall()}

    if 1 not in applied:
        await db.executescript(_SCHEMA_SQL)
        await _mark_migration(db, 1, "create current schema")
    if 2 not in applied:
        for table, columns in _LEGACY_COLUMNS.items():
            existing = await _column_names(db, table)
            for column, definition in columns:
                if column not in existing:
                    await db.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
                    )
        await _mark_migration(db, 2, "add legacy columns")
    if 3 not in applied:
        await _repair_legacy_foreign_keys(db)
        await _mark_migration(db, 3, "repair legacy foreign keys")
    if 4 not in applied:
        existing = await _column_names(db, "edit_jobs")
        for column, definition in _METADATA_COLUMNS:
            if column not in existing:
                await db.execute(
                    f'ALTER TABLE "edit_jobs" ADD COLUMN "{column}" {definition}'
                )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_edit_jobs_metadata_status "
            "ON edit_jobs(metadata_status)"
        )
        await _mark_migration(db, 4, "add automatic metadata state")
    if 5 not in applied:
        for table in ("presets", "edit_jobs"):
            existing = await _column_names(db, table)
            if "voice_mode" not in existing:
                await db.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "voice_mode" TEXT'
                )
        await _mark_migration(db, 5, "add voice-over mode")
    if 6 not in applied:
        existing = await _column_names(db, "edit_jobs")
        for column, definition in _RENDER_MESSAGE_COLUMNS:
            if column not in existing:
                await db.execute(
                    f'ALTER TABLE "edit_jobs" ADD COLUMN "{column}" {definition}'
                )
        await _mark_migration(db, 6, "track render preparation messages")


async def _mark_migration(db: aiosqlite.Connection, version: int, name: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
        (version, name),
    )
    await db.commit()


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f'PRAGMA table_info("{table}")') as cursor:
        return {row["name"] for row in await cursor.fetchall()}


async def _repair_legacy_foreign_keys(db: aiosqlite.Connection) -> None:
    """Repair known historical orphans without deleting user records or media."""

    await db.execute(
        "CREATE TABLE IF NOT EXISTS migration_repairs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, migration_version INTEGER NOT NULL, "
        "table_name TEXT NOT NULL, row_id INTEGER, detail TEXT NOT NULL, "
        "repaired_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )

    # Older builds could delete a source job while leaving edit history behind.
    # A tombstone parent preserves the edit row and makes future FK enforcement safe.
    async with db.execute(
        "SELECT DISTINCT e.source_job_id, e.user_id FROM edit_jobs e "
        "LEFT JOIN jobs j ON j.id = e.source_job_id WHERE j.id IS NULL"
    ) as cursor:
        orphan_sources = await cursor.fetchall()
    for row in orphan_sources:
        source_id = row["source_job_id"]
        await db.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, url, user_id, chat_id, status, error_message) "
            "VALUES (?, ?, ?, ?, 'deleted', ?)",
            (
                source_id,
                f"recovered://orphaned-job/{source_id}",
                row["user_id"],
                row["user_id"],
                "recovered placeholder for an orphaned edit during schema migration",
            ),
        )
        await db.execute(
            "INSERT INTO migration_repairs "
            "(migration_version, table_name, row_id, detail) VALUES (3, 'jobs', ?, ?)",
            (source_id, "created a tombstone parent for existing edit rows"),
        )

    # Tokens also require a parent job. Preserve their audit history using a
    # tombstone; expired-token cleanup can remove them normally afterward.
    async with db.execute(
        "SELECT DISTINCT t.job_id, t.user_id FROM download_tokens t "
        "LEFT JOIN jobs j ON j.id = t.job_id WHERE j.id IS NULL"
    ) as cursor:
        orphan_token_jobs = await cursor.fetchall()
    for row in orphan_token_jobs:
        job_id = row["job_id"]
        await db.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, url, user_id, chat_id, status, error_message) "
            "VALUES (?, ?, ?, ?, 'deleted', ?)",
            (
                job_id,
                f"recovered://orphaned-token/{job_id}",
                row["user_id"],
                row["user_id"],
                "recovered placeholder for an orphaned token during schema migration",
            ),
        )

    # Preserve other mandatory child rows by creating visibly disabled/missing
    # parent tombstones. This is safer than deleting history during startup, and
    # makes PRAGMA foreign_key_check useful immediately after an upgrade.
    async with db.execute(
        "SELECT DISTINCT s.preset_id, s.user_id FROM shared_presets s "
        "LEFT JOIN presets p ON p.id = s.preset_id WHERE p.id IS NULL"
    ) as cursor:
        orphan_presets = await cursor.fetchall()
    for row in orphan_presets:
        preset_id = row["preset_id"]
        await db.execute(
            "INSERT OR IGNORE INTO presets (id, user_id, name, shared) "
            "VALUES (?, ?, ?, 1)",
            (preset_id, row["user_id"], f"Recovered preset #{preset_id}"),
        )

    async with db.execute(
        "SELECT missing_pool_id, MIN(user_id) AS user_id FROM ("
        "SELECT t.pool_item_id AS missing_pool_id, t.user_id FROM pool_tags t "
        "LEFT JOIN pool_items p ON p.id = t.pool_item_id WHERE p.id IS NULL "
        "UNION ALL "
        "SELECT r.pool_item_id AS missing_pool_id, r.user_id FROM workflow_runs r "
        "LEFT JOIN pool_items p ON p.id = r.pool_item_id WHERE p.id IS NULL"
        ") GROUP BY missing_pool_id"
    ) as cursor:
        orphan_pool_items = await cursor.fetchall()
    for row in orphan_pool_items:
        pool_id = row["missing_pool_id"]
        await db.execute(
            "INSERT OR IGNORE INTO pool_items "
            "(id, user_id, file_path, title, status) VALUES (?, ?, ?, ?, 'missing')",
            (
                pool_id,
                row["user_id"],
                f"recovered://missing-pool-item/{pool_id}",
                f"Recovered Pool item #{pool_id}",
            ),
        )

    async with db.execute(
        "SELECT DISTINCT t.classification_id FROM pool_tags t "
        "LEFT JOIN classifications c ON c.id = t.classification_id WHERE c.id IS NULL"
    ) as cursor:
        orphan_classifications = await cursor.fetchall()
    for row in orphan_classifications:
        classification_id = row["classification_id"]
        await db.execute(
            "INSERT OR IGNORE INTO classifications (id, name, description) "
            "VALUES (?, ?, ?)",
            (
                classification_id,
                f"Recovered classification #{classification_id}",
                "placeholder restored during foreign-key migration",
            ),
        )

    async with db.execute(
        "SELECT DISTINCT r.workflow_id, r.user_id FROM workflow_runs r "
        "LEFT JOIN workflows w ON w.id = r.workflow_id WHERE w.id IS NULL"
    ) as cursor:
        orphan_workflows = await cursor.fetchall()
    for row in orphan_workflows:
        workflow_id = row["workflow_id"]
        await db.execute(
            "INSERT OR IGNORE INTO workflows "
            "(id, user_id, name, action_type, enabled) VALUES (?, ?, ?, 'recovered', 0)",
            (
                workflow_id,
                row["user_id"],
                f"Recovered workflow #{workflow_id}",
            ),
        )

    # Optional references can be detached without losing the owning record.
    optional_repairs = (
        (
            "edit_jobs",
            "preset_id",
            "presets",
            "UPDATE edit_jobs SET preset_id = NULL WHERE preset_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM presets WHERE presets.id = edit_jobs.preset_id)",
        ),
        (
            "download_tokens",
            "edit_job_id",
            "edit_jobs",
            "UPDATE download_tokens SET edit_job_id = NULL WHERE edit_job_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM edit_jobs WHERE edit_jobs.id = download_tokens.edit_job_id)",
        ),
        (
            "pool_items",
            "source_job_id",
            "jobs",
            "UPDATE pool_items SET source_job_id = NULL WHERE source_job_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id = pool_items.source_job_id)",
        ),
        (
            "pool_items",
            "edit_job_id",
            "edit_jobs",
            "UPDATE pool_items SET edit_job_id = NULL WHERE edit_job_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM edit_jobs WHERE edit_jobs.id = pool_items.edit_job_id)",
        ),
        (
            "workflows",
            "trigger_classification_id",
            "classifications",
            "UPDATE workflows SET trigger_classification_id = NULL "
            "WHERE trigger_classification_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM classifications WHERE classifications.id = workflows.trigger_classification_id)",
        ),
        (
            "workflows",
            "action_preset_id",
            "presets",
            "UPDATE workflows SET action_preset_id = NULL WHERE action_preset_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM presets WHERE presets.id = workflows.action_preset_id)",
        ),
    )
    for table, column, parent, statement in optional_repairs:
        cursor = await db.execute(statement)
        if cursor.rowcount:
            await db.execute(
                "INSERT INTO migration_repairs "
                "(migration_version, table_name, detail) VALUES (3, ?, ?)",
                (table, f"detached {cursor.rowcount} orphaned {column} references to {parent}"),
            )
    await db.commit()


async def foreign_key_violations(db_path: Path) -> list[aiosqlite.Row]:
    """Return SQLite's current foreign-key integrity report."""

    async with open_database(db_path) as db:
        async with db.execute("PRAGMA foreign_key_check") as cursor:
            return list(await cursor.fetchall())


async def create_job(db_path: Path, url: str, user_id: int, chat_id: int) -> JobRecord:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO jobs (url, user_id, chat_id) VALUES (?, ?, ?)",
            (url, user_id, chat_id),
        )
        await db.commit()
        job_id = cursor.lastrowid
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row)


async def update_job(db_path: Path, job_id: int, **kwargs) -> JobRecord | None:
    if not kwargs:
        return await get_job(db_path, job_id)
    _validate_update_fields("jobs", kwargs, _JOB_UPDATE_FIELDS)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [job_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE jobs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None


async def get_job(db_path: Path, job_id: int) -> JobRecord | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None


async def list_user_jobs(db_path: Path, user_id: int, limit: int = 20) -> list[JobRecord]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(row) for row in rows]


async def list_all_jobs(db_path: Path, limit: int = 30) -> list[JobRecord]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(row) for row in rows]


async def create_download_token(
    db_path: Path,
    job_id: int,
    user_id: int,
    expiry_minutes: int,
    *,
    edit_job_id: int | None = None,
) -> str:
    job = await get_job(db_path, job_id)
    if job is None or job.user_id != user_id:
        raise ValueError("download token job ownership mismatch")
    if edit_job_id is not None:
        edit = await get_edit_job(db_path, edit_job_id)
        if (
            edit is None
            or edit.user_id != user_id
            or edit.source_job_id != job_id
        ):
            raise ValueError("download token edit ownership mismatch")
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
    async with open_database(db_path) as db:
        await db.execute(
            "INSERT INTO download_tokens "
            "(token_hash, job_id, edit_job_id, expires_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (token_hash, job_id, edit_job_id, expires_at.isoformat(), user_id),
        )
        await db.commit()
    return raw_token


async def lookup_download_token(db_path: Path, raw_token: str) -> DownloadToken | None:
    """Return an unused, unexpired token without consuming it."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM download_tokens "
            "WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (token_hash, now),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_token(row) if row else None


async def consume_download_token(db_path: Path, raw_token: str) -> DownloadToken | None:
    """Atomically claim a one-time token. Exactly one concurrent caller wins."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "UPDATE download_tokens "
                "SET used_at = ? "
                "WHERE token_hash = ? AND used_at IS NULL AND expires_at > ? "
                "RETURNING *",
                (now, token_hash, now),
            ) as cur:
                row = await cur.fetchone()
            await db.commit()
            return _row_to_token(row) if row else None
        except aiosqlite.OperationalError:
            # Fallback for SQLite builds without UPDATE RETURNING.
            # A failed statement leaves the connection in an aborted transaction;
            # roll back before starting the conditional-update path.
            await db.rollback()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "UPDATE download_tokens "
                    "SET used_at = ? "
                    "WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
                    (now, token_hash, now),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return None
                async with db.execute(
                    "SELECT * FROM download_tokens WHERE token_hash = ?",
                    (token_hash,),
                ) as cur:
                    row = await cur.fetchone()
                await db.commit()
                return _row_to_token(row) if row else None
            except Exception:
                await db.rollback()
                raise


async def get_or_create_user_settings(db_path: Path, user_id: int) -> UserSettings:
    async with open_database(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user_settings(row)


async def update_user_settings(db_path: Path, user_id: int, **kwargs) -> UserSettings:
    if not kwargs:
        return await get_or_create_user_settings(db_path, user_id)
    _validate_update_fields(
        "user_settings", kwargs, _USER_SETTINGS_UPDATE_FIELDS,
    )
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values())
    async with open_database(db_path) as db:
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        await db.execute(
            f"INSERT INTO user_settings (user_id, {columns}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(user_id) DO UPDATE SET {set_clause}, updated_at = datetime('now')",
            (user_id, *values, *values),
        )
        await db.commit()
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user_settings(row)


async def create_preset(db_path: Path, user_id: int, name: str, **kwargs) -> Preset:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO presets (user_id, name, crop_preset, caption_text, voice_over_voice, "
            "caption_color, caption_style, caption_position, auto_captions, voice_quality, voice_speed, "
            "voice_text, tts_engine, voice_mode, banner_path, banner_position, banner_scale, "
            "watermark_removal, watermark_position, watermark_mode, watermark_text, channel_banner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                name,
                kwargs.get("crop_preset"),
                kwargs.get("caption_text"),
                kwargs.get("voice_over_voice"),
                kwargs.get("caption_color"),
                kwargs.get("caption_style"),
                kwargs.get("caption_position"),
                int(bool(kwargs.get("auto_captions"))),
                kwargs.get("voice_quality"),
                kwargs.get("voice_speed"),
                kwargs.get("voice_text"),
                kwargs.get("tts_engine"),
                kwargs.get("voice_mode"),
                kwargs.get("banner_path"),
                kwargs.get("banner_position"),
                kwargs.get("banner_scale"),
                int(bool(kwargs.get("watermark_removal"))),
                kwargs.get("watermark_position"),
                kwargs.get("watermark_mode"),
                kwargs.get("watermark_text"),
                int(bool(kwargs.get("channel_banner"))),
            ),
        )
        await db.commit()
        preset_id = cursor.lastrowid
        async with db.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_preset(row)


async def update_preset(db_path: Path, preset_id: int, user_id: int, **kwargs) -> Preset | None:
    if not kwargs:
        async with open_database(db_path) as db:
            async with db.execute("SELECT * FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id)) as cur:
                row = await cur.fetchone()
            return _row_to_preset(row) if row else None
    _validate_update_fields("presets", kwargs, _PRESET_UPDATE_FIELDS)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [preset_id, user_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE presets SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id)) as cur:
            row = await cur.fetchone()
        return _row_to_preset(row) if row else None


async def list_presets(db_path: Path, user_id: int) -> list[Preset]:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM presets WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_preset(row) for row in rows]


async def delete_preset(db_path: Path, user_id: int, preset_id: int) -> bool:
    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Explicitly detach legacy NO ACTION references. New databases use
            # SET NULL/CASCADE, but these statements keep upgraded DBs safe too.
            await db.execute(
                "UPDATE edit_jobs SET preset_id = NULL "
                "WHERE preset_id = ? AND EXISTS "
                "(SELECT 1 FROM presets WHERE id = ? AND user_id = ?)",
                (preset_id, preset_id, user_id),
            )
            await db.execute(
                "UPDATE workflows SET action_preset_id = NULL "
                "WHERE action_preset_id = ? AND EXISTS "
                "(SELECT 1 FROM presets WHERE id = ? AND user_id = ?)",
                (preset_id, preset_id, user_id),
            )
            await db.execute(
                "DELETE FROM shared_presets WHERE preset_id = ? AND EXISTS "
                "(SELECT 1 FROM presets WHERE id = ? AND user_id = ?)",
                (preset_id, preset_id, user_id),
            )
            cursor = await db.execute(
                "DELETE FROM presets WHERE id = ? AND user_id = ?",
                (preset_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def get_preset_by_share_code(db_path: Path, share_code: str) -> Preset | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM presets WHERE share_code = ?", (share_code,)) as cur:
            row = await cur.fetchone()
        return _row_to_preset(row) if row else None


async def share_preset(db_path: Path, preset_id: int, user_id: int) -> str | None:
    share_code = secrets.token_urlsafe(16)
    try:
        async with open_database(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT 1 FROM presets WHERE id = ? AND user_id = ?",
                (preset_id, user_id),
            ) as cursor:
                if await cursor.fetchone() is None:
                    await db.rollback()
                    return None
            await db.execute(
                "INSERT INTO shared_presets (preset_id, user_id, share_code) VALUES (?, ?, ?)",
                (preset_id, user_id, share_code),
            )
            await db.execute(
                "UPDATE presets SET share_code = ?, shared = 1 WHERE id = ? AND user_id = ?",
                (share_code, preset_id, user_id),
            )
            await db.commit()
        return share_code
    except aiosqlite.IntegrityError:
        return None


async def create_edit_job(db_path: Path, source_job_id: int, user_id: int, preset_id: int | None = None) -> EditJob:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO edit_jobs (source_job_id, user_id, preset_id) VALUES (?, ?, ?)",
            (source_job_id, user_id, preset_id),
        )
        await db.commit()
        edit_id = cursor.lastrowid
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row)


async def stage_edit_source(source: Path, destination: Path) -> int:
    """Stage an edit input without blocking the bot's event loop."""
    def _stage() -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        return destination.stat().st_size

    return await asyncio.to_thread(_stage)


async def update_edit_job(db_path: Path, edit_id: int, **kwargs) -> EditJob | None:
    if not kwargs:
        return await get_edit_job(db_path, edit_id)
    _validate_update_fields("edit_jobs", kwargs, _EDIT_UPDATE_FIELDS)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [edit_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE edit_jobs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def get_edit_job(db_path: Path, edit_id: int) -> EditJob | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def find_edit_by_message(
    db_path: Path,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
) -> EditJob | None:
    """Find an owned edit associated with one of its Telegram messages."""
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT e.* FROM edit_jobs e "
            "JOIN jobs j ON j.id = e.source_job_id "
            "WHERE e.user_id = ? AND j.chat_id = ? AND ("
            "e.render_status_message_id = ? OR "
            "e.render_delivery_message_id = ? OR "
            "e.metadata_progress_message_id = ? OR "
            "e.metadata_result_message_id = ? OR "
            "e.metadata_reply_message_id = ?"
            ") ORDER BY e.updated_at DESC, e.id DESC LIMIT 1",
            (user_id, chat_id, message_id, message_id, message_id, message_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def find_job_by_message(
    db_path: Path,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
) -> JobRecord | None:
    """Find an owned source job by its Telegram status message."""
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM jobs WHERE user_id = ? AND chat_id = ? "
            "AND status_message_id = ? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (user_id, chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None


async def queue_metadata_job(
    db_path: Path,
    edit_id: int,
    *,
    model: str,
    reasoning_effort: str,
    progress_message_id: int | None = None,
    render_delivery_message_id: int | None = None,
) -> EditJob | None:
    """Persist a metadata request after the final video has been delivered."""
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "UPDATE edit_jobs SET metadata_status = 'queued', "
            "metadata_description = NULL, metadata_hashtags = NULL, "
            "metadata_error = NULL, metadata_requested_at = datetime('now'), "
            "metadata_started_at = NULL, metadata_completed_at = NULL, "
            "metadata_model = ?, metadata_reasoning_effort = ?, "
            "metadata_progress_message_id = ?, metadata_result_message_id = ?, "
            "metadata_reply_message_id = NULL, render_delivery_message_id = ?, "
            "updated_at = datetime('now') "
            "WHERE id = ? AND status = 'rendered' AND file_path IS NOT NULL",
            (
                model,
                reasoning_effort,
                progress_message_id,
                render_delivery_message_id,
                render_delivery_message_id,
                edit_id,
            ),
        )
        await db.commit()
        if cursor.rowcount <= 0:
            return None
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def claim_metadata_job(db_path: Path, edit_id: int) -> EditJob | None:
    """Atomically claim queued/recovered metadata work for one worker."""
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "UPDATE edit_jobs SET metadata_status = 'running', "
            "metadata_attempt_count = COALESCE(metadata_attempt_count, 0) + 1, "
            "metadata_started_at = datetime('now'), metadata_error = NULL, "
            "updated_at = datetime('now') "
            "WHERE id = ? AND metadata_status IN ('queued', 'running') "
            "AND status = 'rendered' AND file_path IS NOT NULL",
            (edit_id,),
        )
        await db.commit()
        if cursor.rowcount <= 0:
            return None
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def list_metadata_jobs(
    db_path: Path,
    statuses: tuple[str, ...] = ("queued", "running"),
) -> list[EditJob]:
    """List durable metadata jobs for queue recovery and operator reporting."""
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM edit_jobs "
            f"WHERE metadata_status IN ({placeholders}) ORDER BY created_at, id",
            statuses,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_edit_job(row) for row in rows]


async def list_resumable_metadata_jobs(db_path: Path) -> list[EditJob]:
    """Return only jobs with the durable prerequisites needed after restart."""
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM edit_jobs "
            "WHERE metadata_status IN ('queued', 'running') "
            "AND status = 'rendered' AND file_path IS NOT NULL "
            "AND render_delivery_message_id IS NOT NULL "
            "ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_edit_job(row) for row in rows]


async def list_source_jobs_for_user(db_path: Path, user_id: int, limit: int = 20) -> list[JobRecord]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT DISTINCT j.* FROM jobs j "
            "LEFT JOIN edit_jobs ej ON ej.source_job_id = j.id "
            "WHERE j.user_id = ? OR ej.user_id = ? "
            "ORDER BY j.created_at DESC LIMIT ?",
            (user_id, user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(row) for row in rows]


async def create_pool_item(
    db_path: Path,
    user_id: int,
    file_path: str,
    source_job_id: int | None = None,
    title: str | None = None,
    *,
    edit_job_id: int | None = None,
    file_size: int | None = None,
    thumbnail_path: str | None = None,
) -> PoolItem:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO pool_items "
            "(user_id, source_job_id, edit_job_id, file_path, file_size, thumbnail_path, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                source_job_id,
                edit_job_id,
                file_path,
                file_size,
                thumbnail_path,
                title,
            ),
        )
        await db.commit()
        pool_id = cursor.lastrowid
        async with db.execute("SELECT * FROM pool_items WHERE id = ?", (pool_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row)


async def create_durable_pool_item(
    db_path: Path,
    storage_dir: Path,
    user_id: int,
    source_file: Path,
    source_job_id: int | None = None,
    title: str | None = None,
    *,
    edit_job_id: int | None = None,
    thumbnail_file: Path | None = None,
) -> PoolItem:
    """Copy media into Pool-owned storage before recording it in the database.

    The copy is deliberately independent of the job artifact, so retention and
    edit resets cannot invalidate a saved Pool item. Both source paths must be
    inside ``storage_dir``; callers cannot use this API as an arbitrary file-copy
    primitive.
    """

    source = _storage_path(source_file, storage_dir)
    if not source.is_file():
        raise FileNotFoundError(source)
    pool_dir = _resolved_storage_root(storage_dir) / "pool"
    token = secrets.token_hex(16)
    suffix = source.suffix if 0 < len(source.suffix) <= 12 else ".media"
    destination = pool_dir / f"{user_id}-{token}{suffix}"
    await _copy_file_atomically(source, destination)

    thumbnail_destination: Path | None = None
    try:
        if thumbnail_file is not None:
            thumbnail = _storage_path(thumbnail_file, storage_dir)
            if thumbnail.is_file():
                thumbnail_suffix = (
                    thumbnail.suffix if 0 < len(thumbnail.suffix) <= 12 else ".image"
                )
                thumbnail_destination = pool_dir / (
                    f"{user_id}-{token}-thumbnail{thumbnail_suffix}"
                )
                await _copy_file_atomically(thumbnail, thumbnail_destination)
        return await create_pool_item(
            db_path,
            user_id,
            str(destination),
            source_job_id=source_job_id,
            edit_job_id=edit_job_id,
            file_size=destination.stat().st_size,
            thumbnail_path=(
                str(thumbnail_destination) if thumbnail_destination is not None else None
            ),
            title=title,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        if thumbnail_destination is not None:
            thumbnail_destination.unlink(missing_ok=True)
        raise


def _resolved_storage_root(storage_dir: Path) -> Path:
    return storage_dir.expanduser().resolve(strict=False)


def _storage_path(path: Path | str, storage_dir: Path) -> Path:
    """Resolve one artifact path and reject storage-root escapes."""

    root = _resolved_storage_root(storage_dir)
    candidate = Path(path).expanduser().resolve(strict=False)
    if candidate == root or not candidate.is_relative_to(root):
        raise UnsafeStoragePath(
            f"refusing artifact operation outside storage root: {candidate}"
        )
    return candidate


async def _copy_file_atomically(source: Path, destination: Path) -> None:
    def _copy() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(8)}.part"
        )
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(_copy)


async def get_saved_source_pool_item(
    db_path: Path, user_id: int, source_job_id: int
) -> PoolItem | None:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM pool_items "
            "WHERE user_id = ? AND source_job_id = ? AND edit_job_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, source_job_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def get_saved_edit_pool_item(
    db_path: Path, user_id: int, edit_job_id: int
) -> PoolItem | None:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM pool_items WHERE user_id = ? AND edit_job_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, edit_job_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def list_saved_edits_for_source(
    db_path: Path, user_id: int, source_job_id: int
) -> list[PoolItem]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM pool_items "
            "WHERE user_id = ? AND source_job_id = ? AND edit_job_id IS NOT NULL "
            "ORDER BY created_at DESC",
            (user_id, source_job_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_pool_item(row) for row in rows]


async def get_pool_item(db_path: Path, pool_item_id: int) -> PoolItem | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM pool_items WHERE id = ?", (pool_item_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def list_pool_items(db_path: Path, user_id: int, status: str | None = None, classification_id: int | None = None, limit: int = 50) -> list[PoolItem]:
    async with open_database(db_path) as db:
        query = "SELECT DISTINCT pi.* FROM pool_items pi LEFT JOIN pool_tags pt ON pt.pool_item_id = pi.id WHERE pi.user_id = ?"
        params: list = [user_id]
        if status is not None:
            query += " AND pi.status = ?"
            params.append(status)
        if classification_id is not None:
            query += " AND pt.classification_id = ?"
            params.append(classification_id)
        query += " ORDER BY pi.created_at DESC LIMIT ?"
        params.append(limit)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_pool_item(row) for row in rows]


async def update_pool_item(db_path: Path, pool_item_id: int, **kwargs) -> PoolItem | None:
    if not kwargs:
        return await get_pool_item(db_path, pool_item_id)
    _validate_update_fields("pool_items", kwargs, _POOL_UPDATE_FIELDS)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [pool_item_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE pool_items SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM pool_items WHERE id = ?", (pool_item_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def delete_pool_item(db_path: Path, pool_item_id: int, user_id: int) -> bool:
    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "DELETE FROM workflow_runs WHERE pool_item_id = ? AND EXISTS "
                "(SELECT 1 FROM pool_items WHERE id = ? AND user_id = ?)",
                (pool_item_id, pool_item_id, user_id),
            )
            await db.execute(
                "DELETE FROM pool_tags WHERE pool_item_id = ? AND EXISTS "
                "(SELECT 1 FROM pool_items WHERE id = ? AND user_id = ?)",
                (pool_item_id, pool_item_id, user_id),
            )
            cursor = await db.execute(
                "DELETE FROM pool_items WHERE id = ? AND user_id = ?",
                (pool_item_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def create_classification(db_path: Path, name: str, description: str | None = None, color: str | None = None) -> Classification:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO classifications (name, description, color) VALUES (?, ?, ?)",
            (name, description, color),
        )
        await db.commit()
        class_id = cursor.lastrowid
        async with db.execute("SELECT * FROM classifications WHERE id = ?", (class_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_classification(row)


async def get_or_create_classification(db_path: Path, name: str, description: str | None = None, color: str | None = None) -> Classification:
    async with open_database(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO classifications (name, description, color) "
            "VALUES (?, ?, ?)",
            (name, description, color),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM classifications WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_classification(row)


async def get_classification(db_path: Path, classification_id: int) -> Classification | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_classification(row) if row else None


async def list_classifications(db_path: Path) -> list[Classification]:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM classifications ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [_row_to_classification(row) for row in rows]


async def add_pool_tag(db_path: Path, pool_item_id: int, classification_id: int, user_id: int) -> PoolTag | None:
    try:
        async with open_database(db_path) as db:
            cursor = await db.execute(
                "INSERT INTO pool_tags (pool_item_id, classification_id, user_id) VALUES (?, ?, ?)",
                (pool_item_id, classification_id, user_id),
            )
            await db.commit()
            tag_id = cursor.lastrowid
            async with db.execute("SELECT * FROM pool_tags WHERE id = ?", (tag_id,)) as cur:
                row = await cur.fetchone()
            return _row_to_pool_tag(row) if row else None
    except aiosqlite.IntegrityError:
        return None


async def remove_pool_tag(db_path: Path, pool_item_id: int, classification_id: int) -> bool:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM pool_tags WHERE pool_item_id = ? AND classification_id = ?",
            (pool_item_id, classification_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_pool_tags(db_path: Path, pool_item_id: int) -> list[PoolTag]:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM pool_tags WHERE pool_item_id = ?", (pool_item_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_pool_tag(row) for row in rows]


async def create_workflow(db_path: Path, user_id: int, name: str, action_type: str, trigger_classification_id: int | None = None, action_preset_id: int | None = None) -> Workflow:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO workflows (user_id, name, trigger_classification_id, action_type, action_preset_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, trigger_classification_id, action_type, action_preset_id),
        )
        await db.commit()
        wf_id = cursor.lastrowid
        async with db.execute("SELECT * FROM workflows WHERE id = ?", (wf_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow(row)


async def get_workflow(db_path: Path, workflow_id: int) -> Workflow | None:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None


async def list_workflows(db_path: Path, user_id: int) -> list[Workflow]:
    async with open_database(db_path) as db:
        async with db.execute("SELECT * FROM workflows WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow(row) for row in rows]


async def update_workflow(db_path: Path, workflow_id: int, user_id: int, **kwargs) -> Workflow | None:
    if not kwargs:
        async with open_database(db_path) as db:
            async with db.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)) as cur:
                row = await cur.fetchone()
            return _row_to_workflow(row) if row else None
    _validate_update_fields("workflows", kwargs, _WORKFLOW_UPDATE_FIELDS)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [workflow_id, user_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE workflows SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None


async def delete_workflow(db_path: Path, workflow_id: int, user_id: int) -> bool:
    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                "DELETE FROM workflow_runs WHERE workflow_id = ? AND EXISTS "
                "(SELECT 1 FROM workflows WHERE id = ? AND user_id = ?)",
                (workflow_id, workflow_id, user_id),
            )
            cursor = await db.execute(
                "DELETE FROM workflows WHERE id = ? AND user_id = ?",
                (workflow_id, user_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        except Exception:
            await db.rollback()
            raise


async def create_workflow_run(db_path: Path, workflow_id: int, pool_item_id: int, user_id: int) -> WorkflowRun:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO workflow_runs (workflow_id, pool_item_id, user_id) VALUES (?, ?, ?)",
            (workflow_id, pool_item_id, user_id),
        )
        await db.commit()
        run_id = cursor.lastrowid
        async with db.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row)


async def get_workflow_run(db_path: Path, run_id: int) -> WorkflowRun | None:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_workflow_run(row) if row else None


async def update_workflow_run(db_path: Path, run_id: int, **kwargs) -> WorkflowRun | None:
    if not kwargs:
        return await get_workflow_run(db_path, run_id)
    _validate_update_fields(
        "workflow_runs", kwargs, _WORKFLOW_RUN_UPDATE_FIELDS,
    )
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [run_id]
    async with open_database(db_path) as db:
        await db.execute(
            f"UPDATE workflow_runs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row) if row else None


@dataclass(frozen=True)
class DownloadMessage:
    id: int
    chat_id: int
    message_id: int
    expires_at: datetime
    deleted: bool


async def store_download_message(db_path: Path, chat_id: int, message_id: int, expiry_minutes: int) -> int:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO download_messages (chat_id, message_id, expires_at) VALUES (?, ?, ?)",
            (chat_id, message_id, expires_at.isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def list_expired_download_messages(db_path: Path) -> list[DownloadMessage]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT id, chat_id, message_id, expires_at, deleted FROM download_messages "
            "WHERE deleted = 0 AND expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
    return [
        DownloadMessage(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            deleted=bool(row["deleted"]),
        )
        for row in rows
    ]


async def list_undeleted_download_messages(db_path: Path) -> list[DownloadMessage]:
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT id, chat_id, message_id, expires_at, deleted FROM download_messages WHERE deleted = 0",
        ) as cur:
            rows = await cur.fetchall()
    return [
        DownloadMessage(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            deleted=bool(row["deleted"]),
        )
        for row in rows
    ]


async def mark_download_message_deleted(db_path: Path, msg_id: int) -> None:
    async with open_database(db_path) as db:
        await db.execute(
            "UPDATE download_messages SET deleted = 1 WHERE id = ?",
            (msg_id,),
        )
        await db.commit()


async def cleanup_download_messages(db_path: Path, bot) -> int:
    messages = await list_expired_download_messages(db_path)
    removed = 0
    for msg in messages:
        try:
            await bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
        except Exception as exc:
            detail = str(exc).lower()
            terminal = any(text in detail for text in (
                "message to delete not found",
                "message can't be deleted",
                "message identifier is not specified",
                "message_id_invalid",
            ))
            if not terminal:
                LOGGER.warning(
                    "Will retry deletion of Telegram message %s/%s: %s",
                    msg.chat_id,
                    msg.message_id,
                    exc,
                )
                continue
        await mark_download_message_deleted(db_path, msg.id)
        removed += 1
    async with open_database(db_path) as db:
        await db.execute(
            "DELETE FROM download_messages WHERE deleted = 1 AND expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
    return removed


async def cleanup_expired_tokens(db_path: Path) -> int:
    async with open_database(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM download_tokens WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
        return cursor.rowcount


async def reconcile_interrupted_work(db_path: Path) -> tuple[int, int]:
    """Mark non-durable in-flight work interrupted after a process restart."""
    message = "interrupted by bot restart; submit the job again"
    async with open_database(db_path) as db:
        jobs = await db.execute(
            "UPDATE jobs SET status = 'failed', error_message = ?, "
            "updated_at = datetime('now') "
            "WHERE status IN ('pending', 'queued', 'downloading')",
            (message,),
        )
        edits = await db.execute(
            "UPDATE edit_jobs SET status = 'failed', error_message = ?, "
            "updated_at = datetime('now') "
            "WHERE status IN ('queued', 'rendering')",
            (message,),
        )
        await db.commit()
        return jobs.rowcount, edits.rowcount


def _canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


async def _pool_reference_paths(db: aiosqlite.Connection) -> set[Path]:
    async with db.execute(
        "SELECT file_path, thumbnail_path FROM pool_items"
    ) as cursor:
        rows = await cursor.fetchall()
    references: set[Path] = set()
    for row in rows:
        for column in ("file_path", "thumbnail_path"):
            if row[column]:
                references.add(_canonical_path(row[column]))
    return references


def _edit_artifact_paths(row: aiosqlite.Row, storage_dir: Path) -> set[Path]:
    paths = {
        Path(row[column])
        for column in ("file_path", "subtitles_path", "watermark_preview_path")
        if row[column]
    }
    root = _resolved_storage_root(storage_dir)
    if root.is_dir():
        paths.update(root.glob(f"edit-{row['id']}-*"))
        paths.update(root.glob(f".edit-{row['id']}-*"))
    return paths


def _job_artifact_paths(row: aiosqlite.Row, storage_dir: Path) -> set[Path]:
    paths = {
        Path(row[column])
        for column in ("file_path", "thumbnail_path")
        if row[column]
    }
    root = _resolved_storage_root(storage_dir)
    if root.is_dir():
        paths.update(root.glob(f"{row['id']}-*"))
    return paths


async def _delete_artifact_files(
    paths: set[Path],
    storage_dir: Path,
    preserved_paths: set[Path],
) -> CleanupResult:
    """Delete a de-duplicated artifact set while enforcing the storage boundary."""

    def _delete() -> CleanupResult:
        deleted = 0
        preserved = 0
        unsafe: list[str] = []
        seen: set[Path] = set()
        for raw_path in paths:
            canonical = _canonical_path(raw_path)
            if canonical in seen:
                continue
            seen.add(canonical)
            if canonical in preserved_paths:
                if canonical.is_file() or canonical.is_symlink():
                    preserved += 1
                continue
            try:
                artifact = _storage_path(canonical, storage_dir)
            except UnsafeStoragePath:
                unsafe.append(str(canonical))
                continue
            if artifact.is_file() or artifact.is_symlink():
                artifact.unlink(missing_ok=True)
                deleted += 1
        return CleanupResult(
            files_deleted=deleted,
            files_preserved=preserved,
            unsafe_paths=tuple(unsafe),
        )

    return await asyncio.to_thread(_delete)


async def cleanup_edit_artifacts(
    db_path: Path,
    storage_dir: Path,
    edit_id: int,
    *,
    user_id: int | None = None,
    preserve_output: bool = False,
) -> CleanupResult:
    """Remove files belonging to an edit while retaining its database record.

    Set ``preserve_output`` after a successful render to keep the final media and
    subtitle file while removing staged inputs, previews, and FFmpeg
    intermediates. Pool-referenced files are always retained.
    """

    async with open_database(db_path) as db:
        query = "SELECT * FROM edit_jobs WHERE id = ?"
        params: tuple[int, ...] = (edit_id,)
        if user_id is not None:
            query += " AND user_id = ?"
            params = (edit_id, user_id)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return CleanupResult()
        preserved = await _pool_reference_paths(db)

    if preserve_output:
        for column in ("file_path", "subtitles_path"):
            if row[column]:
                preserved.add(_canonical_path(row[column]))
    result = await _delete_artifact_files(
        _edit_artifact_paths(row, storage_dir), storage_dir, preserved
    )

    # Keep an unsafe pointer visible for a later operator audit instead of
    # silently forgetting a file that could not be removed.
    if not result.unsafe_paths:
        updates: dict[str, object | None] = {"watermark_preview_path": None}
        if not preserve_output:
            updates.update(file_path=None, file_size=None, subtitles_path=None)
        await update_edit_job(db_path, edit_id, **updates)
    return result


async def delete_edit_job_with_artifacts(
    db_path: Path,
    storage_dir: Path,
    edit_id: int,
    *,
    user_id: int | None = None,
) -> CleanupResult:
    """Delete one edit record and every unreferenced, in-root artifact it owns."""

    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            query = "SELECT * FROM edit_jobs WHERE id = ?"
            params: tuple[int, ...] = (edit_id,)
            if user_id is not None:
                query += " AND user_id = ?"
                params = (edit_id, user_id)
            async with db.execute(query, params) as row_cursor:
                row = await row_cursor.fetchone()
            if row is None:
                await db.rollback()
                return CleanupResult()
            artifacts = _edit_artifact_paths(row, storage_dir)
            preserved = await _pool_reference_paths(db)
            await db.execute(
                "UPDATE pool_items SET edit_job_id = NULL WHERE edit_job_id = ?",
                (edit_id,),
            )
            await db.execute(
                "DELETE FROM download_tokens WHERE edit_job_id = ?", (edit_id,)
            )
            delete_query = "DELETE FROM edit_jobs WHERE id = ?"
            delete_params: tuple[int, ...] = (edit_id,)
            if user_id is not None:
                delete_query += " AND user_id = ?"
                delete_params = (edit_id, user_id)
            cursor = await db.execute(delete_query, delete_params)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    file_result = await _delete_artifact_files(artifacts, storage_dir, preserved)
    return CleanupResult(records_deleted=cursor.rowcount).merge(file_result)


async def reset_source_edits(
    db_path: Path,
    storage_dir: Path,
    source_job_id: int,
    user_id: int,
    *,
    statuses: tuple[str, ...] = ("pending", "rendered", "failed"),
) -> CleanupResult:
    """Reset editable history for one owned source and clean its artifacts."""

    if not statuses:
        return CleanupResult()
    placeholders = ", ".join("?" for _ in statuses)
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT id FROM edit_jobs WHERE source_job_id = ? AND user_id = ? "
            f"AND status IN ({placeholders})",
            (source_job_id, user_id, *statuses),
        ) as cursor:
            edit_ids = [row["id"] for row in await cursor.fetchall()]
    result = CleanupResult()
    for edit_id in edit_ids:
        result = result.merge(
            await delete_edit_job_with_artifacts(
                db_path, storage_dir, edit_id, user_id=user_id
            )
        )
    return result


async def delete_job_with_artifacts(
    db_path: Path,
    storage_dir: Path,
    job_id: int,
    *,
    user_id: int | None = None,
) -> CleanupResult:
    """Delete a job, its edit records, and all unreferenced in-root artifacts."""

    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            query = "SELECT * FROM jobs WHERE id = ?"
            params: tuple[int, ...] = (job_id,)
            if user_id is not None:
                query += " AND user_id = ?"
                params = (job_id, user_id)
            async with db.execute(query, params) as row_cursor:
                job = await row_cursor.fetchone()
            if job is None:
                await db.rollback()
                return CleanupResult()
            async with db.execute(
                "SELECT * FROM edit_jobs WHERE source_job_id = ?", (job_id,)
            ) as edit_row_cursor:
                edits = await edit_row_cursor.fetchall()
            artifacts = _job_artifact_paths(job, storage_dir)
            for edit in edits:
                artifacts.update(_edit_artifact_paths(edit, storage_dir))
            edit_ids = [edit["id"] for edit in edits]
            preserved = await _pool_reference_paths(db)
            await db.execute(
                "UPDATE pool_items SET source_job_id = NULL WHERE source_job_id = ?",
                (job_id,),
            )
            if edit_ids:
                placeholders = ", ".join("?" for _ in edit_ids)
                await db.execute(
                    f"UPDATE pool_items SET edit_job_id = NULL "
                    f"WHERE edit_job_id IN ({placeholders})",
                    edit_ids,
                )
                await db.execute(
                    f"DELETE FROM download_tokens WHERE edit_job_id IN ({placeholders})",
                    edit_ids,
                )
            await db.execute("DELETE FROM download_tokens WHERE job_id = ?", (job_id,))
            edit_cursor = await db.execute(
                "DELETE FROM edit_jobs WHERE source_job_id = ?", (job_id,)
            )
            delete_query = "DELETE FROM jobs WHERE id = ?"
            delete_params: tuple[int, ...] = (job_id,)
            if user_id is not None:
                delete_query += " AND user_id = ?"
                delete_params = (job_id, user_id)
            job_cursor = await db.execute(delete_query, delete_params)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    file_result = await _delete_artifact_files(artifacts, storage_dir, preserved)
    return CleanupResult(
        records_deleted=job_cursor.rowcount + edit_cursor.rowcount
    ).merge(file_result)


async def delete_durable_pool_item(
    db_path: Path,
    storage_dir: Path,
    pool_item_id: int,
    user_id: int,
) -> CleanupResult:
    """Delete a Pool row and its Pool-owned copy, never a referenced job file."""

    async with open_database(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT * FROM pool_items WHERE id = ? AND user_id = ?",
                (pool_item_id, user_id),
            ) as row_cursor:
                row = await row_cursor.fetchone()
            if row is None:
                await db.rollback()
                return CleanupResult()
            # Explicit deletes support legacy schemas whose FKs predate CASCADE.
            await db.execute(
                "DELETE FROM workflow_runs WHERE pool_item_id = ?", (pool_item_id,)
            )
            await db.execute("DELETE FROM pool_tags WHERE pool_item_id = ?", (pool_item_id,))
            cursor = await db.execute(
                "DELETE FROM pool_items WHERE id = ? AND user_id = ?",
                (pool_item_id, user_id),
            )
            remaining_references = await _pool_reference_paths(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    pool_root = _resolved_storage_root(storage_dir) / "pool"
    owned_paths: set[Path] = set()
    preserved_count = 0
    for column in ("file_path", "thumbnail_path"):
        if not row[column]:
            continue
        candidate = _canonical_path(row[column])
        if candidate.is_relative_to(pool_root):
            owned_paths.add(candidate)
        elif candidate.is_file() or candidate.is_symlink():
            # Legacy Pool rows can reference job-owned media. Removing the row
            # must not remove that media.
            preserved_count += 1
    file_result = await _delete_artifact_files(
        owned_paths, storage_dir, remaining_references
    )
    return CleanupResult(
        records_deleted=cursor.rowcount,
        files_preserved=preserved_count,
    ).merge(file_result)


async def cleanup_user_jobs(
    db_path: Path,
    storage_dir: Path,
    user_id: int,
) -> CleanupResult:
    """Delete all jobs owned by a user while retaining saved Pool media."""

    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT id FROM jobs WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cursor:
            job_ids = [row["id"] for row in await cursor.fetchall()]
    result = CleanupResult()
    for job_id in job_ids:
        result = result.merge(
            await delete_job_with_artifacts(
                db_path, storage_dir, job_id, user_id=user_id
            )
        )
    return result


async def cleanup_old_jobs(db_path: Path, storage_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with open_database(db_path) as db:
        async with db.execute(
            "SELECT id FROM jobs "
            "WHERE status IN ('uploaded', 'deleted', 'failed') "
            "AND datetime(updated_at) < datetime(?) ORDER BY id",
            (cutoff.isoformat(),),
        ) as cur:
            job_ids = [row["id"] for row in await cur.fetchall()]
    removed = 0
    for job_id in job_ids:
        result = await delete_job_with_artifacts(db_path, storage_dir, job_id)
        if result.records_deleted:
            removed += 1
    return removed


def _row_to_job(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        url=row["url"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        status=row["status"],
        file_path=row["file_path"],
        file_size=row["file_size"],
        local_api_used=bool(row["local_api_used"]),
        status_message_id=_safe_int(row, "status_message_id"),
        title=row["title"],
        source_caption=row["source_caption"],
        thumbnail_path=row["thumbnail_path"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error_message=row["error_message"],
    )


def _row_to_token(row: aiosqlite.Row) -> DownloadToken:
    return DownloadToken(
        token_hash=row["token_hash"],
        job_id=row["job_id"],
        edit_job_id=_safe_int(row, "edit_job_id"),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        used_at=datetime.fromisoformat(row["used_at"]) if row["used_at"] else None,
        user_id=row["user_id"],
    )


def _row_to_user_settings(row: aiosqlite.Row) -> UserSettings:
    return UserSettings(
        user_id=row["user_id"],
        preset_name=row["preset_name"],
        crop_preset=row["crop_preset"],
        caption_text=row["caption_text"],
        voice_over_voice=row["voice_over_voice"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_preset(row: aiosqlite.Row) -> Preset:
    return Preset(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        crop_preset=row["crop_preset"],
        caption_text=row["caption_text"],
        voice_over_voice=row["voice_over_voice"],
        voice_mode=row["voice_mode"],
        caption_color=row["caption_color"],
        caption_style=row["caption_style"],
        caption_position=row["caption_position"],
        auto_captions=_safe_bool(row, "auto_captions"),
        voice_quality=row["voice_quality"],
        voice_speed=row["voice_speed"],
        voice_text=row["voice_text"],
        tts_engine=row["tts_engine"],
        banner_path=row["banner_path"],
        banner_position=row["banner_position"],
        banner_scale=row["banner_scale"],
        watermark_removal=_safe_bool(row, "watermark_removal"),
        watermark_position=row["watermark_position"],
        watermark_mode=row["watermark_mode"],
        watermark_text=row["watermark_text"],
        channel_banner=_safe_bool(row, "channel_banner"),
        shared=bool(row["shared"]),
        share_code=row["share_code"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_edit_job(row: aiosqlite.Row) -> EditJob:
    return EditJob(
        id=row["id"],
        source_job_id=row["source_job_id"],
        user_id=row["user_id"],
        preset_id=row["preset_id"],
        caption_text=row["caption_text"],
        caption_color=row["caption_color"],
        caption_style=row["caption_style"],
        caption_position=row["caption_position"],
        auto_captions=_safe_bool(row, "auto_captions"),
        voice_text=row["voice_text"],
        voice_over_voice=row["voice_over_voice"],
        voice_mode=row["voice_mode"],
        voice_quality=row["voice_quality"],
        voice_speed=row["voice_speed"],
        tts_engine=row["tts_engine"],
        banner_path=row["banner_path"],
        banner_position=row["banner_position"],
        banner_scale=row["banner_scale"],
        watermark_removal=_safe_bool(row, "watermark_removal"),
        watermark_position=row["watermark_position"],
        watermark_mode=row["watermark_mode"],
        watermark_text=row["watermark_text"],
        watermark_analysis=row["watermark_analysis"],
        watermark_confidence=row["watermark_confidence"],
        watermark_candidates=row["watermark_candidates"],
        watermark_preview_path=row["watermark_preview_path"],
        channel_banner=_safe_bool(row, "channel_banner"),
        subtitles_path=row["subtitles_path"],
        status=row["status"],
        file_path=row["file_path"],
        file_size=row["file_size"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error_message=row["error_message"],
        metadata_status=row["metadata_status"],
        metadata_description=row["metadata_description"],
        metadata_hashtags=row["metadata_hashtags"],
        metadata_error=row["metadata_error"],
        metadata_attempt_count=int(row["metadata_attempt_count"] or 0),
        metadata_requested_at=_optional_datetime(row, "metadata_requested_at"),
        metadata_started_at=_optional_datetime(row, "metadata_started_at"),
        metadata_completed_at=_optional_datetime(row, "metadata_completed_at"),
        metadata_model=row["metadata_model"],
        metadata_reasoning_effort=row["metadata_reasoning_effort"],
        metadata_progress_message_id=_safe_int(row, "metadata_progress_message_id"),
        metadata_result_message_id=_safe_int(row, "metadata_result_message_id"),
        metadata_reply_message_id=_safe_int(row, "metadata_reply_message_id"),
        render_delivery_message_id=_safe_int(row, "render_delivery_message_id"),
        render_status_message_id=_safe_int(row, "render_status_message_id"),
    )


def _row_to_pool_item(row: aiosqlite.Row) -> PoolItem:
    return PoolItem(
        id=row["id"],
        user_id=row["user_id"],
        source_job_id=row["source_job_id"],
        edit_job_id=row["edit_job_id"],
        file_path=row["file_path"],
        file_size=row["file_size"],
        thumbnail_path=row["thumbnail_path"],
        title=row["title"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_classification(row: aiosqlite.Row) -> Classification:
    return Classification(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        color=row["color"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_pool_tag(row: aiosqlite.Row) -> PoolTag:
    return PoolTag(
        id=row["id"],
        pool_item_id=row["pool_item_id"],
        classification_id=row["classification_id"],
        user_id=row["user_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_workflow(row: aiosqlite.Row) -> Workflow:
    return Workflow(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        trigger_classification_id=row["trigger_classification_id"],
        action_type=row["action_type"],
        action_preset_id=row["action_preset_id"],
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _safe_int(row: aiosqlite.Row, key: str) -> int | None:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _safe_bool(row: aiosqlite.Row, key: str) -> bool:
    try:
        return bool(row[key])
    except (IndexError, KeyError):
        return False


def _optional_datetime(row: aiosqlite.Row, key: str) -> datetime | None:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return None
    return datetime.fromisoformat(value) if value else None


def _row_to_workflow_run(row: aiosqlite.Row) -> WorkflowRun:
    return WorkflowRun(
        id=row["id"],
        workflow_id=row["workflow_id"],
        pool_item_id=row["pool_item_id"],
        user_id=row["user_id"],
        status=row["status"],
        error_message=row["error_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
