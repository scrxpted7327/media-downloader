"""Bounded asynchronous work queues with per-user admission control."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
WorkFactory = Callable[[], Awaitable[None]]


class WorkRejected(RuntimeError):
    """Raised when global or per-user queue capacity is exhausted."""


@dataclass(frozen=True)
class WorkItem:
    user_id: int
    label: str
    factory: WorkFactory


class WorkQueue:
    """Run admitted work with bounded workers and per-user pending limits."""

    def __init__(
        self,
        *,
        name: str,
        workers: int,
        capacity: int,
        per_user_capacity: int,
    ) -> None:
        if min(workers, capacity, per_user_capacity) < 1:
            raise ValueError("work queue limits must be positive")
        self.name = name
        self.workers = workers
        self.capacity = capacity
        self.per_user_capacity = per_user_capacity
        self._queue: asyncio.Queue[WorkItem | None] = asyncio.Queue(maxsize=capacity)
        self._pending: Counter[int] = Counter()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"{self.name}-worker-{index}")
            for index in range(self.workers)
        ]

    def submit(self, *, user_id: int, label: str, factory: WorkFactory) -> None:
        if self._stopping:
            raise WorkRejected(f"{self.name} queue is stopping")
        if self._pending[user_id] >= self.per_user_capacity:
            raise WorkRejected(
                f"you already have {self.per_user_capacity} {self.name} jobs queued or running"
            )
        item = WorkItem(user_id=user_id, label=label, factory=factory)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise WorkRejected(f"{self.name} queue is full") from exc
        self._pending[user_id] += 1

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stopping = True
        # Shutdown must not wait for multi-minute work. Worker cancellation
        # propagates into subprocess cleanup; startup reconciliation marks the
        # durable job rows as interrupted.
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                self._pending[item.user_id] -= 1
                if self._pending[item.user_id] <= 0:
                    del self._pending[item.user_id]
            self._queue.task_done()

    async def _worker(self, index: int) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                await item.factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Unhandled %s work failure in worker %d: %s",
                    self.name,
                    index,
                    item.label,
                )
            finally:
                self._pending[item.user_id] -= 1
                if self._pending[item.user_id] <= 0:
                    del self._pending[item.user_id]
                self._queue.task_done()

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    def pending_for_user(self, user_id: int) -> int:
        return self._pending[user_id]
