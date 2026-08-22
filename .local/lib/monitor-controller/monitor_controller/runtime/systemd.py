"""Keyed systemd user-worker supervision and authoritative dispatch adapter."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import TimeoutExpired
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    ActivateProbe,
    ApplyProfile,
    WorkerUnit,
)
from monitor_controller.runtime.dispatcher import (
    DispatchAdapterError,
    DispatchEffect,
    DispatchFailureStage,
    DispatchStartResult,
    FinalDispatchFence,
    PreparedDispatch,
    WorkerActivity,
    WorkerCompletion,
    WorkerRequestContext,
)
from monitor_controller.runtime.persistence import StateNamespace
from monitor_controller.runtime.recovery import (
    VerifiedWorkerResult,
    WorkerNamespaceSnapshot,
)
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    BoundTransactionRecord,
    Payload,
    TransactionArtifact,
    TransactionProtocolError,
    TransactionRequest,
    TransactionResult,
    TransactionStore,
    parse_action_id,
)
from monitor_controller.workers.autorandr_profile import (
    materialize_autorandr_profile,
)

if TYPE_CHECKING:
    from monitor_controller.observer.snapshot import SavedProfileSource

DEFAULT_SYSTEMCTL_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_SYSTEMCTL_STOP_TIMEOUT_SECONDS: Final = 20.0
MAX_SYSTEMCTL_STOP_TIMEOUT_SECONDS: Final = 60.0
MAX_SYSTEMCTL_OUTPUT_BYTES: Final = 1024 * 1024
MANAGER_START_REJECTED_EXIT_STATUS: Final = 69

DEFAULT_UNIT_TEMPLATES: Final[Mapping[ActionKind, str]] = MappingProxyType(
    {
        ActionKind.PROBE: "monitor-probe@.service",
        ActionKind.APPLICATION: "monitor-apply@.service",
        ActionKind.PREPARATION: "monitor-prepare@.service",
        ActionKind.FINALIZATION: "monitor-finalize@.service",
    }
)

_SHOW_PROPERTIES: Final = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Job",
    "MainPID",
    "Result",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
    "ExecMainCode",
    "ExecMainStatus",
    "ControlGroup",
)
_ACTIVE_STATES: Final = frozenset(
    {"activating", "active", "reloading", "deactivating", "refreshing"}
)
_INACTIVE_STATES: Final = frozenset({"inactive", "failed"})
_UNIT_TEMPLATE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+@\.service$")


class SystemdSupervisorError(RuntimeError):
    """Systemd evidence is unavailable, rejected, or internally ambiguous."""


class SystemdCommandRejectedError(SystemdSupervisorError):
    """The user manager definitely rejected a synchronous command request."""


@dataclass(frozen=True, slots=True)
class SystemctlCommandResult:
    """Bounded output from one argument-array ``systemctl --user`` call."""

    arguments: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class SystemctlCommandRunner(Protocol):
    """Injected synchronous boundary used at the final non-yielding fence."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> SystemctlCommandResult:
        """Run one bounded argument array without invoking a shell."""
        ...


