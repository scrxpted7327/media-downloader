import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supervisor


class SupervisorReportingTests(unittest.TestCase):
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
