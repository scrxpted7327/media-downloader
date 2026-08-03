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
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from media_bot.diagnostics import (
    append_bounded_line,
    append_event_record,
    write_redacted_json,
)

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
SUPERVISOR_STATE = Path("runtime/supervisor-state.json")
EVENTS_MAX_BYTES = 5 * 1024 * 1024
EVENTS_BACKUP_COUNT = 3
SUPERVISOR_LOG_MAX_BYTES = 10 * 1024 * 1024
SUPERVISOR_LOG_BACKUP_COUNT = 3
MAX_CONSECUTIVE_CRASHES = 8
GRACEFUL_STOP_SECONDS = 10
FORCE_KILL_SECONDS = 5
WEEKLY_RESTART_SECONDS = 86400 * 7
SUPERVISOR_HEARTBEAT_SECONDS = 5
RESTART_ACK = Path("runtime/restart-shutdown-notified")
_LOCK_HANDLE = None
_ERROR_LOG_PATTERN = re.compile(r"Error logged: (err_[A-Za-z0-9_]+)")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bot\d+:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"(?:token|secret|password|api[_-]?key)\s*[=:]\s*\S+)"
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


class _BoundedSupervisorLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_bounded_line(
                SUPERVISOR_LOG,
                redact_sensitive(self.format(record)),
                max_bytes=SUPERVISOR_LOG_MAX_BYTES,
                backup_count=SUPERVISOR_LOG_BACKUP_COUNT,
            )
        except Exception:
            pass


if not any(isinstance(handler, _BoundedSupervisorLogHandler) for handler in LOGGER.handlers):
    _bounded_log_handler = _BoundedSupervisorLogHandler()
    _bounded_log_handler.setLevel(logging.INFO)
    _bounded_log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    LOGGER.addHandler(_bounded_log_handler)


def redact_sensitive(value: str, limit: int = 5000) -> str:
    text = _SECRET_PATTERN.sub("[REDACTED]", str(value))

    def clean_url(match: re.Match[str]) -> str:
        try:
            parts = urllib.parse.urlsplit(match.group(0))
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    return _URL_PATTERN.sub(clean_url, text)[:limit]


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


def _restart_notification_chat_id() -> str | None:
    return os.getenv("TELEGRAM_RESTART_CHAT_ID", "").strip() or _notification_chat_id()


