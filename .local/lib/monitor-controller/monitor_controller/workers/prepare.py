"""Guarded, plan-hash-bound, cooperatively cancellable desktop preparation."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final, Never, Protocol, final

from monitor_controller.desktop.plan_codec import (
    AtomicPlanStore,
    DesktopPlanBundle,
    DpiIntent,
    EmacsFontIntent,
    PanelIntent,
    PlannedActionKind,
    TerminalThemeIntent,
    hash_plan_bundle,
)
from monitor_controller.desktop.planner import ConfigurationInput, InputRole
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ConfigurationContentHash,
    RawEvidenceSource,
)
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import (
    ConnectorKind,
    ConnectorStatus,
    EvidenceState,
    ReadOnlyTree,
    RootedSysfsReader,
    sample_drm,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import XrandrEvidenceSource, sample_xrandr
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
    parse_action_id,
)
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    execute_worker,
    install_cooperative_sigterm_handler,
    kill_process_group,
    validate_topology_guard,
    validate_worker_startup,
)
from monitor_controller.workers.identity import validate_noncontradictory_edids

PREPARE_COMMAND_TIMEOUT_SECONDS: Final = 90.0
COMMAND_NOT_FOUND_EXIT_STATUS: Final = 127
COMMAND_TIMEOUT_EXIT_STATUS: Final = 124
MAX_COMMAND_EXIT_STATUS: Final = 255
_XRANDR_QUERY = ("xrandr", "--query")
_XRANDR_PROPERTIES = ("xrandr", "--props")
_PREPARATION_PAYLOAD_FIELDS: Final = frozenset(
    {"allow_temporary_edid_absence", "planning_action_id"}
)
_TRUSTED_PATH: Final = "/usr/bin:/bin"


@dataclass(frozen=True, slots=True)
class PrepareLeafBinding:
    """One captured live helper which must match before staged execution."""

    logical_path: str
    roles: tuple[InputRole, ...]
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class InstallFluxboxOverlay:
    """Install the exact staged overlay bytes."""

    content: bytes


@dataclass(frozen=True, slots=True)
class SetPanelProperties:
    """Apply the exact ordered panel property tuples."""

    panels: tuple[PanelIntent, ...]
    leaves: tuple[PrepareLeafBinding, ...]


@dataclass(frozen=True, slots=True)
class SetXfceDpi:
    """Apply an exact DPI value or the planned explicit no-op."""

    intent: DpiIntent
    leaves: tuple[PrepareLeafBinding, ...]


@dataclass(frozen=True, slots=True)
class ConfigureTerminals:
    """Apply exact terminal font/theme values and staged kitty bytes."""

    intent: TerminalThemeIntent
    kitty_theme: bytes
    leaves: tuple[PrepareLeafBinding, ...]


@dataclass(frozen=True, slots=True)
class ReloadEmacsFonts:
    """Reload one staged verified helper and apply the exact planned height."""

    intent: EmacsFontIntent
    leaves: tuple[PrepareLeafBinding, ...]


@dataclass(frozen=True, slots=True)
class GenerateFluxboxConfiguration:
    """Install exact planning-rendered keys bytes without live reconfigure."""

    content: bytes


type PrepareOperation = (
    InstallFluxboxOverlay
    | SetPanelProperties
    | SetXfceDpi
    | ConfigureTerminals
    | ReloadEmacsFonts
    | GenerateFluxboxConfiguration
)


@dataclass(frozen=True, slots=True)
class PrepareCommandResult:
    """Bounded status from one typed preparation leaf operation."""

    exit_status: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_COMMAND_EXIT_STATUS:
            msg = "prepare command exit status must be between zero and 255"
            raise ValueError(msg)
        if self.timed_out and self.exit_status != COMMAND_TIMEOUT_EXIT_STATUS:
            msg = "timed-out prepare command requires status 124"
            raise ValueError(msg)


class PrepareCommands(XrandrEvidenceSource, Protocol):
    """Injected read-only topology evidence and typed leaf mutation boundary."""

    def apply(self, operation: PrepareOperation) -> PrepareCommandResult:
        """Execute one already-validated typed operation exactly once."""
        ...


class PrepareLeafRunner(Protocol):
    """Injected bounded argument-array subprocess boundary."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        timeout_seconds: float = PREPARE_COMMAND_TIMEOUT_SECONDS,
    ) -> PrepareCommandResult:
        """Run one exact leaf command without a shell."""
        ...


