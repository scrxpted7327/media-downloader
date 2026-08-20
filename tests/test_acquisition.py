from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path

from media_bot.acquisition import (
    AcquisitionError,
    AcquisitionLifecycle,
    AcquisitionRequest,
    AcquisitionState,
    Admission,
    ClaimRecord,
    DownloadedMedia,
    ErrorEvent,
    ProgressEvent,
    PromotionResult,
    RequesterRecord,
    ResultEvent,
    RetryPolicy,
    SourceIdentity,
    normalize_source_url,
)


class FakePersistence:
    def __init__(self) -> None:
        self.claims: dict[str, ClaimRecord] = {}
        self.requesters: dict[str, RequesterRecord] = {}
        self.by_key: dict[tuple[str, str], str] = {}
        self.lock = asyncio.Lock()
        self.changed: dict[str, asyncio.Event] = {}
        self.mutations: list[tuple[str, str, AcquisitionState]] = []
        self.next_claim = 1
        self.next_job = 1

    async def admit(self, request: AcquisitionRequest, identity: SourceIdentity) -> Admission:
        async with self.lock:
            key = (identity.source_key, identity.preset)
            claim_id = self.by_key.get(key)
            owner = claim_id is None
            if owner:
                claim_id = f"claim-{self.next_claim}"
                self.next_claim += 1
                self.by_key[key] = claim_id
                self.claims[claim_id] = ClaimRecord(claim_id, identity, AcquisitionState.QUEUED)
                self.changed[claim_id] = asyncio.Event()
            job_id = f"job-{self.next_job}"
            self.next_job += 1
            requester = RequesterRecord(job_id, claim_id, request.requester_id, AcquisitionState.QUEUED)
            self.requesters[job_id] = requester
            return Admission(requester, self.claims[claim_id], owner)

    async def claim_for_execution(self, claim_id: str) -> ClaimRecord | None:
        async with self.lock:
            claim = self.claims[claim_id]
            if claim.state != AcquisitionState.QUEUED:
                return None
            claim = replace(claim, state=AcquisitionState.RUNNING, attempt=claim.attempt + 1)
            self.claims[claim_id] = claim
            self.mutations.append(("claim", claim_id, claim.state))
            return claim

    async def wait_for_claim(self, claim_id: str) -> ClaimRecord:
        while True:
            claim = await self.get_claim(claim_id)
            if claim.state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
                return claim
            await self.changed[claim_id].wait()
            self.changed[claim_id].clear()

    async def get_requester(self, job_id: str) -> RequesterRecord | None:
        return self.requesters.get(job_id)

    async def get_claim(self, claim_id: str) -> ClaimRecord | None:
        return self.claims.get(claim_id)

    async def set_claim(self, claim_id: str, state: AcquisitionState, **values: object) -> ClaimRecord:
        claim = replace(self.claims[claim_id], state=state, **values)
        self.claims[claim_id] = claim
        self.mutations.append(("claim", claim_id, state))
        if state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
            self.changed[claim_id].set()
        return claim

    async def set_requester(self, job_id: str, state: AcquisitionState, **values: object) -> RequesterRecord:
        requester = replace(self.requesters[job_id], state=state, **values)
        self.requesters[job_id] = requester
        self.mutations.append(("requester", job_id, state))
        return requester

    async def list_for_reconciliation(self):
        by_claim: dict[str, list[RequesterRecord]] = {claim_id: [] for claim_id in self.claims}
        for requester in self.requesters.values():
            by_claim[requester.claim_id].append(requester)
        return [(claim, by_claim[claim_id]) for claim_id, claim in self.claims.items()]


class FakeCancellation:
    def __init__(self) -> None:
        self.cancelled: set[str] = set()

    async def requested(self, job_id: str) -> bool:
        return job_id in self.cancelled

    async def signal(self, job_id: str) -> None:
        self.cancelled.add(job_id)


