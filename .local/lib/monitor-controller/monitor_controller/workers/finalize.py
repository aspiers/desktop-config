# ruff: noqa: EM101, EM102, TRY003
"""Guarded disruptive desktop finalization from one exact prepared plan."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import TYPE_CHECKING, Final, Protocol, final

from monitor_controller.desktop.plan_codec import (
    AtomicPlanStore,
    DesktopPlanBundle,
    KeyboardDisposition,
    PlannedActionKind,
    hash_plan_bundle,
)
from monitor_controller.desktop.tray import (
    StableTray,
    TrayProbe,
    TrayReadinessError,
    wait_for_stable_tray,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    EventGeneration,
    RawEvidenceSource,
)
from monitor_controller.observer.drm import ReadOnlyTree, RootedSysfsReader
from monitor_controller.observer.xrandr import XrandrEvidenceSource
from monitor_controller.runtime.commands import (
    BoundedCommandRunner,
    CommandRequest,
    CommandRunner,
)
from monitor_controller.runtime.systemd import escape_unit_instance
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    TransactionProtocolError,
    TransactionRequest,
    TransactionStore,
    parse_action_id,
)
from monitor_controller.workers.common import (
    CommandResult,
    CurrentTopology,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    execute_worker,
    run_leaf_command,
    validate_worker_startup,
)
from monitor_controller.workers.common import (
    atomic_replace as _atomic_replace,
)
from monitor_controller.workers.common import (
    stale as _stale,
)
from monitor_controller.workers.desktop_guard import (
    sample_exact_desktop_topology,
    validate_plan_request_binding,
)

FINALIZATION_PROOF_MS: Final = 10_000
FINALIZE_COMMAND_TIMEOUT_SECONDS: Final = 120.0
TRAY_TIMEOUT_EXIT_STATUS: Final = 69
_XRANDR_QUERY = ("xrandr", "--query")
_XRANDR_PROPERTIES = ("xrandr", "--props")
_FINALIZATION_PAYLOAD_FIELDS: Final = frozenset(
    {
        "planning_action_id",
        "preparation_action_id",
        "proof_duration_ms",
    }
)
_PREPARATION_PAYLOAD_FIELDS: Final = frozenset(
    {"allow_temporary_edid_absence", "planning_action_id"}
)
_TRUSTED_PATH: Final = "/usr/bin:/bin"
ADVANTAGE_360_ADDRESS: Final = "DC:28:CC:C6:1E:C5"
_ACTIVE_UNIT_STATES: Final = frozenset(
    {"active", "activating", "deactivating", "reloading"}
)
_SAFE_INACTIVE_UNIT_STATES: Final = frozenset({"failed", "inactive"})
_DIAGNOSTIC_COMPONENT = re.compile(r"^[A-Za-z0-9+_.-]+$")

if TYPE_CHECKING:
    from monitor_controller.observer.evidence import TextCommandEvidence


@dataclass(frozen=True, slots=True)
class ApplyFluxboxConfiguration:
    """Install and live-reload the exact staged Fluxbox key configuration."""

    content: bytes


@dataclass(frozen=True, slots=True)
class ApplyKeyboardIntent:
    """Apply only the closed planned Advantage 360 disposition."""

    disposition: KeyboardDisposition


@dataclass(frozen=True, slots=True)
class ApplyWindowLayout:
    """Pass exact resolved window actions to ``ly`` without layout discovery."""

    content: bytes


@dataclass(frozen=True, slots=True)
class RestartFluxbox:
    """Request the existing transient-service-owned Fluxbox restart."""


@dataclass(frozen=True, slots=True)
class RestartXfcePanel:
    """Request one separately owned panel restart unit."""

    action_id: ActionId


@dataclass(frozen=True, slots=True)
class RestartNmApplet:
    """Restart the dedicated persistent nm-applet user service."""


@dataclass(frozen=True, slots=True)
class CaptureTrayDiagnostics:
    """Start diagnostics in a separate bounded user unit."""

    action_id: ActionId


type FinalizeOperation = (
    ApplyFluxboxConfiguration
    | ApplyKeyboardIntent
    | ApplyWindowLayout
    | RestartFluxbox
    | RestartXfcePanel
    | RestartNmApplet
    | CaptureTrayDiagnostics
)


# Shared bounded result shape; retained under the worker's historical name.
FinalizeCommandResult = CommandResult


class FinalizeCommands(XrandrEvidenceSource, Protocol):
    """Injected read-only topology/tray probes and typed mutation operations."""

    def apply(self, operation: FinalizeOperation) -> FinalizeCommandResult:
        """Run one exact closed operation."""
        ...

    def wait_for_stable_tray(self) -> StableTray:
        """Wait read-only for selection-owner and wrapper stability."""
        ...


class FinalizationFence(Protocol):
    """Fresh cross-process event and systemd mutator exclusion guard."""

    def current_event_generation(self) -> EventGeneration:
        """Read the latest producer-side DRM event generation."""
        ...

    def assert_no_other_mutator(self, request: TransactionRequest) -> None:
        """Fail unless the finalizer is the sole active display mutator."""
        ...


class FinalizeLeafRunner(Protocol):
    """Injected bounded no-shell command boundary."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float = FINALIZE_COMMAND_TIMEOUT_SECONDS,
    ) -> FinalizeCommandResult:
        """Run one command in a separately killable process session."""
        ...


