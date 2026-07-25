from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
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
    filters,
)

from .config import Settings
from .downloader import DownloadError, download_instagram, download_media, download_tiktok_slideshow, persist_download
from .download_server import create_download_app
from .editor import list_tts_voices, render_edit
from .error_handler import error_handler
from .fix_agent import ERRORS_DIR, FIX_SCRIPTS_DIR, apply_known_fix, categorize_error, invoke_opencode_fix, load_error_log
from .platforms import extract_supported_urls, is_instagram_url, is_tiktok_photo_url, is_tiktok_url
from .settings_ui import (
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
    get_edit_job,
    get_job,
    init_db,
    list_all_jobs,
    list_source_jobs_for_user,
    list_user_jobs,
    mark_download_message_deleted,
    store_download_message,
    update_edit_job,
    update_job,
)
from .tools import prefer_ffmpeg_full, provision_ytdlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)

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
    "/fix - attempt auto-fix for bot errors\n"
    "/status - bot error status and health\n\n"
    "Send a YouTube, Instagram, TikTok, or Facebook link anywhere in a message. "
    "The bot downloads the first supported link it finds and provides a secure download link."
)


class DownloadReporter:
    """Change the status once yt-dlp starts transferring media bytes."""

    def __init__(self, message) -> None:
        self.message = message
        self.started = False

    async def progress(self, _: int) -> None:
        if self.started:
            return
        self.started = True
        await self.message.edit_text("⬇️ Downloading…")