class FakeDownloader:
    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.block = block

    async def download(self, identity, progress):
        self.calls += 1
        await progress(25)
        if self.block is not None:
            await self.block.wait()
        return DownloadedMedia(Path("/tmp/acquired-media"), {"source": identity.source_key})


class FakePromoter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def promote(self, identity, media):
        self.calls += 1
        if self.error:
            raise self.error
        return PromotionResult("asset-1", "variant-1")


class CancelAfterDownload(FakeDownloader):
    def __init__(self, cancellation: FakeCancellation) -> None:
        super().__init__()
        self.cancellation = cancellation
        self.job_id: str | None = None

    async def download(self, identity, progress):
        result = await super().download(identity, progress)
        assert self.job_id is not None
        await self.cancellation.signal(self.job_id)
        return result


class CancelOnProcessing(FakePersistence):
    def __init__(self, cancellation: FakeCancellation) -> None:
        super().__init__()
        self.cancellation = cancellation
        self.job_id: str | None = None

    async def set_claim(self, claim_id: str, state: AcquisitionState, **values: object) -> ClaimRecord:
        result = await super().set_claim(claim_id, state, **values)
        if state == AcquisitionState.PROCESSING:
            assert self.job_id is not None
            await self.cancellation.signal(self.job_id)
        return result