@final
class SubprocessPrepareLeafRunner:
    """Bounded process-group runner which cleans descendants on timeout/cancel."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        timeout_seconds: float = PREPARE_COMMAND_TIMEOUT_SECONDS,
    ) -> PrepareCommandResult:
        """Run one leaf in a separately killable process group."""
        try:
            process = subprocess.Popen(  # noqa: S603
                arguments,
                env=dict(environment),
                shell=False,
                start_new_session=True,
                stderr=subprocess.DEVNULL,
                stdin=(
                    subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL
                ),
                stdout=subprocess.DEVNULL,
            )
        except OSError:
            return PrepareCommandResult(COMMAND_NOT_FOUND_EXIT_STATUS)
        try:
            try:
                _stdout, _stderr = process.communicate(
                    input=input_bytes,
                    timeout=timeout_seconds,
                )
            except TimeoutExpired:
                kill_process_group(process)
                process.communicate()
                return PrepareCommandResult(COMMAND_TIMEOUT_EXIT_STATUS, timed_out=True)
        except BaseException:
            kill_process_group(process)
            with contextlib.suppress(OSError):
                process.communicate()
            raise
        status = process.returncode
        if status < 0:
            status = min(MAX_COMMAND_EXIT_STATUS, 128 + abs(status))
        return PrepareCommandResult(min(status, MAX_COMMAND_EXIT_STATUS))


@final
class SubprocessPrepareCommands:
    """Production exact-value leaves plus fresh XRandR evidence sampling."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        home_root: Path,
        leaf_root: Path,
        work_root: Path,
        reader: CommandRunner | None = None,
        leaf_runner: PrepareLeafRunner | None = None,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        """Bind explicit home, leaf, work, evidence, and command capabilities."""
        for name, path in (
            ("home root", home_root),
            ("leaf root", leaf_root),
            ("work root", work_root),
        ):
            if not path.is_absolute():
                msg = f"prepare {name} must be absolute"
                raise ValueError(msg)
        self._home_root = home_root
        self._leaf_root = leaf_root
        self._work_root = work_root
        self._reader = BoundedCommandRunner() if reader is None else reader
        self._leaf_runner = (
            SubprocessPrepareLeafRunner() if leaf_runner is None else leaf_runner
        )
        # Accepted only so tests can prove inherited variables have no authority.
        del base_environment

    def query(self) -> TextCommandEvidence:
        """Fresh-sample exact X connected/active topology."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_QUERY,
                RawEvidenceSource.XRANDR_QUERY,
                "prepare:xrandr --query",
            )
        )

    def properties(self) -> TextCommandEvidence:
        """Fresh-sample X connector IDs for contradiction detection."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_PROPERTIES,
                RawEvidenceSource.XRANDR_PROPERTIES,
                "prepare:xrandr --props",
            )
        )

    def apply(self, operation: PrepareOperation) -> PrepareCommandResult:
        """Execute exactly one closed operation with no orchestration fallback."""
        if isinstance(operation, InstallFluxboxOverlay):
            _atomic_replace(
                self._home_root / ".fluxbox" / "overlay",
                operation.content,
            )
            return PrepareCommandResult(0)
        if isinstance(operation, GenerateFluxboxConfiguration):
            _atomic_replace(
                self._home_root / ".fluxbox" / "keys",
                operation.content,
            )
            return PrepareCommandResult(0)
        with self._stage_verified_leaves(operation.leaves) as staged:
            environment = self._environment(staged)
            if isinstance(operation, SetPanelProperties):
                arguments = [str(staged / "setup-panels"), "--exact"]
                for panel in operation.panels:
                    arguments.extend(
                        (
                            str(panel.panel),
                            panel.output,
                            panel.position,
                            str(panel.length),
                            "-" if panel.size is None else str(panel.size),
                        )
                    )
                return self._leaf_runner.run(tuple(arguments), environment=environment)
            if isinstance(operation, SetXfceDpi):
                value = (
                    "unchanged"
                    if operation.intent.value is None
                    else str(operation.intent.value)
                )
                return self._leaf_runner.run(
                    (str(staged / "set-layout-dpi"), "--exact", value),
                    environment=environment,
                )
            if isinstance(operation, ConfigureTerminals):
                theme_path = self._private_temporary_file(operation.kitty_theme)
                try:
                    intent = operation.intent
                    return self._leaf_runner.run(
                        (
                            str(staged / "setup-terminals"),
                            "--exact",
                            intent.theme,
                            intent.gnome_profile,
                            intent.xfce_theme,
                            intent.medium_font_name,
                            str(intent.medium_font_size),
                            str(theme_path),
                        ),
                        environment=environment,
                    )
                finally:
                    theme_path.unlink(missing_ok=True)
            helper = staged / "monitor-controller-emacs-fonts.el"
            expression = (
                f"(progn (load-file {json.dumps(str(helper))}) "
                f"({operation.intent.expression} {operation.intent.font_height}))"
            )
            return self._leaf_runner.run(
                ("emacsclient", "-e", expression),
                environment=environment,
            )

    def _environment(self, staged: Path) -> dict[str, str]:
        runtime = f"/run/user/{os.getuid()}"
        return {
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
            "HOME": str(self._home_root),
            "MONITOR_CONTROLLER_LEAF_BIN": str(staged),
            "PATH": _TRUSTED_PATH,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "XDG_CONFIG_HOME": str(self._home_root / ".config"),
            "XDG_RUNTIME_DIR": runtime,
        }

    @contextmanager
    def _stage_verified_leaves(
        self,
        leaves: tuple[PrepareLeafBinding, ...],
    ) -> Generator[Path]:
        self._work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._work_root.chmod(0o700)
        with tempfile.TemporaryDirectory(
            prefix="leaf-bin-",
            dir=self._work_root,
        ) as temporary:
            staged = Path(temporary)
            staged.chmod(0o700)
            for leaf in leaves:
                name = Path(leaf.logical_path).name
                source = self._leaf_root / name
                try:
                    content = source.read_bytes()
                except OSError as error:
                    _stale(
                        f"captured preparation helper is unavailable: {name}: {error}"
                    )
                actual = ConfigurationInput(leaf.roles, leaf.logical_path, content)
                if actual.content_hash.sha256 != leaf.expected_sha256:
                    _stale(f"captured preparation helper changed: {name}")
                _atomic_replace(staged / name, content, mode=0o700)
            yield staged

    def _private_temporary_file(self, content: bytes) -> Path:
        self._work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._work_root.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix="kitty-theme-", dir=self._work_root)
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


