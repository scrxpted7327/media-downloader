from __future__ import annotations

from urllib.parse import urlparse

_DOMAINS = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch",
)


def is_supported_url(value: str) -> bool:
    """Accept HTTPS links to exact supported domains or their subdomains."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in _DOMAINS)
