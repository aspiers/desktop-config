"""Fail-closed startup, topology, cancellation, and result helpers for workers."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Never

from monitor_controller.model import ActionKind, ActionLifecycle, PhysicalToken
from monitor_controller.observer.drm import (
    ConnectorKind,
    ConnectorStatus,
    DrmSnapshot,
    EvidenceState,
    ReadOnlyTree,
    sample_drm,
)
from monitor_controller.observer.topology import (
    CanonicalTopologyEvidence,
    derive_canonical_topology,
)
from monitor_controller.observer.xrandr import (
    XrandrEvidenceSource,
    XrandrSnapshot,
    sample_xrandr,
)
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    BoundTransactionRecord,
    ExpectedTopology,
    TransactionProtocolError,
    TransactionRequest,
    TransactionResult,
    TransactionStore,
    parse_action_id,
)

UNIMPLEMENTED_EXIT_STATUS: Final = 78
STALE_EXIT_STATUS: Final = 75
TOPOLOGY_REJECTED_EXIT_STATUS: Final = STALE_EXIT_STATUS
CANCELLED_EXIT_STATUS: Final = 143
TIMED_OUT_EXIT_STATUS: Final = 124
WORKER_EXCEPTION_EXIT_STATUS: Final = 70
MAX_EXIT_STATUS: Final = 255
MAX_RESULT_DETAIL_LENGTH: Final = 512
COMMAND_NOT_FOUND_EXIT_STATUS: Final = 127
COMMAND_TIMEOUT_EXIT_STATUS: Final = 124
MAX_COMMAND_EXIT_STATUS: Final = 255


class WorkerStartupError(RuntimeError):
    """The invocation cannot prove exact request, action, unit, or topology identity."""


class WorkerCancelled(BaseException):
    """SIGTERM or a durable stop intent interrupted a mutating boundary."""


def stale(detail: str) -> Never:
    """Refuse a worker boundary whose identity or evidence cannot be proven."""
    raise WorkerStartupError(detail)


_DISPLAY_PATTERN = re.compile(r"(?:[A-Za-z0-9_.-]+)?:[0-9]+(?:\.[0-9]+)?")


def safe_absolute_path(value: str) -> bool:
    """Return whether a value is one clean absolute path."""
    return (
        bool(value)
        and not any(character in value for character in "\x00\r\n")
        and Path(value).is_absolute()
    )


def display_authority_environment(
    base_environment: Mapping[str, str],
    role: str,
) -> dict[str, str]:
    """Return validated DISPLAY and XAUTHORITY for leaves that reach the X server.

    Extracted from the application worker so preparation leaves get the same
    guarantees: `xrdb -merge` inside `set-xfce4-dpi` needs X access just as
    autorandr does, and omitting DISPLAY from the closed preparation
    environment failed the first live unplug's desktop preparation (dc-2in).
    """
    display = base_environment.get("DISPLAY")
    if not display or _DISPLAY_PATTERN.fullmatch(display) is None:
        stale(f"{role} requires one safe DISPLAY value")
    inherited = base_environment.get("XAUTHORITY")
    if inherited:
        authority = Path(inherited)
        value = inherited
        source = "inherited XAUTHORITY"
    else:
        original_home = base_environment.get("HOME")
        if not original_home or not safe_absolute_path(original_home):
            stale(f"{role} requires safe HOME for the .Xauthority fallback")
        try:
            authority = (Path(original_home) / ".Xauthority").resolve()
        except OSError as error:
            stale(f"cannot resolve {role} HOME .Xauthority: {error}")
        value = str(authority)
        source = "HOME .Xauthority fallback"
    if not safe_absolute_path(value):
        stale(f"{role} {source} must be one absolute path")
    detail = (
        f"{role} DISPLAY {display!r} requires a readable regular X11 "
        f"authority file; {source} is unusable: {authority}"
    )
    try:
        authority_stat = authority.stat()
        if not stat.S_ISREG(authority_stat.st_mode):
            stale(detail)
        with authority.open("rb") as stream:
            stream.read(1)
    except OSError as error:
        stale(f"{detail}: {error}")
    return {"DISPLAY": display, "XAUTHORITY": value}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded terminal status from one exact worker command."""

    exit_status: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_COMMAND_EXIT_STATUS:
            msg = "worker command exit status must be between zero and 255"
            raise ValueError(msg)
        if self.timed_out and self.exit_status != COMMAND_TIMEOUT_EXIT_STATUS:
            msg = "timed-out worker command requires status 124"
            raise ValueError(msg)


