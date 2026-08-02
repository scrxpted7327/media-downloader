import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_bot import diagnostics


class DiagnosticsSafetyTests(unittest.TestCase):
    def test_event_context_is_recursively_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with patch.object(diagnostics, "EVENTS_PATH", path):
                diagnostics.append_event(
                    "nested",
                    "request failed",
                    request={
                        "api_token": "short-secret",
                        "headers": {"Authorization": "Bearer visible-secret"},
                        "items": [
                            {"url": "https://example.test/file?token=query-secret#fragment"},
                            {"password": "another-secret"},
                        ],
                    },
                )

            raw = path.read_text(encoding="utf-8")
            event = json.loads(raw)

        self.assertNotIn("short-secret", raw)
        self.assertNotIn("visible-secret", raw)
        self.assertNotIn("query-secret", raw)
        self.assertNotIn("another-secret", raw)
        self.assertEqual(event["request"]["api_token"], "[REDACTED]")
        self.assertEqual(
            event["request"]["items"][0]["url"],
            "https://example.test/file",
        )

    def test_redacted_json_scrubs_nested_error_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "error.json"
            diagnostics.write_redacted_json(
                path,
                {
                    "message": "failed token=top-secret",
                    "update": {
                        "cookies": [{"password": "nested-secret"}],
                        "callback": "https://example.test/cb?api_key=query-secret",
                    },
                },
            )
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertNotIn("top-secret", raw)
        self.assertNotIn("nested-secret", raw)
        self.assertNotIn("query-secret", raw)
        self.assertEqual(payload["update"]["cookies"][0]["password"], "[REDACTED]")

    def test_event_log_rotates_at_a_bounded_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with (
                patch.object(diagnostics, "EVENTS_PATH", path),
                patch.object(diagnostics, "EVENTS_MAX_BYTES", 420),
                patch.object(diagnostics, "EVENTS_BACKUP_COUNT", 2),
            ):
                for index in range(20):
                    diagnostics.append_event("rotation", f"event-{index}-" + "x" * 60)

            files = [path, path.with_name("events.jsonl.1"), path.with_name("events.jsonl.2")]
            existing = [candidate for candidate in files if candidate.exists()]
            sizes = [candidate.stat().st_size for candidate in existing]
            contents = [candidate.read_text(encoding="utf-8") for candidate in existing]

        self.assertGreaterEqual(len(existing), 2)
        self.assertTrue(all(size <= 420 for size in sizes))
        for content in contents:
            for line in content.splitlines():
                json.loads(line)

    def test_recent_events_reads_only_a_bounded_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            lines = [
                json.dumps({"kind": "item", "message": f"event-{index}", "user_id": 1})
                for index in range(100)
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with (
                patch.object(diagnostics, "EVENTS_PATH", path),
                patch.object(diagnostics, "RECENT_EVENTS_MAX_READ_BYTES", 320),
                patch.object(diagnostics, "RECENT_EVENTS_MAX_RESULTS", 3),
            ):
                events = diagnostics.recent_events(user_id=1, limit=1000)

        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1]["message"], "event-99")
        self.assertNotIn("event-0", [event["message"] for event in events])


if __name__ == "__main__":
    unittest.main()
