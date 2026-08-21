"""Verify fail-closed worker-namespace recovery."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EventGeneration,
    ObservationKey,
    ProbeAction,
    ProbeAttemptKey,
    State,
    WorkerUnit,
)
from monitor_controller.runtime.persistence import StateNamespace
from monitor_controller.runtime.recovery import (
    WorkerNamespaceSnapshot,
    recover_state,
)

_OLD_BOOT = BootId(UUID("11111111-1111-1111-1111-111111111111"))
_NEW_BOOT = BootId(UUID("22222222-2222-2222-2222-222222222222"))
_OLD_INSTANCE = ControllerInstanceId(UUID("33333333-3333-3333-3333-333333333333"))
_NEW_INSTANCE = ControllerInstanceId(UUID("44444444-4444-4444-4444-444444444444"))
_DISPLAY = DisplayIdentity(":0")


@dataclass
class _Scanner:
    snapshot: WorkerNamespaceSnapshot
    seen: list[StateNamespace]

    def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
        self.seen.append(namespace)
        return self.snapshot


def _unit(sequence: int = 7, *, name: str = "monitor-probe@7.service") -> WorkerUnit:
    action_id = ActionId(_OLD_INSTANCE, ActionKind.PROBE, sequence)
    return WorkerUnit(action_id, name)


def _in_flight_state() -> State:
    unit = _unit()
    key = ProbeAttemptKey(2, "external", ObservationKey("probe-evidence"))
    return State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        phase=ControllerPhase.PROBING,
        physical_epoch=2,
        attempted_probe_keys=frozenset({key}),
        probe=ProbeAction(
            action_id=unit.action_id,
            key=key,
            admitted_event_generation=EventGeneration(3),
            output="DP-3",
            internal_output="eDP-1",
            preferred_mode="3840x2160",
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=unit,
        ),
        event_generation=EventGeneration(3),
        action_sequence_high_water=7,
    )


def test_same_boot_reconstructs_exact_surviving_worker_without_effects() -> None:
    scanner = _Scanner(WorkerNamespaceSnapshot(units=(_unit(),)), [])

    result = recover_state(
        _in_flight_state(),
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert result.authority_allowed
    assert not result.requires_fresh_observation
    assert result.effects == ()
    assert result.state.recovery_units == (_unit(),)
    assert result.state.probe == _in_flight_state().probe
    assert result.state.controller_instance == _NEW_INSTANCE
    assert scanner.seen == [StateNamespace.ACTIVE]


def test_same_boot_unknown_or_ambiguous_survivor_remains_fail_closed() -> None:
    unknown = _unit(9, name="monitor-probe@9.service")
    scanner = _Scanner(
        WorkerNamespaceSnapshot(
            units=(unknown,),
            ambiguities=("supervisor result and transaction disagree",),
        ),
        [],
    )

    result = recover_state(
        _in_flight_state(),
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert not result.authority_allowed
    assert result.requires_fresh_observation
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.effects == ()
    assert result.state.recovery_units == (unknown,)
    assert any(
        "ambiguous" in reason or "disagree" in reason for reason in result.reasons
    )


def test_duplicate_worker_identity_is_detected_without_scanner_hint() -> None:
    first = _unit(9, name="monitor-probe@first.service")
    second = _unit(9, name="monitor-probe@second.service")
    scanner = _Scanner(WorkerNamespaceSnapshot(units=(first, second)), [])

    result = recover_state(
        None,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
        corruption=ValueError("invalid state"),
    )

    assert not result.authority_allowed
    assert result.state.phase is ControllerPhase.RECOVERING
    assert any("conflicting units" in reason for reason in result.reasons)
    assert result.effects == ()


def test_corrupt_state_never_discards_worker_exclusions_or_authorizes_work() -> None:
    survivor = _unit(41)
    scanner = _Scanner(
        WorkerNamespaceSnapshot(
            units=(survivor,),
            action_sequence_high_water=43,
        ),
        [],
    )

    result = recover_state(
        None,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
        corruption=ValueError("duplicate JSON field"),
    )

    assert not result.authority_allowed
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.state.recovery_units == (survivor,)
    assert result.state.action_sequence_high_water == 43
    assert result.effects == ()
    assert any("corrupt" in reason for reason in result.reasons)


def test_boot_change_drops_monotonic_waits_and_keeps_verified_durable_facts() -> None:
    tombstone = ActionTombstone(
        ActionId(_OLD_INSTANCE, ActionKind.APPLICATION, 8),
        ActionLifecycle.COMPLETED,
    )
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        phase=ControllerPhase.DISCOVER_FAST,
        aggressive_deadline_ms=99_000,
        next_timer_ms=88_000,
        verify_since_ms=77_000,
        last_drm_at_ms=76_000,
        desktop_finalized_profile="external",
        action_sequence_high_water=8,
        action_tombstones=(tombstone,),
    )
    scanner = _Scanner(
        WorkerNamespaceSnapshot(
            verified_tombstones=(tombstone,),
            verified_finalized_profile="external",
        ),
        [],
    )

    result = recover_state(
        persisted,
        current_boot_id=_NEW_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert not result.authority_allowed
    assert result.requires_fresh_observation
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.state.boot_id == _NEW_BOOT
    assert result.state.controller_instance == _NEW_INSTANCE
    assert result.state.aggressive_deadline_ms is None
    assert result.state.next_timer_ms is None
    assert result.state.verify_since_ms is None
    assert result.state.last_drm_at_ms is None
    assert result.state.desktop_finalized_profile == "external"
    assert result.state.action_tombstones == (tombstone,)
    assert result.effects == ()


def test_boot_change_drops_unverified_terminal_facts() -> None:
    unverified = ActionTombstone(
        ActionId(_OLD_INSTANCE, ActionKind.APPLICATION, 8),
        ActionLifecycle.COMPLETED,
    )
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        desktop_finalized_profile="external",
        action_sequence_high_water=8,
        action_tombstones=(unverified,),
    )
    scanner = _Scanner(WorkerNamespaceSnapshot(), [])

    result = recover_state(
        persisted,
        current_boot_id=_NEW_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert result.state.desktop_finalized_profile is None
    assert result.state.action_tombstones == ()
    assert result.state.action_sequence_high_water == 8
    assert result.requires_fresh_observation


def test_instance_uuid_and_recovered_high_water_prevent_sequence_collision() -> None:
    survivor = _unit(900)
    scanner = _Scanner(WorkerNamespaceSnapshot(units=(survivor,)), [])

    result = recover_state(
        None,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.SHADOW,
        scanner=scanner,
        corruption=ValueError("truncated"),
    )
    next_id = ActionId(
        result.state.controller_instance,
        ActionKind.PROBE,
        result.state.action_sequence_high_water + 1,
    )

    assert result.state.action_sequence_high_water == 900
    assert next_id != survivor.action_id
    assert next_id.value != survivor.action_id.value
    assert scanner.seen == [StateNamespace.SHADOW]


def test_same_boot_recovery_unit_requires_verified_terminal_tombstone() -> None:
    survivor = _unit(17)
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        action_sequence_high_water=17,
        recovery_units=(survivor,),
    )

    unresolved = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=_Scanner(WorkerNamespaceSnapshot(), []),
    )

    assert not unresolved.authority_allowed
    assert unresolved.state.phase is ControllerPhase.RECOVERING
    assert unresolved.state.recovery_units == (survivor,)
    assert any("verified terminal tombstone" in item for item in unresolved.reasons)

    tombstone = ActionTombstone(survivor.action_id, ActionLifecycle.CANCELLED)
    resolved = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=_Scanner(WorkerNamespaceSnapshot(verified_tombstones=(tombstone,)), []),
    )

    assert resolved.authority_allowed
    assert resolved.state.recovery_units == ()
    assert resolved.state.action_tombstones == (tombstone,)
    assert resolved.effects == ()


def test_surviving_worker_and_terminal_tombstone_contradiction_is_denied() -> None:
    survivor = _unit(23)
    tombstone = ActionTombstone(survivor.action_id, ActionLifecycle.COMPLETED)
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        action_sequence_high_water=23,
    )
    scanner = _Scanner(
        WorkerNamespaceSnapshot(
            units=(survivor,),
            verified_tombstones=(tombstone,),
        ),
        [],
    )

    result = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert not result.authority_allowed
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.state.recovery_units == (survivor,)
    assert result.state.action_tombstones == ()
    assert result.effects == ()
    assert any("terminal tombstone" in item for item in result.reasons)


def test_same_boot_finalized_profile_disagreement_is_denied() -> None:
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        desktop_finalized_profile="external",
    )
    scanner = _Scanner(WorkerNamespaceSnapshot(verified_finalized_profile="laptop"), [])

    result = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert not result.authority_allowed
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.effects == ()
    assert any("finalized profile disagrees" in item for item in result.reasons)


def test_reconstructed_high_water_marks_cover_all_known_identifiers() -> None:
    survivor = _unit(71)
    tombstone = ActionTombstone(
        ActionId(_OLD_INSTANCE, ActionKind.APPLICATION, 83),
        ActionLifecycle.COMPLETED,
    )
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        action_sequence_high_water=5,
        transition_sequence_high_water=41,
    )
    snapshot = WorkerNamespaceSnapshot(
        units=(survivor,),
        verified_tombstones=(tombstone,),
        action_sequence_high_water=3,
        transition_sequence_high_water=2,
    )

    same_boot = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=_Scanner(snapshot, []),
    )
    boot_change = recover_state(
        persisted,
        current_boot_id=_NEW_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=_Scanner(snapshot, []),
    )

    assert same_boot.state.action_sequence_high_water == 83
    assert same_boot.state.transition_sequence_high_water == 41
    assert boot_change.state.action_sequence_high_water == 83
    assert boot_change.state.transition_sequence_high_water == 41


def test_display_identity_mismatch_never_grants_authority() -> None:
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=DisplayIdentity(":1"),
        desktop_finalized_profile="external",
    )

    result = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=_Scanner(WorkerNamespaceSnapshot(), []),
    )

    assert not result.authority_allowed
    assert result.requires_fresh_observation
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.state.display_identity == _DISPLAY
    assert result.state.desktop_finalized_profile is None
    assert result.effects == ()
    assert any("display identity" in item for item in result.reasons)


def test_scanner_failure_retains_persisted_recovery_exclusions() -> None:
    survivor = _unit(101)
    persisted = State(
        boot_id=_OLD_BOOT,
        controller_instance=_OLD_INSTANCE,
        display_identity=_DISPLAY,
        action_sequence_high_water=101,
        recovery_units=(survivor,),
    )

    class BrokenScanner:
        def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
            raise OSError(namespace.value)

    result = recover_state(
        persisted,
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=BrokenScanner(),
    )

    assert not result.authority_allowed
    assert result.state.recovery_units == (survivor,)
    assert result.state.action_sequence_high_water == 101
    assert result.effects == ()


def test_scanner_failure_is_a_fail_closed_result() -> None:
    class BrokenScanner:
        def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
            raise OSError(namespace.value)

    result = recover_state(
        _in_flight_state(),
        current_boot_id=_OLD_BOOT,
        controller_instance=_NEW_INSTANCE,
        display_identity=_DISPLAY,
        namespace=StateNamespace.ACTIVE,
        scanner=BrokenScanner(),
    )

    assert not result.authority_allowed
    assert result.state.phase is ControllerPhase.RECOVERING
    assert result.effects == ()
    assert any("scan failed" in reason for reason in result.reasons)
