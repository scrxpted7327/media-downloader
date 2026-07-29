from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from .config import Settings
from .downloader import (
    DownloadError,
    create_thumbnail,
    download_instagram,
    download_media,
    download_tiktok_slideshow,
    persist_download,
    read_source_metadata,
)
from .download_server import create_download_app
from .diagnostics import append_event, install_event_logging, recent_events
from .editor import list_tts_voices, render_edit
from .error_handler import error_handler
from .fix_agent import (
    ERRORS_DIR,
    FIX_SCRIPTS_DIR,
    apply_known_fix,
    categorize_error,
    invoke_opencode_fix,
    load_error_log,
    run_fix_script,
    validate_model,
)
from .platforms import extract_supported_urls, is_instagram_url, is_tiktok_photo_url, is_tiktok_url
from .settings_ui import (
    _effective_edit_snapshot,
    handle_editconfig_callback,
    settings_callback,
    settings_command,
    settings_photo_handler,
    settings_text_handler,
    show_editconfig_menu,
)
from .storage import (
    cleanup_download_messages,
    cleanup_expired_tokens,
    cleanup_old_jobs,
    consume_download_token,
    create_download_token,
    create_edit_job,
    create_job,
    create_pool_item,
    create_preset,
    delete_pool_item,
    get_edit_job,
    get_job,
    get_saved_edit_pool_item,
    get_saved_source_pool_item,
    init_db,
    list_all_jobs,
    list_presets,
    list_source_jobs_for_user,
    list_user_jobs,
    mark_download_message_deleted,
    stage_edit_source,
    store_download_message,
    update_edit_job,
    update_job,
)
from .tools import prefer_ffmpeg_full, provision_ytdlp
from .watermark import WatermarkAnalysis, analyze_video, create_preview

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
install_event_logging()

HELP_TEXT = (
    "Commands:\n"
    "/start - show this help message\n"
    "/help - show this message\n"
    "/settings - video presets and customization\n"
    "/pool - manage video pool and workflows\n"
    "/jobs - list your recent downloads\n"
    "/editconfig - set options for the current edit job\n"
    "/delete <job_id> - delete a downloaded file\n"
    "/cleanup - delete bot status messages from recent jobs\n"
    "/voices - list available TTS voices\n"
    "/fix [provider/model] - run the AI repair agent for bot errors\n"
    "/status - bot error status and health\n\n"
    "/report <issue> - send recent activity and errors to the AI repair agent\n\n"
    "Send a YouTube, Instagram, TikTok, or Facebook link anywhere in a message. "
    "The bot downloads the first supported link it finds and provides a secure download link."
)


class DownloadReporter:
    """Show download progress with an ETA based on transferred percent."""

    _MIN_EDIT_INTERVAL = 2.5

    def __init__(self, message) -> None:
        self.message = message
        self.start_time = time.monotonic()
        self.last_pct = -1
        self.last_edit = 0.0

    async def progress(self, pct: int) -> None:
        pct = max(0, min(99, int(pct)))
        now = time.monotonic()
        if pct == self.last_pct:
            return
        if self.last_edit and (now - self.last_edit) < self._MIN_EDIT_INTERVAL:
            return
        self.last_pct = pct
        self.last_edit = now

        elapsed = now - self.start_time
        bar_len = 10
        filled = pct * bar_len // 100
        bar = "▓" * filled + "░" * (bar_len - filled)
        eta_line = _format_eta_line(elapsed, pct)
        text = f"⬇️ Downloading…\n{bar} {pct}%\n{eta_line}"
        try:
            await self.message.edit_text(text)
        except Exception:
            pass


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def _format_eta_line(elapsed: float, pct: int) -> str:
    if pct <= 0 or elapsed < 1:
        return "⏱ calculating…"
    eta_seconds = elapsed * (100 - pct) / pct
    if eta_seconds < 1:
        return "⏱ almost done"
    return f"⏱ {_format_duration(eta_seconds)} left"


async def _safe_status_edit(message, text: str) -> bool:
    """Best-effort cosmetic edit that never interrupts delivery."""
    try:
        await message.edit_text(text)
        return True
    except RetryAfter as exc:
        LOGGER.warning("Status edit rate-limited for %ss; continuing delivery", exc.retry_after)
    except Exception as exc:
        LOGGER.warning("Status edit failed; continuing delivery: %s", exc)
    return False


def _retry_seconds(value) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


