import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from media_bot.settings_ui import FlowState, _State, settings_text_handler
from media_bot.storage import (
    create_preset,
    init_db,
    list_presets,
    share_preset,
)


class PresetSharingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "media.db"
        await init_db(self.db_path)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _context(self):
        context = MagicMock()
        context.application.bot_data = {"db_path": self.db_path}
        context.user_data = {
            "settings_flow": FlowState(action=_State.PRESET_IMPORT_CODE),
        }
        return context

    @staticmethod
    def _update(text: str, user_id: int = 2):
        message = MagicMock()
        message.text = text
        message.reply_text = AsyncMock()
        return SimpleNamespace(
            message=message,
            callback_query=None,
            effective_user=SimpleNamespace(id=user_id),
        )

    async def test_shared_code_imports_a_private_copy(self):
        source = await create_preset(
            self.db_path,
            1,
            "Creator preset",
            auto_captions=True,
            caption_color="yellow",
            banner_path="/private/creator-banner.png",
            watermark_removal=False,
            watermark_mode="keep",
        )
        code = await share_preset(self.db_path, source.id, 1)
        update = self._update(code)
        context = self._context()

        handled = await settings_text_handler(update, context)

        self.assertTrue(handled)
        imported = await list_presets(self.db_path, 2)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].name, "Creator preset (shared)")
        self.assertEqual(imported[0].caption_color, "yellow")
        self.assertIsNone(imported[0].banner_path)
        self.assertFalse(imported[0].shared)
        self.assertIn(
            "banner was omitted",
            update.message.reply_text.await_args_list[0].args[0],
        )

    async def test_invalid_share_code_keeps_import_prompt_active(self):
        update = self._update("not-a-real-code")
        context = self._context()

        handled = await settings_text_handler(update, context)

        self.assertTrue(handled)
        self.assertEqual(
            context.user_data["settings_flow"].action,
            _State.PRESET_IMPORT_CODE,
        )
        self.assertEqual(await list_presets(self.db_path, 2), [])
        self.assertIn("invalid", update.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
