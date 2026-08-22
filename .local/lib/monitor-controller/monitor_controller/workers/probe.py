"""Guarded, keyed one-shot monitor activation probe worker."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final, Never, Protocol, final

from monitor_controller.model import (
    BROKEN_EXTENSION_EDID_INTEGRITIES,
    ActionKind,
    ActionLifecycle,
    EdidIntegrity,
)
from monitor_controller.observer.drm import (
    ConnectorKind,
    ConnectorStatus,
    DrmConnector,
    EvidenceState,
    ReadOnlyTree,
    RootedSysfsReader,
    sample_drm,
)
from monitor_controller.observer.evidence import RawEvidenceSource, TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import (
    XrandrEvidenceSource,
    XrandrOutput,
    sample_xrandr,
)
from monitor_controller.runtime.commands import (
    BoundedCommandRunner,
    CommandRequest,
    CommandRunner,
)
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    ExpectedTopology,
    TransactionProtocolError,
    TransactionRequest,
)
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    execute_worker,
    install_cooperative_sigterm_handler,
    validate_topology_guard,
    validate_worker_startup,
)

PROBE_COMMAND_TIMEOUT_SECONDS: Final = 10.0
COMMAND_NOT_FOUND_EXIT_STATUS: Final = 127
COMMAND_TIMEOUT_EXIT_STATUS: Final = 124
MAX_COMMAND_EXIT_STATUS: Final = 255
EXPECTED_CONNECTED_OUTPUTS: Final = 2
_XRANDR_QUERY = ("xrandr", "--query")
_XRANDR_PROPERTIES = ("xrandr", "--props")
_PROBE_PAYLOAD_FIELDS: Final = frozenset(
    {
        "base_identity_hash",
        "edid_integrity",
        "internal_output",
        "preferred_mode",
        "probe_output",
    }
)


@dataclass(frozen=True, slots=True)
class ProbeCommandResult:
    """Bounded terminal status from the sole mutating XRandR command."""

    exit_status: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_COMMAND_EXIT_STATUS:
            msg = "probe command exit status must be between zero and 255"
            raise ValueError(msg)
        if self.timed_out and self.exit_status != COMMAND_TIMEOUT_EXIT_STATUS:
            msg = "timed-out probe command requires status 124"
            raise ValueError(msg)


class ProbeCommands(XrandrEvidenceSource, Protocol):
    """Injected read-only XRandR evidence plus one exact mutation boundary."""

    def activate(self, arguments: tuple[str, ...]) -> ProbeCommandResult:
        """Execute the already-validated activation argument array once."""
        ...


@final
class SubprocessProbeCommands:
    """Production argument-array XRandR adapter with bounded command execution."""

    def __init__(self, reader: CommandRunner | None = None) -> None:
        """Inject only the read-command runner used by parser evidence."""
        self._reader = BoundedCommandRunner() if reader is None else reader

    def query(self) -> TextCommandEvidence:
        """Fresh-sample the documented XRandR topology query."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_QUERY,
                RawEvidenceSource.XRANDR_QUERY,
                "probe:xrandr --query",
            )
        )

    def properties(self) -> TextCommandEvidence:
        """Fresh-sample connector IDs from XRandR properties."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_PROPERTIES,
                RawEvidenceSource.XRANDR_PROPERTIES,
                "probe:xrandr --props",
            )
        )

    def activate(self, arguments: tuple[str, ...]) -> ProbeCommandResult:
        """Run only the caller's exact, already-validated activation array."""
        try:
            completed = subprocess.run(  # noqa: S603
                arguments,
                check=False,
                shell=False,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                timeout=PROBE_COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutExpired:
            return ProbeCommandResult(COMMAND_TIMEOUT_EXIT_STATUS, timed_out=True)
        except OSError:
            return ProbeCommandResult(COMMAND_NOT_FOUND_EXIT_STATUS)
        status = completed.returncode
        if status < 0:
            status = min(255, 128 + abs(status))
        return ProbeCommandResult(min(status, 255))


def run_probe_worker(  # noqa: PLR0913
    *,
    transaction_root: Path,
    action_id_text: str,
    unit_name: str,
    sysfs_root: Path,
    commands: ProbeCommands | None = None,
    drm_tree: ReadOnlyTree | None = None,
) -> int:
    """Validate, claim, guard, and run one exact keyed activation probe."""
    startup = validate_worker_startup(
        transaction_root=transaction_root,
        action_id_text=action_id_text,
        unit_name=unit_name,
        expected_kind=ActionKind.PROBE,
    )
    install_cooperative_sigterm_handler()
    selected_commands = SubprocessProbeCommands() if commands is None else commands
    selected_tree = RootedSysfsReader(sysfs_root) if drm_tree is None else drm_tree
    return execute_probe(startup, drm_tree=selected_tree, commands=selected_commands)


def execute_probe(
    startup: WorkerStartup,
    *,
    drm_tree: ReadOnlyTree,
    commands: ProbeCommands,
) -> int:
    """Execute one dependency-injected worker without consulting implicit I/O."""

    def topology_reader(request: TransactionRequest) -> CurrentTopology:
        _validate_execution_claim(startup)
        _raise_if_cancelled(startup)
        current = _sample_exact_probe_topology(request, drm_tree, commands)
        _raise_if_cancelled(startup)
        return current

    def implementation(request: TransactionRequest) -> WorkerExecution:
        _raise_if_cancelled(startup)
        output = _payload_text(request, "probe_output")
        mode = _payload_text(request, "preferred_mode")
        internal = _payload_text(request, "internal_output")
        arguments = (
            "xrandr",
            "--output",
            output,
            "--mode",
            mode,
            "--right-of",
            internal,
        )
        command = commands.activate(arguments)
        if command.timed_out:
            return WorkerExecution(
                ActionLifecycle.TIMED_OUT,
                command.exit_status,
                "xrandr activation timed out",
            )
        if command.exit_status != 0:
            return WorkerExecution(
                ActionLifecycle.FAILED,
                command.exit_status,
                f"xrandr activation exited with status {command.exit_status}",
            )
        return WorkerExecution(
            ActionLifecycle.COMPLETED,
            0,
            "exact preferred-mode activation probe completed",
        )

    return execute_worker(
        startup,
        topology_reader=topology_reader,
        implementation=implementation,
    )


def _sample_exact_probe_topology(
    request: TransactionRequest,
    drm_tree: ReadOnlyTree,
    commands: ProbeCommands,
) -> CurrentTopology:
    admitted_integrity = _validate_probe_authorization(request)
    begin_drm = sample_drm(drm_tree)
    xrandr = sample_xrandr(commands)
    end_drm = sample_drm(drm_tree)
    if begin_drm != end_drm:
        _stale("DRM evidence changed during the worker-local sample")
    if begin_drm.scan_state is not EvidenceState.AVAILABLE:
        _stale("DRM connector scan is not complete")
    if not xrandr.valid:
        _stale("XRandR query and properties evidence is invalid or torn")
    if any(
        item.kind is not ConnectorKind.VIRTUAL
        and (
            item.status_state is not EvidenceState.AVAILABLE
            or item.status is ConnectorStatus.UNKNOWN
        )
        for item in begin_drm.connectors
    ):
        _stale("DRM connector status evidence is uncertain")

    topology = derive_canonical_topology(begin_drm, xrandr)
    if topology.inconsistent:
        _stale("DRM and XRandR connector correspondence is not unique")
    if set(topology.kernel_connected_outputs) != set(topology.x_connected_outputs):
        _stale("kernel and X connected topologies differ")
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

    output = _payload_text(request, "probe_output")
    internal = _payload_text(request, "internal_output")
    preferred_mode = _payload_text(request, "preferred_mode")
    base_hash = _payload_text(request, "base_identity_hash")
    connected = tuple(
        item
        for item in begin_drm.connectors
        if item.connected and item.kind is not ConnectorKind.VIRTUAL
    )
    external = tuple(item for item in connected if item.kind is ConnectorKind.EXTERNAL)
    internal_connectors = tuple(
        item for item in connected if item.kind is ConnectorKind.INTERNAL
    )
    if (
        len(connected) != EXPECTED_CONNECTED_OUTPUTS
        or len(external) != 1
        or len(internal_connectors) != 1
        or topology.live_output_for(external[0].kernel_name) != output
        or topology.live_output_for(internal_connectors[0].kernel_name) != internal
        or topology.kernel_external_outputs != (output,)
        or topology.x_external_outputs != (output,)
        or set(topology.x_connected_outputs) != {output, internal}
        or topology.x_active_outputs != (internal,)
    ):
        _stale("probe no longer has one inactive external and one active internal")

    target = external[0]
    parsed = None if target.edid.parsed is None else target.edid.parsed
    matching_identity = tuple(
        item
        for item in connected
        if item.edid.parsed is not None and item.edid.parsed.base_hash == base_hash
    )
    if (
        parsed is None
        or parsed.base_hash != base_hash
        or parsed.integrity is not admitted_integrity
        or len(matching_identity) != 1
        or matching_identity[0].kernel_name != target.kernel_name
    ):
        _stale("unique checksum-valid base identity or broken extensions changed")

    target_x = _require_exact_target_identity(
        target,
        next((item for item in xrandr.outputs if item.name == output), None),
    )
    internal_x = next((item for item in xrandr.outputs if item.name == internal), None)
    if (
        not target_x.connected
        or target_x.active
        or target_x.preferred_modes != (preferred_mode,)
        or internal_x is None
        or not internal_x.connected
        or not internal_x.active
    ):
        _stale("inactive target, active internal, or preferred mode changed")
    return current


def _require_exact_target_identity(
    target: DrmConnector,
    target_x: XrandrOutput | None,
) -> XrandrOutput:
    """Reject same-name fallback: target authority requires fresh equal IDs."""
    if (
        target_x is None
        or target.connector_id.value is None
        or target_x.connector_id is None
        or target.connector_id.value != target_x.connector_id
    ):
        _stale("target DRM and X connector IDs are missing or differ")
    return target_x


def _validate_probe_authorization(request: TransactionRequest) -> EdidIntegrity:
    if request.action_kind is not ActionKind.PROBE or request.profile is None:
        _stale("request is not a profile-bound probe authorization")
    if frozenset(name for name, _value in request.payload) != _PROBE_PAYLOAD_FIELDS:
        _stale("probe request proof fields differ from the closed protocol")
    try:
        integrity = EdidIntegrity(_payload_text(request, "edid_integrity"))
    except ValueError:
        _stale("probe request extension integrity is invalid")
    if integrity not in BROKEN_EXTENSION_EDID_INTEGRITIES:
        _stale("probe request did not admit broken extensions")
    return integrity


def _payload_text(request: TransactionRequest, name: str) -> str:
    try:
        value = request.payload_value(name)
    except TransactionProtocolError as error:
        raise WorkerStartupError(str(error)) from error
    if not isinstance(value, str) or not value:
        _stale(f"probe request {name} is not non-empty text")
    return value


def _validate_execution_claim(startup: WorkerStartup) -> None:
    request = startup.request
    claim = startup.execution_claim
    if (
        claim is None
        or claim.record_kind is not BoundRecordKind.EXECUTION_CLAIM
        or claim.action_id != request.action_id
        or claim.action_kind is not ActionKind.PROBE
        or claim.unit_name != request.unit_name
        or claim.request_sha256 != request.request_sha256
    ):
        _stale("worker lacks its exact durable execution claim")


def _raise_if_cancelled(startup: WorkerStartup) -> None:
    if startup.store.stop_intent_if_present(startup.request.action_id) is not None:
        raise WorkerCancelled


def _stale(detail: str) -> Never:
    raise WorkerStartupError(detail)
