# Media downloader Telegram bot

This backend accepts media links from YouTube (including Shorts), Instagram,
TikTok, and Facebook, downloads them using `yt-dlp`, and replies with the media.
It is deliberately closed by default: only Telegram users and chats/channels
explicitly listed in its configuration can trigger a download.

TikTok photo-post links (`/photo/`) are rendered as an MP4 slideshow with the
post's downloadable music. That path uses `gallery-dl` to fetch the slides and
audio, then `ffmpeg` to render the video.

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

`ffmpeg` is optional for ordinary downloads, but required for TikTok photo
posts and for merging some separate audio/video streams. Install it using your
OS package manager (`brew install ffmpeg`, `apt install ffmpeg`, etc.). The bot
detects it and tells the operator when it is missing; it does not silently
package or execute an unverified ffmpeg build.

Automatic watermark removal samples the middle 90% of a video for persistent
logos or text. Confident regions render immediately; uncertain regions are
shown as numbered Telegram buttons and remain reviewable after a bot restart.
The pinned Apache-2.0 LaMa ONNX model is downloaded lazily to
`MEDIA_BOT_TOOLS_DIR` and SHA-256 verified. If ONNX inference or provisioning
fails, the render completes with adaptive FFmpeg `delogo` regions and reports
the fallback. Presets can Keep, Remove, or Swap detected watermarks; Swap
removes selected regions and centers the preset's replacement username/text
inside each region. Choosing a named watermark position remains a manual
override.

### Optional Ryzen Whisper worker

To offload automatic caption transcription to a Ryzen machine, add these to
`.env`:

```sh
WHISPER_SSH_HOST=user@ryzen-ip-or-hostname
WHISPER_SSH_KEY=/path/to/ssh-key
```

`WHISPER_SSH_KEY` is optional and defaults to `~/.ssh/id_rsa`. On the Ryzen,
install `faster-whisper` and configure key-based SSH access so
`ssh user@ryzen date` completes without a password prompt:

```sh
pip install faster-whisper
```

When configured, the bot uses `ssh` in batch mode, streams the temporary WAV
to standard input, and runs the transcription inline. No Whisper server or
additional open port is required; the remote temporary WAV is deleted when the
command finishes.

## Telegram setup

Disable BotFather privacy mode if the bot must receive ordinary group messages.
For a channel, add the bot as an administrator so Telegram delivers
`channel_post` updates. Put that channel's numeric chat ID (normally starting
with `-100`) in `TELEGRAM_ALLOWED_CHAT_IDS`.

The bot handles only HTTPS URLs whose hostname belongs to an allowlist.
Arbitrary downloader arguments and arbitrary local paths are never accepted.
Completed files are persisted under `MEDIA_BOT_STORAGE_DIR`, associated with
the requesting Telegram user, and removed by the configured retention cleanup.
Downloads and renders use bounded worker queues with global and per-user
capacity limits. Work interrupted by a restart is marked failed explicitly
rather than remaining stuck in an active state.

The default download timeout is one hour, and the default Telegram upload write
timeout is 15 minutes. The default 47 MiB source limit leaves headroom below
Telegram's 50 MB public Bot API upload limit. A local Bot API server is required
for larger Telegram uploads.

### Direct downloads

Direct downloads are disabled unless
`MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN` is configured with an HTTPS origin. The
embedded server binds to `127.0.0.1` by default; expose it through a TLS reverse
proxy or private tunnel and set the public origin to that externally reachable
address. For example:

```sh
MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN=https://media.example.com
MEDIA_BOT_DOWNLOAD_BIND_HOST=127.0.0.1
MEDIA_BOT_DOWNLOAD_PORT=8080
```

Download links use random, expiring, one-time tokens. `HEAD`, missing files,
paths outside storage, and symlink escapes do not consume a token. A successful
`GET` consumes it atomically. Direct links therefore require an HTTPS proxy;
do not bind the server publicly without an equivalent network boundary.

Interactive settings, pool, and edit prompts are bound to the Telegram chat
where they were opened, expire after 15 minutes, and can be cleared with
`/cancel`. Authorized messages are audited structurally without storing their
raw text. Diagnostic reports redact common credentials and URL query strings
and include only the reporting user's events plus explicitly marked global
health events.

Only download media you are authorized to access and use, and comply with the
source platform's terms and applicable law.

## Run as a service

Run it under a service manager with a dedicated unprivileged user, a private
data directory, and the environment variables above. Do not expose its token
or the tools directory through a web server.

The repository's `restart_bot.py` coordinates with `supervisor.py` before
restarting the stack. Deployment and live Telegram acceptance testing are
separate operator actions; local unit tests do not prove the Inspiron service is
ready.
