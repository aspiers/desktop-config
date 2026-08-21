"""Encode and decode authoritative controller state as strict, bounded JSON."""

from __future__ import annotations

import json
import types
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Never, cast, get_args, get_origin, get_type_hints
from uuid import UUID

from monitor_controller.invariants import assert_controller_invariants
from monitor_controller.model import (
    ActionId,
    ActionLifecycle,
    ActionRecord,
    ControllerPhase,
    PlanningAction,
    PlanningState,
    PreparationState,
    State,
)

MAX_STATE_BYTES = 1_048_576
MAX_JSON_INTEGER = (1 << 53) - 1
MAX_STRING_LENGTH = 65_536
MAX_TOMBSTONES = 1_024
MAX_RECOVERY_UNITS = 256
MAX_ATTEMPT_KEYS = 4_096


class StateCodecError(ValueError):
    """Raised when authoritative state is not strict, valid schema data."""


def _fail(message: str) -> Never:
    raise StateCodecError(message)


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _parse_integer(value: str) -> int:
    parsed = int(value)
    if not -MAX_JSON_INTEGER <= parsed <= MAX_JSON_INTEGER:
        _fail("JSON integer is outside the supported range")
    return parsed


def _reject_float(value: str) -> Never:
    _fail(f"floating-point JSON number is forbidden: {value}")


def _reject_constant(value: str) -> object:
    _fail(f"non-finite JSON number is forbidden: {value}")


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
        return json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
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
    if unknown:
        _fail(f"{path} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        _fail(f"{path} is missing fields: {', '.join(sorted(missing))}")
    hints = cast("dict[str, object]", get_type_hints(dataclass_type))
    kwargs = {
        field.name: _decode_value(
            object_value[field.name], hints[field.name], f"{path}.{field.name}"
        )
        for field in dataclass_fields
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


def _encode_value(value: object, path: str) -> object:  # noqa: PLR0911
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _encode_value(
                cast("object", getattr(value, field.name)), f"{path}.{field.name}"
            )
            for field in fields(cast("Any", value))
        }
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
        action.unit is not None or action.exit_status is not None
    ):
        _fail("admitted action cannot have dispatch or result data")
    if action.lifecycle is ActionLifecycle.DISPATCHED and (
        action.unit is None or action.exit_status is not None
    ):
        _fail("dispatched action requires only its worker unit")


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


def _validate_persistence_relationships(state: State) -> None:
    actions = _state_actions(state)
    action_ids = {action.action_id for action in actions}
    tombstone_ids = {item.action_id for item in state.action_tombstones}
    recovery_unit_ids = {item.action_id for item in state.recovery_units}
    if action_ids & tombstone_ids:
        _fail("active action cannot also have a terminal tombstone")
    if recovery_unit_ids & tombstone_ids:
        _fail("surviving worker cannot also have a terminal tombstone")

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


def encode_state(state: State, *, max_bytes: int = MAX_STATE_BYTES) -> bytes:
    """Encode one validated state to deterministic, bounded UTF-8 JSON."""
    _validate_bounded_state(state)
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


def decode_state(data: bytes | str, *, max_bytes: int = MAX_STATE_BYTES) -> State:
    """Decode and validate state without mutating any caller-owned value."""
    document = _decode_document(data, max_bytes)
    decoded = _decode_value(document, State, "state")
    if not isinstance(decoded, State):
        _fail("decoded document is not controller state")
    _validate_bounded_state(decoded)
    return decoded
