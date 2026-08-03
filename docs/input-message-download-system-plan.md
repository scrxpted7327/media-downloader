# Input, Message, Download, and Delivery System

## Engineering review, target design, and remediation plan

**Repository:** `media-downloader`
**Document status:** Implementation handoff
**Snapshot date:** 2026-08-01
**Review basis:** Live working tree, including uncommitted changes
**Primary host:** Inspiron bot server
**Primary process entrypoints:** `python -m media_bot`, `restart_bot.py`, and `supervisor.py`

This document is the source of truth for understanding and improving the bot's
input, message, download, render, delivery, cleanup, and supervision flow. It is
written for both humans and coding agents.

It records:

- how the current system works;
- which behaviors are confirmed defects and which are design risks;
- the security and reliability invariants the finished system must enforce;
- concrete implementation guidance;
- an ordered remediation plan with tests and acceptance gates;
- deployment and rollback guidance for the Inspiron; and
- a prioritized inventory of work still to be done.

This is a planning document. It does not claim that the fixes described below
have been implemented.

---

## 1. Executive summary

The basic happy-path design is understandable and functional:

1. Telegram polling receives an update.
2. A command, callback, or non-command message handler claims it.
3. The bot checks the configured Telegram user/chat allowlist.
4. A supported URL is extracted from message text or a caption.
5. A SQLite job is created.
6. `yt-dlp`, `gallery-dl`, and sometimes FFmpeg download or transform media.
7. The result is moved into persistent job storage and recorded in SQLite.
8. The bot publishes an action keyboard.
9. A callback creates an edit, render, pool action, or direct-download token.
10. The embedded `aiohttp` server serves a file through a time-limited link.
11. Scheduled cleanup removes expired links, tokens, and retained jobs.
12. `supervisor.py` restarts the bot after exits and reports crashes.

The current system is not yet safe for general multi-user group operation. The
highest-priority problems are:

1. callback resource ownership is not consistently enforced;
2. one-time tokens can be consumed successfully more than once under
   concurrency;
3. downloads and renders are not managed by bounded worker queues;
4. interactive input state can cross chat boundaries and race;
5. subprocess cancellation, output buffering, and final size enforcement are
   incomplete;
6. unauthorised update text and global diagnostics can enter user/AI reports;
7. the download server has unsafe or unusable default exposure behavior; and
8. setup and operational documentation has drifted from the implementation.

The first deployment gate should be **correct authorization and atomic token
consumption**, not new downloader or editor features.

---

## 2. Scope and non-goals

### In scope

- Telegram command, callback, text, caption, photo, and document routing.
- Allowlist authorization and per-resource ownership.
- Active settings, edit, and pool input flows.
- Supported URL extraction and platform selection.
- Downloader subprocess execution and progress messages.
- Temporary and persistent file handling.
- Job, edit, pool, token, and download-message persistence.
- Original-download and rendered-edit delivery.
- Embedded download server behavior.
- Cleanup, retention, restart, and crash supervision.
- Diagnostics and AI repair/report handoff privacy.
- Tests, deployment checks, rollback, and future-agent instructions.

### Out of scope

- Adding new media platforms.
- Reworking video editing algorithms unless required for queueing, ownership, or
  cancellation correctness.
- Provisioning credentials, accepting external service terms, or changing
  billing.
- Deploying to or restarting the Inspiron without explicit approval.
- Committing, pushing, or publishing changes.
- Treating the public source URL as proof that downloading the media is legally
  permitted.

---

## 3. Review baseline and evidence rules

### Current verification baseline

The repository-local environment passed:

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Result at the initial review snapshot:

```text
Ran 94 tests
OK
```

While this document was being authored, a separate concurrent workspace process
added an uncommitted TikTok account-archive path and additional settings tests.
On the final documentation verification pass, the same command passed 99 tests.
The increase from 94 to 99 reflects concurrent worktree tests; it is not
evidence of live TikTok, Telegram, or Inspiron success.

The system `python3` environment does not contain all project dependencies and
cannot run the complete suite. Use `.venv/bin/python` for the current local
baseline. Do not report a failing system-Python import as a product regression.

No live Telegram exchange, public download-domain request, or Inspiron runtime
was exercised during this review.

### Working-tree warning

At the initial snapshot, the working tree already contained uncommitted changes
in:

- `media_bot/__main__.py`
- `media_bot/editor.py`
- `media_bot/settings_ui.py`
- `tests/test_download_actions.py`
- `tests/test_editor.py`

During documentation work, additional uncommitted changes appeared in:

- `media_bot/config.py`
- `media_bot/downloader.py`
- `media_bot/platforms.py`
- `tests/test_downloader.py`
- `tests/test_platforms.py`

Those additions implement a `/tiktokaccount` archive path and were not authored
or accepted by this documentation task. Treat them as concurrent user/workspace
work: inspect and preserve them, but do not assume they are complete merely
because their focused tests pass.

Before implementing any plan item, re-run:

```sh
git status --short
git diff --stat
git diff --check
```

Never discard, overwrite, or fold unrelated user changes into a fix.

### Defect classification

- **Confirmed defect:** the current code directly violates a stated contract, or
  the failure has been reproduced.
- **Design risk:** the current structure permits a realistic failure, but the
  complete failure has not been reproduced in the target deployment.
- **Documentation drift:** documentation and implementation disagree.

Every future finding should include a trigger, impact, code evidence, test, and
acceptance condition.

---

## 4. Current architecture

### 4.1 Component map

| Component | Primary responsibility | Important entrypoints |
|---|---|---|
| `media_bot/__main__.py` | Application construction, Telegram handlers, download orchestration, edit/render delivery, scheduled cleanup | `main`, `_message_router`, `handle_url`, `_process_single_url`, `download_callback`, `_render_edit_job`, `_post_init` |
| `media_bot/config.py` | Load `.env`, validate runtime configuration | `Settings.from_environment`, `_authorized` consumes the resulting sets |
| `media_bot/platforms.py` | Extract supported URLs and classify Instagram/TikTok URLs | `extract_supported_urls`, `is_supported_url`, `is_instagram_url`, `is_tiktok_url` |
| `media_bot/downloader.py` | Run downloader/FFmpeg subprocesses, select results, generate thumbnails, persist files, and package TikTok account archives in the current worktree | `download_media`, `download_instagram`, `download_tiktok_slideshow`, `download_tiktok_account`, `_run_checked`, `persist_download` |
| `media_bot/tools.py` | Provision and checksum-verify external `yt-dlp`; select FFmpeg binaries | `provision_ytdlp`, `prefer_ffmpeg_full` |
| `media_bot/storage.py` | SQLite schema and CRUD for jobs, tokens, presets, edits, pool, workflows, and cleanup records | `init_db`, `create_job`, `create_download_token`, `consume_download_token`, cleanup functions |
| `media_bot/download_server.py` | Serve original/rendered files through raw tokens | `handle_download`, `create_download_app` |
| `media_bot/settings_ui.py` | Settings/preset/edit-config menus and active input parsing | `settings_callback`, `settings_text_handler`, `handle_editconfig_callback` |
| `media_bot/pool_ui.py` | Pool/workflow menus, ownership checks, and pool input | `pool_callback`, `pool_text_handler` |
| `media_bot/editor.py` | Caption, narration, banner, watermark, and render pipeline | `render_edit` and its stage helpers |
| `media_bot/diagnostics.py` | JSONL event logging and recent-event retrieval | `append_event`, `recent_events` |
| `media_bot/error_handler.py` | Persist unhandled Telegram handler exceptions | `error_handler`, `write_error_log` |
| `media_bot/fix_agent.py` | Categorize errors and prepare/run repair actions | `apply_known_fix`, `invoke_codex_fix`, `run_fix_script` |
| `supervisor.py` | Run the bot, capture output, classify exits, report failures, restart with backoff | `supervise`, `notify_error` |
| `restart_bot.py` | Stop project bot/supervisor processes and launch one supervisor | `_managed_processes`, `_stop_existing`, `main` |

