from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from media_bot.api import MediaApiRuntime, create_media_api_app
from media_bot.pwa_service import PwaMediaService
from media_bot.storage import create_job, init_db, update_job
from media_bot.work_queue import WorkQueue


SECRET = "test-signing-secret"
API_KEY = "test-media-api-key"


def _headers(user_id: str, request_id: str, *, issued_at: int | None = None) -> dict[str, str]:
    issued = int(time.time()) if issued_at is None else issued_at
    payload = {
        "version": 1,
        "user_id": user_id,
        "username": user_id,
        "permission": "media.read_own",
        "permissions": ["media.read_own", "media.create_own", "media.cancel_own", "media.delete_own"],
        "request_id": request_id,
        "session_id": f"session-{user_id}",
        "issued_at": issued,
        "expires_at": issued + 60,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "X-Media-Api-Key": API_KEY,
        "X-WatchMyWallet-Client": "pwa-bff",
        "X-WatchMyWallet-Acting-User": encoded,
        "X-WatchMyWallet-Acting-Signature": signature,
        "X-WatchMyWallet-Request-ID": request_id,
    }


class MediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage = root / "jobs"
        self.storage.mkdir()
        self.db_path = root / "media.db"
        await init_db(self.db_path)
        self.queue = WorkQueue(name="test-download", workers=1, capacity=8, per_user_capacity=4)
        self.service = PwaMediaService(
            db_path=self.db_path,
            storage_dir=self.storage,
            ytdlp=Path("/bin/false"),
            gallerydl=Path("/bin/false"),
            work=self.queue,
            max_filesize_mb=47,
            timeout_seconds=30,
        )
        self.client = TestClient(
            TestServer(
                create_media_api_app(
                    MediaApiRuntime(
                        service=self.service,
                        api_key=API_KEY,
                        signing_secret=SECRET,
                    )
                )
            )
        )
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary.cleanup()

    async def test_owner_is_derived_from_signed_context_and_jobs_are_isolated(self) -> None:
        response = await self.client.post(
            "/api/media/jobs",
            headers=_headers("user-a", "create-a"),
            json={"url": "https://www.youtube.com/watch?v=one", "format": "video", "quality": "720p", "owner_user_id": "user-b"},
        )
        self.assertEqual(response.status, 201)
        job = await response.json()
        self.assertEqual(job["source_channel"], "PWA")

        mine = await self.client.get("/api/media/jobs", headers=_headers("user-a", "list-a"))
        self.assertEqual([item["job_id"] for item in (await mine.json())["items"]], [job["job_id"]])
        other = await self.client.get("/api/media/jobs", headers=_headers("user-b", "list-b"))
        self.assertEqual((await other.json())["items"], [])
        hidden = await self.client.get(
            f"/api/media/jobs/{job['job_id']}", headers=_headers("user-b", "detail-b")
        )
        self.assertEqual(hidden.status, 404)

    async def test_service_auth_expiry_and_replay_are_rejected(self) -> None:
        missing = await self.client.get("/api/media/jobs", headers=_headers("user-a", "missing"))
        self.assertEqual(missing.status, 200)
        missing = await self.client.get(
            "/api/media/jobs",
            headers={**_headers("user-a", "missing-key"), "X-Media-Api-Key": "wrong"},
        )
        self.assertEqual(missing.status, 401)
        expired = await self.client.get(
            "/api/media/jobs",
            headers=_headers("user-a", "expired", issued_at=int(time.time()) - 120),
        )
        self.assertEqual(expired.status, 401)

        headers = _headers("user-a", "replay-create")
        first = await self.client.post(
            "/api/media/jobs",
            headers=headers,
            json={"url": "https://www.youtube.com/watch?v=two"},
        )
        replay = await self.client.post(
            "/api/media/jobs",
            headers=headers,
            json={"url": "https://www.youtube.com/watch?v=three"},
        )
        self.assertEqual(first.status, 201)
        self.assertEqual(replay.status, 409)

    async def test_telegram_namespace_is_not_in_pwa_queries(self) -> None:
        telegram = await create_job(self.db_path, "https://www.youtube.com/watch?v=telegram", 7, 9)
        self.assertEqual(telegram.owner_kind, "telegram")
        response = await self.client.get("/api/media/jobs", headers=_headers("7", "list-telegram"))
        self.assertEqual((await response.json())["items"], [])

    async def test_owner_only_result_delivery(self) -> None:
        response = await self.client.post(
            "/api/media/jobs",
            headers=_headers("user-a", "result-create"),
            json={"url": "https://www.youtube.com/watch?v=result"},
        )
        job = await response.json()
        media = self.storage / "result.mp4"
        media.write_bytes(b"private-media")
        await update_job(
            self.db_path,
            int(job["job_id"]),
            status="completed",
            phase="completed",
            progress_percent=100,
            file_path=str(media),
            output_filename="result.mp4",
            output_mime_type="video/mp4",
        )
        allowed = await self.client.get(
            f"/api/media/jobs/{job['job_id']}/result",
            headers=_headers("user-a", "result-read"),
        )
        self.assertEqual(allowed.status, 200)
        self.assertEqual(await allowed.read(), b"private-media")
        denied = await self.client.get(
            f"/api/media/jobs/{job['job_id']}/result",
            headers=_headers("user-b", "result-denied"),
        )
        self.assertEqual(denied.status, 404)

    async def test_pwa_job_worker_persists_real_terminal_state(self) -> None:
        async def fake_download(*args, **kwargs):
            temporary = tempfile.TemporaryDirectory()
            output = Path(temporary.name) / "fixture.txt"
            output.write_bytes(b"controlled fixture output")
            return temporary, output

        self.queue.start()
        with patch("media_bot.pwa_service.download_media", new=fake_download):
            job = await self.service.create_job(
                owner_id="user-a",
                url="https://www.youtube.com/watch?v=fixture",
                requested_format="video",
                requested_quality="best",
            )
            for _ in range(100):
                current = await self.service.get_job(owner_id="user-a", job_id=job.id)
                if current is not None and current.status == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("controlled PWA job did not complete")
        await self.queue.stop()

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.owner_kind, "watchmywallet")
        self.assertEqual(current.owner_id, "user-a")
        self.assertEqual(current.source_channel, "PWA")
        self.assertEqual(current.phase, "completed")
        self.assertEqual(current.progress_percent, 100.0)
        self.assertTrue(current.file_path)
        self.assertTrue(Path(current.file_path).is_file())


if __name__ == "__main__":
    unittest.main()
