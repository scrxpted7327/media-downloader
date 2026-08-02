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
    presets_command,
    settings_photo_handler,
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

    def test_presets_command_opens_preset_list(self):
        async def run():
            await init_db(self.db_path)
            await create_preset(self.db_path, 1, "My preset")
            context = _make_context(self.db_path, self.storage_dir)
            message = MagicMock()
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.callback_query = None
            update.effective_user = SimpleNamespace(id=1)

            await presets_command(update, context)

            self.assertEqual(context.user_data["settings_flow"].action, _State.PRESET_LIST)
            message.reply_text.assert_awaited_once()
            self.assertIn("Your presets", message.reply_text.await_args.args[0])

        asyncio.run(run())

    def test_active_text_input_consumes_url_before_downloader(self):
        from media_bot.__main__ import _message_router

        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=1, user_id=1)
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = {
                "action": "editconfig",
                "edit_id": edit.id,
                "field_name": "voice_text",
            }
            message = MagicMock()
            message.text = "https://example.com/narration"
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_message = message

            with (
                patch("media_bot.__main__._authorized", return_value=True),
                patch("media_bot.__main__.show_editconfig_menu", new_callable=AsyncMock),
                patch("media_bot.__main__.handle_url", new_callable=AsyncMock) as download,
            ):
                await _message_router(update, context)

            updated = await get_edit_job(self.db_path, edit.id)
            self.assertEqual(updated.voice_text, "https://example.com/narration")
            download.assert_not_awaited()

        asyncio.run(run())

    def test_active_preset_watermark_text_is_accepted(self):
        from media_bot.__main__ import _message_router

        async def run():
            await init_db(self.db_path)
            preset = await create_preset(self.db_path, 1, "Watermarked")
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = FlowState(
                action=_State.PRESET_EDIT_FIELD,
                data={"preset_id": preset.id, "field_name": "watermark_text"},
            )
            message = MagicMock()
            message.text = "@PursuitFiles4"
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_message = message
            update.effective_user = SimpleNamespace(id=1)

            with (
                patch("media_bot.__main__._authorized", return_value=True),
                patch("media_bot.__main__.handle_url", new_callable=AsyncMock) as download,
            ):
                await _message_router(update, context)

            updated = next(item for item in await list_presets(self.db_path, 1) if item.id == preset.id)
            self.assertEqual(updated.watermark_text, "@PursuitFiles4")
            download.assert_not_awaited()

        asyncio.run(run())

    def test_active_input_rejects_wrong_message_without_downloading(self):
        from media_bot.__main__ import _message_router

        async def run():
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = {
                "action": "editconfig",
                "edit_id": 1,
                "field_name": "voice_text",
            }
            message = MagicMock()
            message.text = None
            message.photo = []
            message.document = SimpleNamespace(mime_type="application/pdf")
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_message = message

            with (
                patch("media_bot.__main__._authorized", return_value=True),
                patch("media_bot.__main__.handle_url", new_callable=AsyncMock) as download,
            ):
                await _message_router(update, context)

            message.reply_text.assert_awaited_once()
            download.assert_not_awaited()

        asyncio.run(run())

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
            self.assertIn("💾 Save Original to Pool", labels)

        asyncio.run(run())

    def test_download_preset_uses_shared_render_pipeline(self):
        from media_bot.__main__ import download_callback

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com/v", 1, 2)
            source = self.storage_dir / "source.mp4"
            source.write_bytes(b"fake-video")
            await update_job(self.db_path, job.id, file_path=str(source), status="uploaded")
            preset = await create_preset(
                self.db_path, 1, "clean", watermark_removal=True,
                watermark_position="auto",
            )
            update, query = _make_update(f"download:preset:{job.id}:{preset.id}")
            context = _make_context(self.db_path, self.storage_dir)
            scheduled = []

            def capture(coro):
                scheduled.append(coro)
                coro.close()

            with patch("media_bot.__main__.asyncio.create_task", side_effect=capture):
                await download_callback(update, context)

            self.assertEqual(len(scheduled), 1)
            edit_id = max(item.id for item in await _list_edits(self.db_path))
            edit = await get_edit_job(self.db_path, edit_id)
            self.assertEqual(edit.preset_id, preset.id)
            self.assertTrue(edit.watermark_removal)
            query.edit_message_text.assert_awaited_once()

        async def _list_edits(db_path):
            import aiosqlite
            from media_bot.storage import get_edit_job
            async with aiosqlite.connect(db_path) as db:
                rows = await (await db.execute("SELECT id FROM edit_jobs")).fetchall()
            return [await get_edit_job(db_path, row[0]) for row in rows]

        asyncio.run(run())

    def test_download_preset_from_photo_message_still_starts_render(self):
        from media_bot.__main__ import download_callback

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com/v", 1, 2)
            source = self.storage_dir / "source.mp4"
            source.write_bytes(b"fake-video")
            await update_job(self.db_path, job.id, file_path=str(source), status="uploaded")
            preset = await create_preset(self.db_path, 1, "photo preset")
            update, query = _make_update(f"download:preset:{job.id}:{preset.id}")
            query.message.photo = [SimpleNamespace()]
            query.edit_message_text.side_effect = RuntimeError("message has no text")
            query.edit_message_caption = AsyncMock()
            context = _make_context(self.db_path, self.storage_dir)
            scheduled = []

            def capture(coro):
                scheduled.append(coro)
                coro.close()

            with patch("media_bot.__main__.asyncio.create_task", side_effect=capture):
                await download_callback(update, context)

            query.edit_message_caption.assert_awaited_once()
            self.assertEqual(len(scheduled), 1)

        asyncio.run(run())

    def test_rendered_edit_pool_button_toggles_with_confirmation(self):
        from media_bot.__main__ import download_callback

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com/v", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            rendered = self.storage_dir / "edit.mp4"
            rendered.write_bytes(b"rendered")
            await update_edit_job(
                self.db_path,
                edit.id,
                file_path=str(rendered),
                file_size=rendered.stat().st_size,
                status="rendered",
            )
            context = _make_context(self.db_path, self.storage_dir)

            update, query = _make_update(f"download:editsave:{edit.id}")
            query.edit_message_reply_markup = AsyncMock()
            await download_callback(update, context)
            saved_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
            self.assertEqual(
                saved_markup.inline_keyboard[0][0].text,
                "🗑️ Unsave from Pool",
            )

            update, query = _make_update(f"download:editunsaveconfirm:{edit.id}")
            query.edit_message_reply_markup = AsyncMock()
            await download_callback(update, context)
            confirm_markup = query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
            self.assertEqual(
                confirm_markup.inline_keyboard[0][0].text,
                "⚠️ Confirm Unsave",
            )

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
            self.assertIn("🔄 Watermark Mode [Keep]", labels)
            self.assertIn("❌ Channel Banner", labels)
            self.assertIn(f"editcfg:{edit.id}:set:auto_captions:yes", callbacks)
            self.assertIn(f"editcfg:{edit.id}:watermark_mode", callbacks)
            self.assertIn(f"editcfg:{edit.id}:set:channel_banner:yes", callbacks)

        asyncio.run(run())

    def test_preset_menu_has_no_manual_caption_text(self):
        from media_bot.settings_ui import _build_config_rows

        rows = _build_config_rows(
            {"caption_text": "legacy text"},
            field_prefix="preset:field:1",
            toggle_prefix="preset:set:1",
        )
        labels = [button.text for row in rows for button in row]
        callbacks = [button.callback_data for row in rows for button in row]
        self.assertFalse(any("Caption Text" in label for label in labels))
        self.assertFalse(any(value.endswith(":caption_text") for value in callbacks))

    def test_swap_mode_shows_replacement_text_setting(self):
        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=1, user_id=1)
            await update_edit_job(
                self.db_path, edit.id,
                watermark_mode="swap", watermark_text="@my_channel",
            )
            edit = await get_edit_job(self.db_path, edit.id)

            markup = build_editconfig_keyboard(edit)
            labels = [button.text for row in markup.inline_keyboard for button in row]
            callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

            self.assertIn("🔄 Watermark Mode [Swap]", labels)
            self.assertIn("✏️ Replacement Watermark [@my_channel]", labels)
            self.assertIn(f"editcfg:{edit.id}:watermark_text", callbacks)

        asyncio.run(run())

    def test_watermark_text_input_updates_edit_job(self):
        from media_bot.__main__ import editconfig_text

        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=1, user_id=1)
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = {
                "action": "editconfig",
                "edit_id": edit.id,
                "field_name": "watermark_text",
            }
            update = MagicMock()
            update.message = MagicMock()
            update.message.text = "@alexis.s5"
            update.message.reply_text = AsyncMock()

            with patch(
                "media_bot.__main__.show_editconfig_menu",
                new_callable=AsyncMock,
            ) as show_menu:
                handled = await editconfig_text(update, context)

            self.assertTrue(handled)
            updated = await get_edit_job(self.db_path, edit.id)
            self.assertEqual(updated.watermark_text, "@alexis.s5")
            show_menu.assert_awaited_once()

        asyncio.run(run())

    def test_photo_handler_ignores_editconfig_dict_flow(self):
        async def run():
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = {
                "action": "editconfig",
                "edit_id": 1,
                "field_name": "watermark_text",
            }
            update = MagicMock()
            update.message = MagicMock()

            handled = await settings_photo_handler(update, context)

            self.assertFalse(handled)

        asyncio.run(run())

    def test_edit_config_banner_upload_updates_edit_job(self):
        async def run():
            await init_db(self.db_path)
            edit = await create_edit_job(self.db_path, source_job_id=1, user_id=1)
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = {
                "action": "editconfig",
                "edit_id": edit.id,
                "field_name": "banner_path",
            }
            telegram_file = MagicMock()

            async def download(destination):
                Path(destination).write_bytes(b"image")

            telegram_file.download_to_drive = AsyncMock(side_effect=download)
            photo = SimpleNamespace(get_file=AsyncMock(return_value=telegram_file))
            message = MagicMock()
            message.photo = [photo]
            message.document = None
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_user = SimpleNamespace(id=1)

            with patch(
                "media_bot.settings_ui.show_editconfig_menu", new_callable=AsyncMock,
            ) as show_menu:
                handled = await settings_photo_handler(update, context)

            self.assertTrue(handled)
            updated = await get_edit_job(self.db_path, edit.id)
            expected = self.storage_dir / "banners" / f"banner_1_{edit.id}.jpg"
            self.assertEqual(updated.banner_path, str(expected))
            self.assertTrue(expected.is_file())
            self.assertNotIn("field_name", context.user_data["settings_flow"])
            show_menu.assert_awaited_once_with(update, context, edit.id)

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

    def test_banner_upload_accepts_an_image_sent_as_a_document(self):
        async def run():
            context = _make_context(self.db_path, self.storage_dir)
            context.user_data["settings_flow"] = FlowState(
                action=_State.PRESET_CREATE_BANNER,
            )
            telegram_file = MagicMock()
            telegram_file.download_to_drive = AsyncMock()
            document = SimpleNamespace(
                file_name="uploaded-banner.png",
                file_size=2_100_000,
                mime_type="image/png",
                get_file=AsyncMock(return_value=telegram_file),
            )
            message = MagicMock()
            message.photo = []
            message.document = document
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_user = SimpleNamespace(id=1)

            handled = await settings_photo_handler(update, context)

            self.assertTrue(handled)
            destination = telegram_file.download_to_drive.await_args.args[0]
            self.assertEqual(destination, self.storage_dir / "banners" / "banner_1.png")
            self.assertEqual(
                context.user_data["settings_flow"].data["banner_path"],
                str(destination),
            )
            self.assertEqual(
                context.user_data["settings_flow"].action,
                _State.PRESET_CREATE_BANNER_MENU,
            )
            message.reply_text.assert_awaited_once()

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