### 4.2 Runtime topology

```text
Telegram
   |
   v
python-telegram-bot polling
   |
   +--> commands
   +--> callback queries
   +--> non-command message router
   |
   v
SQLite <--> persistent job/edit/pool files
   |
   +--> yt-dlp / gallery-dl / FFmpeg / editor subprocesses
   |
   +--> Telegram messages/documents
   |
   +--> aiohttp download server --> reverse proxy/tunnel --> client

restart_bot.py --> supervisor.py --> python -m media_bot
                         |
                         +--> logs, error JSON, Telegram error notice
```

The bot process owns both Telegram polling and the embedded file server. Heavy
download/render work currently shares the same event loop and update-concurrency
budget as lightweight commands and callbacks.

---

## 5. End-to-end current flow

### 5.1 Startup

`media_bot.__main__.main` currently:

1. prefers a full FFmpeg installation when available;
2. loads and validates settings;
3. provisions the configured/latest official `yt-dlp` release;
4. creates persistent storage;
5. initializes SQLite;
6. creates a Telegram `Application` with `concurrent_updates(4)`;
7. registers audit, command, callback, and message handlers;
8. schedules cleanup every six hours;
9. starts the embedded download server in `_post_init`; and
10. begins polling with all Telegram update types enabled.

Important evidence:

- `media_bot/__main__.py:1725-1736` starts the download server.
- `media_bot/__main__.py:1743-1815` constructs and runs the application.
- `media_bot/tools.py:68-96` provisions and verifies `yt-dlp`.

### 5.2 Update audit and handler selection

`audit_update` is installed in handler group `-1` with `block=False`, so it
records an update independently of the normal handler path.

Commands are routed to explicit `CommandHandler` instances. Settings, pool, and
download callbacks are routed by callback-data prefixes. All remaining
non-command messages go to `_message_router`.

Current message-router order:

1. broad allowlist check;
2. give any active settings/edit/pool input flow exclusive ownership;
3. otherwise invoke URL handling.

### 5.3 Authorization

`_authorized` currently allows an update when:

- it is a channel update and the channel ID is allowed; or
- the user ID is allowed; or
- the chat ID is allowed.

Allowing a group chat therefore allows every user in that group to pass the
top-level authorization check. This can be valid for accepting new downloads,
but it is not sufficient authorization for reading or mutating an existing
user-owned job.

Authorization and ownership must be treated as separate checks:

```text
May this principal use the bot here?
                 AND
Does this principal own or have an explicit grant for this resource?
```

### 5.4 Active input

`_handle_active_input` examines `context.user_data` for:

- `settings_flow`;
- edit-config fields represented in `settings_flow`; and
- `pool_flow`.

If a flow expects text or an image, the next matching non-command message is
parsed as that value instead of being treated as a download URL.

This avoids accidentally downloading a URL intended as voice text or another
setting, but the state is user-scoped rather than `(chat, user)` scoped.

### 5.5 URL extraction and platform dispatch

`handle_url`:

1. reads `message.text` or `message.caption`;
2. extracts supported URLs;
3. processes at most eight URLs;
4. processes those URLs sequentially; and
5. sends a summary when more than one URL was present.

`_process_single_url`:

1. sends `Searching`;
2. inserts a job;
3. dispatches TikTok photo URLs to `gallery-dl`;
4. dispatches Instagram URLs to `gallery-dl` with a `yt-dlp` fallback;
5. dispatches other URLs to `yt-dlp`;
6. falls back from failed TikTok `yt-dlp` to `gallery-dl`;
7. reads metadata;
8. persists the selected file;
9. creates a thumbnail;
10. updates SQLite;
11. publishes an action keyboard; and
12. cleans its temporary directory in `finally`.

### 5.6 Downloader process behavior

Ordinary `yt-dlp` downloads use:

- `--no-playlist`;
- `--no-config`;
- restricted filenames;
- a configured maximum size;
- bounded retries and socket timeout;
- four concurrent fragments;
- progress lines;
- a unique temporary directory;
- metadata and thumbnail sidecars; and
- a fixed output template.

Progress is parsed from stderr and used to edit a Telegram status message.

TikTok and Instagram have separate result-selection and post-processing paths.
TikTok photo posts may become an MP4 slideshow or ZIP. `.webm` results may be
converted with FFmpeg.

### 5.6.1 Concurrent TikTok account-archive path

The current uncommitted worktree also contains `/tiktokaccount <username>
[50|all]`.

Its current flow is:

1. normalize a username/profile URL;
2. accept a limit of 1-500 posts or `all`;
3. create a normal job;
4. start an untracked background task;
5. serialize account downloads with a single process-local semaphore;
6. ask `gallery-dl` to download profile media;
7. calculate aggregate media size after downloading;
8. package selected media into a ZIP;
9. persist the ZIP;
10. create a direct-download token; and
11. place the raw link into the status message.

Relevant current-worktree evidence:

- `media_bot/__main__.py:407`
- `media_bot/__main__.py:852-930`
- `media_bot/__main__.py:1783`
- `media_bot/config.py:46-93`
- `media_bot/platforms.py:48-64`
- `media_bot/downloader.py:297-352`

This path partially acknowledges workload pressure by using one semaphore, but
it does not satisfy the durable bounded-queue design in this document. The
default archive allowance is currently 50 GiB, the `all` mode can run for at
least six hours, the disk bound is evaluated only after profile media has been
downloaded, and the background task is not durable across restart. See F-012.

### 5.7 Persistence and action message

`persist_download` moves the selected temporary file into persistent storage
using the job ID in the destination name. The temporary directory is then
cleaned, while the moved file remains until user deletion or retention cleanup.

The completion message does not immediately send the original file. It presents
buttons for:

- direct download of the original;
- edit configuration;
- reset;
- saving/removing the original from the pool; and
- quick rendering with presets.

### 5.8 Callback, edit, and render paths

`download_callback` parses `download:<action>:<id>...`, loads a job or edit, and
performs the requested action.

Edit configuration is stored in `edit_jobs`. Rendering is started with
`asyncio.create_task(_render_edit_job(...))`. The render pipeline can perform:

- watermark analysis/removal/swap;
- captions or automatic transcription;
- voice-over;
- channel banner;
- image banner; and
- final encoding.

Rendered output is first sent through Telegram in the current working tree. If
that upload fails, the bot attempts to issue a direct-download link.

### 5.9 Direct-download link

