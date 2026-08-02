#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LOG_PATH = PROJECT_DIR / "runtime" / "supervisor.log"
RESTART_MARKER = PROJECT_DIR / "runtime" / "restart-requested"
RESTART_ACK = PROJECT_DIR / "runtime" / "restart-shutdown-notified"


def _managed_processes() -> list[int]:
    managed: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return managed
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = entry.joinpath("cmdline").read_bytes().split(b"\0")
            cwd = entry.joinpath("cwd").resolve()
        except (OSError, PermissionError):
            continue
        decoded = [arg.decode("utf-8", "replace") for arg in args if arg]
        is_bot = "-m" in decoded and "media_bot" in decoded
        is_supervisor = any(Path(arg).name == "supervisor.py" for arg in decoded[1:])
        if cwd == PROJECT_DIR and (is_bot or is_supervisor):
            managed.append(int(entry.name))
    return managed


def _supervisor_pid() -> int | None:
    for pid in _managed_processes():
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        decoded = [arg.decode("utf-8", "replace") for arg in args if arg]
        if any(Path(arg).name == "supervisor.py" for arg in decoded[1:]):
            return pid
    return None


def _request_restart_notification() -> None:
    """Signal the live supervisor and wait briefly for its Telegram acknowledgment."""
    RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
    RESTART_MARKER.write_text(f"requested_at={time.time()}\n", encoding="utf-8")
    RESTART_ACK.unlink(missing_ok=True)
    supervisor_pid = _supervisor_pid()
    if supervisor_pid is None:
        return
    try:
        os.kill(supervisor_pid, signal.SIGUSR1)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not RESTART_ACK.exists():
        time.sleep(0.1)


def _stop_existing() -> None:
    pids = _managed_processes()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in pids):
        time.sleep(0.2)
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main() -> None:
    _request_restart_notification()
    _stop_existing()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(PROJECT_DIR / "supervisor.py")],
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"Bot restarted with one supervisor (PID {process.pid}).")


if __name__ == "__main__":
    main()
