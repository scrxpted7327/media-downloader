from __future__ import annotations

import json
import logging
import traceback as tb
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from .fix_agent import ERRORS_DIR, categorize_error

LOGGER = logging.getLogger(__name__)


def write_error_log(
    error_id: str,
    message: str,
    traceback_str: str,
    update_data: dict[str, Any] | None = None,
) -> Path:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    error_info = {
        "id": error_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "traceback": traceback_str,
        "category": categorize_error(message),
        "update": update_data,
    }
    path = ERRORS_DIR / f"{error_id}.json"
    path.write_text(json.dumps(error_info, indent=2, default=str), encoding="utf-8")
    return path


async def error_handler(update: Update | None, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        return

    tb_str = "".join(tb.format_exception(None, error, error.__traceback__))
    error_msg = str(error)[:500]
    error_id = f"err_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{id(error)}"

    update_data = None
    if update:
        try:
            update_data = {
                "update_id": update.update_id,
                "effective_user": str(update.effective_user.id) if update.effective_user else None,
                "effective_chat": str(update.effective_chat.id) if update.effective_chat else None,
                "effective_message": update.effective_message.text if update.effective_message and update.effective_message.text else None,
            }
        except Exception:
            pass

    write_error_log(error_id, error_msg, tb_str, update_data)
    LOGGER.error("Error logged: %s — %s", error_id, error_msg)

    try:
        user = update.effective_user if update else None
        chat = update.effective_chat if update else None
        bot = context.bot

        admin_chat_id = None
        settings = context.application.bot_data.get("settings") if context.application else None
        if settings and settings.allowed_user_ids:
            admin_chat_id = next(iter(settings.allowed_user_ids))
        elif settings and settings.allowed_chat_ids:
            admin_chat_id = next(iter(settings.allowed_chat_ids))
        elif user:
            admin_chat_id = user.id
        elif chat:
            admin_chat_id = chat.id

        if admin_chat_id:
            category = categorize_error(error_msg)
            report = (
                f"⚠️ Bot error\n"
                f"Category: {category}\n"
                f"Error: {error_msg[:300]}\n"
                f"ID: {error_id}"
            )
            if update and update.effective_message:
                report += f"\nChat: {update.effective_message.chat_id}"
            try:
                await bot.send_message(chat_id=admin_chat_id, text=report)
            except Exception as e:
                LOGGER.warning("Failed to send error report: %s", e)
    except Exception:
        LOGGER.exception("Error in error handler")