async def _send_document_with_retry(
    message,
    path: Path,
    caption: str,
    timeout: int,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a document, honoring Telegram's requested flood-control delay."""
    for attempt in range(2):
        try:
            with path.open("rb") as document:
                await message.reply_document(
                    document=document,
                    caption=caption,
                    reply_markup=reply_markup,
                    read_timeout=timeout,
                    write_timeout=timeout,
                    connect_timeout=timeout,
                    pool_timeout=timeout,
                )
            return
        except RetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(_retry_seconds(exc.retry_after) + 1)


class _ProgressReporter:
    """Reports rendering progress to a Telegram message with step names and ETA."""

    _MIN_EDIT_INTERVAL = 2.5

    def __init__(self, message, steps: list[str]) -> None:
        self.message = message
        self.steps = steps
        self.start_time = time.monotonic()
        self.step_idx = 0
        self.last_pct = -1
        self.last_edit = 0.0

    def set_step(self, idx: int) -> None:
        self.step_idx = idx

    async def __call__(self, pct: int) -> None:
        step_idx = self.step_idx
        step_name = self.steps[step_idx] if step_idx < len(self.steps) else "Finalizing"
        elapsed = time.monotonic() - self.start_time

        step_weight = 100.0 / len(self.steps)
        overall = int(step_idx * step_weight + (pct * step_weight / 100.0))
        overall = min(99, overall)

        now = time.monotonic()
        if overall == self.last_pct:
            return
        if self.last_edit and (now - self.last_edit) < self._MIN_EDIT_INTERVAL:
            return
        self.last_pct = overall
        self.last_edit = now

        bar_len = 10
        filled = overall * bar_len // 100
        bar = "▓" * filled + "░" * (bar_len - filled)

        text = (
            f"🎬 Step {step_idx + 1}/{len(self.steps)}: {step_name}\n"
            f"{bar} {overall}%\n"
            f"{_format_eta_line(elapsed, overall)}"
        )
        try:
            await self.message.edit_text(text)
        except Exception:
            pass


def _authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if chat.type == "channel":
        return chat.id in settings.allowed_chat_ids
    return bool(user and user.id in settings.allowed_user_ids) or chat.id in settings.allowed_chat_ids


async def _download_actions_keyboard(
    job_id: int, db_path: Path, user_id: int
) -> InlineKeyboardMarkup:
    saved = await get_saved_source_pool_item(db_path, user_id, job_id)
    rows = [
        [InlineKeyboardButton("⬇️ Download Original", callback_data=f"download:orig:{job_id}")],
        [InlineKeyboardButton("✂️ Edit", callback_data=f"download:edit:{job_id}"),
         InlineKeyboardButton("🔄 Reset", callback_data=f"download:reset:{job_id}")],
        [InlineKeyboardButton(
            "🗑️ Remove Original from Pool" if saved else "💾 Save Original to Pool",
            callback_data=(
                f"download:poolremoveconfirm:{job_id}"
                if saved
                else f"download:poolsave:{job_id}"
            ),
        )],
    ]
    from .storage import get_or_create_user_settings, list_presets
    presets = await list_presets(db_path, user_id)
    settings = await get_or_create_user_settings(db_path, user_id)
    active_name = settings.preset_name
    active = next((p for p in presets if active_name and p.name == active_name), None)
    ordered = ([active] if active is not None else []) + [
        p for p in presets if active is None or p.id != active.id
    ]
    for preset in ordered[:3]:
        mark = "⭐ " if active is not None and preset.id == active.id else ""
        rows.append([InlineKeyboardButton(
            f"{mark}📥 Download {preset.name}",
            callback_data=f"download:preset:{job_id}:{preset.id}",
        )])
    if len(ordered) > 3:
        rows.append([InlineKeyboardButton(
            f"📦 More presets ({len(ordered) - 3})",
            callback_data=f"download:presets:{job_id}",
        )])
    return InlineKeyboardMarkup(rows)


def _render_pool_keyboard(edit_id: int, *, saved: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🗑️ Unsave from Pool" if saved else "💾 Save to Pool",
            callback_data=(
                f"download:editunsaveconfirm:{edit_id}"
                if saved
                else f"download:editsave:{edit_id}"
            ),
        )
    ]])


async def _send_secure_link(status_message, job_id: int, db_path: Path, user_id: int) -> None:
    keyboard = await _download_actions_keyboard(job_id, db_path, user_id)
    job = await get_job(db_path, job_id)
    details = []
    if job and job.title:
        details.append(job.title[:200])
    if job and job.source_caption:
        caption = " ".join(job.source_caption.split())
        details.append(caption[:300] + ("…" if len(caption) > 300 else ""))
    suffix = "\n\n" + "\n".join(details) if details else ""
    text = f"✅ Download complete. Choose an action:{suffix}"
    thumbnail = Path(job.thumbnail_path) if job and job.thumbnail_path else None
    if thumbnail is not None and thumbnail.is_file():
        with thumbnail.open("rb") as photo:
            await status_message.reply_photo(
                photo=photo,
                caption=text,
                reply_markup=keyboard,
            )
        try:
            await status_message.delete()
        except Exception:
            pass
    else:
        await status_message.edit_text(text, reply_markup=keyboard)


async def _send_render_download_link(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    edit,
    settings: Settings,
    db_path: Path,
) -> str:
    """Send a usable edit link before attempting the slower Telegram upload."""
    token = await create_download_token(
        db_path,
        edit.source_job_id,
        edit.user_id,
        settings.token_expiry_minutes,
        edit_job_id=edit.id,
    )
    url = _build_download_url(settings, token)
    link_message = await message.reply_text(
        f"⬇️ Direct download ({settings.token_expiry_minutes} min):\n{url}",
        disable_web_page_preview=True,
        reply_markup=_render_pool_keyboard(edit.id, saved=False),
    )
    record_id = await store_download_message(
        db_path, link_message.chat_id, link_message.message_id,
        settings.token_expiry_minutes,
    )
    context.job_queue.run_once(
        _delete_expired_link,
        settings.token_expiry_minutes * 60,
        data={
            "chat_id": link_message.chat_id,
            "message_id": link_message.message_id,
            "db_path": str(db_path),
            "msg_record_id": record_id,
        },
    )
    return url


def _build_download_url(settings: Settings, token: str) -> str:
    domain = settings.download_domain or "localhost"
    return f"https://{domain}/download/{token}"