def _send_telegram_message(text: str, chat_id: str | None = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or _notification_chat_id()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and an error destination must be configured")
    api_base = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip().rstrip("/") or "https://api.telegram.org"
    request = urllib.request.Request(
        f"{api_base}/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4096]}).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 400:
                raise RuntimeError(f"Telegram returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read(4096).decode("utf-8", "replace"))
            if isinstance(payload, dict):
                detail = str(payload.get("description") or "")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        finally:
            exc.close()
        detail = redact_sensitive(detail or exc.reason or "unknown API error", 500)
        raise RuntimeError(f"Telegram API rejected sendMessage (HTTP {exc.code}): {detail}") from exc


async def notify_restart_shutdown() -> bool:
    """Handle restart_bot.py's SIGUSR1 shutdown-notification handshake."""
    success = False
    try:
        chat_id = _restart_notification_chat_id()
        if not chat_id:
            raise RuntimeError("a Telegram restart destination must be configured")
        await asyncio.to_thread(
            _send_telegram_message,
            "🔴 MediaDL bot is shutting down for a restart…",
            chat_id,
        )
        append_event("restart_shutdown_notified", "Restart shutdown notification sent")
        success = True
    except Exception as exc:
        LOGGER.error("Could not send restart shutdown notification: %s", exc)
        append_event("restart_shutdown_notification_failed", str(exc))
    finally:
        RESTART_ACK.parent.mkdir(parents=True, exist_ok=True)
        RESTART_ACK.write_text("sent\n" if success else "failed\n", encoding="utf-8")
    return success


async def notify_error(error_id: str, path: Path | None = None) -> bool:
    path = path or ERRORS_DIR / f"{error_id}.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        message = redact_sensitive(info.get("message") or "Unknown bot error", 700)
        category = str(info.get("category") or "unknown")
        traceback_text = redact_sensitive(
            info.get("traceback") or info.get("stderr") or "", 2800,
        )
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
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": redact_sensitive(message),
        "source": "supervisor",
        **context,
    }
    append_event_record(
        EVENTS_PATH,
        payload,
        max_bytes=EVENTS_MAX_BYTES,
        backup_count=EVENTS_BACKUP_COUNT,
    )


def _write_supervisor_state(state: dict[str, object]) -> None:
    """Publish a small atomic heartbeat for bot-side and operator diagnostics."""
    try:
        write_redacted_json(SUPERVISOR_STATE, state)
    except Exception:
        LOGGER.debug("Could not write supervisor state", exc_info=True)


async def _supervisor_heartbeat(
    state: dict[str, object],
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        state["pid"] = os.getpid()
        _write_supervisor_state(state)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=SUPERVISOR_HEARTBEAT_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


def _set_supervisor_state(
    state: dict[str, object],
    status: str,
    **updates: object,
) -> None:
    state.update(updates)
    state["state"] = status
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_supervisor_state(state)


async def _capture_stream(stream, name: str, output: deque[str]) -> None:
    if stream is None:
        return
    while line := await stream.readline():
        text = line.decode("utf-8", "replace").rstrip()
        output.append(text)
        append_bounded_line(
            SUPERVISOR_LOG,
            f"{datetime.now(timezone.utc).isoformat()} {name}: {redact_sensitive(text)}",
            max_bytes=SUPERVISOR_LOG_MAX_BYTES,
            backup_count=SUPERVISOR_LOG_BACKUP_COUNT,
        )
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
    if (
        "module not found" in lower
        or "no module named" in lower
        or "modulenotfounderror" in lower
        or "importerror" in lower.replace(" ", "")
    ):
        return "dependency"
    if "token" in lower and ("invalid" in lower or "expired" in lower):
        return "auth"
    if (
        "connection" in lower and ("refused" in lower or "reset" in lower)
        or "connecterror" in lower
        or "temporary failure in name resolution" in lower
        or "name or service not known" in lower
        or "dns" in lower
    ):
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
    write_redacted_json(path, info)
    return path


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    term_timeout: float = GRACEFUL_STOP_SECONDS,
    kill_timeout: float = FORCE_KILL_SECONDS,
) -> bool:
    """Stop the bot and all of its descendants, escalating only after a grace period."""
    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    if process.returncode is not None and not group_exists():
        return True
    wait_task = asyncio.create_task(process.wait())
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await asyncio.gather(wait_task, return_exceptions=True)
        return True

    loop = asyncio.get_running_loop()
    deadline = loop.time() + term_timeout
    while group_exists() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(deadline - loop.time(), 0)))
    if not group_exists():
        await asyncio.gather(wait_task, return_exceptions=True)
        return True

    LOGGER.warning("Bot process group %s ignored SIGTERM; sending SIGKILL", process.pid)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        await asyncio.gather(wait_task, return_exceptions=True)
        return True
    deadline = loop.time() + kill_timeout
    while group_exists() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(deadline - loop.time(), 0)))
    stopped = not group_exists()
    if stopped:
        await asyncio.gather(wait_task, return_exceptions=True)
        return True
    LOGGER.error("Bot process group %s did not exit after SIGKILL", process.pid)
    if not wait_task.done():
        wait_task.cancel()
    await asyncio.gather(wait_task, return_exceptions=True)
    return False


