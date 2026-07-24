# Media downloader Telegram bot

This backend accepts media links from YouTube (including Shorts), Instagram,
TikTok, and Facebook, downloads them using `yt-dlp`, and replies with the media.
It is deliberately closed by default: only Telegram users and chats/channels
explicitly listed in its configuration can trigger a download.

## Setup

1. Use Python 3.10+ and install the application dependency:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`, set `TELEGRAM_BOT_TOKEN`, and replace the
   example IDs with real numeric Telegram IDs. Do not put a token in source
   control.

3. Start the bot:

   ```sh
   python3 -m media_bot
   ```

On first start the bot downloads the platform-appropriate `yt-dlp` executable
to `MEDIA_BOT_TOOLS_DIR` (by default a per-user application-data directory).
No downloader binary is bundled with this project. The release checksum is
downloaded from the same official yt-dlp release and verified before use.

`ffmpeg` is optional: yt-dlp can download many formats without it, but it is
needed to merge some separate audio/video streams. Install it using your OS
package manager when higher-quality merged downloads are needed (`brew install
ffmpeg`, `apt install ffmpeg`, etc.). The bot detects it and tells the operator
when it is missing; it does not silently package or execute an unverified
ffmpeg build.

## Telegram setup

Disable BotFather privacy mode if the bot must receive ordinary group messages.
For a channel, add the bot as an administrator so Telegram delivers
`channel_post` updates. Put that channel's numeric chat ID (normally starting
with `-100`) in `TELEGRAM_ALLOWED_CHAT_IDS`.

The bot handles only URLs whose hostname belongs to an allowlist. Redirects,
arbitrary downloader arguments, and arbitrary local paths are never accepted.
Downloaded files live in a unique temporary directory and are deleted after the
Telegram upload attempt. The configured size limit is passed to yt-dlp before
download; Telegram's own upload limits also apply.

Only download media you are authorized to access and use, and comply with the
source platform's terms and applicable law.

## Run as a service

Run it under a service manager with a dedicated unprivileged user, a private
data directory, and the environment variables above. Do not expose its token
or the tools directory through a web server.
