"""Deterministic nearest-deadline and no-event progress tests."""

from __future__ import annotations

import asyncio
from uuid import UUID

from monitor_controller.model import BootId, Event, TimerFired
from monitor_controller.runtime.scheduler import DeadlineScheduler

_BOOT = BootId(UUID(int=801))


class _FakeClock:
    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms
        self.waiters: list[tuple[int, asyncio.Event]] = []

    def monotonic_ms(self) -> int:
        return self.now_ms

    async def sleep_until(self, deadline_ms: int) -> None:
        if deadline_ms <= self.now_ms:
            await asyncio.sleep(0)
            return
        event = asyncio.Event()
        self.waiters.append((deadline_ms, event))
        await event.wait()

    def advance(self, now_ms: int) -> None:
        assert now_ms >= self.now_ms
        self.now_ms = now_ms
        for deadline, event in tuple(self.waiters):
            if deadline <= now_ms:
                event.set()
        self.waiters = [item for item in self.waiters if not item[1].is_set()]


def test_overdue_persisted_deadline_fires_without_an_external_event() -> None:
    async def exercise() -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        clock = _FakeClock(now_ms=500)
        scheduler = DeadlineScheduler(queue, clock)

        scheduler.arm(100, _BOOT)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        event = queue.get_nowait()

        assert isinstance(event, TimerFired)
        assert event.deadline_ms == 100
        assert event.metadata.processed_at_ms == 500
        await scheduler.close()

    asyncio.run(exercise())


def test_rearming_keeps_only_the_new_nearest_deadline() -> None:
    async def exercise() -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        clock = _FakeClock()
        scheduler = DeadlineScheduler(queue, clock)

        scheduler.arm(1_000, _BOOT)
        await asyncio.sleep(0)
        scheduler.arm(250, _BOOT)
        await asyncio.sleep(0)
        clock.advance(250)
        await asyncio.sleep(0)

        event = queue.get_nowait()
        assert isinstance(event, TimerFired)
        assert event.deadline_ms == 250
        assert queue.empty()

        clock.advance(1_000)
        await asyncio.sleep(0)
        assert queue.empty()
        await scheduler.close()

    asyncio.run(exercise())


def test_cancelling_deadline_prevents_a_late_stale_wake() -> None:
    async def exercise() -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        clock = _FakeClock()
        scheduler = DeadlineScheduler(queue, clock)

        scheduler.arm(100, _BOOT)
        await asyncio.sleep(0)
        scheduler.arm(None, _BOOT)
        clock.advance(100)
        await asyncio.sleep(0)

        assert queue.empty()
        assert scheduler.deadline_ms is None
        await scheduler.close()

    asyncio.run(exercise())
