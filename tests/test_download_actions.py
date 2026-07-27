import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from media_bot.storage import create_job, get_edit_job, init_db, update_job


def _make_update(callback_data: str, user_id: int = 1):
    query = MagicMock()
    query.data = callback_data
    query.from_user = SimpleNamespace(id=user_id)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = SimpleNamespace(chat_id=2, message_id=99)
    update = MagicMock()
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_message = None
    update.message = None
    return update, query


def _make_context(db_path: Path, storage_dir: Path, allowed_user_ids=None):
    settings = SimpleNamespace(
        allowed_user_ids=allowed_user_ids or {1},
        allowed_chat_ids=set(),
        token_expiry_minutes=30,
        timeout_seconds=120,
    )
    context = MagicMock()
    context.application.bot_data = {
        "settings": settings,
        "db_path": db_path,
        "storage_dir": storage_dir,
    }
    context.user_data = {}
    return context


class DownloadEditActionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "test.db"
        self.storage_dir = self.root / "storage"
        self.storage_dir.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_edit_opens_editconfig_and_creates_job(self):
        from media_bot.__main__ import download_callback

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com/v", 1, 2)
            source = self.storage_dir / "source.mp4"
            source.write_bytes(b"fake-video")
            await update_job(self.db_path, job.id, file_path=str(source), status="uploaded")

            update, query = _make_update(f"download:edit:{job.id}")
            context = _make_context(self.db_path, self.storage_dir)

            with patch("media_bot.__main__.show_editconfig_menu", new_callable=AsyncMock) as show_menu:
                await download_callback(update, context)

            query.answer.assert_awaited()
            show_menu.assert_awaited_once()
            args, kwargs = show_menu.await_args
            self.assertEqual(args[2], context.user_data["settings_flow"]["edit_id"])
            self.assertIn("Edit job #", kwargs["intro"])

            edit_id = context.user_data["settings_flow"]["edit_id"]
            edit = await get_edit_job(self.db_path, edit_id)
            self.assertIsNotNone(edit)
            self.assertEqual(edit.source_job_id, job.id)
            self.assertEqual(edit.status, "pending")
            self.assertTrue(Path(edit.file_path).is_file())

        asyncio.run(run())

    def test_gallery_action_is_gone(self):
        from media_bot.__main__ import download_callback

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com/v", 1, 2)
            source = self.storage_dir / "source.mp4"
            source.write_bytes(b"fake-video")
            await update_job(self.db_path, job.id, file_path=str(source), status="uploaded")

            update, query = _make_update(f"download:gallery:{job.id}")
            context = _make_context(self.db_path, self.storage_dir)
            await download_callback(update, context)

            query.edit_message_text.assert_not_awaited()
            query.answer.assert_awaited_once()
            self.assertIn("removed", query.answer.await_args.args[0].lower())

        asyncio.run(run())

    def test_secure_link_keyboard_has_no_gallery(self):
        from media_bot.__main__ import _send_secure_link

        async def run():
            await init_db(self.db_path)
            status = MagicMock()
            status.edit_text = AsyncMock()
            with patch("media_bot.storage.list_presets", new_callable=AsyncMock, return_value=[]), \
                 patch("media_bot.storage.get_or_create_user_settings", new_callable=AsyncMock) as gus:
                gus.return_value = SimpleNamespace(preset_name=None)
                await _send_secure_link(status, 42, self.db_path, 1)

            status.edit_text.assert_awaited_once()
            markup = status.edit_text.await_args.kwargs["reply_markup"]
            labels = [btn.text for row in markup.inline_keyboard for btn in row]
            self.assertNotIn("📲 Save to Gallery (iOS)", labels)
            self.assertIn("✂️ Edit", labels)
            self.assertIn("⬇️ Download Original", labels)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