class _ProgressReporter:
    """Reports rendering progress to a Telegram message with step names and ETA."""

    def __init__(self, message, steps: list[str]) -> None:
        self.message = message
        self.steps = steps
        self.start_time = time.monotonic()
        self.step_idx = 0
        self.last_pct = -1

    def set_step(self, idx: int) -> None:
        self.step_idx = idx

    async def __call__(self, pct: int) -> None:
        step_idx = self.step_idx
        step_name = self.steps[step_idx] if step_idx < len(self.steps) else "Finalizing"
        elapsed = time.monotonic() - self.start_time

        step_weight = 100.0 / len(self.steps)
        overall = int(step_idx * step_weight + (pct * step_weight / 100.0))
        overall = min(99, overall)

        if overall == self.last_pct:
            return
        self.last_pct = overall

        if overall > 0 and elapsed > 5:
            eta_seconds = elapsed * (100 - overall) / overall
            if eta_seconds > 60:
                eta_str = f" (~{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s)"
            elif eta_seconds > 0:
                eta_str = f" (~{int(eta_seconds)}s)"
            else:
                eta_str = ""
        else:
            eta_str = ""

        if elapsed < 60:
            elapsed_str = f"{int(elapsed)}s"
        else:
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

        bar_len = 10
        filled = overall * bar_len // 100
        bar = "▓" * filled + "░" * (bar_len - filled)

        text = (
            f"🎬 Step {step_idx + 1}/{len(self.steps)}: {step_name}\n"
            f"{bar} {overall}%\n"
            f"⏱ {elapsed_str}{eta_str}"
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


async def _send_secure_link(status_message, job_id: int, db_path: Path, user_id: int) -> None:
    rows = [
        [InlineKeyboardButton("⬇️ Download Original", callback_data=f"download:orig:{job_id}")],
        [InlineKeyboardButton("✂️ Edit", callback_data=f"download:edit:{job_id}"),
         InlineKeyboardButton("🔄 Reset", callback_data=f"download:reset:{job_id}")],
    ]
    from .storage import get_or_create_user_settings, list_presets
    presets = await list_presets(db_path, user_id)
    settings = await get_or_create_user_settings(db_path, user_id)
    active_name = settings.preset_name
    active = next((p for p in presets if active_name and p.name == active_name), None)
    others = [p for p in presets if active is None or p.id != active.id]
    if active is not None:
        rows.append([InlineKeyboardButton(
            f"⭐ 📥 Download {active.name}",
            callback_data=f"download:preset:{job_id}:{active.id}",
        )])
    if others:
        if len(others) <= 2:
            for p in others:
                rows.append([InlineKeyboardButton(
                    f"📥 Download {p.name}",
                    callback_data=f"download:preset:{job_id}:{p.id}",
                )])
        else:
            rows.append([InlineKeyboardButton(
                f"📦 More presets ({len(others)})",
                callback_data=f"download:presets:{job_id}",
            )])
    keyboard = InlineKeyboardMarkup(rows)
    await status_message.edit_text("✅ Download complete. Choose an action:", reply_markup=keyboard)


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
    handled = await settings_photo_entry(update, context)
    if handled:
        return
    handled = await pool_text_entry(update, context)
    if handled:
        return
    handled = await editconfig_text(update, context)
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
        await status.edit_text("💾 Saving…")
        persisted = await persist_download(media, job.id, storage_dir)
        await update_job(db_path, job.id, file_path=str(persisted), file_size=persisted.stat().st_size)
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

    job = await get_job(db_path, job_id)
    if job is None or job.file_path is None:
        await query.answer("File not found", show_alert=True)
        return

    current_user_id = query.from_user.id

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
        await _send_secure_link(query.message, job_id, db_path, current_user_id)
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
        edit = await create_edit_job(db_path, job_id, current_user_id, preset_id=None)
        dest = storage_dir / f"edit-{edit.id}-{source_path.name}"
        shutil.copy2(source_path, dest)
        await update_edit_job(db_path, edit.id, file_path=str(dest), file_size=dest.stat().st_size)
        await query.answer()
        edit_id = edit.id
        context.user_data["settings_flow"] = {
            "action": "editconfig",
            "edit_id": edit_id,
            "source_job_id": job_id,
        }
        await show_editconfig_menu(
            update,
            context,
            edit_id,
            intro=f"🎬 Edit job #{edit_id} created.\nCurrent settings are shown on each button.",
        )
        return

    if action == "reset":
        await query.answer()
        async with aiosqlite.connect(db_path) as db:
            await db.execute("DELETE FROM edit_jobs WHERE source_job_id = ? AND user_id = ? AND status IN ('pending', 'rendered')", (job_id, current_user_id))
            await db.commit()
        flow = context.user_data.get("settings_flow")
        if flow and flow.get("action") == "editconfig":
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
        shutil.copy2(source_path, dest)
        await update_edit_job(
            db_path, edit.id,
            file_path=str(dest), file_size=dest.stat().st_size,
            auto_captions=preset.auto_captions,
            watermark_removal=preset.watermark_removal,
            channel_banner=preset.channel_banner,
        )

        msg = await query.edit_message_text(f"🎬 Rendering with \"{preset.name}\"...")

        async def _preset_render_task():
            out_path = storage_dir / f"edit-{edit.id}-final.mp4"
            try:
                steps = []
                if preset.watermark_removal:
                    steps.append("Watermark removal")
                if preset.caption_text or preset.auto_captions:
                    steps.append("Captions")
                if preset.voice_text:
                    steps.append("Voice-over")
                if preset.channel_banner and job.url:
                    steps.append("Channel banner")
                if preset.banner_path:
                    steps.append("Banner overlay")
                if not steps:
                    steps.append("Rendering")

                reporter = _ProgressReporter(msg, steps)

                out_path, subtitles_path = await render_edit(
                    input_path=Path(dest),
                    output_path=out_path,
                    caption_text=preset.caption_text,
                    caption_color=preset.caption_color or "white",
                    caption_style=preset.caption_style or "basic",
                    caption_position=preset.caption_position or "bottom",
                    auto_captions=preset.auto_captions,
                    voice_text=preset.voice_text,
                    voice=preset.voice_over_voice or "default",
                    voice_quality=preset.voice_quality or "basic",
                    voice_speed=preset.voice_speed or 1.0,
                    tts_engine=preset.tts_engine,
                    banner_path=Path(preset.banner_path) if preset.banner_path else None,
                    banner_position=preset.banner_position or "bottom",
                    banner_scale=preset.banner_scale or "fit",
                    watermark_removal=preset.watermark_removal,
                    watermark_position=preset.watermark_position or "auto",
                    channel_banner=preset.channel_banner,
                    source_url=job.url,
                    timeout_seconds=settings.timeout_seconds,
                    progress_callback=reporter,
                )
            except DownloadError as exc:
                await update_edit_job(db_path, edit.id, status="failed", error_message=str(exc))
                await msg.edit_text(f"❌ Render failed: {exc}")
                return
            await update_edit_job(db_path, edit.id, status="rendered", file_path=str(out_path), file_size=out_path.stat().st_size, subtitles_path=subtitles_path)
            token = await create_download_token(db_path, edit.id, current_user_id, settings.token_expiry_minutes)
            url = _build_download_url(settings, token)
            dl_msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ Rendered with \"{preset.name}\" – one-time download ({settings.token_expiry_minutes} min):\n{url}",
                reply_to_message_id=query.message.message_id,
                disable_web_page_preview=True,
            )
            msg_record_id = await store_download_message(db_path, dl_msg.chat_id, dl_msg.message_id, settings.token_expiry_minutes)
            context.job_queue.run_once(
                _delete_expired_link,
                settings.token_expiry_minutes * 60,
                data={"chat_id": dl_msg.chat_id, "message_id": dl_msg.message_id, "db_path": str(db_path), "msg_record_id": msg_record_id},
            )
            if subtitles_path:
                srt_token = await create_download_token(db_path, edit.id, current_user_id, settings.token_expiry_minutes)
                srt_url = _build_download_url(settings, srt_token)
                srt_msg = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"📄 Subtitles: {srt_url}",
                    reply_to_message_id=query.message.message_id,
                    disable_web_page_preview=True,
                )
                srt_record_id = await store_download_message(db_path, srt_msg.chat_id, srt_msg.message_id, settings.token_expiry_minutes)
                context.job_queue.run_once(
                    _delete_expired_link,
                    settings.token_expiry_minutes * 60,
                    data={"chat_id": srt_msg.chat_id, "message_id": srt_msg.message_id, "db_path": str(db_path), "msg_record_id": srt_record_id},
                )
            try:
                await msg.edit_text(f"✅ Rendered with \"{preset.name}\" — download link sent above.")
            except Exception:
                pass

        asyncio.create_task(_preset_render_task())
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
        b_scale = edit.banner_scale if edit.banner_scale is not None else (preset.banner_scale or "fit" if preset else "fit")
        wm_removal = edit.watermark_removal
        wm_pos = edit.watermark_position if edit.watermark_position is not None else (preset.watermark_position or "auto" if preset else "auto")
        ch_banner = edit.channel_banner

        steps = []
        if wm_removal:
            steps.append("Watermark removal")
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
        )
    except DownloadError as exc:
        await update_edit_job(db_path, edit.id, status="failed", error_message=str(exc))
        await msg.edit_text(f"❌ Render failed: {exc}")
        return
    await update_edit_job(db_path, edit.id, status="rendered", file_path=str(out_path), file_size=out_path.stat().st_size, subtitles_path=subtitles_path)
    with out_path.open("rb") as f:
        await update.effective_message.reply_document(document=f, caption=f"✅ Render complete. Job #{edit.id} ready.")
    if subtitles_path:
        with Path(subtitles_path).open("rb") as f:
            await update.effective_message.reply_document(document=f, caption="📄 Subtitles available.")


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        return
    user = update.effective_user
    if user is None:
        return

    await message.reply_text("🔍 Scanning for errors to fix...")
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
        if fix_result is None:
            report_lines.append(f"✅ Fixed [{category}]: {error_info.get('message', '')[:80]}")
            ef.replace(ERRORS_DIR / f"fixed_{ef.name}")
        elif category == "unknown":
            from .fix_agent import invoke_opencode_fix
            workspace = Path.cwd()
            script_path = await invoke_opencode_fix(error_info, workspace)
            report_lines.append(f"🤖 Unknown [{category}]: fix script created at {script_path}")
            ef.replace(ERRORS_DIR / f"unfixed_{ef.name}")
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


async def _start_auto_fix(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]

    async def _report_to_admin(text: str):
        if settings.allowed_user_ids:
            chat_id = next(iter(settings.allowed_user_ids))
            try:
                await application.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    async def _watch_loop():
        from .fix_agent import watch_and_fix
        await watch_and_fix(
            workspace=Path.cwd(),
            tools_dir=settings.tools_dir,
            report_callback=_report_to_admin,
        )

    asyncio.create_task(_watch_loop())
    LOGGER.info("Auto-fix daemon started")


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

    await _start_auto_fix(application)

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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("voices", voices_command))
    application.add_handler(CommandHandler("settings", settings_command_entry))
    application.add_handler(CommandHandler("pool", pool_command_entry))
    application.add_handler(CommandHandler("fix", fix_command))
    application.add_handler(CommandHandler("status", status_command))
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
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^download:"))
    application.add_handler(MessageHandler(filters.PHOTO, _message_router))
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
