"""Argument-array systemd supervisor and dispatcher contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    ActivateProbe,
    ApplicationAttemptKey,
    ApplyProfile,
    ControllerInstanceId,
    EdidIntegrity,
    EventGeneration,
    MappingProof,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    ProbeAttemptKey,
    WorkerUnit,
)
from monitor_controller.runtime.dispatcher import (
    DispatchAdapterError,
    DispatchStartResult,
    WorkerActivity,
    WorkerRequestContext,
)
from monitor_controller.runtime.persistence import StateNamespace
from monitor_controller.runtime.systemd import (
    SystemctlCommandResult,
    SystemdCommandRejectedError,
    SystemdDispatcher,
    SystemdRecoveryScanner,
    SystemdSupervisor,
    SystemdSupervisorError,
    escape_unit_instance,
)
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    BoundTransactionRecord,
    ExpectedTopology,
    TransactionProtocolError,
    TransactionResult,
    TransactionStore,
    with_bound_record_hash,
)

_INSTANCE = ControllerInstanceId(UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
_ACTION = ActionId(_INSTANCE, ActionKind.APPLICATION, 7)
_PROBE_ACTION = ActionId(_INSTANCE, ActionKind.PROBE, 8)
_OBSERVATION_KEY = ObservationKey("observation")
_MAPPING = MappingProof(
    "dock",
    3,
    _OBSERVATION_KEY,
    (OutputMapping("DP-SAVED", "DP-1"),),
)
_TOPOLOGY = ExpectedTopology(("DP-1",), ("DP-1",), ("DP-1",), ("DP-1",))


class _SubmissionGuard:
    def __init__(self, unit: WorkerUnit) -> None:
        self.unit = unit
        self.claimed = False

    def __call__(self) -> BoundTransactionRecord:
        if self.claimed:
            msg = "submission already claimed"
            raise RuntimeError(msg)
        self.claimed = True
        return with_bound_record_hash(
            BoundTransactionRecord(
                BoundRecordKind.SUBMISSION_CLAIM,
                self.unit.action_id,
                self.unit.action_id.kind,
                self.unit.unit_name,
                "sha256:" + "1" * 64,
            )
        )


class _Runner:
    def __init__(self, *, start_returncode: int = 0) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.start_returncode = start_returncode
        self.show_output = _show_output()

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> SystemctlCommandResult:
        self.calls.append((arguments, timeout_seconds))
        verb = next(
            item
            for item in ("show", "start", "stop", "reset-failed", "list-units")
            if item in arguments
        )
        if verb == "show":
            return SystemctlCommandResult(arguments, 0, self.show_output, "")
        if verb == "list-units":
            return SystemctlCommandResult(arguments, 0, "", "")
        if verb == "stop":
            self.show_output = _show_output(started_us=100, exited_us=200)
        returncode = self.start_returncode if verb == "start" else 0
        return SystemctlCommandResult(
            arguments,
            returncode,
            "",
            "rejected" if returncode else "",
        )


def _show_output(  # noqa: PLR0913
    *,
    active_state: str = "inactive",
    sub_state: str = "dead",
    job: str = "",
    main_pid: int = 0,
    result: str = "success",
    started_us: int = 0,
    exited_us: int = 0,
    status: int = 0,
    control_group: str = "",
) -> str:
    return "\n".join(
        (
            "LoadState=loaded",
            f"ActiveState={active_state}",
            f"SubState={sub_state}",
            f"Job={job}",
            f"MainPID={main_pid}",
            f"Result={result}",
            f"ExecMainStartTimestampMonotonic={started_us}",
            f"ExecMainExitTimestampMonotonic={exited_us}",
            "ExecMainCode=1",
            f"ExecMainStatus={status}",
            f"ControlGroup={control_group}",
            "",
        )
    )


def _supervisor(runner: _Runner) -> SystemdSupervisor:
    return SystemdSupervisor(systemctl=Path("/usr/bin/systemctl"), runner=runner)


def _effect() -> ApplyProfile:
    return ApplyProfile(
        action_id=_ACTION,
        key=ApplicationAttemptKey(3, "dock", _OBSERVATION_KEY),
        profile="dock",
        mapping=_MAPPING,
        admitted_event_generation=EventGeneration(8),
        observation_key=_OBSERVATION_KEY,
    )


def _context() -> WorkerRequestContext:
    return WorkerRequestContext(
        physical_epoch=3,
        physical_token=PhysicalToken("physical"),
        output_mapping=_MAPPING.outputs,
        expected_topology=_TOPOLOGY,
    )


def test_instance_escape_and_exact_action_template_binding() -> None:
    runner = _Runner()
    supervisor = _supervisor(runner)
    unit = supervisor.unit_for_action(_ACTION)

    assert escape_unit_instance(_ACTION.value) == _ACTION.value.replace("-", r"\x2d")
    assert unit.unit_name == (
        "monitor-apply@" + _ACTION.value.replace("-", r"\x2d") + ".service"
    )
    with pytest.raises(SystemdSupervisorError):
        supervisor.validate_unit(replace(unit, unit_name="monitor-probe@wrong.service"))


def test_final_fence_false_makes_no_start_call_and_true_is_immediately_submitted() -> (
    None
):
    runner = _Runner()
    supervisor = _supervisor(runner)
    unit = supervisor.unit_for_action(_ACTION)

    guard = _SubmissionGuard(unit)
    assert supervisor.start(unit, lambda: False, guard) is (
        DispatchStartResult.FENCE_REJECTED
    )
    assert len(runner.calls) == 1
    assert "show" in runner.calls[0][0]

    fence_observed_call_count: list[int] = []

    def fence() -> bool:
        fence_observed_call_count.append(len(runner.calls))
        return True

    assert supervisor.start(unit, fence, guard) is DispatchStartResult.ACCEPTED
    assert fence_observed_call_count == [2]
    arguments, timeout_seconds = runner.calls[-1]
    assert arguments == (
        "/usr/bin/systemctl",
        "--user",
        "--no-pager",
        "--no-ask-password",
        "start",
        "--no-block",
        unit.unit_name,
    )
    assert timeout_seconds == 10


def test_rejected_start_is_typed_and_argument_array_is_not_retried() -> None:
    runner = _Runner(start_returncode=5)
    supervisor = _supervisor(runner)

    unit = supervisor.unit_for_action(_ACTION)
    with pytest.raises(SystemdCommandRejectedError, match="rejected"):
        supervisor.start(
            unit,
            lambda: True,
            _SubmissionGuard(unit),
        )

    assert len(runner.calls) == 2


def test_submission_guard_must_return_an_independently_hashed_exact_claim() -> None:
    runner = _Runner()
    supervisor = _supervisor(runner)
    unit = supervisor.unit_for_action(_ACTION)
    unhashed = BoundTransactionRecord(
        BoundRecordKind.SUBMISSION_CLAIM,
        unit.action_id,
        unit.action_id.kind,
        unit.unit_name,
        "sha256:" + "1" * 64,
    )

    with pytest.raises(SystemdCommandRejectedError, match="differently bound claim"):
        supervisor.start(unit, lambda: True, lambda: unhashed)

    assert len(runner.calls) == 1
    assert "show" in runner.calls[0][0]


def test_query_reattach_and_stop_use_only_exact_keyed_unit() -> None:
    runner = _Runner()
    runner.show_output = _show_output(
        active_state="active",
        sub_state="running",
        main_pid=42,
        started_us=100,
        control_group="/user.slice/test",
    )
    supervisor = _supervisor(runner)
    unit = supervisor.unit_for_action(_ACTION)

    state = supervisor.inspect(unit)
    assert state.activity is WorkerActivity.ACTIVE
    assert supervisor.reattach(unit) == state
    supervisor.stop(unit)

    verbs = [
        next(item for item in ("show", "stop") if item in arguments)
        for arguments, _timeout in runner.calls
    ]
    assert verbs == [
        "show",
        "show",
        "show",
        "stop",
        "show",
    ]
    assert all(call[0][-1] == unit.unit_name for call in runner.calls)
    stop_call = next(call for call in runner.calls if "stop" in call[0])
    assert stop_call[1] == 20


def test_dispatcher_hash_binds_complete_probe_identity_proof(
    tmp_path: Path,
) -> None:
    effect = ActivateProbe(
        action_id=_PROBE_ACTION,
        key=ProbeAttemptKey(3, "dock", _OBSERVATION_KEY),
        output="DP-1",
        internal_output="eDP-1",
        preferred_mode="2560x1440",
        admitted_event_generation=EventGeneration(8),
        observation_key=_OBSERVATION_KEY,
    )
    context = WorkerRequestContext(
        physical_epoch=3,
        physical_token=PhysicalToken("physical"),
        output_mapping=(),
        expected_topology=ExpectedTopology(
            ("DP-1", "eDP-1"),
            ("DP-1",),
            ("DP-1", "eDP-1"),
            ("eDP-1",),
        ),
        probe_base_hash="a" * 64,
        probe_edid_integrity=EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
    )
    store = TransactionStore(tmp_path / "transactions")
    dispatcher = SystemdDispatcher(store, _supervisor(_Runner()))

    prepared = asyncio.run(dispatcher.write_request(effect, context))
    request = store.read_request(prepared.action_id)

    assert request.profile == "dock"
    assert dict(request.payload) == {
        "base_identity_hash": "a" * 64,
        "edid_integrity": "base_valid_extensions_invalid",
        "internal_output": "eDP-1",
        "preferred_mode": "2560x1440",
        "probe_output": "DP-1",
    }
    assert prepared.request_sha256 == request.request_sha256
    with pytest.raises(TransactionProtocolError, match="lacks immutable"):
        asyncio.run(
            dispatcher.write_request(
                effect,
                replace(
                    context,
                    probe_base_hash=None,
                    probe_edid_integrity=None,
                ),
            )
        )


def test_dispatcher_writes_before_start_and_reads_rapid_terminal_result(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        runner = _Runner()
        supervisor = _supervisor(runner)
        store = TransactionStore(tmp_path / "transactions")
        dispatcher = SystemdDispatcher(store, supervisor)
        prepared = await dispatcher.write_request(_effect(), _context())

        assert runner.calls == []
        request = store.read_request(_ACTION)
        assert request.request_sha256 == prepared.request_sha256
        assert request.unit_name == prepared.unit.unit_name
        assert request.physical_epoch == 3
        assert request.expected_topology == _TOPOLOGY
        assert await dispatcher.start(prepared, lambda: True) is (
            DispatchStartResult.ACCEPTED
        )
        store.claim_execution(_ACTION)
        assert [
            next(item for item in ("show", "start") if item in arguments)
            for arguments, _timeout in runner.calls
        ] == ["show", "start"]

        result = TransactionResult(
            action_id=_ACTION,
            action_kind=ActionKind.APPLICATION,
            unit_name=request.unit_name,
            request_sha256=request.request_sha256,
            outcome=ActionLifecycle.COMPLETED,
            exit_status=0,
            started_monotonic_ms=1,
            finished_monotonic_ms=2,
            detail="rapid",
        )
        store.write_result(result)
        runner.show_output = _show_output(started_us=1, exited_us=2)
        assert await dispatcher.worker_activity(prepared.unit) is (
            WorkerActivity.INACTIVE
        )
        completion = await dispatcher.worker_completion(prepared.unit)
        assert completion is not None
        assert completion.action_id == _ACTION
        assert completion.terminal_lifecycle is ActionLifecycle.COMPLETED

    asyncio.run(exercise())


def test_dispatcher_maps_definite_start_rejection_without_unit_substitution(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        runner = _Runner(start_returncode=5)
        store = TransactionStore(tmp_path / "transactions")
        supervisor = _supervisor(runner)
        dispatcher = SystemdDispatcher(store, supervisor)
        prepared = await dispatcher.write_request(_effect(), _context())
        with pytest.raises(DispatchAdapterError) as caught:
            await dispatcher.start(prepared, lambda: True)

        result = store.read_result(prepared.action_id)
        snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)
        assert caught.value.stage.value == "unit_start"
        assert caught.value.completion is not None
        assert caught.value.completion.action_id == result.action_id
        assert caught.value.completion.terminal_lifecycle is result.outcome
        assert caught.value.completion.exit_status == result.exit_status
        assert store.submission_claim_if_present(prepared.action_id) is not None
        assert store.execution_claim_if_present(prepared.action_id) is None
        assert result.outcome is ActionLifecycle.FAILED
        assert result.exit_status != 0
        assert snapshot.ambiguities == ()
        assert snapshot.verified_tombstones == (
            ActionTombstone(prepared.action_id, ActionLifecycle.FAILED),
        )
        assert snapshot.verified_results[0].exit_status == result.exit_status

    asyncio.run(exercise())


def test_recovery_scanner_reattaches_exact_active_transaction(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        runner = _Runner()
        runner.show_output = _show_output(
            active_state="active",
            sub_state="running",
            main_pid=42,
            started_us=100,
            control_group="/user.slice/test",
        )
        supervisor = _supervisor(runner)
        store = TransactionStore(tmp_path / "transactions")
        dispatcher = SystemdDispatcher(store, supervisor)
        prepared = await dispatcher.write_request(_effect(), _context())
        store.claim_submission(_ACTION)
        store.claim_execution(_ACTION)
        snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)

        assert snapshot.units == (prepared.unit,)
        assert snapshot.verified_tombstones == ()
        assert snapshot.action_sequence_high_water == _ACTION.sequence
        assert snapshot.ambiguities == ()

    asyncio.run(exercise())


def test_supervisor_direct_repeat_never_resets_or_restarts_invoked_key() -> None:
    runner = _Runner()
    supervisor = _supervisor(runner)
    unit = supervisor.unit_for_action(_ACTION)

    guard = _SubmissionGuard(unit)
    assert supervisor.start(unit, lambda: True, guard) is DispatchStartResult.ACCEPTED
    runner.show_output = _show_output(
        started_us=100,
        exited_us=200,
        result="exit-code",
        status=70,
    )
    with pytest.raises(SystemdSupervisorError, match="already invoked"):
        supervisor.start(unit, lambda: True, guard)

    verbs = [
        next(item for item in ("show", "start", "reset-failed") if item in arguments)
        for arguments, _timeout in runner.calls
    ]
    assert verbs == ["show", "start", "show"]


def test_dispatcher_repeat_is_rejected_by_durable_claim_without_manager_call(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        runner = _Runner()
        store = TransactionStore(tmp_path / "transactions")
        dispatcher = SystemdDispatcher(store, _supervisor(runner))
        prepared = await dispatcher.write_request(_effect(), _context())
        store.claim_submission(_ACTION)
        store.claim_execution(_ACTION)

        with pytest.raises(DispatchAdapterError, match="already submitted"):
            await dispatcher.start(prepared, lambda: True)
        assert runner.calls == []

    asyncio.run(exercise())


def test_recovery_marks_previously_invoked_no_result_ambiguous_and_unrestartable(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    runner.show_output = _show_output(
        started_us=100,
        exited_us=200,
        result="exit-code",
        status=70,
    )
    supervisor = _supervisor(runner)
    store = TransactionStore(tmp_path / "transactions")
    asyncio.run(
        SystemdDispatcher(store, supervisor).write_request(_effect(), _context())
    )
    store.claim_submission(_ACTION)
    store.claim_execution(_ACTION)

    snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)

    assert snapshot.units == ()
    assert snapshot.verified_tombstones == ()
    assert any("lacks terminal result" in item for item in snapshot.ambiguities)
    unit = supervisor.unit_for_action(_ACTION)
    with pytest.raises(SystemdSupervisorError, match="already invoked"):
        supervisor.start(
            unit,
            lambda: True,
            _SubmissionGuard(unit),
        )


@pytest.mark.parametrize(
    ("lifecycle", "status", "manager_result"),
    [
        (ActionLifecycle.COMPLETED, 0, "success"),
        (ActionLifecycle.FAILED, 23, "exit-code"),
        (ActionLifecycle.CANCELLED, 143, "signal"),
        (ActionLifecycle.TIMED_OUT, 124, "timeout"),
        (ActionLifecycle.UNKNOWN, 70, "success"),
    ],
)
def test_recovery_scanner_reconstructs_every_exact_terminal_outcome(
    tmp_path: Path,
    lifecycle: ActionLifecycle,
    status: int,
    manager_result: str,
) -> None:
    runner = _Runner()
    runner.show_output = _show_output(
        started_us=100,
        exited_us=200,
        result=manager_result,
        status=0 if manager_result == "success" else status,
    )
    supervisor = _supervisor(runner)
    store = TransactionStore(tmp_path / "transactions")
    request = asyncio.run(
        SystemdDispatcher(store, supervisor).write_request(_effect(), _context())
    )
    stored_request = store.read_request(request.action_id)
    store.claim_submission(_ACTION)
    store.claim_execution(_ACTION)
    store.write_result(
        TransactionResult(
            action_id=_ACTION,
            action_kind=ActionKind.APPLICATION,
            unit_name=stored_request.unit_name,
            request_sha256=stored_request.request_sha256,
            outcome=lifecycle,
            exit_status=status,
            started_monotonic_ms=1,
            finished_monotonic_ms=2,
            detail="exact terminal recovery",
        )
    )

    snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)

    assert snapshot.units == ()
    assert len(snapshot.verified_tombstones) == 1
    assert snapshot.verified_tombstones[0].lifecycle is lifecycle
    assert snapshot.ambiguities == ()


@pytest.mark.parametrize(
    ("lifecycle", "status"),
    [
        (ActionLifecycle.COMPLETED, 0),
        (ActionLifecycle.FAILED, 23),
        (ActionLifecycle.CANCELLED, 143),
        (ActionLifecycle.TIMED_OUT, 124),
        (ActionLifecycle.UNKNOWN, 70),
    ],
)
def test_recovery_accepts_bound_results_after_manager_history_is_collected(
    tmp_path: Path,
    lifecycle: ActionLifecycle,
    status: int,
) -> None:
    runner = _Runner()
    supervisor = _supervisor(runner)
    store = TransactionStore(tmp_path / "transactions")
    prepared = asyncio.run(
        SystemdDispatcher(store, supervisor).write_request(_effect(), _context())
    )
    request = store.read_request(prepared.action_id)
    store.claim_submission(request.action_id)
    store.claim_execution(request.action_id)
    store.write_result(
        TransactionResult(
            action_id=request.action_id,
            action_kind=request.action_kind,
            unit_name=request.unit_name,
            request_sha256=request.request_sha256,
            outcome=lifecycle,
            exit_status=status,
            started_monotonic_ms=1,
            finished_monotonic_ms=2,
            detail="manager history collected",
        )
    )

    snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)

    assert snapshot.verified_tombstones == (
        ActionTombstone(request.action_id, lifecycle),
    )
    assert snapshot.ambiguities == ()


def test_recovery_scanner_has_no_shadow_systemd_path(tmp_path: Path) -> None:
    scanner = SystemdRecoveryScanner(
        TransactionStore(tmp_path / "transactions"),
        _supervisor(_Runner()),
    )

    with pytest.raises(PermissionError, match="active authority"):
        scanner.scan(StateNamespace.SHADOW)