class BoundedSystemctlRunner:
    """Execute ``systemctl`` in a killable subprocess session with output bounds."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> SystemctlCommandResult:
        """Run a complete command, killing its process group on timeout."""
        if (
            not arguments
            or any(not item or "\x00" in item for item in arguments)
            or not 0 < timeout_seconds <= MAX_SYSTEMCTL_STOP_TIMEOUT_SECONDS
        ):
            msg = "invalid bounded systemctl command"
            raise ValueError(msg)
        process = subprocess.Popen(  # noqa: S603
            arguments,
            shell=False,
            start_new_session=True,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=False,
        )
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
        except TimeoutExpired as error:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout_bytes, stderr_bytes = process.communicate()
            msg = f"systemctl timed out after {timeout_seconds:g} seconds"
            raise SystemdSupervisorError(msg) from error
        if (
            len(stdout_bytes) > MAX_SYSTEMCTL_OUTPUT_BYTES
            or len(stderr_bytes) > MAX_SYSTEMCTL_OUTPUT_BYTES
        ):
            msg = "systemctl output exceeded the protocol bound"
            raise SystemdSupervisorError(msg)
        return SystemctlCommandResult(
            arguments=arguments,
            returncode=process.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True, slots=True)
class SystemdUnitState:
    """Strict subset of manager state needed to retain mutation exclusion."""

    unit_name: str
    load_state: str
    active_state: str
    sub_state: str
    job: str
    main_pid: int
    result: str
    exec_main_start_monotonic_us: int
    exec_main_exit_monotonic_us: int
    exec_main_code: int
    exec_main_status: int
    control_group: str

    @property
    def activity(self) -> WorkerActivity:
        """Classify only states whose cgroup mutation potential is unambiguous."""
        if self.job or self.active_state in _ACTIVE_STATES:
            return WorkerActivity.ACTIVE
        if self.active_state in _INACTIVE_STATES and self.main_pid == 0:
            return WorkerActivity.INACTIVE
        msg = (
            f"unit {self.unit_name} has ambiguous activity "
            f"{self.active_state}/{self.sub_state} pid={self.main_pid}"
        )
        raise SystemdSupervisorError(msg)

    @property
    def was_invoked(self) -> bool:
        """Return whether the manager retained evidence of an executed main process."""
        return self.exec_main_start_monotonic_us > 0


class SystemdSupervisor:
    """Make bounded, exact user-manager calls for keyed worker instances."""

    def __init__(
        self,
        *,
        systemctl: Path | None = None,
        runner: SystemctlCommandRunner | None = None,
        unit_templates: Mapping[ActionKind, str] = DEFAULT_UNIT_TEMPLATES,
        timeout_seconds: float = DEFAULT_SYSTEMCTL_TIMEOUT_SECONDS,
        stop_timeout_seconds: float = DEFAULT_SYSTEMCTL_STOP_TIMEOUT_SECONDS,
    ) -> None:
        """Bind fixed command and template identities without contacting systemd."""
        executable = systemctl
        if executable is None:
            discovered = shutil.which("systemctl")
            if discovered is None:
                msg = "systemctl is unavailable"
                raise ValueError(msg)
            executable = Path(discovered)
        if not executable.is_absolute():
            msg = "systemctl path must be absolute"
            raise ValueError(msg)
        if not 0 < timeout_seconds <= DEFAULT_SYSTEMCTL_STOP_TIMEOUT_SECONDS:
            msg = "systemctl timeout is outside the accepted bound"
            raise ValueError(msg)
        if not (
            timeout_seconds
            <= stop_timeout_seconds
            <= MAX_SYSTEMCTL_STOP_TIMEOUT_SECONDS
        ):
            msg = "systemctl stop timeout is outside the accepted bound"
            raise ValueError(msg)
        self._systemctl = executable
        self._runner = runner or BoundedSystemctlRunner()
        self._templates = _validate_templates(unit_templates)
        self._timeout_seconds = timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds

    @property
    def unit_templates(self) -> Mapping[ActionKind, str]:
        """Return the exact action-to-template binding."""
        return self._templates

    def unit_for_action(self, action_id: ActionId) -> WorkerUnit:
        """Derive the only accepted unit name for *action_id*."""
        template = self._templates.get(action_id.kind)
        if template is None:
            msg = f"action kind {action_id.kind.value} has no systemd worker template"
            raise ValueError(msg)
        escaped = escape_unit_instance(action_id.value)
        unit_name = template.replace("@.service", f"@{escaped}.service")
        return WorkerUnit(action_id, unit_name)

    def validate_unit(self, unit: WorkerUnit) -> None:
        """Reject any unit/action substitution before manager interaction."""
        expected = self.unit_for_action(unit.action_id)
        if unit != expected:
            msg = (
                f"unit {unit.unit_name!r} is not the exact instance for "
                f"{unit.action_id.value}"
            )
            raise SystemdSupervisorError(msg)

    def prepare_start(self, unit: WorkerUnit) -> None:
        """Fail closed unless manager truth proves this key was never invoked."""
        self.validate_unit(unit)
        state = self.inspect(unit)
        if state.activity is WorkerActivity.ACTIVE:
            msg = f"unit {unit.unit_name} is already active before first submission"
            raise SystemdSupervisorError(msg)
        if state.was_invoked or state.exec_main_exit_monotonic_us > 0:
            msg = f"unit {unit.unit_name} was already invoked and cannot be restarted"
            raise SystemdSupervisorError(msg)
        if state.active_state == "failed":
            msg = f"unit {unit.unit_name} has ambiguous failed manager state"
            raise SystemdSupervisorError(msg)

    def start(
        self,
        unit: WorkerUnit,
        final_fence: FinalDispatchFence,
        submission_guard: Callable[[], object],
    ) -> DispatchStartResult:
        """Durably claim and submit immediately after the final controller fence."""
        self.prepare_start(unit)
        if not final_fence():
            return DispatchStartResult.FENCE_REJECTED
        try:
            submission = submission_guard()
        except Exception as error:
            msg = f"durable submission guard rejected {unit.unit_name}: {error}"
            raise SystemdCommandRejectedError(msg) from error
        if (
            not isinstance(submission, BoundTransactionRecord)
            or not submission.record_sha256
            or submission.record_kind is not BoundRecordKind.SUBMISSION_CLAIM
            or submission.action_id != unit.action_id
            or submission.action_kind is not unit.action_id.kind
            or submission.unit_name != unit.unit_name
        ):
            msg = "durable submission guard returned a differently bound claim"
            raise SystemdCommandRejectedError(msg)
        # Deliberately synchronous: callback/claim success and Popen occur in this stack
        # without returning to the asyncio scheduler. --no-block acknowledges the
        # manager job rather than waiting for a short-lived oneshot to finish.
        result = self._invoke("start", "--no-block", unit.unit_name)
        if result.returncode != 0:
            raise SystemdCommandRejectedError(_command_detail("start", result))
        return DispatchStartResult.ACCEPTED

    def stop(self, unit: WorkerUnit) -> None:
        """Wait through cooperative SIGTERM and prove systemd cgroup cleanup."""
        self.validate_unit(unit)
        if self.inspect(unit).activity is WorkerActivity.INACTIVE:
            return
        result = self._invoke(
            "stop",
            unit.unit_name,
            timeout_seconds=self._stop_timeout_seconds,
        )
        if result.returncode != 0:
            raise SystemdSupervisorError(_command_detail("stop", result))
        terminal = self.inspect(unit)
        if terminal.activity is not WorkerActivity.INACTIVE:
            msg = f"unit {unit.unit_name} remained active after synchronous stop"
            raise SystemdSupervisorError(msg)

    def inspect(self, unit: WorkerUnit) -> SystemdUnitState:
        """Read a strict manager state snapshot for the exact keyed instance."""
        self.validate_unit(unit)
        property_arguments = tuple(
            argument for name in _SHOW_PROPERTIES for argument in ("--property", name)
        )
        result = self._invoke("show", *property_arguments, unit.unit_name)
        if result.returncode != 0:
            raise SystemdSupervisorError(_command_detail("show", result))
        values = _parse_show(result.stdout)
        state = SystemdUnitState(
            unit_name=unit.unit_name,
            load_state=values["LoadState"],
            active_state=values["ActiveState"],
            sub_state=values["SubState"],
            job=values["Job"],
            main_pid=_nonnegative_int(values["MainPID"], "MainPID"),
            result=values["Result"],
            exec_main_start_monotonic_us=_nonnegative_int(
                values["ExecMainStartTimestampMonotonic"],
                "ExecMainStartTimestampMonotonic",
            ),
            exec_main_exit_monotonic_us=_nonnegative_int(
                values["ExecMainExitTimestampMonotonic"],
                "ExecMainExitTimestampMonotonic",
            ),
            exec_main_code=_nonnegative_int(values["ExecMainCode"], "ExecMainCode"),
            exec_main_status=_nonnegative_int(
                values["ExecMainStatus"], "ExecMainStatus"
            ),
            control_group=values["ControlGroup"],
        )
        if state.load_state != "loaded":
            msg = f"unit {unit.unit_name} is not loaded: {state.load_state}"
            raise SystemdSupervisorError(msg)
        return state

    def query(self, unit: WorkerUnit) -> SystemdUnitState:
        """Expose the explicit query operation without changing manager state."""
        return self.inspect(unit)

    def reattach(self, unit: WorkerUnit) -> SystemdUnitState:
        """Re-query one persisted exact unit without starting or resetting it."""
        return self.inspect(unit)

    def list_worker_units(self) -> tuple[str, ...]:
        """List all loaded instances of the configured worker templates."""
        patterns = tuple(
            template.replace("@.service", "@*.service")
            for template in self._templates.values()
        )
        result = self._invoke(
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            *patterns,
        )
        if result.returncode != 0:
            raise SystemdSupervisorError(_command_detail("list-units", result))
        units: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if fields:
                units.append(fields[0])
        if len(units) != len(set(units)):
            msg = "systemd returned duplicate worker unit identities"
            raise SystemdSupervisorError(msg)
        return tuple(sorted(units))

    def _invoke(
        self,
        verb: str,
        *arguments: str,
        timeout_seconds: float | None = None,
    ) -> SystemctlCommandResult:
        command = (
            str(self._systemctl),
            "--user",
            "--no-pager",
            "--no-ask-password",
            verb,
            *arguments,
        )
        return self._runner.run(
            command,
            timeout_seconds=(
                self._timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
        )


class SystemdDispatcher:
    """Materialize immutable requests and supervise their exact systemd units."""

    def __init__(
        self,
        store: TransactionStore,
        supervisor: SystemdSupervisor,
        *,
        autorandr_profiles: SavedProfileSource | None = None,
    ) -> None:
        """Bind only the active transaction, profile, and manager boundaries."""
        self._store = store
        self._supervisor = supervisor
        self._autorandr_profiles = autorandr_profiles

    async def write_request(
        self,
        effect: DispatchEffect,
        context: WorkerRequestContext,
    ) -> PreparedDispatch:
        """Atomically create one fully bound request before any manager call."""
        unit = self._supervisor.unit_for_action(effect.action_id)
        request = _request_from_effect(effect, context, unit)
        artifacts: tuple[TransactionArtifact, ...] = ()
        if isinstance(effect, ApplyProfile):
            if self._autorandr_profiles is None:
                msg = "application dispatch requires an autorandr profile source"
                raise TransactionProtocolError(msg)
            profiles = tuple(
                item
                for item in self._autorandr_profiles.saved_profiles()
                if item.name == effect.profile
            )
            if len(profiles) != 1:
                msg = "application request lacks one exact saved autorandr profile"
                raise TransactionProtocolError(msg)
            profile = profiles[0]
            if (
                not context.profile_configuration_hashes
                or profile.configuration_hashes != context.profile_configuration_hashes
            ):
                msg = "saved autorandr profile content differs from admission"
                raise TransactionProtocolError(msg)
            materialized = materialize_autorandr_profile(
                profile,
                context.output_mapping,
                effect.action_id.value,
            )
            request = replace(request, payload=materialized.payload)
            artifacts = materialized.artifacts
        written = await asyncio.to_thread(
            self._store.create_request,
            request,
            artifacts,
        )
        return PreparedDispatch(
            action_id=effect.action_id,
            unit=unit,
            reference=str(
                self._store.action_directory(effect.action_id) / "request.json"
            ),
            request_sha256=written.request_sha256,
        )

    async def start(
        self,
        prepared: PreparedDispatch,
        final_fence: FinalDispatchFence,
    ) -> DispatchStartResult:
        """Validate, preflight, and invoke the supervisor's non-yielding fence."""
        request = await asyncio.to_thread(
            self._validated_prepared_request,
            prepared,
        )
        if self._store.result_if_present(request.action_id) is not None:
            msg = "prepared transaction already has terminal evidence"
            raise DispatchAdapterError(DispatchFailureStage.UNIT_START, msg)
        if self._store.submission_claim_if_present(request.action_id) is not None:
            msg = "prepared transaction was already submitted to the manager"
            raise DispatchAdapterError(DispatchFailureStage.UNIT_START, msg)
        if self._store.execution_claim_if_present(request.action_id) is not None:
            msg = "prepared transaction was already claimed by a worker invocation"
            raise DispatchAdapterError(DispatchFailureStage.UNIT_START, msg)
        try:
            return self._supervisor.start(
                prepared.unit,
                final_fence,
                lambda: self._store.claim_submission(request.action_id),
            )
        except SystemdCommandRejectedError as error:
            # The synchronous manager command may reject only after the durable
            # submission claim. Bind that definite terminal outcome to the exact
            # request so a crash before the controller rejection acknowledgement
            # cannot leave an unrestartable result-less transaction.
            completion: WorkerCompletion | None = None
            if self._store.submission_claim_if_present(request.action_id) is not None:
                now_ms = time.monotonic_ns() // 1_000_000
                result = self._store.write_result(
                    TransactionResult(
                        action_id=request.action_id,
                        action_kind=request.action_kind,
                        unit_name=request.unit_name,
                        request_sha256=request.request_sha256,
                        outcome=ActionLifecycle.FAILED,
                        exit_status=MANAGER_START_REJECTED_EXIT_STATUS,
                        started_monotonic_ms=now_ms,
                        finished_monotonic_ms=now_ms,
                        detail=str(error)[:512],
                        plan_hash=request.plan_hash,
                    )
                )
                completion = WorkerCompletion(
                    action_id=result.action_id,
                    terminal_lifecycle=result.outcome,
                    exit_status=result.exit_status,
                    plan_hash=result.plan_hash,
                )
            raise DispatchAdapterError(
                DispatchFailureStage.UNIT_START,
                str(error),
                completion=completion,
            ) from error

    async def discard_prepared(self, prepared: PreparedDispatch) -> None:
        """Remove only the exact request proven never submitted by the caller."""
        request = await asyncio.to_thread(
            self._validated_prepared_request,
            prepared,
        )
        await asyncio.to_thread(self._store.discard_unacknowledged, request)

    async def stop(
        self,
        action_id: ActionId,
        terminal_lifecycle: ActionLifecycle,
    ) -> None:
        """Persist exact stop intent before requesting manager cancellation."""
        request = await asyncio.to_thread(self._store.read_request, action_id)
        await asyncio.to_thread(
            self._store.create_stop_intent,
            action_id,
            terminal_lifecycle,
        )
        unit = WorkerUnit(action_id, request.unit_name)
        await asyncio.to_thread(self._supervisor.stop, unit)

    async def worker_activity(self, unit: WorkerUnit) -> WorkerActivity:
        """Return whether the exact manager unit can still mutate."""
        await asyncio.to_thread(self._validate_unit_request, unit)
        state = await asyncio.to_thread(self._supervisor.reattach, unit)
        return state.activity

    async def worker_completion(self, unit: WorkerUnit) -> WorkerCompletion | None:
        """Return a result only when manager and transaction evidence agree."""
        await asyncio.to_thread(self._validate_unit_request, unit)
        state = await asyncio.to_thread(self._supervisor.reattach, unit)
        if state.activity is WorkerActivity.ACTIVE:
            return None
        result = await asyncio.to_thread(self._store.result_if_present, unit.action_id)
        if result is None:
            return None
        _validate_terminal_manager_result(state, result)
        return WorkerCompletion(
            action_id=result.action_id,
            terminal_lifecycle=result.outcome,
            exit_status=result.exit_status,
            plan_hash=result.plan_hash,
        )

    def _validated_prepared_request(
        self, prepared: PreparedDispatch
    ) -> TransactionRequest:
        self._supervisor.validate_unit(prepared.unit)
        request = self._store.read_request(prepared.action_id)
        expected_reference = str(
            self._store.action_directory(prepared.action_id) / "request.json"
        )
        if (
            request.unit_name != prepared.unit.unit_name
            or prepared.reference != expected_reference
            or prepared.request_sha256 != request.request_sha256
        ):
            msg = "prepared dispatch does not match its immutable request"
            raise TransactionProtocolError(msg)
        return request

    def _validate_unit_request(self, unit: WorkerUnit) -> None:
        self._supervisor.validate_unit(unit)
        request = self._store.read_request(unit.action_id)
        if request.unit_name != unit.unit_name:
            msg = "persisted request is bound to a different worker unit"
            raise TransactionProtocolError(msg)


