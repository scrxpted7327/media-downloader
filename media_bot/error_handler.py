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
from .diagnostics import append_event

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
    append_event(
        "error",
        error_msg,
        error_id=error_id,
        user_id=update_data.get("effective_user") if update_data else None,
        chat_id=update_data.get("effective_chat") if update_data else None,
        traceback=tb_str[:5000],
    )
    LOGGER.error("Error logged: %s — %s", error_id, error_msg)