`create_download_token` creates a random token, stores only its SHA-256 hash,
and records expiry and ownership metadata.

`_build_download_url` inserts the raw token into a public URL. The raw token is
sent through Telegram but is not stored in SQLite.

The embedded server:

1. hashes the presented token;
2. looks it up and marks it used;
3. loads the associated source job or rendered edit;
4. verifies a file exists;
5. performs a lexical storage-directory check; and
6. returns `aiohttp.web.FileResponse`.

### 5.10 Cleanup and retention

Scheduled cleanup:

- deletes expired token records;
- deletes old uploaded/deleted job files and job rows;
- deletes expired Telegram link messages; and
- marks those messages deleted in SQLite.

The default retention period is seven days.

### 5.11 Supervision and restart

`restart_bot.py`:

1. scans Linux `/proc`;
2. identifies project bot and supervisor processes by command line and cwd;
3. sends `SIGTERM`;
4. waits up to eight seconds;
5. sends `SIGKILL` to remaining original PIDs; and
6. launches a detached `supervisor.py`.

`supervisor.py`:

1. takes an advisory file lock;
2. launches `python -m media_bot`;
3. captures bounded stdout/stderr tails;
4. writes event/log records;
5. reports recognized errors through Telegram;
6. restarts after exit with exponential backoff; and
7. deliberately refreshes the bot after seven days.

The restart script is Linux-specific. That matches the Inspiron role but must
not be assumed to work on macOS.

---

## 6. Trust boundaries and protected assets

### 6.1 Principals

- An explicitly allowed Telegram user.
- Any member posting in an explicitly allowed group chat.
- An allowed Telegram channel.
- A holder of a raw direct-download token.
- The local unprivileged bot process.
- The reverse proxy/tunnel in front of the embedded download server.
- The Inspiron operator.
- External downloader and media-processing programs.
- An AI repair provider invoked by `/fix` or `/report`.

### 6.2 Protected assets

- Telegram bot token.
- Downloaded media and rendered edits.
- User presets, edit configuration, pool items, and workflows.
- Source URLs, titles, descriptions, and message text.
- Direct-download raw tokens.
- Error reports, tracebacks, and recent event history.
- External AI repair prompts and generated repair scripts.
- The integrity and availability of the bot host.

### 6.3 Required data-lifecycle guarantees

1. Secrets never enter logs, documentation, Git, or AI repair prompts.
2. A user cannot access or mutate another user's resource without an explicit
   grant.
3. A raw download token is unguessable, short-lived, and consumable at most once.
4. A failed probe must not silently destroy an otherwise valid download.
5. Files are served only from the canonical configured storage root.
6. Temporary files and subprocesses are cleaned after success, failure,
   cancellation, or shutdown.
7. Persistent files are removed according to user deletion and retention rules.
8. Work admission is bounded so downloads/renders cannot starve control-plane
   messages.
9. Diagnostic collection is authorized, minimized, and redacted before any
   external handoff.

---

## 7. Findings and required remediation

## F-001: Callback ownership is not consistently enforced

**Severity:** Critical
**Classification:** Confirmed defect

### Trigger

Two users can interact in an allowed group. User A creates a download or edit.
User B presses an inline button attached to that resource.

### Current behavior

`download_callback` performs the broad allowlist check but does not require the
loaded source job to belong to `query.from_user.id` before original download,
edit, reset, preset rendering, or pool actions.

`handle_editconfig_callback` accepts an `edit_id` and performs reads/mutations
without first requiring `edit.user_id == query.from_user.id`. Its handler is
registered directly.

Relevant snapshot evidence:

- `media_bot/__main__.py:265-272` — broad authorization.
- `media_bot/__main__.py:586-604` — callback parsing and caller ID.
- `media_bot/__main__.py:657-661` — source job loaded without owner check.
- `media_bot/__main__.py:734-810` — original/edit/reset/preset actions.
- `media_bot/__main__.py:1154-1166` — edit-config result launches render or link.
- `media_bot/settings_ui.py:1548-1700` — edit-config callback mutation paths.
- `media_bot/__main__.py:1801-1803` — direct callback registration.

### Impact

- Cross-user original download access.
- Cross-user edit creation and rendering.
- Cross-user reset or mutation of edit state.
- Pool operations using another user's file.
- Broken privacy assumptions in an allowed group.

### Target design

Create explicit resource-access helpers and make them the only way callback
handlers load protected records:

```python
async def require_owned_job(db_path, job_id, principal_user_id):
    job = await get_job(db_path, job_id)
    if job is None or job.user_id != principal_user_id:
        raise ResourceNotFound
    return job

async def require_owned_edit(db_path, edit_id, principal_user_id):
    edit = await get_edit_job(db_path, edit_id)
    if edit is None or edit.user_id != principal_user_id:
        raise ResourceNotFound
    return edit
```

Return the same user-visible result for missing and unauthorized resources. Do
not disclose that another user's resource exists.

All callbacks must perform both:

1. top-level bot authorization; and
2. resource ownership or an explicit share grant.

### Required tests

- `test_download_callback_rejects_another_users_original`
- `test_download_callback_rejects_another_users_edit`
- `test_download_callback_rejects_another_users_reset`
- `test_download_callback_rejects_another_users_preset_render`
- `test_editconfig_callback_rejects_another_users_edit`
- `test_editconfig_text_rejects_flow_for_another_users_edit`
- `test_authorized_group_member_does_not_imply_resource_ownership`

### Acceptance gate

Every callback-data family has a centralized authorization/ownership check, and
the negative tests prove that no token, edit, pool item, file mutation, or render
task is created for a non-owner.

---

## F-002: One-time token consumption is non-atomic

**Severity:** High
**Classification:** Confirmed defect

### Trigger

Multiple clients request the same token at nearly the same time.

### Current behavior

`consume_download_token` selects an unused record, then performs a separate
unconditional update. Multiple concurrent connections can all observe
`used_at IS NULL`.

Local stress reproduction at review time launched ten concurrent consumers.
Depending on the run, between one and ten calls returned success for the same
token.

Relevant evidence:

- `media_bot/storage.py:477-494`
- `media_bot/download_server.py:13-55`

### Impact

A link advertised as one-time may serve the file multiple times.

### Target design

Use one write transaction and a conditional state transition. For modern
SQLite, prefer:

```sql
UPDATE download_tokens
SET used_at = :now
WHERE token_hash = :token_hash
  AND used_at IS NULL
  AND expires_at > :now
RETURNING *;
```

If the deployed SQLite version cannot use `RETURNING`, use `BEGIN IMMEDIATE`,
perform the conditional update, check `rowcount == 1`, then select the row
inside the same transaction.

The update predicate, not a preceding read, decides which request wins.

### Required tests

- Launch at least 20 simultaneous `consume_download_token` calls.
- Assert exactly one non-`None` result.
- Repeat the race enough times to expose scheduling variation.
- Add an HTTP-level simultaneous GET test.

### Acceptance gate

Exactly one request can transition a valid token from unused to used under
concurrency on the deployed SQLite version.

---

## F-003: Tokens are consumed before the resource is validated

**Severity:** High
**Classification:** Confirmed design defect

### Trigger

- The file is missing.
- The path is invalid.
- A reverse proxy or messaging client makes a `HEAD` request.
- A valid request disconnects before transfer begins.

