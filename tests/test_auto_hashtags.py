import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from media_bot.auto_hashtags import (
    MetadataValidationError,
    CodexUnavailable,
    MetadataResult,
    build_codex_command,
    build_metadata_prompt,
    extract_frames,
    generate_metadata,
    normalize_hashtags,
    parse_metadata_output,
    sample_frame_times,
)
from media_bot.storage import (
    claim_metadata_job,
    create_edit_job,
    create_job,
    get_edit_job,
    init_db,
    list_resumable_metadata_jobs,
    queue_metadata_job,
    update_edit_job,
    update_job,
)
from media_bot.work_queue import WorkQueue


class MetadataShapeTests(unittest.TestCase):
    def test_normalizes_description_and_hashtags(self):
        result = parse_metadata_output(json.dumps({
            "description": "  A\n short   clip.  ",
            "hashtags": ["#Video", "#video", "bad tag", "#one", "#two", "#three", "#four", "#five"],
        }))
        self.assertEqual(result.description, "A short clip.")
        self.assertEqual(result.hashtags, ("#Video", "#one", "#two", "#three", "#four", "#five"))

    def test_rejects_too_few_valid_hashtags(self):
        with self.assertRaises(MetadataValidationError):
            normalize_hashtags(["#one", "#two", "#three", "#four", "not-a-tag"])

    def test_title_is_bounded(self):
        result = parse_metadata_output({
            "title": "x" * 2_000,
            "hashtags": [f"#tag{number}" for number in range(5)],
        })
        self.assertEqual(len(result.title), 100)

    def test_hashtag_string_is_bounded_and_keeps_model_order(self):
        result = parse_metadata_output({
            "title": "A clip",
            "hashtags": [
                "#popular", "#video", "#news", "#clip", "#fun",
                "#" + "x" * 101,
            ],
        })
        self.assertEqual(result.hashtags[:2], ("#popular", "#video"))
        self.assertLessEqual(len(" ".join(result.hashtags)), 100)

    def test_prompt_requests_reach_order_without_inventing_tags(self):
        prompt = build_metadata_prompt("speech", 8)
        self.assertIn('"title"', prompt)
        self.assertIn("no longer than 100 characters", prompt)
        self.assertIn("likely reach", prompt)
        self.assertIn("Do not invent a popular tag", prompt)

    def test_frame_times_are_evenly_spaced(self):
        times = sample_frame_times(80, 8)
        self.assertEqual(len(times), 8)
        self.assertAlmostEqual(times[0], 5)
        self.assertAlmostEqual(times[-1], 75)
        self.assertEqual({round(times[i + 1] - times[i], 6) for i in range(7)}, {10})

    def test_codex_command_is_shell_free_and_repeats_images(self):
        command = build_codex_command(
            executable="codex",
            model="gpt-5.6-luna",
            reasoning_effort="max",
            schema_path=Path("/tmp/schema.json"),
            output_path=Path("/tmp/output.json"),
            frame_paths=[Path("/tmp/frame-01.jpg"), Path("/tmp/frame-02.jpg")],
        )
        self.assertEqual(command[0:2], ["codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertEqual(command.count("--image"), 2)
        self.assertNotIn("sh", command)


class MetadataPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_eight_frames_and_cleans_generation_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "final.mp4"
            video.write_bytes(b"video")
            calls = []

            async def fake_run(command, timeout, error, **kwargs):
                calls.append(command)
                if command[0] == "ffprobe":
                    return b"80\n", b""
                Path(command[-1]).write_bytes(b"jpeg")
                return b"", b""

            with (
                patch("media_bot.auto_hashtags.shutil.which", return_value="tool"),
                patch("media_bot.auto_hashtags._run_checked", new=fake_run),
            ):
                output = root / "frames"
                frames = await extract_frames(video, output)

            self.assertEqual(len(frames), 8)
            self.assertEqual(len([call for call in calls if call[0] == "ffmpeg"]), 8)
            self.assertTrue(all(frame.is_file() for frame in frames))

    async def test_generation_uses_transcript_frames_and_removes_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "final.mp4"
            video.write_bytes(b"video")
            captured = {}

            async def fake_transcribe(path, timeout_seconds=0):
                captured["transcript_path"] = path
                return [{"text": "spoken evidence"}]

            async def fake_frames(path, output_dir, **kwargs):
                output_dir.mkdir(parents=True)
                frames = []
                for index in range(8):
                    frame = output_dir / f"frame-{index:02d}.jpg"
                    frame.write_bytes(b"jpeg")
                    frames.append(frame)
                captured["frames"] = frames
                return frames

            async def fake_codex(command, prompt, **kwargs):
                captured["command"] = list(command)
                captured["prompt"] = prompt
                captured["working_dir"] = kwargs["working_dir"]
                Path(kwargs["output_path"]).write_text(json.dumps({
                    "description": "A grounded clip",
                    "hashtags": [f"#tag{index}" for index in range(8)],
                }))
                return Path(kwargs["output_path"]).read_text()

            with (
                patch("media_bot.auto_hashtags.transcribe_audio", new=fake_transcribe),
                patch("media_bot.auto_hashtags.extract_frames", new=fake_frames),
                patch("media_bot.auto_hashtags._run_codex", new=fake_codex),
            ):
                result = await generate_metadata(
                    video,
                    model="gpt-5.6-luna",
                    reasoning_effort="max",
                )

            self.assertEqual(result.description, "A grounded clip")
            self.assertEqual(len(result.hashtags), 8)
            self.assertIn("spoken evidence", captured["prompt"])
            self.assertEqual(captured["command"].count("--image"), 8)
            self.assertFalse(Path(captured["working_dir"]).exists())


class MetadataStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "media.db"
        self.original = self.root / "original.mp4"
        self.original.write_bytes(b"original video")
        self.video = self.root / "edit-final.mp4"
        self.video.write_bytes(b"video")
        await init_db(self.db_path)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_queue_claim_and_restart_candidates(self):
        job = await create_job(self.db_path, "https://example.test/video", 1, 1)
        await update_job(self.db_path, job.id, status="uploaded")
        edit = await create_edit_job(self.db_path, job.id, 1)
        await update_edit_job(
            self.db_path,
            edit.id,
            status="rendered",
            file_path=str(self.video),
            file_size=self.video.stat().st_size,
        )

        queued = await queue_metadata_job(
            self.db_path,
            edit.id,
            model="gpt-5.6-luna",
            reasoning_effort="max",
            progress_message_id=10,
            render_delivery_message_id=11,
        )
        self.assertEqual(queued.metadata_status, "queued")
        self.assertEqual((await list_resumable_metadata_jobs(self.db_path))[0].id, edit.id)

        claimed = await claim_metadata_job(self.db_path, edit.id)
        self.assertEqual(claimed.metadata_status, "running")
        self.assertEqual(claimed.metadata_attempt_count, 1)
        self.assertEqual((await get_edit_job(self.db_path, edit.id)).metadata_status, "running")

    async def test_new_and_legacy_rendered_rows_do_not_request_metadata(self):
        job = await create_job(self.db_path, "https://example.test/video", 1, 1)
        edit = await create_edit_job(self.db_path, job.id, 1)
        self.assertEqual(edit.metadata_status, "not_requested")
        await update_edit_job(self.db_path, edit.id, status="rendered")
        self.assertEqual((await get_edit_job(self.db_path, edit.id)).metadata_status, "not_requested")

    async def _metadata_context(self, work):
        from media_bot.__main__ import _metadata_job

        job = await create_job(self.db_path, "https://example.test/video", 1, 1)
        await update_job(
            self.db_path,
            job.id,
            status="uploaded",
            file_path=str(self.original),
            file_size=self.original.stat().st_size,
        )
        edit = await create_edit_job(self.db_path, job.id, 1)
        await update_edit_job(
            self.db_path,
            edit.id,
            status="rendered",
            file_path=str(self.video),
            file_size=self.video.stat().st_size,
            metadata_status="queued",
            metadata_model="gpt-5.6-luna",
            metadata_reasoning_effort="max",
            metadata_progress_message_id=10,
            render_delivery_message_id=11,
            metadata_result_message_id=11,
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=20)),
            edit_message_text=AsyncMock(),
        )
        settings = SimpleNamespace(
            metadata_model="gpt-5.6-luna",
            metadata_reasoning_effort="max",
            metadata_codex_executable="codex",
            metadata_timeout_seconds=60,
            metadata_codex_home=None,
        )
        application = SimpleNamespace(
            bot_data={
                "settings": settings,
                "db_path": self.db_path,
                "metadata_work": work,
            },
            bot=bot,
        )
        return _metadata_job, SimpleNamespace(application=application, bot=bot), edit.id, bot

    async def test_metadata_worker_persists_and_delivers_separately(self):
        work = WorkQueue(name="metadata", workers=1, capacity=2, per_user_capacity=2)
        metadata_job, context, edit_id, bot = await self._metadata_context(work)
        with patch(
            "media_bot.__main__.generate_metadata",
            new=AsyncMock(return_value=MetadataResult(
                "A grounded clip", tuple(f"#tag{index}" for index in range(8))
            )),
        ) as generate:
            await metadata_job(context, edit_id)

        updated = await get_edit_job(self.db_path, edit_id)
        self.assertEqual(updated.metadata_status, "generated")
        self.assertEqual(json.loads(updated.metadata_hashtags), [f"#tag{index}" for index in range(8)])
        self.assertEqual(updated.metadata_reply_message_id, 20)
        bot.send_message.assert_awaited_once()
        self.assertIn("Title and hashtags", bot.send_message.await_args.kwargs["text"])
        self.assertEqual(generate.await_args.args[0], self.original)

    async def test_codex_unavailable_skips_metadata_without_affecting_render_status(self):
        work = WorkQueue(name="metadata", workers=1, capacity=2, per_user_capacity=2)
        metadata_job, context, edit_id, _ = await self._metadata_context(work)
        with patch(
            "media_bot.__main__.generate_metadata",
            new=AsyncMock(side_effect=CodexUnavailable("Codex executable is not installed")),
        ):
            await metadata_job(context, edit_id)

        updated = await get_edit_job(self.db_path, edit_id)
        self.assertEqual(updated.status, "rendered")
        self.assertEqual(updated.metadata_status, "skipped")
        self.assertIn("not installed", updated.metadata_error)

    async def test_startup_recovery_only_admits_durable_deliveries(self):
        from media_bot.__main__ import _resume_metadata_work

        job = await create_job(self.db_path, "https://example.test/video", 1, 1)
        edit = await create_edit_job(self.db_path, job.id, 1)
        await update_edit_job(
            self.db_path,
            edit.id,
            status="rendered",
            file_path=str(self.video),
            metadata_status="queued",
            render_delivery_message_id=11,
            metadata_result_message_id=11,
        )
        missing = await create_edit_job(self.db_path, job.id, 1)
        await update_edit_job(
            self.db_path,
            missing.id,
            status="rendered",
            file_path=str(self.video),
            metadata_status="running",
        )
        work = WorkQueue(name="metadata", workers=1, capacity=4, per_user_capacity=4)
        application = SimpleNamespace(
            bot_data={"db_path": self.db_path, "metadata_work": work},
            bot=SimpleNamespace(),
        )

        await _resume_metadata_work(application)

        self.assertTrue(work.has_label(f"metadata:{edit.id}"))
        self.assertFalse(work.has_label(f"metadata:{missing.id}"))
        missing_row = await get_edit_job(self.db_path, missing.id)
        self.assertEqual(missing_row.metadata_status, "failed")

    async def test_claim_does_not_run_after_queued_metadata_is_cancelled(self):
        from media_bot.__main__ import _metadata_job

        job = await create_job(self.db_path, "https://example.test/video", 1, 1)
        edit = await create_edit_job(self.db_path, job.id, 1)
        await update_edit_job(
            self.db_path,
            edit.id,
            status="rendered",
            file_path=str(self.video),
            metadata_status="cancelled",
        )
        bot = SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock())
        application = SimpleNamespace(
            bot_data={
                "settings": SimpleNamespace(),
                "db_path": self.db_path,
                "metadata_work": WorkQueue(
                    name="metadata", workers=1, capacity=2, per_user_capacity=2,
                ),
            },
            bot=bot,
        )
        with patch("media_bot.__main__.generate_metadata", new=AsyncMock()) as generate:
            await _metadata_job(SimpleNamespace(application=application, bot=bot), edit.id)
        generate.assert_not_awaited()
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
