import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supervisor
import restart_bot


class SupervisorReportingTests(unittest.TestCase):
    def test_restart_shutdown_signal_sends_and_acknowledges(self):
        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / "restart-ack"
            sent: list[tuple[str, str | None]] = []

            def capture(text: str, chat_id: str | None = None) -> None:
                sent.append((text, chat_id))

            with (
                patch.object(supervisor, "RESTART_ACK", ack),
                patch.object(supervisor, "_restart_notification_chat_id", return_value="-1008"),
                patch.object(supervisor, "_send_telegram_message", capture),
                patch.object(supervisor, "append_event"),
            ):
                result = asyncio.run(supervisor.notify_restart_shutdown())

            self.assertTrue(result)
            self.assertTrue(ack.is_file())
            self.assertIn("shutting down", sent[0][0])
            self.assertEqual(sent[0][1], "-1008")

    def test_restart_script_signals_supervisor_and_waits_for_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "restart-requested"
            ack = root / "restart-ack"

            def acknowledge(pid, sent_signal):
                self.assertEqual((pid, sent_signal), (123, supervisor.signal.SIGUSR1))
                ack.write_text("sent\n")

            with (
                patch.object(restart_bot, "RESTART_MARKER", marker),
                patch.object(restart_bot, "RESTART_ACK", ack),
                patch.object(restart_bot, "_supervisor_pid", return_value=123),
                patch.object(restart_bot.os, "kill", side_effect=acknowledge),
            ):
                restart_bot._request_restart_notification()

            self.assertTrue(marker.is_file())
            self.assertTrue(ack.is_file())

    def test_error_destination_prefers_explicit_chat(self):
        env = {
            "TELEGRAM_ERROR_CHAT_ID": "-1009",
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1008,-1007",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        }
        with patch.dict(supervisor.os.environ, env, clear=True):
            self.assertEqual(supervisor._notification_chat_id(), "-1009")

    def test_error_destination_defaults_to_allowed_group(self):
        env = {
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1008,-1007",
            "TELEGRAM_ALLOWED_USER_IDS": "123",
        }
        with patch.dict(supervisor.os.environ, env, clear=True):
            self.assertEqual(supervisor._notification_chat_id(), "-1008")

    def test_notify_error_sends_original_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "err_test.json"
            path.write_text(
                json.dumps(
                    {
                        "message": "pool callback failed",
                        "category": "telegram",
                        "traceback": "Traceback here",
                        "update": {"effective_chat": "-1008"},
                    }
                ),
                encoding="utf-8",
            )
            sent: list[str] = []

            def capture(text: str) -> None:
                sent.append(text)

            with (
                patch.object(supervisor, "_send_telegram_message", capture),
                patch.object(supervisor, "append_event"),
            ):
                result = asyncio.run(supervisor.notify_error("err_test", path))

            self.assertTrue(result)
            self.assertIn("pool callback failed", sent[0])
            self.assertIn("Traceback here", sent[0])
            self.assertIn("reported by supervisor", sent[0])


if __name__ == "__main__":
    unittest.main()