async def _delete_expired_link(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception as exc:
        LOGGER.warning("Failed to delete expired download message: %s", exc)
    await mark_download_message_deleted(data["db_path"], data["msg_record_id"])


_RENDER_QUEUE: asyncio.Queue = asyncio.Queue()


async def _message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        LOGGER.warning("Rejected update for unapproved chat/user")
        return

    handled = await settings_text_entry(update, context)
    if handled:
        return
    handled = await editconfig_text(update, context)
    if handled:
        return
    handled = await pool_text_entry(update, context)
    if handled:
        return
    handled = await settings_photo_entry(update, context)
    if handled:
        return
    await handle_url(update, context)


async def settings_photo_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _authorized(update, context.application.bot_data["settings"]):
        return False
    return await settings_photo_handler(update, context)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    ytdlp = context.application.bot_data["ytdlp"]
    gallerydl: Path = context.application.bot_data["gallerydl"]
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected update for unapproved chat/user")
        return

    user = update.effective_user
    user_id = user.id if user else 0
    chat_id = message.chat_id

    text = message.text or message.caption or ""
    urls = extract_supported_urls(text)
    if not urls:
        await message.reply_text("Send one supported YouTube, Instagram, TikTok, or Facebook URL.")
        return

    results = []
    for idx, url in enumerate(urls[:8]):
        if idx > 0:
            await asyncio.sleep(2)
        result = await _process_single_url(
            update, context, url, user_id, chat_id,
            settings, ytdlp, gallerydl, db_path, storage_dir,
        )
        results.append(result)

    if len(urls) > 1:
        lines = [f"Processed {len(results)}/{len(urls)} URLs:"]
        for r in results:
            lines.append(f"  {r}")
        await message.reply_text("\n".join(lines))


async def _process_single_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    url: str, user_id: int, chat_id: int,
    settings: Settings, ytdlp: Path, gallerydl: Path,
    db_path: Path, storage_dir: Path,
) -> str:
    status = await update.effective_message.reply_text("🔍 Searching…")
    job = await create_job(db_path, url, user_id, chat_id)
    await update_job(db_path, job.id, status_message_id=status.message_id)
    temporary = None
    try:
        if is_tiktok_photo_url(url):
            temporary, media = await download_tiktok_slideshow(
                gallerydl, url, settings.max_filesize_mb, settings.timeout_seconds,
                DownloadReporter(status).progress,
            )
        elif is_instagram_url(url):
            temporary, media = await download_instagram(
                gallerydl, url, settings.max_filesize_mb, settings.timeout_seconds,
                DownloadReporter(status).progress,
            )
        else:
            try:
                temporary, media = await download_media(
                    ytdlp, url, settings.max_filesize_mb, settings.timeout_seconds,
                    DownloadReporter(status).progress,
                )
            except DownloadError as exc:
                if is_tiktok_url(url):
                    LOGGER.info("yt-dlp failed on TikTok URL, trying gallery-dl: %s (%s)", url, exc)
                    temporary, media = await download_tiktok_slideshow(
                        gallerydl, url, settings.max_filesize_mb, settings.timeout_seconds,
                        DownloadReporter(status).progress,
                    )
                else:
                    raise
        title, source_caption = read_source_metadata(media.parent)
        await status.edit_text("💾 Saving…")
        persisted = await persist_download(media, job.id, storage_dir)
        thumbnail = await create_thumbnail(
            persisted,
            storage_dir / f"{job.id}-thumbnail.jpg",
        )
        await update_job(
            db_path,
            job.id,
            file_path=str(persisted),
            file_size=persisted.stat().st_size,
            title=title,
            source_caption=source_caption,
            thumbnail_path=str(thumbnail) if thumbnail else None,
        )
        await _send_secure_link(status, job.id, db_path, user_id)
        await update_job(db_path, job.id, status="uploaded", local_api_used=bool(settings.local_api_url))
        return f"#{job.id}: OK"
    except DownloadError as exc:
        LOGGER.warning("Download failed for %s: %s", url, exc)
        try:
            await status.edit_text(f"❌ Download failed: {exc}")
        except Exception:
            pass
        await update_job(db_path, job.id, status="failed", error_message=str(exc))
        return f"#{job.id}: FAILED"
    except Exception as exc:
        LOGGER.exception("Unexpected failure for %s", url)
        try:
            await status.edit_text(f"❌ Download failed: {exc.__class__.__name__}: {exc}")
        except Exception:
            pass
        await update_job(db_path, job.id, status="failed", error_message=f"unexpected error: {exc}")
        return f"#{job.id}: ERROR"
    finally:
        if temporary is not None:
            temporary.cleanup()


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    query = update.callback_query
    if query is None or query.data is None:
        return
    if not _authorized(update, settings):
        await query.answer("Not authorized", show_alert=True)
        return
    parts = query.data.split(":")
    action, job_id_str = parts[1], parts[2]
    try:
        job_id = int(job_id_str)
    except ValueError:
        await query.answer("Invalid job", show_alert=True)
        return

    current_user_id = query.from_user.id

    if action in {"editsave", "editunsaveconfirm", "editunsave", "editunsavecancel"}:
        edit = await get_edit_job(db_path, job_id)
        if edit is None or edit.user_id != current_user_id or not edit.file_path:
            await query.answer("Rendered edit not found", show_alert=True)
            return
        saved = await get_saved_edit_pool_item(db_path, current_user_id, edit.id)
        if action == "editsave":
            if saved is None:
                saved = await create_pool_item(
                    db_path,
                    current_user_id,
                    edit.file_path,
                    source_job_id=edit.source_job_id,
                    edit_job_id=edit.id,
                    file_size=edit.file_size,
                    title=f"Edit #{edit.id}",
                )
            await query.answer("Saved to pool")
            await query.edit_message_reply_markup(
                reply_markup=_render_pool_keyboard(edit.id, saved=True)
            )
            return
        if action == "editunsaveconfirm":
            await query.answer()
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⚠️ Confirm Unsave",
                        callback_data=f"download:editunsave:{edit.id}",
                    ),
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=f"download:editunsavecancel:{edit.id}",
                    ),
                ]])
            )
            return
        if action == "editunsave":
            if saved is not None:
                await delete_pool_item(db_path, saved.id, current_user_id)
            await query.answer("Removed from pool")
            await query.edit_message_reply_markup(
                reply_markup=_render_pool_keyboard(edit.id, saved=False)
            )
            return
        await query.answer("Kept saved")
        await query.edit_message_reply_markup(
            reply_markup=_render_pool_keyboard(edit.id, saved=saved is not None)
        )
        return

    job = await get_job(db_path, job_id)
    if job is None or job.file_path is None:
        await query.answer("File not found", show_alert=True)
        return

    if action == "poolsave":
        saved = await get_saved_source_pool_item(db_path, current_user_id, job.id)
        if saved is None:
            await create_pool_item(
                db_path,
                current_user_id,
                job.file_path,
                source_job_id=job.id,
                file_size=job.file_size,
                thumbnail_path=job.thumbnail_path,
                title=job.title or f"Original #{job.id}",
            )
        await query.answer("Original saved to pool")
        await query.edit_message_reply_markup(
            reply_markup=await _download_actions_keyboard(job.id, db_path, current_user_id)
        )
        return

    if action == "poolremoveconfirm":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⚠️ Confirm Remove",
                    callback_data=f"download:poolremove:{job.id}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"download:actions:{job.id}",
                ),
            ]])
        )
        return

    if action == "poolremove":
        saved = await get_saved_source_pool_item(db_path, current_user_id, job.id)
        if saved is not None:
            await delete_pool_item(db_path, saved.id, current_user_id)
        await query.answer("Original removed from pool")
        await query.edit_message_reply_markup(
            reply_markup=await _download_actions_keyboard(job.id, db_path, current_user_id)
        )
        return

    if action == "presets":
        await query.answer()
        from .storage import get_or_create_user_settings, list_presets
        presets = await list_presets(db_path, current_user_id)
        user_settings = await get_or_create_user_settings(db_path, current_user_id)
        active_name = user_settings.preset_name
        rows = []
        for p in presets:
            mark = "⭐ " if active_name and p.name == active_name else ""
            rows.append([InlineKeyboardButton(
                f"{mark}📥 {p.name}",
                callback_data=f"download:preset:{job_id}:{p.id}",
            )])
        rows.append([InlineKeyboardButton("← Back", callback_data=f"download:actions:{job_id}")])
        await query.edit_message_text(
            "📦 Choose a preset to render with:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if action == "actions":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=await _download_actions_keyboard(job_id, db_path, current_user_id)
        )
        return

    if action == "orig":
        await query.answer()
        token = await create_download_token(db_path, job.id, current_user_id, settings.token_expiry_minutes)
        url = _build_download_url(settings, token)
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Download ready (one-time, {settings.token_expiry_minutes} min):\n{url}",
            reply_to_message_id=query.message.message_id,
            disable_web_page_preview=True,
        )
        msg_record_id = await store_download_message(db_path, msg.chat_id, msg.message_id, settings.token_expiry_minutes)
        context.job_queue.run_once(
            _delete_expired_link,
            settings.token_expiry_minutes * 60,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id, "db_path": str(db_path), "msg_record_id": msg_record_id},
        )
        return

    if action == "edit":
        source_path = Path(job.file_path)
        if not source_path.is_file():
            await query.answer("Source file missing", show_alert=True)
            return
        await query.answer()
        edit = await create_edit_job(db_path, job_id, current_user_id, preset_id=None)
        dest = storage_dir / f"edit-{edit.id}-{source_path.name}"
        file_size = await stage_edit_source(source_path, dest)
        await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=file_size)
        context.user_data["settings_flow"] = {
            "action": "editconfig",
            "edit_id": edit.id,
            "source_job_id": job_id,
        }
        await show_editconfig_menu(
            update,
            context,
            edit.id,
            intro=f"🎬 Edit job #{edit.id} created.\nCurrent settings are shown on each button.",
        )
        return

    if action == "reset":
        await query.answer()
        async with aiosqlite.connect(db_path) as db:
            await db.execute("DELETE FROM edit_jobs WHERE source_job_id = ? AND user_id = ? AND status IN ('pending', 'rendered')", (job_id, current_user_id))
            await db.commit()
        flow = context.user_data.get("settings_flow")
        flow_action = flow.get("action") if isinstance(flow, dict) else getattr(flow, "action", None)
        if flow_action == "editconfig":
            context.user_data.pop("settings_flow", None)
        await query.edit_message_text("Edit config reset for this download.")
        return

    if action == "preset":
        await query.answer()
        preset_id = int(parts[3])
        from .storage import list_presets
        preset = next((p for p in await list_presets(db_path, current_user_id) if p.id == preset_id), None)
        if preset is None:
            await query.edit_message_text("Preset not found.")
            return
        source_path = Path(job.file_path)
        if not source_path.is_file():
            await query.edit_message_text("Source file missing.")
            return
        edit = await create_edit_job(db_path, job_id, current_user_id, preset_id)
        dest = storage_dir / f"edit-{edit.id}-{source_path.name}"
        file_size = await stage_edit_source(source_path, dest)
        await update_edit_job(
            db_path, edit.id,
            file_path=str(dest), file_size=file_size,
            auto_captions=preset.auto_captions,
            watermark_removal=preset.watermark_removal,
            channel_banner=preset.channel_banner,
        )
        await query.edit_message_text(f"🎬 Rendering with \"{preset.name}\"…")
        asyncio.create_task(_render_edit_job(update, context, edit.id))
        return