### Current behavior

`handle_download` consumes the token before loading and validating the resource.
`add_get` normally makes a `HEAD` route available unless explicitly disabled.

### Impact

A valid user can receive a permanently invalid link without receiving the file.

### Target design

Define an explicit token state model:

```text
unused -> reserved -> used
             |
             +-> unused after bounded reservation expiry, if no response began
```

For a simpler first implementation:

1. reject `HEAD` without consuming;
2. validate the token and resource without mutating state;
3. validate canonical file containment and readability;
4. atomically claim the token;
5. return the response.

Document that once a GET response begins, a disconnected client still consumes
the token. If retry-after-disconnect is required, implement reservations rather
than pretending transfer completion is observable through a basic
`FileResponse`.

### Required tests

- `HEAD` does not consume.
- Missing files do not consume.
- Outside-root files do not consume.
- A successful GET consumes exactly once.
- A second GET returns the chosen generic invalid/expired response.

### Acceptance gate

Automated probes and pre-response validation failures cannot burn a valid token.

---

## F-004: Long-running work can starve bot control traffic

**Severity:** High
**Classification:** Confirmed architectural risk

### Trigger

Four messages begin long downloads while `concurrent_updates(4)` is configured.
Each message can contain eight sequential URLs, each with a one-hour timeout.

### Current behavior

The Telegram update handler awaits the complete download pipeline. There is no
download worker queue. Render jobs use unbounded `asyncio.create_task`.
`_RENDER_QUEUE` is declared but unused.

Relevant evidence:

- `media_bot/__main__.py:406`
- `media_bot/__main__.py:472-507`
- `media_bot/__main__.py:810`
- `media_bot/__main__.py:1157`
- `media_bot/__main__.py:1327`
- `media_bot/__main__.py:1756-1760`

### Impact

- `/status`, `/help`, cancel, and callback acknowledgements can stall.
- Multiple FFmpeg/ONNX processes can exhaust CPU, memory, disk, or file
  descriptors.
- Telegram retries and stale callback errors become more likely.

### Target design

Separate the control plane from the work plane:

```text
Telegram handler
  -> validate and authorize
  -> create durable queued job
  -> send "queued" acknowledgement
  -> return quickly

bounded download workers
  -> claim queued job
  -> execute download
  -> persist result
  -> deliver completion/error

bounded render workers
  -> claim queued edit
  -> execute render
  -> deliver completion/error
```

Recommended first limits:

- global download workers: 2;
- global render workers: 1;
- active download jobs per user: 1;
- queued jobs per user: a small configured limit;
- URLs accepted in one Telegram message: retain 8 only if they become separate
  queued jobs;
- bounded queue length with an explicit "busy, try later" response.

Do not store live `Update`, `Context`, or message objects in a durable job.
Persist IDs and immutable input data:

- job ID;
- user ID;
- chat ID;
- source message ID;
- URL;
- status-message ID;
- requested operation.

### Crash/restart requirement

On startup, jobs left in `downloading` or `rendering` must be reconciled:

- retry if the operation is explicitly retry-safe; or
- mark interrupted with a clear user/operator message.

Do not silently leave them pending forever.

### Required tests

- Four long queued downloads do not block `/status`.
- Global and per-user limits are enforced.
- Queue overflow receives a clear response.
- Jobs transition through `queued`, `downloading`, and terminal states.
- Interrupted jobs are reconciled at startup.
- Duplicate worker claims cannot execute the same job twice.

### Acceptance gate

Control-plane handlers remain responsive while configured worker capacity is
fully occupied.

---

## F-005: Active input state can cross chats and race

**Severity:** High
**Classification:** Design risk

### Trigger

A user begins a settings/edit flow in one chat and sends their next message in
another chat. Two updates from the same user arrive concurrently.

### Current behavior

Flow state lives in `context.user_data`, while the application processes four
updates concurrently. `_handle_active_input` gives the active flow exclusive
ownership of the next eligible message.

### Impact

- A URL can become narration/settings text unexpectedly.
- A settings value can be routed to the downloader.
- A private value can be applied to an edit initiated in a group.
- Concurrent messages can overwrite flow fields or update the wrong resource.

### Target design

Use a flow record keyed by `(chat_id, user_id)` with:

- flow type;
- resource ID;
- expected input kind/field;
- creation and expiry timestamps;
- monotonic version;
- optional origin message ID.

Serialize transitions per flow key using a lock or an atomic persistent
version/update. Provide `/cancel`, visible Back/Cancel buttons, and a short TTL.

Every input transition must re-check resource ownership. Never trust an
`edit_id` merely because it was previously stored in user state.

### Required tests

- Flow created in chat A does not consume a message in chat B.
- Two concurrent messages cannot both satisfy one expected input.
- Expired flow falls back to normal URL handling.
- `/cancel` clears only the current chat/user flow.
- Deleted or transferred resources fail ownership revalidation.

### Acceptance gate

One input is consumed by at most one flow, scoped to the originating chat and
user.

---

## F-006: Final size, cancellation, and output bounds are incomplete

**Severity:** Medium
**Classification:** Confirmed code gap

### Current behavior

- `yt-dlp --max-filesize` is configured, but the final merged output is not
  checked again before persistence.
- Downloader process cleanup catches `OSError` and timeout, but cancellation
  can bypass the cleanup branch.
- Downloader stdout/stderr are retained in unbounded lists for the duration of
  the process.
- Helper subprocesses use `communicate`, which buffers all output.

Relevant evidence:

- `media_bot/downloader.py:74-145`
- `media_bot/downloader.py:187-203`
- `media_bot/downloader.py:479-484`

### Impact

- Merged or post-processed output can exceed the configured bound.
- Shutdown/cancellation can leave child processes and temporary files.
- Verbose tools can consume excessive memory.

### Target design

Create one reusable cancellation-safe process runner:

```python
async def run_bounded_process(...):
    process = await create_subprocess_exec(..., start_new_session=True)
    try:
        await wait_with_timeout_and_bounded_stream_capture(process)
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
    except asyncio.TimeoutError:
        await terminate_process_group(process)
        raise DownloadError(...)
    finally:
        await close_reader_tasks()
```

Requirements:

- use bounded deques for output tails;
- keep progress parsing separate from diagnostic retention;
- send `SIGTERM`, wait briefly, then `SIGKILL`;
- terminate descendants/process groups on Linux;
- preserve cancellation by re-raising `CancelledError`;
- perform a final file-size check after download, merge, conversion, ZIP, and
  render stages;
- remove partial outputs on failure;
- clean temporary directories in all exit paths.

### Required tests

- Cancellation terminates a fake child process.
- Timeout terminates a fake child process.
- Output capture remains bounded.
- Final merged file over the limit is rejected.
- Partial output is removed.
- Successful output at the boundary is accepted.

### Acceptance gate

No supported cancellation/timeout path leaves the test child alive or preserves
an untracked partial file.

---

## F-007: Diagnostics can capture unauthorized or cross-user content

**Severity:** Medium
**Classification:** Confirmed data-flow problem

### Current behavior

`audit_update` records message text/caption before normal authorization because
it runs in handler group `-1`. `recent_events(user_id=...)` includes events
whose `user_id` is missing, so global log events enter user reports. `/report`
then prepares an AI repair handoff using recent events.

