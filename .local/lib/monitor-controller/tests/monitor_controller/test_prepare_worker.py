"""Ordered plan, guard, cancellation, leaf, and result contracts for prepare."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

import monitor_controller.workers.prepare as prepare_module
from monitor_controller.desktop.layout import DisplayScreenSnapshot
from monitor_controller.desktop.plan_codec import (
    AtomicPlanStore,
    DesktopPlanBundle,
    PlannedTopology,
    hash_plan_bundle,
)
from monitor_controller.desktop.planner import (
    DesktopDisplaySnapshot,
    FilesystemDesktopPlanningInputSource,
    build_desktop_plan,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EventGeneration,
    ObservationKey,
    OutputMapping,
    PlanningInputKey,
    PrepareDesktop,
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
    TransactionProtocolError,
    TransactionRequest,
    TransactionStore,
)
from monitor_controller.shadow import ShadowDesktopContextSource, load_saved_profiles
from monitor_controller.workers.common import (
    CANCELLED_EXIT_STATUS,
    STALE_EXIT_STATUS,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    kill_process_group,
    validate_worker_startup,
    write_worker_result,
)
from monitor_controller.workers.prepare import (
    ConfigureTerminals,
    GenerateFluxboxConfiguration,
    InstallFluxboxOverlay,
    PrepareCommandResult,
    PrepareOperation,
    ReloadEmacsFonts,
    SetPanelProperties,
    SetXfceDpi,
    SubprocessPrepareCommands,
    SubprocessPrepareLeafRunner,
    execute_preparation,
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
_TRANSITION = TransitionId(_INSTANCE, 1)
_UNIT = f"monitor-prepare@{_PREPARE_ACTION.value}.service"
_MAPPING = (
    OutputMapping("DisplayPort-1", "DisplayPort-9"),
    OutputMapping("eDP", "eDP"),
)
_EXPECTED_KINDS: Final = (
    InstallFluxboxOverlay,
    SetPanelProperties,
    SetXfceDpi,
    ConfigureTerminals,
    ReloadEmacsFonts,
    GenerateFluxboxConfiguration,
)


def _edid_bytes(name: str) -> bytes:
    return bytes.fromhex((_EDID / name).read_text(encoding="ascii"))


def _saved_edid(output: str) -> bytes:
    value = next(
        line.split()[1]
        for line in (_REPO / ".config" / "autorandr" / _PROFILE / "setup")
        .read_text(encoding="ascii")
        .splitlines()
        if line.startswith(f"{output} ")
    )
    return bytes.fromhex(value.replace("*", "00" * 32))


def _sysfs_tree(root: Path) -> Path:
    for name, connector_id, edid in (
        ("card0-eDP-1", 73, _saved_edid("eDP")),
        ("card0-DP-3", 91, _saved_edid("DisplayPort-1")),
    ):
        connector = root / name
        connector.mkdir(parents=True)
        connector.joinpath("status").write_text("connected\n", encoding="ascii")
        connector.joinpath("connector_id").write_text(
            f"{connector_id}\n", encoding="ascii"
        )
        connector.joinpath("edid").write_bytes(edid)
    return root


class _FakeCommands:
    def __init__(
        self,
        *,
        status: int = 0,
        timed_out: bool = False,
        on_apply: Callable[[int, PrepareOperation], None] | None = None,
    ) -> None:
        self.query_text = (_XRANDR / "samsung.query").read_text(encoding="utf-8")
        self.properties_text = (_XRANDR / "samsung.props").read_text(encoding="utf-8")
        self.status = status
        self.timed_out = timed_out
        self.on_apply = on_apply
        self.read_calls: list[tuple[str, ...]] = []
        self.operations: list[PrepareOperation] = []

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

    def apply(self, operation: PrepareOperation) -> PrepareCommandResult:
        self.operations.append(operation)
        if self.on_apply is not None:
            self.on_apply(len(self.operations), operation)
        return PrepareCommandResult(self.status, timed_out=self.timed_out)


def _plan(
    tree: ReadOnlyTree,
    commands: _FakeCommands,
) -> tuple[DesktopPlanBundle, RequestPlan, ExpectedTopology]:
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
    request = RequestPlan(_PLAN_ACTION, _TRANSITION, key, _PROFILE)
    bundle = build_desktop_plan(source.load(request))
    source.close()
    expected = ExpectedTopology(
        topology.kernel_connected_outputs,
        topology.kernel_external_outputs,
        topology.x_connected_outputs,
        topology.x_active_outputs,
    )
    return bundle, request, expected


def _startup(
    tmp_path: Path,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
    *,
    allow_absence: bool = True,
    mutate_request: Callable[[TransactionRequest], TransactionRequest] | None = None,
) -> tuple[WorkerStartup, TransactionStore, AtomicPlanStore, DesktopPlanBundle]:
    bundle, plan_request, expected = _plan(tree, commands)
    plan_store = AtomicPlanStore(tmp_path / "plans")
    plan_hash = plan_store.stage(plan_request.action_id, bundle)
    request = TransactionRequest(
        action_id=_PREPARE_ACTION,
        action_kind=ActionKind.PREPARATION,
        unit_name=_UNIT,
        physical_epoch=7,
        physical_token=bundle.plan.guards.physical_token,
        admitted_event_generation=EventGeneration(43),
        observation_key=bundle.plan.guards.observation_key,
        output_mapping=_MAPPING,
        expected_topology=expected,
        profile=_PROFILE,
        layout=_LAYOUT,
        transition_id=_TRANSITION,
        transition_key=TransitionKey(f"7|{_PROFILE}|admitted-samsung-desktop"),
        plan_hash=plan_hash,
        payload=(
            ("allow_temporary_edid_absence", allow_absence),
            ("planning_action_id", _PLAN_ACTION.value),
        ),
    )
    if mutate_request is not None:
        request = mutate_request(request)
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(request)
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PREPARATION,
    )
    commands.read_calls.clear()
    return startup, store, plan_store, bundle


def _execute(
    startup: WorkerStartup,
    plan_store: AtomicPlanStore,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
) -> int:
    return execute_preparation(
        startup,
        plan_store=plan_store,
        drm_tree=tree,
        commands=commands,
    )


def test_exact_plan_actions_run_in_order_with_fresh_guards_and_bound_result(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store, plan_store, bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands) == 0

    assert tuple(type(item) for item in commands.operations) == _EXPECTED_KINDS
    assert commands.operations[0] == InstallFluxboxOverlay(
        next(
            item.content
            for item in bundle.artifacts
            if item.relative_path == bundle.plan.overlay.artifact_path
        )
    )
    panels = commands.operations[1]
    dpi = commands.operations[2]
    emacs = commands.operations[4]
    assert isinstance(panels, SetPanelProperties)
    assert isinstance(dpi, SetXfceDpi)
    assert isinstance(emacs, ReloadEmacsFonts)
    assert panels.panels == bundle.plan.panels
    assert dpi.intent == bundle.plan.dpi
    assert emacs.intent == bundle.plan.emacs
    assert len(commands.read_calls) == 2 * len(bundle.plan.prepare_actions)
    result = store.read_result(_PREPARE_ACTION)
    assert result.outcome is ActionLifecycle.COMPLETED
    assert result.plan_hash == hash_plan_bundle(bundle)
    assert result.plan_hash is not None
    assert _TRANSITION.value in result.detail
    assert result.plan_hash.value in result.detail
    assert result.request_sha256 == startup.request.request_sha256


@pytest.mark.parametrize(
    "guard", ["transition-key", "event-generation", "active-topology"]
)
def test_request_transition_identity_must_derive_exactly_from_plan_guards(
    tmp_path: Path,
    guard: str,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()

    def mutate(request: TransactionRequest) -> TransactionRequest:
        if guard == "transition-key":
            return replace(request, transition_key=TransitionKey("7|bogus|observation"))
        if guard == "event-generation":
            return replace(request, admitted_event_generation=EventGeneration(40))
        return replace(
            request,
            expected_topology=replace(
                request.expected_topology,
                x_active_outputs=("eDP",),
            ),
        )

    startup, store, plan_store, _bundle = _startup(
        tmp_path,
        tree,
        commands,
        mutate_request=mutate,
    )

    assert _execute(startup, plan_store, tree, commands) == STALE_EXIT_STATUS
    assert commands.operations == []
    assert "transition guards" in store.read_result(_PREPARE_ACTION).detail


@pytest.mark.parametrize("change", ["edid", "status", "connector-id", "x-active"])
def test_contradiction_after_first_action_stops_before_next_mutation(
    tmp_path: Path,
    change: str,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)

    def mutate(_index: int, _operation: PrepareOperation) -> None:
        if change == "edid":
            root.joinpath("card0-DP-3", "edid").write_bytes(
                _edid_bytes("valid-base.hex")
            )
        elif change == "status":
            root.joinpath("card0-DP-3", "status").write_text(
                "disconnected\n", encoding="ascii"
            )
        elif change == "connector-id":
            root.joinpath("card0-DP-3", "connector_id").write_text(
                "92\n", encoding="ascii"
            )
        else:
            commands.query_text = commands.query_text.replace(
                "DisplayPort-9 connected primary 5120x2160+2880+0",
                "DisplayPort-9 connected primary",
            )
            commands.properties_text = commands.properties_text.replace(
                "DisplayPort-9 connected primary 5120x2160+2880+0",
                "DisplayPort-9 connected primary",
            )

    commands = _FakeCommands(on_apply=mutate)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, RootedSysfsReader(root), commands) == (
        STALE_EXIT_STATUS
    )
    assert len(commands.operations) == 1
    assert store.read_result(_PREPARE_ACTION).detail.startswith("STALE:")


def test_request_and_plan_are_revalidated_before_each_mutation(tmp_path: Path) -> None:
    for case in ("request", "plan"):
        root = _sysfs_tree(tmp_path / case / "sysfs")
        tree = RootedSysfsReader(root)
        commands = _FakeCommands()
        startup, store, plan_store, _bundle = _startup(tmp_path / case, tree, commands)

        def tamper(
            _index: int,
            _operation: PrepareOperation,
            *,
            current_case: str = case,
            current_store: TransactionStore = store,
            current_plan_store: AtomicPlanStore = plan_store,
        ) -> None:
            if current_case == "request":
                path = current_store.action_directory(_PREPARE_ACTION) / "request.json"
                raw = json.loads(path.read_bytes())
                raw["physical_epoch"] = 8
            else:
                path = current_plan_store.action_directory(_PLAN_ACTION) / "plan.json"
                raw = json.loads(path.read_bytes())
                raw["guards"]["profile"] = "substituted"
            path.unlink()
            path.write_text(json.dumps(raw), encoding="utf-8")
            path.chmod(0o600)

        commands.on_apply = tamper
        if case == "request":
            with pytest.raises(TransactionProtocolError, match="request content hash"):
                _execute(startup, plan_store, tree, commands)
            assert store.result_if_present(_PREPARE_ACTION) is None
        else:
            assert _execute(startup, plan_store, tree, commands) == STALE_EXIT_STATUS
            assert store.read_result(_PREPARE_ACTION).detail.startswith("STALE:")
        assert len(commands.operations) == 1


def test_temporary_edid_absence_continues_only_when_request_admits_policy(
    tmp_path: Path,
) -> None:
    cases = ((True, 0, 6), (False, 75, 1))
    for allow_absence, expected_status, expected_count in cases:
        case = tmp_path / str(allow_absence)
        root = _sysfs_tree(case / "sysfs")
        tree = RootedSysfsReader(root)

        def remove_edid(
            _index: int,
            _operation: PrepareOperation,
            *,
            current_root: Path = root,
        ) -> None:
            current_root.joinpath("card0-DP-3", "edid").unlink(missing_ok=True)

        commands = _FakeCommands(on_apply=remove_edid)
        startup, store, plan_store, _bundle = _startup(
            case,
            tree,
            commands,
            allow_absence=allow_absence,
        )

        assert _execute(startup, plan_store, RootedSysfsReader(root), commands) == (
            expected_status
        )
        assert len(commands.operations) == expected_count
        expected_outcome = (
            ActionLifecycle.COMPLETED if allow_absence else ActionLifecycle.FAILED
        )
        assert store.read_result(_PREPARE_ACTION).outcome is expected_outcome


@pytest.mark.parametrize("extension_state", ["incomplete", "invalid"])
def test_matching_valid_base_with_broken_extensions_is_noncontradictory(
    tmp_path: Path,
    extension_state: str,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    external = root / "card0-DP-3" / "edid"
    if extension_state == "incomplete":
        external.write_bytes(external.read_bytes()[:128])
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands) == 0
    assert len(commands.operations) == len(_EXPECTED_KINDS)
    assert store.read_result(_PREPARE_ACTION).outcome is ActionLifecycle.COMPLETED


@pytest.mark.parametrize(
    ("status", "timed_out", "outcome"),
    [
        (23, False, ActionLifecycle.FAILED),
        (124, True, ActionLifecycle.TIMED_OUT),
    ],
)
def test_leaf_failure_and_timeout_are_exact_terminal_results(
    tmp_path: Path,
    status: int,
    *,
    timed_out: bool,
    outcome: ActionLifecycle,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands(status=status, timed_out=timed_out)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)

    assert _execute(startup, plan_store, tree, commands) == status
    result = store.read_result(_PREPARE_ACTION)
    assert result.outcome is outcome
    assert len(commands.operations) == 1


@pytest.mark.parametrize(
    ("when", "expected_count"),
    [("before", 0), ("between", 1)],
)
def test_cooperative_stop_intent_cancels_at_safe_boundaries(
    tmp_path: Path,
    when: str,
    expected_count: int,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)
    if when == "before":
        store.create_stop_intent(_PREPARE_ACTION, ActionLifecycle.CANCELLED)
    else:

        def stop_between(_index: int, _operation: PrepareOperation) -> None:
            store.create_stop_intent(
                _PREPARE_ACTION,
                ActionLifecycle.CANCELLED,
            )

        commands.on_apply = stop_between

    assert _execute(startup, plan_store, tree, commands) == CANCELLED_EXIT_STATUS
    assert len(commands.operations) == expected_count
    assert store.read_result(_PREPARE_ACTION).outcome is ActionLifecycle.CANCELLED


def test_sigterm_without_stop_intent_and_repeat_or_late_result_are_at_most_once(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))

    def interrupt(_index: int, _operation: PrepareOperation) -> None:
        raise WorkerCancelled

    commands = _FakeCommands(on_apply=interrupt)
    startup, store, plan_store, _bundle = _startup(tmp_path, tree, commands)
    with pytest.raises(WorkerCancelled):
        _execute(startup, plan_store, tree, commands)
    assert store.result_if_present(_PREPARE_ACTION) is None
    with pytest.raises(WorkerStartupError, match="already claimed"):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_PREPARE_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.PREPARATION,
        )

    store.create_stop_intent(_PREPARE_ACTION, ActionLifecycle.CANCELLED)
    first = write_worker_result(
        startup,
        execution=WorkerExecution(ActionLifecycle.CANCELLED, 143, "cancelled"),
        started_monotonic_ms=1,
        finished_monotonic_ms=2,
    )
    with pytest.raises(ImmutableTransactionError):
        write_worker_result(
            startup,
            execution=WorkerExecution(ActionLifecycle.FAILED, 1, "late failure"),
            started_monotonic_ms=3,
            finished_monotonic_ms=4,
        )
    assert store.read_result(_PREPARE_ACTION) == first


class _CaptureLeafRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None, dict[str, str]]] = []

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        environment: Mapping[str, str],
        input_bytes: bytes | None = None,
        timeout_seconds: float = 90.0,
    ) -> PrepareCommandResult:
        del timeout_seconds
        self.calls.append((arguments, input_bytes, dict(environment)))
        return PrepareCommandResult(0)


def test_production_adapter_captures_only_allowed_exact_leaves_in_temp_home(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    fake = _FakeCommands()
    startup_value, _store, plan_store, bundle = _startup(tmp_path, tree, fake)
    assert _execute(startup_value, plan_store, tree, fake) == 0
    capture = _CaptureLeafRunner()
    home = tmp_path / "harmless-home"
    commands = SubprocessPrepareCommands(
        home_root=home,
        leaf_root=_REPO / "bin",
        work_root=tmp_path / "work",
        leaf_runner=capture,
        base_environment={
            "BASH_FUNC_xfconf-query%%": "() { touch /tmp/escaped; }",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/test/bus",
            "DISPLAY": ":attacker",
            "LD_PRELOAD": "forbidden",
            "PATH": "/attacker/bin",
            "PYTHONPATH": "/attacker/python",
            "PYTHONUSERBASE": "/attacker/user-site",
            "RUBY_FUTURE_OPTION": "forbidden",
            "RUBYOPT": "forbidden",
            "XAUTHORITY": "/attacker/Xauthority",
            "XDG_CONFIG_HOME": "/attacker/config",
            "XDG_RUNTIME_DIR": "/run/user/test",
        },
    )
    operations = tuple(fake.operations)
    dpi_operation = operations[2]
    terminal_operation = operations[3]
    assert isinstance(dpi_operation, SetXfceDpi)
    assert isinstance(terminal_operation, ConfigureTerminals)
    assert {leaf.logical_path for leaf in dpi_operation.leaves} == {
        "bin/set-layout-dpi",
        "bin/set-xfce4-dpi",
    }
    assert {leaf.logical_path for leaf in terminal_operation.leaves} == {
        "bin/gnome-terminal-config",
        "bin/gnome-terminal-profile",
        "bin/kitty-theme-config",
        "bin/setup-terminals",
        "bin/xfce4-terminal-config",
    }

    assert all(commands.apply(operation).exit_status == 0 for operation in operations)

    overlay = operations[0]
    assert isinstance(overlay, InstallFluxboxOverlay)
    assert (home / ".fluxbox" / "overlay").read_bytes() == overlay.content
    executable_names = tuple(Path(call[0][0]).name for call in capture.calls)
    assert executable_names == (
        "setup-panels",
        "set-layout-dpi",
        "setup-terminals",
        "emacsclient",
    )
    joined = "\0".join(argument for call in capture.calls for argument in call[0])
    for forbidden in (
        "setup-monitor",
        "autorandr",
        "fluxbox-remote",
        "xfce4-panel",
        "nm-applet",
        "setup-keyboard",
        " xrandr ",
    ):
        assert forbidden not in joined
    generated = operations[-1]
    assert isinstance(generated, GenerateFluxboxConfiguration)
    assert (home / ".fluxbox" / "keys").read_bytes() == generated.content
    assert capture.calls[-1][1] is None
    assert str(bundle.plan.emacs.font_height) in capture.calls[-1][0][2]
    for _arguments, _input, environment in capture.calls:
        assert set(environment) == {
            "DBUS_SESSION_BUS_ADDRESS",
            "HOME",
            "MONITOR_CONTROLLER_LEAF_BIN",
            "PATH",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "XDG_CONFIG_HOME",
            "XDG_RUNTIME_DIR",
        }
        runtime = f"/run/user/{os.getuid()}"
        assert environment["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"
        assert environment["HOME"] == str(home)
        assert environment["PATH"] == "/usr/bin:/bin"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert environment["PYTHONNOUSERSITE"] == "1"
        assert environment["XDG_CONFIG_HOME"] == str(home / ".config")
        assert environment["XDG_RUNTIME_DIR"] == runtime
        assert Path(environment["MONITOR_CONTROLLER_LEAF_BIN"]).parent == (
            tmp_path / "work"
        )


def test_dispatcher_binds_preparation_request_to_planning_identity(
    tmp_path: Path,
) -> None:
    store = TransactionStore(tmp_path / "transactions")
    supervisor = SystemdSupervisor(systemctl=Path("/harmless/systemctl"))
    dispatcher = SystemdDispatcher(store, supervisor)
    effect = PrepareDesktop(
        _PREPARE_ACTION,
        _TRANSITION,
        TransitionKey("transition"),
        _PROFILE,
        # The worker independently verifies the actual staged content.
        hash_plan_bundle(
            _plan(
                RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs")),
                _FakeCommands(),
            )[0]
        ),
        EventGeneration(2),
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
    )

    prepared = asyncio.run(dispatcher.write_request(effect, context))
    request = store.read_request(prepared.action_id)

    assert request.payload == (
        ("allow_temporary_edid_absence", True),
        ("planning_action_id", _PLAN_ACTION.value),
    )
    assert request.plan_hash == effect.plan_hash
    assert request.transition_id == _TRANSITION


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _planned_operations(tmp_path: Path) -> tuple[PrepareOperation, ...]:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    fake = _FakeCommands()
    startup, _store, plan_store, _bundle = _startup(tmp_path, tree, fake)
    assert _execute(startup, plan_store, tree, fake) == 0
    return tuple(fake.operations)


def _primitive_environment(
    tmp_path: Path,
    *,
    block_xfconf: bool = False,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "primitive-bin"
    fake_bin.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".config" / "kitty").mkdir(parents=True)
    log = tmp_path / "primitive.log"
    child_pid = tmp_path / "child.pid"
    common = 'printf \'%s %s\\n\' "${0##*/}" "$*" >> ' + shlex.quote(str(log)) + "\n"
    blocking = (
        "sleep 30 &\n"
        "printf '%s\\n' \"$!\" > " + shlex.quote(str(child_pid)) + "\nwait\n"
        if block_xfconf
        else ""
    )
    _write_executable(
        fake_bin / "xfconf-query",
        "#!/bin/sh\n" + common + blocking,
    )
    _write_executable(
        fake_bin / "dconf",
        "#!/bin/sh\n"
        + common
        + 'if [ "${1:-}" = dump ]; then\n'
        + '  printf "[:b1dcc9dd-5262-4d8d-a863-c897e6d979b9]\\n'
        + "visible-name='Dark'\\n"
        + "[:bc1dfcac-1690-4dec-9176-20bd65652b75]\\n"
        + "visible-name='Bright'\\n\"\n"
        + 'elif [ "${1:-}" = load ]; then\n'
        + "  cat >/dev/null\n"
        + "fi\n",
    )
    _write_executable(fake_bin / "busctl", "#!/bin/sh\n" + common)
    # set-xfce4-dpi pipes to `xrdb -merge` since df2b31e; the real xrdb would
    # fail with no display, and its invocation must be observable like the rest.
    _write_executable(fake_bin / "xrdb", "#!/bin/sh\n" + common + "cat >/dev/null\n")
    _write_executable(
        fake_bin / "cp",
        "#!/bin/sh\n" + common + '/usr/bin/cp "$@"\n',
    )
    _write_executable(fake_bin / "kitty", "#!/bin/sh\n" + common)
    _write_executable(fake_bin / "emacsclient", "#!/bin/sh\n" + common)
    environment = {
        "CHILD_PID": str(child_pid),
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "RUBYLIB": "forbidden",
        "RUBYOPT": "forbidden",
        "ZDOTDIR": str(home),
    }
    return environment, log


def _assert_process_gone(pid: int) -> None:
    for _unused in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"descendant process {pid} survived process-group cleanup")


def test_real_exact_leaves_use_only_fake_primitives_and_temp_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _planned_operations(tmp_path / "plan")
    environment, log = _primitive_environment(tmp_path)
    monkeypatch.setattr(prepare_module, "_TRUSTED_PATH", environment["PATH"])
    home = Path(environment["HOME"])
    shell_escape = tmp_path / "shell-environment-escaped"
    python_escape = tmp_path / "python-environment-escaped"
    malicious_python = tmp_path / "malicious-python"
    malicious_python.mkdir()
    malicious_module = (
        f"from pathlib import Path\nPath({str(python_escape)!r}).touch()\n"
    )
    malicious_python.joinpath("sitecustomize.py").write_text(
        malicious_module,
        encoding="utf-8",
    )
    user_site = home / ".local" / "lib" / "python3.13" / "site-packages"
    user_site.mkdir(parents=True)
    user_site.joinpath("sitecustomize.py").write_text(
        malicious_module,
        encoding="utf-8",
    )
    assert (_REPO / "bin" / "gnome-terminal-profile").read_bytes().splitlines()[
        0
    ] == b"#!/usr/bin/python3 -I"
    malicious_config = tmp_path / "malicious-config"
    environment.update(
        {
            "BASH_FUNC_xfconf-query%%": (
                f"() {{ touch {shlex.quote(str(shell_escape))}; }}"
            ),
            "PYTHONPATH": str(malicious_python),
            "PYTHONUSERBASE": str(malicious_python),
            "RUBYLIB": str(tmp_path / "malicious-ruby"),
            "XDG_CONFIG_HOME": str(malicious_config),
        }
    )
    commands = SubprocessPrepareCommands(
        home_root=home,
        leaf_root=_REPO / "bin",
        work_root=tmp_path / "work",
        base_environment=environment,
    )

    assert all(commands.apply(operation).exit_status == 0 for operation in operations)

    lines = log.read_text(encoding="utf-8").splitlines()
    names = {line.split(maxsplit=1)[0] for line in lines}
    assert {"xfconf-query", "dconf", "busctl", "cp", "emacsclient"} <= names
    assert (home / ".config" / "gnome-terminal-profile").read_text() == "Dark\n"
    assert not malicious_config.exists()
    assert not python_escape.exists()
    assert not shell_escape.exists()
    emacs = next(line for line in lines if line.startswith("emacsclient "))
    assert "monitor-controller-apply-font-height 130" in emacs
    nested_output = "\n".join(lines)
    for forbidden in (
        "autorandr",
        "erb",
        "fluxbox-remote",
        "ruby",
        "setup-keyboard",
        "setup-monitor",
        "xrandr",
    ):
        assert forbidden not in nested_output.casefold()


def test_changed_leaf_stales_before_any_subprocess_mutation(tmp_path: Path) -> None:
    operations = _planned_operations(tmp_path / "plan")
    panel = operations[1]
    assert isinstance(panel, SetPanelProperties)
    leaf_root = tmp_path / "leaf-bin"
    leaf_root.mkdir()
    for leaf in panel.leaves:
        name = Path(leaf.logical_path).name
        leaf_root.joinpath(name).write_bytes((_REPO / "bin" / name).read_bytes())
    leaf_root.joinpath("setup-panels").write_bytes(b"#!/bin/sh\nexit 0\n")
    capture = _CaptureLeafRunner()
    commands = SubprocessPrepareCommands(
        home_root=tmp_path / "home",
        leaf_root=leaf_root,
        work_root=tmp_path / "work",
        leaf_runner=capture,
        base_environment={"PATH": "/usr/bin:/bin"},
    )

    with pytest.raises(WorkerStartupError, match="helper changed"):
        commands.apply(panel)
    assert capture.calls == []


@pytest.mark.parametrize("interruption", ["timeout", "sigterm"])
def test_real_exact_leaf_cleans_descendant_process_group(
    tmp_path: Path,
    interruption: str,
) -> None:
    environment, _log = _primitive_environment(tmp_path, block_xfconf=True)
    arguments = (
        str(_REPO / "bin" / "setup-panels"),
        "--exact",
        "1",
        "Primary",
        "p=8;x=0;y=0",
        "100",
        "-",
    )
    runner = SubprocessPrepareLeafRunner()
    if interruption == "timeout":
        result = runner.run(
            arguments,
            environment=environment,
            timeout_seconds=0.2,
        )
        assert result == PrepareCommandResult(124, timed_out=True)
    else:
        previous = signal.getsignal(signal.SIGTERM)

        def cancel(_signum: int, _frame: object) -> None:
            raise WorkerCancelled

        signal.signal(signal.SIGTERM, cancel)
        timer = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            with pytest.raises(WorkerCancelled):
                runner.run(arguments, environment=environment, timeout_seconds=5)
        finally:
            timer.cancel()
            signal.signal(signal.SIGTERM, previous)
    child_pid = Path(environment["CHILD_PID"])
    assert child_pid.exists()
    _assert_process_gone(int(child_pid.read_text(encoding="ascii")))


def test_prepare_group_cleanup_kills_child_after_leader_exit(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "orphan.pid"
    process = subprocess.Popen(  # noqa: S603
        (
            "/bin/sh",
            "-c",
            f"sleep 30 & printf '%s\\n' $! > {shlex.quote(str(child_pid_path))}",
        ),
        start_new_session=True,
    )
    assert process.wait(timeout=2) == 0
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    os.kill(child_pid, 0)

    kill_process_group(process)

    _assert_process_gone(child_pid)
    kill_process_group(process)  # ESRCH is harmless.


def test_shell_exact_leaf_regressions_and_fluxbox_generation_split(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    environment = {
        "HOME": str(tmp_path / "home"),
        "MONITOR_CONTROLLER_LEAF_BIN": str(fake_bin),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "CALLS": str(log),
        "ZDOTDIR": str(tmp_path / "home"),
    }
    Path(environment["HOME"]).joinpath(".config").mkdir(parents=True)
    output = tmp_path / "generated" / "keys"
    generated = subprocess.run(  # noqa: S603
        (
            str(_REPO / "bin" / "fluxbox-gen-config"),
            "--template",
            "-",
            "--template-label",
            "staged-template",
            "--output",
            str(output),
            "--monitor-count",
            "1",
            "--host-name",
            "celtic",
        ),
        check=False,
        env={**environment, "RUBYOPT": "forbidden"},
        input=(_REPO / ".fluxbox" / "keys.erb").read_bytes(),
        capture_output=True,
    )
    assert generated.returncode == 0, generated.stderr
    assert "# Number of monitors connected: 1" in output.read_text(encoding="utf-8")

    _write_executable(
        fake_bin / "monitors-connected",
        "#!/bin/sh\nprintf '%s\\n' \"${FAKE_MONITOR_COUNT:-1}\"\n",
    )
    normal = subprocess.run(  # noqa: S603
        (str(_REPO / "bin" / "fluxbox-gen-config"),),
        check=False,
        env={**environment, "FAKE_MONITOR_COUNT": "3"},
        capture_output=True,
    )
    assert normal.returncode == 0, normal.stderr
    normal_keys = Path(environment["HOME"]) / ".fluxbox" / "keys"
    assert "# Number of monitors connected: 3" in normal_keys.read_text()

    missing_exact_count = subprocess.run(  # noqa: S603
        (
            str(_REPO / "bin" / "fluxbox-gen-config"),
            "--template",
            "-",
            "--output",
            str(output),
        ),
        check=False,
        env={**environment, "MONITORS_CONNECTED": "3"},
        input=(_REPO / ".fluxbox" / "keys.erb").read_bytes(),
        capture_output=True,
    )
    assert missing_exact_count.returncode == 2
    assert b"--monitor-count is required" in missing_exact_count.stderr
    post_install = (_REPO / ".cfg-post.d" / "desktop-config").read_text()
    assert '"$here/../bin/fluxbox-gen-config"' in post_install

    for name in (
        "fluxbox-gen-config",
        "fluxbox-remote",
        "xfconf-query",
        "set-xfce4-dpi",
        "gnome-terminal-config",
        "gnome-terminal-profile",
        "xfce4-terminal-config",
        "kitty-theme-config",
        "div",
    ):
        _write_executable(
            fake_bin / name,
            '#!/bin/sh\nprintf \'%s %s\\n\' "${0##*/}" "$*" >> "$CALLS"\n',
        )
    for arguments in (("--generate-only",), ("--live-only",), ()):
        completed = subprocess.run(  # noqa: S603
            (str(_REPO / "bin" / "fluxbox-reconfigure"), *arguments),
            check=False,
            env=environment,
        )
        assert completed.returncode == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[:4] == [
        "fluxbox-gen-config --monitor-count 1",
        "fluxbox-remote Reconfigure",
        "fluxbox-gen-config --monitor-count 1",
        "fluxbox-remote Reconfigure",
    ]

    log.write_text("", encoding="utf-8")
    panel = subprocess.run(  # noqa: S603
        (
            str(_REPO / "bin" / "setup-panels"),
            "--exact",
            "1",
            "Primary",
            "p=8;x=0;y=0",
            "100",
            "30",
        ),
        check=False,
        env=environment,
    )
    dpi = subprocess.run(  # noqa: S603
        (str(_REPO / "bin" / "set-layout-dpi"), "--exact", "139"),
        check=False,
        env=environment,
    )
    theme = tmp_path / "theme.conf"
    theme.write_text("foreground #fff\n", encoding="utf-8")
    terminals = subprocess.run(  # noqa: S603
        (
            str(_REPO / "bin" / "setup-terminals"),
            "--exact",
            "dark",
            "Dark",
            "dark",
            "SauceCodePro Nerd Font",
            "14",
            str(theme),
        ),
        check=False,
        env=environment,
    )
    assert panel.returncode == dpi.returncode == terminals.returncode == 0
    exact_calls = log.read_text(encoding="utf-8")
    assert exact_calls.count("xfconf-query ") == 4
    assert "set-xfce4-dpi 139" in exact_calls
    assert "gnome-terminal-config --exact-font SauceCodePro Nerd Font 14" in exact_calls
    assert (
        "xfce4-terminal-config --exact-font SauceCodePro Nerd Font 14 dark"
        in exact_calls
    )
    assert f"kitty-theme-config --exact dark {theme} 14" in exact_calls
    for forbidden in ("libdpy", "get-layout", "fluxbox-remote Reconfigure"):
        assert forbidden not in exact_calls
