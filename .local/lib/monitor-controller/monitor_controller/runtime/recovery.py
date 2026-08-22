"""Provide injected, fail-closed recovery reconciliation interfaces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from monitor_controller.invariants import assert_controller_invariants
from monitor_controller.model import (
    TERMINAL_ACTION_LIFECYCLES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionRecord,
    ActionTombstone,
    ApplicationDispatched,
    ApplicationFinished,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    Effect,
    EventGeneration,
    EventMetadata,
    FinalizationDispatched,
    FinalizationFinished,
    ObservationGeneration,
    PlanHash,
    PlanningAction,
    PlanningState,
    PreparationDispatched,
    PreparationFinished,
    PreparationState,
    ProbeDispatched,
    ProbeFinished,
    State,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerUnit,
    bound_action_tombstones,
)
from monitor_controller.reducer import reduce

if TYPE_CHECKING:
    from monitor_controller.runtime.persistence import StateNamespace


MAX_EXIT_STATUS = 255


class WorkerNamespaceScanner(Protocol):
    """Discover recovery-relevant workers without granting action authority."""

    def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
        """Return one complete point-in-time namespace snapshot."""
        ...


@dataclass(frozen=True, slots=True)
class VerifiedWorkerResult:
    """Independently bound terminal transaction output from an inactive unit."""

    unit: WorkerUnit
    terminal_lifecycle: ActionLifecycle
    exit_status: int
    finished_monotonic_ms: int
    plan_hash: PlanHash | None = None

    def __post_init__(self) -> None:
        if self.terminal_lifecycle not in TERMINAL_ACTION_LIFECYCLES:
            msg = "verified worker result must have a terminal lifecycle"
            raise ValueError(msg)
        if not 0 <= self.exit_status <= MAX_EXIT_STATUS:
            msg = "verified worker result exit status must be between zero and 255"
            raise ValueError(msg)
        if self.finished_monotonic_ms < 0:
            msg = "verified worker result finish time must be non-negative"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is ActionLifecycle.COMPLETED
            and self.exit_status != 0
        ):
            msg = "completed verified worker result requires status zero"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is not ActionLifecycle.COMPLETED
            and self.exit_status == 0
        ):
            msg = "non-completed verified worker result requires non-zero status"
            raise ValueError(msg)

    @property
    def action_id(self) -> ActionId:
        """Return the exact action identity carried by the worker unit."""
        return self.unit.action_id


@dataclass(frozen=True, slots=True)
class WorkerNamespaceSnapshot:
    """Independently observed worker and durable terminal facts."""

    units: tuple[WorkerUnit, ...] = ()
    verified_tombstones: tuple[ActionTombstone, ...] = ()
    verified_results: tuple[VerifiedWorkerResult, ...] = ()
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
        result_ids = tuple(item.action_id for item in self.verified_results)
        if len(result_ids) != len(set(result_ids)):
            msg = "verified worker results must have unique action identities"
            raise ValueError(msg)
        tombstones = {item.action_id: item for item in self.verified_tombstones}
        for result in self.verified_results:
            tombstone = tombstones.get(result.action_id)
            if (
                tombstone is not None
                and tombstone.lifecycle is not result.terminal_lifecycle
            ):
                msg = "verified worker result conflicts with its tombstone"
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


def _action_for_id(state: State, action_id: ActionId) -> ActionRecord | None:
    return next(
        (action for action in _actions(state) if action.action_id == action_id),
        None,
    )


def _result_dispatch_event(
    result: VerifiedWorkerResult,
    metadata: EventMetadata,
) -> (
    ProbeDispatched
    | ApplicationDispatched
    | PreparationDispatched
    | FinalizationDispatched
):
    action_id = result.action_id
    if action_id.kind is ActionKind.PROBE:
        return ProbeDispatched(metadata, action_id, result.unit)
    if action_id.kind is ActionKind.APPLICATION:
        return ApplicationDispatched(metadata, action_id, result.unit)
    if action_id.kind is ActionKind.PREPARATION:
        return PreparationDispatched(metadata, action_id, result.unit)
    if action_id.kind is ActionKind.FINALIZATION:
        return FinalizationDispatched(metadata, action_id, result.unit)
    msg = "planning action cannot have a verified systemd result"
    raise ValueError(msg)


def _result_completion_event(
    result: VerifiedWorkerResult,
    metadata: EventMetadata,
) -> ProbeFinished | ApplicationFinished | PreparationFinished | FinalizationFinished:
    outcome = {
        ActionLifecycle.COMPLETED: WorkerOutcome.SUCCEEDED,
        ActionLifecycle.FAILED: WorkerOutcome.FAILED,
        ActionLifecycle.CANCELLED: WorkerOutcome.CANCELLED,
    }.get(result.terminal_lifecycle)
    if outcome is None:
        msg = "unknown and timed-out results require cancellation reconciliation"
        raise ValueError(msg)
    if result.action_id.kind is ActionKind.PROBE:
        return ProbeFinished(metadata, result.action_id, outcome, result.exit_status)
    if result.action_id.kind is ActionKind.APPLICATION:
        return ApplicationFinished(
            metadata,
            result.action_id,
            outcome,
            result.exit_status,
        )
    if result.action_id.kind is ActionKind.PREPARATION:
        if result.plan_hash is None:
            msg = "verified preparation result lacks its exact plan hash"
            raise ValueError(msg)
        return PreparationFinished(
            metadata,
            result.action_id,
            outcome,
            result.exit_status,
            result.plan_hash,
        )
    if result.action_id.kind is ActionKind.FINALIZATION:
        return FinalizationFinished(
            metadata,
            result.action_id,
            outcome,
            result.exit_status,
        )
    msg = "planning action cannot have a verified systemd result"
    raise ValueError(msg)


def _verified_result_is_represented(
    state: State,
    result: VerifiedWorkerResult,
) -> bool:
    action = _action_for_id(state, result.action_id)
    tombstone = next(
        (
            item
            for item in state.action_tombstones
            if item.action_id == result.action_id
        ),
        None,
    )
    if isinstance(action, PlanningAction):
        return False
    if (
        result.terminal_lifecycle is ActionLifecycle.COMPLETED
        and result.action_id.kind in {ActionKind.PREPARATION, ActionKind.FINALIZATION}
    ):
        if (
            action is None
            or action.unit != result.unit
            or action.exit_status != result.exit_status
            or getattr(action, "plan_hash", None) != result.plan_hash
        ):
            return False
        if action.lifecycle is ActionLifecycle.RESULT_PENDING:
            return tombstone is None
        return (
            action.lifecycle is ActionLifecycle.COMPLETED
            and tombstone
            == ActionTombstone(result.action_id, ActionLifecycle.COMPLETED)
        )
    if result.terminal_lifecycle in {
        ActionLifecycle.FAILED,
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    }:
        return (
            action is not None
            and action.unit == result.unit
            and action.lifecycle is result.terminal_lifecycle
            and action.exit_status == result.exit_status
            and getattr(action, "plan_hash", None) == result.plan_hash
            and tombstone
            == ActionTombstone(result.action_id, result.terminal_lifecycle)
        )
    return action is None and tombstone == ActionTombstone(
        result.action_id,
        result.terminal_lifecycle,
    )


def _verified_result_relationship_error(
    action: ActionRecord,
    result: VerifiedWorkerResult,
) -> str | None:
    if isinstance(action, PlanningAction):
        return "planning actions cannot have systemd terminal results"
    if action.lifecycle is not ActionLifecycle.ADMITTED and action.unit != result.unit:
        return "worker identity conflicts with verified terminal transaction output"
    if (
        action.action_id.kind in {ActionKind.PREPARATION, ActionKind.FINALIZATION}
        and getattr(action, "plan_hash", None) != result.plan_hash
    ):
        return "plan proof conflicts with verified terminal transaction output"
    if action.lifecycle not in {
        ActionLifecycle.ADMITTED,
        ActionLifecycle.DISPATCHED,
        ActionLifecycle.STOPPING,
        ActionLifecycle.RESULT_PENDING,
        *TERMINAL_ACTION_LIFECYCLES,
    }:
        return f"cannot reconcile a terminal result from {action.lifecycle.value}"
    return None


def _apply_verified_result(
    state: State,
    action: ActionRecord,
    result: VerifiedWorkerResult,
) -> State | None:
    if isinstance(action, PlanningAction):
        return None
    metadata = EventMetadata(result.finished_monotonic_ms, state.boot_id)
    if action.lifecycle is ActionLifecycle.ADMITTED:
        state = reduce(state, _result_dispatch_event(result, metadata)).state
        promoted_action = _action_for_id(state, result.action_id)
        if promoted_action is None:
            return None
        action = promoted_action
    if action.lifecycle is ActionLifecycle.STOPPING:
        event = WorkerCancellationAcknowledged(
            metadata,
            result.action_id,
            result.terminal_lifecycle,
            result.exit_status,
        )
    elif result.terminal_lifecycle in {
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    }:
        state = reduce(
            state,
            WorkerStatusUnknown(
                metadata,
                result.action_id,
                "recovery: exact inactive worker terminal result",
            ),
        ).state
        event = WorkerCancellationAcknowledged(
            metadata,
            result.action_id,
            result.terminal_lifecycle,
            result.exit_status,
        )
    else:
        event = _result_completion_event(result, metadata)
    return reduce(state, event).state


def _reconcile_verified_results(
    persisted: State,
    results: tuple[VerifiedWorkerResult, ...],
) -> tuple[State, frozenset[ActionId], bool, tuple[str, ...]]:
    """Apply exact inactive-worker results without emitting recovery effects."""
    state = persisted
    matched: set[ActionId] = set()
    changed = False
    reasons: list[str] = []
    for result in sorted(results, key=lambda item: item.action_id.value):
        action = _action_for_id(state, result.action_id)
        if action is None or isinstance(action, PlanningAction):
            continue
        matched.add(result.action_id)
        if problem := _verified_result_relationship_error(action, result):
            reasons.append(f"persisted {result.action_id.value}: {problem}")
            continue
        if action.lifecycle in TERMINAL_ACTION_LIFECYCLES | {
            ActionLifecycle.RESULT_PENDING
        }:
            if not _verified_result_is_represented(state, result):
                reasons.append(
                    f"persisted result state for {result.action_id.value} "
                    "conflicts with verified terminal transaction output"
                )
            continue
        reconciled = _apply_verified_result(state, action, result)
        if reconciled is None:
            reasons.append(
                f"persisted action {result.action_id.value} vanished during "
                "terminal reconciliation"
            )
            continue
        state = reconciled
        if not _verified_result_is_represented(state, result):
            reasons.append(
                f"verified terminal result for {result.action_id.value} "
                "does not match its persisted action proof"
            )
            continue
        changed = True
    return state, frozenset(matched), changed, tuple(reasons)


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
    sequences.extend(item.action_id.sequence for item in snapshot.verified_tombstones)
    sequences.extend(item.action_id.sequence for item in snapshot.verified_results)
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


def _persisted_terminal_evidence_reasons(state: State) -> tuple[str, ...]:
    reasons: list[str] = []
    tombstones = {
        tombstone.action_id: tombstone for tombstone in state.action_tombstones
    }
    for action in _actions(state):
        tombstone = tombstones.get(action.action_id)
        if action.lifecycle in TERMINAL_ACTION_LIFECYCLES and (
            tombstone is None or tombstone.lifecycle is not action.lifecycle
        ):
            reasons.append(
                "retained terminal action "
                f"{action.action_id.value} lacks matching terminal evidence"
            )
        elif tombstone is not None and (
            action.lifecycle not in TERMINAL_ACTION_LIFECYCLES
            or tombstone.lifecycle is not action.lifecycle
        ):
            reasons.append(
                f"retained action {action.action_id.value} conflicts with "
                "terminal evidence"
            )
    tombstone_ids = frozenset(tombstones)
    reasons.extend(
        f"possibly-live recovery worker {unit.action_id.value} "
        "also has terminal evidence"
        for unit in state.recovery_units
        if unit.action_id in tombstone_ids
    )
    return tuple(reasons)


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


def _represented_worker_units(persisted: State) -> frozenset[WorkerUnit]:
    return frozenset(
        action.unit
        for action in _actions(persisted)
        if not isinstance(action, PlanningAction) and action.unit is not None
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


def _same_boot_relationship_reasons(  # noqa: C901, PLR0912
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
    for key in scanned.keys() & retained.keys():
        if scanned[key] != retained[key]:
            reasons.append(f"recovery worker unit identity differs for action {key}")

    expected: dict[str, WorkerUnit] = {}
    actions_by_id: dict[str, ActionRecord] = {}
    for action in _actions(persisted):
        actions_by_id[action.action_id.value] = action
        if isinstance(action, PlanningAction) or action.unit is None:
            continue
        expected[action.action_id.value] = action.unit
    for key, unit in expected.items():
        scanned_unit = scanned.get(key)
        action = actions_by_id[key]
        if scanned_unit is None:
            if action.action_id not in verified_terminal_ids and action.lifecycle in {
                ActionLifecycle.DISPATCHED,
                ActionLifecycle.STOPPING,
                ActionLifecycle.RESULT_PENDING,
            }:
                reasons.append(f"persisted in-flight worker {key} is not discoverable")
        elif scanned_unit != unit:
            reasons.append(f"worker unit identity differs for action {key}")
        elif action.lifecycle not in {
            ActionLifecycle.DISPATCHED,
            ActionLifecycle.STOPPING,
        }:
            reasons.append(
                f"scanner-confirmed possibly-live worker {key} conflicts with "
                f"persisted {action.lifecycle.value} lifecycle"
            )
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


def recover_state(  # noqa: PLR0913, PLR0915
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
            ()
            if persisted_state is None
            else _unique_units(
                (
                    *persisted_state.recovery_units,
                    *_represented_worker_units(persisted_state),
                )
            )
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
    result_tombstones = tuple(
        ActionTombstone(item.action_id, item.terminal_lifecycle)
        for item in snapshot.verified_results
    )
    tombstones, tombstone_reasons = _merge_tombstones(
        (),
        (*snapshot.verified_tombstones, *result_tombstones),
    )
    scanned_ids = frozenset(unit.action_id for unit in snapshot.units)
    contradicted_ids = scanned_ids & frozenset(item.action_id for item in tombstones)
    contradiction_reasons = tuple(
        f"surviving worker {action_id.value} also has a verified terminal tombstone"
        for action_id in sorted(contradicted_ids, key=lambda item: item.value)
    )
    tombstones = tuple(
        item for item in tombstones if item.action_id not in contradicted_ids
    )
    tombstones = bound_action_tombstones(tombstones)
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

    (
        reconciled_state,
        reconciled_result_ids,
        result_reconciliation_changed,
        result_reconciliation_reasons,
    ) = _reconcile_verified_results(persisted_state, snapshot.verified_results)
    deferred_result_ids = frozenset(
        action_id
        for action_id in reconciled_result_ids
        if (
            (action := _action_for_id(reconciled_state, action_id)) is not None
            and action.lifecycle is ActionLifecycle.RESULT_PENDING
        )
    )
    same_boot_verified_tombstones = tuple(
        item
        for item in (*snapshot.verified_tombstones, *result_tombstones)
        if item.action_id not in deferred_result_ids
    )
    merged_tombstones, same_boot_tombstone_reasons = _merge_tombstones(
        reconciled_state.action_tombstones,
        same_boot_verified_tombstones,
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
    protected_action_ids = frozenset(
        action.action_id for action in _actions(reconciled_state)
    )
    tombstones = bound_action_tombstones(
        tombstones,
        protected_action_ids=protected_action_ids,
    )
    verified_terminal_ids = frozenset(
        item.action_id for item in (*snapshot.verified_tombstones, *result_tombstones)
    )
    recovery_units = _same_boot_recovery_units(
        reconciled_state,
        snapshot,
        verified_terminal_ids,
    )
    profile_reasons: tuple[str, ...] = ()
    if (
        snapshot.verified_finalized_profile is not None
        and snapshot.verified_finalized_profile
        != reconciled_state.desktop_finalized_profile
    ):
        profile_reasons = ("verified finalized profile disagrees with persisted state",)
    reasons = (
        *reasons,
        *same_boot_tombstone_reasons,
        *merged_contradiction_reasons,
        *profile_reasons,
        *result_reconciliation_reasons,
        *_persisted_terminal_evidence_reasons(reconciled_state),
        *_same_boot_relationship_reasons(
            reconciled_state,
            snapshot,
            verified_terminal_ids,
        ),
    )
    if reasons:
        fail_closed_units = _same_boot_recovery_units(
            reconciled_state,
            snapshot,
            verified_terminal_ids,
        )
        state = _minimum_recovery_state(
            current_boot_id=current_boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            snapshot=snapshot,
            tombstones=tombstones,
            persisted=reconciled_state,
            units=fail_closed_units,
        )
        assert_controller_invariants(state)
        return RecoveryResult(
            state=state,
            authority_allowed=False,
            requires_fresh_observation=True,
            reasons=reasons,
        )

    state = replace(
        reconciled_state,
        controller_instance=controller_instance,
        recovery_units=recovery_units,
        action_sequence_high_water=_action_high_water(
            reconciled_state, snapshot, tombstones, recovery_units
        ),
        transition_sequence_high_water=_transition_high_water(
            reconciled_state,
            snapshot,
        ),
        action_tombstones=tombstones,
    )
    assert_controller_invariants(state)
    return RecoveryResult(
        state=state,
        authority_allowed=True,
        requires_fresh_observation=result_reconciliation_changed,
    )
