"""Wake the controller when screens blanked at bedtime come back on.

The reducer defers lighting work while the latest observation saw DPMS
asleep (dc-sv3q). DPMS changes raise no DRM or X event the controller hears,
so this polls DPMS alone, and only while that deferral can be in force.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from monitor_controller.model import (
    CanonicalObservation,
    DisplayPower,
    ObservationGeneration,
)
from monitor_controller.runtime.journal import service_logger

DISPLAY_WAKE_POLL_SECONDS = 2.0
_JOURNAL = service_logger("monitor_controller.journal")


class DisplayWakeMonitor:
    """Hint the controller once when screens it last saw asleep are awake."""

    def __init__(
        self,
        *,
        latest_observation: Callable[[], CanonicalObservation | None],
        display_power: Callable[[], DisplayPower],
        poll_seconds: float = DISPLAY_WAKE_POLL_SECONDS,
    ) -> None:
        """Bind state and DPMS readers; awake screens cost no command at all."""
        self._latest_observation = latest_observation
        self._display_power = display_power
        self._poll_seconds = poll_seconds
        self._hinted: ObservationGeneration | None = None

    async def run(self, notify: Callable[[], object]) -> None:
        """Poll until cancelled, sharing the DRM producer's hint contract."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            await self.poll_once(notify)

    async def poll_once(self, notify: Callable[[], object]) -> None:
        """Hint at most once per asleep observation, when DPMS reports on."""
        observation = self._latest_observation()
        if (
            observation is None
            or observation.display_power is not DisplayPower.ASLEEP
            or observation.observation_generation == self._hinted
        ):
            return
        try:
            power = await asyncio.to_thread(self._display_power)
        except Exception:  # noqa: BLE001 - a failed sample must not stop the service
            return
        if power is DisplayPower.ON:
            self._hinted = observation.observation_generation
            _JOURNAL.info("blanked screens woke; re-observing")
            notify()
