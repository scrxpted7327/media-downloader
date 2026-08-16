from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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


def _bool_value(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


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
    download_public_origin: str | None = None
    download_bind_host: str = "127.0.0.1"
    download_port: int = 8080
    media_api_bind_host: str = "127.0.0.1"
    media_api_port: int = 8082
    media_api_key: str | None = field(default=None, repr=False)
    internal_signing_secret: str | None = field(default=None, repr=False)
    acting_context_max_age_seconds: int = 60
    acting_context_clock_skew_seconds: int = 5
    storage_dir: Path = field(default_factory=lambda: Path("runtime/jobs"))
    db_path: Path = field(default_factory=lambda: Path("runtime/jobs/media-bot.db"))
    token_expiry_minutes: int = 15
    retention_days: int = 7
    mass_download_max_mb: int = 2048
    allow_mass_download_all: bool = False
    download_workers: int = 2
    render_workers: int = 1
    work_queue_capacity: int = 32
    per_user_work_capacity: int = 4
    admin_user_ids: frozenset[int] = field(default_factory=frozenset)
    repair_enabled: bool = False
    metadata_model: str = "gpt-5.6-luna"
    metadata_reasoning_effort: str = "max"
    metadata_codex_executable: str = "codex"
    metadata_workers: int = 1
    metadata_timeout_seconds: int = 1800
    metadata_codex_home: Path | None = None

    @property
    def auto_hashtags_model(self) -> str:
        """Compatibility name for callers that refer to the feature directly."""
        return self.metadata_model

    @property
    def codex_model(self) -> str:
        return self.metadata_model

    @property
    def auto_hashtags_reasoning_effort(self) -> str:
        return self.metadata_reasoning_effort

    @property
    def codex_reasoning_effort(self) -> str:
        return self.metadata_reasoning_effort

    @property
    def auto_hashtags_codex_executable(self) -> str:
        return self.metadata_codex_executable

    @property
    def codex_executable(self) -> str:
        return self.metadata_codex_executable

    @property
    def auto_hashtags_timeout_seconds(self) -> int:
        return self.metadata_timeout_seconds

    @property
    def auto_hashtags_codex_home(self) -> Path | None:
        return self.metadata_codex_home

    @classmethod
    def from_environment(cls) -> "Settings":
        _load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        users = _id_set("TELEGRAM_ALLOWED_USER_IDS")
        configured_admins = _id_set("TELEGRAM_ADMIN_USER_IDS")
        admins = configured_admins or users
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
        download_public_origin = os.getenv("MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN", "").strip() or None
        if download_public_origin is None and download_domain:
            download_public_origin = f"https://{download_domain}"
        if download_public_origin:
            parts = urlsplit(download_public_origin)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in {"", "/"}
                or parts.query
                or parts.fragment
            ):
                raise ValueError(
                    "MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN must be an HTTPS origin "
                    "without credentials, query, or fragment"
                )
            download_public_origin = download_public_origin.rstrip("/")
        download_bind_host = os.getenv("MEDIA_BOT_DOWNLOAD_BIND_HOST", "127.0.0.1").strip()
        if not download_bind_host:
            raise ValueError("MEDIA_BOT_DOWNLOAD_BIND_HOST cannot be empty")
        download_port = int(os.getenv("MEDIA_BOT_DOWNLOAD_PORT") or "8080")
        media_api_bind_host = os.getenv("MEDIA_BOT_API_BIND_HOST", "127.0.0.1").strip()
        media_api_port = int(os.getenv("MEDIA_BOT_API_PORT") or "8082")
        media_api_key = os.getenv("MEDIA_BOT_API_KEY", "").strip() or None
        internal_signing_secret = (
            os.getenv("WATCHMYWALLET_INTERNAL_SIGNING_SECRET", "").strip() or None
        )
        acting_context_max_age_seconds = int(
            os.getenv("WATCHMYWALLET_ACTING_CONTEXT_MAX_AGE_SECONDS") or "60"
        )
        acting_context_clock_skew_seconds = int(
            os.getenv("WATCHMYWALLET_ACTING_CONTEXT_CLOCK_SKEW_SECONDS") or "5"
        )
        storage_dir = Path(os.getenv("MEDIA_BOT_STORAGE_DIR") or "runtime/jobs").expanduser()
        db_path = Path(os.getenv("MEDIA_BOT_DB_PATH") or "runtime/jobs/media-bot.db").expanduser()
        token_expiry = int(os.getenv("MEDIA_BOT_TOKEN_EXPIRY_MINUTES") or "15")
        retention_days = int(os.getenv("MEDIA_BOT_RETENTION_DAYS") or "7")
        mass_download_max_mb = int(os.getenv("MEDIA_BOT_MASS_DOWNLOAD_MAX_MB") or "2048")
        allow_mass_download_all = _bool_value("MEDIA_BOT_ALLOW_MASS_DOWNLOAD_ALL")
        repair_enabled = _bool_value("MEDIA_BOT_ENABLE_REPAIR")
        download_workers = int(os.getenv("MEDIA_BOT_DOWNLOAD_WORKERS") or "2")
        render_workers = int(os.getenv("MEDIA_BOT_RENDER_WORKERS") or "1")
        work_queue_capacity = int(os.getenv("MEDIA_BOT_WORK_QUEUE_CAPACITY") or "32")
        per_user_work_capacity = int(os.getenv("MEDIA_BOT_PER_USER_WORK_CAPACITY") or "4")
        metadata_model = (
            os.getenv("MEDIA_BOT_AUTO_HASHTAGS_CODEX_MODEL", "").strip()
            or os.getenv("MEDIA_BOT_AUTO_HASHTAGS_MODEL", "").strip()
            or os.getenv("MEDIA_BOT_METADATA_MODEL", "").strip()
            or "gpt-5.6-luna"
        )
        metadata_reasoning_effort = (
            os.getenv("MEDIA_BOT_AUTO_HASHTAGS_CODEX_REASONING_EFFORT", "").strip()
            or os.getenv("MEDIA_BOT_AUTO_HASHTAGS_REASONING_EFFORT", "").strip()
            or os.getenv("MEDIA_BOT_METADATA_REASONING_EFFORT", "").strip()
            or "max"
        )
        metadata_codex_executable = (
            os.getenv("MEDIA_BOT_AUTO_HASHTAGS_CODEX_EXECUTABLE", "").strip()
            or os.getenv("MEDIA_BOT_CODEX_EXECUTABLE", "").strip()
            or os.getenv("MEDIA_BOT_METADATA_CODEX_EXECUTABLE", "").strip()
            or "codex"
        )
        metadata_workers = int(
            os.getenv("MEDIA_BOT_AUTO_HASHTAGS_WORKERS", "")
            or os.getenv("MEDIA_BOT_AUTO_HASHTAGS_METADATA_WORKERS", "")
            or os.getenv("MEDIA_BOT_METADATA_WORKERS", "")
            or "1"
        )
        metadata_timeout_seconds = int(
            os.getenv("MEDIA_BOT_AUTO_HASHTAGS_CODEX_TIMEOUT_SECONDS", "")
            or os.getenv("MEDIA_BOT_AUTO_HASHTAGS_TIMEOUT_SECONDS", "")
            or os.getenv("MEDIA_BOT_METADATA_TIMEOUT_SECONDS", "")
            or "1800"
        )
        metadata_codex_home_raw = os.getenv(
            "MEDIA_BOT_AUTO_HASHTAGS_CODEX_HOME", ""
        ).strip()
        metadata_codex_home = (
            Path(metadata_codex_home_raw).expanduser()
            if metadata_codex_home_raw else None
        )
        if max_size < 1 or timeout < 1 or upload_timeout < 1:
            raise ValueError("download size and timeouts must be positive")
        if not media_api_bind_host:
            raise ValueError("MEDIA_BOT_API_BIND_HOST cannot be empty")
        if not (1 <= download_port <= 65535) or not (1 <= media_api_port <= 65535):
            raise ValueError("download and media API ports must be valid TCP ports")
        if download_bind_host == media_api_bind_host and download_port == media_api_port:
            raise ValueError("download and media API endpoints must not use the same address")
        if acting_context_max_age_seconds < 1 or acting_context_max_age_seconds > 300:
            raise ValueError("acting context max age must be between 1 and 300 seconds")
        if acting_context_clock_skew_seconds < 0 or acting_context_clock_skew_seconds > 60:
            raise ValueError("acting context clock skew must be between 0 and 60 seconds")
        if not (1 <= token_expiry <= 1440):
            raise ValueError("token expiry must be between 1 and 1440 minutes")
        if min(
            retention_days,
            mass_download_max_mb,
            download_workers,
            render_workers,
            metadata_workers,
            work_queue_capacity,
            per_user_work_capacity,
        ) < 1:
            raise ValueError("retention, size, worker, and queue limits must be positive")
        if not metadata_model or not metadata_reasoning_effort or not metadata_codex_executable:
            raise ValueError("metadata model, reasoning effort, and Codex executable cannot be empty")
        if not (1 <= metadata_timeout_seconds <= 1800):
            raise ValueError("metadata timeout must be between 1 and 1800 seconds")
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
            download_public_origin=download_public_origin,
            download_bind_host=download_bind_host,
            download_port=download_port,
            media_api_bind_host=media_api_bind_host,
            media_api_port=media_api_port,
            media_api_key=media_api_key,
            internal_signing_secret=internal_signing_secret,
            acting_context_max_age_seconds=acting_context_max_age_seconds,
            acting_context_clock_skew_seconds=acting_context_clock_skew_seconds,
            storage_dir=storage_dir,
            db_path=db_path,
            token_expiry_minutes=token_expiry,
            retention_days=retention_days,
            mass_download_max_mb=mass_download_max_mb,
            allow_mass_download_all=allow_mass_download_all,
            download_workers=download_workers,
            render_workers=render_workers,
            work_queue_capacity=work_queue_capacity,
            per_user_work_capacity=per_user_work_capacity,
            admin_user_ids=admins,
            repair_enabled=repair_enabled,
            metadata_model=metadata_model,
            metadata_reasoning_effort=metadata_reasoning_effort,
            metadata_codex_executable=metadata_codex_executable,
            metadata_workers=metadata_workers,
            metadata_timeout_seconds=metadata_timeout_seconds,
            metadata_codex_home=metadata_codex_home,
        )
