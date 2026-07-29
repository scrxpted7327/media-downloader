#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("supervisor")

ERRORS_DIR = Path("runtime/errors")
RESTART_DELAY = 3
MAX_RESTART_DELAY = 300
EVENTS_PATH = Path("runtime/events.jsonl")
SUPERVISOR_LOG = Path("runtime/supervisor.log")
_LOCK_HANDLE = None
_ERROR_LOG_PATTERN = re.compile(r"Error logged: (err_[A-Za-z0-9_]+)")


def load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _notification_chat_id() -> str | None:
    explicit = os.getenv("TELEGRAM_ERROR_CHAT_ID", "").strip()
    if explicit:
        return explicit
    for name in ("TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_USER_IDS"):
        first = next((item.strip() for item in os.getenv(name, "").split(",") if item.strip()), None)
        if first:
            return first
    return None


def _send_telegram_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _notification_chat_id()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and an error destination must be configured")
    api_base = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip().rstrip("/") or "https://api.telegram.org"
    request = urllib.request.Request(
        f"{api_base}/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4096]}).encode(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status >= 400:
            raise RuntimeError(f"Telegram returned HTTP {response.status}")


async def notify_error(error_id: str, path: Path | None = None) -> bool:
    path = path or ERRORS_DIR / f"{error_id}.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        message = str(info.get("message") or "Unknown bot error")
        category = str(info.get("category") or "unknown")
        traceback_text = str(info.get("traceback") or info.get("stderr") or "")
        update = info.get("update") or {}
        report = (
            "⚠️ Bot error reported by supervisor\n"
            f"Category: {category}\n"
            f"Error: {message[:700]}\n"
            f"ID: {error_id}"
        )
        if update.get("effective_chat"):
            report += f"\nChat: {update['effective_chat']}"
        if traceback_text:
            report += f"\n\nTraceback:\n{traceback_text[-2800:]}"
        await asyncio.to_thread(_send_telegram_message, report)
        append_event("error_report_sent", message, error_id=error_id)
        return True
    except Exception as exc:
        LOGGER.error("Could not send supervisor error report %s: %s", error_id, exc)
        append_event("error_report_failed", str(exc), error_id=error_id)
        return False


def acquire_supervisor_lock() -> None:
    """Exit when another supervisor already owns this project."""
    global _LOCK_HANDLE
    lock_path = Path("runtime/supervisor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_HANDLE = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another supervisor is already running")
    _LOCK_HANDLE.write(str(os.getpid()))
    _LOCK_HANDLE.flush()


def append_event(kind: str, message: str, **context) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": message[:5000],
        "source": "supervisor",
        **context,
    }
    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, default=str) + "\n")


async def _capture_stream(stream, name: str, output: deque[str]) -> None:
    if stream is None:
        return
    SUPERVISOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    while line := await stream.readline():
        text = line.decode("utf-8", "replace").rstrip()
        output.append(text)
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now(timezone.utc).isoformat()} {name}: {text}\n")
        if name == "stderr" or "error" in text.lower() or "failed" in text.lower():
            append_event("process_output", text, stream=name)
        match = _ERROR_LOG_PATTERN.search(text)
        if match:
            await notify_error(match.group(1))


def has_traceback(text: str) -> bool:
    return bool(re.search(r"Traceback \(most recent call last\)|Error:|raise \w+\(|^  File \".*\", line \d+", text, re.MULTILINE))


def has_code_error(text: str) -> bool:
    return bool(re.search(r"(SyntaxError|TypeError|ValueError|KeyError|IndexError|AttributeError|ImportError|ModuleNotFoundError|NameError|ZeroDivisionError|OSError|RuntimeError|StopIteration|RecursionError|SystemExit|KeyboardInterrupt|GeneratorExit)", text))


