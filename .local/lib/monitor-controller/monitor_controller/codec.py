"""Encode and decode authoritative controller state as strict, bounded JSON."""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass, replace
from enum import Enum
from typing import Any, Never, cast, get_args, get_origin, get_type_hints
from uuid import UUID

from .invariants import assert_controller_invariants
from .model import (
    SCHEMA_VERSION,
    TERMINAL_ACTION_LIFECYCLES,
    ActionId,
    ActionLifecycle,
    ActionRecord,
    ActionTombstone,
    ControllerPhase,
    PlanningAction,
    PlanningState,
    PreparationState,
    State,
    bound_action_tombstones,
)
from .strictjson import strict_loads

MAX_STATE_BYTES: int = 1_048_576
MAX_JSON_INTEGER: int = (1 << 53) - 1
MAX_STRING_LENGTH = 65_536
MAX_TOMBSTONES = 1_024
MAX_RECOVERY_UNITS = 256
MAX_ATTEMPT_KEYS = 4_096
_LEGACY_SCHEMA_VERSION = 1
_PREVIOUS_SCHEMA_VERSION = 2
_STATE_WORKER_ACTION_FIELDS = (
    "probe",
    "application",
    "preparation",
    "finalization",
)
_IN_FLIGHT_LIFECYCLE_VALUES = frozenset(
    {
        ActionLifecycle.DISPATCHED.value,
        ActionLifecycle.STOPPING.value,
        ActionLifecycle.RESULT_PENDING.value,
    }
)


class StateCodecError(ValueError):
    """Raised when authoritative state is not strict, valid schema data."""


def _fail(message: str) -> Never:
    raise StateCodecError(message)


