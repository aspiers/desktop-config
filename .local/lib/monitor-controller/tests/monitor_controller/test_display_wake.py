"""Contract tests for waking the controller when blanked screens come on."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from monitor_controller.model import (
    CanonicalObservation,
    DisplayPower,
    ObservationGeneration,
)
from monitor_controller.runtime.display_wake import DisplayWakeMonitor
from monitor_controller.simulation.scenario import load_scenarios, run_scenario

_SCENARIO_PATH = Path(__file__).parent / "scenarios" / "reducer-scenarios.json"


def _observation(power: DisplayPower, generation: int = 1) -> CanonicalObservation:
    scenario = load_scenarios(_SCENARIO_PATH)[0]
    observation = next(
        decision.state.latest_observation
        for decision in run_scenario(scenario).decisions
        if decision.state.latest_observation is not None
    )
    return replace(
        observation,
        display_power=power,
        observation_generation=ObservationGeneration(generation),
    )


class _Harness:
    def __init__(self, observation: CanonicalObservation, power: DisplayPower) -> None:
        self.observation: CanonicalObservation | None = observation
        self.power = power
        self.samples = 0
        self.hints = 0
        self.monitor = DisplayWakeMonitor(
            latest_observation=lambda: self.observation,
            display_power=self._sample,
        )

    def _sample(self) -> DisplayPower:
        self.samples += 1
        return self.power

    def _notify(self) -> None:
        self.hints += 1

    def poll(self) -> None:
        asyncio.run(self.monitor.poll_once(self._notify))


def test_awake_screens_cost_no_dpms_command() -> None:
    harness = _Harness(_observation(DisplayPower.ON), DisplayPower.ON)

    harness.poll()

    assert harness.samples == 0
    assert harness.hints == 0


def test_screens_still_asleep_are_sampled_but_not_hinted() -> None:
    harness = _Harness(_observation(DisplayPower.ASLEEP), DisplayPower.ASLEEP)

    harness.poll()

    assert harness.samples == 1
    assert harness.hints == 0


def test_waking_screens_hint_exactly_once_per_asleep_observation() -> None:
    harness = _Harness(_observation(DisplayPower.ASLEEP), DisplayPower.ON)

    harness.poll()
    harness.poll()

    assert harness.hints == 1
    harness.observation = _observation(DisplayPower.ASLEEP, generation=2)
    harness.poll()
    assert harness.hints == 2


def test_a_failed_sample_never_stops_the_service() -> None:
    def broken() -> DisplayPower:
        message = "injected xset failure"
        raise OSError(message)

    monitor = DisplayWakeMonitor(
        latest_observation=lambda: _observation(DisplayPower.ASLEEP),
        display_power=broken,
    )

    asyncio.run(monitor.poll_once(lambda: pytest.fail("must not hint")))