class EventSink:
    def __init__(self, persistence: FakePersistence) -> None:
        self.persistence = persistence
        self.events: list[object] = []

    async def emit(self, event) -> None:
        self.events.append(event)
        requester = self.persistence.requesters[event.job_id]
        self.asserted_state = requester.state
        if isinstance(event, (ProgressEvent, ResultEvent, ErrorEvent)):
            assert event.state == requester.state


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    def make_lifecycle(self, *, promoter: FakePromoter | None = None, downloader: FakeDownloader | None = None):
        persistence = FakePersistence()
        cancellation = FakeCancellation()
        sink = EventSink(persistence)
        lifecycle = AcquisitionLifecycle(
            persistence=persistence,
            downloader=downloader or FakeDownloader(),
            promoter=promoter or FakePromoter(),
            cancellation=cancellation,
            progress=sink,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        return lifecycle, persistence, cancellation, sink

    async def test_normalizes_source_and_preset_identity(self):
        self.assertEqual(
            normalize_source_url("HTTPS://Example.COM/watch/?b=2&utm_source=x&a=1"),
            "https://example.com/watch?a=1&b=2",
        )
        lifecycle, persistence, _, _ = self.make_lifecycle()
        first = await lifecycle.submit(AcquisitionRequest("a", "https://x.test/?utm_medium=x", " VIDEO_BEST "))
        second = await lifecycle.submit(AcquisitionRequest("b", "https://x.test/", "video_best"))
        self.assertEqual(first.claim_id, second.claim_id)
        self.assertEqual(len(persistence.claims), 1)

    async def test_duplicate_claim_has_one_download_and_independent_requesters_complete(self):
        lifecycle, persistence, _, _ = self.make_lifecycle()
        one = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        two = await lifecycle.submit(AcquisitionRequest("b", "https://example.test/media", "video_best"))
        results = await asyncio.gather(lifecycle.run(one.job_id), lifecycle.run(two.job_id))
        self.assertEqual(results[0].state, AcquisitionState.COMPLETED)
        self.assertEqual(results[1].state, AcquisitionState.COMPLETED)
        self.assertNotEqual(one.job_id, two.job_id)
        self.assertEqual(persistence.claims[one.claim_id].attempt, 1)

    async def test_cancellation_at_claim_checkpoint_is_durable(self):
        lifecycle, persistence, cancellation, sink = self.make_lifecycle()
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        await cancellation.signal(requester.job_id)
        result = await lifecycle.run(requester.job_id)
        self.assertEqual(result.state, AcquisitionState.CANCELLED)
        self.assertEqual(persistence.claims[requester.claim_id].state, AcquisitionState.CANCELLED)
        self.assertTrue(any(isinstance(event, ErrorEvent) and event.code == "cancelled" for event in sink.events))

    async def test_cancellation_at_download_checkpoint_stops_before_promotion(self):
        persistence = FakePersistence()
        cancellation = FakeCancellation()
        downloader = CancelAfterDownload(cancellation)
        promoter = FakePromoter()
        sink = EventSink(persistence)
        lifecycle = AcquisitionLifecycle(
            persistence=persistence, downloader=downloader, promoter=promoter,
            cancellation=cancellation, progress=sink,
        )
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        downloader.job_id = requester.job_id
        result = await lifecycle.run(requester.job_id)
        self.assertEqual(result.state, AcquisitionState.CANCELLED)
        self.assertEqual(promoter.calls, 0)

    async def test_cancellation_at_promotion_checkpoint_preserves_no_new_promotion(self):
        cancellation = FakeCancellation()
        persistence = CancelOnProcessing(cancellation)
        sink = EventSink(persistence)
        promoter = FakePromoter()
        lifecycle = AcquisitionLifecycle(
            persistence=persistence, downloader=FakeDownloader(), promoter=promoter,
            cancellation=cancellation, progress=sink,
        )
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        persistence.job_id = requester.job_id
        result = await lifecycle.run(requester.job_id)
        self.assertEqual(result.state, AcquisitionState.CANCELLED)
        self.assertEqual(promoter.calls, 0)

    async def test_promotion_failure_does_not_fail_acquisition(self):
        lifecycle, persistence, _, sink = self.make_lifecycle(
            promoter=FakePromoter(AcquisitionError("promotion_failed", "library unavailable")),
        )
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        result = await lifecycle.run(requester.job_id)
        self.assertEqual(result.state, AcquisitionState.COMPLETED)
        self.assertEqual(persistence.claims[requester.claim_id].state, AcquisitionState.COMPLETED)
        self.assertTrue(any(isinstance(event, ErrorEvent) and event.code == "promotion_failed" for event in sink.events))
        self.assertEqual(sum(isinstance(event, ResultEvent) for event in sink.events), 1)

    async def test_events_are_emitted_after_durable_mutations(self):
        lifecycle, persistence, _, sink = self.make_lifecycle()
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        await lifecycle.run(requester.job_id)
        self.assertGreater(len(sink.events), 3)
        requester_states = [state for kind, job_id, state in persistence.mutations if kind == "requester" and job_id == requester.job_id]
        self.assertIn(AcquisitionState.COMPLETED, requester_states)
        self.assertEqual(sink.events[-1].state, AcquisitionState.COMPLETED)

    async def test_restart_reconciliation_requeues_inflight_claim_and_requester(self):
        lifecycle, persistence, _, _ = self.make_lifecycle()
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        await persistence.set_claim(requester.claim_id, AcquisitionState.RUNNING)
        await persistence.set_requester(requester.job_id, AcquisitionState.PROCESSING)
        self.assertEqual(await lifecycle.reconcile(), 1)
        self.assertEqual(persistence.claims[requester.claim_id].state, AcquisitionState.QUEUED)
        self.assertEqual(persistence.requesters[requester.job_id].state, AcquisitionState.QUEUED)

    async def test_retry_policy_is_bounded_and_delivery_cancellation_does_not_retroactively_cancel(self):
        lifecycle, _, cancellation, _ = self.make_lifecycle()
        requester = await lifecycle.submit(AcquisitionRequest("a", "https://example.test/media", "video_best"))
        await lifecycle.run(requester.job_id)
        self.assertFalse(await lifecycle.retry(requester.job_id))
        await cancellation.signal(requester.job_id)
        delivered = await lifecycle.record_delivery(requester.job_id, success=True)
        self.assertEqual(delivered.state, AcquisitionState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
