import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from media_bot.settings_ui import (
    FlowState,
    _State,
    _handle_preset_create_callback,
    build_editconfig_keyboard,
)
from media_bot.storage import (
    create_edit_job,
    create_job,
    create_preset,
    get_edit_job,
    init_db,
    list_presets,
    update_edit_job,
    update_job,
)


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

    def test_edit_menu_has_inline_toggles_and_no_caption_text(self):
        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=1, user_id=1)

            markup = build_editconfig_keyboard(edit)
            buttons = [button for row in markup.inline_keyboard for button in row]
            labels = [button.text for button in buttons]
            callbacks = [button.callback_data for button in buttons]

            self.assertFalse(any("Caption Text" in label for label in labels))
            self.assertFalse(any(label.startswith("🎤 Voice Name") for label in labels))
            self.assertFalse(any(label.startswith("📝 Voice Text") for label in labels))
            self.assertFalse(any(label.startswith("⏩ Voice Speed") for label in labels))
            self.assertTrue(any(label.startswith("🎤 Voice Settings [") for label in labels))
            self.assertIn("❌ Auto Captions", labels)
            self.assertIn("❌ Remove Watermark", labels)
            self.assertIn("❌ Channel Banner", labels)
            self.assertIn(f"editcfg:{edit.id}:set:auto_captions:yes", callbacks)
            self.assertIn(f"editcfg:{edit.id}:set:watermark_removal:yes", callbacks)
            self.assertIn(f"editcfg:{edit.id}:set:channel_banner:yes", callbacks)

        asyncio.run(run())

    def test_edit_menu_returns_to_source_download(self):
        from media_bot.settings_ui import handle_editconfig_callback

        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=42, user_id=1)
            update, _ = _make_update(f"editcfg:{edit.id}:download")
            context = _make_context(self.db_path, self.storage_dir)

            result = await handle_editconfig_callback(update, context)

            self.assertEqual(result, ("download", 42))

        asyncio.run(run())

    def test_edit_toggle_updates_and_refreshes_in_one_callback(self):
        from media_bot.settings_ui import handle_editconfig_callback

        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=42, user_id=1)
            update, query = _make_update(f"editcfg:{edit.id}:set:auto_captions:yes")
            context = _make_context(self.db_path, self.storage_dir)

            await handle_editconfig_callback(update, context)

            updated = await get_edit_job(self.db_path, edit.id)
            self.assertTrue(updated.auto_captions)
            query.edit_message_text.assert_awaited()
            markup = query.edit_message_text.await_args.kwargs["reply_markup"]
            labels = [button.text for row in markup.inline_keyboard for button in row]
            self.assertIn("✅ Auto Captions", labels)

        asyncio.run(run())

    def test_channel_banner_buttons_finalize_preset_creation(self):
        async def run():
            await init_db(self.db_path)
            update, query = _make_update(
                f"preset_create:yes:{_State.PRESET_CREATE_CHANNEL_BANNER}"
            )
            context = _make_context(self.db_path, self.storage_dir)
            flow = FlowState(
                action=_State.PRESET_CREATE_CHANNEL_BANNER,
                data={"name": "test", "caption_color": "white"},
            )
            context.user_data["settings_flow"] = flow

            await _handle_preset_create_callback(update, context, flow, query)

            presets = await list_presets(self.db_path, 1)
            self.assertEqual(len(presets), 1)
            self.assertEqual(presets[0].name, "test")
            self.assertTrue(presets[0].channel_banner)
            self.assertEqual(flow.action, _State.PRESET_LIST)

        asyncio.run(run())

    def test_edit_config_can_overwrite_existing_preset(self):
        from media_bot.settings_ui import handle_editconfig_callback

        async def run():
            await init_db(self.db_path)
            preset = await create_preset(
                self.db_path,
                1,
                "Existing",
                caption_color="white",
                auto_captions=False,
            )
            edit = await create_edit_job(self.db_path, source_job_id=42, user_id=1)
            await update_edit_job(
                self.db_path,
                edit.id,
                caption_color="#800080",
                auto_captions=True,
            )
            update, _ = _make_update(
                f"editcfg:{edit.id}:save_preset:{preset.id}"
            )
            context = _make_context(self.db_path, self.storage_dir)

            await handle_editconfig_callback(update, context)

            updated = (await list_presets(self.db_path, 1))[0]
            self.assertEqual(updated.caption_color, "#800080")
            self.assertTrue(updated.auto_captions)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