def _decode_document(data: bytes | str, max_bytes: int) -> object:
    if isinstance(data, bytes):
        size = len(data)
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            message = "state is not valid UTF-8"
            raise StateCodecError(message) from error
    else:
        try:
            size = len(data.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as error:
            message = "state is not valid UTF-8"
            raise StateCodecError(message) from error
        text = data
    if size > max_bytes:
        _fail(f"state record exceeds {max_bytes} bytes")
    if not text:
        _fail("state record is empty")
    try:
        return strict_loads(
            text,
            StateCodecError,
            reject_floats=True,
            max_integer=MAX_JSON_INTEGER,
        )
    except StateCodecError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        message = f"cannot decode state JSON: {error}"
        raise StateCodecError(message) from error


def _strict_string(value: object, path: str) -> str:
    if not isinstance(value, str):
        _fail(f"{path} must be a string")
    if len(value) > MAX_STRING_LENGTH:
        _fail(f"{path} string is too long")
    if "\x00" in value:
        _fail(f"{path} contains a NUL byte")
    return value


def _decode_union(value: object, choices: tuple[object, ...], path: str) -> object:
    if value is None and type(None) in choices:
        return None
    errors: list[str] = []
    for choice in choices:
        if choice is type(None):
            continue
        try:
            return _decode_value(value, choice, path)
        except StateCodecError as error:
            errors.append(str(error))
    detail = "; ".join(errors[:3])
    _fail(f"{path} does not match any permitted schema type: {detail}")


def _decode_dataclass(value: object, expected: object, path: str) -> object:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    object_value = cast("dict[str, object]", value)
    dataclass_type = cast("type[object]", expected)
    dataclass_fields = fields(cast("Any", dataclass_type))
    allowed = {field.name for field in dataclass_fields}
    actual = set(object_value)
    unknown = actual - allowed
    missing = allowed - actual
    optional = {
        item.name for item in dataclass_fields if item.metadata.get("codec_optional")
    }
    if unknown:
        _fail(f"{path} has unknown fields: {', '.join(sorted(unknown))}")
    required_missing = missing - optional
    if required_missing:
        _fail(f"{path} is missing fields: {', '.join(sorted(required_missing))}")
    hints = cast("dict[str, object]", get_type_hints(dataclass_type))
    kwargs = {
        item.name: _decode_value(
            object_value[item.name], hints[item.name], f"{path}.{item.name}"
        )
        for item in dataclass_fields
        if item.name in object_value
    }
    constructor = cast("Callable[..., object]", dataclass_type)
    try:
        return constructor(**kwargs)
    except (TypeError, ValueError) as error:
        message = f"{path} is invalid: {error}"
        raise StateCodecError(message) from error


def _decode_value(  # noqa: C901, PLR0911, PLR0912
    value: object, expected: object, path: str
) -> object:
    origin = get_origin(expected)
    args = cast("tuple[object, ...]", get_args(expected))
    if origin is types.UnionType:
        return _decode_union(value, args, path)
    if origin is tuple:
        if not isinstance(value, list):
            _fail(f"{path} must be an array")
        if len(args) != 2 or args[1] is not Ellipsis:  # noqa: PLR2004
            _fail(f"{path} uses an unsupported tuple schema")
        items = cast("list[object]", value)
        return tuple(
            _decode_value(item, args[0], f"{path}[{index}]")
            for index, item in enumerate(items)
        )
    if origin is frozenset:
        if not isinstance(value, list):
            _fail(f"{path} must be an array")
        items = cast("list[object]", value)
        decoded = tuple(
            _decode_value(item, args[0], f"{path}[{index}]")
            for index, item in enumerate(items)
        )
        try:
            result = frozenset(decoded)
        except TypeError as error:
            message = f"{path} contains an unhashable value"
            raise StateCodecError(message) from error
        if len(result) != len(decoded):
            _fail(f"{path} contains duplicate values")
        return result
    if expected is str:
        return _strict_string(value, path)
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(f"{path} must be an integer")
        if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
            _fail(f"{path} integer is outside the supported range")
        return value
    if expected is bool:
        if not isinstance(value, bool):
            _fail(f"{path} must be a boolean")
        return value
    if expected is UUID:
        text = _strict_string(value, path)
        try:
            parsed = UUID(text)
        except ValueError as error:
            message = f"{path} is not a UUID"
            raise StateCodecError(message) from error
        if str(parsed) != text:
            _fail(f"{path} UUID is not in canonical form")
        return parsed
    if isinstance(expected, type) and issubclass(expected, Enum):
        text = _strict_string(value, path)
        constructor = cast("Callable[[str], object]", expected)
        try:
            return constructor(text)
        except ValueError as error:
            message = f"{path} has an invalid enum value"
            raise StateCodecError(message) from error
    if isinstance(expected, type) and is_dataclass(expected):
        return _decode_dataclass(value, expected, path)
    _fail(f"{path} uses an unsupported schema type")


def _encode_dataclass(value: object, path: str) -> dict[str, object]:
    encoded: dict[str, object] = {}
    for item in fields(cast("Any", value)):
        item_value = cast("object", getattr(value, item.name))
        if item.metadata.get("codec_optional"):
            if item.default is not MISSING:
                default = item.default
            else:
                factory = cast("Callable[[], object]", item.default_factory)
                default = factory()
            if item_value == default:
                continue
        encoded[item.name] = _encode_value(item_value, f"{path}.{item.name}")
    return encoded


def _encode_value(value: object, path: str) -> object:  # noqa: PLR0911
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _encode_dataclass(value, path)
    if isinstance(value, tuple):
        items = cast("tuple[object, ...]", value)
        return [
            _encode_value(item, f"{path}[{index}]") for index, item in enumerate(items)
        ]
    if isinstance(value, frozenset):
        items = cast("frozenset[object]", value)
        encoded = [_encode_value(item, f"{path}[]") for item in items]
        return sorted(
            encoded,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
            _fail(f"{path} integer is outside the supported range")
        return value
    if isinstance(value, str):
        return _strict_string(value, path)
    _fail(f"{path} contains a value outside the state schema")


def encode_schema_value(value: object) -> object:
    """Encode one typed domain value into deterministic JSON-compatible data."""
    return _encode_value(value, "value")


def decode_schema_value(value: object, expected: object) -> object:
    """Strictly decode JSON-compatible data against an explicit domain type."""
    return _decode_value(value, expected, "value")


def _state_actions(state: State) -> tuple[ActionRecord, ...]:
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


def _validate_action_record(action: ActionRecord) -> None:
    if isinstance(action, PlanningAction):
        return
    if action.lifecycle is ActionLifecycle.ADMITTED and (
        action.unit is not None
        or action.worker_deadline_ms is not None
        or action.exit_status is not None
    ):
        _fail("admitted action cannot have dispatch or result data")
    if action.lifecycle is ActionLifecycle.DISPATCHED and (
        action.unit is None
        or action.worker_deadline_ms is None
        or action.exit_status is not None
    ):
        _fail("dispatched action requires its worker unit and deadline")


def _validate_recovering_relationships(
    state: State, actions: tuple[ActionRecord, ...]
) -> None:
    if state.phase is not ControllerPhase.RECOVERING:
        return
    if actions:
        _fail("recovering state cannot retain action identities")
    if state.planning_state is not PlanningState.PLAN_IDLE:
        _fail("recovering state requires idle planning")
    if state.preparation_state is not PreparationState.PREPARE_IDLE:
        _fail("recovering state requires idle preparation")


def _validate_terminal_evidence(
    actions: tuple[ActionRecord, ...],
    tombstones_by_id: Mapping[ActionId, ActionTombstone],
) -> None:
    for action in actions:
        tombstone = tombstones_by_id.get(action.action_id)
        if action.lifecycle in TERMINAL_ACTION_LIFECYCLES and (
            tombstone is None or tombstone.lifecycle is not action.lifecycle
        ):
            _fail("retained terminal action requires a matching terminal tombstone")
        if tombstone is not None and (
            action.lifecycle not in TERMINAL_ACTION_LIFECYCLES
            or tombstone.lifecycle is not action.lifecycle
        ):
            _fail("retained action and terminal tombstone lifecycles must match")


def _validate_persistence_relationships(state: State) -> None:
    actions = _state_actions(state)
    action_ids = {action.action_id for action in actions}
    tombstone_ids = {item.action_id for item in state.action_tombstones}
    recovery_unit_ids = {item.action_id for item in state.recovery_units}
    tombstones_by_id = {item.action_id: item for item in state.action_tombstones}
    _validate_terminal_evidence(actions, tombstones_by_id)
    if recovery_unit_ids & tombstone_ids:
        _fail("possibly-live worker cannot also have terminal evidence")

    _validate_recovering_relationships(state, actions)

    sequence_kinds: dict[tuple[UUID, int], ActionId] = {}
    identities = (
        *action_ids,
        *tombstone_ids,
        *recovery_unit_ids,
    )
    for action_id in identities:
        key = (action_id.controller_instance.value, action_id.sequence)
        previous = sequence_kinds.get(key)
        if previous is not None and previous.kind is not action_id.kind:
            _fail("one controller sequence is assigned to multiple action kinds")
        sequence_kinds[key] = action_id

    for action in actions:
        _validate_action_record(action)

    if (
        state.probe is not None
        and state.probe.lifecycle is ActionLifecycle.ADMITTED
        and state.probe.key in state.attempted_probe_keys
    ):
        _fail("admitted probe key cannot already be attempted")
    if (
        state.application is not None
        and state.application.lifecycle is ActionLifecycle.ADMITTED
        and state.application.key in state.attempted_application_keys
    ):
        _fail("admitted application key cannot already be attempted")
    if any(
        key.physical_epoch != state.physical_epoch
        for key in (
            *state.attempted_probe_keys,
            *state.attempted_application_keys,
        )
    ):
        _fail("attempt history belongs to another physical epoch")


def _validate_bounded_state(state: State) -> None:
    if len(state.action_tombstones) > MAX_TOMBSTONES:
        _fail("state contains too many action tombstones")
    if len(state.recovery_units) > MAX_RECOVERY_UNITS:
        _fail("state contains too many recovery worker units")
    if len(state.attempted_probe_keys) > MAX_ATTEMPT_KEYS:
        _fail("state contains too many attempted probe keys")
    if len(state.attempted_application_keys) > MAX_ATTEMPT_KEYS:
        _fail("state contains too many attempted application keys")
    _validate_persistence_relationships(state)
    try:
        assert_controller_invariants(state)
    except (TypeError, ValueError) as error:
        message = f"state relationships are invalid: {error}"
        raise StateCodecError(message) from error


def validate_state(state: State) -> None:
    """Validate all authoritative-state bounds and cross-field invariants."""
    _validate_bounded_state(state)


def _migrate_version_one_document(
    state_data: dict[str, object],
) -> dict[str, object]:
    migrated = dict(state_data)
    for field in _STATE_WORKER_ACTION_FIELDS:
        action = migrated.get(field)
        if not isinstance(action, dict):
            continue
        action_data = cast("dict[str, object]", action)
        if "worker_deadline_ms" in action_data:
            _fail(f"state.{field}.worker_deadline_ms is not valid in schema version 1")
        migrated_action = dict(action_data)
        lifecycle = migrated_action.get("lifecycle")
        migrated_action["worker_deadline_ms"] = (
            0 if lifecycle in _IN_FLIGHT_LIFECYCLE_VALUES else None
        )
        migrated[field] = migrated_action
    migrated["schema_version"] = _PREVIOUS_SCHEMA_VERSION
    return migrated


def _legacy_active_outputs(state_data: Mapping[str, object]) -> list[object]:
    """Derive the old implicit active set solely for strict legacy validation."""
    planning = state_data.get("planning")
    if not isinstance(planning, dict):
        return []
    input_key = cast("dict[str, object]", planning).get("input_key")
    if not isinstance(input_key, dict):
        return []
    key_data = cast("dict[str, object]", input_key)
    mapping = key_data.get("mapping")
    mapped_outputs: set[str] = set()
    if isinstance(mapping, list):
        for item in cast("list[object]", mapping):
            if isinstance(item, dict):
                live_output = cast("dict[str, object]", item).get("live_output")
                if isinstance(live_output, str):
                    mapped_outputs.add(live_output)

    observation = state_data.get("latest_observation")
    if isinstance(observation, dict):
        observation_data = cast("dict[str, object]", observation)
        if observation_data.get("observation_key") == key_data.get("observation_key"):
            active = observation_data.get("x_active_outputs")
            if isinstance(active, list) and all(
                isinstance(item, str) and item in mapped_outputs
                for item in cast("list[object]", active)
            ):
                return list(cast("list[object]", active))
    # Before v3 every connected mapped output was implicitly treated as active.
    return [cast("object", item) for item in sorted(mapped_outputs)]


def _migrate_version_two_document(
    state_data: dict[str, object],
) -> dict[str, object]:
    migrated = dict(state_data)
    planning = migrated.get("planning")
    if isinstance(planning, dict):
        planning_data = cast("dict[str, object]", planning)
        input_key = planning_data.get("input_key")
        if isinstance(input_key, dict):
            input_data = cast("dict[str, object]", input_key)
            if "active_outputs" in input_data:
                _fail(
                    "state.planning.input_key.active_outputs is not valid "
                    "in schema version 2"
                )
            migrated_input = dict(input_data)
            migrated_input["active_outputs"] = _legacy_active_outputs(migrated)
            migrated_planning = dict(planning_data)
            migrated_planning["input_key"] = migrated_input
            migrated["planning"] = migrated_planning
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def _migrate_state_document(document: object) -> tuple[object, bool]:
    if not isinstance(document, dict):
        return document, False
    state_data = cast("dict[str, object]", document)
    version = state_data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("state.schema_version must be an integer")
    if version == SCHEMA_VERSION:
        return state_data, False
    if version == _LEGACY_SCHEMA_VERSION:
        state_data = _migrate_version_one_document(state_data)
        version = _PREVIOUS_SCHEMA_VERSION
    if version != _PREVIOUS_SCHEMA_VERSION:
        _fail(f"state.schema_version {version} is unsupported")
    had_unproven_plan = state_data.get("planning") is not None
    return _migrate_version_two_document(state_data), had_unproven_plan


def _migration_recovery_state(state: State) -> State:
    """Revoke unproven v1/v2 transition authority while retaining exclusions."""
    tombstones = list(state.action_tombstones)
    protected: set[ActionId] = set()
    recovery_units = list(state.recovery_units)
    seen_units = set(recovery_units)
    for action in _state_actions(state):
        if action.lifecycle in TERMINAL_ACTION_LIFECYCLES:
            continue
        if (
            not isinstance(action, PlanningAction)
            and action.lifecycle.value in _IN_FLIGHT_LIFECYCLE_VALUES
            and action.unit is not None
        ):
            if action.unit not in seen_units:
                recovery_units.append(action.unit)
                seen_units.add(action.unit)
            continue
        tombstone = ActionTombstone(action.action_id, ActionLifecycle.CANCELLED)
        tombstones.append(tombstone)
        protected.add(action.action_id)
    retained_tombstones = bound_action_tombstones(
        tuple(tombstones), protected_action_ids=frozenset(protected)
    )
    return replace(
        state,
        latest_observation=None,
        phase=ControllerPhase.RECOVERING,
        planning_state=PlanningState.PLAN_IDLE,
        preparation_state=PreparationState.PREPARE_IDLE,
        physical_token=None,
        candidate=None,
        aggressive_deadline_ms=None,
        next_timer_ms=None,
        backoff_index=0,
        verify_since_ms=None,
        last_drm_at_ms=None,
        stable_x_profile=None,
        external_intent=False,
        baseline_adoption=state.desktop_finalized_profile is None,
        probe=None,
        application=None,
        planning=None,
        preparation=None,
        finalization=None,
        unknown_key=None,
        unknown_since_ms=None,
        unplug_proof=None,
        action_tombstones=retained_tombstones,
        recovery_units=tuple(recovery_units),
    )


def _decode_state_document(document: object, *, authoritative: bool) -> State:
    migrated_document, had_unproven_plan = _migrate_state_document(document)
    decoded = _decode_value(migrated_document, State, "state")
    if not isinstance(decoded, State):
        _fail("decoded document is not controller state")
    validate_state(decoded)
    if authoritative and had_unproven_plan:
        decoded = _migration_recovery_state(decoded)
        validate_state(decoded)
    return decoded


def encode_state(state: State, *, max_bytes: int = MAX_STATE_BYTES) -> bytes:
    """Encode one validated state to deterministic, bounded UTF-8 JSON."""
    validate_state(state)
    document = _encode_value(state, "state")
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        _fail(f"state record exceeds {max_bytes} bytes")
    return encoded


def decode_state_value(value: object, *, authoritative: bool = True) -> State:
    """Decode one parsed state value, applying strict schema migration."""
    return _decode_state_document(value, authoritative=authoritative)


def decode_state(data: bytes | str, *, max_bytes: int = MAX_STATE_BYTES) -> State:
    """Decode and validate state without mutating any caller-owned value."""
    return _decode_state_document(
        _decode_document(data, max_bytes),
        authoritative=True,
    )
