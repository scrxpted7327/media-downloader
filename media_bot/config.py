from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    upload_timeout_seconds: int
    local_api_url: str | None = None
    download_domain: str | None = None
    download_port: int = 8080
    storage_dir: Path = field(default_factory=lambda: Path("runtime/jobs"))
    db_path: Path = field(default_factory=lambda: Path("runtime/jobs/media-bot.db"))
    token_expiry_minutes: int = 15
    retention_days: int = 7

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
        max_size = int(os.getenv("MEDIA_BOT_MAX_FILESIZE_MB") or "47")
        timeout = int(os.getenv("MEDIA_BOT_DOWNLOAD_TIMEOUT_SECONDS") or "3600")
        upload_timeout = int(os.getenv("MEDIA_BOT_UPLOAD_TIMEOUT_SECONDS") or "900")
        local_api_url = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip() or None
        download_domain = os.getenv("MEDIA_BOT_DOWNLOAD_DOMAIN", "").strip() or None
        download_port = int(os.getenv("MEDIA_BOT_DOWNLOAD_PORT") or "8080")
        storage_dir = Path(os.getenv("MEDIA_BOT_STORAGE_DIR") or "runtime/jobs").expanduser()
        db_path = Path(os.getenv("MEDIA_BOT_DB_PATH") or "runtime/jobs/media-bot.db").expanduser()
        token_expiry = int(os.getenv("MEDIA_BOT_TOKEN_EXPIRY_MINUTES") or "15")
        retention_days = int(os.getenv("MEDIA_BOT_RETENTION_DAYS") or "7")
        if max_size < 1 or timeout < 1 or upload_timeout < 1:
            raise ValueError("download size and timeouts must be positive")
        if not (1 <= download_port <= 65535):
            raise ValueError("download port must be a valid TCP port")
        if not (1 <= token_expiry <= 1440):
            raise ValueError("token expiry must be between 1 and 1440 minutes")
        if retention_days < 1:
            raise ValueError("retention days must be positive")
        return cls(
            token=token,
            allowed_user_ids=users,
            allowed_chat_ids=chats,
            tools_dir=Path(os.getenv("MEDIA_BOT_TOOLS_DIR") or "~/.local/share/media-downloader/tools").expanduser(),
            ytdlp_version=os.getenv("YTDLP_VERSION", "").strip() or None,
            max_filesize_mb=max_size,
            timeout_seconds=timeout,
            upload_timeout_seconds=upload_timeout,
            local_api_url=local_api_url,
            download_domain=download_domain,
            download_port=download_port,
            storage_dir=storage_dir,
            db_path=db_path,
            token_expiry_minutes=token_expiry,
            retention_days=retention_days,
        )
