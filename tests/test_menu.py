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

    def test_codex_fix_accepts_provider_model(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(fix_agent, "FIX_SCRIPTS_DIR", root / "fixes"):
                    script_path = await fix_agent.invoke_codex_fix(
                        {"id": "test", "message": "broken", "traceback": "trace"},
                        root,
                        model="openai/gpt-5",
                    )
                script = Path(script_path).read_text()
                self.assertIn(
                    "'codex' exec --ephemeral --sandbox workspace-write",
                    script,
                )
                self.assertIn("--model 'openai/gpt-5'", script)
                self.assertIn('model_reasoning_effort="max"', script)

        asyncio.run(run())

    def test_codex_fix_defaults_to_luna_max(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(fix_agent, "FIX_SCRIPTS_DIR", root / "fixes"):
                    script_path = await fix_agent.invoke_codex_fix(
                        {"id": "default", "message": "broken"},
                        root,
                    )
                script = Path(script_path).read_text()
                self.assertIn("--model 'gpt-5.6-luna'", script)
                self.assertIn('model_reasoning_effort="max"', script)

        asyncio.run(run())

    def test_codex_fix_prompt_contains_operator_reason_and_mutation_instruction(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(fix_agent, "FIX_SCRIPTS_DIR", root / "fixes"):
                    script_path = await fix_agent.invoke_codex_fix(
                        {
                            "id": "operator-request",
                            "message": "database locked",
                            "stderr": "sqlite3.OperationalError: database is locked",
                        },
                        root,
                        operator_reason="repair the database crash loop",
                    )
                script = Path(script_path).read_text()
                self.assertIn("repair the database crash loop", script)
                self.assertIn("authorized mutation request", script)
                self.assertIn("leave the workspace in the corrected state", script)
                self.assertIn("database is locked", script)

        asyncio.run(run())

    def test_pending_error_files_ignore_completed_records_and_sort_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.json"
            new = root / "new.json"
            old.write_text("{}")
            new.write_text("{}")
            (root / "fixed_old.json").write_text("{}")
            (root / "failed_old.json").write_text("{}")
            (root / "unfixed_old.json").write_text("{}")
            (root / "report_user.json").write_text("{}")
            old.touch()
            new.touch()

            pending = fix_agent.pending_error_files(root)

        self.assertEqual([path.name for path in pending], ["new.json", "old.json"])

    def test_codex_fix_rejects_unsafe_model(self):
        with self.assertRaises(ValueError):
            fix_agent.validate_model("openai/gpt-5; touch /tmp/bad")

    def test_known_fix_refuses_to_mutate_when_repair_is_disabled(self):
        async def run():
            with patch(
                "media_bot.fix_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as create_process:
                result = await fix_agent.apply_known_fix(
                    {"category": "ytdlp", "message": "yt-dlp failed"},
                    Path("/tmp/tools"),
                )

            self.assertIn("disabled", result.lower())
            create_process.assert_not_awaited()

        asyncio.run(run())

    def test_dependency_fix_never_installs_an_inferred_package(self):
        async def run():
            with patch(
                "media_bot.fix_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as create_process:
                result = await fix_agent.apply_known_fix(
                    {
                        "category": "dependency",
                        "message": "ModuleNotFoundError: No module named 'surprise_pkg'",
                    },
                    Path("/tmp/tools"),
                    repair_enabled=True,
                )

            self.assertIn("surprise_pkg", result)
            self.assertIn("disabled", result.lower())
            create_process.assert_not_awaited()

        asyncio.run(run())

    def test_fix_script_execution_requires_explicit_enablement(self):
        async def run():
            with patch(
                "media_bot.fix_agent.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as create_process:
                code, output = await fix_agent.run_fix_script("ignored.sh")

            self.assertEqual(code, -1)
            self.assertIn("disabled", output.lower())
            create_process.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
