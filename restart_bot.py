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
GRACEFUL_STOP_SECONDS = 8


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


def _termination_targets(pids: list[int]) -> tuple[set[int], set[int]]:
    """Split managed processes into safe process groups and individual fallbacks."""
    groups: set[int] = set()
    individuals: set[int] = set()
    current_group = os.getpgrp()
    for pid in pids:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if group > 0 and group != current_group:
            groups.add(group)
        else:
            individuals.add(pid)
    return groups, individuals


def _signal_targets(groups: set[int], individuals: set[int], sent_signal: int) -> None:
    for group in groups:
        try:
            os.killpg(group, sent_signal)
        except ProcessLookupError:
            pass
    for pid in individuals:
        try:
            os.kill(pid, sent_signal)
        except ProcessLookupError:
            pass


def _targets_alive(pids: list[int], groups: set[int]) -> bool:
    if any(Path(f"/proc/{pid}").exists() for pid in pids):
        return True
    for group in groups:
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            continue
        except PermissionError:
            return True
    return False


def _stop_existing() -> None:
    pids = _managed_processes()
    groups, individuals = _termination_targets(pids)
    _signal_targets(groups, individuals, signal.SIGTERM)
    deadline = time.monotonic() + GRACEFUL_STOP_SECONDS
    while time.monotonic() < deadline and _targets_alive(pids, groups):
        time.sleep(0.2)
    if _targets_alive(pids, groups):
        _signal_targets(groups, individuals, signal.SIGKILL)


def _project_python() -> Path:
    candidate = PROJECT_DIR / ".venv" / "bin" / "python"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return Path(sys.executable)


def _install_requirements(python: Path) -> None:
    """Reconcile the project environment before touching the live stack."""
    requirements = PROJECT_DIR / "requirements.txt"
    if not requirements.is_file():
        raise FileNotFoundError(f"project requirements file is missing: {requirements}")
    print(f"Installing project requirements with {python}...")
    # Keep the install tied to the interpreter that will run supervisor.py.
    # This is the equivalent of: pip install -r requirements.txt
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=PROJECT_DIR,
        check=True,
    )


def main() -> None:
    python = _project_python()
    # Install first so a failed dependency reconciliation leaves a healthy
    # existing stack running instead of turning a restart into downtime.
    _install_requirements(python)
    _request_restart_notification()
    _stop_existing()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [str(python), str(PROJECT_DIR / "supervisor.py")],
        cwd=PROJECT_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"Bot restarted with one supervisor (PID {process.pid}).")


if __name__ == "__main__":
    main()
