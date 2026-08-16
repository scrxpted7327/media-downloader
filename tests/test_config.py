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
        self.assertEqual(settings.media_api_bind_host, "127.0.0.1")
        self.assertEqual(settings.media_api_port, 8082)
        self.assertIsNone(settings.media_api_key)

    def test_private_media_api_configuration_is_separate_from_download_links(self):
        with self._environment(
            MEDIA_BOT_API_BIND_HOST="127.0.0.1",
            MEDIA_BOT_API_PORT="9092",
            MEDIA_BOT_API_KEY="media-key",
            WATCHMYWALLET_INTERNAL_SIGNING_SECRET="signing-key",
            WATCHMYWALLET_ACTING_CONTEXT_MAX_AGE_SECONDS="45",
            WATCHMYWALLET_ACTING_CONTEXT_CLOCK_SKEW_SECONDS="7",
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.media_api_port, 9092)
        self.assertEqual(settings.media_api_key, "media-key")
        self.assertEqual(settings.internal_signing_secret, "signing-key")
        self.assertEqual(settings.acting_context_max_age_seconds, 45)
        self.assertEqual(settings.acting_context_clock_skew_seconds, 7)

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

    def test_metadata_defaults_are_isolated_from_global_codex_config(self):
        with self._environment():
            settings = Settings.from_environment()
        self.assertEqual(settings.metadata_model, "gpt-5.6-luna")
        self.assertEqual(settings.metadata_reasoning_effort, "max")
        self.assertEqual(settings.metadata_codex_executable, "codex")
        self.assertEqual(settings.metadata_workers, 1)
        self.assertEqual(settings.metadata_timeout_seconds, 1800)
        self.assertIsNone(settings.metadata_codex_home)

    def test_metadata_settings_can_be_overridden_without_api_credentials(self):
        with self._environment(
            MEDIA_BOT_AUTO_HASHTAGS_MODEL="test-model",
            MEDIA_BOT_AUTO_HASHTAGS_REASONING_EFFORT="high",
            MEDIA_BOT_AUTO_HASHTAGS_CODEX_EXECUTABLE="/opt/codex",
            MEDIA_BOT_AUTO_HASHTAGS_WORKERS="2",
            MEDIA_BOT_AUTO_HASHTAGS_TIMEOUT_SECONDS="120",
            MEDIA_BOT_AUTO_HASHTAGS_CODEX_HOME="~/.codex-test",
        ):
            settings = Settings.from_environment()
        self.assertEqual(settings.metadata_model, "test-model")
        self.assertEqual(settings.metadata_reasoning_effort, "high")
        self.assertEqual(settings.metadata_codex_executable, "/opt/codex")
        self.assertEqual(settings.metadata_workers, 2)
        self.assertEqual(settings.metadata_timeout_seconds, 120)
        self.assertEqual(settings.metadata_codex_home.name, ".codex-test")


if __name__ == "__main__":
    unittest.main()
