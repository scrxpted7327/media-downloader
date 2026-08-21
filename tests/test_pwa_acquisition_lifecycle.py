from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from media_bot.acquisition import AcquisitionState, DownloadedMedia, PromotionResult
from media_bot.pwa_service import PwaMediaService
from media_bot.storage import init_db


class _PassiveWorkQueue:
    """Keep admission from starting work; the test invokes both workers directly."""

    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []

    def submit(self, *, user_id: str, label: str, factory) -> None:
        del factory
        self.submissions.append((user_id, label))

    def cancellation_requested(self, label: str) -> bool:
        del label
        return False


class _CountingDownloader:
    def __init__(self, output_path: Path) -> None:
        self.calls = 0
        self.output_path = output_path
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, identity, progress) -> DownloadedMedia:
        self.calls += 1
        self.started.set()
        await progress(25)
        await self.release.wait()
        return DownloadedMedia(
            self.output_path,
            {
                "source_details": {
                    "title": "duplicate fixture",
                    "source_caption": "shared acquisition",
                },
                "output_filename": self.output_path.name,
                "output_mime_type": "video/mp4",
            },
        )


class _CountingPromoter:
    def __init__(self) -> None:
        self.calls = 0

    async def promote(self, identity, media) -> PromotionResult:
        del media
        self.calls += 1
        return PromotionResult(
            "asset-duplicate",
            "variant-duplicate",
            {"preset_key": identity.preset},
        )


class PwaAcquisitionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_requesters_share_one_durable_claim_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "media.db"
            storage_dir = root / "jobs"
            storage_dir.mkdir()
            await init_db(db_path)

            service = PwaMediaService(
                db_path=db_path,
                storage_dir=storage_dir,
                ytdlp=Path("/bin/false"),
                gallerydl=Path("/bin/false"),
                work=_PassiveWorkQueue(),
                max_filesize_mb=47,
                timeout_seconds=30,
            )
            downloader = _CountingDownloader(storage_dir / "shared-output.mp4")
            downloader.output_path.write_bytes(b"shared acquisition output")
            promoter = _CountingPromoter()
            service.acquisition_lifecycle.downloader = downloader
            service.acquisition_lifecycle.promoter = promoter

            first = await service.create_job(
                owner_id="user-a",
                url="https://www.youtube.com/watch?v=duplicate-fixture",
            )
            second = await service.create_job(
                owner_id="user-b",
                url="https://www.youtube.com/watch?v=duplicate-fixture",
            )

            first_metadata = json.loads(first.output_metadata or "{}")
            second_metadata = json.loads(second.output_metadata or "{}")
            self.assertNotEqual(
                first_metadata["acquisition_job_id"],
                second_metadata["acquisition_job_id"],
            )
            self.assertEqual(
                first_metadata["acquisition_claim_id"],
                second_metadata["acquisition_claim_id"],
            )

            first_task = asyncio.create_task(
                service._process_job(first.id, "user-a")
            )
            second_task = asyncio.create_task(
                service._process_job(second.id, "user-b")
            )
            await asyncio.wait_for(downloader.started.wait(), timeout=1)
            downloader.release.set()
            await asyncio.gather(first_task, second_task)

            claim_id = first_metadata["acquisition_claim_id"]
            claim = await service.acquisition_storage.get_claim(claim_id)
            self.assertIsNotNone(claim)
            assert claim is not None
            self.assertEqual(claim.claim_id, claim_id)
            self.assertEqual(claim.state, AcquisitionState.COMPLETED)
            self.assertEqual(claim.attempt, 1)

            requesters = [
                await service.acquisition_storage.get_requester(
                    first_metadata["acquisition_job_id"]
                ),
                await service.acquisition_storage.get_requester(
                    second_metadata["acquisition_job_id"]
                ),
            ]
            self.assertTrue(all(requester is not None for requester in requesters))
            for requester in requesters:
                assert requester is not None
                self.assertEqual(requester.claim_id, claim_id)
                self.assertEqual(requester.state, AcquisitionState.COMPLETED)
            self.assertEqual(downloader.calls, 1)
            self.assertEqual(promoter.calls, 1)

            completed_jobs = [
                await service.get_job(owner_id=owner_id, job_id=job_id)
                for owner_id, job_id in (("user-a", first.id), ("user-b", second.id))
            ]
            self.assertTrue(all(job is not None and job.status == "completed" for job in completed_jobs))
            for job in completed_jobs:
                assert job is not None
                metadata = json.loads(job.output_metadata or "{}")
                self.assertEqual(metadata["library_asset_id"], "asset-duplicate")
                self.assertEqual(metadata["library_variant_id"], "variant-duplicate")


if __name__ == "__main__":
    unittest.main()
