import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from media_bot.storage import (
    cleanup_download_messages,
    cleanup_old_jobs,
    create_job,
    get_or_create_classification,
    get_or_create_user_settings,
    init_db,
    list_undeleted_download_messages,
    open_database,
    store_download_message,
    update_job,
)


class StorageGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "media.db"
        await init_db(self.db_path)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_get_or_create_settings_is_race_safe(self):
        settings = await asyncio.gather(*(
            get_or_create_user_settings(self.db_path, 7) for _ in range(8)
        ))

        self.assertEqual({item.user_id for item in settings}, {7})

    async def test_get_or_create_classification_is_race_safe(self):
        classifications = await asyncio.gather(*(
            get_or_create_classification(self.db_path, "sports") for _ in range(8)
        ))

        self.assertEqual(len({item.id for item in classifications}), 1)

    async def test_dynamic_updates_reject_invariant_columns(self):
        job = await create_job(self.db_path, "https://example.com/video", 1, 1)

        with self.assertRaisesRegex(ValueError, "unsupported jobs update field"):
            await update_job(self.db_path, job.id, user_id=999)

    async def test_transient_message_delete_failure_is_retried(self):
        record_id = await store_download_message(self.db_path, 4, 9, 1)
        async with open_database(self.db_path) as db:
            await db.execute(
                "UPDATE download_messages SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", record_id),
            )
            await db.commit()
        bot = AsyncMock()
        bot.delete_message.side_effect = RuntimeError("temporary network failure")

        removed = await cleanup_download_messages(self.db_path, bot)

        self.assertEqual(removed, 0)
        self.assertEqual(len(await list_undeleted_download_messages(self.db_path)), 1)

    async def test_retention_uses_completion_update_time(self):
        storage = Path(self.temporary.name) / "jobs"
        storage.mkdir()
        media = storage / "clip.mp4"
        media.write_bytes(b"clip")
        job = await create_job(self.db_path, "https://example.com/clip", 1, 1)
        await update_job(
            self.db_path, job.id, status="uploaded", file_path=str(media),
        )
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        async with open_database(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?", (old, job.id),
            )
            await db.commit()

        removed = await cleanup_old_jobs(self.db_path, storage, retention_days=7)

        self.assertEqual(removed, 0)
        self.assertTrue(media.is_file())


if __name__ == "__main__":
    unittest.main()
