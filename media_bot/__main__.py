from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

import aiosqlite
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .downloader import DownloadError, download_media, download_tiktok_slideshow, persist_download
from .download_server import create_download_app
from .editor import render_edit
from .platforms import extract_supported_urls, is_tiktok_photo_url
from .settings_ui import settings_callback, settings_command, settings_text_handler
from .storage import (
    cleanup_expired_tokens,
    cleanup_old_jobs,
    consume_download_token,
    create_download_token,
    create_edit_job,
    create_job,
    get_edit_job,
    get_job,
    init_db,
    list_source_jobs_for_user,
    list_user_jobs,
    update_edit_job,
    update_job,
)
from .tools import provision_ytdlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)

HELP_TEXT = (
    "Commands:\n"
    "/help - show this message\n"
    "/settings - video presets and customization\n"
    "/pool - manage video pool and workflows\n"
    "/jobs - list your recent downloads\n"
    "/editconfig - set options for the current edit job\n"
    "/delete <job_id> - delete a downloaded file\n\n"
    "Send a YouTube, Instagram, TikTok, or Facebook link anywhere in a message. "
    "The bot downloads the first supported link it finds.\n\n"
    "Large files may receive a secure download link instead of a Telegram upload."
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
        await self.message.edit_text("Downloading…")


async def _upload_with_retry(message, media: Path, settings: Settings) -> tuple[bool, str | None]:
    """Try to upload via Telegram. Returns (success, error_message)."""
    file_size = media.stat().st_size
    local_api_limit = 2 * 1024 * 1024 * 1024

    if file_size > local_api_limit and not settings.local_api_url:
        return False, f"File is {file_size / 1024 / 1024:.1f} MB, exceeding Telegram's limit"

    last_exc = None
    for attempt in range(2):
        try:
            with media.open("rb") as uploaded:
                await message.reply_document(document=uploaded, filename=media.name, caption="Media")
            return True, None
        except (NetworkError, TimedOut) as exc:
            last_exc = exc
            if attempt:
                break
            LOGGER.warning("Telegram upload timed out; retrying once")
            await asyncio.sleep(3)

    return False, f"Upload failed: {last_exc}"


def _authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if chat.type == "channel":
        return chat.id in settings.allowed_chat_ids
    return bool(user and user.id in settings.allowed_user_ids) or chat.id in settings.allowed_chat_ids


async def _send_secure_link_button(status_message, job_id: int) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Get secure link", callback_data=f"secure_link:{job_id}")]]
    )
    await status_message.edit_text(
        "Download complete. File is too large for Telegram.\n"
        "Press the button below to get a secure download link.",
        reply_markup=keyboard,
    )


def _build_download_url(settings: Settings, token: str) -> str:
    domain = settings.download_domain or "localhost"
    return f"https://{domain}/download/{token}"


async def _message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(update, settings):
        LOGGER.warning("Rejected update for unapproved chat/user")
        return

    handled = await settings_text_entry(update, context)
    if handled:
        return
    handled = await pool_text_entry(update, context)
    if handled:
        return
    handled = await editconfig_text(update, context)
    if handled:
        return
    await handle_url(update, context)


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

    url = urls[0]
    status = await message.reply_text("Searching…")

    job = await create_job(db_path, url, user_id, chat_id)
    temporary = None
    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        if is_tiktok_photo_url(url):
            temporary, media = await download_tiktok_slideshow(
                gallerydl,
                url,
                settings.max_filesize_mb,
                settings.timeout_seconds,
                DownloadReporter(status).progress,
            )
        else:
            temporary, media = await download_media(
                ytdlp,
                url,
                settings.max_filesize_mb,
                settings.timeout_seconds,
                DownloadReporter(status).progress,
            )

        await status.edit_text("Saving…")
        persisted = await persist_download(media, job.id, storage_dir)
        await update_job(
            db_path, job.id, file_path=str(persisted), file_size=persisted.stat().st_size,
        )

        success, error = await _upload_with_retry(message, persisted, settings)
        if success:
            await update_job(
                db_path, job.id, status="uploaded", local_api_used=bool(settings.local_api_url),
            )
            await status.edit_text("Downloaded")
        else:
            await update_job(db_path, job.id, status="failed", error_message=error)
            await status.edit_text(f"Upload failed: {error}")
            if settings.download_domain:
                await _send_secure_link_button(status, job.id)
    except DownloadError as exc:
        await status.edit_text(f"Download failed: {exc}")
        await update_job(db_path, job.id, status="failed", error_message=str(exc))
    except Exception:
        LOGGER.exception("Unexpected download/upload failure")
        await status.edit_text(
            "Download or upload failed. The source may be unavailable or exceed Telegram's limit."
        )
        await update_job(db_path, job.id, status="failed", error_message="unexpected error")
    finally:
        if temporary is not None:
            temporary.cleanup()


