import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from media_bot.__main__ import (
    _admin_authorized,
    _bind_flow_context,
    _render_edit_job,
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
