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
from media_bot.storage import create_or_get_media_asset, init_db, update_media_asset
from media_bot.work_queue import WorkQueue

SECRET = "library-signing-secret"
API_KEY = "library-api-key"


def headers(user_id: str, request_id: str, *, manage: bool = False) -> dict[str, str]:
    permissions = [
        "media.read_own",
        "media.create_own",
        "media.cancel_own",
        "media.delete_own",
        "media.library.read",
        "media.library.download",
        "media.library.variant_request",
    ]
    if manage:
        permissions.append("media.library.manage")
    issued = int(time.time())
    payload = {
        "version": 1,
        "user_id": user_id,
        "username": user_id,
        "permission": permissions[0],
        "permissions": permissions,
        "request_id": request_id,
        "session_id": f"session-{user_id}",
        "issued_at": issued,
        "expires_at": issued + 60,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return {
        "X-Media-Api-Key": API_KEY,
        "X-WatchMyWallet-Client": "pwa-bff",
        "X-WatchMyWallet-Acting-User": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
        "X-WatchMyWallet-Acting-Signature": hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        "X-WatchMyWallet-Request-ID": request_id,
    }


class MediaLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage = root / "jobs"
        self.storage.mkdir()
        self.db_path = root / "media.db"
        await init_db(self.db_path)
        self.queue = WorkQueue(name="download", workers=1, capacity=8, per_user_capacity=4)
        self.service = PwaMediaService(
            db_path=self.db_path,
            storage_dir=self.storage,
            ytdlp=Path("/bin/false"),
            gallerydl=Path("/bin/false"),
            work=self.queue,
            max_filesize_mb=47,
            timeout_seconds=30,
            library_min_free_space_mb=1,
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

    async def test_completed_pwa_job_promotes_and_shared_read_is_not_owner_filtered(self) -> None:
        async def fake_download(*args, **kwargs):
            temporary = tempfile.TemporaryDirectory()
            output = Path(temporary.name) / "fixture.mp4"
            output.write_bytes(b"shared-fixture-media")
            return temporary, output

        self.queue.start()
        with patch("media_bot.pwa_service.download_media", new=fake_download):
            created = await self.service.create_job(
                owner_id="user-a",
                url="https://www.youtube.com/watch?v=shared-fixture",
            )
            for _ in range(100):
                job = await self.service.get_job(owner_id="user-a", job_id=created.id)
                if job is not None and job.status == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("fixture job did not complete")
        await self.queue.stop()

        library_a = await self.client.get("/api/media/library", headers=headers("user-a", "library-a"))
        library_b = await self.client.get("/api/media/library", headers=headers("user-b", "library-b"))
        self.assertEqual(library_a.status, 200)
        self.assertEqual(library_b.status, 200)
        items_a = (await library_a.json())["items"]
        items_b = (await library_b.json())["items"]
        self.assertEqual(len(items_a), 1)
        self.assertEqual([item["asset_id"] for item in items_a], [item["asset_id"] for item in items_b])
        asset_id = items_a[0]["asset_id"]
        variant_id = items_a[0]["variants"][0]["variant_id"]

        streamed = await self.client.get(
            f"/api/media/library/{asset_id}/stream?variant_id={variant_id}",
            headers=headers("user-b", "stream-b"),
        )
        self.assertEqual(streamed.status, 200)
        self.assertEqual(await streamed.read(), b"shared-fixture-media")

        thumbnail = self.storage / "thumbnail.jpg"
        thumbnail.write_bytes(b"thumbnail")
        await update_media_asset(self.db_path, int(asset_id), thumbnail_path=str(thumbnail))
        thumbnail_response = await self.client.get(
            f"/api/media/library/{asset_id}/thumbnail",
            headers=headers("user-b", "thumbnail-b"),
        )
        self.assertEqual(thumbnail_response.status, 200)
        self.assertEqual(await thumbnail_response.read(), b"thumbnail")

        ranged = await self.client.get(
            f"/api/media/library/{asset_id}/stream?variant_id={variant_id}",
            headers={**headers("user-b", "range-b"), "Range": "bytes=0-5"},
        )
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), b"shared")

        invalid = await self.client.get(
            f"/api/media/library/{asset_id}/stream?variant_id={variant_id}",
            headers={**headers("user-b", "range-invalid"), "Range": "bytes=999-1000"},
        )
        self.assertEqual(invalid.status, 416)

    async def test_source_identity_dedupes_concurrent_asset_reservations(self) -> None:
        async def reserve() -> tuple[int, bool]:
            asset, created = await create_or_get_media_asset(
                self.db_path,
                source_platform="youtube",
                source_media_id="same-id",
                source_key="native:youtube:same-id",
                source_canonical_url="https://www.youtube.com/watch?v=same-id",
                title="Same",
                uploader="Fixture",
                duration_seconds=None,
                upload_date=None,
                thumbnail_path=None,
                created_from_job_id=None,
                created_by_owner_id="user-a",
            )
            return asset.id, created

        results = await asyncio.gather(reserve(), reserve())
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(1 for _, created in results if created), 1)

    async def test_video_best_preserves_source_suffix(self) -> None:
        from media_bot.library import preset_extension

        self.assertEqual(preset_extension("video_best", ".webm"), ".webm")
        self.assertEqual(preset_extension("video_720p", ".webm"), ".mp4")


if __name__ == "__main__":
    unittest.main()