@final
class SubprocessFinalizeLeafRunner:
    """Bounded leaf runner; cooperative unit stop does not signal its session."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float = FINALIZE_COMMAND_TIMEOUT_SECONDS,
    ) -> FinalizeCommandResult:
        """Run one leaf and clean its process session on timeout or failure."""
        return run_leaf_command(
            arguments,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )


class KeyboardConnectionProbe(Protocol):
    """Read-only query for whether the planned keyboard is connected."""

    def connected(self, address: str) -> bool | None:
        """Return the connection state, or None when bluez does not know it."""
        ...


@final
class BluetoothctlConnectionProbe:
    """Probe device connection via bounded read-only ``bluetoothctl info``."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        timeout_seconds: float = 10.0,
    ) -> None:
        """Bind the closed leaf environment and a bounded timeout."""
        self._environment = dict(environment)
        self._timeout_seconds = timeout_seconds

    def connected(self, address: str) -> bool | None:
        """Parse ``Connected: yes/no``; None for unknown devices or probe failure."""
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ("bluetoothctl", "info", address),  # noqa: S607 - PATH-resolved
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env=self._environment,
                start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped == "Connected: yes":
                return True
            if stripped == "Connected: no":
                return False
        return None


@final
class SubprocessFinalizeCommands:
    """Production adapter for the seven closed disruptive plan operations."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        home_root: Path,
        leaf_root: Path,
        work_root: Path,
        reader: CommandRunner | None = None,
        leaf_runner: FinalizeLeafRunner | None = None,
        tray_probe: TrayProbe | None = None,
        base_environment: Mapping[str, str] | None = None,
        keyboard_probe: KeyboardConnectionProbe | None = None,
    ) -> None:
        """Bind explicit home, leaf, work, evidence, command, and tray inputs."""
        for name, path in (
            ("home root", home_root),
            ("leaf root", leaf_root),
            ("work root", work_root),
        ):
            if not path.is_absolute():
                msg = f"finalize {name} must be absolute"
                raise ValueError(msg)
        self._home_root = home_root
        self._leaf_root = leaf_root
        self._work_root = work_root
        self._reader = BoundedCommandRunner() if reader is None else reader
        self._leaf_runner = (
            SubprocessFinalizeLeafRunner() if leaf_runner is None else leaf_runner
        )
        values = os.environ if base_environment is None else base_environment
        self._environment = _finalize_environment(values, home_root, leaf_root)
        self._tray_probe = (
            TrayProbe(display=self._environment.get("DISPLAY"))
            if tray_probe is None
            else tray_probe
        )
        self._keyboard_probe = (
            BluetoothctlConnectionProbe(environment=self._environment)
            if keyboard_probe is None
            else keyboard_probe
        )

    def query(self) -> TextCommandEvidence:
        """Fresh-sample exact X connected/active topology."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_QUERY,
                RawEvidenceSource.XRANDR_QUERY,
                "finalize:xrandr --query",
            )
        )

    def properties(self) -> TextCommandEvidence:
        """Fresh-sample X connector IDs for contradiction detection."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_PROPERTIES,
                RawEvidenceSource.XRANDR_PROPERTIES,
                "finalize:xrandr --props",
            )
        )

    def apply(  # noqa: PLR0911
        self, operation: FinalizeOperation
    ) -> FinalizeCommandResult:
        """Execute one operation without shell strings or orchestration fallback."""
        if isinstance(operation, ApplyFluxboxConfiguration):
            _atomic_replace(
                self._home_root / ".fluxbox" / "keys",
                operation.content,
            )
            return self._run(("fluxbox-remote", "Reconfigure"))
        if isinstance(operation, ApplyKeyboardIntent):
            return self._apply_keyboard_intent(operation)
        if isinstance(operation, ApplyWindowLayout):
            payload = self._private_temporary_file("window-actions-", operation.content)
            try:
                return self._run(
                    (
                        str(self._leaf_root / "run-with-local-X-display"),
                        str(self._leaf_root / "ly"),
                        "--resolved-actions",
                        str(payload),
                    )
                )
            finally:
                payload.unlink(missing_ok=True)
        if isinstance(operation, RestartFluxbox):
            return self._run(
                (
                    str(self._leaf_root / "run-with-local-X-display"),
                    str(self._leaf_root / "fluxbox-restart"),
                )
            )
        if isinstance(operation, RestartXfcePanel):
            # Escape like the dispatcher does: an unescaped dash in the
            # instance unescapes to '/' in %I, which mangled the action ID
            # every diagnostics run received (dc-ocx).
            instance = escape_unit_instance(operation.action_id.value)
            unit = f"monitor-panel-restart@{instance}.service"
            return self._run(("systemctl", "--user", "start", unit))
        if isinstance(operation, RestartNmApplet):
            return self._run(("systemctl", "--user", "restart", "nm-applet.service"))
        instance = escape_unit_instance(operation.action_id.value)
        unit = f"monitor-tray-diagnostics@{instance}.service"
        return self._run(("systemctl", "--user", "start", "--no-block", unit))

    def wait_for_stable_tray(self) -> StableTray:
        """Wait for both proven tray signals; absence is a hard finalization error."""
        return wait_for_stable_tray(self._tray_probe.sample)

    def _run(self, arguments: tuple[str, ...]) -> FinalizeCommandResult:
        return self._leaf_runner.run(arguments, environment=self._environment)

    def _apply_keyboard_intent(
        self,
        operation: ApplyKeyboardIntent,
    ) -> FinalizeCommandResult:
        """Converge on the planned keyboard state instead of replaying commands.

        The intent describes an end state, so a state that already holds is
        success: ``bluetoothctl disconnect`` exits 1 for a device that is not
        connected, which failed the first live unplug's finalization (dc-2in).
        A device bluez does not know at all is also a no-op in both
        directions — the legacy pipeline ran this best-effort, and failing
        forever over a missing pairing would wedge every finalization.
        """
        if operation.disposition is KeyboardDisposition.UNCHANGED:
            return FinalizeCommandResult(0)
        want_connected = (
            operation.disposition is KeyboardDisposition.CONNECT_ADVANTAGE_360
        )
        connected = self._keyboard_probe.connected(ADVANTAGE_360_ADDRESS)
        if connected is None:
            print(
                "keyboard intent skipped: bluez does not know "
                f"device {ADVANTAGE_360_ADDRESS}",
                file=sys.stderr,
            )
            return FinalizeCommandResult(0)
        if connected == want_connected:
            return FinalizeCommandResult(0)
        command = "connect" if want_connected else "disconnect"
        return self._run(("bluetoothctl", command, ADVANTAGE_360_ADDRESS))

    def _private_temporary_file(self, prefix: str, content: bytes) -> Path:
        self._work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._work_root.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix=prefix, dir=self._work_root)
        path = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        return path


@final
class FileSystemdFinalizationFence:
    """Production event-generation file plus exact user-unit activity checks."""

    def __init__(
        self,
        *,
        generation_file: Path,
        transaction_store: TransactionStore,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the exact generation record and transaction namespace."""
        if not generation_file.is_absolute():
            raise ValueError("event generation fence path must be absolute")
        self._generation_file = generation_file
        self._store = transaction_store
        values = os.environ if environment is None else environment
        self._environment = {
            key: value
            for key, value in values.items()
            if key in {"DBUS_SESSION_BUS_ADDRESS", "HOME", "XDG_RUNTIME_DIR"}
        }
        self._environment["PATH"] = _TRUSTED_PATH

    def current_event_generation(self) -> EventGeneration:
        """Read one strict regular no-follow generation record."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._generation_file, flags)
        except OSError as error:
            _stale(f"cannot open event-generation fence: {error}")
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                _stale("event-generation fence is not an owned regular file")
            content = os.read(descriptor, 64)
            if os.read(descriptor, 1):
                _stale("event-generation fence exceeds its size limit")
        finally:
            os.close(descriptor)
        try:
            text = content.decode("ascii", errors="strict")
        except UnicodeError as error:
            _stale(f"event-generation fence is malformed: {error}")
        if not text.endswith("\n") or not text[:-1].isdigit():
            _stale("event-generation fence is malformed")
        try:
            return EventGeneration(int(text[:-1]))
        except ValueError as error:
            _stale(f"event-generation fence is malformed: {error}")

    def assert_no_other_mutator(self, request: TransactionRequest) -> None:
        """Require manager visibility and no other active typed mutation unit."""
        own_state = self._unit_state(request.unit_name, absent_is_safe=False)
        if own_state not in _ACTIVE_UNIT_STATES:
            _stale("finalizer unit is not active in the user manager")
        for directory in self._store.action_directories():
            try:
                action_id = parse_action_id(directory.name)
                other = self._store.read_request(action_id)
            except (OSError, TransactionProtocolError) as error:
                _stale(f"cannot inspect display-mutator exclusion: {error}")
            if other.action_id == request.action_id:
                continue
            state = self._unit_state(other.unit_name, absent_is_safe=True)
            if state in _ACTIVE_UNIT_STATES:
                _stale(f"another display mutator remains active: {other.unit_name}")
            if state not in _SAFE_INACTIVE_UNIT_STATES and state != "absent":
                _stale(f"display mutator has unknown manager state: {other.unit_name}")

    def _unit_state(self, unit: str, *, absent_is_safe: bool) -> str:
        try:
            completed = subprocess.run(  # noqa: S603
                (
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=ActiveState",
                    "--value",
                ),
                check=False,
                capture_output=True,
                env=self._environment,
                text=True,
                timeout=5,
            )
        except (OSError, TimeoutExpired) as error:
            _stale(f"cannot query user-manager mutator state: {error}")
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            return value
        if absent_is_safe and completed.returncode != 0:
            return "absent"
        return _stale(f"cannot prove user-manager state for {unit}")


@dataclass(slots=True)
class DeferredCancellation:
    """Record SIGTERM while allowing the current bounded atomic step to finish."""

    requested: bool = False

    def install(self) -> None:
        """Install a non-throwing main-process handler for ``KillMode=mixed``."""

        def request(_signum: int, _frame: object) -> None:
            self.requested = True

        signal.signal(signal.SIGTERM, request)


def run_finalize_worker(  # noqa: PLR0913
    *,
    transaction_root: Path,
    plan_root: Path,
    event_generation_file: Path,
    action_id_text: str,
    unit_name: str,
    sysfs_root: Path,
    home_root: Path,
    leaf_root: Path,
    commands: FinalizeCommands | None = None,
    drm_tree: ReadOnlyTree | None = None,
    plan_store: AtomicPlanStore | None = None,
    fence: FinalizationFence | None = None,
    cancellation: DeferredCancellation | None = None,
) -> int:
    """Validate exact preparation and guards, then run seven ordered actions."""
    startup = validate_worker_startup(
        transaction_root=transaction_root,
        action_id_text=action_id_text,
        unit_name=unit_name,
        expected_kind=ActionKind.FINALIZATION,
    )
    selected_cancellation = (
        DeferredCancellation() if cancellation is None else cancellation
    )
    selected_cancellation.install()
    selected_commands = (
        SubprocessFinalizeCommands(
            home_root=home_root,
            leaf_root=leaf_root,
            work_root=transaction_root.parent / "finalize-work" / action_id_text,
        )
        if commands is None
        else commands
    )
    selected_tree = RootedSysfsReader(sysfs_root) if drm_tree is None else drm_tree
    selected_store = AtomicPlanStore(plan_root) if plan_store is None else plan_store
    selected_fence = (
        FileSystemdFinalizationFence(
            generation_file=event_generation_file,
            transaction_store=startup.store,
        )
        if fence is None
        else fence
    )
    return execute_finalization(
        startup,
        plan_store=selected_store,
        drm_tree=selected_tree,
        commands=selected_commands,
        fence=selected_fence,
        cancellation=selected_cancellation,
    )


def execute_finalization(  # noqa: C901, PLR0913
    startup: WorkerStartup,
    *,
    plan_store: AtomicPlanStore,
    drm_tree: ReadOnlyTree,
    commands: FinalizeCommands,
    fence: FinalizationFence,
    cancellation: DeferredCancellation,
) -> int:
    """Run exact actions with a complete fresh guard before every mutation."""
    guarded_bundle: DesktopPlanBundle | None = None

    def boundary() -> CurrentTopology:
        nonlocal guarded_bundle
        guarded_bundle = _validate_boundary(startup, plan_store, cancellation)
        current = sample_exact_desktop_topology(
            startup.request,
            guarded_bundle,
            drm_tree,
            commands,
            allow_temporary_edid_absence=False,
        )
        # Read the producer generation after the complete DRM/X sample, as
        # close as possible to the mutation. A hint arriving during sampling
        # therefore rejects this boundary rather than authorizing stale work.
        if (
            fence.current_event_generation()
            != startup.request.admitted_event_generation
        ):
            _stale("fresh event-generation fence differs from finalization admission")
        fence.assert_no_other_mutator(startup.request)
        _raise_if_cancelled(startup, cancellation)
        return current

    def topology_reader(_request: TransactionRequest) -> CurrentTopology:
        return boundary()

    def implementation(  # noqa: C901
        _request: TransactionRequest,
    ) -> WorkerExecution:
        nonlocal guarded_bundle
        bundle = guarded_bundle
        if bundle is None:
            _stale("staged plan was not validated before finalization")
        for index, action in enumerate(bundle.plan.finalize_actions):
            if index:
                boundary()
                bundle = guarded_bundle
                if bundle is None:
                    _stale("staged plan disappeared at a finalization boundary")
            _raise_if_cancelled(startup, cancellation)
            if action.kind is PlannedActionKind.RESTART_NM_APPLET:
                try:
                    commands.wait_for_stable_tray()
                except TrayReadinessError as error:
                    return WorkerExecution(
                        ActionLifecycle.FAILED,
                        TRAY_TIMEOUT_EXIT_STATUS,
                        f"stable tray readiness failed: {error}",
                    )
                # Waiting is deliberately read-only. Re-prove every authority
                # immediately before the applet service restart.
                boundary()
                bundle = guarded_bundle
                if bundle is None:
                    _stale("staged plan disappeared after tray readiness")
                _raise_if_cancelled(startup, cancellation)
            operation = _operation(bundle, action.kind, startup.request.action_id)
            result = commands.apply(operation)
            # A durable cancellation which arrived during an atomic restart
            # wins only after the bounded command has returned.
            _raise_if_cancelled(startup, cancellation)
            if result.timed_out:
                return WorkerExecution(
                    ActionLifecycle.TIMED_OUT,
                    result.exit_status,
                    f"{action.kind.value} timed out",
                )
            if result.exit_status != 0:
                return WorkerExecution(
                    ActionLifecycle.FAILED,
                    result.exit_status,
                    f"{action.kind.value} exited with status {result.exit_status}",
                )
        request = startup.request
        transition = request.transition_id
        plan_hash = request.plan_hash
        if transition is None or plan_hash is None:
            _stale("finalization completion lost transition or plan identity")
        return WorkerExecution(
            ActionLifecycle.COMPLETED,
            0,
            (
                f"finalized {len(bundle.plan.finalize_actions)} ordered actions for "
                f"{transition.value} plan {plan_hash.value}; awaiting observation"
            ),
        )

    return execute_worker(
        startup,
        topology_reader=topology_reader,
        implementation=implementation,
    )


def _validate_boundary(
    startup: WorkerStartup,
    plan_store: AtomicPlanStore,
    cancellation: DeferredCancellation,
) -> DesktopPlanBundle:
    _raise_if_cancelled(startup, cancellation)
    request = startup.request
    try:
        current_request = startup.store.read_request(request.action_id)
        current_claim = startup.store.read_execution_claim(request.action_id)
    except (OSError, TransactionProtocolError) as error:
        _stale(f"cannot revalidate finalization request or claim: {error}")
    claim = startup.execution_claim
    if current_request != request:
        _stale("immutable finalization request changed during execution")
    if (
        claim is None
        or current_claim != claim
        or claim.record_kind is not BoundRecordKind.EXECUTION_CLAIM
        or claim.action_id != request.action_id
        or claim.action_kind is not ActionKind.FINALIZATION
        or claim.unit_name != request.unit_name
        or claim.request_sha256 != request.request_sha256
    ):
        _stale("worker lacks its exact durable finalization execution claim")
    planning_action, preparation_action, proof_duration = _finalization_payload(request)
    if proof_duration < FINALIZATION_PROOF_MS:
        _stale("finalization request lacks ten seconds of continuous proof")
    try:
        bundle = plan_store.read(planning_action)
    except (OSError, ValueError) as error:
        _stale(f"cannot read exact staged desktop plan: {error}")
    if request.plan_hash is None or hash_plan_bundle(bundle) != request.plan_hash:
        _stale("staged desktop plan hash differs from finalization request")
    validate_plan_request_binding(request, planning_action, bundle)
    _validate_prepared_transition(
        startup.store,
        request,
        planning_action,
        preparation_action,
    )
    _raise_if_cancelled(startup, cancellation)
    return bundle


def _validate_prepared_transition(
    store: TransactionStore,
    finalization: TransactionRequest,
    planning_action: ActionId,
    preparation_action: ActionId,
) -> None:
    try:
        prepared = store.read_request(preparation_action)
        result = store.read_result(preparation_action)
    except (OSError, TransactionProtocolError) as error:
        _stale(f"matching prepared transition is unavailable: {error}")
    values = dict(prepared.payload)
    if frozenset(values) != _PREPARATION_PAYLOAD_FIELDS:
        _stale("prepared transition payload differs from closed protocol")
    temporary_absence = values.get("allow_temporary_edid_absence")
    if (
        values.get("planning_action_id") != planning_action.value
        or not isinstance(temporary_absence, bool)
        or not temporary_absence
        or prepared.action_kind is not ActionKind.PREPARATION
        or prepared.transition_id != finalization.transition_id
        or prepared.transition_key != finalization.transition_key
        or prepared.plan_hash != finalization.plan_hash
        or prepared.profile != finalization.profile
        or prepared.layout != finalization.layout
        or prepared.physical_epoch != finalization.physical_epoch
        or prepared.physical_token != finalization.physical_token
        or prepared.observation_key != finalization.observation_key
        or prepared.output_mapping != finalization.output_mapping
        or prepared.expected_topology != finalization.expected_topology
        or prepared.admitted_event_generation > finalization.admitted_event_generation
        or result.outcome is not ActionLifecycle.COMPLETED
        or result.exit_status != 0
        or result.plan_hash != finalization.plan_hash
    ):
        _stale("prepared transition does not exactly authorize finalization")


def _finalization_payload(
    request: TransactionRequest,
) -> tuple[ActionId, ActionId, int]:
    values = dict(request.payload)
    if frozenset(values) != _FINALIZATION_PAYLOAD_FIELDS:
        _stale("finalization request payload differs from closed protocol")
    planning = values["planning_action_id"]
    preparation = values["preparation_action_id"]
    duration = values["proof_duration_ms"]
    if (
        not isinstance(planning, str)
        or not isinstance(preparation, str)
        or not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
    ):
        _stale("finalization proof or prepared identities have wrong types")
    try:
        planning_action = parse_action_id(planning)
        preparation_action = parse_action_id(preparation)
    except TransactionProtocolError as error:
        _stale(f"finalization staged action identity is malformed: {error}")
    if (
        planning_action.kind is not ActionKind.PLAN
        or preparation_action.kind is not ActionKind.PREPARATION
        or planning_action.controller_instance != request.action_id.controller_instance
        or preparation_action.controller_instance
        != request.action_id.controller_instance
    ):
        _stale("finalization staged action identities have wrong kinds or instance")
    return planning_action, preparation_action, duration


def _operation(  # noqa: PLR0911
    bundle: DesktopPlanBundle,
    kind: PlannedActionKind,
    action_id: ActionId,
) -> FinalizeOperation:
    plan = bundle.plan
    artifacts = {item.relative_path: item.content for item in bundle.artifacts}
    if kind is PlannedActionKind.APPLY_FLUXBOX_CONFIGURATION:
        return ApplyFluxboxConfiguration(artifacts[plan.fluxbox.rendered_keys_artifact])
    if kind is PlannedActionKind.APPLY_KEYBOARD_INTENT:
        return ApplyKeyboardIntent(plan.keyboard.disposition)
    if kind is PlannedActionKind.APPLY_WINDOW_LAYOUT:
        return ApplyWindowLayout(artifacts[plan.windows.actions_artifact])
    if kind is PlannedActionKind.RESTART_FLUXBOX:
        return RestartFluxbox()
    if kind is PlannedActionKind.RESTART_XFCE_PANEL:
        return RestartXfcePanel(action_id)
    if kind is PlannedActionKind.RESTART_NM_APPLET:
        return RestartNmApplet()
    if kind is PlannedActionKind.CAPTURE_TRAY_DIAGNOSTICS:
        return CaptureTrayDiagnostics(action_id)
    return _stale(f"forbidden non-finalize action appeared in finalize phase: {kind}")


def _raise_if_cancelled(
    startup: WorkerStartup,
    cancellation: DeferredCancellation,
) -> None:
    if (
        cancellation.requested
        or startup.store.stop_intent_if_present(startup.request.action_id) is not None
    ):
        raise WorkerCancelled


def _finalize_environment(
    values: Mapping[str, str],
    home_root: Path,
    leaf_root: Path,
) -> dict[str, str]:
    runtime = values.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment = {
        "DBUS_SESSION_BUS_ADDRESS": values.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus"
        ),
        "HOME": str(home_root),
        "PATH": f"{leaf_root}:{_TRUSTED_PATH}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "XDG_RUNTIME_DIR": runtime,
        "ZDOTDIR": str(home_root),
    }
    for name in ("DISPLAY", "HOST", "HOSTNAME", "XAUTHORITY"):
        value = values.get(name)
        if value:
            environment[name] = value
    return environment


def run_tray_diagnostics(  # noqa: PLR0913
    *,
    transaction_root: Path,
    action_id_text: str,
    tray_diag: Path,
    output_root: Path,
    delay_seconds: float = 5.0,
    timeout_seconds: float = 30.0,
) -> int:
    """Capture delayed diagnostics in this separate bounded unit process."""
    try:
        action_id = parse_action_id(action_id_text)
    except TransactionProtocolError as error:
        message = f"invalid diagnostic action identity: {error}"
        raise WorkerStartupError(message) from error
    if action_id.kind is not ActionKind.FINALIZATION:
        raise WorkerStartupError("tray diagnostics require a finalization action")
    store = TransactionStore(transaction_root)
    try:
        request = store.read_request(action_id)
        store.read_submission_claim(action_id)
        store.read_execution_claim(action_id)
    except (OSError, TransactionProtocolError) as error:
        raise WorkerStartupError(
            f"cannot validate diagnostic finalization request: {error}"
        ) from error
    if request.action_kind is not ActionKind.FINALIZATION:
        raise WorkerStartupError("diagnostic request is not a finalization")
    layout = request.layout or "unknown"
    if _DIAGNOSTIC_COMPONENT.fullmatch(layout) is None:
        raise WorkerStartupError("diagnostic layout is unsafe for a filename")
    time.sleep(delay_seconds)
    output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_root.chmod(0o700)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    output = output_root / f"{timestamp}.{layout}.{action_id.sequence}.txt"
    runner = SubprocessFinalizeLeafRunner()
    result = runner.run(
        (str(tray_diag), str(output)),
        environment=_finalize_environment(os.environ, Path.home(), tray_diag.parent),
        timeout_seconds=timeout_seconds,
    )
    return result.exit_status