async def secure_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    query = update.callback_query
    if query is None or query.data is None:
        return

    if not _authorized(update, settings):
        await query.answer("Not authorized", show_alert=True)
        return

    try:
        _, job_id_str = query.data.split(":", 1)
        job_id = int(job_id_str)
    except (ValueError, IndexError):
        await query.answer("Invalid request", show_alert=True)
        return

    job = await get_job(db_path, job_id)
    if job is None or job.file_path is None:
        await query.answer("File not found", show_alert=True)
        return

    if job.user_id != query.from_user.id:
        await query.answer("Not your file", show_alert=True)
        return

    token = await create_download_token(db_path, job.id, job.user_id, settings.token_expiry_minutes)
    url = _build_download_url(settings, token)
    await query.edit_message_text(
        f"Secure download link (one-time use, expires in {settings.token_expiry_minutes} min):\n{url}",
        disable_web_page_preview=True,
    )
    await query.answer()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected help request for unapproved chat/user")
        return
    await message.reply_text(HELP_TEXT)


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

    jobs = await list_user_jobs(settings.db_path, user.id, limit=10)
    if not jobs:
        await message.reply_text("No downloads yet.")
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
    flow = context.user_data.get("settings_flow")
    if flow is None:
        flow = {"action": "editconfig", "edit_id": edit_id, "source_job_id": source_job_id}
        context.user_data["settings_flow"] = flow
    else:
        flow["action"] = "editconfig"
        flow["edit_id"] = edit_id
        flow["source_job_id"] = source_job_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Caption text", callback_data="editcfg:caption_text")],
        [InlineKeyboardButton("Caption color", callback_data="editcfg:caption_color")],
        [InlineKeyboardButton("Caption style", callback_data="editcfg:caption_style")],
        [InlineKeyboardButton("Caption position", callback_data="editcfg:caption_position")],
        [InlineKeyboardButton("Voice name", callback_data="editcfg:voice_over_voice")],
        [InlineKeyboardButton("Voice quality", callback_data="editcfg:voice_quality")],
        [InlineKeyboardButton("Voice speed", callback_data="editcfg:voice_speed")],
        [InlineKeyboardButton("Render now", callback_data="editcfg:render")],
    ])
    await message.reply_text(f"Edit job #{edit_id} from source #{source_job_id}\nChoose an option:", reply_markup=keyboard)


async def editconfig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    settings: Settings = context.application.bot_data["settings"]
    db_path: Path = context.application.bot_data["db_path"]
    flow = context.user_data.get("settings_flow")
    if not flow or flow.get("action") != "editconfig":
        await query.edit_message_text("No active edit config. Use /editconfig.")
        return

    edit_id = flow.get("edit_id")
    if query.data == "editcfg:render":
        await _render_edit_job(update, context, edit_id)
        return

    field = query.data.split(":", 1)[1]
    flow["field_name"] = field
    pretty = field.replace("_", " ").title()
    await query.edit_message_text(f"Send new value for {pretty} (or /skip to clear):")


