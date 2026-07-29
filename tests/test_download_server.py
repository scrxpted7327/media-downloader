import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from media_bot.download_server import create_download_app
from media_bot.storage import (
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
        self.client = TestClient(TestServer(create_download_app(self.db_path, self.storage)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
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


if __name__ == "__main__":
    unittest.main()
