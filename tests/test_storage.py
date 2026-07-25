import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from media_bot.storage import (
    Classification,
    EditJob,
    JobRecord,
    PoolItem,
    Preset,
    SharedPreset,
    UserSettings,
    Workflow,
    add_pool_tag,
    cleanup_expired_tokens,
    cleanup_old_jobs,
    consume_download_token,
    create_classification,
    create_download_token,
    create_edit_job,
    create_job,
    create_pool_item,
    create_preset,
    create_workflow,
    delete_pool_item,
    delete_preset,
    delete_workflow,
    get_classification,
    get_edit_job,
    get_job,
    get_or_create_classification,
    get_pool_item,
    get_preset_by_share_code,
    get_workflow,
    init_db,
    list_classifications,
    list_pool_items,
    list_pool_tags,
    list_presets,
    list_source_jobs_for_user,
    list_user_jobs,
    list_workflows,
    remove_pool_tag,
    share_preset,
    update_edit_job,
    update_job,
    update_pool_item,
    update_preset,
    update_user_settings,
    update_workflow,
)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_init_db_creates_tables(self):
        import asyncio

        asyncio.run(init_db(self.db_path))
        self.assertTrue(self.db_path.is_file())

    def test_create_and_get_job(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            self.assertEqual(job.url, "https://example.com")
            self.assertEqual(job.user_id, 1)
            self.assertEqual(job.chat_id, 2)
            self.assertEqual(job.status, "pending")

            fetched = await get_job(self.db_path, job.id)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.id, job.id)

        asyncio.run(run())

    def test_update_job(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            updated = await update_job(self.db_path, job.id, status="uploaded", file_path="/tmp/x.mp4")
            self.assertEqual(updated.status, "uploaded")
            self.assertEqual(updated.file_path, "/tmp/x.mp4")

        asyncio.run(run())

    def test_list_user_jobs(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            await create_job(self.db_path, "https://a.com", 1, 2)
            await create_job(self.db_path, "https://b.com", 1, 2)
            await create_job(self.db_path, "https://c.com", 2, 2)
            jobs = await list_user_jobs(self.db_path, 1)
            self.assertEqual(len(jobs), 2)

        asyncio.run(run())

    def test_download_token_lifecycle(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            token = await create_download_token(self.db_path, job.id, 1, 15)
            self.assertIsInstance(token, str)
            self.assertEqual(len(token), 43)

            consumed = await consume_download_token(self.db_path, token)
            self.assertIsNotNone(consumed)
            self.assertEqual(consumed.job_id, job.id)
            self.assertEqual(consumed.user_id, 1)

            reused = await consume_download_token(self.db_path, token)
            self.assertIsNone(reused)

        asyncio.run(run())

    def test_expired_token_is_rejected(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            token = await create_download_token(self.db_path, job.id, 1, 0)
            consumed = await consume_download_token(self.db_path, token)
            self.assertIsNone(consumed)

        asyncio.run(run())

    def test_cleanup_expired_tokens(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await create_download_token(self.db_path, job.id, 1, 0)
            removed = await cleanup_expired_tokens(self.db_path)
            self.assertEqual(removed, 1)

        asyncio.run(run())

    def test_cleanup_old_jobs(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir(parents=True, exist_ok=True)
            fake = storage_dir / f"{job.id}-fake.mp4"
            fake.write_text("data")
            await update_job(self.db_path, job.id, status="uploaded", file_path=str(fake))
            old_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (old_ts, job.id))
                await db.commit()
            removed = await cleanup_old_jobs(self.db_path, storage_dir, 0)
            self.assertEqual(removed, 1)
            self.assertFalse(fake.exists())

        asyncio.run(run())

    def test_user_settings(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            settings = await update_user_settings(self.db_path, 1, crop_preset="16:9")
            self.assertEqual(settings.user_id, 1)
            self.assertEqual(settings.crop_preset, "16:9")

        asyncio.run(run())

    def test_presets(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            preset = await create_preset(self.db_path, 1, "default", crop_preset="1:1")
            self.assertEqual(preset.name, "default")
            presets = await list_presets(self.db_path, 1)
            self.assertEqual(len(presets), 1)
            deleted = await delete_preset(self.db_path, 1, preset.id)
            self.assertTrue(deleted)
            presets = await list_presets(self.db_path, 1)
            self.assertEqual(len(presets), 0)

        asyncio.run(run())

    def test_preset_with_caption_and_voice(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            preset = await create_preset(
                self.db_path, 1, "full",
                caption_text="hello", caption_color="yellow", caption_style="bold",
                caption_position="high", voice_over_voice="alice", voice_quality="premium", voice_speed=1.2,
            )
            self.assertEqual(preset.caption_text, "hello")
            self.assertEqual(preset.caption_color, "yellow")
            self.assertEqual(preset.caption_style, "bold")
            self.assertEqual(preset.caption_position, "high")
            self.assertEqual(preset.voice_over_voice, "alice")
            self.assertEqual(preset.voice_quality, "premium")
            self.assertEqual(preset.voice_speed, 1.2)
            updated = await update_preset(self.db_path, preset.id, 1, caption_color="red")
            self.assertEqual(updated.caption_color, "red")

        asyncio.run(run())

    def test_share_preset(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            preset = await create_preset(self.db_path, 1, "shareme")
            code = await share_preset(self.db_path, preset.id, 1)
            self.assertIsInstance(code, str)
            found = await get_preset_by_share_code(self.db_path, code)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, preset.id)

        asyncio.run(run())

    def test_edit_jobs(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1, preset_id=None)
            self.assertEqual(edit.source_job_id, job.id)
            self.assertIsNone(edit.preset_id)
            updated = await update_edit_job(self.db_path, edit.id, status="rendered")
            self.assertEqual(updated.status, "rendered")
            fetched = await get_edit_job(self.db_path, edit.id)
            self.assertIsNotNone(fetched)

        asyncio.run(run())

    def test_list_source_jobs_for_user(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            j1 = await create_job(self.db_path, "https://a.com", 1, 2)
            await create_job(self.db_path, "https://b.com", 2, 2)
            edit = await create_edit_job(self.db_path, j1.id, 1)
            jobs = await list_source_jobs_for_user(self.db_path, 1)
            ids = [j.id for j in jobs]
            self.assertIn(j1.id, ids)
            self.assertIn(edit.source_job_id, ids)

        asyncio.run(run())

    def test_pool_items(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            pool_item = await create_pool_item(self.db_path, 1, "/tmp/video.mp4", source_job_id=job.id, title="clip1")
            self.assertEqual(pool_item.user_id, 1)
            self.assertEqual(pool_item.source_job_id, job.id)
            fetched = await get_pool_item(self.db_path, pool_item.id)
            self.assertIsNotNone(fetched)
            updated = await update_pool_item(self.db_path, pool_item.id, title="new title")
            self.assertEqual(updated.title, "new title")
            items = await list_pool_items(self.db_path, 1)
            self.assertEqual(len(items), 1)

        asyncio.run(run())

    def test_classifications(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            c1 = await create_classification(self.db_path, "cars", description="Car clips", color="red")
            self.assertEqual(c1.name, "cars")
            c2 = await get_or_create_classification(self.db_path, "cars")
            self.assertEqual(c2.id, c1.id)
            c3 = await get_or_create_classification(self.db_path, "police", color="blue")
            self.assertEqual(c3.name, "police")
            all_classes = await list_classifications(self.db_path)
            self.assertEqual(len(all_classes), 2)

        asyncio.run(run())

    def test_pool_tags(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            pool_item = await create_pool_item(self.db_path, 1, "/tmp/video.mp4", source_job_id=job.id)
            c = await create_classification(self.db_path, "cars")
            tag = await add_pool_tag(self.db_path, pool_item.id, c.id, 1)
            self.assertIsNotNone(tag)
            tags = await list_pool_tags(self.db_path, pool_item.id)
            self.assertEqual(len(tags), 1)
            removed = await remove_pool_tag(self.db_path, pool_item.id, c.id)
            self.assertTrue(removed)
            tags = await list_pool_tags(self.db_path, pool_item.id)
            self.assertEqual(len(tags), 0)

        asyncio.run(run())

    def test_workflows(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            c = await create_classification(self.db_path, "cars")
            wf = await create_workflow(self.db_path, 1, "car caption", "caption", trigger_classification_id=c.id)
            self.assertEqual(wf.name, "car caption")
            self.assertEqual(wf.action_type, "caption")
            self.assertTrue(wf.enabled)
            wfs = await list_workflows(self.db_path, 1)
            self.assertEqual(len(wfs), 1)
            updated = await update_workflow(self.db_path, wf.id, 1, enabled=False)
            self.assertFalse(updated.enabled)
            deleted = await delete_workflow(self.db_path, wf.id, 1)
            self.assertTrue(deleted)
            wfs = await list_workflows(self.db_path, 1)
            self.assertEqual(len(wfs), 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
