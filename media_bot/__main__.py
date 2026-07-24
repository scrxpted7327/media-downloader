from __future__ import annotations

import asyncio
import logging
import shutil

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .config import Settings
from .downloader import DownloadError, download_media
from .platforms import is_supported_url
from .tools import provision_ytdlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)


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
    message = update.effective_message
    if message is None or not _authorized(update, settings):
        LOGGER.warning("Rejected update for unapproved chat/user")
        return
    text = message.text or message.caption or ""
    urls = [part for part in text.split() if is_supported_url(part)]
    if not urls:
        await message.reply_text("Send one supported YouTube, Instagram, TikTok, or Facebook URL.")
        return
    url = urls[0]
    status = await message.reply_text("Downloading…")
    temporary = None
    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        temporary, media = await download_media(ytdlp, url, settings.max_filesize_mb, settings.timeout_seconds)
        with media.open("rb") as uploaded:
            await message.reply_document(document=uploaded, filename=media.name, caption="Downloaded media")
        await status.delete()
    except DownloadError as exc:
        await status.edit_text(f"Download failed: {exc}")
    except Exception:
        LOGGER.exception("Unexpected download/upload failure")
        await status.edit_text("Download or upload failed. The source may be unavailable or exceed Telegram's limit.")
    finally:
        if temporary is not None:
            temporary.cleanup()


def main() -> None:
    settings = Settings.from_environment()
    ytdlp = provision_ytdlp(settings.tools_dir, settings.ytdlp_version)
    if shutil.which("ffmpeg") is None:
        LOGGER.warning("ffmpeg not found; some separate audio/video streams cannot be merged")
    application = Application.builder().token(settings.token).build()
    application.bot_data["settings"] = settings
    application.bot_data["ytdlp"] = ytdlp
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
