# ruff: noqa: EM101, EM102, TRY003, TID252
"""Pure, normalized capture and replay for reducer event streams."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Never, cast

from ..codec import decode_schema_value, encode_schema_value
from ..model import (
    EFFECT_TYPES,
    EVENT_TYPES,
    Decision,
    Effect,
    Event,
    EventEnvelope,
    State,
)
from ..reducer import reduce

REPLAY_SCHEMA_VERSION = 1
MAX_REPLAY_BYTES = 16 * 1_048_576

_EVENT_BY_NAME: Mapping[str, type[EventEnvelope]] = {
    item.__name__: item for item in EVENT_TYPES
}
_EFFECT_BY_NAME: Mapping[str, type[object]] = {
    item.__name__: item for item in EFFECT_TYPES
}


class ReplayFormatError(ValueError):
    """Raised when a replay stream is not strict, bounded schema data."""


class ReplayMismatchError(AssertionError):
    """Raised when a recorded decision differs from a fresh reduction."""


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One reducer input and its complete expected decision."""

    event: Event
    expected: Decision


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    """An initial state and ordered, fully checked reducer decisions."""

    initial_state: State
    steps: tuple[ReplayStep, ...]

    @property
    def events(self) -> tuple[Event, ...]:
        """Return the ordered reducer inputs."""
        return tuple(step.event for step in self.steps)


def capture_replay(initial_state: State, events: Iterable[Event]) -> ReplayTrace:
    """Reduce *events* once and retain every complete expected decision."""
    state = initial_state
    steps: list[ReplayStep] = []
    for event in events:
        decision = reduce(state, event)
        steps.append(ReplayStep(event, decision))
        state = decision.state
    return ReplayTrace(initial_state, tuple(steps))


def replay(trace: ReplayTrace) -> tuple[Decision, ...]:
    """Replay and verify every recorded state and ordered effect exactly."""
    state = trace.initial_state
    decisions: list[Decision] = []
    for index, step in enumerate(trace.steps):
        actual = reduce(state, step.event)
        if actual != step.expected:
            msg = (
                f"replay mismatch at step {index}: expected {step.expected!r}, "
                f"got {actual!r}"
            )
            raise ReplayMismatchError(msg)
        decisions.append(actual)
        state = actual.state
    return tuple(decisions)


def encode_replay(trace: ReplayTrace) -> bytes:
    """Encode a trace as canonical, language-neutral JSONL."""
    records: list[dict[str, object]] = [
        {
            "record": "header",
            "schema_version": REPLAY_SCHEMA_VERSION,
            "initial_state": encode_schema_value(trace.initial_state),
        }
    ]
    records.extend(_encode_step(step) for step in trace.steps)
    payload = b"".join(
        json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )
    if len(payload) > MAX_REPLAY_BYTES:
        raise ReplayFormatError(f"replay exceeds {MAX_REPLAY_BYTES} bytes")
    return payload


def decode_replay(data: bytes | str) -> ReplayTrace:
    """Strictly decode canonical JSONL without executing or reducing it."""
    raw = _bounded_text(data)
    lines = raw.splitlines()
    if not lines:
        raise ReplayFormatError("replay is empty")
    records = tuple(_decode_json_line(line, index) for index, line in enumerate(lines))
    header = records[0]
    _exact_fields(
        header,
        frozenset({"record", "schema_version", "initial_state"}),
        "header",
    )
    if header["record"] != "header":
        raise ReplayFormatError("first replay record must be a header")
    if header["schema_version"] != REPLAY_SCHEMA_VERSION:
        raise ReplayFormatError(
            f"replay schema_version must be {REPLAY_SCHEMA_VERSION}"
        )
    initial = decode_schema_value(header["initial_state"], State)
    if not isinstance(initial, State):  # defensive narrowing for strict checkers
        raise ReplayFormatError("initial_state did not decode as State")
    steps = tuple(
        _decode_step(record, index) for index, record in enumerate(records[1:], start=1)
    )
    return ReplayTrace(initial, steps)