async def editconfig_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    flow = context.user_data.get("settings_flow")
    if not flow or flow.get("action") != "editconfig" or not update.message or not update.message.text:
        return False
    field = flow.get("field_name")
    edit_id = flow.get("edit_id")
    if field is None or edit_id is None:
        return False
    text = update.message.text.strip()
    value = None if text.lower() == "/skip" else text
    if field == "voice_speed":
        try:
            value = float(text)
            if not (0.5 <= value <= 2.0):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Enter a number between 0.5 and 2.0")
            return True
    if field in {"caption_color", "caption_style", "caption_position", "voice_quality"}:
        opts = {
            "caption_color": {"white", "black", "yellow", "red", "blue", "green"},
            "caption_style": {"basic", "bold", "bubble"},
            "caption_position": {"low", "middle", "high"},
            "voice_quality": {"basic", "premium"},
        }[field]
        if text.lower() not in opts:
            await update.message.reply_text(f"Choose: {', '.join(sorted(opts))}")
            return True
        value = text.lower()

    await update_edit_job(context.application.bot_data["db_path"], edit_id, **{field: value})
    flow.pop("field_name", None)
    await update.message.reply_text("Updated. Use /editconfig to change more or Render.")
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
    await update.effective_message.reply_text("Rendering... this may take a while.")
    out_path = storage_dir / f"edit-{edit.id}-final.mp4"
    try:
        preset = None
        if edit.preset_id:
            from .storage import get_preset_by_share_code, list_presets
            preset = None
            for p in await list_presets(db_path, edit.user_id):
                if p.id == edit.preset_id:
                    preset = p
                    break
        await render_edit(
            input_path=Path(edit.file_path),
            output_path=out_path,
            caption_text=preset.caption_text if preset else None,
            caption_color=preset.caption_color or "white" if preset else "white",
            caption_style=preset.caption_style or "basic" if preset else "basic",
            caption_position=preset.caption_position or "bottom" if preset else "bottom",
            voice_text=preset.voice_over_voice if preset else None,
            voice=preset.voice_over_voice or "default" if preset else "default",
            voice_quality=preset.voice_quality or "basic" if preset else "basic",
            voice_speed=preset.voice_speed or 1.0 if preset else 1.0,
            timeout_seconds=settings.timeout_seconds,
        )
    except DownloadError as exc:
        await update_edit_job(db_path, edit.id, status="failed", error_message=str(exc))
        await update.effective_message.reply_text(f"Render failed: {exc}")
        return
    await update_edit_job(db_path, edit.id, status="rendered", file_path=str(out_path), file_size=out_path.stat().st_size)
    await update.effective_message.reply_text(f"Render complete. Job #{edit.id} ready.", document=out_path.open("rb"))


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


def main() -> None:
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
    application.bot_data["gallerydl"] = Path(sys.executable).with_name("gallery-dl")
    application.bot_data["db_path"] = db_path
    application.bot_data["storage_dir"] = storage_dir

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command_entry))
    application.add_handler(CommandHandler("pool", pool_command_entry))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^settings:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^preset:"))
    application.add_handler(CallbackQueryHandler(settings_callback_entry, pattern=r"^edit:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^pool:"))
    application.add_handler(CallbackQueryHandler(pool_callback_entry, pattern=r"^workflow:"))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("editconfig", editconfig_command))
    application.add_handler(CallbackQueryHandler(editconfig_callback, pattern=r"^editcfg:"))
    application.add_handler(CallbackQueryHandler(secure_link_callback, pattern=r"^secure_link:\d+$"))
    application.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, _message_router))

    application.job_queue.run_repeating(cleanup_task, interval=6 * 60 * 60, first=60)

    if settings.download_domain:
        LOGGER.info(
            "Secure downloads enabled at https://%s/download/<token>", settings.download_domain,
        )

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
