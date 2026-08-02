import asyncio
import unittest

from media_bot.work_queue import WorkAlreadyQueued, WorkQueue, WorkRejected


class WorkQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounds_per_user_and_keeps_worker_capacity(self):
        queue = WorkQueue(name="download", workers=1, capacity=2, per_user_capacity=1)
        queue.start()
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()

        queue.submit(user_id=1, label="one", factory=blocked)
        await started.wait()
        with self.assertRaises(WorkRejected):
            queue.submit(user_id=1, label="two", factory=blocked)

        queue.submit(user_id=2, label="other", factory=lambda: asyncio.sleep(0))
        release.set()
        await asyncio.wait_for(queue._queue.join(), timeout=2)
        await queue.stop()
        self.assertEqual(queue.pending_for_user(1), 0)
        self.assertEqual(queue.pending_for_user(2), 0)

    async def test_rejects_global_overflow_without_creating_coroutine(self):
        queue = WorkQueue(name="render", workers=1, capacity=1, per_user_capacity=2)
        calls = 0

        async def work():
            nonlocal calls
            calls += 1

        queue.submit(user_id=1, label="one", factory=work)
        with self.assertRaises(WorkRejected):
            queue.submit(user_id=1, label="two", factory=work)
        self.assertEqual(calls, 0)

    async def test_stop_cancels_running_work_and_drains_pending_items(self):
        queue = WorkQueue(
            name="test", workers=1, capacity=4, per_user_capacity=4,
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def long_work():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        queue.start()
        queue.submit(user_id=1, label="running", factory=long_work)
        queue.submit(user_id=1, label="queued", factory=long_work)
        await started.wait()
        await queue.stop()

        self.assertTrue(cancelled.is_set())
        self.assertEqual(queue.pending_for_user(1), 0)
        self.assertEqual(queue.queued, 0)

    async def test_duplicate_label_is_rejected_without_disturbing_original(self):
        queue = WorkQueue(name="render", workers=1, capacity=2, per_user_capacity=2)
        release = asyncio.Event()
        queue.start()
        queue.submit(user_id=1, label="render:7", factory=release.wait)

        with self.assertRaises(WorkAlreadyQueued):
            queue.submit(user_id=1, label="render:7", factory=release.wait)

        release.set()
        await asyncio.wait_for(queue._queue.join(), timeout=2)
        await queue.stop()
        self.assertEqual(queue.pending_for_user(1), 0)

    async def test_cancel_queued_and_running_work_by_owner(self):
        queue = WorkQueue(name="download", workers=1, capacity=3, per_user_capacity=3)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def running():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        queue.start()
        queue.submit(user_id=1, label="download:1", factory=running)
        queue.submit(user_id=1, label="download:2", factory=running)
        await started.wait()

        self.assertEqual(
            dict(queue.items_for_user(1)),
            {"download:1": "running", "download:2": "queued"},
        )

        self.assertFalse(queue.cancel(user_id=2, label="download:1"))
        self.assertTrue(queue.cancel(user_id=1, label="download:2"))
        self.assertTrue(queue.cancel(user_id=1, label="download:1"))
        await asyncio.wait_for(queue._queue.join(), timeout=2)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(queue.active, 0)
        self.assertEqual(queue.pending_for_user(1), 0)
        await queue.stop()
