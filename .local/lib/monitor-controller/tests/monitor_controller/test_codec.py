"""Tests for the strict authoritative-state codec."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from monitor_controller.codec import (
    MAX_JSON_INTEGER,
    StateCodecError,
    decode_state,
    decode_state_value,
    encode_state,
)
from monitor_controller.model import (
    SCHEMA_VERSION,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    ApplicationAttemptKey,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EventGeneration,
    EventMetadata,
    ObservationKey,
    PlanningState,
    PlanRequested,
    PreparationState,
    ProbeAction,
    ProbeAttemptKey,
    State,
    WorkerCancellationAcknowledged,
    WorkerUnit,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.simulation.replay import (
    REPLAY_SCHEMA_VERSION,
    ReplayFormatError,
    ReplayTrace,
    capture_replay,
    decode_replay,
    encode_replay,
    replay,
)

_BOOT = BootId(UUID("11111111-1111-1111-1111-111111111111"))
_INSTANCE = ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222"))
_FIXTURES = Path(__file__).parent / "fixtures"
_LIVE_V2_STATE = _FIXTURES / "state" / "live-shadow-v2-sanitized.json"
_PREPARATION_TRACE = _FIXTURES / "traces" / "genuine_unplug.jsonl"


def _state() -> State:
    action_id = ActionId(_INSTANCE, ActionKind.PROBE, 1)
    return State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":0"),
        phase=ControllerPhase.PROBE_FAILED,
        physical_epoch=3,
        next_timer_ms=10_000,
        attempted_probe_keys=frozenset(
            {ProbeAttemptKey(3, "external", ObservationKey("observation-1"))}
        ),
        probe=ProbeAction(
            action_id=action_id,
            key=ProbeAttemptKey(3, "external", ObservationKey("observation-1")),
            admitted_event_generation=EventGeneration(7),
            output="DP-3",
            internal_output="eDP-1",
            preferred_mode="3840x2160",
            lifecycle=ActionLifecycle.FAILED,
            exit_status=1,
        ),
        event_generation=EventGeneration(7),
        action_sequence_high_water=2,
        action_tombstones=(
            ActionTombstone(
                ActionId(_INSTANCE, ActionKind.APPLICATION, 2),
                ActionLifecycle.CANCELLED,
            ),
            ActionTombstone(action_id, ActionLifecycle.FAILED),
        ),
    )


def _document() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(encode_state(_state())))


def test_state_codec_round_trips_all_present_relationships_deterministically() -> None:
    state = _state()

    first = encode_state(state)
    decoded = decode_state(first)

    assert decoded == state
    assert decoded.schema_version == SCHEMA_VERSION == 3
    assert encode_state(decoded) == first
    assert first == encode_state(state)


def test_codec_round_trips_multiple_application_attempt_keys() -> None:
    state = replace(
        _state(),
        attempted_application_keys=frozenset(
            {
                ApplicationAttemptKey(3, "external-a", ObservationKey("key-a")),
                ApplicationAttemptKey(3, "external-b", ObservationKey("key-b")),
            }
        ),
    )

    assert decode_state(encode_state(state)) == state


def test_version_one_state_migrates_in_flight_worker_to_immediate_deadline() -> None:
    state = _state()
    probe = state.probe
    assert probe is not None
    unit = WorkerUnit(probe.action_id, "monitor-probe@1.service")
    dispatched = replace(
        state,
        phase=ControllerPhase.PROBING,
        probe=replace(
            probe,
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=unit,
            worker_deadline_ms=123_456,
            exit_status=None,
        ),
        action_tombstones=state.action_tombstones[:1],
    )
    document = cast("dict[str, object]", json.loads(encode_state(dispatched)))
    document["schema_version"] = 1
    old_probe = cast("dict[str, object]", document["probe"])
    old_probe.pop("worker_deadline_ms")

    migrated = decode_state(json.dumps(document))

    assert migrated.schema_version == SCHEMA_VERSION
    assert migrated.probe is not None
    assert migrated.probe.lifecycle is ActionLifecycle.DISPATCHED
    assert migrated.probe.worker_deadline_ms == 0
    assert decode_state(encode_state(migrated)) == migrated


def test_version_one_state_rejects_current_only_or_malformed_fields() -> None:
    document = _document()
    document["schema_version"] = 1
    probe = cast("dict[str, object]", document["probe"])
    probe["worker_deadline_ms"] = "not-an-old-schema-field"

    with pytest.raises(StateCodecError, match="not valid in schema version 1"):
        decode_state(json.dumps(document))


def _live_v2_document() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(_LIVE_V2_STATE.read_text()))


def _pending_live_v2_document() -> dict[str, object]:
    document = _live_v2_document()
    planning = cast("dict[str, object]", document["planning"])
    planning["lifecycle"] = ActionLifecycle.ADMITTED.value
    document["planning_state"] = PlanningState.PLAN_PENDING.value
    document["action_tombstones"] = [
        item
        for item in cast("list[dict[str, object]]", document["action_tombstones"])
        if cast("dict[str, object]", item["action_id"])["sequence"] != 12
    ]
    return document


def test_version_two_without_planning_preserves_independent_trusted_facts() -> None:
    state = replace(_state(), desktop_finalized_profile="celtic")
    document = cast("dict[str, object]", json.loads(encode_state(state)))
    document["schema_version"] = 2

    migrated = decode_state(json.dumps(document))

    assert migrated == state
    assert migrated.desktop_finalized_profile == "celtic"
    assert migrated.attempted_probe_keys == state.attempted_probe_keys
    assert migrated.action_tombstones == state.action_tombstones


def test_version_two_live_state_revokes_planning_and_reobserves() -> None:
    document = _live_v2_document()
    original_tombstones = tuple(
        (
            cast("dict[str, object]", item["action_id"])["kind"],
            cast("dict[str, object]", item["action_id"])["sequence"],
        )
        for item in cast("list[dict[str, object]]", document["action_tombstones"])
    )

    migrated = decode_state(json.dumps(document))

    assert migrated.schema_version == SCHEMA_VERSION
    assert migrated.phase is ControllerPhase.RECOVERING
    assert migrated.planning_state is PlanningState.PLAN_IDLE
    assert migrated.preparation_state is PreparationState.PREPARE_IDLE
    assert migrated.planning is None
    assert migrated.preparation is None
    assert migrated.finalization is None
    assert migrated.latest_observation is None
    assert migrated.physical_token is None
    assert migrated.verify_since_ms is None
    assert migrated.baseline_adoption
    assert migrated.action_sequence_high_water == 12
    assert migrated.transition_sequence_high_water == 8
    assert (
        tuple(
            (item.action_id.kind.value, item.action_id.sequence)
            for item in migrated.action_tombstones
        )
        == original_tombstones
    )
    assert len(migrated.action_tombstones) == 12
    assert decode_state(encode_state(migrated)) == migrated


def _legacy_v2_preparing_document() -> dict[str, object]:
    for line in _PREPARATION_TRACE.read_text().splitlines():
        record = cast("dict[str, object]", json.loads(line))
        state = record.get("state")
        if not isinstance(state, dict):
            continue
        state_data = cast("dict[str, object]", state)
        preparation = state_data.get("preparation")
        if not isinstance(preparation, dict):
            continue
        preparation_data = cast("dict[str, object]", preparation)
        if preparation_data.get("lifecycle") != ActionLifecycle.DISPATCHED.value:
            continue
        state_data["schema_version"] = 2
        planning = cast("dict[str, object]", state_data["planning"])
        input_key = cast("dict[str, object]", planning["input_key"])
        input_key.pop("active_outputs")
        return state_data
    message = "preparation trace lacks a dispatched preparation state"
    raise AssertionError(message)


def test_version_two_preparation_retains_worker_exclusion_during_recovery() -> None:
    document = _legacy_v2_preparing_document()
    preparation = cast("dict[str, object]", document["preparation"])
    old_id = cast("dict[str, object]", preparation["action_id"])

    migrated = decode_state(json.dumps(document))

    assert migrated.phase is ControllerPhase.RECOVERING
    assert migrated.planning is None
    assert migrated.preparation is None
    assert len(migrated.recovery_units) == 1
    retained = migrated.recovery_units[0]
    assert retained.action_id.kind.value == old_id["kind"]
    assert retained.action_id.sequence == old_id["sequence"]
    assert all(
        item.action_id != retained.action_id for item in migrated.action_tombstones
    )


def test_version_two_pending_plan_is_cancelled_without_becoming_authority() -> None:
    document = _pending_live_v2_document()
    old_action_id = cast(
        "dict[str, object]",
        cast("dict[str, object]", document["planning"])["action_id"],
    )
    old_instance = cast("dict[str, object]", old_action_id["controller_instance"])[
        "value"
    ]

    migrated = decode_state(json.dumps(document))

    assert migrated.phase is ControllerPhase.RECOVERING
    assert migrated.planning is None
    cancelled = [
        item
        for item in migrated.action_tombstones
        if item.action_id.controller_instance.value.hex
        == str(old_instance).replace("-", "")
        and item.action_id.kind.value == old_action_id["kind"]
        and item.action_id.sequence == old_action_id["sequence"]
    ]
    assert len(cancelled) == 1
    assert cancelled[0].lifecycle is ActionLifecycle.CANCELLED


@pytest.mark.parametrize("corruption", ["current-only", "missing-key"])
def test_version_two_migration_rejects_malformed_planning_keys(
    corruption: str,
) -> None:
    document = _live_v2_document()
    planning = cast("dict[str, object]", document["planning"])
    input_key = cast("dict[str, object]", planning["input_key"])
    if corruption == "current-only":
        input_key["active_outputs"] = ["eDP"]
        message = "not valid in schema version 2"
    else:
        input_key.pop("mapping")
        message = "missing fields"

    with pytest.raises(StateCodecError, match=message):
        decode_state(json.dumps(document))


def _downgrade_replay_value(value: object) -> None:
    if isinstance(value, list):
        for item in cast("list[object]", value):
            _downgrade_replay_value(item)
        return
    if not isinstance(value, dict):
        return
    data = cast("dict[str, object]", value)
    if data.get("schema_version") == SCHEMA_VERSION and "boot_id" in data:
        data["schema_version"] = 2
    planning_fields = {
        "physical_epoch",
        "profile",
        "layout",
        "observation_key",
        "mapping",
        "active_outputs",
        "configuration_hashes",
    }
    if set(data) == planning_fields:
        data.pop("active_outputs")
    for item in data.values():
        _downgrade_replay_value(item)


def test_version_two_replay_migrates_planning_keys_deterministically() -> None:
    legacy_initial = _pending_live_v2_document()
    initial = decode_state_value(legacy_initial, authoritative=False)
    planning = initial.planning
    assert planning is not None
    event = PlanRequested(
        EventMetadata(378_366_136, initial.boot_id),
        planning.action_id,
        planning.input_key,
    )
    trace = capture_replay(initial, (event,))
    records = [json.loads(line) for line in encode_replay(trace).splitlines()]
    records[0]["schema_version"] = 2
    for record in records:
        _downgrade_replay_value(record)
    encoded = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )

    migrated = decode_replay(encoded)

    assert migrated == trace
    assert replay(migrated) == tuple(step.expected for step in trace.steps)
    assert decode_replay(encode_replay(migrated)) == migrated


def test_duplicate_fields_are_rejected_before_construction() -> None:
    encoded = encode_state(_state()).decode()
    version_field = f'"schema_version":{SCHEMA_VERSION}'
    duplicate = encoded.replace(version_field, f"{version_field},{version_field}", 1)

    with pytest.raises(StateCodecError, match="duplicate JSON field"):
        decode_state(duplicate)


def test_unknown_root_and_nested_fields_are_rejected() -> None:
    document = _document()
    document["command"] = "xrandr --auto"
    with pytest.raises(StateCodecError, match=r"unknown fields.*command"):
        decode_state(json.dumps(document))

    document = _document()
    probe = cast("dict[str, object]", document["probe"])
    probe["shell"] = True
    with pytest.raises(StateCodecError, match=r"unknown fields.*shell"):
        decode_state(json.dumps(document))


def test_truncation_invalid_utf8_float_and_oversize_are_rejected() -> None:
    encoded = encode_state(_state())
    with pytest.raises(StateCodecError, match="cannot decode"):
        decode_state(encoded[:-1])
    with pytest.raises(StateCodecError, match="UTF-8"):
        decode_state(b"\xff")
    with pytest.raises(StateCodecError, match="floating-point"):
        decode_state(encoded.replace(b'"physical_epoch":3', b'"physical_epoch":3.0'))
    with pytest.raises(StateCodecError, match="exceeds"):
        decode_state(encoded, max_bytes=len(encoded) - 1)


def test_schema_enums_uuid_and_numeric_types_are_strict() -> None:
    mutations: tuple[tuple[str, object, str], ...] = (
        ("schema_version", SCHEMA_VERSION + 1, "schema_version"),
        ("phase", "run-this", "invalid enum"),
        ("physical_epoch", "1+2", "must be an integer"),
        ("physical_epoch", True, "must be an integer"),
        ("controller_instance", {"value": "not-a-uuid"}, "not a UUID"),
    )
    for field, value, message in mutations:
        document = _document()
        document[field] = value
        with pytest.raises(StateCodecError, match=message):
            decode_state(json.dumps(document))

    document = _document()
    document["physical_epoch"] = MAX_JSON_INTEGER + 1
    with pytest.raises(StateCodecError, match="outside the supported range"):
        decode_state(json.dumps(document))


def test_recovering_state_still_enforces_action_and_planning_relationships() -> None:
    recovered = State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":0"),
    )
    for field, value, message in (
        ("planning_state", PlanningState.PLAN_READY.value, "idle planning"),
        ("preparation_state", PreparationState.PREPARED.value, "idle preparation"),
    ):
        document = cast("dict[str, object]", json.loads(encode_state(recovered)))
        document[field] = value
        with pytest.raises(StateCodecError, match=message):
            decode_state(json.dumps(document))

    document = _document()
    document["phase"] = ControllerPhase.RECOVERING.value
    with pytest.raises(StateCodecError, match="cannot retain action identities"):
        decode_state(json.dumps(document))


def test_cross_field_lifecycle_corruption_is_rejected() -> None:
    document = _document()
    document["phase"] = ControllerPhase.QUIESCENT.value
    with pytest.raises(StateCodecError, match="relationships are invalid"):
        decode_state(json.dumps(document))

    document = _document()
    document["phase"] = ControllerPhase.PROBE_PENDING.value
    probe = cast("dict[str, object]", document["probe"])
    probe["lifecycle"] = ActionLifecycle.ADMITTED.value
    probe["exit_status"] = None
    document["action_tombstones"] = cast(
        "list[dict[str, object]]", document["action_tombstones"]
    )[:1]
    with pytest.raises(StateCodecError, match="already be attempted"):
        decode_state(json.dumps(document))

    document = _document()
    tombstones = cast("list[dict[str, object]]", document["action_tombstones"])
    tombstone_id = cast("dict[str, object]", tombstones[0]["action_id"])
    tombstone_id["sequence"] = 1
    with pytest.raises(StateCodecError, match="multiple action kinds"):
        decode_state(json.dumps(document))


@pytest.mark.parametrize(
    "lifecycle",
    [
        ActionLifecycle.FAILED,
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    ],
)
def test_retained_failure_requires_matching_terminal_tombstone(
    lifecycle: ActionLifecycle,
) -> None:
    state = _state()
    probe = state.probe
    assert probe is not None
    matching = ActionTombstone(probe.action_id, lifecycle)
    valid = replace(
        state,
        probe=replace(probe, lifecycle=lifecycle),
        action_tombstones=(state.action_tombstones[0], matching),
    )
    malformed = replace(valid, action_tombstones=valid.action_tombstones[:1])

    with pytest.raises(StateCodecError, match="matching terminal tombstone"):
        encode_state(malformed)

    document = cast("dict[str, object]", json.loads(encode_state(valid)))
    document["action_tombstones"] = cast(
        "list[dict[str, object]]", document["action_tombstones"]
    )[:1]
    with pytest.raises(StateCodecError, match="matching terminal tombstone"):
        decode_state(json.dumps(document))


@pytest.mark.parametrize(
    "lifecycle",
    [
        ActionLifecycle.FAILED,
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    ],
)
def test_replay_rejects_retained_terminal_action_without_tombstone(
    lifecycle: ActionLifecycle,
) -> None:
    state = _state()
    probe = state.probe
    assert probe is not None
    state = replace(
        state,
        probe=replace(probe, lifecycle=lifecycle),
        action_tombstones=(
            state.action_tombstones[0],
            ActionTombstone(probe.action_id, lifecycle),
        ),
    )
    malformed = replace(state, action_tombstones=state.action_tombstones[:1])

    with pytest.raises(ReplayFormatError, match="matching terminal tombstone"):
        encode_replay(ReplayTrace(malformed, ()))

    records = [json.loads(encode_replay(ReplayTrace(state, ())).decode())]
    initial_state = cast("dict[str, object]", records[0]["initial_state"])
    initial_state["action_tombstones"] = cast(
        "list[dict[str, object]]", initial_state["action_tombstones"]
    )[:1]
    payload = json.dumps(records[0], sort_keys=True, separators=(",", ":")) + "\n"
    with pytest.raises(ReplayFormatError, match="matching terminal tombstone"):
        decode_replay(payload)


def test_v1_cancellation_replay_migrates_and_current_schema_round_trips(
    tmp_path: Path,
) -> None:
    state = _state()
    probe = state.probe
    assert probe is not None
    stopping = replace(
        state,
        phase=ControllerPhase.PROBING,
        probe=replace(
            probe,
            lifecycle=ActionLifecycle.STOPPING,
            unit=WorkerUnit(probe.action_id, "monitor-probe@1.service"),
            worker_deadline_ms=1_000,
            exit_status=None,
            terminal_after_stop=None,
        ),
        action_tombstones=state.action_tombstones[:1],
    )
    event = WorkerCancellationAcknowledged(
        EventMetadata(500, _BOOT),
        probe.action_id,
        ActionLifecycle.CANCELLED,
        143,
    )
    trace = capture_replay(stopping, (event,))
    current = encode_replay(trace)
    current_records = [json.loads(line) for line in current.splitlines()]
    assert current_records[0]["schema_version"] == REPLAY_SCHEMA_VERSION == 3
    assert decode_replay(current) == trace

    legacy_records = [json.loads(line) for line in current.splitlines()]
    legacy_records[0]["schema_version"] = 1
    legacy_event = legacy_records[1]["event"]
    legacy_event.pop("terminal_lifecycle")
    legacy_event.pop("exit_status")
    legacy = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in legacy_records
    )
    migrated = decode_replay(legacy)

    assert migrated == trace
    assert replay(migrated) == (trace.steps[0].expected,)
    assert decode_replay(encode_replay(migrated)) == migrated

    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(legacy, encoding="utf-8")
    audit = RotatingAuditLog(audit_path, stopping, max_files=2)
    retained_legacy = audit_path.with_name("audit.jsonl.1")
    assert retained_legacy in audit.retained_paths
    assert replay(decode_replay(retained_legacy.read_bytes()))


def test_failed_decode_cannot_partially_mutate_existing_state() -> None:
    existing = _state()
    encoded = encode_state(existing).decode()
    corrupted = encoded.replace('"physical_epoch":3', '"physical_epoch":"3+0"', 1)

    with pytest.raises(StateCodecError):
        decode_state(corrupted)

    assert existing == _state()
    assert replace(existing) == existing
