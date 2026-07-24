from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Load a minimal local .env without adding a runtime dependency."""
    env_file = Path(".env")
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _id_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    try:
        return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must contain comma-separated numeric IDs") from exc


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    tools_dir: Path
    ytdlp_version: str | None
    max_filesize_mb: int
    timeout_seconds: int

    @classmethod
    def from_environment(cls) -> "Settings":
        _load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        users = _id_set("TELEGRAM_ALLOWED_USER_IDS")
        chats = _id_set("TELEGRAM_ALLOWED_CHAT_IDS")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if not (users or chats):
            raise ValueError("configure at least one allowed user or chat ID")
        max_size = int(os.getenv("MEDIA_BOT_MAX_FILESIZE_MB", "45"))
        timeout = int(os.getenv("MEDIA_BOT_DOWNLOAD_TIMEOUT_SECONDS", "900"))
        if max_size < 1 or timeout < 1:
            raise ValueError("download size and timeout must be positive")
        return cls(
            token=token,
            allowed_user_ids=users,
            allowed_chat_ids=chats,
            tools_dir=Path(os.getenv("MEDIA_BOT_TOOLS_DIR", "~/.local/share/media-downloader/tools")).expanduser(),
            ytdlp_version=os.getenv("YTDLP_VERSION", "").strip() or None,
            max_filesize_mb=max_size,
            timeout_seconds=timeout,
        )
