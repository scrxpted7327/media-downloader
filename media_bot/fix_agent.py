from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

ERRORS_DIR = Path("runtime/errors")
FIX_SCRIPTS_DIR = Path("runtime/fix_scripts")
KNOWN_FIXES_DIR = Path("runtime/known_fixes")

_ERROR_CATEGORIES: dict[str, str] = {
    "yt-dlp": "ytdlp",
    "ytdlp": "ytdlp",
    "downloader failed": "downloader",
    "download failed": "downloader",
    "timed out": "timeout",
    "timeout": "timeout",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffmpeg",
    "database": "database",
    "disk": "disk",
    "no space": "disk",
    "quota": "disk",
    "gallery-dl": "gallerydl",
    "authentication": "auth",
    "login": "auth",
    "token": "auth",
    "forbidden": "auth",
    "import": "dependency",
    "no module": "dependency",
    "ModuleNotFoundError": "dependency",
    "connection": "network",
    "connection refused": "network",
    "connection reset": "network",
    "dns": "network",
}


def categorize_error(error_message: str) -> str:
    lower = error_message.lower()
    for keyword, category in _ERROR_CATEGORIES.items():
        if keyword in lower:
            return category
    return "unknown"


def load_error_log(error_path: Path) -> dict | None:
    try:
        return json.loads(error_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


async def apply_known_fix(error_info: dict, tools_dir: Path) -> str | None:
    category = error_info.get("category", "unknown")
    error_message = error_info.get("message", "")

    if category == "ytdlp":
        return await _fix_ytdlp(tools_dir)
    if category == "ffmpeg":
        if shutil.which("ffmpeg") is None:
            return "ffmpeg is not installed. Install it with: apt install ffmpeg"
        return None
    if category == "dependency":
        return await _fix_dependency(error_message)
    if category == "disk":
        return "Disk space issue detected. Free up space manually."
    if category == "downloader":
        return await _fix_ytdlp(tools_dir)
    if category == "timeout":
        return "Timeout error. The operation may need more time or the resource may be unavailable."
    return None


async def _fix_ytdlp(tools_dir: Path) -> str | None:
    ytdlp_path = tools_dir / "yt-dlp"
    if not ytdlp_path.is_file():
        return "yt-dlp not found in tools directory."
    try:
        proc = await asyncio.create_subprocess_exec(
            str(ytdlp_path), "--update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode == 0:
            LOGGER.info("yt-dlp updated successfully")
            return None
        LOGGER.warning("yt-dlp update failed: %s", stderr.decode()[:200])
        return f"yt-dlp update failed: {stderr.decode()[:200]}"
    except (OSError, asyncio.TimeoutError) as exc:
        return f"yt-dlp update error: {exc}"


async def _fix_dependency(error_message: str) -> str | None:
    import re
    match = re.search(r"no module named\s+'?(\w+(?:[.-]\w+)*)'?", error_message, re.IGNORECASE)
    if not match:
        match = re.search(r"import\s+(\w+)", error_message)
    if match:
        module = match.group(1).lower()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", module,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                LOGGER.info("Installed dependency: %s", module)
                return None
            return f"Failed to install {module}: {stderr.decode()[:200]}"
        except (OSError, asyncio.TimeoutError) as exc:
            return f"Dependency install error: {exc}"
    return None


async def invoke_opencode_fix(error_info: dict, workspace: Path) -> str | None:
    error_id = error_info.get("id", int(time.time()))
    error_message = error_info.get("message", "")
    traceback = error_info.get("traceback", "")
    category = error_info.get("category", "unknown")

    prompt = (
        f"The media-downloader Telegram bot at {workspace} encountered an error.\n\n"
        f"Error category: {category}\n"
        f"Error message: {error_message}\n\n"
        f"Traceback:\n{traceback[:2000]}\n\n"
        f"Analyze the error and relevant code, then apply a fix. "
        f"Run the tests with `python3 -m unittest discover tests/ -v` after applying the fix "
        f"to verify it works."
    )

    fix_script = FIX_SCRIPTS_DIR / f"fix_{error_id}.sh"
    FIX_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    fix_script.write_text(
        "#!/usr/bin/env bash\n"
        f"set -e\n"
        f"cd {shlex_quote(str(workspace))}\n"
        f"opencode --prompt {shlex_quote(prompt)}\n",
    )
    fix_script.chmod(0o755)
    return str(fix_script)


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


async def watch_and_fix(
    workspace: Path,
    tools_dir: Path,
    report_callback: callable | None = None,
) -> None:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    FIX_SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    processed: set[str] = set()

    LOGGER.info("Auto-fix agent watching %s for errors...", ERRORS_DIR)

    while True:
        try:
            error_files = sorted(ERRORS_DIR.glob("*.json"))
            for ef in error_files:
                if ef.name in processed:
                    continue
                if ef.name.startswith(("fixed_", "failed_", "unfixed_")):
                    continue
                error_info = load_error_log(ef)
                if error_info is None:
                    processed.add(ef.name)
                    continue

                LOGGER.info("Processing error: %s", ef.name)
                category = categorize_error(error_info.get("message", ""))
                error_info["category"] = category

                fix_result = await apply_known_fix(error_info, tools_dir)
                if fix_result is None:
                    LOGGER.info("Known fix applied successfully for %s", ef.name)
                    if report_callback:
                        await report_callback(
                            f"✅ Auto-fixed error [{category}]: {error_info.get('message', '')[:100]}"
                        )
                    ef.replace(ERRORS_DIR / f"fixed_{ef.name}")
                elif category == "unknown":
                    LOGGER.info("Unknown error, invoking opencode for %s", ef.name)
                    if report_callback:
                        await report_callback(
                            f"🤖 Unknown error [{category}]: {error_info.get('message', '')[:100]}\n"
                            f"Creating fix script..."
                        )
                    fix_script_path = await invoke_opencode_fix(error_info, workspace)
                    if fix_script_path:
                        if report_callback:
                            await report_callback(
                                f"Fix script created: {fix_script_path}\n"
                                f"Run it manually to attempt a fix."
                            )
                    ef.replace(ERRORS_DIR / f"unfixed_{ef.name}")
                else:
                    LOGGER.warning("Known fix failed for %s: %s", ef.name, fix_result)
                    if report_callback:
                        await report_callback(
                            f"⚠️ Fix failed for [{category}]: {fix_result}"
                        )
                    ef.replace(ERRORS_DIR / f"failed_{ef.name}")

                processed.add(ef.name)

        except Exception as exc:
            LOGGER.error("Auto-fix agent error: %s", exc)

        await asyncio.sleep(10)
