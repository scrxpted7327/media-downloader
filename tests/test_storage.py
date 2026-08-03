import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from media_bot.storage import (
    LATEST_SCHEMA_VERSION,
    SQLITE_BUSY_TIMEOUT_MS,
    CleanupResult,
    UnsafeStoragePath,
    add_pool_tag,
    cleanup_edit_artifacts,
    cleanup_expired_tokens,
    cleanup_old_jobs,
    cleanup_user_jobs,
    consume_download_token,
    create_classification,
    create_download_token,
    create_durable_pool_item,
    create_edit_job,
    create_job,
    create_pool_item,
    create_preset,
    create_workflow,
    create_workflow_run,
    delete_durable_pool_item,
    delete_edit_job_with_artifacts,
    delete_job_with_artifacts,
    delete_preset,
    delete_workflow,
    foreign_key_violations,
    get_edit_job,
    get_job,
    get_or_create_classification,
    get_pool_item,
    get_saved_edit_pool_item,
    get_saved_source_pool_item,
    get_preset_by_share_code,
    get_workflow_run,
    init_db,
    list_classifications,
    list_pool_items,
    list_saved_edits_for_source,
    list_pool_tags,
    list_presets,
    list_source_jobs_for_user,
    list_user_jobs,
    list_workflows,
    open_database,
    remove_pool_tag,
    reconcile_interrupted_work,
    reset_source_edits,
    share_preset,
    stage_edit_source,
    update_edit_job,
    update_job,
    update_pool_item,
    update_preset,
    update_user_settings,
    update_workflow,
    update_workflow_run,
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

    def test_reconciles_interrupted_downloads_and_renders(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await update_job(self.db_path, job.id, status="downloading")
            edit = await create_edit_job(self.db_path, job.id, 1)
            await update_edit_job(self.db_path, edit.id, status="rendering")

            self.assertEqual(await reconcile_interrupted_work(self.db_path), (1, 1))
            self.assertEqual((await get_job(self.db_path, job.id)).status, "failed")
            self.assertEqual((await get_edit_job(self.db_path, edit.id)).status, "failed")

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

    def test_download_token_has_exactly_one_concurrent_winner(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            for _ in range(5):
                token = await create_download_token(self.db_path, job.id, 1, 15)
                results = await asyncio.gather(
                    *(consume_download_token(self.db_path, token) for _ in range(20))
                )
                self.assertEqual(sum(result is not None for result in results), 1)

        asyncio.run(run())

    def test_download_token_rejects_job_and_edit_ownership_mismatch(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            other_job = await create_job(self.db_path, "https://example.com/other", 2, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)

            with self.assertRaises(ValueError):
                await create_download_token(self.db_path, job.id, 2, 15)
            with self.assertRaises(ValueError):
                await create_download_token(
                    self.db_path, other_job.id, 2, 15, edit_job_id=edit.id,
                )

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

    def test_download_token_can_target_rendered_edit(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            token = await create_download_token(
                self.db_path, job.id, 1, 15, edit_job_id=edit.id,
            )

            consumed = await consume_download_token(self.db_path, token)

            self.assertEqual(consumed.job_id, job.id)
            self.assertEqual(consumed.edit_job_id, edit.id)

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
                await db.execute(
                    "UPDATE jobs SET created_at = ?, updated_at = ? WHERE id = ?",
                    (old_ts, old_ts, job.id),
                )
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
                voice_mode="swearify",
                watermark_mode="swap", watermark_text="@replacement",
            )
            self.assertEqual(preset.caption_text, "hello")
            self.assertEqual(preset.caption_color, "yellow")
            self.assertEqual(preset.caption_style, "bold")
            self.assertEqual(preset.caption_position, "high")
            self.assertEqual(preset.voice_over_voice, "alice")
            self.assertEqual(preset.voice_mode, "swearify")
            self.assertEqual(preset.voice_quality, "premium")
            self.assertEqual(preset.voice_speed, 1.2)
            self.assertEqual(preset.watermark_mode, "swap")
            self.assertEqual(preset.watermark_text, "@replacement")
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
            self.assertIsNone(await share_preset(self.db_path, preset.id, 2))

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

    def test_pool_groups_original_with_saved_edits(self):
        import asyncio

        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            original = await create_pool_item(
                self.db_path,
                1,
                "/tmp/original.mp4",
                source_job_id=job.id,
                title="Original",
            )
            saved_edit = await create_pool_item(
                self.db_path,
                1,
                "/tmp/edit.mp4",
                source_job_id=job.id,
                edit_job_id=edit.id,
                title="Edit",
            )

            self.assertEqual(
                (await get_saved_source_pool_item(self.db_path, 1, job.id)).id,
                original.id,
            )
            self.assertEqual(
                (await get_saved_edit_pool_item(self.db_path, 1, edit.id)).id,
                saved_edit.id,
            )
            grouped = await list_saved_edits_for_source(self.db_path, 1, job.id)
            self.assertEqual([item.id for item in grouped], [saved_edit.id])

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

    def test_stage_edit_source(self):
        async def run():
            root = Path(self.tmpdir.name)
            source = root / "source.mp4"
            destination = root / "edits" / "edit.mp4"
            source.write_bytes(b"video-data")

            size = await stage_edit_source(source, destination)

            self.assertEqual(size, 10)
            self.assertEqual(destination.read_bytes(), b"video-data")

        asyncio.run(run())

    def test_database_connections_always_enable_integrity_settings(self):
        async def run():
            await init_db(self.db_path)
            async with open_database(self.db_path) as db:
                async with db.execute("PRAGMA foreign_keys") as cursor:
                    foreign_keys = await cursor.fetchone()
                async with db.execute("PRAGMA busy_timeout") as cursor:
                    busy_timeout = await cursor.fetchone()
                async with db.execute("SELECT 1 AS value") as cursor:
                    row = await cursor.fetchone()

            self.assertEqual(foreign_keys[0], 1)
            self.assertEqual(busy_timeout[0], SQLITE_BUSY_TIMEOUT_MS)
            self.assertIsInstance(row, aiosqlite.Row)
            self.assertEqual(row["value"], 1)

        asyncio.run(run())

    def test_foreign_keys_reject_invalid_edit_parent(self):
        async def run():
            await init_db(self.db_path)
            with self.assertRaises(aiosqlite.IntegrityError):
                await create_edit_job(self.db_path, 999, 1)
            self.assertEqual(await foreign_key_violations(self.db_path), [])

        asyncio.run(run())

    def test_migrations_are_versioned_and_idempotent(self):
        async def run():
            await init_db(self.db_path)
            await init_db(self.db_path)
            async with open_database(self.db_path) as db:
                async with db.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ) as cursor:
                    versions = [row["version"] for row in await cursor.fetchall()]
            self.assertEqual(versions, list(range(1, LATEST_SCHEMA_VERSION + 1)))

        asyncio.run(run())

    def test_migrates_legacy_database_without_metadata_columns(self):
        async def run():
            # This is the shape of the live database after the metadata feature
            # was deployed: the migration table exists but has no markers, and
            # edit_jobs still has the pre-metadata columns.
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    CREATE TABLE edit_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_job_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        preset_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'pending',
                        file_path TEXT,
                        file_size INTEGER,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        error_message TEXT
                    );
                    """
                )
                await db.commit()

            await init_db(self.db_path)

            async with open_database(self.db_path) as db:
                async with db.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ) as cursor:
                    versions = [row["version"] for row in await cursor.fetchall()]
                async with db.execute("PRAGMA table_info(edit_jobs)") as cursor:
                    columns = {row["name"] for row in await cursor.fetchall()}
                async with db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_edit_jobs_metadata_status'"
                ) as cursor:
                    metadata_index = await cursor.fetchone()

            self.assertEqual(versions, list(range(1, LATEST_SCHEMA_VERSION + 1)))
            self.assertIn("metadata_status", columns)
            self.assertIn("metadata_hashtags", columns)
            self.assertIn("voice_mode", columns)
            self.assertIn("render_status_message_id", columns)
            self.assertIsNotNone(metadata_index)

        asyncio.run(run())

    def test_migration_repairs_orphaned_edits_without_deleting_them(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 7, 8)
            edit = await create_edit_job(self.db_path, job.id, 7)

            # Reproduce a database produced by the historical connections that
            # did not enable foreign-key enforcement.
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys=OFF")
                await db.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
                await db.execute("DELETE FROM schema_migrations WHERE version = 3")
                await db.commit()

            await init_db(self.db_path)

            recovered_job = await get_job(self.db_path, job.id)
            self.assertIsNotNone(recovered_job)
            self.assertEqual(recovered_job.status, "deleted")
            self.assertTrue(recovered_job.url.startswith("recovered://"))
            self.assertIsNotNone(await get_edit_job(self.db_path, edit.id))
            self.assertEqual(await foreign_key_violations(self.db_path), [])
            async with open_database(self.db_path) as db:
                async with db.execute(
                    "SELECT COUNT(*) AS count FROM migration_repairs "
                    "WHERE migration_version = 3 AND table_name = 'jobs'"
                ) as cursor:
                    repair_count = (await cursor.fetchone())["count"]
            self.assertEqual(repair_count, 1)

        asyncio.run(run())

    def test_migration_repairs_all_legacy_relation_shapes_without_data_loss(self):
        async def run():
            await init_db(self.db_path)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys=OFF")
                await db.execute(
                    "INSERT INTO shared_presets "
                    "(preset_id, user_id, share_code) VALUES (901, 7, 'orphan-share')"
                )
                await db.execute(
                    "INSERT INTO pool_tags "
                    "(pool_item_id, classification_id, user_id) VALUES (902, 903, 7)"
                )
                await db.execute(
                    "INSERT INTO workflow_runs "
                    "(workflow_id, pool_item_id, user_id) VALUES (904, 905, 7)"
                )
                await db.execute(
                    "INSERT INTO pool_items "
                    "(user_id, source_job_id, edit_job_id, file_path) "
                    "VALUES (7, 906, 907, 'missing.mp4')"
                )
                await db.execute(
                    "INSERT INTO workflows "
                    "(user_id, name, trigger_classification_id, action_type, action_preset_id) "
                    "VALUES (7, 'legacy refs', 908, 'none', 909)"
                )
                await db.execute("DELETE FROM schema_migrations WHERE version = 3")
                await db.commit()

            await init_db(self.db_path)

            self.assertEqual(await foreign_key_violations(self.db_path), [])
            async with open_database(self.db_path) as db:
                for table in ("shared_presets", "pool_tags", "workflow_runs"):
                    async with db.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ) as cursor:
                        self.assertEqual((await cursor.fetchone())["count"], 1)
                async with db.execute(
                    "SELECT source_job_id, edit_job_id FROM pool_items "
                    "WHERE file_path = 'missing.mp4'"
                ) as cursor:
                    pool = await cursor.fetchone()
                async with db.execute(
                    "SELECT trigger_classification_id, action_preset_id FROM workflows "
                    "WHERE name = 'legacy refs'"
                ) as cursor:
                    workflow = await cursor.fetchone()
            self.assertIsNone(pool["source_job_id"])
            self.assertIsNone(pool["edit_job_id"])
            self.assertIsNone(workflow["trigger_classification_id"])
            self.assertIsNone(workflow["action_preset_id"])

        asyncio.run(run())

    def test_migration_does_not_swallow_unexpected_schema_errors(self):
        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
                await db.commit()

            with self.assertRaises(aiosqlite.OperationalError):
                await init_db(self.db_path)

        asyncio.run(run())

    def test_update_no_kwargs_paths_use_a_real_connection(self):
        async def run():
            await init_db(self.db_path)
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            self.assertEqual((await update_edit_job(self.db_path, edit.id)).id, edit.id)

            pool = await create_pool_item(
                self.db_path, 1, "/tmp/video.mp4", source_job_id=job.id
            )
            workflow = await create_workflow(self.db_path, 1, "noop", "none")
            workflow_run = await create_workflow_run(
                self.db_path, workflow.id, pool.id, 1
            )
            self.assertEqual(
                (await update_workflow_run(self.db_path, workflow_run.id)).id,
                workflow_run.id,
            )
            self.assertEqual(
                (await get_workflow_run(self.db_path, workflow_run.id)).id,
                workflow_run.id,
            )

        asyncio.run(run())

    def test_durable_pool_copy_survives_job_cleanup(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            source = storage_dir / "1-source.mp4"
            source.write_bytes(b"durable-video")
            thumbnail = storage_dir / "1-thumbnail.jpg"
            thumbnail.write_bytes(b"image")
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await update_job(
                self.db_path,
                job.id,
                status="uploaded",
                file_path=str(source),
                thumbnail_path=str(thumbnail),
            )

            pool = await create_durable_pool_item(
                self.db_path,
                storage_dir,
                1,
                source,
                source_job_id=job.id,
                thumbnail_file=thumbnail,
            )
            pool_file = Path(pool.file_path)
            pool_thumbnail = Path(pool.thumbnail_path)
            self.assertNotEqual(pool_file, source)
            self.assertEqual(pool_file.read_bytes(), b"durable-video")
            self.assertTrue(
                pool_file.is_relative_to((storage_dir / "pool").resolve())
            )

            deleted = await delete_job_with_artifacts(
                self.db_path, storage_dir, job.id, user_id=1
            )
            self.assertGreaterEqual(deleted.records_deleted, 1)
            self.assertFalse(source.exists())
            self.assertFalse(thumbnail.exists())
            self.assertTrue(pool_file.exists())
            self.assertTrue(pool_thumbnail.exists())
            surviving_pool = await get_pool_item(self.db_path, pool.id)
            self.assertIsNone(surviving_pool.source_job_id)
            self.assertEqual(await foreign_key_violations(self.db_path), [])

            removed_pool = await delete_durable_pool_item(
                self.db_path, storage_dir, pool.id, 1
            )
            self.assertEqual(removed_pool.records_deleted, 1)
            self.assertEqual(removed_pool.files_deleted, 2)
            self.assertFalse(pool_file.exists())
            self.assertFalse(pool_thumbnail.exists())

        asyncio.run(run())

    def test_legacy_pool_reference_preserves_job_file(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            source = storage_dir / "source.mp4"
            source.write_bytes(b"saved-video")
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await update_job(self.db_path, job.id, file_path=str(source))
            pool = await create_pool_item(
                self.db_path, 1, str(source), source_job_id=job.id
            )

            result = await delete_job_with_artifacts(
                self.db_path, storage_dir, job.id, user_id=1
            )

            self.assertTrue(source.exists())
            self.assertEqual(result.files_preserved, 1)
            self.assertIsNone((await get_pool_item(self.db_path, pool.id)).source_job_id)
            self.assertEqual(await foreign_key_violations(self.db_path), [])

        asyncio.run(run())

    def test_cleanup_edit_artifacts_removes_staging_preview_and_intermediates(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            staged = storage_dir / f"edit-{edit.id}-source.mp4"
            final = storage_dir / f"edit-{edit.id}-final.mp4"
            intermediate = storage_dir / f"edit-{edit.id}-final_cap.mp4"
            hidden_intermediate = storage_dir / f".edit-{edit.id}-final_cap.mp4"
            subtitles = storage_dir / f"edit-{edit.id}-final.srt"
            preview = storage_dir / f"edit-{edit.id}-watermarks.jpg"
            for path in (
                staged, final, intermediate, hidden_intermediate, subtitles, preview,
            ):
                path.write_bytes(b"artifact")
            await update_edit_job(
                self.db_path,
                edit.id,
                status="rendered",
                file_path=str(final),
                subtitles_path=str(subtitles),
                watermark_preview_path=str(preview),
            )

            result = await cleanup_edit_artifacts(
                self.db_path,
                storage_dir,
                edit.id,
                user_id=1,
                preserve_output=True,
            )

            self.assertEqual(result.files_deleted, 4)
            self.assertTrue(final.exists())
            self.assertTrue(subtitles.exists())
            self.assertFalse(staged.exists())
            self.assertFalse(intermediate.exists())
            self.assertFalse(hidden_intermediate.exists())
            self.assertFalse(preview.exists())
            updated = await get_edit_job(self.db_path, edit.id)
            self.assertIsNone(updated.watermark_preview_path)
            self.assertEqual(updated.file_path, str(final))

        asyncio.run(run())

    def test_reset_source_edits_cleans_every_unpooled_artifact(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            artifacts = [
                storage_dir / f"edit-{edit.id}-source.mp4",
                storage_dir / f"edit-{edit.id}-final.mp4",
                storage_dir / f"edit-{edit.id}-final.srt",
                storage_dir / f"edit-{edit.id}-final_voice.mp4",
                storage_dir / f"edit-{edit.id}-watermarks.jpg",
            ]
            for path in artifacts:
                path.write_bytes(b"artifact")
            await update_edit_job(
                self.db_path,
                edit.id,
                status="rendered",
                file_path=str(artifacts[1]),
                subtitles_path=str(artifacts[2]),
                watermark_preview_path=str(artifacts[4]),
            )

            result = await reset_source_edits(
                self.db_path, storage_dir, job.id, 1
            )

            self.assertEqual(result.records_deleted, 1)
            self.assertEqual(result.files_deleted, len(artifacts))
            self.assertIsNone(await get_edit_job(self.db_path, edit.id))
            self.assertFalse(any(path.exists() for path in artifacts))
            self.assertEqual(await foreign_key_violations(self.db_path), [])

        asyncio.run(run())

    def test_reset_preserves_pool_referenced_edit_output(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            staged = storage_dir / f"edit-{edit.id}-source.mp4"
            final = storage_dir / f"edit-{edit.id}-final.mp4"
            staged.write_bytes(b"staged")
            final.write_bytes(b"saved")
            await update_edit_job(
                self.db_path, edit.id, status="rendered", file_path=str(final)
            )
            pool = await create_pool_item(
                self.db_path,
                1,
                str(final),
                source_job_id=job.id,
                edit_job_id=edit.id,
            )

            result = await reset_source_edits(
                self.db_path, storage_dir, job.id, 1
            )

            self.assertTrue(final.exists())
            self.assertFalse(staged.exists())
            self.assertEqual(result.files_preserved, 1)
            saved = await get_pool_item(self.db_path, pool.id)
            self.assertIsNone(saved.edit_job_id)
            self.assertEqual(saved.source_job_id, job.id)

        asyncio.run(run())

    def test_cleanup_refuses_to_unlink_outside_storage_root(self):
        async def run():
            await init_db(self.db_path)
            root = Path(self.tmpdir.name)
            storage_dir = root / "jobs"
            storage_dir.mkdir()
            outside = root / "outside.mp4"
            outside.write_bytes(b"do-not-delete")
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await update_job(self.db_path, job.id, file_path=str(outside))

            result = await delete_job_with_artifacts(
                self.db_path, storage_dir, job.id, user_id=1
            )

            self.assertIsInstance(result, CleanupResult)
            self.assertEqual(result.unsafe_paths, (str(outside.resolve()),))
            self.assertTrue(outside.exists())
            self.assertIsNone(await get_job(self.db_path, job.id))

        asyncio.run(run())

    def test_durable_pool_copy_rejects_source_outside_storage_root(self):
        async def run():
            await init_db(self.db_path)
            root = Path(self.tmpdir.name)
            storage_dir = root / "jobs"
            storage_dir.mkdir()
            outside = root / "outside.mp4"
            outside.write_bytes(b"outside")

            with self.assertRaises(UnsafeStoragePath):
                await create_durable_pool_item(
                    self.db_path, storage_dir, 1, outside
                )
            self.assertTrue(outside.exists())

        asyncio.run(run())

    def test_cleanup_user_jobs_preserves_durable_pool_items(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            source = storage_dir / "source.mp4"
            source.write_bytes(b"source")
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            await update_job(self.db_path, job.id, file_path=str(source))
            pool = await create_durable_pool_item(
                self.db_path, storage_dir, 1, source, source_job_id=job.id
            )

            result = await cleanup_user_jobs(self.db_path, storage_dir, 1)

            self.assertGreaterEqual(result.records_deleted, 1)
            self.assertFalse(source.exists())
            self.assertTrue(Path(pool.file_path).exists())
            self.assertIsNone((await get_pool_item(self.db_path, pool.id)).source_job_id)
            self.assertEqual(await foreign_key_violations(self.db_path), [])

        asyncio.run(run())

    def test_delete_edit_api_checks_owner_before_touching_files(self):
        async def run():
            await init_db(self.db_path)
            storage_dir = Path(self.tmpdir.name) / "jobs"
            storage_dir.mkdir()
            job = await create_job(self.db_path, "https://example.com", 1, 2)
            edit = await create_edit_job(self.db_path, job.id, 1)
            artifact = storage_dir / f"edit-{edit.id}-source.mp4"
            artifact.write_bytes(b"artifact")
            await update_edit_job(self.db_path, edit.id, file_path=str(artifact))

            result = await delete_edit_job_with_artifacts(
                self.db_path, storage_dir, edit.id, user_id=2
            )

            self.assertEqual(result.records_deleted, 0)
            self.assertTrue(artifact.exists())
            self.assertIsNotNone(await get_edit_job(self.db_path, edit.id))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
