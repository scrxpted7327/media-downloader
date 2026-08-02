import os
import unittest
from unittest.mock import patch

from media_bot.config import Settings


class SettingsTests(unittest.TestCase):
    def _environment(self, **overrides):
        values = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "TELEGRAM_ALLOWED_CHAT_IDS": "",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

    def test_download_server_defaults_to_loopback_without_public_origin(self):
        with self._environment():
            settings = Settings.from_environment()
        self.assertEqual(settings.download_bind_host, "127.0.0.1")
        self.assertIsNone(settings.download_public_origin)

    def test_validates_and_normalizes_public_origin(self):
        with self._environment(
            MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN="https://media.example.test/",
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.download_public_origin, "https://media.example.test")

    def test_rejects_insecure_or_credentialed_public_origin(self):
        for origin in (
            "http://media.example.test",
            "https://user:pass@media.example.test",
            "https://media.example.test/base",
            "https://media.example.test?token=secret",
        ):
            with self.subTest(origin=origin), self._environment(
                MEDIA_BOT_DOWNLOAD_PUBLIC_ORIGIN=origin,
            ):
                with self.assertRaises(ValueError):
                    Settings.from_environment()

    def test_legacy_domain_is_upgraded_to_https_origin(self):
        with self._environment(MEDIA_BOT_DOWNLOAD_DOMAIN="media.example.test"):
            settings = Settings.from_environment()
        self.assertEqual(settings.download_public_origin, "https://media.example.test")

    def test_allowed_users_are_admins_when_admin_list_is_not_configured(self):
        with self._environment(TELEGRAM_ALLOWED_USER_IDS="1, 2"):
            settings = Settings.from_environment()

        self.assertEqual(settings.admin_user_ids, frozenset({1, 2}))

    def test_explicit_admin_list_overrides_allowed_user_fallback(self):
        with self._environment(
            TELEGRAM_ALLOWED_USER_IDS="1, 2",
            TELEGRAM_ADMIN_USER_IDS="2",
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.admin_user_ids, frozenset({2}))

    def test_repair_is_disabled_by_default_and_requires_a_valid_boolean(self):
        with self._environment():
            settings = Settings.from_environment()
        self.assertFalse(settings.repair_enabled)

        with self._environment(MEDIA_BOT_ENABLE_REPAIR="yes"):
            settings = Settings.from_environment()
        self.assertTrue(settings.repair_enabled)

        with self._environment(MEDIA_BOT_ENABLE_REPAIR="sometimes"):
            with self.assertRaisesRegex(ValueError, "MEDIA_BOT_ENABLE_REPAIR"):
                Settings.from_environment()


if __name__ == "__main__":
    unittest.main()