Relevant evidence:

- `media_bot/__main__.py:1620-1631`
- `media_bot/__main__.py:1634-1673`
- `media_bot/__main__.py:1779`
- `media_bot/diagnostics.py:13-38`

### Impact

- Unauthorized message text can be persisted.
- One user's report can contain global activity related to another user.
- Source URLs, captions, traceback data, or local paths can be sent to an
  external repair provider.

### Target design

Use structured, minimized events:

```json
{
  "kind": "download_requested",
  "user_id": 123,
  "chat_id": -100,
  "job_id": 42,
  "platform": "youtube",
  "url_fingerprint": "sha256-prefix",
  "message_length": 87
}
```

Do not record raw message text by default. If operator diagnostics requires raw
content, make it explicit, time-limited, access-controlled, and redacted.

Before external AI handoff:

- include only the reporting user's scoped events;
- include an allowlisted set of global health event types;
- strip tokens, query strings, usernames where unnecessary, absolute private
  paths, and environment-like values;
- show or record exactly what provider/model receives the report;
- never allow AI-generated scripts to execute without the existing safety and
  approval boundary being reviewed.

### Required tests

- Unauthorized updates do not persist message content.
- A user report excludes another user's event.
- A user report excludes unapproved global message/log events.
- Known token/URL/query/path patterns are redacted.
- Global health counters can still be included without raw content.

### Acceptance gate

A report fixture containing two users and sensitive-looking strings produces a
strictly scoped, redacted payload.

---

## F-008: Download-server origin and binding defaults are unsafe or unusable

**Severity:** Medium
**Classification:** Confirmed configuration defect

### Current behavior

- The server starts on `0.0.0.0` regardless of whether a public domain is
  configured.
- `_build_download_url` falls back to `https://localhost/...`.
- The embedded server itself serves HTTP, with HTTPS expected from a reverse
  proxy/tunnel.

Relevant evidence:

- `media_bot/__main__.py:392-394`
- `media_bot/__main__.py:1725-1736`
- `media_bot/config.py:39-45`

### Impact

- The port may be reachable on unintended interfaces.
- Generated fallback links may be unusable.
- Scheme/host/proxy assumptions are implicit.

### Target design

Replace the optional domain with explicit settings:

```text
MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN=https://media.example.test
MEDIA_BOT_DOWNLOAD_BIND_HOST=127.0.0.1
MEDIA_BOT_DOWNLOAD_PORT=8080
```

Validation:

- public origin must be HTTPS outside an explicit development mode;
- public origin must not contain credentials, query, or fragment;
- bind host defaults to loopback;
- action buttons requiring direct download are disabled with a clear message
  when no public origin is configured;
- trust forwarded headers only from a configured proxy, or do not use them.

Canonicalize file paths before containment checks:

```python
root = storage_dir.resolve(strict=True)
file = file_path.resolve(strict=True)
file.relative_to(root)
```

Decide whether symlinks are forbidden. The safest rule is to forbid them for
stored/served media.

### Required tests

- Default bind host is loopback.
- Missing public origin disables link creation.
- Invalid/non-HTTPS origin fails configuration.
- Symlink escaping storage is rejected.
- Normal source and rendered files remain downloadable.

### Acceptance gate

The service exposes no listening interface or public link beyond what the
operator explicitly configured.

---

## F-009: URL scheme contract and redirect language are inconsistent

**Severity:** Low to medium
**Classification:** Documentation drift and security hardening gap

### Current behavior

`is_supported_url` accepts both HTTP and HTTPS while its docstring and the
historical security contract state HTTPS. The README says redirects are not
accepted, while downloader tools can necessarily encounter platform redirects.

Relevant evidence:

- `media_bot/platforms.py:13-19`
- `README.md:84-85`

### Target design

- Accept only HTTPS user input.
- Normalize and classify the hostname.
- Document that only the initial submitted hostname is allowlisted unless
  redirect targets are independently enforced.
- Research downloader-specific redirect controls before making stronger claims.
- Keep fixed arguments and the `--` separator.

### Required tests

- HTTP is rejected.
- Mixed-case HTTPS and safe trailing punctuation behave as intended.
- User-info, malformed ports, Unicode/lookalike hosts, and trailing-dot cases
  are covered.

### Acceptance gate

Tests, docstrings, README, and implementation describe the same URL policy.

---

## F-010: Setup and operational documentation is not reproducible

**Severity:** Medium operational risk
**Classification:** Confirmed documentation/dependency drift

### Current behavior

`requirements.txt` contains what appears to be a broad Fedora environment
freeze, including `file:///builddir/...` dependencies and unrelated system
packages. This is not a portable application dependency contract.

The README says the bot replies with media and deletes temporary files after a
Telegram upload attempt. The current source-download flow persists files and
offers action buttons/direct links.

### Target design

Split dependency intent:

- a minimal, reviewed application dependency file or `pyproject.toml`;
- an optional development/test dependency set;
- documented external runtime tools such as FFmpeg;
- a lock file produced by a repeatable tool if exact transitive pinning is
  required.

Do not mechanically replace the current file without first proving a clean
Inspiron installation. Some editing/transcription features have large optional
dependency sets.

Update README diagrams and statements after behavior fixes stabilize.

### Required tests

- Create a fresh temporary virtual environment using the supported Python
  version.
- Install only documented dependencies.
- Import every production module.
- Run all unit tests.
- Run a no-secret configuration failure smoke test.

### Acceptance gate

A clean supported host can install and run the test suite without machine-local
wheel URLs.

---

## F-011: Restart behavior needs explicit Linux and race documentation

**Severity:** Low to medium
**Classification:** Design risk

### Current behavior

`restart_bot.py` uses Linux `/proc`; on macOS it finds no managed processes. It
collects one initial PID set, signals those PIDs, then starts a new supervisor.
There is no explicit supervisor signal-forwarding protocol documented for the
child bot.

Relevant evidence:

- `restart_bot.py:15-66`
- `supervisor.py:193-275`

### Target design

For the Inspiron:

- document Linux as a requirement;
- have the supervisor own child termination;
- handle `SIGTERM`/`SIGINT` by terminating and awaiting the bot child;
- stop the old supervisor first through a controlled signal path;
- verify the old lock is released before declaring restart success;
- after launch, verify one supervisor, one bot, and a healthy log/endpoint.

Do not make the script a cross-platform process manager unless macOS operation
becomes an actual requirement.

### Required tests

- Supervisor forwards termination to a fake child.
- Restart leaves exactly one supervisor and one bot in a Linux integration
  fixture.
- A second supervisor fails the lock cleanly.
- Startup failure is reported and retried with backoff.

### Acceptance gate

The restart script reports success only after the new supervisor owns the lock
and the bot reaches a defined readiness point.

---

## F-012: TikTok account archives bypass the planned durable work model

**Severity:** High while unbounded `all` mode is available
**Classification:** Uncommitted feature risk requiring review

### Current behavior

The concurrent `/tiktokaccount` implementation creates a job and immediately
starts `_run_tiktok_account_job` with `asyncio.create_task`. A process-local
semaphore limits execution to one account at a time, but queued tasks are not
durable, visible, cancellable, or limited per user.

