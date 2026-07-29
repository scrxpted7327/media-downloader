from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Self

import aiosqlite


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
    voice_quality: str | None
    voice_speed: float | None
    tts_engine: str | None
    banner_path: str | None
    banner_position: str | None
    banner_scale: str | None
    watermark_removal: bool
    watermark_position: str | None
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


async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                file_path TEXT,
                file_size INTEGER,
                local_api_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS download_tokens (
                token_hash TEXT PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
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
                voice_quality TEXT,
                voice_speed REAL,
                shared INTEGER NOT NULL DEFAULT 0,
                share_code TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS edit_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_job_id INTEGER NOT NULL REFERENCES jobs(id),
                user_id INTEGER NOT NULL,
                preset_id INTEGER REFERENCES presets(id),
                status TEXT NOT NULL DEFAULT 'pending',
                file_path TEXT,
                file_size INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS shared_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_id INTEGER NOT NULL REFERENCES presets(id),
                user_id INTEGER NOT NULL,
                share_code TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_tokens_job ON download_tokens(job_id);
            CREATE INDEX IF NOT EXISTS idx_tokens_expires ON download_tokens(expires_at);
            CREATE INDEX IF NOT EXISTS idx_presets_user ON presets(user_id);
            CREATE INDEX IF NOT EXISTS idx_edit_jobs_source ON edit_jobs(source_job_id);
            CREATE INDEX IF NOT EXISTS idx_edit_jobs_user ON edit_jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_shared_presets_code ON shared_presets(share_code);
            CREATE TABLE IF NOT EXISTS pool_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_job_id INTEGER REFERENCES jobs(id),
                edit_job_id INTEGER REFERENCES edit_jobs(id),
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
                classification_id INTEGER NOT NULL REFERENCES classifications(id),
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(pool_item_id, classification_id)
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                trigger_classification_id INTEGER REFERENCES classifications(id),
                action_type TEXT NOT NULL,
                action_preset_id INTEGER REFERENCES presets(id),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER NOT NULL REFERENCES workflows(id),
                pool_item_id INTEGER NOT NULL REFERENCES pool_items(id),
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pool_user ON pool_items(user_id);
            CREATE INDEX IF NOT EXISTS idx_pool_status ON pool_items(status);
            CREATE INDEX IF NOT EXISTS idx_tags_item ON pool_tags(pool_item_id);
            CREATE INDEX IF NOT EXISTS idx_tags_class ON pool_tags(classification_id);
            CREATE INDEX IF NOT EXISTS idx_workflows_user ON workflows(user_id);
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_item ON workflow_runs(pool_item_id);
            CREATE TABLE IF NOT EXISTS download_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_dlmsg_expires ON download_messages(expires_at);
        """)
        for _table, _cols in [
            ("download_tokens", [
                ("edit_job_id", "INTEGER REFERENCES edit_jobs(id) ON DELETE CASCADE"),
            ]),
            ("presets", [
                ("auto_captions", "INTEGER NOT NULL DEFAULT 0"),
                ("voice_text", "TEXT"),
                ("tts_engine", "TEXT"),
                ("banner_path", "TEXT"),
                ("banner_position", "TEXT"),
                ("banner_scale", "TEXT"),
                ("watermark_removal", "INTEGER NOT NULL DEFAULT 0"),
                ("watermark_position", "TEXT"),
                ("channel_banner", "INTEGER NOT NULL DEFAULT 0"),
            ]),
            ("jobs", [
                ("status_message_id", "INTEGER"),
                ("title", "TEXT"),
                ("source_caption", "TEXT"),
                ("thumbnail_path", "TEXT"),
            ]),
            ("edit_jobs", [
                ("caption_text", "TEXT"),
                ("caption_color", "TEXT"),
                ("caption_style", "TEXT"),
                ("caption_position", "TEXT"),
                ("auto_captions", "INTEGER NOT NULL DEFAULT 0"),
                ("voice_text", "TEXT"),
                ("voice_over_voice", "TEXT"),
                ("voice_quality", "TEXT"),
                ("voice_speed", "REAL"),
                ("tts_engine", "TEXT"),
                ("banner_path", "TEXT"),
                ("banner_position", "TEXT"),
                ("banner_scale", "TEXT"),
                ("watermark_removal", "INTEGER NOT NULL DEFAULT 0"),
                ("watermark_position", "TEXT"),
                ("watermark_analysis", "TEXT"),
                ("watermark_confidence", "REAL"),
                ("watermark_candidates", "TEXT"),
                ("watermark_preview_path", "TEXT"),
                ("channel_banner", "INTEGER NOT NULL DEFAULT 0"),
                ("subtitles_path", "TEXT"),
            ]),
            ("pool_items", [
                ("edit_job_id", "INTEGER REFERENCES edit_jobs(id)"),
            ]),
        ]:
            for col, ctype in _cols:
                try:
                    await db.execute(f"ALTER TABLE {_table} ADD COLUMN {col} {ctype}")
                except aiosqlite.OperationalError:
                    pass
        await db.commit()


async def create_job(db_path: Path, url: str, user_id: int, chat_id: int) -> JobRecord:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [job_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE jobs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None


async def get_job(db_path: Path, job_id: int) -> JobRecord | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_job(row) if row else None


async def list_user_jobs(db_path: Path, user_id: int, limit: int = 20) -> list[JobRecord]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(row) for row in rows]


async def list_all_jobs(db_path: Path, limit: int = 30) -> list[JobRecord]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiry_minutes)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO download_tokens "
            "(token_hash, job_id, edit_job_id, expires_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (token_hash, job_id, edit_job_id, expires_at.isoformat(), user_id),
        )
        await db.commit()
    return raw_token


async def consume_download_token(db_path: Path, raw_token: str) -> DownloadToken | None:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        now = datetime.now(timezone.utc).isoformat()
        async with db.execute(
            "SELECT * FROM download_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
            (token_hash, now),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await db.execute(
            "UPDATE download_tokens SET used_at = ? WHERE token_hash = ?",
            (now, token_hash),
        )
        await db.commit()
    return _row_to_token(row)


async def get_or_create_user_settings(db_path: Path, user_id: int) -> UserSettings:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            return _row_to_user_settings(row)
        await db.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        await db.commit()
        async with db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user_settings(row)


async def update_user_settings(db_path: Path, user_id: int, **kwargs) -> UserSettings:
    if not kwargs:
        return await get_or_create_user_settings(db_path, user_id)
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values())
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO presets (user_id, name, crop_preset, caption_text, voice_over_voice, "
            "caption_color, caption_style, caption_position, auto_captions, voice_quality, voice_speed, "
            "voice_text, tts_engine, banner_path, banner_position, banner_scale, "
            "watermark_removal, watermark_position, channel_banner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                kwargs.get("banner_path"),
                kwargs.get("banner_position"),
                kwargs.get("banner_scale"),
                int(bool(kwargs.get("watermark_removal"))),
                kwargs.get("watermark_position"),
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
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id)) as cur:
                row = await cur.fetchone()
            return _row_to_preset(row) if row else None
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [preset_id, user_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE presets SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id)) as cur:
            row = await cur.fetchone()
        return _row_to_preset(row) if row else None


async def list_presets(db_path: Path, user_id: int) -> list[Preset]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM presets WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_preset(row) for row in rows]


async def delete_preset(db_path: Path, user_id: int, preset_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def get_preset_by_share_code(db_path: Path, share_code: str) -> Preset | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM presets WHERE share_code = ?", (share_code,)) as cur:
            row = await cur.fetchone()
        return _row_to_preset(row) if row else None


async def share_preset(db_path: Path, preset_id: int, user_id: int) -> str | None:
    share_code = secrets.token_urlsafe(16)
    try:
        async with aiosqlite.connect(db_path) as db:
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_edit_job(row) if row else None
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [edit_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE edit_jobs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def get_edit_job(db_path: Path, edit_id: int) -> EditJob | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM edit_jobs WHERE id = ?", (edit_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_edit_job(row) if row else None


async def list_source_jobs_for_user(db_path: Path, user_id: int, limit: int = 20) -> list[JobRecord]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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


async def get_saved_source_pool_item(
    db_path: Path, user_id: int, source_job_id: int
) -> PoolItem | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pool_items "
            "WHERE user_id = ? AND source_job_id = ? AND edit_job_id IS NOT NULL "
            "ORDER BY created_at DESC",
            (user_id, source_job_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_pool_item(row) for row in rows]


async def get_pool_item(db_path: Path, pool_item_id: int) -> PoolItem | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pool_items WHERE id = ?", (pool_item_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def list_pool_items(db_path: Path, user_id: int, status: str | None = None, classification_id: int | None = None, limit: int = 50) -> list[PoolItem]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [pool_item_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE pool_items SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM pool_items WHERE id = ?", (pool_item_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_pool_item(row) if row else None


async def delete_pool_item(db_path: Path, pool_item_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM pool_items WHERE id = ? AND user_id = ?", (pool_item_id, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def create_classification(db_path: Path, name: str, description: str | None = None, color: str | None = None) -> Classification:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM classifications WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            return _row_to_classification(row)
        cursor = await db.execute(
            "INSERT INTO classifications (name, description, color) VALUES (?, ?, ?)",
            (name, description, color),
        )
        await db.commit()
        class_id = cursor.lastrowid
        async with db.execute("SELECT * FROM classifications WHERE id = ?", (class_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_classification(row)


async def get_classification(db_path: Path, classification_id: int) -> Classification | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM classifications WHERE id = ?", (classification_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_classification(row) if row else None


async def list_classifications(db_path: Path) -> list[Classification]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM classifications ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [_row_to_classification(row) for row in rows]


async def add_pool_tag(db_path: Path, pool_item_id: int, classification_id: int, user_id: int) -> PoolTag | None:
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM pool_tags WHERE pool_item_id = ? AND classification_id = ?",
            (pool_item_id, classification_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_pool_tags(db_path: Path, pool_item_id: int) -> list[PoolTag]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pool_tags WHERE pool_item_id = ?", (pool_item_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_pool_tag(row) for row in rows]


async def create_workflow(db_path: Path, user_id: int, name: str, action_type: str, trigger_classification_id: int | None = None, action_preset_id: int | None = None) -> Workflow:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None


async def list_workflows(db_path: Path, user_id: int) -> list[Workflow]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM workflows WHERE user_id = ? ORDER BY name", (user_id,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_workflow(row) for row in rows]


async def update_workflow(db_path: Path, workflow_id: int, user_id: int, **kwargs) -> Workflow | None:
    if not kwargs:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)) as cur:
                row = await cur.fetchone()
            return _row_to_workflow(row) if row else None
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [workflow_id, user_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            f"UPDATE workflows SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            values,
        )
        await db.commit()
        async with db.execute("SELECT * FROM workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow(row) if row else None


async def delete_workflow(db_path: Path, workflow_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM workflows WHERE id = ? AND user_id = ?", (workflow_id, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def create_workflow_run(db_path: Path, workflow_id: int, pool_item_id: int, user_id: int) -> WorkflowRun:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "INSERT INTO workflow_runs (workflow_id, pool_item_id, user_id) VALUES (?, ?, ?)",
            (workflow_id, pool_item_id, user_id),
        )
        await db.commit()
        run_id = cursor.lastrowid
        async with db.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_workflow_run(row)


async def update_workflow_run(db_path: Path, run_id: int, **kwargs) -> WorkflowRun | None:
    if not kwargs:
        async with db.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_workflow_run(row) if row else None
    set_clause = ", ".join(f"{key} = ?" for key in kwargs)
    values = list(kwargs.values()) + [run_id]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO download_messages (chat_id, message_id, expires_at) VALUES (?, ?, ?)",
            (chat_id, message_id, expires_at.isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def list_expired_download_messages(db_path: Path) -> list[DownloadMessage]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
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
    async with aiosqlite.connect(db_path) as db:
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
        except Exception:
            pass
        await mark_download_message_deleted(db_path, msg.id)
        removed += 1
    return removed


async def cleanup_expired_tokens(db_path: Path) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM download_tokens WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
        return cursor.rowcount


async def cleanup_old_jobs(db_path: Path, storage_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, file_path, thumbnail_path FROM jobs "
            "WHERE status IN ('uploaded', 'deleted') AND created_at < ?",
            (cutoff.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row["file_path"]:
                path = Path(row["file_path"])
                if path.is_file():
                    path.unlink(missing_ok=True)
            if "thumbnail_path" in row.keys() and row["thumbnail_path"]:
                Path(row["thumbnail_path"]).unlink(missing_ok=True)
            await db.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
        await db.commit()
    return len(rows)


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
        voice_quality=row["voice_quality"],
        voice_speed=row["voice_speed"],
        tts_engine=row["tts_engine"],
        banner_path=row["banner_path"],
        banner_position=row["banner_position"],
        banner_scale=row["banner_scale"],
        watermark_removal=_safe_bool(row, "watermark_removal"),
        watermark_position=row["watermark_position"],
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
    except IndexError:
        return None


def _safe_bool(row: aiosqlite.Row, key: str) -> bool:
    try:
        return bool(row[key])
    except IndexError:
        return False


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
