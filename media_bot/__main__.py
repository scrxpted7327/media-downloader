from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from .auto_hashtags import (
    CodexUnavailable,
    MetadataError,
    MetadataResult,
    generate_metadata,
)
from .access import (
    NOT_FOUND_OR_UNAUTHORIZED,
    ResourceNotFound,
    require_owned_edit,
    require_owned_job,
)
from .config import Settings
from .downloader import (
    DownloadError,
    create_thumbnail,
    download_instagram,
    download_media,
    download_tiktok_account,
    download_tiktok_slideshow,
    persist_download,
    read_source_metadata,
)
from .download_server import create_download_app
from .diagnostics import (
    append_event,
    install_event_logging,
    recent_events,
    redact_sensitive,
    write_redacted_json,
)
from .editor import list_tts_voices, render_edit
from .error_handler import error_handler
from .fix_agent import (
    ERRORS_DIR,
    apply_known_fix,
    categorize_error,
    invoke_opencode_fix,
    load_error_log,
    run_fix_script,
    validate_model,
)
from .platforms import (
    extract_supported_urls,
    is_instagram_url,
    is_tiktok_photo_url,
    is_tiktok_url,
    normalize_tiktok_profile,
)
from .settings_ui import (
    _edit_message,
    _effective_edit_snapshot,
    handle_editconfig_callback,
    presets_command,
    settings_callback,
    settings_command,
    settings_photo_handler,
    settings_text_handler,
    show_editconfig_menu,
)
from .storage import (
    cleanup_download_messages,
    cleanup_edit_artifacts,
    cleanup_expired_tokens,
    cleanup_old_jobs,
    claim_metadata_job,
    create_durable_pool_item,
    create_download_token,
    create_edit_job,
    create_job,
    create_preset,
    delete_durable_pool_item,
    delete_job_with_artifacts,
    foreign_key_violations,
    get_edit_job,
    get_job,
    get_saved_edit_pool_item,
    get_saved_source_pool_item,
    init_db,
    list_metadata_jobs,
    list_resumable_metadata_jobs,
    list_all_jobs,
    list_presets,
    list_user_jobs,
    mark_download_message_deleted,
    open_database,
    reconcile_interrupted_work,
    reset_source_edits,
    stage_edit_source,
    store_download_message,
    queue_metadata_job,
    update_edit_job,
    update_job,
)
from .tools import prefer_ffmpeg_full, provision_ytdlp
from .watermark import WatermarkAnalysis, analyze_video, create_preview
from .work_queue import WorkAlreadyQueued, WorkQueue, WorkRejected

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
install_event_logging()

RESTART_MARKER = Path("runtime/restart-requested")
RESTART_ACK = Path("runtime/restart-shutdown-notified")
FLOW_TTL_SECONDS = 15 * 60
STARTED_MONOTONIC = time.monotonic()

HELP_TEXT = (
    "Commands:\n"
    "/start - show this help message\n"
    "/help - show this message\n"
    "/settings - video presets and customization\n"
    "/presets - list and manage presets\n"
    "/tiktokaccount <username> [50|all] - download a TikTok account archive\n"
    "/pool - manage saved videos and classifications\n"
    "/jobs - list your recent downloads\n"
    "/editconfig - set options for the current edit job\n"
    "/delete <job_id> - delete a downloaded file\n"
    "/cleanup - delete bot status messages from recent jobs\n"
    "/voices - list available TTS voices\n"
    "/queue - show your queued and running work\n"
    "/canceljob <download|render|metadata>:<id> - cancel queued or running work\n"
    "/report <issue> - save a diagnostic ticket for the operator\n"
    "/skip - skip the current optional input\n"
    "/cancel - cancel the current interactive flow\n\n"
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
) -> object:
    """Send a document, honoring Telegram's requested flood-control delay."""
    for attempt in range(2):
        try:
            with path.open("rb") as document:
                sent_message = await message.reply_document(
                    document=document,
                    caption=caption,
                    reply_markup=reply_markup,
                    read_timeout=timeout,
                    write_timeout=timeout,
                    connect_timeout=timeout,
                    pool_timeout=timeout,
                )
            return sent_message
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