@dataclass(frozen=True, slots=True)
class _ScannedTransaction:
    unit: WorkerUnit | None = None
    tombstone: ActionTombstone | None = None
    result: VerifiedWorkerResult | None = None
    represented_name: str | None = None
    sequence: int = 0
    transition_sequence: int = 0
    ambiguity: str | None = None


class SystemdRecoveryScanner:
    """Reconcile active transactions with surviving user-manager worker units."""

    def __init__(
        self,
        store: TransactionStore,
        supervisor: SystemdSupervisor,
    ) -> None:
        """Bind the active-only namespace scanner."""
        self._store = store
        self._supervisor = supervisor

    def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
        """Return active worker/tombstone truth, denying all shadow use."""
        if namespace is not StateNamespace.ACTIVE:
            msg = "systemd recovery scanner is forbidden outside active authority"
            raise PermissionError(msg)
        units: list[WorkerUnit] = []
        tombstones: list[ActionTombstone] = []
        results: list[VerifiedWorkerResult] = []
        ambiguities: list[str] = []
        represented_names: set[str] = set()
        high_water = 0
        transition_high_water = 0
        for directory in self._store.action_directories():
            scanned = self._scan_transaction(directory)
            high_water = max(high_water, scanned.sequence)
            transition_high_water = max(
                transition_high_water,
                scanned.transition_sequence,
            )
            if scanned.represented_name is not None:
                represented_names.add(scanned.represented_name)
            if scanned.unit is not None:
                units.append(scanned.unit)
            if scanned.tombstone is not None:
                tombstones.append(scanned.tombstone)
            if scanned.result is not None:
                results.append(scanned.result)
            if scanned.ambiguity is not None:
                ambiguities.append(scanned.ambiguity)
        try:
            loaded_names = self._supervisor.list_worker_units()
        except SystemdSupervisorError as error:
            ambiguities.append(f"worker unit listing failed: {error}")
        else:
            ambiguities.extend(
                f"loaded worker unit {unit_name} has no exact transaction"
                for unit_name in loaded_names
                if unit_name not in represented_names
            )
        return WorkerNamespaceSnapshot(
            units=tuple(sorted(units, key=lambda item: item.action_id.value)),
            verified_tombstones=tuple(
                sorted(tombstones, key=lambda item: item.action_id.value)
            ),
            verified_results=tuple(
                sorted(results, key=lambda item: item.action_id.value)
            ),
            action_sequence_high_water=high_water,
            transition_sequence_high_water=transition_high_water,
            ambiguities=tuple(sorted(set(ambiguities))),
        )

    def _scan_transaction(self, directory: Path) -> _ScannedTransaction:
        try:
            action_id = parse_action_id(directory.name)
            request = self._store.read_request(action_id)
            unit = WorkerUnit(action_id, request.unit_name)
            self._supervisor.validate_unit(unit)
            state = self._supervisor.reattach(unit)
            submission = self._store.submission_claim_if_present(action_id)
            claim = self._store.execution_claim_if_present(action_id)
            result = self._store.result_if_present(action_id)
            transition_sequence = (
                0 if request.transition_id is None else request.transition_id.sequence
            )
            if state.activity is WorkerActivity.ACTIVE:
                if result is not None:
                    ambiguity = f"live worker {action_id.value} already has a result"
                elif claim is None:
                    ambiguity = (
                        f"live worker {action_id.value} has not acquired its durable "
                        "execution claim"
                    )
                else:
                    ambiguity = None
                return _ScannedTransaction(
                    unit=unit,
                    represented_name=unit.unit_name,
                    sequence=action_id.sequence,
                    transition_sequence=transition_sequence,
                    ambiguity=ambiguity,
                )
            if result is None:
                invoked = (
                    submission is not None
                    or claim is not None
                    or state.was_invoked
                    or state.exec_main_exit_monotonic_us > 0
                    or state.active_state == "failed"
                )
                return _ScannedTransaction(
                    represented_name=unit.unit_name,
                    sequence=action_id.sequence,
                    transition_sequence=transition_sequence,
                    ambiguity=(
                        f"inactive invoked worker {action_id.value} "
                        "lacks terminal result"
                        if invoked
                        else None
                    ),
                )
            _validate_terminal_manager_result(state, result)
            return _ScannedTransaction(
                tombstone=ActionTombstone(action_id, result.outcome),
                result=VerifiedWorkerResult(
                    unit=unit,
                    terminal_lifecycle=result.outcome,
                    exit_status=result.exit_status,
                    finished_monotonic_ms=result.finished_monotonic_ms,
                    plan_hash=result.plan_hash,
                ),
                represented_name=unit.unit_name,
                sequence=action_id.sequence,
                transition_sequence=transition_sequence,
            )
        except (OSError, ValueError, SystemdSupervisorError) as error:
            return _ScannedTransaction(
                ambiguity=(
                    f"transaction {directory.name} is ambiguous: "
                    f"{type(error).__name__}: {error}"
                )
            )


