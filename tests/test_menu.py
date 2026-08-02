import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from media_bot.menu import Menu
from media_bot.settings_ui import _edit_message
from media_bot import diagnostics, fix_agent


class MenuTests(unittest.TestCase):
    def test_toggle_targets_next_state(self):
        markup = (
            Menu()
            .toggle("Auto Captions", False, "edit:set:auto_captions:yes")
            .toggle("Remove Watermark", True, "edit:set:watermark_removal:no")
            .back("download:actions:1")
            .home("settings:menu")
            .build()
        )

        buttons = [row[0] for row in markup.inline_keyboard]
        self.assertEqual(buttons[0].text, "❌ Auto Captions")
        self.assertEqual(buttons[0].callback_data, "edit:set:auto_captions:yes")
        self.assertEqual(buttons[1].text, "✅ Remove Watermark")
        self.assertEqual(buttons[1].callback_data, "edit:set:watermark_removal:no")
        self.assertEqual(buttons[2].text, "← Back")
        self.assertEqual(buttons[3].text, "🏠 Home")

    def test_media_menu_falls_back_to_caption_edit(self):
        async def run():
            query = MagicMock()
            query.edit_message_text = AsyncMock(side_effect=RuntimeError("message has no text"))
            query.edit_message_caption = AsyncMock()

            await _edit_message(query, "Edit menu", reply_markup="keyboard")

            query.edit_message_caption.assert_awaited_once_with(
                caption="Edit menu",
                reply_markup="keyboard",
            )

        asyncio.run(run())

    def test_recent_events_keeps_global_and_reporting_user_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            with patch.object(diagnostics, "EVENTS_PATH", events_path):
                diagnostics.append_event("update", "mine", user_id=1)
                diagnostics.append_event("update", "theirs", user_id=2)
                diagnostics.append_event("process_exit", "global", scope="global_health")
                diagnostics.append_event("process_output", "unscoped")

                events = diagnostics.recent_events(user_id=1)

            self.assertEqual([event["message"] for event in events], ["mine", "global"])

    def test_diagnostics_redact_secrets_and_url_queries(self):
        redacted = diagnostics.redact_sensitive(
            "token=very-secret-value https://example.test/path?token=secret#fragment"
        )
        self.assertNotIn("very-secret-value", redacted)
        self.assertNotIn("?token=", redacted)
        self.assertNotIn("#fragment", redacted)
        self.assertIn("https://example.test/path", redacted)

    def test_opencode_fix_accepts_provider_model(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(fix_agent, "FIX_SCRIPTS_DIR", root / "fixes"):
                    script_path = await fix_agent.invoke_opencode_fix(
                        {"id": "test", "message": "broken", "traceback": "trace"},
                        root,
                        model="openai/gpt-5",
                    )
                script = Path(script_path).read_text()
                self.assertIn("opencode run --model 'openai/gpt-5'", script)

        asyncio.run(run())

    def test_opencode_fix_rejects_unsafe_model(self):
        with self.assertRaises(ValueError):
            fix_agent.validate_model("openai/gpt-5; touch /tmp/bad")


if __name__ == "__main__":
    unittest.main()
