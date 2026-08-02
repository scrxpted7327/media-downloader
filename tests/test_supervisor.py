import asyncio
import io
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

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

    def test_telegram_http_error_includes_api_description(self):
        error = supervisor.urllib.error.HTTPError(
            "https://api.telegram.org/bot[REDACTED]/sendMessage",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
            ),
        )
        env = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ERROR_CHAT_ID": "-1008",
        }
        with (
            patch.dict(supervisor.os.environ, env, clear=True),
            patch.object(supervisor.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "chat not found"),
        ):
            supervisor._send_telegram_message("test")

    def test_classify_error_recognizes_missing_dependencies_and_dns(self):
        self.assertEqual(
            supervisor.classify_error("ModuleNotFoundError: No module named 'numpy'"),
            "dependency",
        )
        self.assertEqual(
            supervisor.classify_error("telegram.error.NetworkError: temporary failure in name resolution"),
            "network",
        )

    def test_supervisor_error_file_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = Path(directory) / "errors"
            with patch.object(supervisor, "ERRORS_DIR", errors):
                path = asyncio.run(
                    supervisor.write_error_file(
                        "err_secret",
                        "failed token=do-not-store https://example.test/path?key=hidden",
                        "runtime",
                    )
                )
            raw = path.read_text(encoding="utf-8")

        self.assertNotIn("do-not-store", raw)
        self.assertNotIn("key=hidden", raw)
        self.assertIn("https://example.test/path", raw)

    def test_supervisor_events_redact_nested_context(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            with patch.object(supervisor, "EVENTS_PATH", events):
                supervisor.append_event(
                    "test",
                    "message",
                    request={"password": "nested-secret", "url": "https://x.test/a?q=secret"},
                )
            raw = events.read_text(encoding="utf-8")

        self.assertNotIn("nested-secret", raw)
        self.assertNotIn("q=secret", raw)

    def test_captured_process_output_rotates_supervisor_log(self):
        class Stream:
            def __init__(self):
                self.lines = deque(
                    [f"output-{index}-{'x' * 70}\n".encode() for index in range(8)]
                )

            async def readline(self):
                return self.lines.popleft() if self.lines else b""

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "supervisor.log"
            with (
                patch.object(supervisor, "SUPERVISOR_LOG", log),
                patch.object(supervisor, "SUPERVISOR_LOG_MAX_BYTES", 240),
                patch.object(supervisor, "SUPERVISOR_LOG_BACKUP_COUNT", 2),
            ):
                asyncio.run(supervisor._capture_stream(Stream(), "stdout", deque(maxlen=20)))
            sizes = [
                path.stat().st_size
                for path in (log, log.with_name("supervisor.log.1"), log.with_name("supervisor.log.2"))
                if path.exists()
            ]

        self.assertGreaterEqual(len(sizes), 2)
        self.assertTrue(all(size <= 240 for size in sizes))

    def test_process_group_shutdown_escalates_after_grace_period(self):
        async def run():
            stopped = asyncio.Event()
            process = MagicMock()
            process.pid = 456
            process.returncode = None

            async def wait():
                await stopped.wait()
                process.returncode = -9
                return -9

            process.wait = wait
            signals: list[int] = []

            def signal_group(pid, sent_signal):
                self.assertEqual(pid, 456)
                if sent_signal == 0:
                    if stopped.is_set():
                        raise ProcessLookupError
                    return
                signals.append(sent_signal)
                if sent_signal == supervisor.signal.SIGKILL:
                    stopped.set()

            with patch.object(supervisor.os, "killpg", side_effect=signal_group):
                result = await supervisor._terminate_process_group(
                    process,
                    term_timeout=0.01,
                    kill_timeout=0.1,
                )

            self.assertTrue(result)
            self.assertEqual(signals, [supervisor.signal.SIGTERM, supervisor.signal.SIGKILL])

        asyncio.run(run())

    def test_process_group_shutdown_allows_graceful_term(self):
        async def run():
            stopped = asyncio.Event()
            process = MagicMock()
            process.pid = 321
            process.returncode = None

            async def wait():
                await stopped.wait()
                process.returncode = 0
                return 0

            process.wait = wait
            signals: list[int] = []

            def signal_group(pid, sent_signal):
                self.assertEqual(pid, 321)
                if sent_signal == 0:
                    if stopped.is_set():
                        raise ProcessLookupError
                    return
                signals.append(sent_signal)
                if sent_signal == supervisor.signal.SIGTERM:
                    stopped.set()

            with patch.object(supervisor.os, "killpg", side_effect=signal_group):
                result = await supervisor._terminate_process_group(
                    process,
                    term_timeout=0.1,
                    kill_timeout=0.1,
                )

            self.assertTrue(result)
            self.assertEqual(signals, [supervisor.signal.SIGTERM])

        asyncio.run(run())

    def test_bot_launch_starts_a_separate_process_session(self):
        async def run():
            process = MagicMock()
            with patch.object(
                supervisor.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as create_process:
                result = await supervisor._launch_bot(
                    ["python", "-m", "media_bot"],
                    Path("/srv/media-bot"),
                )

            self.assertIs(result, process)
            self.assertTrue(create_process.await_args.kwargs["start_new_session"])
            self.assertEqual(create_process.await_args.kwargs["cwd"], "/srv/media-bot")

        asyncio.run(run())

    def test_restart_prefers_project_virtualenv_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)

            with patch.object(restart_bot, "PROJECT_DIR", root):
                selected = restart_bot._project_python()

        self.assertEqual(selected, python)

    def test_restart_installs_requirements_with_project_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            (root / "requirements.txt").write_text("example==1\n", encoding="utf-8")

            with (
                patch.object(restart_bot, "PROJECT_DIR", root),
                patch.object(restart_bot.subprocess, "run") as run,
            ):
                restart_bot._install_requirements(python)

        run.assert_called_once_with(
            [str(python), "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=root,
            check=True,
        )

    def test_restart_targets_managed_process_groups_without_its_own_group(self):
        with (
            patch.object(restart_bot.os, "getpgrp", return_value=10),
            patch.object(restart_bot.os, "getpgid", side_effect={101: 20, 102: 20, 103: 10}.get),
        ):
            groups, individuals = restart_bot._termination_targets([101, 102, 103])

        self.assertEqual(groups, {20})
        self.assertEqual(individuals, {103})

    def test_restart_escalates_managed_groups_after_grace_period(self):
        with (
            patch.object(restart_bot, "GRACEFUL_STOP_SECONDS", 0),
            patch.object(restart_bot, "_managed_processes", return_value=[101]),
            patch.object(restart_bot, "_termination_targets", return_value=({20}, set())),
            patch.object(restart_bot, "_targets_alive", return_value=True),
            patch.object(restart_bot, "_signal_targets") as signal_targets,
        ):
            restart_bot._stop_existing()

        self.assertEqual(
            signal_targets.call_args_list,
            [
                call({20}, set(), restart_bot.signal.SIGTERM),
                call({20}, set(), restart_bot.signal.SIGKILL),
            ],
        )


if __name__ == "__main__":
    unittest.main()