def escape_unit_instance(value: str) -> str:
    """Implement systemd's non-path unit-instance escaping for UTF-8 text."""
    if not value or "\x00" in value:
        msg = "unit instance must be non-empty text without NUL bytes"
        raise ValueError(msg)
    escaped: list[str] = []
    for index, byte in enumerate(value.encode("utf-8")):
        character = chr(byte)
        safe = character.isascii() and (character.isalnum() or character in "_.:")
        if safe and not (index == 0 and character == "."):
            escaped.append(character)
        else:
            escaped.append(f"\\x{byte:02x}")
    return "".join(escaped)


def _request_from_effect(
    effect: DispatchEffect,
    context: WorkerRequestContext,
    unit: WorkerUnit,
) -> TransactionRequest:
    profile: str | None
    transition_id = None
    transition_key = None
    plan_hash = None
    payload: Payload
    if not isinstance(effect, ActivateProbe) and (
        context.probe_base_hash is not None or context.probe_edid_integrity is not None
    ):
        msg = "non-probe request context cannot carry probe identity proof"
        raise TransactionProtocolError(msg)
    if not isinstance(effect, ApplyProfile) and context.profile_configuration_hashes:
        msg = "non-application request cannot carry autorandr profile hashes"
        raise TransactionProtocolError(msg)
    if isinstance(effect, ActivateProbe):
        if context.physical_epoch != effect.key.physical_epoch:
            msg = "probe request context physical epoch differs"
            raise TransactionProtocolError(msg)
        if context.probe_base_hash is None or context.probe_edid_integrity is None:
            msg = "probe request context lacks immutable base-identity proof"
            raise TransactionProtocolError(msg)
        profile = effect.key.profile
        payload = tuple(
            sorted(
                (
                    ("base_identity_hash", context.probe_base_hash),
                    ("edid_integrity", context.probe_edid_integrity.value),
                    ("internal_output", effect.internal_output),
                    ("preferred_mode", effect.preferred_mode),
                    ("probe_output", effect.output),
                )
            )
        )
    elif isinstance(effect, ApplyProfile):
        if (
            context.physical_epoch != effect.key.physical_epoch
            or context.output_mapping != effect.mapping.outputs
        ):
            msg = "application request context proof differs"
            raise TransactionProtocolError(msg)
        profile = effect.profile
        payload = ()
    else:
        # DispatchEffect is closed; after probe/application only the two desktop
        # effect classes remain.
        profile = effect.profile
        transition_id = effect.transition_id
        transition_key = effect.transition_key
        plan_hash = effect.plan_hash
        payload = ()
    return TransactionRequest(
        action_id=effect.action_id,
        action_kind=effect.action_id.kind,
        unit_name=unit.unit_name,
        physical_epoch=context.physical_epoch,
        physical_token=context.physical_token,
        admitted_event_generation=effect.admitted_event_generation,
        observation_key=effect.observation_key,
        output_mapping=context.output_mapping,
        expected_topology=context.expected_topology,
        profile=profile,
        layout=context.layout,
        transition_id=transition_id,
        transition_key=transition_key,
        plan_hash=plan_hash,
        payload=payload,
    )


