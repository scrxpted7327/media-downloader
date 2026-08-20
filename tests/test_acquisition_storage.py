from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from media_bot.acquisition import (
    AcquisitionRequest,
    AcquisitionState,
    DownloadedMedia,
    PromotionResult,
    SourceIdentity,
)
from media_bot.acquisition_storage import AcquisitionStorage, initialize_acquisition_storage


class AcquisitionStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "acquisition.sqlite3"
        self.storage = await initialize_acquisition_storage(self.db_path)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def request(number: int) -> AcquisitionRequest:
        return AcquisitionRequest(
            requester_id=f"requester-{number}",
            source_url="https://example.test/video/?utm_source=ignored&id=7",
            preset="1080p",
            platform="example",
            media_id="video-7",
        )

    async def test_init_is_idempotent_and_admission_joins_atomically(self) -> None:
        await self.storage.init()
        admissions = await asyncio.gather(
            *(self.storage.admit(self.request(i), SourceIdentity.from_request(self.request(i))) for i in range(12))
        )
        self.assertEqual({item.claim.claim_id for item in admissions}, {admissions[0].claim.claim_id})
        self.assertEqual(sum(item.owner for item in admissions), 1)
        self.assertEqual({item.requester.job_id for item in admissions}, {item.requester.job_id for item in admissions})
        rows = await self.storage.list_for_reconciliation()
        self.assertEqual(rows, [])
        claim = await self.storage.get_claim(admissions[0].claim.claim_id)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.identity.source_key, admissions[0].claim.identity.source_key)

    async def test_only_one_concurrent_execution_claim_owner(self) -> None:
        admission = await self.storage.admit(self.request(1), SourceIdentity.from_request(self.request(1)))
        results = await asyncio.gather(
            *(self.storage.claim_for_execution(admission.claim.claim_id) for _ in range(8))
        )
        owners = [result for result in results if result is not None]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0].attempt, 1)
        self.assertEqual((await self.storage.get_claim(admission.claim.claim_id)).state, AcquisitionState.RUNNING)

    async def test_requesters_have_independent_state_and_output(self) -> None:
        first = await self.storage.admit(self.request(1), SourceIdentity.from_request(self.request(1)))
        second = await self.storage.admit(self.request(2), SourceIdentity.from_request(self.request(2)))
        output = PromotionResult("asset-1", "variant-1", {"mime": "video/mp4"})
        await self.storage.set_requester(first.requester.job_id, AcquisitionState.COMPLETED, output=output)
        await self.storage.set_requester(second.requester.job_id, AcquisitionState.FAILED, error_code="delivery", error_message="x" * 10000)
        saved_first = await self.storage.get_requester(first.requester.job_id)
        saved_second = await self.storage.get_requester(second.requester.job_id)
        self.assertEqual(saved_first.output, output)
        self.assertEqual(saved_first.state, AcquisitionState.COMPLETED)
        self.assertEqual(saved_second.state, AcquisitionState.FAILED)
        self.assertEqual(len(saved_second.error_message), 4096)

    async def test_durable_claim_transition_and_waiter_wakeup(self) -> None:
        admission = await self.storage.admit(self.request(1), SourceIdentity.from_request(self.request(1)))
        waiter_storage = AcquisitionStorage(self.db_path)
        waiting = asyncio.create_task(waiter_storage.wait_for_claim(admission.claim.claim_id))
        await asyncio.sleep(0.05)
        media = DownloadedMedia(Path(self.temp.name) / "video.mp4", {"size": 42})
        await self.storage.set_claim(admission.claim.claim_id, AcquisitionState.PROCESSING, output=media)
        await self.storage.set_claim(
            admission.claim.claim_id,
            AcquisitionState.COMPLETED,
            promotion=PromotionResult("asset-1", "variant-1"),
        )
        result = await asyncio.wait_for(waiting, timeout=2)
        self.assertEqual(result.state, AcquisitionState.COMPLETED)
        self.assertEqual(result.output.path, media.path)
        self.assertEqual(result.promotion.asset_id, "asset-1")

    async def test_reconciliation_is_read_only(self) -> None:
        admission = await self.storage.admit(self.request(1), SourceIdentity.from_request(self.request(1)))
        await self.storage.claim_for_execution(admission.claim.claim_id)
        await self.storage.set_requester(admission.requester.job_id, AcquisitionState.RUNNING)
        before = await self.storage.get_claim(admission.claim.claim_id)
        rows = await self.storage.list_for_reconciliation()
        after = await self.storage.get_claim(admission.claim.claim_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].state, AcquisitionState.RUNNING)
        self.assertEqual(before, after)
        self.assertEqual(rows[0][1][0].state, AcquisitionState.RUNNING)


if __name__ == "__main__":
    unittest.main()
