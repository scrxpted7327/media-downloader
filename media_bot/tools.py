from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import urllib.request
from pathlib import Path

GITHUB_RELEASE = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/"
USER_AGENT = "media-downloader-bot/1.0"


def _asset_name() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin":
        return "yt-dlp_macos" if machine in {"arm64", "aarch64"} else "yt-dlp_macos_legacy"
    if system == "Linux":
        return "yt-dlp_linux_aarch64" if machine in {"aarch64", "arm64"} else "yt-dlp_linux"
    if system == "Windows":
        return "yt-dlp.exe"
    raise RuntimeError(f"unsupported operating system for automatic yt-dlp installation: {system}")


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310: official fixed endpoint
        return json.load(response)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:  # nosec B310
        shutil.copyfileobj(response, output)


def provision_ytdlp(tools_dir: Path, version: str | None = None) -> Path:
    """Fetch a verified official yt-dlp release into the user's tools directory."""
    asset_name = _asset_name()
    target = tools_dir / asset_name
    metadata = tools_dir / "yt-dlp-version"
    release_url = GITHUB_RELEASE + (f"tags/{version}" if version else "latest")
    release = _get_json(release_url)
    tag = release["tag_name"]
    if target.is_file() and metadata.is_file() and metadata.read_text().strip() == tag:
        return target

    assets = {item["name"]: item["browser_download_url"] for item in release["assets"]}
    if asset_name not in assets or "SHA2-256SUMS" not in assets:
        raise RuntimeError("official yt-dlp release did not provide the required executable/checksum")
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tools_dir) as temporary:
        temp_dir = Path(temporary)
        binary = temp_dir / asset_name
        sums = temp_dir / "SHA2-256SUMS"
        _download(assets[asset_name], binary)
        _download(assets["SHA2-256SUMS"], sums)
        expected = next((line.split()[0] for line in sums.read_text().splitlines() if line.split()[-1].lstrip("*") == asset_name), None)
        actual = hashlib.sha256(binary.read_bytes()).hexdigest()
        if not expected or actual.lower() != expected.lower():
            raise RuntimeError("yt-dlp checksum verification failed; executable was not installed")
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(binary, target)
        metadata.write_text(tag + "\n", encoding="utf-8")
    return target