async def _wait_for_stop(stop_event: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return True
    except asyncio.TimeoutError:
        return False


async def _launch_bot(bot_args: list[str], cwd: Path) -> asyncio.subprocess.Process:
    """Launch the bot in its own session so its complete process tree is controllable."""
    return await asyncio.create_subprocess_exec(
        *bot_args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


async def supervise(cwd: Path) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    state: dict[str, object] = {
        "pid": os.getpid(),
        "state": "starting",
        "cwd": str(cwd),
        "child_pid": None,
        "crash_count": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_supervisor_state(state)
    restart_event = asyncio.Event()

    def request_stop(signum: signal.Signals) -> None:
        if not stop_event.is_set():
            LOGGER.info("Supervisor received %s; shutting down", signum.name)
            append_event("supervisor_shutdown_requested", "Supervisor shutdown requested", signal=signum.name)
            stop_event.set()

    def request_bot_restart() -> None:
        if not stop_event.is_set():
            LOGGER.info("Supervisor received %s; restarting the bot", signal.SIGUSR2.name)
            append_event("bot_restart_requested", "Bot restart requested after an operator repair")
            restart_event.set()

    installed_signals: list[signal.Signals] = []
    for signum, callback in (
        (signal.SIGUSR1, lambda: asyncio.create_task(notify_restart_shutdown())),
        (signal.SIGUSR2, request_bot_restart),
        (signal.SIGTERM, lambda: request_stop(signal.SIGTERM)),
        (signal.SIGINT, lambda: request_stop(signal.SIGINT)),
    ):
        try:
            loop.add_signal_handler(signum, callback)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            LOGGER.warning("Could not install handler for %s", signum.name)

    bot_args = [sys.executable, "-m", "media_bot"]
    crash_count = 0
    last_crash_time = 0.0
    heartbeat_task = asyncio.create_task(
        _supervisor_heartbeat(state, stop_event),
        name="supervisor-heartbeat",
    )

    try:
        while not stop_event.is_set():
            _set_supervisor_state(
                state,
                "starting",
                child_pid=None,
                crash_count=crash_count,
                next_restart_at=None,
            )
            LOGGER.info("Starting bot: %s", " ".join(bot_args))
            append_event("launch_attempt", "Starting bot", command=bot_args, cwd=str(cwd))
            try:
                proc = await _launch_bot(bot_args, cwd)
            except Exception as exc:
                error_id = f"launch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
                detail = f"{exc.__class__.__name__}: {exc}"
                error_path = await write_error_file(error_id, detail, "launch_failure")
                append_event("launch_failure", detail, error_id=error_id)
                LOGGER.exception("Could not launch bot")
                await notify_error(error_id, error_path)
                _set_supervisor_state(
                    state,
                    "backoff",
                    child_pid=None,
                    crash_count=crash_count + 1,
                    last_error=detail,
                    last_error_category="launch_failure",
                )
                if await _wait_for_stop(stop_event, RESTART_DELAY):
                    return
                continue
            start_time = time.monotonic()
            _set_supervisor_state(
                state,
                "running",
                child_pid=proc.pid,
                child_started_at=datetime.now(timezone.utc).isoformat(),
                last_error=None,
                next_restart_at=None,
                crash_count=crash_count,
            )
            stdout_lines: deque[str] = deque(maxlen=2000)
            stderr_lines: deque[str] = deque(maxlen=2000)
            readers = (
                asyncio.create_task(_capture_stream(proc.stdout, "stdout", stdout_lines)),
                asyncio.create_task(_capture_stream(proc.stderr, "stderr", stderr_lines)),
            )
            process_wait = asyncio.create_task(proc.wait())
            shutdown_wait = asyncio.create_task(stop_event.wait())
            restart_wait = asyncio.create_task(restart_event.wait())

            try:
                done, _ = await asyncio.wait(
                    {process_wait, shutdown_wait, restart_wait},
                    timeout=WEEKLY_RESTART_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown_wait in done:
                    _set_supervisor_state(
                        state,
                        "stopping",
                        child_pid=proc.pid,
                        reason="shutdown requested",
                    )
                    await _terminate_process_group(proc)
                    await asyncio.gather(*readers, return_exceptions=True)
                    append_event("supervisor_stopped", "Supervisor and bot stopped gracefully")
                    _set_supervisor_state(
                        state,
                        "stopped",
                        child_pid=None,
                        reason="shutdown requested",
                    )
                    return
                if restart_wait in done:
                    await _terminate_process_group(proc)
                    await asyncio.gather(*readers, return_exceptions=True)
                    restart_event.clear()
                    crash_count = 0
                    append_event("bot_restart_completed", "Bot restarted after an operator repair")
                    continue
                if process_wait not in done:
                    LOGGER.info("Bot running for 7 days; restarting gracefully")
                    append_event("weekly_restart", "Starting scheduled weekly bot restart")
                    _set_supervisor_state(
                        state,
                        "restarting",
                        child_pid=proc.pid,
                        reason="scheduled weekly restart",
                    )
                    await _terminate_process_group(proc)
                    await asyncio.gather(*readers, return_exceptions=True)
                    crash_count = 0
                    continue
            except asyncio.CancelledError:
                await _terminate_process_group(proc)
                await asyncio.gather(*readers, return_exceptions=True)
                raise
            finally:
                for waiter in (process_wait, shutdown_wait, restart_wait):
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(
                    process_wait,
                    shutdown_wait,
                    restart_wait,
                    return_exceptions=True,
                )

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
            _set_supervisor_state(
                state,
                "exited" if exit_code == 0 else "crashed",
                child_pid=None,
                last_exit_code=exit_code,
                last_exit_at=datetime.now(timezone.utc).isoformat(),
                last_exit_duration_seconds=round(duration, 3),
                crash_count=crash_count,
            )

            if exit_code == 0:
                crash_count = 0
                _set_supervisor_state(
                    state,
                    "backoff",
                    child_pid=None,
                    crash_count=0,
                    reason="bot exited cleanly; waiting before restart",
                    next_restart_at=datetime.fromtimestamp(
                        time.time() + RESTART_DELAY, timezone.utc,
                    ).isoformat(),
                )
                if await _wait_for_stop(stop_event, RESTART_DELAY):
                    return
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
                if category == "dependency":
                    LOGGER.critical(
                        "Stopping supervisor after dependency failure; run restart_bot.py "
                        "to install requirements before starting the stack"
                    )
                    append_event(
                        "supervisor_stopped",
                        "Supervisor stopped after a dependency failure",
                        category=category,
                        error_id=error_id,
                    )
                    _set_supervisor_state(
                        state,
                        "halted",
                        child_pid=None,
                        crash_count=crash_count,
                        reason="dependency failure requires operator restart",
                        last_error_category=category,
                        last_error=stderr_text[-1000:],
                    )
                    return
            else:
                LOGGER.info("No actionable error output (exit=%d), restarting...", exit_code)

            if crash_count >= MAX_CONSECUTIVE_CRASHES:
                LOGGER.critical(
                    "Stopping supervisor after %d consecutive bot crashes; operator intervention required",
                    crash_count,
                )
                append_event(
                    "supervisor_stopped",
                    "Supervisor stopped after repeated bot crashes",
                    crash_count=crash_count,
                )
                _set_supervisor_state(
                    state,
                    "halted",
                    child_pid=None,
                    crash_count=crash_count,
                    reason="maximum consecutive crashes reached",
                    last_error_category="repeated_crash",
                    last_error=stderr_text[-1000:],
                )
                return

            delay = min(RESTART_DELAY * (2 ** min(crash_count - 1, 6)), MAX_RESTART_DELAY)
            _set_supervisor_state(
                state,
                "backoff",
                child_pid=None,
                crash_count=crash_count,
                reason=f"bot crashed; retrying in {delay}s",
                next_restart_at=datetime.fromtimestamp(
                    time.time() + delay, timezone.utc,
                ).isoformat(),
            )
            if await _wait_for_stop(stop_event, delay):
                return
    finally:
        shutdown_requested = stop_event.is_set()
        stop_event.set()
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if state.get("state") not in {"halted", "stopped"}:
            _set_supervisor_state(
                state,
                "stopped" if shutdown_requested else "halted",
                child_pid=None,
            )
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


def main() -> None:
    cwd = Path.cwd()
    load_dotenv()
    acquire_supervisor_lock()
    LOGGER.info("Supervisor starting in %s", cwd)
    try:
        asyncio.run(supervise(cwd))
    except KeyboardInterrupt:
        LOGGER.info("Supervisor interrupted")
    except BaseException:
        LOGGER.exception("Supervisor stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
