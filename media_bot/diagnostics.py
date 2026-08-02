from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

EVENTS_PATH = Path("runtime/events.jsonl")
EVENTS_MAX_BYTES = 5 * 1024 * 1024
EVENTS_BACKUP_COUNT = 3
RECENT_EVENTS_MAX_READ_BYTES = 2 * 1024 * 1024
RECENT_EVENTS_MAX_RESULTS = 1_000
MAX_EVENT_RECORD_BYTES = 128 * 1024
MAX_DIAGNOSTIC_DEPTH = 8
MAX_DIAGNOSTIC_ITEMS = 200
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bot\d+:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+)"
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[_-])(?:authorization|cookie|password|passwd|secret|token|api[_-]?key)(?:$|[_-])"
)


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


def redact_structure(
    value: Any,
    *,
    string_limit: int = 5000,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Recursively redact diagnostic data while keeping JSON-friendly types."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_sensitive(value, string_limit)
    if isinstance(value, bytes):
        return redact_sensitive(value.decode("utf-8", "replace"), string_limit)
    if _depth >= MAX_DIAGNOSTIC_DEPTH:
        return "[TRUNCATED: maximum diagnostic depth]"

    seen = _seen if _seen is not None else set()
    track_identity = isinstance(value, (dict, list, tuple, set, frozenset))
    identity = id(value)
    if track_identity and identity in seen:
        return "[TRUNCATED: recursive value]"
    if track_identity:
        seen.add(identity)
    try:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for index, (raw_key, item) in enumerate(value.items()):
                if index >= MAX_DIAGNOSTIC_ITEMS:
                    cleaned["[TRUNCATED]"] = "maximum diagnostic item count reached"
                    break
                key = redact_sensitive(str(raw_key), 200)
                if _SENSITIVE_KEY_PATTERN.search(key):
                    cleaned[key] = "[REDACTED]"
                else:
                    cleaned[key] = redact_structure(
                        item,
                        string_limit=string_limit,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
            return cleaned
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            cleaned_items = [
                redact_structure(
                    item,
                    string_limit=string_limit,
                    _depth=_depth + 1,
                    _seen=seen,
                )
                for item in items[:MAX_DIAGNOSTIC_ITEMS]
            ]
            if len(items) > MAX_DIAGNOSTIC_ITEMS:
                cleaned_items.append("[TRUNCATED: maximum diagnostic item count reached]")
            return cleaned_items
        return redact_sensitive(str(value), string_limit)
    finally:
        if track_identity:
            seen.discard(identity)


def _trim_file(path: Path, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    with path.open("rb") as stream:
        stream.seek(size - max_bytes)
        data = stream.read(max_bytes)
    newline = data.find(b"\n")
    if newline >= 0:
        data = data[newline + 1:]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.trim")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rotate_file(path: Path, backup_count: int, max_bytes: int) -> None:
    if backup_count < 1:
        path.unlink(missing_ok=True)
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    oldest.unlink(missing_ok=True)
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            _trim_file(source, max_bytes)
            os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        _trim_file(path, max_bytes)
        os.replace(path, path.with_name(f"{path.name}.1"))


def append_bounded_line(
    path: Path,
    line: str,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    """Append one line with cross-process locking and bounded file rotation."""
    if max_bytes < 1 or backup_count < 0:
        raise ValueError("log rotation limits must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = line.rstrip("\r\n") + "\n"
    encoded = normalized.encode("utf-8", "replace")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
        if not encoded.endswith(b"\n"):
            encoded = encoded.rstrip(b"\r\n") + b"\n"

    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current_size = path.stat().st_size if path.exists() else 0
            if current_size and current_size + len(encoded) > max_bytes:
                _rotate_file(path, backup_count, max_bytes)
            with path.open("ab") as stream:
                stream.write(encoded)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def append_event_record(
    path: Path,
    event: dict[str, Any],
    *,
    max_bytes: int = EVENTS_MAX_BYTES,
    backup_count: int = EVENTS_BACKUP_COUNT,
) -> None:
    """Redact and append one JSON event without allowing an oversized record."""
    cleaned = redact_structure(event, string_limit=5000)
    serialized = json.dumps(cleaned, default=str, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_EVENT_RECORD_BYTES:
        serialized = json.dumps(
            {
                "timestamp": cleaned.get("timestamp") if isinstance(cleaned, dict) else None,
                "kind": cleaned.get("kind", "oversized_event") if isinstance(cleaned, dict) else "oversized_event",
                "message": "[TRUNCATED: diagnostic event exceeded record limit]",
            },
            separators=(",", ":"),
        )
    append_bounded_line(
        path,
        serialized,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def append_event(kind: str, message: str, **context: Any) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": redact_sensitive(message, 2000),
        **context,
    }
    append_event_record(
        EVENTS_PATH,
        event,
        max_bytes=EVENTS_MAX_BYTES,
        backup_count=EVENTS_BACKUP_COUNT,
    )


def write_redacted_json(path: Path, payload: Any) -> None:
    """Write recursively redacted diagnostic JSON through an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = redact_structure(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(cleaned, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tail_bytes(path: Path, budget: int) -> tuple[bytes, int]:
    try:
        size = path.stat().st_size
        amount = min(size, budget)
        with path.open("rb") as stream:
            stream.seek(size - amount)
            data = stream.read(amount)
    except OSError:
        return b"", 0
    if amount < size:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return data, amount


def recent_events(*, user_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    bounded_limit = min(max(int(limit), 0), RECENT_EVENTS_MAX_RESULTS)
    if bounded_limit == 0 or not EVENTS_PATH.is_file():
        return []

    matches: deque[dict[str, Any]] = deque(maxlen=bounded_limit)
    remaining_bytes = RECENT_EVENTS_MAX_READ_BYTES
    paths = [EVENTS_PATH] + [
        EVENTS_PATH.with_name(f"{EVENTS_PATH.name}.{index}")
        for index in range(1, EVENTS_BACKUP_COUNT + 1)
    ]
    for path in paths:
        if remaining_bytes <= 0:
            break
        data, consumed = _tail_bytes(path, remaining_bytes)
        remaining_bytes -= consumed
        for raw_line in reversed(data.splitlines()):
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except ValueError:
                continue
            event_user = event.get("user_id")
            if (
                user_id is None
                or str(event_user) == str(user_id)
                or (event_user is None and event.get("scope") == "global_health")
            ):
                matches.appendleft(event)
                if len(matches) >= bounded_limit:
                    return list(matches)
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
