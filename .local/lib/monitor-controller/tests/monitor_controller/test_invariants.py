"""Focused fail-closed tests for central cross-cutting invariants."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import cache
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.codec import decode_state, encode_state
from monitor_controller.invariants import (
    ControllerInvariantError,
    assert_controller_invariants,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ApplicationAction,
    ApplicationAttemptKey,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EventGeneration,
    MappingProof,
    ObservationInvalidityReason,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    ProbeAction,
    ProbeAttemptKey,
    ProfileScope,
    State,
    WorkerUnit,
)
from monitor_controller.simulation.scenario import load_scenarios, run_scenario

_SCENARIOS: Path = Path(__file__).parent / "scenarios" / "reducer-scenarios.json"
_INSTANCE: ControllerInstanceId = ControllerInstanceId(UUID(int=901))


@cache
def _states() -> tuple[State, ...]:
    return tuple(
        decision.state
        for scenario in load_scenarios(_SCENARIOS)
        for decision in run_scenario(scenario).decisions
    )


def _first(predicate: Callable[[State], bool]) -> State:
    return next(state for state in _states() if predicate(state))


def test_every_explicit_transition_state_satisfies_invariants_and_persistence() -> None:
    for state in _states():
        assert_controller_invariants(state)
        assert decode_state(encode_state(state)) == state


def test_only_one_acknowledged_display_mutator_can_exist() -> None:
    probe_id = ActionId(_INSTANCE, ActionKind.PROBE, 1)
    application_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 2)
    probe_key = ProbeAttemptKey(1, "dock", ObservationKey("probe"))
    application_key = ApplicationAttemptKey(1, "dock", ObservationKey("apply"))
    mapping = MappingProof(
        "dock",
        1,
        ObservationKey("apply"),
        (OutputMapping("DP-SAVED", "DP-1"),),
    )
    probe_unit = WorkerUnit(probe_id, "monitor-probe@1.service")
    application_unit = WorkerUnit(application_id, "monitor-apply@2.service")
    state = State(
        boot_id=BootId(UUID(int=900)),
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":invariant"),
        phase=ControllerPhase.RECOVERING,
        physical_epoch=1,
        physical_token=PhysicalToken("physical"),
        attempted_probe_keys=frozenset({probe_key}),
        probe=ProbeAction(
            probe_id,
            probe_key,
            EventGeneration(0),
            "DP-1",
            "eDP-1",
            "3840x2160",
            ActionLifecycle.DISPATCHED,
            probe_unit,
        ),
        attempted_application_keys=frozenset({application_key}),
        application=ApplicationAction(
            application_id,
            application_key,
            EventGeneration(0),
            "dock",
            ProfileScope.MIXED,
            mapping,
            ActionLifecycle.DISPATCHED,
            application_unit,
        ),
        action_sequence_high_water=2,
    )

    with pytest.raises(
        ControllerInvariantError,
        match="more than one display mutation",
    ):
        assert_controller_invariants(state)


def test_external_evidence_rejects_retained_internal_candidate() -> None:
    internal = _first(
        lambda state: (
            state.candidate is not None
            and state.candidate.scope is ProfileScope.INTERNAL_ONLY
        )
    )
    external = _first(
        lambda state: (
            state.latest_observation is not None
            and state.latest_observation.has_external_hardware
            and state.physical_epoch == internal.physical_epoch
        )
    )
    invalid = replace(
        internal,
        latest_observation=external.latest_observation,
        observation_generation=external.observation_generation,
        event_generation=external.event_generation,
        external_intent=True,
    )

    with pytest.raises(ControllerInvariantError, match="internal-only candidate"):
        assert_controller_invariants(invalid)


def test_verification_requires_current_valid_exact_evidence() -> None:
    verifying = _first(
        lambda state: (
            state.phase is ControllerPhase.VERIFYING
            and state.verify_since_ms is not None
        )
    )
    invalid_observation = verifying.latest_observation
    assert invalid_observation is not None
    invalid = replace(
        verifying,
        latest_observation=replace(
            invalid_observation,
            exact_profile=None,
            probe_candidate=None,
            validity=ObservationValidity.INVALID,
            invalidity_reason=ObservationInvalidityReason.INCONSISTENT_EVIDENCE,
        ),
    )

    with pytest.raises(ControllerInvariantError, match="continuous exact/current"):
        assert_controller_invariants(invalid)


def test_finalization_requires_full_ten_second_proof() -> None:
    pending = _first(
        lambda state: (
            state.phase is ControllerPhase.FINALIZE_PENDING
            and state.latest_observation is not None
        )
    )
    observation = pending.latest_observation
    assert observation is not None
    invalid = replace(pending, verify_since_ms=observation.observed_at_ms - 9_999)

    with pytest.raises(ControllerInvariantError, match="ten seconds"):
        assert_controller_invariants(invalid)


def test_preparation_must_match_ready_plan_hash() -> None:
    pending = _first(lambda state: state.preparation is not None)
    assert pending.planning is not None
    invalid = replace(
        pending,
        planning=replace(
            pending.planning,
            plan_hash=PlanHash("other"),
        ),
    )

    with pytest.raises(ControllerInvariantError, match="currently completed plan hash"):
        assert_controller_invariants(invalid)


def test_current_instance_action_id_cannot_exceed_high_water_mark() -> None:
    pending = _first(lambda state: state.application is not None)
    invalid = replace(pending, action_sequence_high_water=0)

    with pytest.raises(ControllerInvariantError, match="high-water mark"):
        assert_controller_invariants(invalid)


def test_acknowledged_worker_requires_unit_and_attempt_history() -> None:
    applying = _first(
        lambda state: (
            state.application is not None
            and state.application.lifecycle is ActionLifecycle.DISPATCHED
        )
    )
    assert applying.application is not None
    missing_unit = replace(
        applying,
        application=replace(applying.application, unit=None),
    )
    missing_attempt = replace(applying, attempted_application_keys=frozenset())

    with pytest.raises(ControllerInvariantError, match="retain its unit"):
        assert_controller_invariants(missing_unit)
    with pytest.raises(ControllerInvariantError, match="attempted key"):
        assert_controller_invariants(missing_attempt)


def test_baseline_adoption_forbids_planning() -> None:
    planning = _first(lambda state: state.planning is not None)
    invalid = replace(planning, baseline_adoption=True)

    with pytest.raises(ControllerInvariantError, match="forbids planning"):
        assert_controller_invariants(invalid)


def test_unplug_proof_requires_retained_external_intent() -> None:
    proving = _first(lambda state: state.unplug_proof is not None)
    invalid = replace(proving, external_intent=False)

    with pytest.raises(ControllerInvariantError, match="retained external intent"):
        assert_controller_invariants(invalid)
