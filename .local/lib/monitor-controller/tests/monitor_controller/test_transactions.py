"""Immutable transaction codec and common worker-boundary tests."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EventGeneration,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    TransitionId,
    TransitionKey,
)
from monitor_controller.runtime.transactions import (
    ExpectedTopology,
    ImmutableTransactionError,
    TransactionArtifact,
    TransactionProtocolError,
    TransactionRequest,
    TransactionResult,
    TransactionStore,
    decode_request,
    decode_result,
    encode_request,
    encode_result,
    with_request_hash,
)
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerExecution,
    WorkerStartupError,
    execute_worker,
    record_systemd_result,
    reject_unimplemented,
    validate_topology_guard,
    validate_worker_startup,
    write_worker_result,
)

_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_ACTION = ActionId(_INSTANCE, ActionKind.PREPARATION, 17)
_TRANSITION = TransitionId(_INSTANCE, 9)
_TOPOLOGY = ExpectedTopology(
    kernel_connected_outputs=("DP-1", "eDP-1"),
    kernel_external_outputs=("DP-1",),
    x_connected_outputs=("DP-1", "eDP-1"),
    x_active_outputs=("DP-1", "eDP-1"),
)
_MAPPING = (
    OutputMapping("DP-SAVED", "DP-1"),
    OutputMapping("eDP-SAVED", "eDP-1"),
)
_INSTALLATION_BOUNDARIES = (
    "temporary_directory_created",
    "request_installed",
    "prepared_installed",
    "bundle_synced",
    "bundle_published",
    "parent_synced",
)


class _InjectedCrashError(BaseException):
    """Simulate process death without running ordinary exception cleanup."""


def _request(*, unit_name: str = "monitor-prepare@test.service") -> TransactionRequest:
    return TransactionRequest(
        action_id=_ACTION,
        action_kind=ActionKind.PREPARATION,
        unit_name=unit_name,
        physical_epoch=4,
        physical_token=PhysicalToken("physical-proof"),
        admitted_event_generation=EventGeneration(11),
        observation_key=ObservationKey("observation-proof"),
        output_mapping=_MAPPING,
        expected_topology=_TOPOLOGY,
        profile="dock",
        layout="wide",
        transition_id=_TRANSITION,
        transition_key=TransitionKey("transition-proof"),
        plan_hash=PlanHash("sha256:plan"),
        payload=(("test_behavior", "success"),),
    )


def _result(request: TransactionRequest) -> TransactionResult:
    hashed = with_request_hash(request)
    return TransactionResult(
        action_id=hashed.action_id,
        action_kind=hashed.action_kind,
        unit_name=hashed.unit_name,
        request_sha256=hashed.request_sha256,
        outcome=ActionLifecycle.COMPLETED,
        exit_status=0,
        started_monotonic_ms=10,
        finished_monotonic_ms=12,
        detail="contract succeeded",
        plan_hash=hashed.plan_hash,
    )


def test_request_and_result_round_trip_every_identity_and_proof_field() -> None:
    request = with_request_hash(_request())
    result = _result(request)

    decoded_request = decode_request(encode_request(request))
    decoded_result = decode_result(encode_result(result))

    assert decoded_request == request
    assert decoded_request.request_sha256.startswith("sha256:")
    assert len(decoded_request.request_sha256) == len("sha256:") + 64
    assert decoded_request.action_id == _ACTION
    assert decoded_request.unit_name == "monitor-prepare@test.service"
    assert decoded_request.physical_epoch == 4
    assert decoded_request.physical_token == PhysicalToken("physical-proof")
    assert decoded_request.admitted_event_generation == EventGeneration(11)
    assert decoded_request.observation_key == ObservationKey("observation-proof")
    assert decoded_request.output_mapping == _MAPPING
    assert decoded_request.expected_topology == _TOPOLOGY
    assert decoded_request.transition_id == _TRANSITION
    assert decoded_request.transition_key == TransitionKey("transition-proof")
    assert decoded_request.plan_hash == PlanHash("sha256:plan")
    assert decoded_result.request_sha256 == request.request_sha256
    assert decoded_result.result_sha256.startswith("sha256:")


@pytest.mark.parametrize("mutation", ["hash", "unit", "unknown", "duplicate"])
def test_request_codec_rejects_tampering_unknown_and_duplicate_fields(
    mutation: str,
) -> None:
    encoded = encode_request(_request())
    raw = json.loads(encoded)
    if mutation == "hash":
        raw["request_sha256"] = "sha256:" + "0" * 64
        payload = json.dumps(raw).encode()
    elif mutation == "unit":
        raw["unit_name"] = "monitor-finalize@substitution.service"
        payload = json.dumps(raw).encode()
    elif mutation == "unknown":
        raw["authority"] = True
        payload = json.dumps(raw).encode()
    else:
        text = encoded.decode()
        payload = text.replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
        ).encode()

    with pytest.raises(TransactionProtocolError):
        decode_request(payload)


@pytest.mark.parametrize("boundary", _INSTALLATION_BOUNDARIES)
def test_request_prepared_bundle_is_retryable_across_every_crash_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / boundary / "transactions"

    def crash_at_boundary(reached: str) -> None:
        if reached == boundary:
            raise _InjectedCrashError

    interrupted = TransactionStore(root, installation_fault=crash_at_boundary)
    with pytest.raises(_InjectedCrashError):
        interrupted.create_request(_request())
    interrupted.close()

    visible = root / _ACTION.value
    if visible.exists():
        assert sorted(item.name for item in visible.iterdir()) == [
            "prepared.json",
            "request.json",
        ]
    restarted = TransactionStore(root)
    request = restarted.create_request(_request())

    assert restarted.read_request(_ACTION) == request
    assert restarted.read_prepared(_ACTION).request_sha256 == request.request_sha256
    assert restarted.submission_claim_if_present(_ACTION) is None
    assert restarted.execution_claim_if_present(_ACTION) is None
    assert restarted.result_if_present(_ACTION) is None
    assert restarted.action_directories() == (restarted.action_directory(_ACTION),)


@pytest.mark.parametrize(
    "relative_path",
    [
        "./artifacts/profile/config",
        "artifacts//profile/config",
        "artifacts/profile/config/",
    ],
)
def test_noncanonical_artifact_paths_fail_before_request_publication(
    tmp_path: Path,
    relative_path: str,
) -> None:
    with pytest.raises(TransactionProtocolError, match="canonical"):
        TransactionArtifact(relative_path, b"content")

    # Revalidate at the store boundary as well, even if an injected fault bypasses
    # the frozen dataclass constructor.
    malformed = object.__new__(TransactionArtifact)
    object.__setattr__(malformed, "relative_path", relative_path)
    object.__setattr__(malformed, "content", b"content")
    object.__setattr__(malformed, "executable", False)
    store = TransactionStore(tmp_path / "transactions")
    with pytest.raises(TransactionProtocolError, match="canonical"):
        store.create_request(_request(), (malformed,))
    assert not store.action_directory(_ACTION).exists()


def test_store_atomically_installs_modes_and_never_replaces_evidence(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "runtime" / "transactions")
    request = store.create_request(_request())
    request_path = store.action_directory(_ACTION) / "request.json"

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(request_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert request_path.stat().st_nlink == 1
    assert store.create_request(request) == request
    with pytest.raises(ImmutableTransactionError):
        store.create_request(replace(request, layout="other", request_sha256=""))

    store.claim_submission(request.action_id)
    store.claim_execution(request.action_id)
    result = store.write_result(_result(request))
    assert (
        stat.S_IMODE((store.action_directory(_ACTION) / "result.json").stat().st_mode)
        == 0o600
    )
    assert store.write_result(result) == result
    with pytest.raises(ImmutableTransactionError):
        store.write_result(replace(result, detail="substituted", result_sha256=""))
    with pytest.raises(ImmutableTransactionError):
        store.discard_unacknowledged(request)


def test_store_refuses_symlinked_request_and_cross_unit_result(tmp_path: Path) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    request_path = store.action_directory(_ACTION) / "request.json"
    saved = request_path.read_bytes()
    request_path.unlink()
    target = tmp_path / "outside.json"
    target.write_bytes(saved)
    target.chmod(0o600)
    request_path.symlink_to(target)

    with pytest.raises(TransactionProtocolError):
        store.read_request(_ACTION)

    request_path.unlink()
    request_path.write_bytes(saved)
    request_path.chmod(0o600)
    with pytest.raises(TransactionProtocolError):
        store.write_result(
            replace(_result(request), unit_name="monitor-prepare@other.service")
        )


def test_execution_claim_read_requires_its_submission_predecessor(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    store.claim_execution(request.action_id)
    submission_path = (
        store.action_directory(request.action_id) / "submission-claim.json"
    )
    submission_path.unlink()

    with pytest.raises(TransactionProtocolError, match="manager-submission claim"):
        store.execution_claim_if_present(request.action_id)


def test_stop_and_completed_result_require_durable_execution_evidence(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())

    with pytest.raises(TransactionProtocolError, match="manager-submission claim"):
        store.create_stop_intent(request.action_id, ActionLifecycle.CANCELLED)
    with pytest.raises(TransactionProtocolError, match="manager-submission claim"):
        store.write_result(_result(request))

    store.claim_submission(request.action_id)
    store.create_stop_intent(request.action_id, ActionLifecycle.CANCELLED)
    with pytest.raises(TransactionProtocolError, match="execution claim"):
        store.write_result(_result(request))

    store.claim_execution(request.action_id)
    stored = store.write_result(_result(request))
    execution_path = store.action_directory(request.action_id) / "execution-claim.json"
    execution_path.unlink()
    with pytest.raises(TransactionProtocolError, match="execution claim"):
        store.result_if_present(request.action_id)
    assert stored.action_id == request.action_id


def test_common_startup_and_topology_guard_require_exact_action_and_unit(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )

    validate_topology_guard(
        request,
        CurrentTopology(request.physical_token, request.expected_topology),
    )
    with pytest.raises(WorkerStartupError):
        validate_topology_guard(
            request,
            CurrentTopology(PhysicalToken("changed"), request.expected_topology),
        )
    with pytest.raises(WorkerStartupError):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=request.action_id.value,
            unit_name="monitor-prepare@substitution.service",
            expected_kind=ActionKind.PREPARATION,
        )
    assert startup.request == request
    assert startup.execution_claim == store.read_execution_claim(request.action_id)


def test_execution_claim_precedes_topology_read_and_worker_implementation(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    calls: list[str] = []

    def topology_reader(item: TransactionRequest) -> CurrentTopology:
        assert store.execution_claim_if_present(item.action_id) is not None
        calls.append("topology")
        return CurrentTopology(item.physical_token, item.expected_topology)

    def implementation(item: TransactionRequest) -> WorkerExecution:
        assert store.execution_claim_if_present(item.action_id) is not None
        calls.append("implementation")
        return WorkerExecution(ActionLifecycle.COMPLETED, 0, "claimed before mutation")

    assert (
        execute_worker(
            startup,
            topology_reader=topology_reader,
            implementation=implementation,
        )
        == 0
    )
    assert calls == ["topology", "implementation"]


def test_unimplemented_worker_cannot_succeed_and_result_is_request_bound(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )

    assert reject_unimplemented(startup) != 0
    result = store.read_result(request.action_id)
    assert result.outcome is ActionLifecycle.FAILED
    assert result.exit_status != 0
    assert result.action_id == request.action_id
    assert result.unit_name == request.unit_name
    assert result.request_sha256 == request.request_sha256
    assert result.plan_hash == request.plan_hash


def test_result_writer_preserves_first_atomic_terminal_report(tmp_path: Path) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    first = write_worker_result(
        startup,
        execution=WorkerExecution(ActionLifecycle.FAILED, 12, "first"),
        started_monotonic_ms=1,
        finished_monotonic_ms=2,
    )

    assert store.read_result(request.action_id) == first
    with pytest.raises(ImmutableTransactionError):
        write_worker_result(
            startup,
            execution=WorkerExecution(ActionLifecycle.FAILED, 13, "late"),
            started_monotonic_ms=3,
            finished_monotonic_ms=4,
        )
    assert not any(path.name.endswith(".tmp") for path in request_path_entries(store))


def test_worker_execution_claim_rejects_every_repeat_after_terminal_result(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    store.write_result(_result(request))

    assert startup.execution_claim is not None
    with pytest.raises(WorkerStartupError, match="already claimed"):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=request.action_id.value,
            unit_name=request.unit_name,
            expected_kind=ActionKind.PREPARATION,
        )


@pytest.mark.parametrize(
    ("intent", "service_result", "exit_status", "expected"),
    [
        (None, "timeout", "KILL", ActionLifecycle.TIMED_OUT),
        (ActionLifecycle.TIMED_OUT, "signal", "TERM", ActionLifecycle.TIMED_OUT),
        (ActionLifecycle.UNKNOWN, "signal", "TERM", ActionLifecycle.UNKNOWN),
        (ActionLifecycle.CANCELLED, "timeout", "KILL", ActionLifecycle.TIMED_OUT),
        (None, "exit-code", "23", ActionLifecycle.FAILED),
        (None, "success", "0", ActionLifecycle.UNKNOWN),
    ],
)
def test_exec_stop_post_reconstructs_every_exact_terminal_semantic(
    tmp_path: Path,
    intent: ActionLifecycle | None,
    service_result: str,
    exit_status: str,
    expected: ActionLifecycle,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    store.claim_submission(request.action_id)
    if intent is not None:
        store.create_stop_intent(request.action_id, intent)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
        acquire_execution_claim=False,
    )

    assert (
        record_systemd_result(
            startup,
            service_result=service_result,
            exit_code="killed" if service_result != "exit-code" else "exited",
            exit_status=exit_status,
        )
        == 0
    )
    result = store.read_result(request.action_id)
    assert result.outcome is expected
    assert result.request_sha256 == request.request_sha256


def test_transaction_root_rejects_symlinked_parent_components(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    store = TransactionStore(linked_parent / "transactions")

    with pytest.raises(TransactionProtocolError, match="safely opened"):
        store.create_request(_request())
    assert not (outside / "transactions").exists()


def test_retained_directory_fds_defeat_parent_replacement_race(tmp_path: Path) -> None:
    root = tmp_path / "transactions"
    store = TransactionStore(root)
    request = store.create_request(_request())
    moved = tmp_path / "transactions-original"
    root.rename(moved)
    root.mkdir(mode=0o700)
    replacement_action = root / request.action_id.value
    replacement_action.mkdir(mode=0o700)

    assert store.read_request(request.action_id) == request
    store.claim_submission(request.action_id)
    store.claim_execution(request.action_id)
    assert (moved / request.action_id.value / "execution-claim.json").is_file()
    assert not (replacement_action / "execution-claim.json").exists()


def test_prepared_digest_rejects_self_consistent_request_rewrite(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    request_path = store.action_directory(request.action_id) / "request.json"
    rewritten = replace(request, layout="attacker-layout", request_sha256="")
    request_path.unlink()
    request_path.write_bytes(encode_request(rewritten))
    request_path.chmod(0o600)

    with pytest.raises(TransactionProtocolError, match="bound record"):
        store.read_request(request.action_id)


def test_final_file_mode_and_link_count_changes_are_rejected(tmp_path: Path) -> None:
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(_request())
    prepared_path = store.action_directory(request.action_id) / "prepared.json"
    prepared_path.chmod(0o644)
    with pytest.raises(TransactionProtocolError, match="unsafe metadata"):
        store.read_request(request.action_id)

    prepared_path.chmod(0o600)
    hardlink = tmp_path / "prepared-hardlink.json"
    hardlink.hardlink_to(prepared_path)
    with pytest.raises(TransactionProtocolError, match="unsafe metadata"):
        store.read_request(request.action_id)


def request_path_entries(store: TransactionStore) -> tuple[Path, ...]:
    """Return transaction entries for the atomic-temp cleanup assertion."""
    return tuple(store.action_directory(_ACTION).iterdir())
