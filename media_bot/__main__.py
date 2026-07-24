from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .downloader import DownloadError, download_media, download_tiktok_slideshow
from .platforms import extract_supported_urls, is_tiktok_photo_url
from .tools import provision_ytdlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
HELP_TEXT = (
    "Commands:\n"
    "/help - show this message\n\n"
    "Send a YouTube, Instagram, TikTok, or Facebook link anywhere in a message. "
    "The bot downloads the first supported link it finds."
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


async def _upload_with_retry(message, media: Path) -> None:
    for attempt in range(2):
        try:
            with media.open("rb") as uploaded:
                await message.reply_document(document=uploaded, filename=media.name, caption="Media")
            return
        except (NetworkError, TimedOut):
            if attempt:
                raise
            LOGGER.warning("Telegram upload timed out; retrying once")
            await asyncio.sleep(3)


def _authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return False
    if chat.type == "channel":
        return chat.id in settings.allowed_chat_ids
    return bool(user and user.id in settings.allowed_user_ids) or chat.id in settings.allowed_chat_ids


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    ytdlp = context.application.bot_data["ytdlp"]
    gallerydl: Path = context.application.bot_data["gallerydl"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected update for unapproved chat/user")
        return
    text = message.text or message.caption or ""
    urls = extract_supported_urls(text)
    if not urls:
        await message.reply_text("Send one supported YouTube, Instagram, TikTok, or Facebook URL.")
        return
    url = urls[0]
    status = await message.reply_text("Searching…")
    reporter = DownloadReporter(status)
    temporary = None
    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        if is_tiktok_photo_url(url):
            temporary, media = await download_tiktok_slideshow(
                gallerydl, url, settings.max_filesize_mb, settings.timeout_seconds, reporter.progress,
            )
        else:
            temporary, media = await download_media(
                ytdlp, url, settings.max_filesize_mb, settings.timeout_seconds, reporter.progress,
            )
        await status.edit_text("Downloaded")
        await _upload_with_retry(message, media)
    except DownloadError as exc:
        await status.edit_text(f"Download failed: {exc}")
    except Exception:
        LOGGER.exception("Unexpected download/upload failure")
        await status.edit_text("Download or upload failed. The source may be unavailable or exceed Telegram's limit.")
    finally:
        if temporary is not None:
            temporary.cleanup()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected help request for unapproved chat/user")
        return
    await message.reply_text(HELP_TEXT)


def main() -> None:
    settings = Settings.from_environment()
    ytdlp = provision_ytdlp(settings.tools_dir, settings.ytdlp_version)
    if shutil.which("ffmpeg") is None:
        LOGGER.warning("ffmpeg not found; some separate audio/video streams cannot be merged")
    application = (
        Application.builder().token(settings.token).concurrent_updates(4)
        .connect_timeout(20).read_timeout(120).write_timeout(settings.upload_timeout_seconds).pool_timeout(20).build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["ytdlp"] = ytdlp
    application.bot_data["gallerydl"] = Path(sys.executable).with_name("gallery-dl")
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, handle_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
