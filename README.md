# Media downloader Telegram bot

This backend accepts media links from YouTube (including Shorts), Instagram,
TikTok, and Facebook, downloads them using `yt-dlp`, and replies with the media.
It is deliberately closed by default: only Telegram users and chats/channels
explicitly listed in its configuration can trigger a download.

TikTok photo-post links (`/photo/`) are rendered as an MP4 slideshow with the
post's downloadable music. That path uses `gallery-dl` to fetch the slides and
audio, then `ffmpeg` to render the video.

## Setup

1. Use Python 3.11+ and install the application dependencies:

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

When no verified cache exists, the bot downloads the platform-appropriate `yt-dlp` executable
to `MEDIA_BOT_TOOLS_DIR` (by default a per-user application-data directory).
No downloader binary is bundled with this project. The release checksum is
downloaded from the same official yt-dlp release and verified before use.
Later restarts reuse that verified binary, including while offline. Set
`YTDLP_VERSION` when an operator wants startup to require a particular release.

`ffmpeg` is optional for ordinary downloads, but required for TikTok photo
posts and for merging some separate audio/video streams. Install it using your
OS package manager (`brew install ffmpeg`, `apt install ffmpeg`, etc.). The bot
detects it and tells the operator when it is missing; it does not silently
package or execute an unverified ffmpeg build.

Watermark removal is opt-in and new presets keep the original video by default.
When enabled, automatic detection samples the middle 90% of a video for persistent
logos or text. Confident regions render immediately; uncertain regions are
uploaded as a swipeable Telegram album with one full-size preview per candidate,
then remain reviewable after a bot restart.
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

### Swearify voice-over

Voice settings include an opt-in `Swearify (AI roast)` mode. It samples the
source clip's frames and transcript, asks the configured local Codex CLI for a
short evidence-bound comedic roast, replaces the audio with TTS, and burns
captions generated from that replacement audio. The mode permits ordinary
profanity for entertainment but instructs the generator not to use slurs,
threats, doxxing, protected-trait attacks, or unsupported claims. It uses the
same `MEDIA_BOT_AUTO_HASHTAGS_CODEX_*` settings as metadata generation and
fails the render with an actionable error when Codex is unavailable.

Voice settings also include an optional `Like & Subscribe` end plug. It uses
the selected TTS voice to append a short call-to-action after the original
audio, holds the final video frame while it plays, and includes the plug in
automatic captions when Auto Captions is enabled.

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

Use `/queue` to see your queued/running job IDs and
`/canceljob download:<id>`, `/canceljob render:<id>`, or
`/canceljob metadata:<edit_id>` to request cancellation.

After a rendered video is delivered, the bot queues evidence-bound description
and hashtag generation in the authenticated local Codex CLI. It transcribes
the original source audio and samples eight frames from the original source,
so watermarks, banners, and other edit overlays do not become the description's
subject. The result is delivered as a separate Telegram message. The default
runtime settings are `gpt-5.6-luna`, `max` reasoning, one metadata worker, and a
1,800-second subprocess limit. Set `MEDIA_BOT_AUTO_HASHTAGS_CODEX_HOME` when
Codex authentication is stored in a non-default home. A missing or unavailable
Codex installation never fails the rendered video delivery.
Auto Hashtags is currently automatic after each successful render rather than a
separate per-render toggle.

Before rendering, the bot edits its preparation message to show the source,
preset, resolved watermark/caption/voice/banner settings, planned stages, and
the later Auto Hashtags step. Long watermark, Swearify, metadata, and repair
analysis stages refresh their progress messages periodically. Reply `/fix` to an authorized bot message to ask
for a durable job and supervisor-state check; it reports whether the work is
still running, queued, waiting for review, halted, or crashed. A standalone
`/fix` remains the admin-only repair scan.
Pool saves are durable copies: source retention, Reset, and `/delete` do not
invalidate media explicitly saved in the Pool.
Shared preset codes can be imported from Settings; another user's private
banner asset is intentionally omitted from the imported copy.

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

`TELEGRAM_ALLOWED_USER_IDS` is also the default operator/admin allowlist. Set
`TELEGRAM_ADMIN_USER_IDS` only when a narrower admin set is needed. `/report`
always writes a diagnostic ticket and never executes code. Repair execution is
admin-only and disabled unless `MEDIA_BOT_ENABLE_REPAIR=true`; inferred Python
packages are never installed automatically.

Only download media you are authorized to access and use, and comply with the
source platform's terms and applicable law.

## Run as a service

Run it under a service manager with a dedicated unprivileged user, a private
data directory, and the environment variables above. Do not expose its token
or the tools directory through a web server.

The repository's `restart_bot.py` coordinates with `supervisor.py` before
restarting the stack and runs `pip install -r requirements.txt` through the
project virtualenv before stopping the existing processes. If dependency
installation fails, the current stack is left running. Deployment and live
Telegram acceptance testing are separate operator actions; local unit tests do
not prove the Inspiron service is ready.

The embedded loopback HTTP service exposes `/healthz` for a local service
manager or reverse proxy. Diagnostic event and supervisor logs rotate with
bounded backups, and download-token request paths are excluded from access
logging.
