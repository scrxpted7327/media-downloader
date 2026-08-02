import asyncio
import unittest

from media_bot.work_queue import WorkQueue, WorkRejected


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