def _admin_authorized(update: Update, settings: Settings) -> bool:
    """Admin rights are always user-bound; an allowed group is not enough."""
    user = update.effective_user
    return bool(user and user.id in settings.admin_user_ids)


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
    job = await require_owned_job(db_path, job_id, user_id)
    keyboard = await _download_actions_keyboard(job_id, db_path, user_id)
    details = []
    if job.title:
        details.append(job.title[:200])
    if job.source_caption:
        caption = " ".join(job.source_caption.split())
        details.append(caption[:300] + ("…" if len(caption) > 300 else ""))
    suffix = "\n\n" + "\n".join(details) if details else ""
    text = f"✅ Download complete. Choose an action:{suffix}"
    thumbnail = Path(job.thumbnail_path) if job.thumbnail_path else None
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
    *,
    return_message: bool = False,
) -> str | object:
    """Send a usable edit link before attempting the slower Telegram upload."""
    if not settings.download_public_origin:
        raise RuntimeError("Direct downloads are not configured.")
    owned_edit = await require_owned_edit(db_path, edit.id, edit.user_id)
    await require_owned_job(db_path, owned_edit.source_job_id, owned_edit.user_id)
    token = await create_download_token(
        db_path,
        owned_edit.source_job_id,
        owned_edit.user_id,
        settings.token_expiry_minutes,
        edit_job_id=owned_edit.id,
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
    return link_message if return_message else url


def _build_download_url(settings: Settings, token: str) -> str:
    origin = settings.download_public_origin
    if not origin:
        raise RuntimeError("Direct downloads are not configured.")
    return f"{origin}/download/{token}"


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

    user = update.effective_user
    if user is None:
        return
    locks = context.application.bot_data.setdefault("input_locks", {})
    lock = locks.setdefault(user.id, asyncio.Lock())
    async with lock:
        if _flow_is_current(update, context) and await _handle_active_input(update, context):
            return
        await handle_url(update, context)


def _bind_flow_context(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, owner: str,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if owner == "settings":
        context.user_data.pop("pool_flow", None)
    elif owner == "pool":
        context.user_data.pop("settings_flow", None)
    else:
        raise ValueError(f"unknown flow owner: {owner}")
    context.user_data["_flow_chat_id"] = chat.id
    context.user_data["_flow_expires_at"] = time.monotonic() + FLOW_TTL_SECONDS


def _clear_flow_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("settings_flow", None)
    context.user_data.pop("pool_flow", None)
    context.user_data.pop("_flow_chat_id", None)
    context.user_data.pop("_flow_expires_at", None)


def _flow_is_current(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    has_flow = "settings_flow" in context.user_data or "pool_flow" in context.user_data
    if not has_flow:
        return False
    expires_at = context.user_data.get("_flow_expires_at", 0)
    if not isinstance(expires_at, (int, float)) or expires_at <= time.monotonic():
        _clear_flow_context(context)
        return False
    chat = update.effective_chat
    if chat is None or context.user_data.get("_flow_chat_id") != chat.id:
        return False
    context.user_data["_flow_expires_at"] = time.monotonic() + FLOW_TTL_SECONDS
    return True


async def _handle_active_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Give an active text/image prompt exclusive ownership of the next message."""
    settings_flow = context.user_data.get("settings_flow")
    action = (
        settings_flow.get("action") if isinstance(settings_flow, dict)
        else getattr(settings_flow, "action", None)
    )
    field_name = settings_flow.get("field_name") if isinstance(settings_flow, dict) else None
    settings_text_actions = {
        "preset_create_name", "preset_import_code", "preset_create_voice", "preset_create_voice_text",
        "preset_create_voice_speed", "preset_create_banner", "preset_edit_field",
        "set_profile_banner",
    }
    editconfig_input = action == "editconfig" and field_name not in (None, "voice_menu", "banner_menu")
    settings_input = action in settings_text_actions

    pool_flow = context.user_data.get("pool_flow")
    pool_action = getattr(pool_flow, "action", None)
    pool_input = pool_action in {
        "pool_add_name", "workflow_create_name", "workflow_create_trigger",
        "workflow_create_action",
    }
    if not (editconfig_input or settings_input or pool_input):
        return False

    # Images must be offered to the upload parser before text parsers inspect
    # their caption (or the downloader inspects it as a URL).
    if await settings_photo_entry(update, context):
        return True
    if editconfig_input and await editconfig_text(update, context):
        return True
    if settings_input and await settings_text_entry(update, context):
        return True
    if pool_input and await pool_text_entry(update, context):
        return True

    message = update.effective_message
    if message is not None:
        expected = "an image or text value" if field_name == "banner_path" or action in {
            "preset_create_banner", "set_profile_banner",
        } else "a text value"
        await message.reply_text(f"That input could not be read. Please send {expected}, or use Back/Skip.")
    return True


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
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            await message.reply_text(
                "Send one supported YouTube, Instagram, TikTok, or Facebook URL."
            )
        return

    work: WorkQueue = context.application.bot_data["download_work"]
    results = []
    for idx, url in enumerate(urls[:8]):
        if idx > 0:
            await asyncio.sleep(0.2)
        status = await message.reply_text("⏳ Download queued…")
        job = await create_job(db_path, url, user_id, chat_id)
        await update_job(
            db_path, job.id, status="queued", status_message_id=status.message_id,
        )
        try:
            work.submit(
                user_id=user_id,
                label=f"download:{job.id}",
                factory=lambda job=job, status=status, url=url: _process_single_url(
                    update, context, job, status, url, user_id, chat_id,
                    settings, ytdlp, gallerydl, db_path, storage_dir,
                ),
            )
            results.append(f"#{job.id}: QUEUED")
        except WorkRejected as exc:
            await update_job(db_path, job.id, status="failed", error_message=str(exc))
            await status.edit_text(f"❌ Download not queued: {exc}")
            results.append(f"#{job.id}: REJECTED")

    if len(urls) > 1:
        lines = [f"Queued {len(results)}/{len(urls)} URLs:"]
        for r in results:
            lines.append(f"  {r}")
        if len(urls) > 8:
            lines.append("Only the first 8 URLs were accepted; send the remainder separately.")
        await message.reply_text("\n".join(lines))


async def _process_single_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    job, status, url: str, user_id: int, chat_id: int,
    settings: Settings, ytdlp: Path, gallerydl: Path,
    db_path: Path, storage_dir: Path,
) -> str:
    await update_job(db_path, job.id, status="downloading")
    await status.edit_text("🔍 Searching…")
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
                ytdlp=ytdlp,
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
        try:
            edit = await require_owned_edit(db_path, job_id, current_user_id)
        except ResourceNotFound:
            await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
            return
        if not edit.file_path:
            await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
            return
        saved = await get_saved_edit_pool_item(db_path, current_user_id, edit.id)
        if action == "editsave":
            if saved is None:
                saved = await create_durable_pool_item(
                    db_path,
                    storage_dir,
                    current_user_id,
                    Path(edit.file_path),
                    source_job_id=edit.source_job_id,
                    edit_job_id=edit.id,
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
                await delete_durable_pool_item(
                    db_path, storage_dir, saved.id, current_user_id,
                )
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

    try:
        job = await require_owned_job(db_path, job_id, current_user_id)
    except ResourceNotFound:
        await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
        return
    if job.file_path is None:
        await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
        return

    if action == "poolsave":
        saved = await get_saved_source_pool_item(db_path, current_user_id, job.id)
        if saved is None:
            await create_durable_pool_item(
                db_path,
                storage_dir,
                current_user_id,
                Path(job.file_path),
                source_job_id=job.id,
                thumbnail_file=Path(job.thumbnail_path) if job.thumbnail_path else None,
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
            await delete_durable_pool_item(
                db_path, storage_dir, saved.id, current_user_id,
            )
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
        await _edit_message(
            query,
            "📦 Choose a preset to render with:",
            InlineKeyboardMarkup(rows),
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
        if not settings.download_public_origin:
            await query.answer("Direct downloads are not configured.", show_alert=True)
            return
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
        _clear_flow_context(context)
        context.user_data["settings_flow"] = {
            "action": "editconfig",
            "edit_id": edit.id,
            "source_job_id": job_id,
        }
        _bind_flow_context(update, context, owner="settings")
        await show_editconfig_menu(
            update,
            context,
            edit.id,
            intro=f"🎬 Edit job #{edit.id} created.\nCurrent settings are shown on each button.",
        )
        return

    if action == "reset":
        await query.answer()
        result = await reset_source_edits(
            db_path, storage_dir, job_id, current_user_id,
        )
        flow = context.user_data.get("settings_flow")
        flow_action = flow.get("action") if isinstance(flow, dict) else getattr(flow, "action", None)
        if flow_action == "editconfig":
            context.user_data.pop("settings_flow", None)
        await _edit_message(
            query,
            f"Edit config reset for this download ({result.records_deleted} removed).",
        )
        return

    if action == "preset":
        await query.answer()
        preset_id = int(parts[3])
        from .storage import list_presets
        preset = next((p for p in await list_presets(db_path, current_user_id) if p.id == preset_id), None)
        if preset is None:
            await _edit_message(query, "Preset not found.")
            return
        source_path = Path(job.file_path)
        if not source_path.is_file():
            await _edit_message(query, "Source file missing.")
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
        await _edit_message(query, f"🎬 Rendering with \"{preset.name}\"…")
        await _enqueue_render(update, context, edit.id)
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


async def tiktok_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected TikTok account request for unapproved chat/user")
        return
    args = context.args or []
    if not args:
        await message.reply_text(
            "Usage: /tiktokaccount username [50|all]\n"
            "You can also send @username or the full TikTok profile URL.\n"
            "The default is the newest 50 posts; 'all' downloads the entire public account."
        )
        return
    profile_url = normalize_tiktok_profile(args[0])
    if profile_url is None:
        await message.reply_text(
            "Send a TikTok username, @username, or profile URL like https://www.tiktok.com/@username"
        )
        return
    limit: int | None = 50
    if len(args) > 1:
        if args[1].lower() == "all":
            if not settings.allow_mass_download_all or not _admin_authorized(update, settings):
                await message.reply_text(
                    "Whole-account downloads are restricted to operators and currently enabled admins. "
                    "Choose a limit from 1 to 500."
                )
                return
            limit = None
        else:
            try:
                limit = int(args[1])
            except ValueError:
                limit = 0
            if not (1 <= limit <= 500):
                await message.reply_text("The post limit must be 1-500, or 'all'.")
                return
    user = update.effective_user
    if user is None:
        return
    status = await message.reply_text("📥 TikTok account download queued…")
    job = await create_job(settings.db_path, profile_url, user.id, message.chat_id)
    await update_job(
        settings.db_path, job.id, status="queued", status_message_id=status.message_id,
    )
    work: WorkQueue = context.application.bot_data["download_work"]
    try:
        work.submit(
            user_id=user.id,
            label=f"tiktok-account:{job.id}",
            factory=lambda: _run_tiktok_account_job(
                context, settings, status, job, profile_url, user.id, limit,
            ),
        )
    except WorkRejected as exc:
        await update_job(
            settings.db_path, job.id, status="failed", error_message=str(exc),
        )
        await status.edit_text(f"❌ TikTok account download not queued: {exc}")


async def _run_tiktok_account_job(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    status,
    job,
    profile_url: str,
    user_id: int,
    limit: int | None,
) -> None:
    temporary = None
    try:
        await update_job(settings.db_path, job.id, status="downloading")
        await status.edit_text(
            f"📥 Downloading {'the entire account' if limit is None else f'up to {limit} posts'}\n"
            "This can take several minutes."
        )
        timeout = max(settings.timeout_seconds, 6 * 60 * 60 if limit is None else (limit or 1) * 30)
        temporary, archive, media_count = await download_tiktok_account(
            context.application.bot_data["gallerydl"], profile_url,
            settings.mass_download_max_mb, timeout, limit,
        )
        persisted = await persist_download(archive, job.id, settings.storage_dir)
        await update_job(
            settings.db_path, job.id, status="uploaded", file_path=str(persisted),
            file_size=persisted.stat().st_size,
            title=f"TikTok account archive ({media_count} media files)",
        )
        if not settings.download_public_origin:
            await status.edit_text(
                f"✅ TikTok account archive ready: {media_count} media files\n"
                "Direct download delivery is not configured; ask the operator to "
                "configure MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN."
            )
            return
        token = await create_download_token(
            settings.db_path, job.id, user_id, settings.token_expiry_minutes,
        )
        url = _build_download_url(settings, token)
        await status.edit_text(
            f"✅ TikTok account archive ready: {media_count} media files\n"
            f"One-time download ({settings.token_expiry_minutes} min):\n{url}",
            disable_web_page_preview=True,
        )
        record_id = await store_download_message(
            settings.db_path,
            status.chat_id,
            status.message_id,
            settings.token_expiry_minutes,
        )
        context.job_queue.run_once(
            _delete_expired_link,
            settings.token_expiry_minutes * 60,
            data={
                "chat_id": status.chat_id,
                "message_id": status.message_id,
                "db_path": str(settings.db_path),
                "msg_record_id": record_id,
            },
        )
    except Exception as exc:
        LOGGER.warning("TikTok account download failed for %s: %s", profile_url, exc)
        await update_job(settings.db_path, job.id, status="failed", error_message=str(exc))
        await status.edit_text(f"❌ TikTok account download failed: {exc}")
    finally:
        if temporary is not None:
            temporary.cleanup()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected help request for unapproved chat/user")
        return
    help_text = HELP_TEXT
    if _admin_authorized(update, settings):
        repair_state = "enabled" if settings.repair_enabled else "disabled"
        help_text += (
            "\n\nOperator commands:\n"
            "/status - service health and repair status\n"
            "/fix [provider/model] - run approved repairs "
            f"(currently {repair_state})"
        )
    await message.reply_text(help_text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await help_command(update, context)


async def settings_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected settings request for unapproved chat/user")
        return
    _clear_flow_context(context)
    await settings_command(update, context)
    _bind_flow_context(update, context, owner="settings")


async def presets_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected presets request for unapproved chat/user")
        return
    _clear_flow_context(context)
    await presets_command(update, context)
    _bind_flow_context(update, context, owner="settings")


async def pool_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected pool request for unapproved chat/user")
        return
    _clear_flow_context(context)
    from .pool_ui import pool_command
    await pool_command(update, context)
    _bind_flow_context(update, context, owner="pool")


async def settings_callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        query = update.callback_query
        if query:
            await query.answer("Not authorized", show_alert=True)
        return
    result = await settings_callback(update, context)
    if result is not None and result[0] == "render":
        await _enqueue_render(update, context, result[1])
    if "settings_flow" in context.user_data:
        _bind_flow_context(update, context, owner="settings")


async def pool_callback_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        query = update.callback_query
        if query:
            await query.answer("Not authorized", show_alert=True)
        return
    from .pool_ui import pool_callback
    await pool_callback(update, context)
    if "pool_flow" in context.user_data:
        _bind_flow_context(update, context, owner="pool")


async def settings_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        return False
    return await settings_text_handler(update, context)


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route /skip through whichever text-input flow is currently active."""
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        return
    if not _flow_is_current(update, context):
        await message.reply_text("There is no active input to skip in this chat.")
        return
    settings_flow = context.user_data.get("settings_flow")
    action = (
        settings_flow.get("action") if isinstance(settings_flow, dict)
        else getattr(settings_flow, "action", None)
    )
    if action == "editconfig":
        handled = await editconfig_text(update, context)
    elif settings_flow is not None:
        handled = await settings_text_entry(update, context)
    else:
        handled = await pool_text_entry(update, context)
    if not handled:
        await message.reply_text("The current input cannot be skipped.")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        return
    if not _flow_is_current(update, context):
        await message.reply_text("There is no active input to cancel in this chat.")
        return
    _clear_flow_context(context)
    await message.reply_text("Cancelled the active input.")


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
        if not _admin_authorized(update, settings):
            await message.reply_text("Browsing all users' downloads is admin-only.")
            return
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
            await message.reply_text("No downloads yet. Send a supported media URL to start one.")
            return
        lines = ["Your recent downloads:"]
        for job in jobs:
            status_label = {"pending": "pending", "downloading": "downloading", "uploaded": "uploaded", "failed": "failed"}.get(
                job.status, "unknown"
            )
            size = f"{job.file_size / 1024 / 1024:.1f} MB" if job.file_size else "unknown size"
            lines.append(f"[{status_label}] Job #{job.id}: {job.url[:60]}... ({size})")
    await message.reply_text("\n".join(lines))


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not _authorized(update, settings):
        return
    download_work: WorkQueue = context.application.bot_data["download_work"]
    render_work: WorkQueue = context.application.bot_data["render_work"]
    metadata_work: WorkQueue | None = context.application.bot_data.get("metadata_work")
    lines = [
        "⏳ Your work queue",
        f"Downloads queued/running: {download_work.pending_for_user(user.id)}",
        f"Renders queued/running: {render_work.pending_for_user(user.id)}",
    ]
    if metadata_work is not None:
        lines.append(f"Metadata queued/running: {metadata_work.pending_for_user(user.id)}")
    items = (
        download_work.items_for_user(user.id)
        + render_work.items_for_user(user.id)
        + (metadata_work.items_for_user(user.id) if metadata_work is not None else ())
    )
    if items:
        lines.extend(["", *(f"• {label} — {state}" for label, state in items)])
    else:
        lines.extend(["", "No queued or running work."])
    if _admin_authorized(update, settings):
        lines.extend([
            "",
            f"Global downloads: active={download_work.active}, queued={download_work.queued}",
            f"Global renders: active={render_work.active}, queued={render_work.queued}",
        ])
        if metadata_work is not None:
            lines.insert(
                -1,
                f"Global metadata: active={metadata_work.active}, queued={metadata_work.queued}",
            )
    lines.append(
        "Use /canceljob download:<id>, /canceljob render:<id>, or "
        "/canceljob metadata:<edit_id> to cancel work."
    )
    await message.reply_text("\n".join(lines))


async def cancel_job_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not _authorized(update, settings):
        return
    value = context.args[0].strip().lower() if len(context.args or []) == 1 else ""
    if ":" not in value:
        await message.reply_text("Usage: /canceljob <download|render|metadata>:<id>")
        return
    kind, raw_id = value.split(":", 1)
    if kind not in {"download", "render", "metadata"} or not raw_id.isdigit():
        await message.reply_text("Usage: /canceljob <download|render|metadata>:<id>")
        return
    item_id = int(raw_id)
    db_path: Path = context.application.bot_data["db_path"]

    if kind in {"render", "metadata"}:
        try:
            edit = await require_owned_edit(db_path, item_id, user.id)
        except ResourceNotFound:
            await message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
            return
        work: WorkQueue = context.application.bot_data[
            "render_work" if kind == "render" else "metadata_work"
        ]
        label = f"{kind}:{item_id}"
        was_active = work.is_active(label)
        cancelled = work.cancel(user_id=user.id, label=label)
        if cancelled:
            if kind == "render":
                await update_edit_job(
                    db_path, edit.id, status="failed", error_message="cancelled by user",
                )
            else:
                await update_edit_job(
                    db_path,
                    edit.id,
                    metadata_status="cancelled",
                    metadata_error="cancelled by user",
                    metadata_completed_at=datetime.now(timezone.utc).isoformat(),
                )
            if kind == "render" and not was_active:
                await cleanup_edit_artifacts(
                    db_path,
                    context.application.bot_data["storage_dir"],
                    edit.id,
                    user_id=user.id,
                    preserve_output=False,
                )
    else:
        try:
            job = await require_owned_job(db_path, item_id, user.id)
        except ResourceNotFound:
            await message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
            return
        work = context.application.bot_data["download_work"]
        cancelled = work.cancel(user_id=user.id, label=f"download:{item_id}")
        if not cancelled:
            cancelled = work.cancel(user_id=user.id, label=f"tiktok-account:{item_id}")
        if cancelled:
            await update_job(
                db_path, job.id, status="failed", error_message="cancelled by user",
            )

    if cancelled:
        await message.reply_text(f"Cancellation requested for {kind} #{item_id}.")
    else:
        await message.reply_text(f"{kind.title()} #{item_id} is not queued or running.")


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
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    job = await get_job(db_path, job_id)
    if job is None or job.user_id != user.id:
        await message.reply_text("Job not found or not authorized.")
        return

    result = await delete_job_with_artifacts(
        db_path, storage_dir, job_id, user_id=user.id,
    )
    detail = f" Removed {result.files_deleted} file(s)."
    if result.files_preserved:
        detail += " Saved Pool copies were preserved."
    if result.unsafe_paths:
        detail += " One or more outside-storage paths were left untouched for safety."
    await message.reply_text(f"Job #{job_id} deleted.{detail}")


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
    _clear_flow_context(context)
    context.user_data["settings_flow"] = {
        "action": "editconfig",
        "edit_id": edit_id,
        "source_job_id": source_job_id,
    }
    _bind_flow_context(update, context, owner="settings")
    await show_editconfig_menu(update, context, edit_id)


async def editconfig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    query = update.callback_query
    if query is None:
        return
    if not _authorized(update, settings):
        await query.answer("Not authorized", show_alert=True)
        return
    result = await handle_editconfig_callback(update, context)
    if "settings_flow" in context.user_data:
        _bind_flow_context(update, context, owner="settings")
    if isinstance(result, tuple) and result[0] == "render":
        await _enqueue_render(update, context, result[1])
    elif isinstance(result, tuple) and result[0] == "download":
        try:
            await require_owned_job(
                context.application.bot_data["db_path"],
                result[1],
                query.from_user.id,
            )
        except ResourceNotFound:
            await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
            return
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
    user = update.effective_user
    if user is None:
        return False
    db_path: Path = context.application.bot_data["db_path"]
    try:
        edit = await require_owned_edit(db_path, edit_id, user.id)
    except ResourceNotFound:
        flow.clear()
        await update.message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
        return True
    text = update.message.text.strip()
    if field == "save_preset_name":
        if text.lower() == "/skip":
            flow.pop("field_name", None)
            await show_editconfig_menu(update, context, edit_id)
            return True
        existing = await list_presets(db_path, edit.user_id)
        if any(preset.name.casefold() == text.casefold() for preset in existing):
            await update.message.reply_text(
                "That preset name already exists. Choose it from Save Config to Preset to overwrite it."
            )
            return True
        preset = await create_preset(
            db_path,
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
    if field == "banner_path" and value is not None:
        await update.message.reply_text(
            "Send the banner as a Telegram photo, or use /skip to clear it."
        )
        return True
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

    await update_edit_job(db_path, edit_id, **{field: value})
    flow.pop("field_name", None)
    await show_editconfig_menu(update, context, edit_id)
    return True


async def _get_latest_pending_edit(db_path: Path, user_id: int) -> tuple[int, int] | None:
    async with open_database(db_path) as db:
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
    try:
        edit = await require_owned_edit(db_path, edit_id, query.from_user.id)
    except ResourceNotFound:
        await query.answer(NOT_FOUND_OR_UNAUTHORIZED, show_alert=True)
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
    await _enqueue_render(update, context, edit_id)


async def _enqueue_render(
    update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int,
) -> bool:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return False
    db_path: Path = context.application.bot_data["db_path"]
    try:
        edit = await require_owned_edit(db_path, edit_id, user.id)
    except ResourceNotFound:
        await message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
        return False
    work: WorkQueue = context.application.bot_data["render_work"]
    label = f"render:{edit_id}"
    if edit.status in {"queued", "rendering", "awaiting_watermark_review"}:
        await message.reply_text(f"Render #{edit_id} is already {edit.status.replace('_', ' ')}.")
        return False
    if edit.status == "rendered":
        await message.reply_text(f"Render #{edit_id} is already complete.")
        return False
    try:
        work.submit(
            user_id=user.id,
            label=label,
            factory=lambda: _render_edit_job(update, context, edit_id),
        )
    except WorkAlreadyQueued:
        await message.reply_text(f"Render #{edit_id} is already queued or running.")
        return False
    except WorkRejected as exc:
        await update_edit_job(
            db_path, edit_id, status="failed", error_message=str(exc),
        )
        await message.reply_text(f"❌ Render not queued: {exc}")
        return False
    await update_edit_job(db_path, edit_id, status="queued", error_message=None)
    return True


def _telegram_message_id(message: object | None) -> int | None:
    value = getattr(message, "message_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _metadata_settings(settings: Settings) -> tuple[str, str, str, int, Path | None]:
    model = str(
        getattr(settings, "metadata_model", "")
        or getattr(settings, "auto_hashtags_model", "")
        or getattr(settings, "codex_model", "")
        or "gpt-5.6-luna"
    )
    reasoning = str(
        getattr(settings, "metadata_reasoning_effort", "")
        or getattr(settings, "auto_hashtags_reasoning_effort", "")
        or getattr(settings, "codex_reasoning_effort", "")
        or "max"
    )
    executable = str(
        getattr(settings, "metadata_codex_executable", "")
        or getattr(settings, "auto_hashtags_codex_executable", "")
        or getattr(settings, "codex_executable", "")
        or "codex"
    )
    timeout = int(
        getattr(settings, "metadata_timeout_seconds", 0)
        or getattr(settings, "auto_hashtags_timeout_seconds", 0)
        or 1800
    )
    codex_home = (
        getattr(settings, "metadata_codex_home", None)
        or getattr(settings, "auto_hashtags_codex_home", None)
    )
    return model, reasoning, executable, timeout, Path(codex_home) if codex_home else None


async def _metadata_progress(
    context: ContextTypes.DEFAULT_TYPE,
    edit_id: int,
    stage: str,
    percent: int,
) -> None:
    """Best-effort persisted Telegram progress for a metadata worker."""
    db_path: Path = context.application.bot_data["db_path"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None:
        return
    source = await get_job(db_path, edit.source_job_id)
    if source is None:
        return
    text = f"📝 {stage}: {max(0, min(100, int(percent)))}%\nJob #{edit_id}"
    message_id = edit.metadata_progress_message_id
    try:
        if message_id is None:
            progress_message = await context.bot.send_message(
                chat_id=source.chat_id,
                text=text,
                reply_to_message_id=edit.render_delivery_message_id,
            )
            message_id = _telegram_message_id(progress_message)
            if message_id is not None:
                await update_edit_job(
                    db_path,
                    edit_id,
                    metadata_progress_message_id=message_id,
                )
        else:
            await context.bot.edit_message_text(
                chat_id=source.chat_id,
                message_id=message_id,
                text=text,
            )
    except Exception:
        LOGGER.debug("Could not update metadata progress for edit %s", edit_id, exc_info=True)


async def _metadata_job(context: ContextTypes.DEFAULT_TYPE, edit_id: int) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    work: WorkQueue | None = context.application.bot_data.get("metadata_work")
    label = f"metadata:{edit_id}"
    edit = await claim_metadata_job(db_path, edit_id)
    if edit is None:
        return
    source = await get_job(db_path, edit.source_job_id)
    if source is None or not edit.file_path:
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="failed",
            metadata_error="rendered video or source job is missing",
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return

    async def progress(stage: str, percent: int) -> None:
        await _metadata_progress(context, edit_id, stage, percent)

    model, reasoning, executable, timeout, codex_home = _metadata_settings(settings)
    try:
        result: MetadataResult = await generate_metadata(
            Path(edit.file_path),
            model=model,
            reasoning_effort=reasoning,
            codex_executable=executable,
            timeout_seconds=timeout,
            codex_home=codex_home,
            progress_callback=progress,
        )
        await update_edit_job(
            db_path,
            edit_id,
            metadata_description=result.description,
            metadata_hashtags=json.dumps(list(result.hashtags), ensure_ascii=False),
        )
        current = await get_edit_job(db_path, edit_id)
        if (
            current is None
            or current.metadata_status != "running"
            or (work is not None and work.cancellation_requested(label))
        ):
            raise asyncio.CancelledError
        await progress("metadata delivery", 0)
        if work is not None and work.cancellation_requested(label):
            raise asyncio.CancelledError
        hashtags = " ".join(result.hashtags)
        reply_kwargs: dict[str, object] = {
            "chat_id": source.chat_id,
            "text": f"📝 Description and hashtags for job #{edit_id}\n\n"
            f"{result.description}\n\n{hashtags}",
        }
        if edit.render_delivery_message_id is not None:
            reply_kwargs["reply_to_message_id"] = edit.render_delivery_message_id
        reply_message = await context.bot.send_message(**reply_kwargs)
        reply_message_id = _telegram_message_id(reply_message)
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="generated",
            metadata_error=None,
            metadata_reply_message_id=reply_message_id,
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        await progress("metadata delivery", 100)
    except asyncio.CancelledError:
        # WorkQueue.stop() cancels active tasks during a restart. Leave those
        # durable rows running so startup recovery can resume them; only an
        # explicit /canceljob request marks the metadata permanently cancelled.
        if work is not None and work.cancellation_requested(label):
            await update_edit_job(
                db_path,
                edit_id,
                metadata_status="cancelled",
                metadata_error="cancelled by user",
                metadata_completed_at=datetime.now(timezone.utc).isoformat(),
            )
        raise
    except CodexUnavailable as exc:
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="skipped",
            metadata_error=str(exc)[:500],
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        await _metadata_progress(context, edit_id, "metadata skipped", 100)
    except MetadataError as exc:
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="failed",
            metadata_error=str(exc)[:500],
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        await _metadata_progress(context, edit_id, "metadata failed", 100)
    except Exception as exc:
        LOGGER.exception("Unexpected metadata failure for edit %s", edit_id)
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="failed",
            metadata_error=str(exc)[:500],
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        await _metadata_progress(context, edit_id, "metadata failed", 100)


async def _enqueue_metadata(
    context: ContextTypes.DEFAULT_TYPE,
    edit_id: int,
    delivery_message: object | None,
) -> bool:
    """Create durable metadata work after a successful video delivery."""
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.status != "rendered" or not edit.file_path:
        return False
    source = await get_job(db_path, edit.source_job_id)
    if source is None:
        return False
    delivery_message_id = _telegram_message_id(delivery_message)
    progress_message = None
    try:
        progress_kwargs: dict[str, object] = {
            "chat_id": source.chat_id,
            "text": f"📝 Generating description and hashtags for job #{edit_id}…",
        }
        if delivery_message_id is not None:
            progress_kwargs["reply_to_message_id"] = delivery_message_id
        progress_message = await context.bot.send_message(**progress_kwargs)
    except Exception:
        LOGGER.warning("Could not create metadata progress message for edit %s", edit_id)

    model, reasoning, _, _, _ = _metadata_settings(settings)
    queued = await queue_metadata_job(
        db_path,
        edit_id,
        model=model,
        reasoning_effort=reasoning,
        progress_message_id=_telegram_message_id(progress_message),
        render_delivery_message_id=delivery_message_id,
    )
    if queued is None:
        return False
    work: WorkQueue | None = context.application.bot_data.get("metadata_work")
    if work is None:
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="skipped",
            metadata_error="metadata queue is unavailable",
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return False
    try:
        work.submit(
            user_id=edit.user_id,
            label=f"metadata:{edit_id}",
            factory=lambda: _metadata_job(context, edit_id),
        )
    except WorkAlreadyQueued:
        return True
    except WorkRejected as exc:
        await update_edit_job(
            db_path,
            edit_id,
            metadata_status="failed",
            metadata_error=str(exc)[:500],
            metadata_completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return False
    return True


async def _render_edit_job(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_id: int) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    storage_dir: Path = context.application.bot_data["storage_dir"]
    user = update.effective_user
    if user is None:
        return
    try:
        edit = await require_owned_edit(db_path, edit_id, user.id)
        source = await require_owned_job(db_path, edit.source_job_id, user.id)
    except ResourceNotFound:
        await update.effective_message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
        return
    if edit.file_path is None:
        await update.effective_message.reply_text(NOT_FOUND_OR_UNAUTHORIZED)
        return

    msg = None
    out_path = storage_dir / f"edit-{edit.id}-final.mp4"

    async def fail_render(reason: str, user_text: str) -> None:
        try:
            await update_edit_job(
                db_path, edit.id, status="failed", error_message=reason,
            )
        except Exception:
            LOGGER.exception("Could not persist failure for render %s", edit.id)
        try:
            await cleanup_edit_artifacts(
                db_path,
                storage_dir,
                edit.id,
                user_id=edit.user_id,
                preserve_output=False,
            )
        except Exception:
            LOGGER.exception("Could not clean failed render %s", edit.id)
        if msg is not None:
            await _safe_status_edit(msg, user_text)
        else:
            try:
                await update.effective_message.reply_text(user_text)
            except Exception:
                LOGGER.exception("Could not report failure for render %s", edit.id)

    try:
        await update_edit_job(db_path, edit.id, status="rendering", error_message=None)
        msg = await update.effective_message.reply_text("🎬 Preparing render...")
        preset = None
        if edit.preset_id:
            from .storage import list_presets
            preset = next(
                (p for p in await list_presets(db_path, edit.user_id) if p.id == edit.preset_id),
                None,
            )

        # Manual caption text is intentionally ignored; captions come only from
        # automatic transcription when Auto Captions is enabled.
        cap_text = None
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
                if analysis.candidates and not wm_candidates:
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
        if out_path.stat().st_size > settings.max_filesize_mb * 1024 * 1024:
            out_path.unlink(missing_ok=True)
            raise DownloadError(
                f"rendered output exceeds the configured "
                f"{settings.max_filesize_mb} MB size limit"
            )
    except asyncio.CancelledError:
        await fail_render("Render cancelled by user.", "⏹ Render cancelled.")
        raise
    except DownloadError as exc:
        await fail_render(str(exc), f"❌ Render failed: {exc}")
        return
    except Exception as exc:
        LOGGER.exception("Unexpected render failure for edit %s", edit.id)
        await fail_render(str(exc), "❌ Render failed unexpectedly. The error was recorded.")
        return
    await update_edit_job(db_path, edit.id, status="rendered", file_path=str(out_path), file_size=out_path.stat().st_size, subtitles_path=subtitles_path)
    try:
        await cleanup_edit_artifacts(
            db_path,
            storage_dir,
            edit.id,
            user_id=edit.user_id,
            preserve_output=True,
        )
    except Exception:
        LOGGER.exception("Could not clean intermediate artifacts for render %s", edit.id)
    fallback_note = (
        "\n⚠️ LaMa was unavailable; adaptive FFmpeg removal was used."
        if getattr(reporter, "watermark_fallback_used", False) else ""
    )
    await _safe_status_edit(msg, f"✅ Render complete. Uploading job #{edit.id}…{fallback_note}")
    delivery_message = None
    try:
        delivery_message = await _send_document_with_retry(
            update.effective_message,
            out_path,
            f"✅ Render complete. Job #{edit.id} ready.{fallback_note}",
            min(settings.upload_timeout_seconds, 120),
            _render_pool_keyboard(edit.id, saved=False),
        )
    except Exception as exc:
        LOGGER.warning("Telegram upload failed for edit %s: %s", edit.id, exc)
        try:
            delivery_message = await _send_render_download_link(
                update.effective_message, context, edit, settings, db_path,
                return_message=True,
            )
        except Exception as link_exc:
            delivery_message = await update.effective_message.reply_text(
                f"✅ Render complete. Job #{edit.id} ready.{fallback_note}\n"
                f"⚠️ Could not send the file ({exc}) or fallback link ({link_exc}).",
                reply_markup=_render_pool_keyboard(edit.id, saved=False),
            )
    delivery_message_id = _telegram_message_id(delivery_message)
    if delivery_message_id is not None:
        try:
            await update_edit_job(
                db_path,
                edit.id,
                render_delivery_message_id=delivery_message_id,
                metadata_result_message_id=delivery_message_id,
            )
        except Exception:
            LOGGER.exception("Could not persist render delivery for edit %s", edit.id)
    try:
        if not await _enqueue_metadata(context, edit.id, delivery_message):
            LOGGER.info("Metadata work was not queued for rendered edit %s", edit.id)
    except Exception:
        # Metadata is explicitly post-delivery and must never turn a successful
        # render into a failed render.
        LOGGER.exception("Could not enqueue metadata for rendered edit %s", edit.id)
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
    if message is None or not _admin_authorized(update, settings):
        return
    if not settings.repair_enabled:
        await message.reply_text(
            "AI repair execution is disabled. Set MEDIA_BOT_ENABLE_REPAIR=true "
            "and restart the bot to enable it for admins."
        )
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
        fix_result = await apply_known_fix(
            error_info,
            settings.tools_dir,
            repair_enabled=settings.repair_enabled,
        )
        if category == "unknown":
            workspace = Path.cwd()
            script_path = await invoke_opencode_fix(error_info, workspace, model=model)
            if script_path:
                code, output = await run_fix_script(
                    script_path, repair_enabled=settings.repair_enabled,
                )
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
    if message is None or not _admin_authorized(update, settings):
        return

    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    error_files = list(ERRORS_DIR.glob("*.json"))
    pending = sum(1 for ef in error_files if not ef.name.startswith(("fixed_", "failed_", "unfixed_")))
    fixed = sum(1 for ef in error_files if ef.name.startswith("fixed_"))
    failed = sum(1 for ef in error_files if ef.name.startswith("failed_"))
    unfixed = sum(1 for ef in error_files if ef.name.startswith("unfixed_"))

    db_health = "unavailable"
    fk_violations: int | str = "unknown"
    job_states: list[str] = []
    metadata_states: list[str] = []
    try:
        async with open_database(settings.db_path) as db:
            async with db.execute("PRAGMA quick_check") as cursor:
                row = await cursor.fetchone()
                db_health = str(row[0]) if row else "unknown"
            async with db.execute(
                "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status"
            ) as cursor:
                job_states = [f"{state}={count}" for state, count in await cursor.fetchall()]
            async with db.execute(
                "SELECT metadata_status, count(*) FROM edit_jobs "
                "GROUP BY metadata_status ORDER BY metadata_status"
            ) as cursor:
                metadata_states = [
                    f"{state}={count}" for state, count in await cursor.fetchall()
                ]
        fk_violations = len(await foreign_key_violations(settings.db_path))
    except Exception as exc:
        LOGGER.warning("Status database check failed: %s", exc)

    try:
        usage = shutil.disk_usage(settings.storage_dir)
        disk_line = (
            f"Disk: {usage.free / 1024 ** 3:.1f} GiB free of "
            f"{usage.total / 1024 ** 3:.1f} GiB"
        )
    except OSError:
        disk_line = "Disk: unavailable"

    download_work: WorkQueue | None = context.application.bot_data.get("download_work")
    render_work: WorkQueue | None = context.application.bot_data.get("render_work")
    metadata_work: WorkQueue | None = context.application.bot_data.get("metadata_work")
    uptime = _format_duration(time.monotonic() - STARTED_MONOTONIC)

    lines = [
        "🤖 Operator Status",
        f"Uptime: {uptime}",
        f"Database: {db_health}; foreign-key violations={fk_violations}",
        f"Jobs: {', '.join(job_states) if job_states else 'none'}",
        f"Metadata: {', '.join(metadata_states) if metadata_states else 'none'}",
        (
            f"Download queue: active={download_work.active}, queued={download_work.queued}"
            if download_work else "Download queue: unavailable"
        ),
        (
            f"Render queue: active={render_work.active}, queued={render_work.queued}"
            if render_work else "Render queue: unavailable"
        ),
        (
            f"Metadata queue: active={metadata_work.active}, queued={metadata_work.queued}"
            if metadata_work else "Metadata queue: unavailable"
        ),
        disk_line,
        f"AI repair execution: {'enabled' if settings.repair_enabled else 'disabled'}",
        "",
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
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        return
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    body = (message.text or message.caption or "") if message else ""
    callback = update.callback_query.data if update.callback_query else None
    append_event(
        "update",
        "authorized update received",
        update_id=update.update_id,
        user_id=user.id if user else None,
        chat_id=chat.id if chat else None,
        message_length=len(body),
        has_photo=bool(message and message.photo),
        callback_prefix=callback.split(":", 1)[0] if callback else None,
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not _authorized(update, settings):
        return
    issue = redact_sensitive(" ".join(context.args).strip(), 2000)
    if not issue:
        await message.reply_text("Usage: /report <what went wrong>")
        return

    events = recent_events(user_id=user.id, limit=100)
    report_id = (
        f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{user.id}"
    )
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
    write_redacted_json(report_path, report)
    append_event("user_report", issue, report_id=report_id, user_id=user.id)

    await message.reply_text(
        f"✅ Report {report_id} saved for operator review with "
        f"{len(events)} recent events. No code was executed."
    )


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


async def _notify_restart_online(application: Application, settings: Settings) -> bool:
    if not RESTART_MARKER.is_file():
        return False
    explicit_chat = (
        os.getenv("TELEGRAM_RESTART_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_ERROR_CHAT_ID", "").strip()
    )
    chat_id = int(explicit_chat) if explicit_chat else next(
        iter(sorted(settings.allowed_chat_ids or settings.allowed_user_ids)), None,
    )
    if chat_id is None:
        return False
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text="🟢 MediaDL bot is back online.",
        )
        RESTART_MARKER.unlink(missing_ok=True)
        RESTART_ACK.unlink(missing_ok=True)
        append_event("restart_online_notified", "Restart online notification sent")
        return True
    except Exception as exc:
        LOGGER.warning("Could not send restart online notification: %s", exc)
        return False


async def _resume_metadata_work(application: Application) -> None:
    """Re-admit only durable metadata jobs whose final delivery is usable."""
    db_path: Path = application.bot_data["db_path"]
    work: WorkQueue = application.bot_data["metadata_work"]
    pending = await list_metadata_jobs(db_path)
    resumable = {
        edit.id
        for edit in await list_resumable_metadata_jobs(db_path)
        if edit.file_path and Path(edit.file_path).is_file()
    }
    context = SimpleNamespace(application=application, bot=application.bot)
    for edit in pending:
        if edit.id not in resumable:
            await update_edit_job(
                db_path,
                edit.id,
                metadata_status="failed",
                metadata_error=(
                    "metadata work was not resumed because the final video "
                    "or successful delivery message is missing"
                ),
                metadata_completed_at=datetime.now(timezone.utc).isoformat(),
            )
            continue
        try:
            work.submit(
                user_id=edit.user_id,
                label=f"metadata:{edit.id}",
                factory=lambda edit_id=edit.id: _metadata_job(context, edit_id),
            )
        except WorkAlreadyQueued:
            continue
        except WorkRejected as exc:
            LOGGER.warning("Could not resume metadata edit %s: %s", edit.id, exc)
            await update_edit_job(
                db_path,
                edit.id,
                metadata_status="failed",
                metadata_error=f"metadata recovery queue rejected work: {exc}"[:500],
                metadata_completed_at=datetime.now(timezone.utc).isoformat(),
            )


async def _post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    db_path: Path = application.bot_data["db_path"]
    storage_dir: Path = application.bot_data["storage_dir"]

    app = create_download_app(db_path, storage_dir)
    # Download URLs contain bearer tokens; aiohttp's default access log would
    # persist the full request target. Handler logs use token-free identifiers.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.download_bind_host, settings.download_port)
    await site.start()
    application.bot_data["download_runner"] = runner
    LOGGER.info("Download server started on port %d", settings.download_port)

    interrupted_jobs, interrupted_edits = await reconcile_interrupted_work(db_path)
    if interrupted_jobs or interrupted_edits:
        LOGGER.warning(
            "Reconciled interrupted work after restart: %d downloads, %d renders",
            interrupted_jobs,
            interrupted_edits,
        )
    application.bot_data["download_work"].start()
    application.bot_data["render_work"].start()
    application.bot_data["metadata_work"].start()
    await _resume_metadata_work(application)

    cleaned = await cleanup_download_messages(db_path, application.bot)
    if cleaned:
        LOGGER.info("Startup: cleaned up %d expired download messages", cleaned)

    await _notify_restart_online(application, settings)


async def _post_shutdown(application: Application) -> None:
    for key in ("download_work", "render_work", "metadata_work"):
        work = application.bot_data.get(key)
        if work is not None:
            await work.stop()
    runner = application.bot_data.get("download_runner")
    if runner is not None:
        await runner.cleanup()


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

    application = builder.post_init(_post_init).post_shutdown(_post_shutdown).build()
    application.bot_data["settings"] = settings
    application.bot_data["ytdlp"] = ytdlp
    gallerydl_path = shutil.which("gallery-dl")
    if gallerydl_path is None:
        gallerydl_path = str(Path(sys.executable).with_name("gallery-dl"))
    application.bot_data["gallerydl"] = Path(gallerydl_path)
    application.bot_data["db_path"] = db_path
    application.bot_data["storage_dir"] = storage_dir
    application.bot_data["download_work"] = WorkQueue(
        name="download",
        workers=settings.download_workers,
        capacity=settings.work_queue_capacity,
        per_user_capacity=settings.per_user_work_capacity,
    )
    application.bot_data["render_work"] = WorkQueue(
        name="render",
        workers=settings.render_workers,
        capacity=settings.work_queue_capacity,
        per_user_capacity=settings.per_user_work_capacity,
    )
    application.bot_data["metadata_work"] = WorkQueue(
        name="metadata",
        workers=settings.metadata_workers,
        capacity=settings.work_queue_capacity,
        per_user_capacity=settings.per_user_work_capacity,
    )

    application.add_handler(TypeHandler(Update, audit_update, block=False), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("voices", voices_command))
    application.add_handler(CommandHandler("tiktokaccount", tiktok_account_command))
    application.add_handler(CommandHandler("settings", settings_command_entry))
    application.add_handler(CommandHandler("presets", presets_command_entry))
    application.add_handler(CommandHandler("pool", pool_command_entry))
    application.add_handler(CommandHandler("fix", fix_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^settings:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^preset:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^edit:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^preset_create:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^pool:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^workflow:"))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("canceljob", cancel_job_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("cleanup", cleanup_command))
    application.add_handler(CommandHandler("editconfig", editconfig_command))
    application.add_handler(CallbackQueryHandler(editconfig_callback, pattern=r"^editcfg:"))
    application.add_handler(CallbackQueryHandler(watermark_callback, pattern=r"^watermark:"))
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^download:"))
    application.add_handler(MessageHandler(~filters.COMMAND, _message_router))

    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(cleanup_task, interval=6 * 60 * 60, first=60)

    if settings.download_public_origin:
        LOGGER.info(
            "Secure downloads enabled at %s/download/<token>",
            settings.download_public_origin,
        )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
