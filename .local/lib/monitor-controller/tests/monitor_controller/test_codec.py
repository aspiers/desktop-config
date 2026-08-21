"""Tests for the strict authoritative-state codec."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from monitor_controller.codec import (
    MAX_JSON_INTEGER,
    StateCodecError,
    decode_state,
    encode_state,
)
from monitor_controller.model import (
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
    ObservationKey,
    PlanningState,
    PreparationState,
    ProbeAction,
    ProbeAttemptKey,
    State,
)

_BOOT = BootId(UUID("11111111-1111-1111-1111-111111111111"))
_INSTANCE = ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222"))


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
        ),
    )


def _document() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(encode_state(_state())))


def test_state_codec_round_trips_all_present_relationships_deterministically() -> None:
    state = _state()

    first = encode_state(state)
    decoded = decode_state(first)

    assert decoded == state
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


def test_duplicate_fields_are_rejected_before_construction() -> None:
    encoded = encode_state(_state()).decode()
    duplicate = encoded.replace(
        '"schema_version":1', '"schema_version":1,"schema_version":1', 1
    )

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
        ("schema_version", 2, "schema version"),
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
    with pytest.raises(StateCodecError, match="already be attempted"):
        decode_state(json.dumps(document))

    document = _document()
    tombstones = cast("list[dict[str, object]]", document["action_tombstones"])
    tombstone_id = cast("dict[str, object]", tombstones[0]["action_id"])
    tombstone_id["sequence"] = 1
    with pytest.raises(StateCodecError, match="multiple action kinds"):
        decode_state(json.dumps(document))


def test_failed_decode_cannot_partially_mutate_existing_state() -> None:
    existing = _state()
    encoded = encode_state(existing).decode()
    corrupted = encoded.replace('"physical_epoch":3', '"physical_epoch":"3+0"', 1)

    with pytest.raises(StateCodecError):
        decode_state(corrupted)

    assert existing == _state()
    assert replace(existing) == existing
