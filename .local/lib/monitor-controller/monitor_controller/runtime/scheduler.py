"""Persisted monotonic deadline scheduling for the serialized controller."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from monitor_controller.model import BootId, Event, EventMetadata, TimerFired


class SchedulerClock(Protocol):
    """Clock capable of waiting for an absolute monotonic millisecond deadline."""

    def monotonic_ms(self) -> int:
        """Return the current non-negative monotonic time in milliseconds."""
        ...

    async def sleep_until(self, deadline_ms: int) -> None:
        """Sleep until *deadline_ms*, returning immediately when it is overdue."""
        ...


class AsyncioMonotonicClock:
    """Production monotonic clock backed by the running asyncio event loop."""

    def monotonic_ms(self) -> int:
        """Return the event loop's monotonic clock in integer milliseconds."""
        return time.monotonic_ns() // 1_000_000

    async def sleep_until(self, deadline_ms: int) -> None:
        """Wait without using wall-clock time."""
        delay_ms = max(0, deadline_ms - self.monotonic_ms())
        await asyncio.sleep(delay_ms / 1_000)


class DeadlineScheduler:
    """Keep exactly one nearest-deadline producer for a controller event queue."""

    def __init__(
        self,
        queue: asyncio.Queue[Event],
        clock: SchedulerClock,
    ) -> None:
        """Bind an injected queue and clock without starting any task."""
        self._queue = queue
        self._clock = clock
        self._deadline_ms: int | None = None
        self._boot_id: BootId | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def deadline_ms(self) -> int | None:
        """Return the currently armed absolute deadline, if any."""
        return self._deadline_ms

    def arm(self, deadline_ms: int | None, boot_id: BootId) -> None:
        """Replace the current wake with the nearest authoritative deadline."""
        if deadline_ms is not None and deadline_ms < 0:
            msg = "scheduler deadline must be non-negative"
            raise ValueError(msg)
        if (
            deadline_ms == self._deadline_ms
            and boot_id == self._boot_id
            and self._task is not None
            and not self._task.done()
        ):
            return
        self._cancel_task()
        self._deadline_ms = deadline_ms
        self._boot_id = boot_id
        if deadline_ms is not None:
            self._task = asyncio.create_task(
                self._wait(deadline_ms, boot_id),
                name="monitor-controller-deadline",
            )

    async def close(self) -> None:
        """Cancel the producer without emitting a synthetic timer event."""
        task = self._task
        self._deadline_ms = None
        self._boot_id = None
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    async def _wait(self, deadline_ms: int, boot_id: BootId) -> None:
        try:
            await self._clock.sleep_until(deadline_ms)
            now_ms = max(deadline_ms, self._clock.monotonic_ms())
            self._queue.put_nowait(
                TimerFired(EventMetadata(now_ms, boot_id), deadline_ms)
            )
        finally:
            current = asyncio.current_task()
            if self._task is current:
                self._task = None
                self._deadline_ms = None
                self._boot_id = None
