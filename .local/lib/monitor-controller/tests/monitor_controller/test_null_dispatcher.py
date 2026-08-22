"""Null-dispatch structure, audit rotation, and replay-integrity tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActivateProbe,
    ApplicationAttemptKey,
    ApplyProfile,
    BootId,
    ControllerInstanceId,
    Decision,
    DisplayIdentity,
    EventGeneration,
    EventMetadata,
    FinalizeDesktop,
    MappingProof,
    ObservationKey,
    OutputMapping,
    PlanHash,
    PrepareDesktop,
    ProbeAttemptKey,
    Schedule,
    State,
    TimerFired,
    TransitionId,
    TransitionKey,
)
from monitor_controller.reducer import reduce
from monitor_controller.runtime.audit import DecisionAuditTiming, RotatingAuditLog
from monitor_controller.runtime.dispatcher import NullDispatcher, WouldDispatchKind
from monitor_controller.simulation.replay import (
    MAX_REPLAY_BYTES,
    ReplayFormatError,
    ReplayMismatchError,
    ReplayStep,
    ReplayTrace,
    capture_replay,
    decode_replay,
    encode_replay,
    replay,
)

_BOOT = BootId(UUID(int=601))
_INSTANCE = ControllerInstanceId(UUID(int=602))
_KEY = ObservationKey("null-observation")


def _state() -> State:
    return State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":null"),
    )


def _effects() -> tuple[
    ActivateProbe,
    ApplyProfile,
    PrepareDesktop,
    FinalizeDesktop,
]:
    probe_id = ActionId(_INSTANCE, ActionKind.PROBE, 1)
    apply_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 2)
    prepare_id = ActionId(_INSTANCE, ActionKind.PREPARATION, 3)
    finalize_id = ActionId(_INSTANCE, ActionKind.FINALIZATION, 4)
    transition_id = TransitionId(_INSTANCE, 1)
    transition_key = TransitionKey("transition")
    plan_hash = PlanHash("plan")
    mapping = MappingProof(
        "dock",
        1,
        _KEY,
        (OutputMapping("DP-SAVED", "DP-1"),),
    )
    return (
        ActivateProbe(
            probe_id,
            ProbeAttemptKey(1, "dock", _KEY),
            "DP-1",
            "eDP-1",
            "3840x2160",
            EventGeneration(7),
            _KEY,
        ),
        ApplyProfile(
            apply_id,
            ApplicationAttemptKey(1, "dock", _KEY),
            "dock",
            mapping,
            EventGeneration(7),
            _KEY,
        ),
        PrepareDesktop(
            prepare_id,
            transition_id,
            transition_key,
            "dock",
            plan_hash,
            EventGeneration(7),
            _KEY,
        ),
        FinalizeDesktop(
            finalize_id,
            transition_id,
            transition_key,
            "dock",
            plan_hash,
            EventGeneration(7),
            _KEY,
        ),
    )


def test_null_dispatcher_has_no_transaction_supervisor_or_start_surface() -> None:
    dispatcher = NullDispatcher()

    assert not hasattr(dispatcher, "write_request")
    assert not hasattr(dispatcher, "start")
    assert not hasattr(dispatcher, "stop")
    assert not hasattr(dispatcher, "query")
    assert not hasattr(dispatcher, "supervisor")
    assert not hasattr(dispatcher, "transaction")


def test_every_worker_effect_becomes_only_a_would_audit_record(tmp_path: Path) -> None:
    dispatcher = NullDispatcher()
    audit = RotatingAuditLog(tmp_path / "shadow.jsonl", _state())

    for index, effect in enumerate(_effects()):
        audit.append_would_dispatch(dispatcher.record(effect, index))

    assert tuple(item.kind for item in dispatcher.records) == tuple(WouldDispatchKind)
    trace = decode_replay(audit.path.read_bytes())
    assert trace.initial_state == _state()
    assert not trace.steps
    assert not replay(trace)
    text = audit.path.read_text()
    assert all(kind.value in text for kind in WouldDispatchKind)
    assert "systemctl" not in text
    assert "request.json" not in text


def test_rotating_audit_is_size_and_count_bounded_and_each_segment_replays(
    tmp_path: Path,
) -> None:
    state = _state()
    audit = RotatingAuditLog(
        tmp_path / "audit.jsonl",
        state,
        max_bytes=8_000,
        max_files=3,
    )
    for index in range(20):
        event = TimerFired(EventMetadata(index, _BOOT), index)
        decision = reduce(state, event)
        audit.append_decision(
            state,
            event,
            decision,
            DecisionAuditTiming(index, index, index),
        )
        state = decision.state

    paths = audit.retained_paths
    assert len(paths) == 3
    assert all(path.stat().st_size <= 8_000 for path in paths)
    for path in paths:
        trace = decode_replay(path.read_bytes())
        replay(trace)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("action_id", "application-wrong-1", "action_id"),
        ("kind", "WOULD_APPLY", "kind"),
    ],
)
def test_would_dispatch_annotation_must_match_decoded_effect(
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    audit = RotatingAuditLog(tmp_path / "shadow.jsonl", _state())
    effect = _effects()[0]
    audit.append_would_dispatch(NullDispatcher().record(effect, 0))
    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    records[1][field] = replacement
    encoded = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )

    with pytest.raises(ReplayFormatError, match=message):
        decode_replay(encoded)


def test_audit_rejects_segment_limit_above_replay_bound(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=str(MAX_REPLAY_BYTES)):
        RotatingAuditLog(
            tmp_path / "audit.jsonl",
            _state(),
            max_bytes=MAX_REPLAY_BYTES + 1,
        )


def test_audit_cleans_lowered_suffixes_and_preexisting_oversized_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"x" * 8_001)
    path.with_name("audit.jsonl.1").write_bytes(b"x" * 8_001)
    path.with_name("audit.jsonl.01").write_text("noncanonical")
    path.with_name("audit.jsonl.2").write_text("obsolete")
    path.with_name("audit.jsonl.9").write_text("obsolete")

    audit = RotatingAuditLog(path, _state(), max_bytes=8_000, max_files=2)

    assert audit.retained_paths == (path,)
    assert path.stat().st_size <= 8_000
    assert not path.with_name("audit.jsonl.01").exists()
    assert not path.with_name("audit.jsonl.1").exists()
    assert not path.with_name("audit.jsonl.2").exists()
    assert not path.with_name("audit.jsonl.9").exists()
    replay(decode_replay(path.read_bytes()))


def test_audit_cleanup_removes_truncated_and_non_replayable_segments(
    tmp_path: Path,
) -> None:
    state = _state()
    valid_source = tmp_path / "valid-source.jsonl"
    RotatingAuditLog(valid_source, state)
    valid_segment = valid_source.read_bytes()
    valid_source.unlink()

    event = TimerFired(EventMetadata(1, _BOOT), 1)
    non_replayable = encode_replay(
        ReplayTrace(
            state,
            (ReplayStep(event, Decision(state, (Schedule(1),))),),
        )
    )
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b'{"record":"header"')
    path.with_name("audit.jsonl.1").write_bytes(non_replayable)
    path.with_name("audit.jsonl.2").write_bytes(valid_segment)
    path.with_name("audit.jsonl.3").write_bytes(valid_segment[:-10])

    audit = RotatingAuditLog(path, state, max_files=4)

    assert path.with_name("audit.jsonl.1").exists() is False
    assert path.with_name("audit.jsonl.2").exists()
    assert path.with_name("audit.jsonl.3").exists() is False
    assert audit.retained_paths == (path, path.with_name("audit.jsonl.2"))
    for retained in audit.retained_paths:
        replay(decode_replay(retained.read_bytes()))


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("monotonic_ms", 6, "monotonic_ms"),
        ("wake_reason", "TimerFired", "wake_reason"),
    ],
)
def test_replay_validates_audit_time_and_wake_reason_against_decision(
    tmp_path: Path,
    field: str,
    replacement: object,
    message: str,
) -> None:
    state = replace(_state(), next_timer_ms=5)
    event = TimerFired(EventMetadata(5, _BOOT), 5)
    decision = reduce(state, event)
    audit = RotatingAuditLog(tmp_path / "audit.jsonl", state)
    audit.append_decision(
        state,
        event,
        decision,
        DecisionAuditTiming(5, 5, 5),
    )
    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    records[1]["audit"][field] = replacement
    encoded = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )

    with pytest.raises(ReplayFormatError, match=message):
        decode_replay(encoded)


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(None, id="header-only"),
        pytest.param(
            {
                "record": "runtime_failure",
                "schema_version": 1,
                "boundary": "test",
                "detail": "annotation only",
                "action_id": None,
                "recorded_at_ms": 0,
            },
            id="annotation-only",
        ),
    ],
)
def test_replay_rejects_invalid_state_without_any_decision_records(
    annotation: dict[str, object] | None,
) -> None:
    records = [
        json.loads(line)
        for line in encode_replay(ReplayTrace(_state(), ())).decode().splitlines()
    ]
    initial_state = records[0]["initial_state"]
    assert isinstance(initial_state, dict)
    initial_state["phase"] = "probe_pending"
    if annotation is not None:
        records.append(annotation)
    encoded = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )

    with pytest.raises(ReplayFormatError, match="relationships are invalid"):
        decode_replay(encoded)


def test_production_reducer_replay_detects_a_changed_decision() -> None:
    state = _state()
    event = TimerFired(EventMetadata(1, _BOOT), 1)
    captured = capture_replay(state, (event,))
    changed = ReplayTrace(
        state,
        (
            ReplayStep(
                event,
                Decision(captured.steps[0].expected.state, (Schedule(1),)),
            ),
        ),
    )

    with pytest.raises(ReplayMismatchError, match="replay mismatch"):
        replay(changed)


def test_null_records_are_plain_data_without_async_tasks() -> None:
    async def exercise() -> None:
        before = set(asyncio.all_tasks())
        dispatcher = NullDispatcher()
        for effect in _effects():
            dispatcher.record(effect, 0)
        after = set(asyncio.all_tasks())

        assert after == before

    asyncio.run(exercise())
