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


class WorkAlreadyQueued(WorkRejected):
    """Raised when the same durable work label is submitted more than once."""


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
        self._items: dict[str, WorkItem] = {}
        self._active: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
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
        if label in self._items:
            raise WorkAlreadyQueued(f"{label} is already queued or running")
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
        self._items[label] = item

    def cancel(self, *, user_id: int, label: str) -> bool:
        """Request cancellation of one queued/running item owned by ``user_id``."""
        item = self._items.get(label)
        if item is None or item.user_id != user_id:
            return False
        self._cancelled.add(label)
        task = self._active.get(label)
        if task is not None:
            task.cancel()
        return True

    def has_label(self, label: str) -> bool:
        return label in self._items

    def is_active(self, label: str) -> bool:
        return label in self._active

    def cancellation_requested(self, label: str) -> bool:
        """Return whether an active item was explicitly cancelled by its owner."""
        return label in self._cancelled

    def items_for_user(self, user_id: int) -> tuple[tuple[str, str], ...]:
        """Return stable labels and states for one user's admitted work."""
        return tuple(
            (
                label,
                "cancelling" if label in self._cancelled
                else "running" if label in self._active
                else "queued",
            )
            for label, item in self._items.items()
            if item.user_id == user_id
        )

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
                self._finish_item(item)
            self._queue.task_done()

    async def _worker(self, index: int) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                if item.label in self._cancelled:
                    continue
                task = asyncio.create_task(
                    item.factory(), name=f"{self.name}:{item.label}",
                )
                self._active[item.label] = task
                await task
            except asyncio.CancelledError:
                # Cancelling an individual child must not tear down its worker.
                # Worker shutdown cancels the worker itself and should propagate.
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
            except Exception:
                LOGGER.exception(
                    "Unhandled %s work failure in worker %d: %s",
                    self.name,
                    index,
                    item.label,
                )
            finally:
                self._active.pop(item.label, None)
                self._finish_item(item)
                self._queue.task_done()

    def _finish_item(self, item: WorkItem) -> None:
        """Release admission state exactly once after work leaves the queue."""
        if self._items.pop(item.label, None) is None:
            return
        self._cancelled.discard(item.label)
        self._pending[item.user_id] -= 1
        if self._pending[item.user_id] <= 0:
            del self._pending[item.user_id]

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    @property
    def active(self) -> int:
        return len(self._active)

    def pending_for_user(self, user_id: int) -> int:
        return self._pending[user_id]
