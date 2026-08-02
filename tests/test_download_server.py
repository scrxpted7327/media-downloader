import logging
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from media_bot.download_server import create_download_app
from media_bot.storage import (
    consume_download_token,
    create_download_token,
    create_edit_job,
    create_job,
    init_db,
    update_edit_job,
    update_job,
)


class DownloadServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage = root / "jobs"
        self.storage.mkdir()
        self.db_path = root / "media.db"
        await init_db(self.db_path)
        self.access_logger = logging.getLogger("aiohttp.access")
        self.access_logger_was_disabled = self.access_logger.disabled
        self.access_logger.disabled = True
        self.addCleanup(
            setattr,
            self.access_logger,
            "disabled",
            self.access_logger_was_disabled,
        )
        self.client = TestClient(TestServer(create_download_app(self.db_path, self.storage)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.access_logger.disabled = self.access_logger_was_disabled
        self.temporary.cleanup()

    async def test_source_download_is_one_time_and_binary(self):
        media = self.storage / "clip.mp4"
        media.write_bytes(b"video-data")
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(media))
        token = await create_download_token(self.db_path, job.id, 7, 15)

        response = await self.client.get(f"/download/{token}")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"video-data")
        self.assertEqual(response.content_type, "application/octet-stream")
        self.assertIn("clip.mp4", response.headers["Content-Disposition"])

        reused = await self.client.get(f"/download/{token}")
        self.assertEqual(reused.status, 403)

    async def test_healthz_reports_minimal_database_and_storage_readiness(self):
        response = await self.client.get("/healthz")

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"status": "ok"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_healthz_fails_closed_without_storage(self):
        unavailable = self.storage.with_name("jobs-unavailable")
        self.storage.rename(unavailable)

        response = await self.client.get("/healthz")

        self.assertEqual(response.status, 503)
        self.assertEqual(await response.json(), {"status": "unhealthy"})

    async def test_healthz_fails_closed_for_a_corrupt_database(self):
        self.db_path.write_bytes(b"not a sqlite database")

        response = await self.client.get("/healthz")

        self.assertEqual(response.status, 503)
        self.assertEqual(await response.json(), {"status": "unhealthy"})

    async def test_rendered_edit_download(self):
        source = self.storage / "source.mp4"
        rendered = self.storage / "rendered.mp4"
        source.write_bytes(b"source")
        rendered.write_bytes(b"rendered")
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(source))
        edit = await create_edit_job(self.db_path, job.id, 7)
        await update_edit_job(self.db_path, edit.id, file_path=str(rendered))
        token = await create_download_token(
            self.db_path, job.id, 7, 15, edit_job_id=edit.id
        )

        response = await self.client.get(f"/download/{token}")

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"rendered")

    async def test_refuses_file_outside_storage(self):
        outside = Path(self.temporary.name) / "outside.mp4"
        outside.write_bytes(b"private")
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(outside))
        token = await create_download_token(self.db_path, job.id, 7, 15)

        response = await self.client.get(f"/download/{token}")

        self.assertEqual(response.status, 403)
        self.assertEqual(await response.text(), "Access denied")

        outside.unlink()
        inside = self.storage / "recovered.mp4"
        inside.write_bytes(b"recovered")
        await update_job(self.db_path, job.id, file_path=str(inside))
        retry = await self.client.get(f"/download/{token}")
        self.assertEqual(retry.status, 200)
        self.assertEqual(await retry.read(), b"recovered")

    async def test_head_does_not_consume_download_token(self):
        media = self.storage / "head-safe.mp4"
        media.write_bytes(b"video")
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(media))
        token = await create_download_token(self.db_path, job.id, 7, 15)

        response = await self.client.head(f"/download/{token}")
        self.assertEqual(response.status, 405)
        claimed = await consume_download_token(self.db_path, token)
        self.assertIsNotNone(claimed)

    async def test_missing_file_does_not_consume_download_token(self):
        missing = self.storage / "missing.mp4"
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(missing))
        token = await create_download_token(self.db_path, job.id, 7, 15)

        response = await self.client.get(f"/download/{token}")
        self.assertEqual(response.status, 404)
        self.assertIsNotNone(await consume_download_token(self.db_path, token))

    async def test_refuses_symlink_escape_without_consuming_token(self):
        outside = Path(self.temporary.name) / "private.mp4"
        outside.write_bytes(b"private")
        symlink = self.storage / "escape.mp4"
        symlink.symlink_to(outside)
        job = await create_job(self.db_path, "https://example.com/video", 7, 9)
        await update_job(self.db_path, job.id, file_path=str(symlink))
        token = await create_download_token(self.db_path, job.id, 7, 15)

        response = await self.client.get(f"/download/{token}")
        self.assertEqual(response.status, 403)
        self.assertIsNotNone(await consume_download_token(self.db_path, token))


if __name__ == "__main__":
    unittest.main()
