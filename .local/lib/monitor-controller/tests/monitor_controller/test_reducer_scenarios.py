"""Exercise reducer parity, strict loading, and determinism."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from monitor_controller.codec import MAX_TOMBSTONES, encode_state
from monitor_controller.invariants import NUMBERED_INVARIANTS
from monitor_controller.model import (
    ACTION_TOMBSTONE_RETENTION_LIMIT,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    ApplicationFinished,
    ControllerPhase,
    DispatchRejected,
    EdidEvidence,
    EdidIntegrity,
    EventGeneration,
    EventMetadata,
    FinalizationFinished,
    ObservationCompleted,
    ObservationGeneration,
    ObservationKey,
    PreparationAction,
    PreparationFinished,
    ProbeFinished,
    RequestObservation,
    State,
    StopAction,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerTimedOut,
)
from monitor_controller.reducer import reduce
from monitor_controller.runtime.persistence import StateNamespace
from monitor_controller.runtime.recovery import (
    VerifiedWorkerResult,
    WorkerNamespaceSnapshot,
    recover_state,
)
from monitor_controller.simulation.scenario import (
    SCENARIO_SCHEMA_VERSION,
    Scenario,
    ScenarioFormatError,
    load_scenarios,
    normalize_effect,
    normalize_state,
    run_scenario,
)

_TEST_ROOT = Path(__file__).parent
_SCENARIO_PATH = _TEST_ROOT / "scenarios" / "reducer-scenarios.json"
_PARITY_PATH = _TEST_ROOT / "bash-scenario-parity.json"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
_BASH_TEST_PATH = (
    _REPOSITORY_ROOT / "specs" / "spikes" / "test-monitor-watcher-state-machine.sh"
)
_SCENARIOS: tuple[Scenario, ...] = load_scenarios(_SCENARIO_PATH)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.name)
def test_explicit_reducer_scenario(scenario: Scenario) -> None:
    """Assert every intermediate state, ordered effect, ID, timer, and count."""
    result = run_scenario(scenario)

    assert len(result.decisions) == len(scenario.steps)


def _scenario_state(
    name: str,
    phase: ControllerPhase,
    lifecycle: ActionLifecycle,
) -> State:
    scenario = next(item for item in _SCENARIOS if item.name == name)
    result = run_scenario(scenario)
    return next(
        decision.state
        for decision in result.decisions
        if decision.state.phase is phase
        and any(
            action is not None and action.lifecycle is lifecycle
            for action in (
                decision.state.probe,
                decision.state.application,
                decision.state.preparation,
                decision.state.finalization,
            )
        )
    )


def test_unchanged_quiescent_observation_does_not_schedule_another_poll() -> None:
    scenario = next(
        item
        for item in _SCENARIOS
        if item.name == "test_resume_to_same_profile_skips_finalization"
    )
    state = run_scenario(scenario).decisions[-1].state
    previous = state.latest_observation
    assert state.phase is ControllerPhase.QUIESCENT
    assert previous is not None

    now_ms = previous.observed_at_ms + 60_000
    observation = replace(
        previous,
        observed_at_ms=now_ms,
        observation_generation=ObservationGeneration(
            previous.observation_generation.value + 1
        ),
    )
    decision = reduce(
        state,
        ObservationCompleted(
            EventMetadata(now_ms, state.boot_id),
            observation,
        ),
    )

    assert decision.state.phase is ControllerPhase.QUIESCENT
    assert decision.state.next_timer_ms is None
    assert decision.effects == ()


def test_worker_completion_schedules_from_processing_not_sample_time() -> None:
    state = _scenario_state(
        "production_worker_timeout",
        ControllerPhase.PROBING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.probe
    assert action is not None

    decision = reduce(
        state,
        ProbeFinished(
            EventMetadata(50_000, state.boot_id),
            action.action_id,
            WorkerOutcome.SUCCEEDED,
            0,
        ),
    )

    assert state.latest_observation is not None
    assert state.latest_observation.observed_at_ms == 0
    assert decision.state.next_timer_ms == 50_000


def test_running_preparation_retains_exclusion_across_temporary_edid_absence() -> None:
    state = _scenario_state(
        "production_prepare_pending_preparing_prepared",
        ControllerPhase.VERIFYING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.preparation
    exact = state.latest_observation
    assert action is not None
    assert exact is not None
    absent = replace(
        exact,
        observed_at_ms=exact.observed_at_ms + 1,
        observation_generation=ObservationGeneration(
            exact.observation_generation.value + 1
        ),
        begin_event_generation=EventGeneration(exact.event_generation.value + 1),
        end_event_generation=EventGeneration(exact.event_generation.value + 1),
        base_identity_profiles=(),
        edid_integrity=(EdidEvidence("DP-1", EdidIntegrity.ABSENT),),
        eligible_profiles=(),
        exact_profile=None,
        observation_key=ObservationKey("temporary-edid-absence"),
    )

    retained = reduce(
        state,
        ObservationCompleted(
            EventMetadata(absent.observed_at_ms, state.boot_id),
            absent,
        ),
    )

    assert retained.state.preparation == action
    assert retained.state.preparation_state.value == "preparing"
    assert retained.state.phase is ControllerPhase.DISCOVER_FAST
    assert retained.state.verify_since_ms is None
    assert not any(isinstance(effect, StopAction) for effect in retained.effects)

    completed = reduce(
        retained.state,
        PreparationFinished(
            EventMetadata(absent.observed_at_ms + 1, state.boot_id),
            action.action_id,
            WorkerOutcome.SUCCEEDED,
            0,
            action.plan_hash,
        ),
    )
    pending = completed.state.preparation
    assert pending is not None
    assert pending.lifecycle is ActionLifecycle.RESULT_PENDING

    still_absent = replace(
        absent,
        observed_at_ms=absent.observed_at_ms + 2,
        observation_generation=ObservationGeneration(
            absent.observation_generation.value + 1
        ),
    )
    retained_result = reduce(
        completed.state,
        ObservationCompleted(
            EventMetadata(still_absent.observed_at_ms, state.boot_id),
            still_absent,
        ),
    )
    assert retained_result.state.preparation == pending

    restored = replace(
        exact,
        observed_at_ms=still_absent.observed_at_ms + 1,
        observation_generation=ObservationGeneration(
            still_absent.observation_generation.value + 1
        ),
        begin_event_generation=still_absent.end_event_generation,
        end_event_generation=still_absent.end_event_generation,
    )
    accepted = reduce(
        retained_result.state,
        ObservationCompleted(
            EventMetadata(restored.observed_at_ms, state.boot_id),
            restored,
        ),
    )
    prepared = accepted.state.preparation
    assert prepared is not None
    assert prepared.lifecycle is ActionLifecycle.COMPLETED
    assert accepted.state.preparation_state.value == "prepared"
    assert accepted.state.verify_since_ms == restored.observed_at_ms


def _preparing_state_with_connected_disabled_output() -> tuple[
    State, PreparationAction
]:
    state = _scenario_state(
        "production_prepare_pending_preparing_prepared",
        ControllerPhase.VERIFYING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.preparation
    planning = state.planning
    observation = state.latest_observation
    assert action is not None
    assert planning is not None
    assert observation is not None
    active_outputs = ("eDP-1",)
    profiles = tuple(
        replace(item, active_outputs=active_outputs)
        if item.profile == action.profile
        else item
        for item in observation.eligible_profiles
    )
    admitted = replace(
        observation,
        x_active_outputs=active_outputs,
        eligible_profiles=profiles,
    )
    return (
        replace(
            state,
            planning=replace(
                planning,
                input_key=replace(
                    planning.input_key,
                    active_outputs=active_outputs,
                ),
            ),
            latest_observation=admitted,
        ),
        action,
    )


def test_preparation_guard_accepts_connected_but_disabled_output() -> None:
    state, action = _preparing_state_with_connected_disabled_output()
    observation = state.latest_observation
    assert observation is not None
    fresh = replace(
        observation,
        observed_at_ms=observation.observed_at_ms + 1,
        observation_generation=ObservationGeneration(
            observation.observation_generation.value + 1
        ),
    )

    decision = reduce(
        state,
        ObservationCompleted(
            EventMetadata(fresh.observed_at_ms, state.boot_id),
            fresh,
        ),
    )

    assert decision.state.preparation == action
    assert decision.state.preparation_state.value == "preparing"
    assert not any(isinstance(effect, StopAction) for effect in decision.effects)


@pytest.mark.parametrize(
    "active_outputs",
    [(), ("DP-1", "eDP-1")],
    ids=("missing", "extra"),
)
def test_preparation_guard_stops_active_topology_contradictions(
    active_outputs: tuple[str, ...],
) -> None:
    state, action = _preparing_state_with_connected_disabled_output()
    observation = state.latest_observation
    assert observation is not None
    contradicted = replace(
        observation,
        observed_at_ms=observation.observed_at_ms + 1,
        observation_generation=ObservationGeneration(
            observation.observation_generation.value + 1
        ),
        x_active_outputs=active_outputs,
        eligible_profiles=(),
        exact_profile=None,
        observation_key=ObservationKey("contradictory-active-topology"),
    )

    decision = reduce(
        state,
        ObservationCompleted(
            EventMetadata(contradicted.observed_at_ms, state.boot_id),
            contradicted,
        ),
    )

    preparation = decision.state.preparation
    assert preparation is not None
    assert preparation.lifecycle is ActionLifecycle.STOPPING
    assert StopAction(action.action_id) in decision.effects


@pytest.mark.parametrize(
    "integrity",
    [
        EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE,
        EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
    ],
)
def test_running_preparation_retains_exclusion_for_broken_extensions(
    integrity: EdidIntegrity,
) -> None:
    state = _scenario_state(
        "production_prepare_pending_preparing_prepared",
        ControllerPhase.VERIFYING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.preparation
    exact = state.latest_observation
    assert action is not None
    assert exact is not None
    external = next(item for item in exact.edid_integrity if item.output == "DP-1")
    assert external.base_hash is not None
    broken = replace(
        exact,
        observed_at_ms=exact.observed_at_ms + 1,
        observation_generation=ObservationGeneration(
            exact.observation_generation.value + 1
        ),
        begin_event_generation=EventGeneration(exact.event_generation.value + 1),
        end_event_generation=EventGeneration(exact.event_generation.value + 1),
        edid_integrity=tuple(
            EdidEvidence(item.output, integrity, item.base_hash)
            if item.output == external.output
            else item
            for item in exact.edid_integrity
        ),
        eligible_profiles=(),
        exact_profile=None,
        observation_key=ObservationKey(f"broken-extensions-{integrity.value}"),
    )

    retained = reduce(
        state,
        ObservationCompleted(
            EventMetadata(broken.observed_at_ms, state.boot_id),
            broken,
        ),
    )

    assert retained.state.preparation == action
    assert retained.state.preparation_state.value == "preparing"
    assert retained.state.phase is ControllerPhase.DISCOVER_FAST
    assert retained.state.verify_since_ms is None
    assert not any(isinstance(effect, StopAction) for effect in retained.effects)


def test_retained_completed_actions_have_atomic_terminal_evidence() -> None:
    state = _scenario_state(
        "production_prepare_pending_preparing_prepared",
        ControllerPhase.VERIFYING,
        ActionLifecycle.COMPLETED,
    )
    planning = state.planning
    preparation = state.preparation
    assert planning is not None
    assert preparation is not None
    assert ActionTombstone(planning.action_id, ActionLifecycle.COMPLETED) in (
        state.action_tombstones
    )
    assert ActionTombstone(preparation.action_id, ActionLifecycle.COMPLETED) in (
        state.action_tombstones
    )
    assert encode_state(state)


def test_recovery_accepts_already_observation_confirmed_preparation_result() -> None:
    state = _scenario_state(
        "production_prepare_pending_preparing_prepared",
        ControllerPhase.VERIFYING,
        ActionLifecycle.COMPLETED,
    )
    preparation = state.preparation
    assert preparation is not None
    assert preparation.unit is not None
    assert preparation.exit_status == 0
    result = VerifiedWorkerResult(
        unit=preparation.unit,
        terminal_lifecycle=ActionLifecycle.COMPLETED,
        exit_status=preparation.exit_status,
        finished_monotonic_ms=0,
        plan_hash=preparation.plan_hash,
    )

    class Scanner:
        def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
            assert namespace is StateNamespace.ACTIVE
            return WorkerNamespaceSnapshot(verified_results=(result,))

    recovered = recover_state(
        state,
        current_boot_id=state.boot_id,
        controller_instance=state.controller_instance,
        display_identity=state.display_identity,
        namespace=StateNamespace.ACTIVE,
        scanner=Scanner(),
    )

    assert recovered.authority_allowed
    assert not recovered.requires_fresh_observation
    assert recovered.reasons == ()
    assert recovered.state.preparation == preparation


def test_terminal_worker_event_atomically_releases_recovery_exclusion() -> None:
    state = _scenario_state(
        "production_worker_status_unknown",
        ControllerPhase.APPLYING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.application
    assert action is not None
    assert action.unit is not None
    recovered = replace(state, recovery_units=(action.unit,))

    decision = reduce(
        recovered,
        ApplicationFinished(
            EventMetadata(1, state.boot_id),
            action.action_id,
            WorkerOutcome.FAILED,
            1,
        ),
    )

    terminal = decision.state.application
    assert terminal is not None
    assert terminal.lifecycle is ActionLifecycle.FAILED
    assert not decision.state.recovery_units
    assert ActionTombstone(action.action_id, ActionLifecycle.FAILED) in (
        decision.state.action_tombstones
    )
    assert encode_state(decision.state)


def test_worker_timeout_rejects_future_and_non_authoritative_deadlines() -> None:
    state = _scenario_state(
        "production_worker_timeout",
        ControllerPhase.PROBING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.probe
    assert action is not None

    with pytest.raises(ValueError, match="later than event processing"):
        WorkerTimedOut(
            EventMetadata(100, state.boot_id),
            action.action_id,
            101,
        )

    premature = WorkerTimedOut(
        EventMetadata(100, state.boot_id),
        action.action_id,
        100,
    )
    assert reduce(state, premature).state == state


@pytest.mark.parametrize(
    ("scenario_name", "phase", "kind"),
    [
        (
            "production_worker_timeout",
            ControllerPhase.PROBING,
            ActionKind.PROBE,
        ),
        (
            "production_worker_status_unknown",
            ControllerPhase.APPLYING,
            ActionKind.APPLICATION,
        ),
        (
            "production_prepare_pending_preparing_prepared",
            ControllerPhase.VERIFYING,
            ActionKind.PREPARATION,
        ),
    ],
)
@pytest.mark.parametrize("outcome", list(WorkerOutcome))
def test_late_worker_completion_keeps_stopping_exclusion_and_terminal_outcome(
    scenario_name: str,
    phase: ControllerPhase,
    kind: ActionKind,
    outcome: WorkerOutcome,
) -> None:
    state = _scenario_state(scenario_name, phase, ActionLifecycle.DISPATCHED)
    action = {
        ActionKind.PROBE: state.probe,
        ActionKind.APPLICATION: state.application,
        ActionKind.PREPARATION: state.preparation,
    }[kind]
    assert action is not None
    deadline_ms = action.worker_deadline_ms
    assert deadline_ms is not None
    stopping = reduce(
        state,
        WorkerTimedOut(
            EventMetadata(deadline_ms, state.boot_id),
            action.action_id,
            deadline_ms,
        ),
    )
    stopped_action = {
        ActionKind.PROBE: stopping.state.probe,
        ActionKind.APPLICATION: stopping.state.application,
        ActionKind.PREPARATION: stopping.state.preparation,
    }[kind]
    assert stopped_action is not None
    assert stopped_action.lifecycle is ActionLifecycle.STOPPING
    assert stopped_action.terminal_after_stop is ActionLifecycle.TIMED_OUT

    metadata = EventMetadata(deadline_ms + 1, state.boot_id)
    if kind is ActionKind.PROBE:
        finished = ProbeFinished(
            metadata,
            action.action_id,
            outcome,
            0,
        )
    elif kind is ActionKind.APPLICATION:
        finished = ApplicationFinished(
            metadata,
            action.action_id,
            outcome,
            0,
        )
    else:
        preparation = state.preparation
        assert preparation is not None
        finished = PreparationFinished(
            metadata,
            action.action_id,
            outcome,
            0,
            preparation.plan_hash,
        )
    late = reduce(stopping.state, finished)
    retained = {
        ActionKind.PROBE: late.state.probe,
        ActionKind.APPLICATION: late.state.application,
        ActionKind.PREPARATION: late.state.preparation,
    }[kind]

    assert retained == stopped_action
    assert late.state.action_tombstones == stopping.state.action_tombstones
    assert any(
        isinstance(effect, StopAction) and effect.action_id == action.action_id
        for effect in late.effects
    )

    acknowledged = reduce(
        late.state,
        WorkerCancellationAcknowledged(
            EventMetadata(deadline_ms + 2, state.boot_id),
            action.action_id,
            ActionLifecycle.TIMED_OUT,
            124,
        ),
    )
    terminal = {
        ActionKind.PROBE: acknowledged.state.probe,
        ActionKind.APPLICATION: acknowledged.state.application,
        ActionKind.PREPARATION: acknowledged.state.preparation,
    }[kind]
    assert terminal is not None
    assert terminal.lifecycle is ActionLifecycle.TIMED_OUT


def test_late_finalizer_success_cannot_supersede_timeout_or_commit_profile() -> None:
    state = _scenario_state(
        "test_topology_change_stops_running_finalizer",
        ControllerPhase.FINALIZING,
        ActionLifecycle.DISPATCHED,
    )
    action = state.finalization
    assert action is not None
    deadline_ms = action.worker_deadline_ms
    assert deadline_ms is not None

    timed_out = reduce(
        state,
        WorkerTimedOut(
            EventMetadata(deadline_ms, state.boot_id),
            action.action_id,
            deadline_ms,
        ),
    )
    late_success = reduce(
        timed_out.state,
        FinalizationFinished(
            EventMetadata(deadline_ms + 1, state.boot_id),
            action.action_id,
            WorkerOutcome.SUCCEEDED,
            0,
        ),
    )

    retained = late_success.state.finalization
    assert retained is not None
    assert retained.lifecycle is ActionLifecycle.STOPPING
    assert retained.terminal_after_stop is ActionLifecycle.TIMED_OUT
    assert late_success.state.phase is ControllerPhase.FINALIZE_STOPPING
    assert late_success.state.desktop_finalized_profile == "celtic"
    assert not any(
        isinstance(effect, RequestObservation) for effect in late_success.effects
    )

    acknowledged = reduce(
        late_success.state,
        WorkerCancellationAcknowledged(
            EventMetadata(deadline_ms + 2, state.boot_id),
            action.action_id,
            ActionLifecycle.TIMED_OUT,
            124,
        ),
    )
    terminal = acknowledged.state.finalization
    assert terminal is not None
    assert terminal.lifecycle is ActionLifecycle.TIMED_OUT
    assert acknowledged.state.phase is ControllerPhase.FINALIZE_FAILED

    observation = state.latest_observation
    assert observation is not None
    reobserved = replace(
        observation,
        observed_at_ms=deadline_ms + 3,
        observation_generation=ObservationGeneration(
            observation.observation_generation.value + 1
        ),
    )
    after_observation = reduce(
        acknowledged.state,
        ObservationCompleted(
            EventMetadata(deadline_ms + 3, state.boot_id),
            reobserved,
        ),
    )
    assert after_observation.state.phase is ControllerPhase.FINALIZE_FAILED
    assert after_observation.state.desktop_finalized_profile == "celtic"


def test_first_unplug_sample_clears_failed_probe_before_discovery() -> None:
    state = _scenario_state(
        "production_worker_timeout",
        ControllerPhase.PROBE_PENDING,
        ActionLifecycle.ADMITTED,
    )
    action = state.probe
    observation = state.latest_observation
    assert action is not None
    assert observation is not None
    failed = reduce(
        state,
        DispatchRejected(
            EventMetadata(1, state.boot_id),
            action.action_id,
            "null dispatcher",
        ),
    ).state
    internal = replace(
        observation,
        observed_at_ms=2,
        observation_generation=ObservationGeneration(
            observation.observation_generation.value + 1
        ),
        kernel_connected_outputs=("eDP-1",),
        kernel_external_outputs=(),
        x_connected_outputs=("eDP-1",),
        x_active_outputs=("eDP-1",),
        x_external_outputs=(),
        connector_identities=tuple(
            item for item in observation.connector_identities if item.output == "eDP-1"
        ),
        live_fingerprints=tuple(
            item for item in observation.live_fingerprints if item.output == "eDP-1"
        ),
        base_identity_profiles=(),
        edid_integrity=tuple(
            item for item in observation.edid_integrity if item.output == "eDP-1"
        ),
        probe_candidate=None,
        eligible_profiles=(),
        current_profiles=(),
        exact_profile=None,
        observation_key=ObservationKey("internal-after-failed-probe"),
    )

    decision = reduce(
        failed,
        ObservationCompleted(EventMetadata(2, state.boot_id), internal),
    )

    assert decision.state.phase is ControllerPhase.DISCOVER_FAST
    assert decision.state.probe is None
    assert decision.state.unplug_proof is not None
    assert decision.state.next_timer_ms == 1_002


def test_reducer_prunes_tombstones_and_preserves_failure_high_water() -> None:
    state = _scenario_state(
        "production_worker_timeout",
        ControllerPhase.PROBE_PENDING,
        ActionLifecycle.ADMITTED,
    )
    action = state.probe
    assert action is not None
    high_water = ACTION_TOMBSTONE_RETENTION_LIMIT + 1
    tombstones = tuple(
        ActionTombstone(
            ActionId(state.controller_instance, ActionKind.APPLICATION, sequence),
            ActionLifecycle.COMPLETED,
        )
        for sequence in range(2, high_water + 1)
    )
    crowded = replace(
        state,
        action_sequence_high_water=high_water,
        action_tombstones=tombstones,
    )

    failed = reduce(
        crowded,
        DispatchRejected(
            EventMetadata(1, state.boot_id),
            action.action_id,
            "injected rejection",
        ),
    )
    retained_ids = {item.action_id for item in failed.state.action_tombstones}

    assert (
        len(failed.state.action_tombstones)
        == ACTION_TOMBSTONE_RETENTION_LIMIT
        < MAX_TOMBSTONES
    )
    assert action.action_id in retained_ids
    assert (
        ActionId(state.controller_instance, ActionKind.APPLICATION, 2)
        not in retained_ids
    )
    assert failed.state.action_sequence_high_water == high_water
    assert encode_state(failed.state)


def test_every_scenario_replays_to_byte_equivalent_normalized_decisions() -> None:
    def replay_bytes(scenario: Scenario) -> bytes:
        result = run_scenario(scenario)
        normalized = [
            {
                "state": normalize_state(decision.state),
                "effects": [normalize_effect(effect) for effect in decision.effects],
            }
            for decision in result.decisions
        ]
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    for scenario in _SCENARIOS:
        assert replay_bytes(scenario) == replay_bytes(scenario)


def test_parity_manifest_classifies_every_bash_test_honestly() -> None:
    source = _BASH_TEST_PATH.read_text()
    bash_names = set(re.findall(r"^(test_[a-z0-9_]+)\(\) \{", source, re.MULTILINE))
    manifest = cast(
        "dict[str, object]",
        json.loads(_PARITY_PATH.read_text()),
    )
    executable = cast("dict[str, dict[str, str]]", manifest["executable_behavior"])
    divergence = cast(
        "dict[str, dict[str, str]]", manifest["intentional_safety_divergence"]
    )
    codec = cast("dict[str, dict[str, list[str]]]", manifest["codec_behavior"])
    scenario_names = {scenario.name for scenario in _SCENARIOS}
    codec_source = (_TEST_ROOT / "test_codec.py").read_text()
    codec_test_names = set(
        re.findall(r"^def (test_[a-z0-9_]+)\(", codec_source, re.MULTILINE)
    )

    assert manifest["schema_version"] == 3
    assert manifest["bash_test_count"] == len(bash_names) == 50
    assert set(executable) | set(divergence) | set(codec) == bash_names
    assert not (set(executable) & set(divergence))
    assert not (set(executable) & set(codec))
    assert not (set(divergence) & set(codec))
    assert {entry["scenario"] for entry in executable.values()} <= scenario_names
    assert {entry["scenario"] for entry in divergence.values()} <= scenario_names
    assert {
        test_name for entry in codec.values() for test_name in entry["python_tests"]
    } <= codec_test_names


def test_executable_bash_oracle_passes_all_classified_behavior_cases() -> None:
    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(_BASH_TEST_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.splitlines()

    assert completed.returncode == 0, completed.stderr
    assert len([line for line in output if line.startswith("ok - ")]) == 50
    assert not [line for line in output if line.startswith("not ok - ")]


def test_parity_manifest_names_exact_production_only_lifecycle_coverage() -> None:
    manifest = cast(
        "dict[str, object]",
        json.loads(_PARITY_PATH.read_text()),
    )
    production = cast("dict[str, str]", manifest["production_only"])
    required = {
        "PLAN_PENDING",
        "PLANNING",
        "PLAN_READY",
        "PLAN_FAILED",
        "PREPARE_PENDING",
        "PREPARING",
        "PREPARED",
        "PREPARE_STOPPING",
        "PREPARE_FAILED",
        "FINALIZE_STOPPING",
        "startup_baseline_exclusion",
        "dirty_probe_admission",
        "dirty_application_admission",
        "dirty_preparation_admission",
        "dirty_finalization_admission",
        "dispatch_rejection",
        "unknown_worker_status",
        "worker_timeout",
        "probe_failure",
        "application_failure",
        "planning_failure",
        "preparation_failure",
        "finalization_failure",
        "invalid_event_fail_closed",
        "deterministic_replay",
    }
    scenario_names = {scenario.name for scenario in _SCENARIOS}

    assert set(production) == required
    assert set(production.values()) <= scenario_names


def test_all_thirteen_numbered_invariants_are_explicitly_named() -> None:
    assert len(NUMBERED_INVARIANTS) == 13
    assert len(set(NUMBERED_INVARIANTS)) == 13


def test_v1_cancellation_events_migrate_and_current_scenarios_round_trip(
    tmp_path: Path,
) -> None:
    current = json.loads(_SCENARIO_PATH.read_text())
    assert current["schema_version"] == SCENARIO_SCHEMA_VERSION == 2
    legacy = json.loads(_SCENARIO_PATH.read_text())
    legacy["schema_version"] = 1
    for scenario in legacy["scenarios"]:
        for step in scenario["steps"]:
            event = step["event"]
            if event["type"] == "cancellation_acknowledged":
                event.pop("terminal_lifecycle")
                event.pop("exit_status")
    path = tmp_path / "legacy-v1.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = load_scenarios(path)

    assert len(migrated) == len(_SCENARIOS)
    for scenario in migrated:
        run_scenario(scenario)


def test_strict_loader_rejects_duplicate_fields(tmp_path: Path) -> None:
    text = _SCENARIO_PATH.read_text().replace(
        '"schema_version": 2',
        '"schema_version": 2, "schema_version": 2',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(text)

    with pytest.raises(ScenarioFormatError, match="duplicate JSON field"):
        load_scenarios(path)


def test_strict_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    document = json.loads(_SCENARIO_PATH.read_text())
    document["unexpected"] = True
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)


def test_strict_loader_rejects_truncated_json(tmp_path: Path) -> None:
    path = tmp_path / "truncated.json"
    path.write_text(_SCENARIO_PATH.read_text()[:-20])

    with pytest.raises(ScenarioFormatError, match="cannot decode"):
        load_scenarios(path)


def test_strict_loader_rejects_unknown_event_and_effect_fields(
    tmp_path: Path,
) -> None:
    document = cast("dict[str, object]", json.loads(_SCENARIO_PATH.read_text()))
    scenarios = cast("list[dict[str, object]]", document["scenarios"])
    steps = cast("list[dict[str, object]]", scenarios[0]["steps"])
    event = cast("dict[str, object]", steps[0]["event"])
    event["shell"] = "xrandr --auto"
    path = tmp_path / "event-unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)

    document = cast("dict[str, object]", json.loads(_SCENARIO_PATH.read_text()))
    scenarios = cast("list[dict[str, object]]", document["scenarios"])
    steps = cast("list[dict[str, object]]", scenarios[0]["steps"])
    expected = cast("dict[str, object]", steps[0]["expect"])
    effects = cast("list[dict[str, object]]", expected["effects"])
    effects[0]["command"] = "setup-monitor"
    path = tmp_path / "effect-unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)


def test_scenario_corpus_is_comprehensive_and_explicit() -> None:
    assert len(_SCENARIOS) == 57
    assert sum(len(scenario.steps) for scenario in _SCENARIOS) == 325
    steps = (step for scenario in _SCENARIOS for step in scenario.steps)
    assert all(step.expected_effect_counts for step in steps)
    steps = (step for scenario in _SCENARIOS for step in scenario.steps)
    assert all(step.expected_state for step in steps)