The default `MEDIA_BOT_MASS_DOWNLOAD_MAX_MB` is 50 GiB. `gallery-dl` downloads
the selected profile content before aggregate media size is measured. ZIP
creation uses stored entries, which avoids a second compressed expansion but
still requires the downloaded media plus the archive to coexist temporarily.
The `all` option can request the entire public account and receives a minimum
six-hour timeout.

The resulting link is edited directly into the status message rather than being
recorded through the normal download-message expiry/deletion flow.

Relevant current-worktree evidence:

- `media_bot/__main__.py:852-930`
- `media_bot/config.py:46-93`
- `media_bot/downloader.py:297-352`
- `tests/test_downloader.py:32-59`
- `tests/test_platforms.py:11-23`

### Impact

- A user can request very large disk and network consumption.
- Multiple users can accumulate an unbounded number of waiting tasks.
- Restart loses the task while leaving a pending job and partial temporary data.
- Aggregate-size rejection happens only after disk/network cost is incurred.
- The final ZIP can temporarily double storage requirements.
- Generated link messages are not tracked for normal expiry deletion.
- A unit test with two tiny fake files does not validate live profile behavior,
  pagination, disk bounds, cancellation, restart, or delivery.

### Target design

Do not create a separate long-running execution model for account archives.
Represent each account request in the same durable bounded download queue
described by F-004.

Before enabling `all`:

1. require an operator-approved configurable feature flag;
2. set conservative byte, item, duration, and per-user quotas;
3. reserve/check available disk before and during work;
4. stop downloads as soon as the running byte counter crosses the limit;
5. use a staging filesystem with a configured quota where practical;
6. expose queue position and cancellation;
7. reconcile interrupted account jobs after restart;
8. create links through the normal tracked expiry/deletion helper;
9. enforce the same atomic token behavior as every other download; and
10. document that platform pagination/auth/rate limits can make archive results
    partial.

Prefer a bounded default such as the newest 25-50 posts. Treat `all` as an
operator-only or explicitly approved high-cost action until real resource and
platform behavior is measured.

### Required tests

- Per-user and global queue admission.
- More than one account request cannot accumulate unbounded tasks.
- Running byte limit aborts before the final archive stage.
- Insufficient disk prevents admission.
- Cancellation cleans downloader, staging files, and job state.
- Restart reconciles an interrupted archive.
- Final ZIP is checked against the configured bound.
- Link message is stored and deleted on expiry.
- Live integration test with a dedicated harmless profile, only when explicitly
  authorized.

### Acceptance gate

The account path uses the common durable queue/token/message-cleanup mechanisms,
has conservative quotas, and cannot consume unbounded disk, time, or queued
tasks.

---

## 8. Target architecture and invariants

### 8.1 Target flow

```text
Telegram update
  |
  +--> minimal structured audit after authorization
  |
  +--> command/callback/message router
          |
          +--> bot-use authorization
          +--> resource ownership/grant check
          +--> chat-scoped flow transition
          |
          +--> durable queue admission
                    |
             bounded worker
                    |
             cancellation-safe subprocess
                    |
             canonical persistence
                    |
             terminal job state
                    |
             Telegram delivery or atomic token link

HTTP GET
  -> validate token without mutation
  -> validate canonical resource
  -> atomic claim
  -> stream file
```

### 8.2 Non-negotiable invariants

Use these as review questions and test names:

1. **Authorization:** passing a chat allowlist never implies ownership.
2. **Enumeration resistance:** missing and unauthorized resources look the same.
3. **Token atomicity:** at most one request can claim a one-time token.
4. **Probe safety:** `HEAD` and invalid-resource requests do not consume tokens.
5. **Queue bounds:** work cannot exceed configured global/per-user capacity.
6. **Control responsiveness:** work does not occupy Telegram update handlers.
7. **State scoping:** active input is scoped to chat and user.
8. **Cancellation safety:** cancelling work terminates descendants and cleans
   temporary/partial files.
9. **Final size enforcement:** every persisted/delivered result is measured
   after all post-processing.
10. **Canonical containment:** only canonical regular files below storage can be
    served.
11. **Diagnostic minimization:** raw unauthorized/cross-user message content
    never enters user or AI reports.
12. **Explicit exposure:** public origin and listening interface are configured,
    never guessed.
13. **Durable states:** restart reconciliation leaves no indefinitely active
    job.
14. **Honest delivery:** a job is marked delivered only after an actual delivery
    path succeeds.

---

## 9. Ordered implementation plan

Do not implement this as one broad refactor. Each phase should be independently
reviewable, testable, and reversible.

## Phase 0: Reconcile live state and freeze the baseline

### Work

1. Read `AGENTS.md` and this document.
2. Inspect current `git status`, diff, branch, and tests.
3. Determine which uncommitted changes belong to the user/current feature work.
4. Re-run the local 94-test baseline.
5. Record current schema version/shape without copying runtime data.
6. Confirm the deployed Python and SQLite versions on the Inspiron only when
   remote read-only inspection is authorized.

### Gate

- No user changes were lost.
- Baseline tests are reproducible.
- The implementation branch/scope is explicit.

### Rollback

No production or schema changes occur in this phase.

---

## Phase 1: Centralize authorization and ownership

### Work

1. Add centralized owned-resource access helpers.
2. Add an authorization wrapper for every callback family.
3. Enforce job/edit/preset/pool/workflow ownership at the storage query boundary
   where practical.
4. Revalidate ownership on active text/photo input.
5. Return generic missing/not-authorized responses.
6. Add all F-001 negative tests.

### Dependency

None. This should be the first code change.

### Gate

All ownership tests pass, and no protected mutation path uses a raw global
`get_job(id)`/`get_edit_job(id)` without a deliberate access decision.

### Rollback

Code-only. Revert the focused ownership patch if it blocks valid owners; no
schema migration should be required.

---

## Phase 2: Repair direct-download token semantics

### Work

1. Add atomic token claim.
2. Separate validation from claim.
3. define/reject `HEAD` without consumption.
4. Canonicalize file containment and reject symlinks.
5. Add concurrency and HTTP probe tests.
6. Decide whether to add a reservation state or document consume-on-GET-start.

### Dependency

Phase 1, because token creation must already require ownership.

### Gate

The repeated concurrency test always produces exactly one winner and all
pre-response validation failures preserve the token.

### Rollback

If a schema migration adds reservation columns, write forward and rollback SQL
before deployment. Back up the SQLite file before applying it.

---

## Phase 3: Add bounded durable work queues

### Work

1. Define explicit job state transitions.
2. Add queue metadata/state fields through a versioned migration.
3. Make Telegram handlers enqueue and return.
4. Add bounded download workers.
5. Add bounded render workers.
6. Add per-user admission controls.
7. Reconcile interrupted work on startup.
8. Replace raw render `create_task` sites.
9. Remove or implement `_RENDER_QUEUE`.
10. Move `/tiktokaccount` from its process-local semaphore/background task into
    the same durable queue, or keep the command disabled until that migration is
    complete.

### Dependency

Phases 1 and 2.

### Gate

Control commands remain responsive while workers are saturated, and duplicate
execution is prevented.

### Rollback