def run_prepare_worker(  # noqa: PLR0913
    *,
    transaction_root: Path,
    plan_root: Path,
    action_id_text: str,
    unit_name: str,
    sysfs_root: Path,
    home_root: Path,
    leaf_root: Path,
    commands: PrepareCommands | None = None,
    drm_tree: ReadOnlyTree | None = None,
    plan_store: AtomicPlanStore | None = None,
) -> int:
    """Validate, claim, guard each boundary, and execute one staged plan."""
    startup = validate_worker_startup(
        transaction_root=transaction_root,
        action_id_text=action_id_text,
        unit_name=unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    install_cooperative_sigterm_handler()
    selected_commands = (
        SubprocessPrepareCommands(
            home_root=home_root,
            leaf_root=leaf_root,
            work_root=transaction_root.parent / "prepare-work" / action_id_text,
        )
        if commands is None
        else commands
    )
    selected_tree = RootedSysfsReader(sysfs_root) if drm_tree is None else drm_tree
    selected_store = AtomicPlanStore(plan_root) if plan_store is None else plan_store
    return execute_preparation(
        startup,
        plan_store=selected_store,
        drm_tree=selected_tree,
        commands=selected_commands,
    )


def execute_preparation(
    startup: WorkerStartup,
    *,
    plan_store: AtomicPlanStore,
    drm_tree: ReadOnlyTree,
    commands: PrepareCommands,
) -> int:
    """Run the six typed actions in plan order with a guard before every one."""
    guarded_bundle: DesktopPlanBundle | None = None

    def topology_reader(_request: TransactionRequest) -> CurrentTopology:
        nonlocal guarded_bundle
        guarded_bundle = _validate_boundary(startup, plan_store, drm_tree, commands)
        return _sample_exact_preparation_topology(
            startup,
            guarded_bundle,
            drm_tree,
            commands,
        )

    def implementation(_request: TransactionRequest) -> WorkerExecution:
        nonlocal guarded_bundle
        bundle = guarded_bundle
        if bundle is None:
            _stale("staged plan was not validated before preparation")
        for index, action in enumerate(bundle.plan.prepare_actions):
            if index:
                bundle = _validate_boundary(startup, plan_store, drm_tree, commands)
                _sample_exact_preparation_topology(
                    startup,
                    bundle,
                    drm_tree,
                    commands,
                )
            _raise_if_cancelled(startup)
            operation = _operation(bundle, action.kind)
            result = commands.apply(operation)
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
            _raise_if_cancelled(startup)
        request = startup.request
        transition = request.transition_id
        plan_hash = request.plan_hash
        if transition is None or plan_hash is None:
            _stale("preparation completion lost transition or plan identity")
        return WorkerExecution(
            ActionLifecycle.COMPLETED,
            0,
            (
                f"prepared {len(bundle.plan.prepare_actions)} ordered actions for "
                f"{transition.value} plan {plan_hash.value}"
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
    drm_tree: ReadOnlyTree,
    commands: PrepareCommands,
) -> DesktopPlanBundle:
    del drm_tree, commands
    _raise_if_cancelled(startup)
    request = startup.request
    try:
        current_request = startup.store.read_request(request.action_id)
        current_claim = startup.store.read_execution_claim(request.action_id)
    except (OSError, TransactionProtocolError) as error:
        _stale(f"cannot revalidate request or execution claim: {error}")
    if current_request != request:
        _stale("immutable preparation request changed during execution")
    claim = startup.execution_claim
    if (
        claim is None
        or current_claim != claim
        or claim.record_kind is not BoundRecordKind.EXECUTION_CLAIM
        or claim.action_id != request.action_id
        or claim.action_kind is not ActionKind.PREPARATION
        or claim.unit_name != request.unit_name
        or claim.request_sha256 != request.request_sha256
    ):
        _stale("worker lacks its exact durable preparation execution claim")
    planning_action, _allow_absence = _preparation_payload(request)
    try:
        bundle = plan_store.read(planning_action)
    except (OSError, ValueError) as error:
        _stale(f"cannot read exact staged desktop plan: {error}")
    if request.plan_hash is None or hash_plan_bundle(bundle) != request.plan_hash:
        _stale("staged desktop plan hash differs from preparation request")
    _validate_plan_request_binding(request, planning_action, bundle)
    _raise_if_cancelled(startup)
    return bundle


def _validate_plan_request_binding(
    request: TransactionRequest,
    planning_action: ActionId,
    bundle: DesktopPlanBundle,
) -> None:
    plan = bundle.plan
    guards = plan.guards
    topology = ExpectedTopology(
        guards.topology.kernel_connected_outputs,
        guards.topology.kernel_external_outputs,
        guards.topology.x_connected_outputs,
        guards.topology.x_active_outputs,
    )
    transition_key = (
        f"{guards.input_key.physical_epoch}|{guards.profile}|"
        f"{guards.observation_key.value}"
    )
    if (
        guards.action_id != planning_action
        or request.transition_id != guards.transition_id
        or request.transition_key is None
        or request.transition_key.value != transition_key
        or request.profile != guards.profile
        or request.layout != guards.layout
        or request.physical_epoch != guards.input_key.physical_epoch
        or request.admitted_event_generation < guards.admitted_event_generation
        or request.physical_token != guards.physical_token
        or request.observation_key != guards.observation_key
        or request.output_mapping != guards.output_mapping
        or request.expected_topology != topology
    ):
        _stale("preparation request differs from staged transition guards")


def _sample_exact_preparation_topology(
    startup: WorkerStartup,
    bundle: DesktopPlanBundle,
    drm_tree: ReadOnlyTree,
    commands: PrepareCommands,
) -> CurrentTopology:
    request = startup.request
    begin_drm = sample_drm(drm_tree)
    xrandr = sample_xrandr(commands)
    end_drm = sample_drm(drm_tree)
    if begin_drm != end_drm:
        _stale("DRM evidence changed during preparation boundary sample")
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
        _stale("DRM and X connector identity is contradictory or non-unique")
    if set(topology.kernel_connected_outputs) != set(topology.x_connected_outputs):
        _stale("kernel and X connected topologies differ")
    current = CurrentTopology(
        topology.physical_token,
        ExpectedTopology(
            topology.kernel_connected_outputs,
            topology.kernel_external_outputs,
            topology.x_connected_outputs,
            topology.x_active_outputs,
        ),
    )
    validate_topology_guard(request, current)
    profile = _staged_profile(request, bundle)
    saved_to_live = {
        item.saved_output: item.live_output for item in request.output_mapping
    }
    if set(saved_to_live) != {item.output for item in profile.setup}:
        _stale("staged setup outputs differ from admitted output mapping")
    patterns = {saved_to_live[item.output]: item.value for item in profile.setup}
    _planning_action, allow_absence = _preparation_payload(request)
    validate_noncontradictory_edids(
        patterns,
        begin_drm.connectors,
        topology,
        allow_temporary_absence=allow_absence,
    )
    return current


def _staged_profile(
    request: TransactionRequest,
    bundle: DesktopPlanBundle,
) -> SavedAutorandrProfile:
    artifacts = {item.relative_path: item.content for item in bundle.artifacts}
    intent = bundle.plan.autorandr
    try:
        parsed = parse_saved_profile(
            request.profile or "",
            _profile_evidence(
                intent.config_artifact,
                artifacts[intent.config_artifact],
            ),
            _profile_evidence(
                intent.setup_artifact,
                artifacts[intent.setup_artifact],
            ),
            (
                None
                if intent.layout_artifact is None
                else _profile_evidence(
                    intent.layout_artifact,
                    artifacts[intent.layout_artifact],
                )
            ),
        )
    except (KeyError, UnicodeDecodeError) as error:
        _stale(f"staged autorandr identity artifacts are invalid: {error}")
    if not parsed.valid or parsed.profile is None:
        reasons = ",".join(item.code.value for item in parsed.issues)
        _stale(f"staged autorandr identity grammar is invalid: {reasons}")
    return parsed.profile


def _profile_evidence(path: str, content: bytes) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        f"staged-plan:{path}",
        content.decode("utf-8", errors="strict"),
    )


def _preparation_payload(request: TransactionRequest) -> tuple[ActionId, bool]:
    values = dict(request.payload)
    if frozenset(values) != _PREPARATION_PAYLOAD_FIELDS:
        _stale("preparation request payload differs from closed protocol")
    planning_text = values["planning_action_id"]
    allow_absence = values["allow_temporary_edid_absence"]
    if not isinstance(planning_text, str) or not isinstance(allow_absence, bool):
        _stale("preparation request plan identity or EDID policy has wrong type")
    try:
        action_id = parse_action_id(planning_text)
    except TransactionProtocolError as error:
        _stale(f"preparation planning action identity is malformed: {error}")
    if action_id.kind is not ActionKind.PLAN:
        _stale("preparation request does not reference a planning action")
    return action_id, allow_absence


def _operation(  # noqa: PLR0911 - closed six-action typed dispatch
    bundle: DesktopPlanBundle,
    kind: PlannedActionKind,
) -> PrepareOperation:
    plan = bundle.plan
    artifacts = {item.relative_path: item.content for item in bundle.artifacts}
    if kind is PlannedActionKind.INSTALL_FLUXBOX_OVERLAY:
        return InstallFluxboxOverlay(artifacts[plan.overlay.artifact_path])
    if kind is PlannedActionKind.SET_PANEL_PROPERTIES:
        return SetPanelProperties(
            plan.panels,
            _leaf_bindings(
                plan.panels[0].policy_hashes,
                (("bin/setup-panels", InputRole.PANEL_POLICY),),
            ),
        )
    if kind is PlannedActionKind.SET_XFCE_DPI:
        return SetXfceDpi(
            plan.dpi,
            _leaf_bindings(
                plan.dpi.policy_hashes,
                (
                    ("bin/set-layout-dpi", InputRole.DPI_POLICY),
                    ("bin/set-xfce4-dpi", InputRole.DPI_POLICY),
                ),
            ),
        )
    if kind is PlannedActionKind.CONFIGURE_TERMINALS:
        return ConfigureTerminals(
            plan.terminal,
            artifacts[plan.terminal.kitty_theme_artifact],
            _leaf_bindings(
                plan.terminal.policy_hashes,
                tuple(
                    (f"bin/{name}", InputRole.TERMINAL_POLICY)
                    for name in (
                        "setup-terminals",
                        "gnome-terminal-config",
                        "gnome-terminal-profile",
                        "xfce4-terminal-config",
                        "kitty-theme-config",
                    )
                ),
            ),
        )
    if kind is PlannedActionKind.RELOAD_EMACS_FONTS:
        return ReloadEmacsFonts(
            plan.emacs,
            _leaf_bindings(
                plan.emacs.policy_hashes,
                (
                    (
                        "bin/monitor-controller-emacs-fonts.el",
                        InputRole.EMACS_POLICY,
                    ),
                ),
            ),
        )
    if kind is PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION:
        return GenerateFluxboxConfiguration(
            artifacts[plan.fluxbox.rendered_keys_artifact]
        )
    return _stale(
        f"forbidden non-prepare action appeared in prepare phase: {kind.value}"
    )


def _leaf_bindings(
    hashes: tuple[ConfigurationContentHash, ...],
    specifications: tuple[tuple[str, InputRole], ...],
) -> tuple[PrepareLeafBinding, ...]:
    by_path = {item.path: item.sha256 for item in hashes}
    try:
        return tuple(
            PrepareLeafBinding(path, (role,), by_path[path])
            for path, role in specifications
        )
    except KeyError as error:
        _stale(f"desktop plan lacks captured leaf implementation: {error.args[0]}")


def _raise_if_cancelled(startup: WorkerStartup) -> None:
    if startup.store.stop_intent_if_present(startup.request.action_id) is not None:
        raise WorkerCancelled


def _atomic_replace(destination: Path, content: bytes, *, mode: int = 0o600) -> None:
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


def _stale(detail: str) -> Never:
    raise WorkerStartupError(detail)
