from __future__ import annotations

import re
from urllib.parse import urlparse

_DOMAINS = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch",
)
_URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,!?;:)"


def is_supported_url(value: str) -> bool:
    """Accept HTTPS links to exact supported domains or their subdomains."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in _DOMAINS)


def extract_supported_urls(text: str) -> list[str]:
    """Return supported HTTP(S) links embedded anywhere in a message."""
    return [
        url for match in _URL_PATTERN.finditer(text)
        if is_supported_url(url := match.group(0).rstrip(_TRAILING_PUNCTUATION))
    ]


def is_instagram_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return host == "instagram.com" or host.endswith(".instagram.com")


def is_tiktok_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and "/photo/" in parsed.path.lower()


def is_tiktok_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return host == "tiktok.com" or host.endswith(".tiktok.com")