Keep old job states readable. New workers can be disabled through configuration
while retaining queued records for operator inspection.

---

## Phase 4: Consolidate subprocess and file lifecycle safety

### Work

1. Implement a shared cancellation-safe subprocess runner.
2. Bound stdout/stderr diagnostic retention.
3. Terminate process groups.
4. Add final-size checks to every result path.
5. Centralize canonical persistence and partial-file cleanup.
6. Add timeout/cancellation/size tests.

### Dependency

Can begin after Phase 1, but integration is clearer after the queue contract is
stable.

### Gate

No test child survives cancellation/timeout, and oversized final outputs never
become completed jobs.

### Rollback

Keep each downloader adapter behaviorally compatible. Land the shared runner
and migrate one adapter at a time.

---

## Phase 5: Scope interactive flows and minimize diagnostics

### Work

1. Key flow state by chat and user.
2. Add flow version, TTL, and cancel behavior.
3. Serialize transitions.
4. Audit only after authorization.
5. Replace raw message content with structured/redacted fields.
6. Scope `/report` events strictly.
7. Add a deterministic redaction layer before AI handoff.

### Dependency

Phase 1 ownership helpers.

### Gate

Cross-chat/concurrent flow tests and two-user diagnostic isolation tests pass.

### Rollback

Support reading old in-memory flow shapes only during one process lifetime if
needed; process restart naturally clears them.

---

## Phase 6: Make exposure and installation explicit

### Work

1. Add validated public-origin and bind-host settings.
2. Default bind host to loopback.
3. Disable direct-link UI when no public origin exists.
4. Produce a minimal dependency contract.
5. Add clean-environment installation verification.
6. Update README to match actual persistence/delivery behavior.
7. Document Linux-only restart behavior and readiness checks.

### Dependency

Phase 2 token behavior should be stable before documenting the final link flow.

### Gate

A clean environment installs successfully, and the service never invents a
public origin or binds publicly by default.

---

## Phase 7: Staged Inspiron rollout

### Work

1. Complete all local gates.
2. Review the final diff and schema migration.
3. Obtain explicit approval for remote deployment/restart.
4. Inspect the Inspiron before changing it.
5. Back up the database and identify rollback code.
6. Deploy through the repository's normal mechanism.
7. Run schema migration once.
8. Restart with `restart_bot.py`.
9. Verify one supervisor and one bot.
10. Exercise authorized live smoke tests.
11. Monitor logs and resource use.

### Gate

All live acceptance checks below pass without exposing secrets or other users'
data.

### Rollback

Stop the new process, restore the previous code and compatible database backup,
restart once, and verify the previous readiness behavior. Never improvise a
database downgrade after production mutation.

---

## 10. Test plan and acceptance matrix

| Area | Required automated evidence | Required live evidence |
|---|---|---|
| Authorization | Cross-user callback/input tests | Two authorized test users in an allowed group |
| Token | Concurrent claim, HEAD, missing file, symlink escape | One successful link, failed reuse, safe preview behavior |
| Queue | Saturation, admission, duplicate claim, restart reconciliation | Commands responsive during a real download/render |
| Subprocess | Timeout, cancellation, bounded output, final size | No orphan process after controlled cancellation |
| Flow state | Cross-chat, race, expiry, cancel | Settings flow in private and group chats |
| Diagnostics | Two-user isolation and redaction fixtures | Inspect one sanitized report before AI handoff |
| Exposure | Config validation and bind-host test | Loopback listener and proxy route verified |
| Cleanup | Token, message, job, thumbnail, edit/pool references | Retention dry-run then approved cleanup |
| Supervisor | Signal forwarding, lock, backoff tests | Exactly one supervisor and one bot after restart |
| Installation | Fresh environment install/import/test | Clean service start on supported Inspiron runtime |
| Account archive | Queue/quota/disk/cancel/restart/link-expiry tests | One explicitly approved harmless-profile test |

### Local commands

Run proportionately after every slice:

```sh
git diff --check
.venv/bin/python -m compileall -q media_bot tests supervisor.py restart_bot.py
.venv/bin/python -B -m unittest discover -s tests -v
```

Add focused test commands during development. The complete suite remains the
local merge gate.

Do not run production downloads, remote SSH actions, tunnel changes, or bot
restarts as part of an ordinary local test.

### Live smoke-test sequence

After approved deployment:

1. Confirm one supervisor and one bot process.
2. Confirm the bot answers `/status` and `/help`.
3. Submit one small supported HTTPS URL as the owner.
4. Confirm queued, downloading, completed, and action states.
5. Attempt the owner's callback as a second authorized user; confirm denial.
6. Generate one original link.
7. Confirm `HEAD` does not burn it.
8. Confirm one GET succeeds and reuse fails.
9. Start a bounded long job and confirm `/status` remains responsive.
10. Cancel or interrupt a controlled test job and confirm no child remains.
11. Inspect sanitized logs/events.
12. Confirm cleanup scheduling and disk usage.

Never use private or copyrighted test media when a small public test fixture is
available.

---

## 11. Inspiron deployment and rollback guide

The project instruction defines:

```text
sshi
```

as the shortcut for connecting to the Inspiron. Use it to reduce command noise,
but treat remote writes and restarts as consequential actions requiring explicit
authorization.

### Read-only preflight

Proposed commands after authorization to inspect:

```sh
sshi
cd /path/to/media-downloader
git status --short
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
ps -ef
tail -n 100 runtime/supervisor.log
```

Discover the actual remote repository path rather than assuming it. Do not print
`.env`, tokens, or environment dumps.

Record:

- current commit/branch;
- dirty worktree state;
- Python and SQLite versions;
- service process IDs;
- database path and size;
- available disk space;
- current listener addresses; and
- current readiness/error state.

### Backup

Before a schema change:

1. stop or quiesce writers through the approved process;
2. use SQLite's supported backup mechanism;
3. record backup path and checksum;
4. verify the backup can be opened;
5. keep it outside any directory affected by deployment cleanup.

Do not copy secrets into chat or logs.

### Restart

The project restart entrypoint is:

```sh
python restart_bot.py
```

It is expected to stop existing bot/supervisor processes and restart the full
stack. After execution, independently verify:

- the printed supervisor PID still exists;
- exactly one supervisor owns the lock;
- exactly one bot child exists;
- the log shows successful initialization;
- the Telegram bot responds; and
- the download listener uses the intended bind address.

A printed "restarted" line is not sufficient readiness evidence.

### Rollback triggers

Rollback immediately when:

- ownership tests fail live;
- tokens serve more than once or are burned by probes;
- the bot becomes unresponsive under configured capacity;
- database migration errors appear;
- orphan downloader/render processes accumulate;
- the listener is exposed on an unintended interface; or
- diagnostic payloads contain unapproved content.

---

## 12. Prioritized remaining-work checklist

### P0 — must fix before multi-user group use

- [ ] Add owned job/edit access helpers.
- [ ] Protect every download/edit-config callback path.
- [ ] Revalidate ownership for active text/photo input.
- [ ] Add cross-user negative tests.
- [ ] Make token claim atomic.
- [ ] Prevent `HEAD` and invalid-resource requests from burning tokens.
- [ ] Add simultaneous token-consumption tests.
- [ ] Keep `/tiktokaccount all` disabled or operator-restricted until durable
      queue and quota controls exist.

