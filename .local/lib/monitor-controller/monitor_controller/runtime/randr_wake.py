"""Wake the controller when someone changes the X screen layout by hand.

Switching a still-plugged-in monitor off or on (xfce4-display-settings,
``xrandr``) raises no DRM uevent, so without this the controller never looks
and the desktop keeps a layout for screens that are gone (dc-8xns).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from monitor_controller.runtime.journal import service_logger

_JOURNAL = service_logger("monitor_controller.journal")

# Line-buffered, or xev holds events in its stdout buffer indefinitely.
RANDR_EVENTS_COMMAND = ("stdbuf", "-oL", "xev", "-root", "-event", "randr")
# One display change emits a burst of screen, CRTC and output events.
SETTLE_SECONDS = 1.0
RESTART_SECONDS = 30.0


class RandrWakeMonitor:
    """Hint once per burst of RandR events while *may_hint* allows it."""

    def __init__(
        self,
        *,
        may_hint: Callable[[], bool],
        command: Sequence[str] = RANDR_EVENTS_COMMAND,
        settle_seconds: float = SETTLE_SECONDS,
        restart_seconds: float = RESTART_SECONDS,
    ) -> None:
        """Bind the gate that keeps the controller's own changes from echoing."""
        self._may_hint = may_hint
        self._command = tuple(command)
        self._settle_seconds = settle_seconds
        self._restart_seconds = restart_seconds

    async def run(self, notify: Callable[[], object]) -> None:
        """Follow RandR events until cancelled, restarting a dead listener."""
        while True:
            try:
                await self.follow(notify)
            except OSError as error:
                _JOURNAL.warning(f"cannot follow RandR events: {error}")
            await asyncio.sleep(self._restart_seconds)

    async def follow(self, notify: Callable[[], object]) -> None:
        """Run one listener until its output ends."""
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None  # noqa: S101 - PIPE requested
        try:
            while line := await process.stdout.readline():
                if not line.startswith(b"RR"):
                    continue
                await self._drain_burst(process.stdout)
                # A quiescent controller made no change, so this one was manual.
                if self._may_hint():
                    _JOURNAL.info("X screen layout changed; re-observing")
                    notify()
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _drain_burst(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await asyncio.wait_for(
                    stream.readline(), timeout=self._settle_seconds
                )
            except TimeoutError:
                return
            if not line:
                return