def run_leaf_command(
    arguments: tuple[str, ...],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float,
    input_bytes: bytes | None = None,
) -> CommandResult:
    """Run one exact leaf in a separately killable process session, no shell."""
    try:
        process = subprocess.Popen(  # noqa: S603
            arguments,
            env=dict(environment),
            shell=False,
            start_new_session=True,
            stderr=subprocess.DEVNULL,
            stdin=(subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL),
            stdout=subprocess.DEVNULL,
        )
    except OSError:
        return CommandResult(COMMAND_NOT_FOUND_EXIT_STATUS)
    try:
        try:
            _stdout, _stderr = process.communicate(
                input=input_bytes,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            kill_process_group(process)
            process.communicate()
            return CommandResult(COMMAND_TIMEOUT_EXIT_STATUS, timed_out=True)
    except BaseException:
        kill_process_group(process)
        with contextlib.suppress(OSError):
            process.communicate()
        raise
    status = process.returncode
    if status < 0:
        status = min(MAX_COMMAND_EXIT_STATUS, 128 + abs(status))
    return CommandResult(min(status, MAX_COMMAND_EXIT_STATUS))


def atomic_replace(destination: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Write content beside the destination and rename over it durably."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def payload_text(request: TransactionRequest, name: str) -> str:
    """Return one required non-empty request payload string or refuse."""
    try:
        value = request.payload_value(name)
    except TransactionProtocolError as error:
        raise WorkerStartupError(str(error)) from error
    if not isinstance(value, str) or not value:
        stale(f"worker request {name} is not non-empty text")
    return value


def raise_if_cancelled(startup: WorkerStartup) -> None:
    """Honour a durable keyed stop intent at a cooperative boundary."""
    if startup.store.stop_intent_if_present(startup.request.action_id) is not None:
        raise WorkerCancelled


def validate_execution_claim(startup: WorkerStartup, kind: ActionKind) -> None:
    """Require this invocation's exact durable execution claim for its kind."""
    request = startup.request
    claim = startup.execution_claim
    if (
        claim is None
        or claim.record_kind is not BoundRecordKind.EXECUTION_CLAIM
        or claim.action_id != request.action_id
        or claim.action_kind is not kind
        or claim.unit_name != request.unit_name
        or claim.request_sha256 != request.request_sha256
    ):
        stale("worker lacks its exact durable execution claim")


@dataclass(frozen=True, slots=True)
class SampledTopology:
    """Everything one trustworthy worker-boundary sample proved."""

    current: CurrentTopology
    drm: DrmSnapshot
    topology: CanonicalTopologyEvidence
    xrandr: XrandrSnapshot


def sample_exact_topology(
    request: TransactionRequest,
    drm_tree: ReadOnlyTree,
    commands: XrandrEvidenceSource,
) -> SampledTopology:
    """Prove the display evidence is trustworthy and matches the admitted request.

    This is the evidence-freshness prologue every mutating worker boundary
    repeats: double-sampled DRM with an equality check, complete scan state,
    valid XRandR, certain connector status, unique DRM/X correspondence, and
    kernel/X agreement, guarded against the request's expected topology. It
    exists exactly once so a tightened check reaches every worker (dc-c6e).
    """
    begin_drm = sample_drm(drm_tree)
    xrandr = sample_xrandr(commands)
    end_drm = sample_drm(drm_tree)
    if begin_drm != end_drm:
        stale("DRM evidence changed during the worker-local sample")
    if begin_drm.scan_state is not EvidenceState.AVAILABLE:
        stale("DRM connector scan is not complete")
    if not xrandr.valid:
        stale("XRandR query and properties evidence is invalid or torn")
    if any(
        item.kind is not ConnectorKind.VIRTUAL
        and (
            item.status_state is not EvidenceState.AVAILABLE
            or item.status is ConnectorStatus.UNKNOWN
        )
        for item in begin_drm.connectors
    ):
        stale("DRM connector status evidence is uncertain")
    topology = derive_canonical_topology(begin_drm, xrandr)
    if topology.inconsistent:
        stale("DRM and X connector identity is contradictory or non-unique")
    if set(topology.kernel_connected_outputs) != set(topology.x_connected_outputs):
        stale("kernel and X connected topologies differ")
    current = CurrentTopology(
        physical_token=topology.physical_token,
        topology=ExpectedTopology(
            kernel_connected_outputs=topology.kernel_connected_outputs,
            kernel_external_outputs=topology.kernel_external_outputs,
            x_connected_outputs=topology.x_connected_outputs,
            x_active_outputs=topology.x_active_outputs,
        ),
    )
    validate_topology_guard(request, current)
    return SampledTopology(current, begin_drm, topology, xrandr)


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill a leaf's whole session even when its original leader has exited."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


@dataclass(frozen=True, slots=True)
class CurrentTopology:
    """Fresh worker-local topology evidence sampled immediately before mutation."""

    physical_token: PhysicalToken
    topology: ExpectedTopology


@dataclass(frozen=True, slots=True)
class WorkerStartup:
    """Validated immutable request and its sole result-store capability."""

    request: TransactionRequest
    store: TransactionStore
    execution_claim: BoundTransactionRecord | None


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    """A worker implementation's structured terminal outcome."""

    outcome: ActionLifecycle
    exit_status: int
    detail: str

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_EXIT_STATUS:
            msg = "worker execution status must be between zero and 255"
            raise ValueError(msg)
        if self.outcome is ActionLifecycle.COMPLETED and self.exit_status != 0:
            msg = "completed worker execution requires status zero"
            raise ValueError(msg)
        if self.outcome is not ActionLifecycle.COMPLETED and self.exit_status == 0:
            msg = "non-completed worker execution requires non-zero status"
            raise ValueError(msg)
        if (
            not self.detail
            or self.detail.isspace()
            or len(self.detail) > MAX_RESULT_DETAIL_LENGTH
        ):
            msg = "worker execution detail must be non-empty bounded text"
            raise ValueError(msg)


type TopologyReader = Callable[[TransactionRequest], CurrentTopology]
type WorkerImplementation = Callable[[TransactionRequest], WorkerExecution]


def validate_worker_startup(
    *,
    transaction_root: Path,
    action_id_text: str,
    unit_name: str,
    expected_kind: ActionKind,
    acquire_execution_claim: bool = True,
) -> WorkerStartup:
    """Prove invocation identity and atomically claim before any topology read."""
    try:
        action_id = parse_action_id(action_id_text)
        if action_id.kind is not expected_kind:
            msg = (
                f"invoked {expected_kind.value} worker for "
                f"{action_id.kind.value} action"
            )
            raise WorkerStartupError(msg)
        store = TransactionStore(transaction_root)
        request = store.read_request(action_id)
    except (OSError, TransactionProtocolError, ValueError) as error:
        if isinstance(error, WorkerStartupError):
            raise
        msg = (
            f"cannot validate immutable worker request: {type(error).__name__}: {error}"
        )
        raise WorkerStartupError(msg) from error
    if request.action_kind is not expected_kind:
        msg = "request action kind differs from worker entry point"
        raise WorkerStartupError(msg)
    if request.action_id.value != action_id_text:
        msg = "request action ID differs from the unescaped unit instance"
        raise WorkerStartupError(msg)
    if request.unit_name != unit_name:
        msg = "request unit name differs from the invoked unit"
        raise WorkerStartupError(msg)
    try:
        if (
            acquire_execution_claim
            and store.result_if_present(request.action_id) is not None
        ):
            msg = "worker action already claimed by an immutable terminal result"
            raise WorkerStartupError(msg)
        claim = (
            store.claim_execution(request.action_id)
            if acquire_execution_claim
            else store.execution_claim_if_present(request.action_id)
        )
    except (OSError, TransactionProtocolError) as error:
        msg = f"cannot acquire durable execution claim: {type(error).__name__}: {error}"
        raise WorkerStartupError(msg) from error
    return WorkerStartup(request=request, store=store, execution_claim=claim)


def validate_topology_guard(
    request: TransactionRequest,
    current: CurrentTopology,
) -> None:
    """Require the exact admitted physical token and connected/active topology."""
    if current.physical_token != request.physical_token:
        msg = "worker physical token changed after admission"
        raise WorkerStartupError(msg)
    if current.topology != request.expected_topology:
        msg = "worker connected or active topology changed after admission"
        raise WorkerStartupError(msg)
    mapped_live = {item.live_output for item in request.output_mapping}
    if mapped_live and mapped_live != set(current.topology.x_connected_outputs):
        msg = "worker output mapping no longer covers exact X-connected topology"
        raise WorkerStartupError(msg)
    if request.action_kind is ActionKind.PROBE:
        probe_output = request.payload_value("probe_output")
        internal_output = request.payload_value("internal_output")
        if (
            not isinstance(probe_output, str)
            or not isinstance(internal_output, str)
            or probe_output not in current.topology.x_connected_outputs
            or probe_output in current.topology.x_active_outputs
            or internal_output not in current.topology.x_active_outputs
        ):
            msg = "probe topology no longer has the admitted inactive/active outputs"
            raise WorkerStartupError(msg)


def execute_worker(
    startup: WorkerStartup,
    *,
    topology_reader: TopologyReader,
    implementation: WorkerImplementation,
) -> int:
    """Guard, execute, and atomically report one short-lived worker action."""
    started_ms = _monotonic_ms()
    try:
        current = topology_reader(startup.request)
        validate_topology_guard(startup.request, current)
        execution = implementation(startup.request)
    except WorkerCancelled:
        intent = startup.store.stop_intent_if_present(startup.request.action_id)
        if intent is None:
            # A manager deadline also arrives as SIGTERM.  Without a durable
            # controller intent, do not guess cancellation and thereby preempt
            # ExecStopPost's authoritative SERVICE_RESULT (notably "timeout").
            raise
        execution = WorkerExecution(
            intent.terminal_lifecycle,
            _terminal_exit_status(intent.terminal_lifecycle),
            f"worker interrupted for {intent.terminal_lifecycle.value} stop intent",
        )
    except WorkerStartupError as error:
        execution = WorkerExecution(
            ActionLifecycle.FAILED,
            TOPOLOGY_REJECTED_EXIT_STATUS,
            _bounded_detail("STALE", error),
        )
    except Exception as error:  # noqa: BLE001 - worker implementation boundary
        execution = WorkerExecution(
            ActionLifecycle.FAILED,
            WORKER_EXCEPTION_EXIT_STATUS,
            _bounded_detail("worker exception", error),
        )
    write_worker_result(
        startup,
        execution=execution,
        started_monotonic_ms=started_ms,
        finished_monotonic_ms=max(started_ms, _monotonic_ms()),
    )
    return execution.exit_status


def write_worker_result(
    startup: WorkerStartup,
    *,
    execution: WorkerExecution,
    started_monotonic_ms: int,
    finished_monotonic_ms: int,
) -> TransactionResult:
    """Atomically write terminal output bound to request hash, unit, and plan."""
    request = startup.request
    result = TransactionResult(
        action_id=request.action_id,
        action_kind=request.action_kind,
        unit_name=request.unit_name,
        request_sha256=request.request_sha256,
        outcome=execution.outcome,
        exit_status=execution.exit_status,
        started_monotonic_ms=started_monotonic_ms,
        finished_monotonic_ms=finished_monotonic_ms,
        detail=execution.detail,
        plan_hash=request.plan_hash,
    )
    return startup.store.write_result(result)


def install_cooperative_sigterm_handler() -> None:
    """Turn SIGTERM into a catchable cancellation at the current Python boundary."""

    def cancel(_signum: int, _frame: object) -> Never:
        raise WorkerCancelled

    signal.signal(signal.SIGTERM, cancel)


def reject_unimplemented(startup: WorkerStartup) -> int:
    """Report a terminal failure so no placeholder production action can succeed."""
    now_ms = _monotonic_ms()
    execution = WorkerExecution(
        ActionLifecycle.FAILED,
        UNIMPLEMENTED_EXIT_STATUS,
        f"{startup.request.action_kind.value} production worker is unimplemented",
    )
    write_worker_result(
        startup,
        execution=execution,
        started_monotonic_ms=now_ms,
        finished_monotonic_ms=now_ms,
    )
    return execution.exit_status


def record_systemd_result(
    startup: WorkerStartup,
    *,
    service_result: str,
    exit_code: str,
    exit_status: str,
) -> int:
    """Fill a missing exact result from durable stop intent and manager truth."""
    existing = startup.store.result_if_present(startup.request.action_id)
    if existing is not None:
        return 0
    intent = startup.store.stop_intent_if_present(startup.request.action_id)
    if service_result == "timeout":
        # A manager-enforced deadline is stronger terminal truth than an earlier
        # stop request: the unit actually exhausted a systemd timeout.
        lifecycle = ActionLifecycle.TIMED_OUT
    elif intent is not None:
        lifecycle = intent.terminal_lifecycle
    elif service_result in {
        "exit-code",
        "signal",
        "core-dump",
        "watchdog",
        "resources",
        "protocol",
    }:
        lifecycle = ActionLifecycle.FAILED
    else:
        # A missing worker result plus success/unset/unrecognized manager text does
        # not prove either completion or failure.
        lifecycle = ActionLifecycle.UNKNOWN
    detail_parts = [
        f"systemd {name}={value or 'unset'}"
        for name, value in (
            ("SERVICE_RESULT", service_result),
            ("EXIT_CODE", exit_code),
            ("EXIT_STATUS", exit_status),
        )
    ]
    if intent is not None:
        detail_parts.append(f"stop-intent={intent.terminal_lifecycle.value}")
    status = (
        _failed_systemd_status(exit_status)
        if lifecycle is ActionLifecycle.FAILED
        else _terminal_exit_status(lifecycle)
    )
    now_ms = _monotonic_ms()
    write_worker_result(
        startup,
        execution=WorkerExecution(lifecycle, status, " ".join(detail_parts)[:512]),
        started_monotonic_ms=now_ms,
        finished_monotonic_ms=now_ms,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Common keyed monitor-worker helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("reject-unimplemented", "record-systemd-result"):
        child = subparsers.add_parser(command)
        child.add_argument("--transaction-root", required=True, type=Path)
        child.add_argument("--action-id", required=True)
        child.add_argument("--unit", required=True)
        child.add_argument(
            "--action-kind",
            required=True,
            choices=tuple(
                kind.value for kind in ActionKind if kind is not ActionKind.PLAN
            ),
        )
        if command == "record-systemd-result":
            child.add_argument("--service-result", default="")
            child.add_argument("--exit-code", default="")
            child.add_argument("--exit-status", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run static-unit rejection or post-stop result recovery."""
    arguments = _parser().parse_args(argv)
    kind = ActionKind(arguments.action_kind)
    startup = validate_worker_startup(
        transaction_root=arguments.transaction_root,
        action_id_text=arguments.action_id,
        unit_name=arguments.unit,
        expected_kind=kind,
        acquire_execution_claim=arguments.command != "record-systemd-result",
    )
    if arguments.command == "reject-unimplemented":
        return reject_unimplemented(startup)
    return record_systemd_result(
        startup,
        service_result=arguments.service_result,
        exit_code=arguments.exit_code,
        exit_status=arguments.exit_status,
    )


def _terminal_exit_status(lifecycle: ActionLifecycle) -> int:
    statuses = {
        ActionLifecycle.CANCELLED: CANCELLED_EXIT_STATUS,
        ActionLifecycle.TIMED_OUT: TIMED_OUT_EXIT_STATUS,
        ActionLifecycle.UNKNOWN: WORKER_EXCEPTION_EXIT_STATUS,
    }
    try:
        return statuses[lifecycle]
    except KeyError as error:
        msg = f"{lifecycle.value} is not a stop-intent terminal lifecycle"
        raise ValueError(msg) from error


def _failed_systemd_status(value: str) -> int:
    if value.isascii() and value.isdigit():
        try:
            parsed = int(value)
        except ValueError:
            parsed = 0
        if 0 < parsed <= MAX_EXIT_STATUS:
            return parsed
    return WORKER_EXCEPTION_EXIT_STATUS


def _bounded_detail(prefix: str, error: BaseException) -> str:
    detail = " ".join(str(error).split())[:448] or type(error).__name__
    return f"{prefix}: {detail}"


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


if __name__ == "__main__":
    raise SystemExit(main())