def _encode_step(step: ReplayStep) -> dict[str, object]:
    return {
        "record": "decision",
        "event_type": type(step.event).__name__,
        "event": encode_schema_value(step.event),
        "state": encode_schema_value(step.expected.state),
        "effects": [
            {
                "effect_type": type(effect).__name__,
                "effect": encode_schema_value(effect),
            }
            for effect in step.expected.effects
        ],
    }


def _decode_step(record: Mapping[str, object], index: int) -> ReplayStep:
    where = f"record {index}"
    _exact_fields(
        record,
        frozenset({"record", "event_type", "event", "state", "effects"}),
        where,
    )
    if record["record"] != "decision":
        raise ReplayFormatError(f"{where} must be a decision")
    event_type = _named_type(record["event_type"], _EVENT_BY_NAME, f"{where}.event")
    event = decode_schema_value(record["event"], event_type)
    if not isinstance(event, EVENT_TYPES):
        raise ReplayFormatError(f"{where}.event is outside the closed Event union")
    state = decode_schema_value(record["state"], State)
    if not isinstance(state, State):
        raise ReplayFormatError(f"{where}.state did not decode as State")
    effects_data = record["effects"]
    if not isinstance(effects_data, list):
        raise ReplayFormatError(f"{where}.effects must be an array")
    effects = tuple(
        _decode_effect(item, f"{where}.effects[{effect_index}]")
        for effect_index, item in enumerate(cast("list[object]", effects_data))
    )
    return ReplayStep(cast("Event", event), Decision(state, effects))


def _decode_effect(value: object, where: str) -> Effect:
    if not isinstance(value, dict):
        raise ReplayFormatError(f"{where} must be an object")
    record = cast("dict[str, object]", value)
    _exact_fields(record, frozenset({"effect_type", "effect"}), where)
    effect_type = _named_type(
        record["effect_type"], _EFFECT_BY_NAME, f"{where}.effect_type"
    )
    effect = decode_schema_value(record["effect"], effect_type)
    if not isinstance(effect, EFFECT_TYPES):
        raise ReplayFormatError(f"{where} is outside the closed Effect union")
    return cast("Effect", effect)


def _named_type[T](
    value: object, choices: Mapping[str, type[T]], where: str
) -> type[T]:
    if not isinstance(value, str) or value not in choices:
        raise ReplayFormatError(f"{where} has an unknown type: {value!r}")
    return choices[value]


def _bounded_text(data: bytes | str) -> str:
    if isinstance(data, bytes):
        if len(data) > MAX_REPLAY_BYTES:
            raise ReplayFormatError(f"replay exceeds {MAX_REPLAY_BYTES} bytes")
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ReplayFormatError("replay is not valid UTF-8") from error
    try:
        encoded = data.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ReplayFormatError("replay is not valid UTF-8") from error
    if len(encoded) > MAX_REPLAY_BYTES:
        raise ReplayFormatError(f"replay exceeds {MAX_REPLAY_BYTES} bytes")
    return data


def _duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayFormatError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_number(value: str) -> Never:
    raise ReplayFormatError(f"unsupported JSON number: {value}")


def _decode_json_line(line: str, index: int) -> dict[str, object]:
    if not line:
        raise ReplayFormatError(f"record {index} is empty")
    try:
        value = json.loads(
            line,
            object_pairs_hook=_duplicate_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except ReplayFormatError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ReplayFormatError(f"cannot decode record {index}: {error}") from error
    if not isinstance(value, dict):
        raise ReplayFormatError(f"record {index} must be an object")
    return cast("dict[str, object]", value)


def _exact_fields(
    value: Mapping[str, object], expected: frozenset[str], where: str
) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ReplayFormatError(
            f"{where} fields differ; missing={missing}, unknown={unknown}"
        )