def classify_error(stderr: str) -> str:
    if not stderr.strip():
        return "no_output"
    lower = stderr.lower()
    if "no space" in lower or "disk quota" in lower:
        return "disk"
    if "address already in use" in lower:
        return "port_conflict"
    if "module not found" in lower or "importerror" in lower.replace(" ", ""):
        return "dependency"
    if "token" in lower and ("invalid" in lower or "expired" in lower):
        return "auth"
    if "connection" in lower and ("refused" in lower or "reset" in lower):
        return "network"
    if "timeout" in lower:
        return "timeout"
    if "database" in lower or "sqlite" in lower:
        return "database"
    if has_code_error(stderr):
        return "code_error"
    if has_traceback(stderr):
        return "crash"
    if "error" in lower or "failed" in lower or "exception" in lower:
        return "runtime"
    return "unknown"


async def write_error_file(error_id: str, stderr: str, category: str) -> Path:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "id": error_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": stderr[:500],
        "stderr": stderr[:5000],
        "category": category,
        "source": "supervisor",
    }
    path = ERRORS_DIR / f"{error_id}.json"
    path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return path


async def supervise(cwd: Path) -> None:
    bot_args = [sys.executable, "-m", "media_bot"]
    crash_count = 0
    last_crash_time = 0.0

    while True:
        LOGGER.info("Starting bot: %s", " ".join(bot_args))
        append_event("launch_attempt", "Starting bot", command=bot_args, cwd=str(cwd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *bot_args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            error_id = f"launch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
            detail = f"{exc.__class__.__name__}: {exc}"
            error_path = await write_error_file(error_id, detail, "launch_failure")
            append_event("launch_failure", detail, error_id=error_id)
            LOGGER.exception("Could not launch bot")
            await notify_error(error_id, error_path)
            await asyncio.sleep(RESTART_DELAY)
            continue
        start_time = time.monotonic()
        stdout_lines: deque[str] = deque(maxlen=2000)
        stderr_lines: deque[str] = deque(maxlen=2000)
        readers = (
            asyncio.create_task(_capture_stream(proc.stdout, "stdout", stdout_lines)),
            asyncio.create_task(_capture_stream(proc.stderr, "stderr", stderr_lines)),
        )

        try:
            await asyncio.wait_for(proc.wait(), timeout=86400 * 7)
        except asyncio.TimeoutError:
            LOGGER.info("Bot running for 7 days, restarting...")
            proc.kill()
            await proc.wait()
            await asyncio.gather(*readers, return_exceptions=True)
            crash_count = 0
            continue
        await asyncio.gather(*readers, return_exceptions=True)

        duration = time.monotonic() - start_time
        exit_code = proc.returncode or 0
        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines)
        append_event(
            "process_exit",
            f"Bot exited with code {exit_code}",
            exit_code=exit_code,
            duration_seconds=round(duration, 3),
            stdout_tail=stdout_text[-5000:],
            stderr_tail=stderr_text[-10000:],
        )
        LOGGER.info("Bot exited (code=%s, ran=%.1fs)", exit_code, duration)

        if exit_code == 0:
            crash_count = 0
            await asyncio.sleep(RESTART_DELAY)
            continue

        crash_count += 1
        now = time.monotonic()
        if now - last_crash_time > 600:
            crash_count = 1
        last_crash_time = now

        has_error_output = has_traceback(stderr_text) or has_code_error(stderr_text) or (
            "error" in stderr_text.lower() and exit_code != -9
        )

        if has_error_output:
            category = classify_error(stderr_text)
            error_id = f"crash_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
            error_path = await write_error_file(error_id, stderr_text, category)
            LOGGER.error("Crash #%d [%s]: %s…", crash_count, category, stderr_text[:250])
            await notify_error(error_id, error_path)
        else:
            LOGGER.info("No actionable error output (exit=%d), restarting...", exit_code)

        delay = min(RESTART_DELAY * (2 ** min(crash_count - 1, 6)), MAX_RESTART_DELAY)
        await asyncio.sleep(delay)


def main() -> None:
    cwd = Path.cwd()
    load_dotenv()
    acquire_supervisor_lock()
    LOGGER.info("Supervisor starting in %s", cwd)
    asyncio.run(supervise(cwd))


if __name__ == "__main__":
    main()
