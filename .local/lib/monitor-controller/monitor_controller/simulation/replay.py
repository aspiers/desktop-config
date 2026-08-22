# ruff: noqa: EM101, EM102, TRY003, TID252
"""Pure, normalized capture and replay for reducer event streams."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Never, cast

from ..codec import (
    StateCodecError,
    decode_schema_value,
    encode_schema_value,
    encode_state,
    validate_state,
)
from ..model import (
    EFFECT_TYPES,
    EVENT_TYPES,
    Decision,
    Effect,
    Event,
    EventEnvelope,
    RequestObservation,
    State,
)
from ..reducer import reduce

REPLAY_SCHEMA_VERSION = 1
MAX_REPLAY_BYTES = 16 * 1_048_576
_POLICY_PROVENANCE = "synthetic_policy"
_POLICY_TRACE_SEMANTICS = "scenario_replay_not_production_audit"

_EVENT_BY_NAME: Mapping[str, type[EventEnvelope]] = {
    item.__name__: item for item in EVENT_TYPES
}
_EFFECT_BY_NAME: Mapping[str, type[object]] = {
    item.__name__: item for item in EFFECT_TYPES
}
_WOULD_EFFECT_NAMES: Mapping[str, str] = {
    "WOULD_PROBE": "ActivateProbe",
    "WOULD_APPLY": "ApplyProfile",
    "WOULD_PREPARE": "PrepareDesktop",
    "WOULD_FINALIZE": "FinalizeDesktop",
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
    provenance: str | None = None
    trace_semantics: str | None = None

    def __post_init__(self) -> None:
        """Require the closed policy label pair when replay metadata is present."""
        labels = (self.provenance, self.trace_semantics)
        if labels == (None, None):
            return
        if labels != (_POLICY_PROVENANCE, _POLICY_TRACE_SEMANTICS):
            msg = "replay provenance and trace semantics must use the policy pair"
            raise ValueError(msg)

    @property
    def events(self) -> tuple[Event, ...]:
        """Return the ordered reducer inputs."""
        return tuple(step.event for step in self.steps)


def capture_replay(
    initial_state: State,
    events: Iterable[Event],
    *,
    provenance: str | None = None,
    trace_semantics: str | None = None,
) -> ReplayTrace:
    """Reduce *events* once and retain every complete expected decision."""
    state = initial_state
    steps: list[ReplayStep] = []
    for event in events:
        decision = reduce(state, event)
        steps.append(ReplayStep(event, decision))
        state = decision.state
    return ReplayTrace(
        initial_state,
        tuple(steps),
        provenance=provenance,
        trace_semantics=trace_semantics,
    )


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
    _validate_state_for_replay(trace.initial_state, "initial_state")
    for index, step in enumerate(trace.steps, start=1):
        _validate_state_for_replay(step.expected.state, f"record {index}.state")
    header: dict[str, object] = {
        "record": "header",
        "schema_version": REPLAY_SCHEMA_VERSION,
        "initial_state": encode_schema_value(trace.initial_state),
    }
    if trace.provenance is not None and trace.trace_semantics is not None:
        header["provenance"] = trace.provenance
        header["trace_semantics"] = trace.trace_semantics
    records: list[dict[str, object]] = [header]
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
    base_header_fields = frozenset({"record", "schema_version", "initial_state"})
    policy_header_fields = base_header_fields | {"provenance", "trace_semantics"}
    if frozenset(header) not in {base_header_fields, policy_header_fields}:
        _exact_fields(header, base_header_fields, "header")
    if header["record"] != "header":
        raise ReplayFormatError("first replay record must be a header")
    if header["schema_version"] != REPLAY_SCHEMA_VERSION:
        raise ReplayFormatError(
            f"replay schema_version must be {REPLAY_SCHEMA_VERSION}"
        )
    initial = _decode_replay_state(header["initial_state"], "initial_state")
    provenance = cast("str | None", header.get("provenance"))
    trace_semantics = cast("str | None", header.get("trace_semantics"))
    steps: list[ReplayStep] = []
    state = initial
    for index, record in enumerate(records[1:], start=1):
        record_kind = record.get("record")
        if record_kind in {"would_dispatch", "runtime_failure"}:
            _validate_annotation(record, index)
            continue
        step = _decode_step(record, index)
        _validate_audit_metadata(record.get("audit"), state, step, index)
        steps.append(step)
        state = step.expected.state
    try:
        return ReplayTrace(
            initial,
            tuple(steps),
            provenance=provenance,
            trace_semantics=trace_semantics,
        )
    except ValueError as error:
        raise ReplayFormatError(f"invalid replay header metadata: {error}") from error


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
    base_fields = frozenset({"record", "event_type", "event", "state", "effects"})
    actual_fields = frozenset(record)
    if actual_fields not in {base_fields, base_fields | {"audit"}}:
        _exact_fields(record, base_fields, where)
    if record["record"] != "decision":
        raise ReplayFormatError(f"{where} must be a decision")
    event_type = _named_type(record["event_type"], _EVENT_BY_NAME, f"{where}.event")
    event = decode_schema_value(record["event"], event_type)
    if not isinstance(event, EVENT_TYPES):
        raise ReplayFormatError(f"{where}.event is outside the closed Event union")
    state = _decode_replay_state(record["state"], f"{where}.state")
    effects_data = record["effects"]
    if not isinstance(effects_data, list):
        raise ReplayFormatError(f"{where}.effects must be an array")
    effects = tuple(
        _decode_effect(item, f"{where}.effects[{effect_index}]")
        for effect_index, item in enumerate(cast("list[object]", effects_data))
    )
    return ReplayStep(cast("Event", event), Decision(state, effects))


def _validate_state_for_replay(state: State, where: str) -> None:
    try:
        validate_state(state)
    except StateCodecError as error:
        raise ReplayFormatError(f"{where} is invalid: {error}") from error


def _decode_replay_state(value: object, where: str) -> State:
    try:
        decoded = decode_schema_value(value, State)
        if not isinstance(decoded, State):
            raise ReplayFormatError(f"{where} did not decode as State")
        _validate_state_for_replay(decoded, where)
    except StateCodecError as error:
        raise ReplayFormatError(f"{where} is invalid: {error}") from error
    return decoded


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


def _validate_audit_metadata(
    value: object,
    prior_state: State,
    step: ReplayStep,
    index: int,
) -> None:
    if value is None:
        return
    where = f"record {index}.audit"
    if not isinstance(value, dict):
        raise ReplayFormatError(f"{where} must be an object")
    audit = cast("dict[str, object]", value)
    _exact_fields(
        audit,
        frozenset(
            {
                "monotonic_ms",
                "wake_reason",
                "prior_state_key",
                "resulting_state_key",
                "timing",
            }
        ),
        where,
    )
    for field in ("monotonic_ms",):
        _nonnegative_integer(audit[field], f"{where}.{field}")
    for field in ("wake_reason", "prior_state_key", "resulting_state_key"):
        if not isinstance(audit[field], str) or not audit[field]:
            raise ReplayFormatError(f"{where}.{field} must be a non-empty string")
    if audit["monotonic_ms"] != step.event.metadata.processed_at_ms:
        raise ReplayFormatError(
            f"{where}.monotonic_ms does not match event processing time"
        )
    expected_wake_reason = _expected_wake_reason(step)
    if audit["wake_reason"] != expected_wake_reason:
        raise ReplayFormatError(
            f"{where}.wake_reason does not match its event and observation request"
        )
    expected_prior = sha256(encode_state(prior_state)).hexdigest()
    expected_result = sha256(encode_state(step.expected.state)).hexdigest()
    if audit["prior_state_key"] != expected_prior:
        raise ReplayFormatError(f"{where}.prior_state_key does not match replay state")
    if audit["resulting_state_key"] != expected_result:
        raise ReplayFormatError(f"{where}.resulting_state_key does not match decision")
    _validate_audit_timing(audit["timing"], where)


def _expected_wake_reason(step: ReplayStep) -> str:
    request = next(
        (
            effect
            for effect in step.expected.effects
            if isinstance(effect, RequestObservation)
        ),
        None,
    )
    return request.reason.value if request is not None else type(step.event).__name__


def _validate_audit_timing(value: object, audit_where: str) -> None:
    where = f"{audit_where}.timing"
    if not isinstance(value, dict):
        raise ReplayFormatError(f"{where} must be an object")
    timing = cast("dict[str, object]", value)
    fields = frozenset(
        {
            "processing_started_ms",
            "reduction_finished_ms",
            "persistence_finished_ms",
            "observation_duration_ms",
            "command_duration_ms",
            "worker_duration_ms",
        }
    )
    _exact_fields(timing, fields, where)
    boundaries = tuple(
        _nonnegative_integer(timing[field], f"{where}.{field}")
        for field in (
            "processing_started_ms",
            "reduction_finished_ms",
            "persistence_finished_ms",
        )
    )
    if boundaries != tuple(sorted(boundaries)):
        raise ReplayFormatError(f"{where} boundaries are not ordered")
    for field in (
        "observation_duration_ms",
        "command_duration_ms",
        "worker_duration_ms",
    ):
        duration = timing[field]
        if duration is not None:
            _nonnegative_integer(duration, f"{where}.{field}")


def _validate_annotation(record: Mapping[str, object], index: int) -> None:
    where = f"record {index}"
    if record.get("record") == "would_dispatch":
        _validate_would_dispatch(record, where)
    else:
        _validate_runtime_failure(record, where)


def _validate_would_dispatch(record: Mapping[str, object], where: str) -> None:
    _exact_fields(
        record,
        frozenset(
            {
                "record",
                "schema_version",
                "kind",
                "action_id",
                "effect_type",
                "effect",
                "recorded_at_ms",
            }
        ),
        where,
    )
    _validate_annotation_header(record, where)
    kind = record["kind"]
    if not isinstance(kind, str) or kind not in _WOULD_EFFECT_NAMES:
        raise ReplayFormatError(f"{where}.kind is not a known WOULD_* value")
    if not isinstance(record["action_id"], str) or not record["action_id"]:
        raise ReplayFormatError(f"{where}.action_id must be a non-empty string")
    effect = _decode_effect(
        {
            "effect_type": record["effect_type"],
            "effect": record["effect"],
        },
        f"{where}.effect",
    )
    if type(effect).__name__ != _WOULD_EFFECT_NAMES[kind]:
        raise ReplayFormatError(f"{where}.kind does not match its decoded effect")
    effect_action_id = getattr(effect, "action_id", None)
    if record["action_id"] != getattr(effect_action_id, "value", None):
        raise ReplayFormatError(f"{where}.action_id does not match its decoded effect")


def _validate_runtime_failure(record: Mapping[str, object], where: str) -> None:
    _exact_fields(
        record,
        frozenset(
            {
                "record",
                "schema_version",
                "boundary",
                "detail",
                "action_id",
                "recorded_at_ms",
            }
        ),
        where,
    )
    _validate_annotation_header(record, where)
    for field in ("boundary", "detail"):
        if not isinstance(record[field], str) or not record[field]:
            raise ReplayFormatError(f"{where}.{field} must be a non-empty string")
    if record["action_id"] is not None and not isinstance(record["action_id"], str):
        raise ReplayFormatError(f"{where}.action_id must be null or a string")


def _validate_annotation_header(record: Mapping[str, object], where: str) -> None:
    if record["schema_version"] != REPLAY_SCHEMA_VERSION:
        raise ReplayFormatError(f"{where} has an unsupported schema version")
    _nonnegative_integer(record["recorded_at_ms"], f"{where}.recorded_at_ms")


def _nonnegative_integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayFormatError(f"{where} must be a non-negative integer")
    return value


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
