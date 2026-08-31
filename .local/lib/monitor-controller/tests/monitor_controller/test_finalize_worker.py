"""Exact proof, ordering, cancellation, and command contracts for finalization."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from worker_evidence import saved_edid, write_sysfs_connectors

from monitor_controller.desktop.layout import DisplayScreenSnapshot
from monitor_controller.desktop.plan_codec import (
    AtomicPlanStore,
    DesktopPlanBundle,
    KeyboardDisposition,
    PlannedTopology,
    hash_plan_bundle,
)
from monitor_controller.desktop.planner import (
    DesktopDisplaySnapshot,
    FilesystemDesktopPlanningInputSource,
    build_desktop_plan,
)
from monitor_controller.desktop.tray import (
    StableTray,
    TrayReadinessError,
    TrayState,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EventGeneration,
    FinalizeDesktop,
    ObservationKey,
    OutputMapping,
    PlanningInputKey,
    RawEvidenceSource,
    RequestPlan,
    TransitionId,
    TransitionKey,
)
from monitor_controller.observer.drm import ReadOnlyTree, RootedSysfsReader, sample_drm
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import sample_xrandr
from monitor_controller.runtime.dispatcher import WorkerRequestContext
from monitor_controller.runtime.systemd import SystemdDispatcher, SystemdSupervisor
from monitor_controller.runtime.transactions import (
    ExpectedTopology,
    ImmutableTransactionError,
    TransactionRequest,
    TransactionStore,
)
from monitor_controller.shadow import ShadowDesktopContextSource, load_saved_profiles
from monitor_controller.workers.common import (
    CANCELLED_EXIT_STATUS,
    STALE_EXIT_STATUS,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    validate_worker_startup,
    write_worker_result,
)
from monitor_controller.workers.finalize import (
    ADVANTAGE_360_ADDRESS,
    ApplyFluxboxConfiguration,
    ApplyKeyboardIntent,
    ApplyWindowLayout,
    BluetoothctlConnectionProbe,
    CaptureTrayDiagnostics,
    DeferredCancellation,
    FinalizationFence,
    FinalizeCommandResult,
    FinalizeCommands,
    FinalizeOperation,
    RestartFluxbox,
    RestartNmApplet,
    RestartXfcePanel,
    SubprocessFinalizeCommands,
    execute_finalization,
)

_REPO = next(
    parent for parent in Path(__file__).parents if (parent / ".fluxbox").is_dir()
)
_FIXTURES = Path(__file__).parent / "fixtures"
_XRANDR = _FIXTURES / "xrandr"
_EDID = _FIXTURES / "edid"
_PROFILE = "celtic+Samsung-Odyssey-G75F"
_LAYOUT = "celtic+ultrawide"
_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_PLAN_ACTION = ActionId(_INSTANCE, ActionKind.PLAN, 1)
_PREPARE_ACTION = ActionId(_INSTANCE, ActionKind.PREPARATION, 2)
_FINALIZE_ACTION = ActionId(_INSTANCE, ActionKind.FINALIZATION, 3)
_TRANSITION = TransitionId(_INSTANCE, 1)
_PREPARE_UNIT = f"monitor-prepare@{_PREPARE_ACTION.value}.service"
_FINALIZE_UNIT = f"monitor-finalize@{_FINALIZE_ACTION.value}.service"
_MAPPING = (
    OutputMapping("DisplayPort-1", "DisplayPort-9"),
    OutputMapping("eDP", "eDP"),
)
_EXPECTED_OPERATIONS: Final = (
    ApplyFluxboxConfiguration,
    ApplyKeyboardIntent,
    ApplyWindowLayout,
    RestartFluxbox,
    RestartXfcePanel,
    RestartNmApplet,
    CaptureTrayDiagnostics,
)


def _saved_edid(output: str) -> bytes:
    return saved_edid(
        _REPO / ".config" / "autorandr" / _PROFILE / "setup",
        output,
        wildcard_fill="00" * 32,
    )


def _sysfs_tree(root: Path) -> Path:
    return write_sysfs_connectors(
        root,
        (
            ("card0-eDP-1", 73, _saved_edid("eDP")),
            ("card0-DP-3", 91, _saved_edid("DisplayPort-1")),
        ),
    )


class _FakeCommands:
    def __init__(
        self,
        *,
        on_apply: Callable[[int, FinalizeOperation], None] | None = None,
        tray_ready: bool = True,
    ) -> None:
        self.query_text = (_XRANDR / "samsung.query").read_text(encoding="utf-8")
        self.properties_text = (_XRANDR / "samsung.props").read_text(encoding="utf-8")
        self.on_apply = on_apply
        self.tray_ready = tray_ready
        self.read_calls: list[tuple[str, ...]] = []
        self.operations: list[FinalizeOperation] = []
        self.tray_waits = 0

    def query(self) -> TextCommandEvidence:
        self.read_calls.append(("xrandr", "--query"))
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "fixture:samsung.query",
            self.query_text,
        )

    def properties(self) -> TextCommandEvidence:
        self.read_calls.append(("xrandr", "--props"))
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_PROPERTIES,
            "fixture:samsung.props",
            self.properties_text,
        )

    def apply(self, operation: FinalizeOperation) -> FinalizeCommandResult:
        self.operations.append(operation)
        if self.on_apply is not None:
            self.on_apply(len(self.operations), operation)
        return FinalizeCommandResult(0)

    def wait_for_stable_tray(self) -> StableTray:
        self.tray_waits += 1
        if not self.tray_ready:
            message = "injected missing tray selection or wrapper"
            raise TrayReadinessError(message)
        return StableTray(TrayState(0x1234, (99,)), 6)


class _Fence:
    def __init__(self, generation: int = 43, *, mutator: bool = False) -> None:
        self.generation = EventGeneration(generation)
        self.mutator = mutator
        self.generation_checks = 0
        self.mutator_checks = 0

    def current_event_generation(self) -> EventGeneration:
        self.generation_checks += 1
        return self.generation

    def assert_no_other_mutator(self, request: TransactionRequest) -> None:
        del request
        self.mutator_checks += 1
        if self.mutator:
            message = "injected overlapping mutator"
            raise WorkerStartupError(message)


def _plan(
    tree: ReadOnlyTree,
    commands: _FakeCommands,
) -> tuple[DesktopPlanBundle, ExpectedTopology]:
    topology = derive_canonical_topology(sample_drm(tree), sample_xrandr(commands))
    commands.read_calls.clear()
    display = DesktopDisplaySnapshot(
        physical_epoch=7,
        physical_token=topology.physical_token,
        admitted_event_generation=EventGeneration(41),
        observation_key=ObservationKey("admitted-samsung-desktop"),
        topology=PlannedTopology(
            topology.kernel_connected_outputs,
            topology.kernel_external_outputs,
            topology.x_connected_outputs,
            topology.x_active_outputs,
        ),
        screens=(
            DisplayScreenSnapshot(
                output="eDP",
                width=2880,
                height=1920,
                x=0,
                y=0,
                width_mm=286,
                height_mm=191,
                primary=False,
            ),
            DisplayScreenSnapshot(
                output="DisplayPort-9",
                width=5120,
                height=2160,
                x=2880,
                y=0,
                width_mm=1480,
                height_mm=620,
                primary=True,
            ),
        ),
    )
    source = FilesystemDesktopPlanningInputSource(
        root=_REPO,
        display=display,
        context=ShadowDesktopContextSource(host_name="celtic", theme="dark"),
    )
    profile = source.complete_profile(
        next(
            item
            for item in load_saved_profiles(_REPO / ".config" / "autorandr")
            if item.name == _PROFILE
        )
    )
    key = PlanningInputKey(
        7,
        _PROFILE,
        _LAYOUT,
        display.observation_key,
        _MAPPING,
        display.topology.x_active_outputs,
        profile.configuration_hashes,
    )
    bundle = build_desktop_plan(
        source.load(RequestPlan(_PLAN_ACTION, _TRANSITION, key, _PROFILE))
    )
    source.close()
    expected = ExpectedTopology(
        topology.kernel_connected_outputs,
        topology.kernel_external_outputs,
        topology.x_connected_outputs,
        topology.x_active_outputs,
    )
    return bundle, expected


def _startup(
    tmp_path: Path,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
    *,
    proof_duration_ms: int = 10_000,
    mutate_final: Callable[[TransactionRequest], TransactionRequest] | None = None,
) -> tuple[WorkerStartup, TransactionStore, AtomicPlanStore, DesktopPlanBundle]:
    bundle, expected = _plan(tree, commands)
    plan_store = AtomicPlanStore(tmp_path / "plans")
    plan_hash = plan_store.stage(_PLAN_ACTION, bundle)
    transition_key = TransitionKey(f"7|{_PROFILE}|admitted-samsung-desktop")
    store = TransactionStore(tmp_path / "transactions")
    preparation = store.create_request(
        TransactionRequest(
            action_id=_PREPARE_ACTION,
            action_kind=ActionKind.PREPARATION,
            unit_name=_PREPARE_UNIT,
            physical_epoch=7,
            physical_token=bundle.plan.guards.physical_token,
            admitted_event_generation=EventGeneration(42),
            observation_key=bundle.plan.guards.observation_key,
            output_mapping=_MAPPING,
            expected_topology=expected,
            profile=_PROFILE,
            layout=_LAYOUT,
            transition_id=_TRANSITION,
            transition_key=transition_key,
            plan_hash=plan_hash,
            payload=(
                ("allow_temporary_edid_absence", True),
                ("planning_action_id", _PLAN_ACTION.value),
            ),
        )
    )
    store.claim_submission(preparation.action_id)
    preparation_startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=preparation.action_id.value,
        unit_name=preparation.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    write_worker_result(
        preparation_startup,
        execution=WorkerExecution(ActionLifecycle.COMPLETED, 0, "prepared exact plan"),
        started_monotonic_ms=1,
        finished_monotonic_ms=2,
    )
    finalization = TransactionRequest(
        action_id=_FINALIZE_ACTION,
        action_kind=ActionKind.FINALIZATION,
        unit_name=_FINALIZE_UNIT,
        physical_epoch=7,
        physical_token=bundle.plan.guards.physical_token,
        admitted_event_generation=EventGeneration(43),
        observation_key=bundle.plan.guards.observation_key,
        output_mapping=_MAPPING,
        expected_topology=expected,
        profile=_PROFILE,
        layout=_LAYOUT,
        transition_id=_TRANSITION,
        transition_key=transition_key,
        plan_hash=plan_hash,
        payload=(
            ("planning_action_id", _PLAN_ACTION.value),
            ("preparation_action_id", _PREPARE_ACTION.value),
            ("proof_duration_ms", proof_duration_ms),
        ),
    )
    if mutate_final is not None:
        finalization = mutate_final(finalization)
    finalization = store.create_request(finalization)
    store.claim_submission(finalization.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=finalization.action_id.value,
        unit_name=finalization.unit_name,
        expected_kind=ActionKind.FINALIZATION,
    )
    commands.read_calls.clear()
    return startup, store, plan_store, bundle


def _execute(  # noqa: PLR0913, PLR0917
    startup: WorkerStartup,
    plan_store: AtomicPlanStore,
    tree: ReadOnlyTree,
    commands: FinalizeCommands,
    fence: FinalizationFence,
    cancellation: DeferredCancellation | None = None,
) -> int:
    return execute_finalization(
        startup,
        plan_store=plan_store,
        drm_tree=tree,
        commands=commands,
        fence=fence,
        cancellation=DeferredCancellation() if cancellation is None else cancellation,
    )


def test_ordered_actions_have_fresh_guards_and_bound_success(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store, plan_store, bundle = _startup(tmp_path, tree, commands)
    fence = _Fence()

    assert _execute(startup, plan_store, tree, commands, fence) == 0

    assert tuple(type(item) for item in commands.operations) == _EXPECTED_OPERATIONS
    assert commands.tray_waits == 1
    assert len(commands.read_calls) == 16
    assert fence.generation_checks == fence.mutator_checks == 8
    window_operation = commands.operations[2]
    assert isinstance(window_operation, ApplyWindowLayout)
    assert json.loads(window_operation.content) == [
        {
            "commands": list(item.commands),
            "map_command": item.map_command,
            "matcher": item.matcher,
        }
        for item in bundle.plan.resolved_layout.window_actions
    ]
    result = store.read_result(_FINALIZE_ACTION)
    assert result.outcome is ActionLifecycle.COMPLETED
    assert result.plan_hash == hash_plan_bundle(bundle)
    assert "awaiting observation" in result.detail


@pytest.mark.parametrize(
    ("case", "expected_detail"),
    [
        ("short-proof", "ten seconds"),
        ("generation", "event-generation"),
        ("mutator", "mutator"),
        ("transition", "transition guards"),
    ],
)
def test_proof_plan_event_mutator_and_transition_guards_fail_before_mutation(
    tmp_path: Path,
    case: str,
    expected_detail: str,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    mutate: Callable[[TransactionRequest], TransactionRequest] | None = None
    if case == "transition":

        def change_transition(request: TransactionRequest) -> TransactionRequest:
            return replace(request, transition_key=TransitionKey("wrong"))

        mutate = change_transition
    startup, store, plan_store, _bundle = _startup(
        tmp_path,
        tree,
        commands,
        proof_duration_ms=9_999 if case == "short-proof" else 10_000,
        mutate_final=mutate,
    )
    fence = _Fence(
        generation=44 if case == "generation" else 43,
        mutator=case == "mutator",
    )

    assert _execute(startup, plan_store, tree, commands, fence) == STALE_EXIT_STATUS
    assert commands.operations == []
    assert expected_detail in store.read_result(_FINALIZE_ACTION).detail


def test_topology_contradiction_between_actions_stops_before_next_mutation(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)

    def disconnect(_index: int, _operation: FinalizeOperation) -> None:
        root.joinpath("card0-DP-3", "status").write_text(
            "disconnected\n", encoding="ascii"
        )

    commands = _FakeCommands(on_apply=disconnect)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands, _Fence()) == STALE_EXIT_STATUS
    assert len(commands.operations) == 1
    assert store.read_result(_FINALIZE_ACTION).detail.startswith("STALE:")


def test_identity_contradiction_between_actions_stops_before_next_mutation(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)

    def substitute_edid(_index: int, _operation: FinalizeOperation) -> None:
        replacement = bytes.fromhex(
            (_EDID / "valid-base.hex").read_text(encoding="ascii")
        )
        root.joinpath("card0-DP-3", "edid").write_bytes(replacement)

    commands = _FakeCommands(on_apply=substitute_edid)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands, _Fence()) == STALE_EXIT_STATUS
    assert len(commands.operations) == 1
    assert "identity" in store.read_result(_FINALIZE_ACTION).detail


def test_missing_stable_tray_blocks_nm_applet_and_diagnostics(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands(tray_ready=False)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands, _Fence()) == 69
    assert tuple(type(item) for item in commands.operations) == _EXPECTED_OPERATIONS[:5]
    result = store.read_result(_FINALIZE_ACTION)
    assert result.outcome is ActionLifecycle.FAILED
    assert "stable tray readiness" in result.detail


def test_durable_cancel_arriving_during_atomic_restart_is_reported_after_step(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    def cancel_on_fluxbox(_index: int, operation: FinalizeOperation) -> None:
        if isinstance(operation, RestartFluxbox):
            store.create_stop_intent(_FINALIZE_ACTION, ActionLifecycle.CANCELLED)

    commands.on_apply = cancel_on_fluxbox

    assert _execute(startup, plan_store, tree, commands, _Fence()) == (
        CANCELLED_EXIT_STATUS
    )
    assert tuple(type(item) for item in commands.operations) == _EXPECTED_OPERATIONS[:4]
    assert isinstance(commands.operations[-1], RestartFluxbox)
    assert store.read_result(_FINALIZE_ACTION).outcome is ActionLifecycle.CANCELLED


def test_late_terminal_result_cannot_replace_success(tmp_path: Path) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)
    assert _execute(startup, plan_store, tree, commands, _Fence()) == 0
    first = store.read_result(_FINALIZE_ACTION)

    with pytest.raises(ImmutableTransactionError):
        write_worker_result(
            startup,
            execution=WorkerExecution(ActionLifecycle.CANCELLED, 143, "late cancel"),
            started_monotonic_ms=3,
            finished_monotonic_ms=4,
        )
    assert store.read_result(_FINALIZE_ACTION) == first


def test_dispatcher_binds_finalization_to_preparation_and_proof(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    dispatcher = SystemdDispatcher(
        store,
        SystemdSupervisor(systemctl=Path("/harmless/systemctl")),
    )
    effect = FinalizeDesktop(
        _FINALIZE_ACTION,
        _TRANSITION,
        TransitionKey("transition"),
        _PROFILE,
        hash_plan_bundle(
            _plan(
                RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs")),
                _FakeCommands(),
            )[0]
        ),
        EventGeneration(43),
        ObservationKey("observation"),
    )
    context = WorkerRequestContext(
        physical_epoch=7,
        physical_token=_plan(RootedSysfsReader(tmp_path / "sysfs"), _FakeCommands())[
            0
        ].plan.guards.physical_token,
        output_mapping=_MAPPING,
        expected_topology=ExpectedTopology(
            ("DisplayPort-9", "eDP"),
            ("DisplayPort-9",),
            ("DisplayPort-9", "eDP"),
            ("DisplayPort-9", "eDP"),
        ),
        layout=_LAYOUT,
        planning_action_id=_PLAN_ACTION,
        preparation_action_id=_PREPARE_ACTION,
        proof_duration_ms=10_000,
    )

    prepared = asyncio.run(dispatcher.write_request(effect, context))
    request = store.read_request(prepared.action_id)

    assert request.payload == (
        ("planning_action_id", _PLAN_ACTION.value),
        ("preparation_action_id", _PREPARE_ACTION.value),
        ("proof_duration_ms", 10_000),
    )


class _CaptureRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.window_payload: object | None = None

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        timeout_seconds: float = 120.0,
    ) -> FinalizeCommandResult:
        del timeout_seconds
        self.calls.append((arguments, dict(environment)))
        if "--resolved-actions" in arguments:
            path = Path(arguments[arguments.index("--resolved-actions") + 1])
            self.window_payload = json.loads(path.read_bytes())
        return FinalizeCommandResult(0)


def test_production_adapter_uses_only_exact_leaves_and_separate_units(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    fake = _FakeCommands()
    startup, _store, _plan_store, bundle = _startup(tmp_path, tree, fake)
    capture = _CaptureRunner()
    commands = SubprocessFinalizeCommands(
        home_root=tmp_path / "home",
        leaf_root=_REPO / "bin",
        work_root=tmp_path / "work",
        leaf_runner=capture,
        tray_probe=None,
        keyboard_probe=_FakeKeyboardProbe(verdict=True),
        base_environment={
            "DISPLAY": ":harmless",
            "HOME": "/attacker",
            "LD_PRELOAD": "/attacker/preload",
            "PATH": "/attacker/bin",
            "PYTHONPATH": "/attacker/python",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
    )
    for action in bundle.plan.finalize_actions:
        operation = {
            0: ApplyFluxboxConfiguration(
                next(
                    item.content
                    for item in bundle.artifacts
                    if item.relative_path == bundle.plan.fluxbox.rendered_keys_artifact
                )
            ),
            1: ApplyKeyboardIntent(bundle.plan.keyboard.disposition),
            2: ApplyWindowLayout(
                next(
                    item.content
                    for item in bundle.artifacts
                    if item.relative_path == bundle.plan.windows.actions_artifact
                )
            ),
            3: RestartFluxbox(),
            4: RestartXfcePanel(_FINALIZE_ACTION),
            5: RestartNmApplet(),
            6: CaptureTrayDiagnostics(_FINALIZE_ACTION),
        }[action.sequence - 1]
        assert commands.apply(operation).exit_status == 0

    joined = "\0".join(
        argument for call, _environment in capture.calls for argument in call
    )
    assert "setup-monitor" not in joined
    assert "--resolved-actions" in joined
    assert "get-layout" not in joined
    assert "fluxbox-restart" in joined
    assert f"monitor-panel-restart@{_FINALIZE_ACTION.value}.service" in joined
    assert "nm-applet.service" in joined
    assert f"monitor-tray-diagnostics@{_FINALIZE_ACTION.value}.service" in joined
    assert capture.window_payload is not None
    assert (tmp_path / "home" / ".fluxbox" / "keys").is_file()
    for _arguments, environment in capture.calls:
        assert environment["HOME"] == str(tmp_path / "home")
        assert environment["PATH"].startswith(f"{_REPO / 'bin'}:")
        assert "LD_PRELOAD" not in environment
        assert "PYTHONPATH" not in environment
    assert startup.request.action_kind is ActionKind.FINALIZATION


class _FakeKeyboardProbe:
    """Injectable connection verdict, recording probed addresses."""

    def __init__(self, *, verdict: bool | None) -> None:
        self.verdict = verdict
        self.addresses: list[str] = []

    def connected(self, address: str) -> bool | None:
        self.addresses.append(address)
        return self.verdict


def _keyboard_commands(
    tmp_path: Path,
    *,
    verdict: bool | None,
) -> tuple[SubprocessFinalizeCommands, _CaptureRunner, _FakeKeyboardProbe]:
    capture = _CaptureRunner()
    probe = _FakeKeyboardProbe(verdict=verdict)
    commands = SubprocessFinalizeCommands(
        home_root=tmp_path / "home",
        leaf_root=_REPO / "bin",
        work_root=tmp_path / "work",
        leaf_runner=capture,
        tray_probe=None,
        keyboard_probe=probe,
        base_environment={
            "DISPLAY": ":harmless",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
    )
    return commands, capture, probe


@pytest.mark.parametrize(
    ("disposition", "verdict", "expected_command"),
    [
        (KeyboardDisposition.DISCONNECT_ADVANTAGE_360, True, "disconnect"),
        (KeyboardDisposition.DISCONNECT_ADVANTAGE_360, False, None),
        (KeyboardDisposition.DISCONNECT_ADVANTAGE_360, None, None),
        (KeyboardDisposition.CONNECT_ADVANTAGE_360, True, None),
        (KeyboardDisposition.CONNECT_ADVANTAGE_360, False, "connect"),
        (KeyboardDisposition.CONNECT_ADVANTAGE_360, None, None),
    ],
)
def test_keyboard_intent_converges_instead_of_replaying_commands(
    tmp_path: Path,
    disposition: KeyboardDisposition,
    verdict: bool | None,
    expected_command: str | None,
) -> None:
    """An end state that already holds is success, not a bluetoothctl failure.

    `bluetoothctl disconnect` exits 1 for a device that is not connected,
    which failed the first live unplug's finalization (dc-2in); a device
    bluez does not know is a no-op in both directions, matching the legacy
    best-effort semantics.
    """
    commands, capture, probe = _keyboard_commands(tmp_path, verdict=verdict)
    result = commands.apply(ApplyKeyboardIntent(disposition))
    assert result.exit_status == 0
    assert probe.addresses == [ADVANTAGE_360_ADDRESS]
    if expected_command is None:
        assert capture.calls == []
    else:
        assert [call for call, _environment in capture.calls] == [
            ("bluetoothctl", expected_command, ADVANTAGE_360_ADDRESS)
        ]


def test_unchanged_keyboard_intent_never_probes(tmp_path: Path) -> None:
    """No planned change means no bluez traffic at all."""
    commands, capture, probe = _keyboard_commands(tmp_path, verdict=True)
    result = commands.apply(ApplyKeyboardIntent(KeyboardDisposition.UNCHANGED))
    assert result.exit_status == 0
    assert probe.addresses == []
    assert capture.calls == []


def test_bluetoothctl_probe_parses_connection_state(tmp_path: Path) -> None:
    """The production probe reads real bluetoothctl output shapes."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    executable = fake_bin / "bluetoothctl"
    environment = {"PATH": f"{fake_bin}:/usr/bin:/bin"}
    probe = BluetoothctlConnectionProbe(environment=environment)

    executable.write_text(
        "#!/bin/sh\nprintf 'Device X\\n\\tConnected: yes\\n'\n"
    )
    executable.chmod(0o700)
    assert probe.connected("AA:BB") is True

    executable.write_text(
        "#!/bin/sh\nprintf 'Device X\\n\\tConnected: no\\n'\n"
    )
    assert probe.connected("AA:BB") is False

    executable.write_text("#!/bin/sh\necho 'Device AA:BB not available' >&2\nexit 1\n")
    assert probe.connected("AA:BB") is None