### P1 — must fix before claiming production resilience

- [ ] Add bounded download queue/workers.
- [ ] Add bounded render queue/workers.
- [ ] Add per-user admission limits.
- [ ] Add durable job states and restart reconciliation.
- [ ] Implement cancellation-safe subprocess/process-group cleanup.
- [ ] Bound downloader output retention.
- [ ] Enforce final post-processing file size.
- [ ] Verify commands remain responsive during saturated work.
- [ ] Move TikTok account archives into the durable queue.
- [ ] Add conservative item/byte/time/disk quotas and tracked link expiry for
      account archives.

### P2 — privacy and deployment hardening

- [ ] Scope flows by chat and user.
- [ ] Add flow TTL and cancel.
- [ ] Move audit after authorization.
- [ ] Redact and scope AI report payloads.
- [ ] Default download server to loopback.
- [ ] Require validated public origin for links.
- [ ] Canonicalize paths and reject symlink escape.
- [ ] Accept HTTPS input only or explicitly revise the contract.

### P3 — operations and maintainability

- [ ] Replace nonportable dependency freeze with reviewed dependency groups.
- [ ] Add clean-environment install verification.
- [ ] Update README current-flow and retention statements.
- [ ] Document Linux-only restart behavior.
- [ ] Add supervisor signal-forwarding/readiness behavior.
- [ ] Add metrics for queue depth, active workers, durations, failures, and disk
      usage without recording raw URLs.
- [ ] Run approved local, then Inspiron acceptance gates.

### Deferred enhancements

- [ ] Explicit resource-sharing grants if community downloads are desired.
- [ ] Admin/operator role separate from ordinary allowed users.
- [ ] Token reservation/retry semantics beyond consume-on-response-start.
- [ ] Graceful user cancellation of queued and running jobs.
- [ ] Quotas by bytes/time in addition to job counts.

---

## 13. Suggested issue breakdown

Create small issues or implementation slices rather than one umbrella change:

1. `security: enforce ownership in download callbacks`
2. `security: enforce ownership in edit-config callbacks and text input`
3. `security: atomically claim one-time download tokens`
4. `security: make HEAD and resource validation non-consuming`
5. `security: canonicalize served paths and default bind to loopback`
6. `reliability: add durable bounded download workers`
7. `reliability: add durable bounded render workers`
8. `reliability: make subprocess execution cancellation-safe`
9. `reliability: enforce final output size across all adapters`
10. `routing: scope active input by chat and user`
11. `privacy: minimize audit events and isolate AI report context`
12. `operations: repair dependency and README contracts`
13. `operations: graceful supervisor shutdown and readiness verification`
14. `reliability: integrate TikTok account archives with durable queues and quotas`

Each issue should contain:

- exact invariants affected;
- source symbols in scope;
- tests required;
- schema impact;
- deployment risk;
- rollback plan; and
- a definition of done.

---

## 14. Instructions for the next AI coding agent

### Before editing

1. Read `AGENTS.md` completely.
2. Read this document completely.
3. Inspect current `git status` and all overlapping diffs.
4. Re-run the baseline tests using `.venv/bin/python`.
5. Select one implementation slice from Section 13.
6. State assumptions and non-goals.

### While editing

- Preserve existing user changes.
- Use small patches.
- Do not combine a security fix with unrelated feature work.
- Do not access `.env`, credentials, runtime media, or private database content.
- Do not install dependencies unless explicitly approved and required.
- Do not commit, push, publish, deploy, restart, or contact external services
  without matching authorization.
- Put access checks close to data access.
- Prefer generic unauthorized/not-found responses.
- Treat callback data and stored flow IDs as untrusted references.
- Preserve cancellation and clean all resources.
- Add a regression test before or with each defect fix.
- Use durable IDs in queued work, not Telegram object instances.
- Verify exact schema/runtime compatibility before using SQLite features.

### Review checklist for every patch

- Which invariant does this enforce?
- Can an allowed group member reach another user's resource?
- Can two concurrent requests both win?
- What happens on cancellation, timeout, restart, and disk-full?
- Is any raw message, URL, token, or path added to logs?
- Does this change a schema or deployment assumption?
- Is there a focused negative test?
- Does a bulk/account operation obey queue, byte, disk, duration, and per-user
  limits?
- Did the full suite pass?
- Did the working-tree scope remain clean?

### Handoff format

Report:

1. outcome first;
2. files and symbols changed;
3. defect/invariant addressed;
4. tests run and exact result;
5. live verification performed or explicitly not performed;
6. schema/deployment impact;
7. remaining risks and next recommended slice.

Never claim the Inspiron or Telegram path is ready based only on unit tests.

---

## 15. Definition of done for the complete remediation

The system is complete for this review scope only when:

- all P0, P1, P2, and P3 checklist items are resolved or explicitly accepted as
  documented residual risk;
- every protected callback and input path enforces resource ownership;
- token concurrency and probe-safety tests pass;
- downloads/renders execute through bounded workers;
- cancellation leaves no child or partial output;
- interactive state is chat/user scoped and race-safe;
- diagnostics are minimized, redacted, and isolated;
- binding and public origin are explicit and safe;
- installation is reproducible on a clean supported environment;
- local tests pass;
- approved Inspiron deployment and rollback evidence exists;
- approved live Telegram acceptance tests pass; and
- documentation matches the deployed behavior.

Until then, the honest status is:

> The happy path works and the local suite passes, but the system still requires
> authorization, token, concurrency, lifecycle, privacy, and deployment
> hardening before it should be considered robust for multi-user production use.

---

## 16. Implementation status on `fix/input-download-hardening`

Implemented locally:

- centralized job/edit ownership checks and generic unauthorized responses;
- ownership validation when creating direct-download tokens;
- atomic single-use token consumption with probe-safe `HEAD` and invalid-file
  behavior;
- canonical storage containment and symlink rejection;
- bounded download/render queues with global and per-user capacity;
- explicit queued/running/failed state transitions and startup reconciliation;
- subprocess process-group termination, bounded captured output, timeout and
  cancellation cleanup, final-output size enforcement, and a live working-tree
  ceiling for TikTok account downloads;
- chat-bound, expiring interactive flow state, per-user input serialization,
  and `/cancel`;
- authorized-only structural update auditing, diagnostic redaction, and
  per-user report isolation;
- HTTPS-only source URLs;
- explicit HTTPS public download origin and loopback-default bind host;
- explicit opt-in for unlimited TikTok account downloads;
- a minimal application dependency manifest; and
- regression coverage for authorization, concurrent token use, probe behavior,
  queues, flow scope/expiry, redaction, configuration, and subprocess limits.

Still requires operator-authorized deployment verification:

- install from `requirements.txt` in a clean supported environment;
- configure the real HTTPS reverse proxy/tunnel origin and verify reachability;
- deploy to the Inspiron using its existing service procedure;
- run live Telegram acceptance tests for source download, render fallback link,
  cross-user denial, queue saturation, cancellation, restart reconciliation,
  and retention cleanup; and
- retain the previous deployment artifacts/configuration for rollback.

These are deliberately not claimed complete by the branch's local unit tests.