async def voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected voices request for unapproved chat/user")
        return
    await message.reply_chat_action(ChatAction.TYPING)
    try:
        voices = await list_tts_voices()
    except Exception as exc:
        await message.reply_text(f"Could not list voices: {exc}")
        return
    if not voices:
        await message.reply_text("No TTS voices found.")
        return
    groups: dict[str, list[str]] = {}
    for v in voices:
        key = v["locale"].split("-")[0] if "-" in v["locale"] else v["locale"]
        groups.setdefault(key, []).append(v["desc"])
    lines = [f"Available voices ({len(voices)} total, engine: auto-detected):"]
    for locale in sorted(groups):
        entries = groups[locale][:5]
        lines.append(f"\n{locale}:")
        for e in entries:
            lines.append(f"  {e}")
        if len(groups[locale]) > 5:
            lines.append(f"  ... and {len(groups[locale]) - 5} more")
    lines.append("\nSet a voice with e.g.: /settings -> Preset -> Voice name")
    lines.append("You can also use: default, male, female")
    # Split into multiple messages if too long
    full = "\n".join(lines)
    if len(full) > 3800:
        for i in range(0, len(full), 3800):
            await message.reply_text(full[i:i + 3800])
    else:
        await message.reply_text(full, disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected help request for unapproved chat/user")
        return
    await message.reply_text(HELP_TEXT)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await help_command(update, context)


async def settings_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected settings request for unapproved chat/user")
        return
    await settings_command(update, context)


async def pool_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected pool request for unapproved chat/user")
        return
    from .pool_ui import pool_command
    await pool_command(update, context)


async def settings_callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        query = update.callback_query
        if query:
            await query.answer("Not authorized", show_alert=True)
        return
    await settings_callback(update, context)


async def pool_callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        query = update.callback_query
        if query:
            await query.answer("Not authorized", show_alert=True)
        return
    from .pool_ui import pool_callback
    await pool_callback(update, context)


async def settings_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        return False
    return await settings_text_handler(update, context)


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /skip through whichever text-input flow is currently active."""
    if await settings_text_entry(update, context):
        return
    if await pool_text_entry(update, context):
        return
    if await editconfig_text(update, context):
        return
    if update.effective_message:
        await update.effective_message.reply_text("There is no active input to skip.")


async def pool_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        return False
    from .pool_ui import pool_text_handler
    return await pool_text_handler(update, context)


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected jobs request for unapproved chat/user")
        return

    user = update.effective_user
    if user is None:
        return

    show_all = context.args and context.args[0].lower() in ("all", "--all", "-a")

    if show_all:
        jobs = await list_all_jobs(settings.db_path, limit=30)
        if not jobs:
            await message.reply_text("No downloads yet.")
            return
        lines = ["📂 All recent downloads:"]
        user_cache: dict[int, str] = {}
        for job in jobs:
            status_label = {"pending": "pending", "downloading": "downloading", "uploaded": "uploaded", "failed": "failed"}.get(
                job.status, "unknown"
            )
            size = f"{job.file_size / 1024 / 1024:.1f} MB" if job.file_size else "unknown size"
            owner = user_cache.get(job.user_id)
            if not owner:
                try:
                    chat_member = await context.bot.get_chat_member(chat_id=message.chat_id, user_id=job.user_id)
                    owner = chat_member.user.full_name if chat_member else f"user#{job.user_id}"
                except Exception:
                    owner = f"user#{job.user_id}"
                user_cache[job.user_id] = owner
            lines.append(f"[{status_label}] #{job.id} by {owner}: {job.url[:50]}... ({size})")
    else:
        jobs = await list_user_jobs(settings.db_path, user.id, limit=10)
        if not jobs:
            await message.reply_text("No downloads yet. Use `/jobs all` to browse community downloads.")
            return
        lines = ["Your recent downloads:"]
        for job in jobs:
            status_label = {"pending": "pending", "downloading": "downloading", "uploaded": "uploaded", "failed": "failed"}.get(
                job.status, "unknown"
            )
            size = f"{job.file_size / 1024 / 1024:.1f} MB" if job.file_size else "unknown size"
            lines.append(f"[{status_label}] Job #{job.id}: {job.url[:60]}... ({size})")
    await message.reply_text("\n".join(lines))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected delete request for unapproved chat/user")
        return

    user = update.effective_user
    if user is None or not context.args or not context.args[0].isdigit():
        await message.reply_text("Usage: /delete <job_id>")
        return

    job_id = int(context.args[0])
    job = await get_job(settings.db_path, job_id)
    if job is None or job.user_id != user.id:
        await message.reply_text("Job not found or not authorized.")
        return

    if job.file_path:
        path = Path(job.file_path)
        if path.is_file():
            path.unlink(missing_ok=True)
    await update_job(settings.db_path, job_id, status="deleted", file_path=None)
    await message.reply_text(f"Job #{job_id} deleted.")


async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected cleanup request")
        return
    user = update.effective_user
    if user is None:
        return
    db_path: Path = context.application.bot_data["db_path"]
    jobs = await list_user_jobs(db_path, user.id, limit=50)
    removed = 0
    for job in jobs:
        if job.status_message_id and job.chat_id:
            try:
                await context.bot.delete_message(chat_id=job.chat_id, message_id=job.status_message_id)
                await update_job(db_path, job.id, status_message_id=None)
                removed += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
    dl_removed = await cleanup_download_messages(db_path, context.bot)
    text = f"Cleaned up {removed} bot status messages."
    if dl_removed:
        text += f" Removed {dl_removed} expired download links."
    await message.reply_text(text)


async def editconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected editconfig request for unapproved chat/user")
        return
    user = update.effective_user
    if user is None:
        return
    db_path: Path = context.application.bot_data["db_path"]
    edit_jobs = await _get_latest_pending_edit(db_path, user.id)
    if edit_jobs is None:
        await message.reply_text("No pending edit job. Use /settings -> Edit Existing Video first.")
        return
    edit_id, source_job_id = edit_jobs
    context.user_data["settings_flow"] = {
        "action": "editconfig",
        "edit_id": edit_id,
        "source_job_id": source_job_id,
    }
    await show_editconfig_menu(update, context, edit_id)


async def editconfig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await handle_editconfig_callback(update, context)
    if isinstance(result, tuple) and result[0] == "render":
        asyncio.create_task(_render_edit_job(update, context, result[1]))
    elif isinstance(result, tuple) and result[0] == "download":
        query = update.callback_query
        if query is not None:
            await _send_secure_link(
                query.message,
                result[1],
                context.application.bot_data["db_path"],
                query.from_user.id,
            )


async def editconfig_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("settings_flow")
    if not flow or not isinstance(flow, dict) or flow.get("action") != "editconfig" or not update.message or not update.message.text:
        return False
    field = flow.get("field_name")
    edit_id = flow.get("edit_id")
    if field is None or edit_id is None:
        return False
    if field in ("voice_menu", "banner_menu"):
        return False
    text = update.message.text.strip()
    if field == "save_preset_name":
        if text.lower() == "/skip":
            flow.pop("field_name", None)
            await show_editconfig_menu(update, context, edit_id)
            return True
        edit = await get_edit_job(context.application.bot_data["db_path"], edit_id)
        if edit is None:
            await update.message.reply_text("Edit job not found.")
            return True
        existing = await list_presets(context.application.bot_data["db_path"], edit.user_id)
        if any(preset.name.casefold() == text.casefold() for preset in existing):
            await update.message.reply_text(
                "That preset name already exists. Choose it from Save Config to Preset to overwrite it."
            )
            return True
        preset = await create_preset(
            context.application.bot_data["db_path"],
            edit.user_id,
            text,
            **_effective_edit_snapshot(
                edit,
                next(
                    (preset for preset in existing if preset.id == edit.preset_id),
                    None,
                ),
            ),
        )
        flow.pop("field_name", None)
        await update.message.reply_text(f"✅ Preset “{preset.name}” created from this edit configuration.")
        await show_editconfig_menu(update, context, edit_id)
        return True
    value = None if text.lower() == "/skip" else text
    if field == "auto_captions":
        if text.lower() in ("yes", "y", "on", "true", "1"):
            value = True
        elif text.lower() in ("no", "n", "off", "false", "0"):
            value = False
        else:
            await update.message.reply_text("Enter yes or no")
            return True
    elif field == "watermark_removal":
        if text.lower() in ("yes", "y", "on", "true", "1"):
            value = True
        elif text.lower() in ("no", "n", "off", "false", "0"):
            value = False
        else:
            await update.message.reply_text("Enter yes or no")
            return True
    elif field == "channel_banner":
        if text.lower() in ("yes", "y", "on", "true", "1"):
            value = True
        elif text.lower() in ("no", "n", "off", "false", "0"):
            value = False
        else:
            await update.message.reply_text("Enter yes or no")
            return True
    if field == "voice_speed":
        try:
            value = float(text)
            if not (0.5 <= value <= 2.0):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 0.5 and 2.0")
            return True

    await update_edit_job(context.application.bot_data["db_path"], edit_id, **{field: value})
    flow.pop("field_name", None)
    await show_editconfig_menu(update, context, edit_id)
    return True


async def _get_latest_pending_edit(db_path: Path, user_id: int) -> tuple[int, int] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, source_job_id FROM edit_jobs WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return (row["id"], row["source_job_id"]) if row else None


def _watermark_review_keyboard(edit, analysis: WatermarkAnalysis) -> InlineKeyboardMarkup:
    selected = set(json.loads(edit.watermark_candidates or "[]"))
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if item.id in selected else '⬜'} Candidate {item.id} ({item.confidence:.0%})",
                callback_data=f"watermark:{edit.id}:toggle:{item.id}",
            )
        ]
        for item in analysis.candidates
    ]
    rows.append([
        InlineKeyboardButton("Apply selected", callback_data=f"watermark:{edit.id}:apply"),
        InlineKeyboardButton("Skip removal", callback_data=f"watermark:{edit.id}:skip"),
    ])
    return InlineKeyboardMarkup(rows)


async def watermark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        return
    try:
        edit_id = int(parts[1])
    except ValueError:
        return
    db_path: Path = context.application.bot_data["db_path"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.user_id != query.from_user.id:
        await query.answer("This review belongs to another user.", show_alert=True)
        return
    if edit.status != "awaiting_watermark_review" or not edit.watermark_analysis:
        await query.answer("This watermark review is no longer active.", show_alert=True)
        return
    await query.answer()
    analysis = WatermarkAnalysis.from_json(edit.watermark_analysis)
    action = parts[2]
    if action == "toggle" and len(parts) == 4:
        try:
            candidate_id = int(parts[3])
        except ValueError:
            return
        valid = {item.id for item in analysis.candidates}
        if candidate_id not in valid:
            return
        selected = set(json.loads(edit.watermark_candidates or "[]"))
        selected.symmetric_difference_update({candidate_id})
        edit = await update_edit_job(
            db_path, edit_id, watermark_candidates=json.dumps(sorted(selected)),
        )
        await query.edit_message_reply_markup(_watermark_review_keyboard(edit, analysis))
        return
    if action == "skip":
        await update_edit_job(
            db_path, edit_id, watermark_candidates="[]", status="pending",
        )
        await query.edit_message_caption("Watermark removal skipped. Resuming render…")
    elif action == "apply":
        await update_edit_job(db_path, edit_id, status="pending")
        await query.edit_message_caption("Selection accepted. Resuming render…")
    else:
        return
    asyncio.create_task(_render_edit_job(update, context, edit_id))


async def _render_edit_job(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.file_path is None:
        await update.effective_message.reply_text("Edit job not found.")
        return
    source = await get_job(db_path, edit.source_job_id)
    if source is None:
        await update.effective_message.reply_text("Source job not found.")
        return

    msg = await update.effective_message.reply_text("🎬 Preparing render...")
    out_path = storage_dir / f"edit-{edit.id}-final.mp4"
    try:
        preset = None
        if edit.preset_id:
            from .storage import list_presets
            preset = next(
                (p for p in await list_presets(db_path, edit.user_id) if p.id == edit.preset_id),
                None,
            )

        cap_text = edit.caption_text if edit.caption_text is not None else (preset.caption_text if preset else None)
        cap_color = edit.caption_color if edit.caption_color is not None else (preset.caption_color or "white" if preset else "white")
        cap_style = edit.caption_style if edit.caption_style is not None else (preset.caption_style or "basic" if preset else "basic")
        cap_pos = edit.caption_position if edit.caption_position is not None else (preset.caption_position or "bottom" if preset else "bottom")
        auto_cap = edit.auto_captions
        v_text = edit.voice_text if edit.voice_text is not None else (preset.voice_text if preset else None)
        v_voice = edit.voice_over_voice if edit.voice_over_voice is not None else (preset.voice_over_voice or "default" if preset else "default")
        v_quality = edit.voice_quality if edit.voice_quality is not None else (preset.voice_quality or "basic" if preset else "basic")
        v_speed = edit.voice_speed if edit.voice_speed is not None else (preset.voice_speed or 1.0 if preset else 1.0)
        tts_eng = edit.tts_engine if edit.tts_engine is not None else (preset.tts_engine if preset else None)
        b_path = edit.banner_path if edit.banner_path is not None else (preset.banner_path if preset else None)
        b_pos = edit.banner_position if edit.banner_position is not None else (preset.banner_position or "bottom" if preset else "bottom")
        b_scale = edit.banner_scale if edit.banner_scale is not None else (preset.banner_scale or "fill" if preset else "fill")
        wm_removal = edit.watermark_removal
        wm_pos = edit.watermark_position if edit.watermark_position is not None else (preset.watermark_position or "auto" if preset else "auto")
        wm_mode = edit.watermark_mode if edit.watermark_mode is not None else (
            preset.watermark_mode if preset and preset.watermark_mode else
            ("remove" if wm_removal else "keep")
        )
        wm_text = edit.watermark_text if edit.watermark_text is not None else (
            preset.watermark_text if preset else None
        )
        if wm_mode == "swap" and not (wm_text and wm_text.strip()):
            raise DownloadError("Set Replacement Watermark text before using Swap mode.")
        wm_removal = wm_mode in ("remove", "swap")
        wm_candidates = None
        if wm_removal and wm_pos == "auto":
            if not edit.watermark_analysis:
                await msg.edit_text("🔎 Analyzing persistent watermark regions…")
                try:
                    analysis = await asyncio.to_thread(analyze_video, Path(edit.file_path))
                except Exception as exc:
                    LOGGER.warning("Persistent watermark analysis unavailable: %s", exc)
                    append_event(
                        "watermark_analysis", "Analysis unavailable; using legacy delogo detection",
                        edit_id=edit_id, user_id=edit.user_id, error=str(exc),
                        fallback_used=True,
                    )
                    analysis = None
                if analysis is None:
                    wm_candidates = None
                else:
                    preview_path = storage_dir / f"edit-{edit.id}-watermarks.jpg"
                    if analysis.candidates:
                        await asyncio.to_thread(
                            create_preview, Path(edit.file_path), analysis, preview_path,
                        )
                    confidence = max((item.confidence for item in analysis.candidates), default=0.0)
                    edit = await update_edit_job(
                        db_path, edit.id, watermark_analysis=analysis.to_json(),
                        watermark_confidence=confidence,
                        watermark_candidates=json.dumps(list(analysis.selected)),
                        watermark_preview_path=str(preview_path) if analysis.candidates else None,
                        status="awaiting_watermark_review" if analysis.requires_review else "pending",
                    )
                    append_event(
                        "watermark_analysis", "Persistent watermark analysis completed",
                        edit_id=edit_id, user_id=edit.user_id, confidence=confidence,
                        candidates=[item.box for item in analysis.candidates],
                        selected=list(analysis.selected),
                        duration_seconds=analysis.duration_seconds,
                    )
                    if analysis.requires_review:
                        await msg.edit_text("Watermark candidates need your review.")
                        with preview_path.open("rb") as preview:
                            await update.effective_message.reply_photo(
                                preview,
                                caption=(
                                    "Select the numbered regions to replace, then tap Apply."
                                    if wm_mode == "swap"
                                    else "Select the numbered regions to remove, then tap Apply."
                                ),
                                reply_markup=_watermark_review_keyboard(edit, analysis),
                            )
                        return
            if edit.watermark_analysis:
                analysis = WatermarkAnalysis.from_json(edit.watermark_analysis)
                selected_ids = set(json.loads(edit.watermark_candidates or "[]"))
                wm_candidates = [
                    asdict(candidate) for candidate in analysis.candidates
                    if candidate.id in selected_ids
                ]
                if not wm_candidates:
                    wm_removal = False
        ch_banner = edit.channel_banner

        steps = []
        if wm_removal:
            steps.append("Watermark swap" if wm_mode == "swap" else "Watermark removal")
        if cap_text or auto_cap:
            steps.append("Captions")
        if v_text:
            steps.append("Voice-over")
        if ch_banner and source.url:
            steps.append("Channel banner")
        if b_path:
            steps.append("Banner overlay")
        if not steps:
            steps.append("Rendering")

        reporter = _ProgressReporter(msg, steps)

        out_path, subtitles_path = await render_edit(
            input_path=Path(edit.file_path),
            output_path=out_path,
            caption_text=cap_text,
            caption_color=cap_color,
            caption_style=cap_style,
            caption_position=cap_pos,
            auto_captions=auto_cap,
            voice_text=v_text,
            voice=v_voice,
            voice_quality=v_quality,
            voice_speed=v_speed,
            tts_engine=tts_eng,
            banner_path=Path(b_path) if b_path else None,
            banner_position=b_pos,
            banner_scale=b_scale,
            watermark_removal=wm_removal,
            watermark_position=wm_pos,
            channel_banner=ch_banner,
            source_url=source.url,
            timeout_seconds=settings.timeout_seconds,
            progress_callback=reporter,
            watermark_candidates=wm_candidates,
            tools_dir=settings.tools_dir,
            watermark_mode=wm_mode,
            watermark_text=wm_text,
        )
    except DownloadError as exc:
        await update_edit_job(db_path, edit.id, status="failed", error_message=str(exc))
        await msg.edit_text(f"❌ Render failed: {exc}")
        return
    await update_edit_job(db_path, edit.id, status="rendered", file_path=str(out_path), file_size=out_path.stat().st_size, subtitles_path=subtitles_path)
    fallback_note = (
        "\n⚠️ LaMa was unavailable; adaptive FFmpeg removal was used."
        if getattr(reporter, "watermark_fallback_used", False) else ""
    )
    try:
        await _send_render_download_link(
            update.effective_message, context, edit, settings, db_path,
        )
        delivery_status = "Direct download ready; uploading a Telegram copy"
    except Exception as exc:
        LOGGER.warning("Could not send direct edit link for job %s: %s", edit.id, exc)
        delivery_status = "Uploading"
    await _safe_status_edit(
        msg, f"✅ Render complete. {delivery_status} for job #{edit.id}…{fallback_note}",
    )
    try:
        await _send_document_with_retry(
            update.effective_message,
            out_path,
            f"✅ Render complete. Job #{edit.id} ready.{fallback_note}",
            min(settings.upload_timeout_seconds, 120),
            _render_pool_keyboard(edit.id, saved=False),
        )
    except Exception as exc:
        await update.effective_message.reply_text(
            f"✅ Render complete. Job #{edit.id} ready.{fallback_note}\n⚠️ Could not send file: {exc}",
            reply_markup=_render_pool_keyboard(edit.id, saved=False),
        )
    if subtitles_path:
        try:
            await _send_document_with_retry(
                update.effective_message,
                Path(subtitles_path),
                "📄 Subtitles available.",
                settings.upload_timeout_seconds,
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"📄 Subtitles available.\n⚠️ Could not send file: {exc}"
            )
    await _safe_status_edit(msg, f"✅ Job #{edit.id} delivery finished.")


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        return
    user = update.effective_user
    if user is None:
        return
    model = context.args[0] if context.args else None
    if len(context.args) > 1:
        await message.reply_text("Usage: /fix [provider/model]")
        return
    if model:
        try:
            model = validate_model(model)
        except ValueError as exc:
            await message.reply_text(f"Invalid model: {exc}\nUsage: /fix [provider/model]")
            return

    model_text = f" using {model}" if model else ""
    await message.reply_text(f"🔍 Scanning for errors to fix{model_text}...")
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    error_files = sorted(ERRORS_DIR.glob("*.json"))
    pending = [ef for ef in error_files if not ef.name.startswith(("fixed_", "failed_", "unfixed_"))]
    if not pending:
        await message.reply_text("No pending errors found.")
        return

    report_lines = []
    for ef in pending[:5]:
        error_info = load_error_log(ef)
        if error_info is None:
            continue
        category = categorize_error(error_info.get("message", ""))
        error_info["category"] = category
        fix_result = await apply_known_fix(error_info, settings.tools_dir)
        if category == "unknown":
            workspace = Path.cwd()
            script_path = await invoke_opencode_fix(error_info, workspace, model=model)
            if script_path:
                code, output = await run_fix_script(script_path)
                append_event(
                    "fix_agent",
                    output[-5000:],
                    error_id=error_info.get("id"),
                    model=model,
                    exit_code=code,
                    user_id=user.id,
                )
                if code == 0:
                    report_lines.append(f"🤖 Fixed with OpenCode{model_text}: {error_info.get('message', '')[:80]}")
                    ef.replace(ERRORS_DIR / f"fixed_{ef.name}")
                else:
                    report_lines.append(f"⚠️ OpenCode failed (exit {code}): {output[-200:]}")
                    ef.replace(ERRORS_DIR / f"failed_{ef.name}")
        elif fix_result is None:
            report_lines.append(f"✅ Fixed [{category}]: {error_info.get('message', '')[:80]}")
            ef.replace(ERRORS_DIR / f"fixed_{ef.name}")
        else:
            report_lines.append(f"⚠️ Fix failed [{category}]: {fix_result[:200]}")
            ef.replace(ERRORS_DIR / f"failed_{ef.name}")

    await message.reply_text("\n".join(report_lines) if report_lines else "No fixable errors.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        return

    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    error_files = list(ERRORS_DIR.glob("*.json"))
    pending = sum(1 for ef in error_files if not ef.name.startswith(("fixed_", "failed_", "unfixed_")))
    fixed = sum(1 for ef in error_files if ef.name.startswith("fixed_"))
    failed = sum(1 for ef in error_files if ef.name.startswith("failed_"))
    unfixed = sum(1 for ef in error_files if ef.name.startswith("unfixed_"))

    lines = [
        "🤖 Bot Status",
        f"Pending errors: {pending}",
        f"Auto-fixed: {fixed}",
        f"Fix failed: {failed}",
        f"Unknown (needs review): {unfixed}",
        "",
        "Commands:",
        "/fix - attempt to auto-fix pending errors",
        "/status - this status report",
    ]
    await message.reply_text("\n".join(lines))


async def audit_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    append_event(
        "update",
        (message.text or message.caption or "")[:500] if message else "",
        update_id=update.update_id,
        user_id=user.id if user else None,
        chat_id=chat.id if chat else None,
        callback_data=update.callback_query.data if update.callback_query else None,
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not _authorized(update, settings):
        return
    issue = " ".join(context.args).strip()
    if not issue:
        await message.reply_text("Usage: /report <what went wrong>")
        return

    events = recent_events(user_id=user.id, limit=100)
    report_id = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{user.id}"
    report = {
        "id": report_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "user_report",
        "user_id": user.id,
        "chat_id": update.effective_chat.id if update.effective_chat else None,
        "issue": issue,
        "recent_events": events,
    }
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = ERRORS_DIR / f"{report_id}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    append_event("user_report", issue, report_id=report_id, user_id=user.id)

    error_info = {
        "id": report_id,
        "message": issue,
        "category": categorize_error(issue),
        "traceback": json.dumps(events, indent=2, default=str),
    }
    script_path = await invoke_opencode_fix(error_info, Path.cwd())
    if not script_path:
        await message.reply_text(f"⚠️ Report {report_id} saved, but the AI handoff could not be created.")
        return

    await message.reply_text(
        f"🤖 Report {report_id} saved with {len(events)} recent events and handed to the AI agent."
    )

    async def _run_report_agent() -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/env", "bash", script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
            output = (stdout + stderr).decode("utf-8", "replace")
            append_event(
                "report_agent",
                output[-5000:],
                report_id=report_id,
                exit_code=process.returncode,
                user_id=user.id,
            )
            result = "completed" if process.returncode == 0 else f"failed (exit {process.returncode})"
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"🤖 AI report {report_id} {result}.",
            )
        except Exception as exc:
            append_event("report_agent_error", str(exc), report_id=report_id, user_id=user.id)
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"⚠️ AI report {report_id} failed: {exc}",
            )

    asyncio.create_task(_run_report_agent())


async def cleanup_task(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]

    expired = await cleanup_expired_tokens(db_path)
    if expired:
        LOGGER.info("Cleaned up %d expired tokens", expired)

    removed = await cleanup_old_jobs(db_path, storage_dir, settings.retention_days)
    if removed:
        LOGGER.info("Cleaned up %d old jobs", removed)

    dl_removed = await cleanup_download_messages(db_path, context.bot)
    if dl_removed:
        LOGGER.info("Cleaned up %d expired download messages", dl_removed)


async def _post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    db_path: Path = application.bot_data["db_path"]
    storage_dir: Path = application.bot_data["storage_dir"]

    app = create_download_app(db_path, storage_dir)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.download_port)
    await site.start()
    application.bot_data["download_runner"] = runner
    LOGGER.info("Download server started on port %d", settings.download_port)

    cleaned = await cleanup_download_messages(db_path, application.bot)
    if cleaned:
        LOGGER.info("Startup: cleaned up %d expired download messages", cleaned)


def main() -> None:
    prefer_ffmpeg_full()
    settings = Settings.from_environment()
    ytdlp = provision_ytdlp(settings.tools_dir, settings.ytdlp_version)
    if shutil.which("ffmpeg") is None:
        LOGGER.warning("ffmpeg not found; some separate audio/video streams cannot be merged")

    storage_dir = settings.storage_dir
    db_path = settings.db_path
    storage_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(init_db(db_path))

    builder = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(4)
        .connect_timeout(20)
        .read_timeout(120)
        .write_timeout(settings.upload_timeout_seconds)
        .pool_timeout(20)
    )
    if settings.local_api_url:
        builder = builder.base_url(settings.local_api_url)
        LOGGER.info("Using local Telegram Bot API at %s", settings.local_api_url)

    application = builder.post_init(_post_init).build()
    application.bot_data["settings"] = settings
    application.bot_data["ytdlp"] = ytdlp
    gallerydl_path = shutil.which("gallery-dl")
    if gallerydl_path is None:
        gallerydl_path = str(Path(sys.executable).with_name("gallery-dl"))
    application.bot_data["gallerydl"] = Path(gallerydl_path)
    application.bot_data["db_path"] = db_path
    application.bot_data["storage_dir"] = storage_dir

    application.add_handler(TypeHandler(Update, audit_update, block=False), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("voices", voices_command))
    application.add_handler(CommandHandler("settings", settings_command_entry))
    application.add_handler(CommandHandler("pool", pool_command_entry))
    application.add_handler(CommandHandler("fix", fix_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^settings:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^preset:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^edit:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^preset_create:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^pool:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^workflow:"))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    application.add_handler(CommandHandler("editconfig", editconfig_command))
    application.add_handler(CallbackQueryHandler(editconfig_callback, pattern=r"^editcfg:"))
    application.add_handler(CallbackQueryHandler(watermark_callback, pattern=r"^watermark:"))
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^download:"))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, _message_router)
    )
    application.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, _message_router))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(cleanup_task, interval=6 * 60 * 60, first=60)

    if settings.download_domain:
        LOGGER.info(
            "Secure downloads enabled at https://%s/download/<token>", settings.download_domain,
        )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