def _validate_terminal_manager_result(
    state: SystemdUnitState,
    result: TransactionResult,
) -> None:
    if state.activity is not WorkerActivity.INACTIVE:
        msg = "terminal result cannot be accepted while its unit is active"
        raise SystemdSupervisorError(msg)
    # The user manager may garbage-collect any inactive template instance before
    # recovery. A freshly materialized inactive template has no process history;
    # the independently bound submission/result evidence still proves the exact
    # terminal outcome, while manager inactivity proves no process can mutate.
    if (
        state.active_state == "inactive"
        and not state.was_invoked
        and state.exec_main_exit_monotonic_us == 0
    ):
        return
    # Once execution is independently claimed, the immutable request-bound result
    # owns semantic outcome. Manager state owns only cgroup inactivity: a stop can
    # race after a completed result is installed but before the process exits and
    # legitimately replace systemd's Result=success with Result=signal.
    if result.outcome is not ActionLifecycle.COMPLETED and (
        result.outcome is not ActionLifecycle.UNKNOWN and state.result == "success"
    ):
        msg = "non-completed worker result conflicts with successful manager truth"
        raise SystemdSupervisorError(msg)


def _validate_templates(
    templates: Mapping[ActionKind, str],
) -> Mapping[ActionKind, str]:
    expected = {
        ActionKind.PROBE,
        ActionKind.APPLICATION,
        ActionKind.PREPARATION,
        ActionKind.FINALIZATION,
    }
    if set(templates) != expected:
        msg = "systemd worker templates must cover exactly four mutation actions"
        raise ValueError(msg)
    copied = dict(templates)
    for template in copied.values():
        if not _UNIT_TEMPLATE_PATTERN.fullmatch(template):
            msg = f"invalid systemd worker template: {template!r}"
            raise ValueError(msg)
    if len(set(copied.values())) != len(copied):
        msg = "systemd worker templates must be distinct"
        raise ValueError(msg)
    return MappingProxyType(copied)


def _parse_show(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in _SHOW_PROPERTIES or name in values:
            msg = f"malformed or duplicate systemd show property: {line!r}"
            raise SystemdSupervisorError(msg)
        values[name] = value
    if set(values) != set(_SHOW_PROPERTIES):
        missing = sorted(set(_SHOW_PROPERTIES) - set(values))
        msg = f"systemd show omitted required properties: {missing}"
        raise SystemdSupervisorError(msg)
    return values


def _nonnegative_int(value: str, name: str) -> int:
    if not value.isascii() or not value.isdigit():
        msg = f"systemd {name} is not a non-negative integer"
        raise SystemdSupervisorError(msg)
    try:
        parsed = int(value)
    except ValueError as error:
        msg = f"systemd {name} is outside the integer domain"
        raise SystemdSupervisorError(msg) from error
    return parsed


def _command_detail(verb: str, result: SystemctlCommandResult) -> str:
    detail = " ".join((result.stderr or result.stdout).split())[:448]
    if not detail:
        detail = "no diagnostic output"
    return f"systemctl {verb} exited {result.returncode}: {detail}"
