from __future__ import annotations

import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

EVENTS_PATH = Path("runtime/events.jsonl")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bot\d+:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+)"
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def redact_sensitive(value: str, limit: int = 5000) -> str:
    """Remove common secrets and URL query/fragment data from diagnostics."""
    text = _SECRET_PATTERN.sub("[REDACTED]", str(value))

    def clean_url(match: re.Match[str]) -> str:
        try:
            parts = urlsplit(match.group(0))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    return _URL_PATTERN.sub(clean_url, text)[:limit]


def append_event(kind: str, message: str, **context: Any) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": redact_sensitive(message, 2000),
        **context,
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, default=str) + "\n")


def recent_events(*, user_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if not EVENTS_PATH.is_file():
        return []
    matches: deque[dict[str, Any]] = deque(maxlen=limit)
    with EVENTS_PATH.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_user = event.get("user_id")
            if (
                user_id is None
                or str(event_user) == str(user_id)
                or (event_user is None and event.get("scope") == "global_health")
            ):
                matches.append(event)
    return list(matches)


class EventLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_event(
                "log",
                self.format(record),
                level=record.levelname,
                logger=record.name,
            )
        except Exception:
            pass


def install_event_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(handler, EventLogHandler) for handler in root.handlers):
        return
    handler = EventLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
