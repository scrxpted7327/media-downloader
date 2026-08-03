import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from media_bot.__main__ import (
    _admin_authorized,
    _bind_flow_context,
    _render_edit_job,
    _send_watermark_review_album,
    cancel_job_command,
    fix_command,
    handle_url,
    report_command,
)
from media_bot.storage import (
    create_edit_job,
    create_job,
    get_edit_job,
    get_job,
    init_db,
    update_edit_job,
    update_job,
)
from media_bot.watermark import WatermarkAnalysis, WatermarkCandidate
from media_bot.work_queue import WorkQueue


def _settings(**overrides):
    values = {
        "allowed_user_ids": frozenset({1}),
        "allowed_chat_ids": frozenset(),
        "admin_user_ids": frozenset({1}),
        "repair_enabled": False,
        "tools_dir": Path("tools"),
        "timeout_seconds": 30,
        "max_filesize_mb": 47,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _update(*, user_id: int = 1, chat_id: int = 1, chat_type: str = "private"):
    message = MagicMock()
    message.text = None
    message.caption = None
    message.chat_id = chat_id
    message.reply_text = AsyncMock()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=message,
        message=message,
        callback_query=None,
    )


def _context(settings, **bot_data):
    context = MagicMock()
    context.args = []
    context.user_data = {}
    context.application.bot_data = {"settings": settings, **bot_data}
    return context


class CommandContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_group_does_not_grant_admin_role(self):
        update = _update(user_id=99, chat_id=-1001, chat_type="supergroup")
        settings = _settings(
            allowed_user_ids=frozenset(),
            allowed_chat_ids=frozenset({-1001}),
            admin_user_ids=frozenset({1}),
        )

        self.assertFalse(_admin_authorized(update, settings))

    async def test_repair_command_stops_at_disabled_feature_flag(self):
        update = _update()
        context = _context(_settings(repair_enabled=False))

        with patch("media_bot.__main__.apply_known_fix", new=AsyncMock()) as repair:
            await fix_command(update, context)

        repair.assert_not_awaited()
        self.assertIn("disabled", update.effective_message.reply_text.await_args.args[0])

    async def test_reply_fix_reports_live_render_and_supervisor_patience(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "media.db"
            await init_db(db_path)
            source = await create_job(db_path, "https://example.com/video", 1, 1)
            await update_job(db_path, source.id, status="uploaded", file_path=str(root / "source.mp4"))
            edit = await create_edit_job(db_path, source.id, 1)
            await update_edit_job(
                db_path,
                edit.id,
                status="rendering",
                render_status_message_id=42,
            )
            update = _update()
            update.effective_message.reply_to_message = SimpleNamespace(
                message_id=42,
                chat_id=1,
                text=f"🎬 Preparing render — Job #{edit.id}",
                caption=None,
            )
            render_work = WorkQueue(
                name="render", workers=1, capacity=4, per_user_capacity=2,
            )
            started = asyncio.Event()
            release = asyncio.Event()

            async def hold_render():
                started.set()
                await release.wait()

            render_work.submit(
                user_id=1,
                label=f"render:{edit.id}",
                factory=hold_render,
            )
            render_work.start()
            await asyncio.wait_for(started.wait(), timeout=1)
            context = _context(
                _settings(),
                db_path=db_path,
                render_work=render_work,
                download_work=None,
                metadata_work=None,
            )
            with patch.object(
                __import__("media_bot.__main__", fromlist=["SUPERVISOR_STATE_PATH"]),
                "SUPERVISOR_STATE_PATH",
                root / "supervisor-state.json",
            ) as state_path:
                state_path.write_text(
                    json.dumps({
                        "state": "running",
                        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                        "child_pid": 123,
                    }),
                    encoding="utf-8",
                )
                await fix_command(update, context)

            release.set()
            await render_work.stop()

            report = update.effective_message.reply_text.await_args.args[0]
            self.assertIn("Supervisor: running", report)
            self.assertIn("still running", report)
            self.assertIn("No code repair", report)

    def test_render_preparation_context_names_resolved_stages(self):
        from media_bot.__main__ import _render_preparation_text

        text = _render_preparation_text(
            SimpleNamespace(id=7),
            SimpleNamespace(title="Original clip", url="https://example.com/video"),
            SimpleNamespace(name="Roast preset"),
            auto_captions=True,
            voice_text="generated roast",
            voice_mode="swearify",
            voice_outro="like_subscribe",
            watermark_mode="remove",
            watermark_position="auto",
            banner_path="/tmp/banner.png",
            channel_banner=True,
        )
        self.assertIn("Original clip", text)
        self.assertIn("Roast preset", text)
        self.assertIn("automatic candidate review", text)
        self.assertIn("Swearify roast", text)
        self.assertIn("like & subscribe plug", text)
        self.assertIn("Auto Hashtags", text)

    async def test_watermark_review_sends_a_swipeable_candidate_album(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previews = []
            for index in range(1, 4):
                preview = root / f"candidate-{index}.jpg"
                preview.write_bytes(b"jpeg")
                previews.append(preview)
            analysis = WatermarkAnalysis(
                width=320,
                height=180,
                sample_count=3,
                candidates=tuple(
                    WatermarkCandidate(index, 10, 10, 30, 20, .8, .8, .9)
                    for index in range(1, 4)
                ),
                selected=(),
                requires_review=True,
                duration_seconds=3,
            )
            message = SimpleNamespace(
                chat_id=1,
                reply_text=AsyncMock(),
                reply_photo=AsyncMock(),
            )
            context = SimpleNamespace(
                bot=SimpleNamespace(send_media_group=AsyncMock()),
            )

            await _send_watermark_review_album(
                message,
                context,
                SimpleNamespace(id=9, watermark_candidates="[]"),
                analysis,
                previews,
            )

            context.bot.send_media_group.assert_awaited_once()
            media = context.bot.send_media_group.await_args.kwargs["media"]
            self.assertEqual(len(media), 3)
            self.assertEqual(message.reply_photo.await_count, 0)
            message.reply_text.assert_awaited_once()

    async def test_report_is_ticket_only_even_for_enabled_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = Path(directory) / "errors"
            update = _update()
            context = _context(_settings(repair_enabled=True))
            context.args = ["download", "failed"]
            with (
                patch("media_bot.__main__.ERRORS_DIR", errors),
                patch("media_bot.__main__.recent_events", return_value=[]),
                patch("media_bot.__main__.append_event"),
                patch(
                    "media_bot.__main__.invoke_opencode_fix",
                    new=AsyncMock(),
                ) as invoke_agent,
            ):
                await report_command(update, context)

            invoke_agent.assert_not_awaited()
            reports = list(errors.glob("report_*.json"))
            self.assertEqual(len(reports), 1)
            self.assertEqual(json.loads(reports[0].read_text())["issue"], "download failed")
            self.assertIn(
                "No code was executed",
                update.effective_message.reply_text.await_args.args[0],
            )

    async def test_group_chatter_without_url_is_ignored(self):
        update = _update(user_id=9, chat_id=-1001, chat_type="supergroup")
        update.effective_message.text = "hello everyone"
        settings = _settings(
            allowed_user_ids=frozenset(), allowed_chat_ids=frozenset({-1001}),
        )
        context = _context(
            settings,
            ytdlp=Path("yt-dlp"),
            gallerydl=Path("gallery-dl"),
            db_path=Path("unused.db"),
            storage_dir=Path("unused"),
        )

        await handle_url(update, context)

        update.effective_message.reply_text.assert_not_awaited()

    async def test_starting_settings_flow_clears_pool_flow(self):
        update = _update(chat_id=44)
        context = _context(_settings())
        context.user_data.update({"settings_flow": object(), "pool_flow": object()})

        _bind_flow_context(update, context, owner="settings")

        self.assertIn("settings_flow", context.user_data)
        self.assertNotIn("pool_flow", context.user_data)
        self.assertEqual(context.user_data["_flow_chat_id"], 44)


class DurableJobStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_dir = self.root / "jobs"
        self.storage_dir.mkdir()
        self.db_path = self.root / "media.db"
        await init_db(self.db_path)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_cancel_command_marks_owned_queued_download_failed(self):
        job = await create_job(self.db_path, "https://example.com/v", 1, 1)
        download_work = WorkQueue(
            name="download", workers=1, capacity=4, per_user_capacity=2,
        )

        async def should_not_run():
            raise AssertionError("cancelled queued work ran")

        download_work.submit(
            user_id=1, label=f"download:{job.id}", factory=should_not_run,
        )
        render_work = WorkQueue(
            name="render", workers=1, capacity=4, per_user_capacity=2,
        )
        update = _update()
        context = _context(
            _settings(),
            db_path=self.db_path,
            download_work=download_work,
            render_work=render_work,
        )
        context.args = [f"download:{job.id}"]

        await cancel_job_command(update, context)

        updated = await get_job(self.db_path, job.id)
        self.assertEqual(updated.status, "failed")
        self.assertIn("cancelled", updated.error_message)
        self.assertIn("Cancellation requested", update.effective_message.reply_text.await_args.args[0])

    async def test_unexpected_render_error_is_persisted_as_failed(self):
        source_path = self.storage_dir / "source.mp4"
        source_path.write_bytes(b"not read because render is mocked")
        source = await create_job(self.db_path, "https://example.com/v", 1, 1)
        await update_job(
            self.db_path,
            source.id,
            status="uploaded",
            file_path=str(source_path),
            file_size=source_path.stat().st_size,
        )
        edit = await create_edit_job(self.db_path, source.id, 1)
        await update_edit_job(self.db_path, edit.id, file_path=str(source_path))
        status_message = MagicMock()
        status_message.edit_text = AsyncMock()
        update = _update()
        update.effective_message.reply_text = AsyncMock(return_value=status_message)
        context = _context(
            _settings(), db_path=self.db_path, storage_dir=self.storage_dir,
        )

        with patch(
            "media_bot.__main__.render_edit",
            new=AsyncMock(side_effect=RuntimeError("codec exploded")),
        ):
            await _render_edit_job(update, context, edit.id)

        updated = await get_edit_job(self.db_path, edit.id)
        self.assertEqual(updated.status, "failed")
        self.assertEqual(updated.error_message, "codec exploded")
        status_message.edit_text.assert_awaited()


if __name__ == "__main__":
    unittest.main()
