import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from media_bot.settings_ui import (
    FlowState,
    _State,
    _banner_asset_path,
    settings_photo_handler,
    settings_text_handler,
)


def _context(storage_dir: Path):
    context = MagicMock()
    context.application.bot_data = {
        "db_path": storage_dir / "test.db",
        "storage_dir": storage_dir,
    }
    context.user_data = {}
    return context


def _text_update(text: str, user_id: int = 1):
    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.message = message
    update.callback_query = None
    update.effective_user = SimpleNamespace(id=user_id)
    return update, message


class BannerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.storage_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_banner_asset_paths_are_user_scoped_and_immutable(self):
        first = _banner_asset_path(self.storage_dir, 7, "preset_draft", ".png")
        second = _banner_asset_path(self.storage_dir, 7, "preset_draft", ".png")

        self.assertNotEqual(first, second)
        self.assertEqual(
            first.parent,
            self.storage_dir / "banners" / "user_7" / "preset_draft",
        )

    def test_banner_assets_have_a_per_user_storage_ceiling(self):
        existing = _banner_asset_path(self.storage_dir, 7, "profile", ".png")
        existing.write_bytes(b"image")
        with (
            patch("media_bot.settings_ui._MAX_BANNER_ASSETS_PER_USER", 1),
            self.assertRaisesRegex(ValueError, "quota"),
        ):
            _banner_asset_path(self.storage_dir, 7, "preset_draft", ".png")

    def test_create_banner_rejects_remote_urls_and_local_paths(self):
        async def run():
            for value in ("https://example.test/banner.png", "/etc/passwd"):
                with self.subTest(value=value):
                    context = _context(self.storage_dir)
                    flow = FlowState(action=_State.PRESET_CREATE_BANNER)
                    context.user_data["settings_flow"] = flow
                    update, message = _text_update(value)

                    handled = await settings_text_handler(update, context)

                    self.assertTrue(handled)
                    self.assertEqual(flow.action, _State.PRESET_CREATE_BANNER)
                    self.assertNotIn("banner_path", flow.data)
                    self.assertNotIn("banner_url", flow.data)
                    self.assertIn(
                        "not accepted",
                        message.reply_text.await_args.args[0],
                    )

        asyncio.run(run())

    def test_profile_reuse_resolves_the_latest_scoped_profile_asset(self):
        async def run():
            profile = _banner_asset_path(self.storage_dir, 1, "profile", ".png")
            Image.new("RGB", (8, 8), "blue").save(profile, format="PNG")
            context = _context(self.storage_dir)
            flow = FlowState(action=_State.PRESET_CREATE_BANNER)
            context.user_data["settings_flow"] = flow
            update, _ = _text_update("profile")

            handled = await settings_text_handler(update, context)

            self.assertTrue(handled)
            self.assertEqual(flow.action, _State.PRESET_CREATE_BANNER_MENU)
            self.assertEqual(flow.data["banner_path"], str(profile))

        asyncio.run(run())

    def test_invalid_uploaded_bytes_are_not_published(self):
        async def run():
            context = _context(self.storage_dir)
            context.user_data["settings_flow"] = FlowState(
                action=_State.PRESET_CREATE_BANNER,
            )
            telegram_file = MagicMock()

            async def download(destination):
                Path(destination).write_bytes(b"not an image")

            telegram_file.download_to_drive = AsyncMock(side_effect=download)
            photo = SimpleNamespace(get_file=AsyncMock(return_value=telegram_file))
            message = MagicMock()
            message.photo = [photo]
            message.document = None
            message.reply_text = AsyncMock()
            update = MagicMock()
            update.message = message
            update.effective_user = SimpleNamespace(id=1)

            handled = await settings_photo_handler(update, context)

            self.assertTrue(handled)
            self.assertNotIn(
                "banner_path",
                context.user_data["settings_flow"].data,
            )
            published = [
                path for path in (self.storage_dir / "banners").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(published, [])
            self.assertIn("not a valid", message.reply_text.await_args.args[0])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
