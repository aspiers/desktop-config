"""Provide injected, fail-closed recovery reconciliation interfaces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from monitor_controller.invariants import assert_controller_invariants
from monitor_controller.model import (
    ActionId,
    ActionLifecycle,
    ActionRecord,
    ActionTombstone,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    Effect,
    EventGeneration,
    ObservationGeneration,
    PlanningAction,
    PlanningState,
    PreparationState,
    State,
    WorkerUnit,
)

if TYPE_CHECKING:
    from monitor_controller.runtime.persistence import StateNamespace


class WorkerNamespaceScanner(Protocol):
    """Discover recovery-relevant workers without granting action authority."""

    def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
        """Return one complete point-in-time namespace snapshot."""
        ...


@dataclass(frozen=True, slots=True)
class WorkerNamespaceSnapshot:
    """Independently observed worker and durable terminal facts."""

    units: tuple[WorkerUnit, ...] = ()
    verified_tombstones: tuple[ActionTombstone, ...] = ()
    action_sequence_high_water: int = 0
    transition_sequence_high_water: int = 0
    verified_finalized_profile: str | None = None
    ambiguities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action_sequence_high_water < 0:
            msg = "recovered action high-water mark must be non-negative"
            raise ValueError(msg)
        if self.transition_sequence_high_water < 0:
            msg = "recovered transition high-water mark must be non-negative"
            raise ValueError(msg)
        if self.verified_finalized_profile is not None and not (
            self.verified_finalized_profile
            and not self.verified_finalized_profile.isspace()
        ):
            msg = "verified finalized profile must not be empty"
            raise ValueError(msg)
        if any(not item or item.isspace() for item in self.ambiguities):
            msg = "recovery ambiguity descriptions must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """A recovery state plus an explicit authority decision."""

    state: State
    authority_allowed: bool
    requires_fresh_observation: bool
    reasons: tuple[str, ...] = ()
    effects: tuple[Effect, ...] = ()

    def __post_init__(self) -> None:
        if self.effects:
            msg = "recovery reconciliation cannot directly authorize effects"
            raise ValueError(msg)
        if (
            not self.authority_allowed
            and self.state.phase is not ControllerPhase.RECOVERING
        ):
            msg = "denied recovery authority requires RECOVERING state"
            raise ValueError(msg)


def _actions(state: State) -> tuple[ActionRecord, ...]:
    return tuple(
        action
        for action in (
            state.probe,
            state.application,
            state.planning,
            state.preparation,
            state.finalization,
        )
        if action is not None
    )


def _unit_map(units: tuple[WorkerUnit, ...]) -> tuple[dict[str, WorkerUnit], list[str]]:
    result: dict[str, WorkerUnit] = {}
    reasons: list[str] = []
    for unit in units:
        key = unit.action_id.value
        previous = result.get(key)
        if previous is not None and previous != unit:
            reasons.append(f"conflicting units for action {key}")
        elif previous is not None:
            reasons.append(f"duplicate unit for action {key}")
        else:
            result[key] = unit
    return result, reasons


def _unique_units(units: tuple[WorkerUnit, ...]) -> tuple[WorkerUnit, ...]:
    unit_map, _ = _unit_map(units)
    return tuple(unit_map[key] for key in sorted(unit_map))


def _action_high_water(
    persisted: State | None,
    snapshot: WorkerNamespaceSnapshot,
    tombstones: tuple[ActionTombstone, ...],
    units: tuple[WorkerUnit, ...],
) -> int:
    sequences = [snapshot.action_sequence_high_water]
    sequences.extend(unit.action_id.sequence for unit in units)
    sequences.extend(item.action_id.sequence for item in tombstones)
    if persisted is not None:
        sequences.append(persisted.action_sequence_high_water)
        sequences.extend(action.action_id.sequence for action in _actions(persisted))
        sequences.extend(
            item.action_id.sequence for item in persisted.action_tombstones
        )
        sequences.extend(unit.action_id.sequence for unit in persisted.recovery_units)
    return max(sequences, default=0)


def _transition_high_water(
    persisted: State | None, snapshot: WorkerNamespaceSnapshot
) -> int:
    sequences = [snapshot.transition_sequence_high_water]
    if persisted is not None:
        sequences.append(persisted.transition_sequence_high_water)
        sequences.extend(
            action.transition_id.sequence
            for action in (
                persisted.planning,
                persisted.preparation,
                persisted.finalization,
            )
            if action is not None
        )
    return max(sequences, default=0)


def _merge_tombstones(
    persisted: tuple[ActionTombstone, ...],
    verified: tuple[ActionTombstone, ...],
) -> tuple[tuple[ActionTombstone, ...], tuple[str, ...]]:
    merged: dict[str, ActionTombstone] = {}
    reasons: list[str] = []
    for tombstone in (*persisted, *verified):
        key = tombstone.action_id.value
        previous = merged.get(key)
        if previous is not None and previous.lifecycle is not tombstone.lifecycle:
            reasons.append(f"conflicting terminal lifecycle for action {key}")
        merged[key] = tombstone
    return tuple(sorted(merged.values(), key=lambda item: item.action_id.value)), tuple(
        reasons
    )


def _minimum_recovery_state(  # noqa: PLR0913
    *,
    current_boot_id: BootId,
    controller_instance: ControllerInstanceId,
    display_identity: DisplayIdentity,
    snapshot: WorkerNamespaceSnapshot,
    tombstones: tuple[ActionTombstone, ...],
    persisted: State | None = None,
    units: tuple[WorkerUnit, ...] | None = None,
) -> State:
    recovery_units = _unique_units(snapshot.units if units is None else units)
    return State(
        boot_id=current_boot_id,
        controller_instance=controller_instance,
        display_identity=display_identity,
        phase=ControllerPhase.RECOVERING,
        desktop_finalized_profile=snapshot.verified_finalized_profile,
        action_sequence_high_water=_action_high_water(
            persisted, snapshot, tombstones, recovery_units
        ),
        transition_sequence_high_water=_transition_high_water(persisted, snapshot),
        action_tombstones=tombstones,
        recovery_units=recovery_units,
    )


def _boot_mismatch_state(
    persisted: State,
    *,
    current_boot_id: BootId,
    controller_instance: ControllerInstanceId,
    snapshot: WorkerNamespaceSnapshot,
    tombstones: tuple[ActionTombstone, ...],
) -> State:
    finalized = snapshot.verified_finalized_profile
    return replace(
        persisted,
        boot_id=current_boot_id,
        controller_instance=controller_instance,
        phase=ControllerPhase.RECOVERING,
        planning_state=PlanningState.PLAN_IDLE,
        preparation_state=PreparationState.PREPARE_IDLE,
        latest_observation=None,
        physical_token=None,
        candidate=None,
        aggressive_deadline_ms=None,
        next_timer_ms=None,
        backoff_index=0,
        verify_since_ms=None,
        last_drm_at_ms=None,
        stable_x_profile=None,
        desktop_finalized_profile=finalized,
        external_intent=False,
        baseline_adoption=finalized is None,
        attempted_probe_keys=frozenset(),
        probe=None,
        attempted_application_keys=frozenset(),
        application=None,
        planning=None,
        preparation=None,
        finalization=None,
        unknown_key=None,
        unknown_since_ms=None,
        unplug_proof=None,
        observation_generation=ObservationGeneration(0),
        event_generation=EventGeneration(0),
        action_sequence_high_water=_action_high_water(
            persisted, snapshot, tombstones, snapshot.units
        ),
        transition_sequence_high_water=_transition_high_water(persisted, snapshot),
        action_tombstones=tombstones,
        recovery_units=_unique_units(snapshot.units),
    )


def _same_boot_recovery_units(
    persisted: State,
    snapshot: WorkerNamespaceSnapshot,
    verified_terminal_ids: frozenset[ActionId],
) -> tuple[WorkerUnit, ...]:
    retained = tuple(
        unit
        for unit in persisted.recovery_units
        if unit.action_id not in verified_terminal_ids
    )
    return _unique_units((*retained, *snapshot.units))


def _same_boot_relationship_reasons(
    persisted: State,
    snapshot: WorkerNamespaceSnapshot,
    verified_terminal_ids: frozenset[ActionId],
) -> tuple[str, ...]:
    scanned, reasons = _unit_map(snapshot.units)
    retained_units = tuple(
        unit
        for unit in persisted.recovery_units
        if unit.action_id not in verified_terminal_ids
    )
    retained, retained_reasons = _unit_map(retained_units)
    reasons.extend(retained_reasons)
    _, combined_reasons = _unit_map((*retained_units, *snapshot.units))
    reasons.extend(combined_reasons)

    expected: dict[str, WorkerUnit] = {}
    actions_by_id: dict[str, ActionRecord] = {}
    for action in _actions(persisted):
        actions_by_id[action.action_id.value] = action
        if isinstance(action, PlanningAction) or action.unit is None:
            continue
        expected[action.action_id.value] = action.unit
    for key, unit in expected.items():
        scanned_unit = scanned.get(key)
        if scanned_unit is None:
            action = actions_by_id[key]
            if action.lifecycle in {
                ActionLifecycle.DISPATCHED,
                ActionLifecycle.STOPPING,
                ActionLifecycle.RESULT_PENDING,
            }:
                reasons.append(f"persisted in-flight worker {key} is not discoverable")
        elif scanned_unit != unit:
            reasons.append(f"worker unit identity differs for action {key}")
    for key in scanned.keys() - expected.keys() - retained.keys():
        reasons.append(f"surviving worker {key} is absent from persisted state")
    for key in retained.keys() - expected.keys():
        if key in scanned:
            reasons.append(f"persisted recovery worker {key} remains live")
        else:
            reasons.append(
                f"persisted recovery worker {key} lacks a verified terminal tombstone"
            )
    return tuple(reasons)


def recover_state(  # noqa: PLR0913
    persisted_state: State | None,
    *,
    current_boot_id: BootId,
    controller_instance: ControllerInstanceId,
    display_identity: DisplayIdentity,
    namespace: StateNamespace,
    scanner: WorkerNamespaceScanner,
    corruption: Exception | None = None,
) -> RecoveryResult:
    """Reconcile persisted and supervisor truth without directly emitting effects.

    ``persisted_state`` is ``None`` only for a missing or corrupt record. Scanner
    failure, contradictory identities, and unknown surviving workers all produce a
    minimal ``RECOVERING`` state with authority denied.
    """
    try:
        snapshot = scanner.scan(namespace)
    except Exception as error:  # noqa: BLE001 - injected trust boundary
        snapshot = WorkerNamespaceSnapshot()
        reason = f"worker namespace scan failed: {type(error).__name__}: {error}"
        retained_units = (
            () if persisted_state is None else persisted_state.recovery_units
        )
        state = _minimum_recovery_state(
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            snapshot=snapshot,
            tombstones=(),
            persisted=persisted_state,
            units=retained_units,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=(reason,),
        )

    _, unit_reasons = _unit_map(snapshot.units)
    tombstones, tombstone_reasons = _merge_tombstones((), snapshot.verified_tombstones)
    scanned_ids = frozenset(unit.action_id for unit in snapshot.units)
    contradicted_ids = scanned_ids & frozenset(
        item.action_id for item in snapshot.verified_tombstones
    )
    contradiction_reasons = tuple(
        f"surviving worker {action_id.value} also has a verified terminal tombstone"
        for action_id in sorted(contradicted_ids, key=lambda item: item.value)
    )
    tombstones = tuple(
        item for item in tombstones if item.action_id not in contradicted_ids
    )
    reasons = (
        *snapshot.ambiguities,
        *unit_reasons,
        *tombstone_reasons,
        *contradiction_reasons,
    )

    if persisted_state is None:
        if corruption is not None:
            reasons = (*reasons, f"authoritative state is corrupt: {corruption}")
        else:
            reasons = (*reasons, "authoritative state is missing")
        state = _minimum_recovery_state(
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            snapshot=snapshot,
            tombstones=tombstones,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=reasons,
        )

    if persisted_state.display_identity != display_identity:
        reasons = (
            *reasons,
            "persisted display identity does not match requested display identity",
        )
        state = _minimum_recovery_state(
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            snapshot=snapshot,
            tombstones=tombstones,
            persisted=persisted_state,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=reasons,
        )

    if persisted_state.boot_id != current_boot_id:
        state = _boot_mismatch_state(
            persisted_state,
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            snapshot=snapshot,
            tombstones=tombstones,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=reasons,
        )

    merged_tombstones, same_boot_tombstone_reasons = _merge_tombstones(
        persisted_state.action_tombstones, snapshot.verified_tombstones
    )
    merged_contradicted_ids = scanned_ids & frozenset(
        item.action_id for item in merged_tombstones
    )
    merged_contradiction_reasons = tuple(
        f"surviving worker {action_id.value} also has a terminal tombstone"
        for action_id in sorted(merged_contradicted_ids, key=lambda item: item.value)
    )
    tombstones = tuple(
        item
        for item in merged_tombstones
        if item.action_id not in merged_contradicted_ids
    )
    verified_terminal_ids = frozenset(item.action_id for item in merged_tombstones)
    recovery_units = _same_boot_recovery_units(
        persisted_state, snapshot, verified_terminal_ids
    )
    profile_reasons: tuple[str, ...] = ()
    if (
        snapshot.verified_finalized_profile is not None
        and snapshot.verified_finalized_profile
        != persisted_state.desktop_finalized_profile
    ):
        profile_reasons = ("verified finalized profile disagrees with persisted state",)
    reasons = (
        *reasons,
        *same_boot_tombstone_reasons,
        *merged_contradiction_reasons,
        *profile_reasons,
        *_same_boot_relationship_reasons(
            persisted_state, snapshot, verified_terminal_ids
        ),
    )
    if reasons:
        state = _minimum_recovery_state(
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            snapshot=snapshot,
            tombstones=tombstones,
            persisted=persisted_state,
            units=recovery_units,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=reasons,
        )

    state = replace(
        persisted_state,
        controller_instance=controller_instance,
        recovery_units=recovery_units,
        action_sequence_high_water=_action_high_water(
            persisted_state, snapshot, tombstones, recovery_units
        ),
        transition_sequence_high_water=_transition_high_water(
            persisted_state, snapshot
        ),
        action_tombstones=tombstones,
    )
    assert_controller_invariants(state)
    return RecoveryResult(
        state=state,
        authority_allowed=True,
        requires_fresh_observation=False,
    )
