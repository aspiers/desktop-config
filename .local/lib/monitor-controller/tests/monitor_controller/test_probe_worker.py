"""Guard and result contracts for the production keyed activation probe."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from worker_evidence import edid_fixture, saved_edid, write_sysfs_connectors

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EdidIntegrity,
    EventGeneration,
    ObservationKey,
    PhysicalToken,
    RawEvidenceSource,
)
from monitor_controller.observer.drm import (
    ReadOnlyTree,
    RootedSysfsReader,
    parse_edid,
    sample_drm,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import sample_xrandr
from monitor_controller.runtime.transactions import (
    ExpectedTopology,
    ImmutableTransactionError,
    TransactionProtocolError,
    TransactionRequest,
    TransactionStore,
)
from monitor_controller.workers.common import (
    CANCELLED_EXIT_STATUS,
    STALE_EXIT_STATUS,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    validate_worker_startup,
    write_worker_result,
)
from monitor_controller.workers.probe import (
    ProbeCommandResult,
    execute_probe,
)

FIXTURES = Path(__file__).parent / "fixtures"
XRANDR = FIXTURES / "xrandr"
EDID = FIXTURES / "edid"
PROFILE_SETUP = (
    FIXTURES / "autorandr" / "profiles" / "celtic+Samsung-Odyssey-G75F" / "setup"
)
_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_ACTION = ActionId(_INSTANCE, ActionKind.PROBE, 29)
_UNIT = f"monitor-probe@{_ACTION.value}.service"
_PROFILE = "celtic+Samsung-Odyssey-G75F"
_EXPECTED_READ_CALLS: Final = (
    ("xrandr", "--query"),
    ("xrandr", "--props"),
)
_EXPECTED_ARGV: Final = (
    "xrandr",
    "--output",
    "DisplayPort-9",
    "--mode",
    "5120x2160",
    "--right-of",
    "eDP",
)


def _changed_token(request: TransactionRequest) -> TransactionRequest:
    return replace(request, physical_token=PhysicalToken("changed-token"))


def _changed_connected_topology(request: TransactionRequest) -> TransactionRequest:
    return replace(
        request,
        expected_topology=replace(
            request.expected_topology,
            x_connected_outputs=("DisplayPort-8", "DisplayPort-9", "eDP"),
        ),
    )


def _changed_active_topology(request: TransactionRequest) -> TransactionRequest:
    return replace(
        request,
        expected_topology=replace(
            request.expected_topology,
            x_active_outputs=("DisplayPort-9", "eDP"),
        ),
    )


def _target_active(text: str) -> str:
    return text.replace(
        "DisplayPort-9 connected (",
        "DisplayPort-9 connected 5120x2160+2880+0 (",
    )


def _internal_inactive(text: str) -> str:
    return text.replace(
        "eDP connected primary 2880x1920+0+0",
        "eDP connected primary",
    )


def _preferred_mode_changed(text: str) -> str:
    return text.replace("5120x2160     60.00+", "5120x2160     60.00").replace(
        "3840x2160     60.00", "3840x2160     60.00+"
    )


def _mode_list_changed(text: str) -> str:
    return text.replace("   3840x2160     60.00\n", "", 1)


def _current_marker_changed(text: str) -> str:
    return text.replace("2880x1920     60.00*+", "2880x1920     60.00+", 1)


def _malformed_query(_text: str) -> str:
    return (XRANDR / "malformed.query").read_text(encoding="utf-8")


def _unchanged(text: str) -> str:
    return text


def _cancel_during_mutation() -> None:
    raise WorkerCancelled


class _FakeCommands:
    def __init__(
        self,
        *,
        query: str | None = None,
        properties: str | None = None,
        mutation_status: int = 0,
        mutation_timed_out: bool = False,
        on_activate: Callable[[], None] | None = None,
    ) -> None:
        self.query_text = query or (XRANDR / "inactive.query").read_text(
            encoding="utf-8"
        )
        self.properties_text = properties or (XRANDR / "inactive.props").read_text(
            encoding="utf-8"
        )
        self.mutation_status = mutation_status
        self.mutation_timed_out = mutation_timed_out
        self.on_activate = on_activate
        self.read_calls: list[tuple[str, ...]] = []
        self.mutations: list[tuple[str, ...]] = []

    def query(self) -> TextCommandEvidence:
        self.read_calls.append(("xrandr", "--query"))
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "fixture:inactive.query",
            self.query_text,
        )

    def properties(self) -> TextCommandEvidence:
        self.read_calls.append(("xrandr", "--props"))
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_PROPERTIES,
            "fixture:inactive.props",
            self.properties_text,
        )

    def activate(self, arguments: tuple[str, ...]) -> ProbeCommandResult:
        self.mutations.append(arguments)
        if self.on_activate is not None:
            self.on_activate()
        return ProbeCommandResult(
            self.mutation_status,
            timed_out=self.mutation_timed_out,
        )


class _SwitchingTree:
    def __init__(self, first: Path, second: Path) -> None:
        self._readers = (RootedSysfsReader(first), RootedSysfsReader(second))
        self._scan = -1

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        self._scan += 1
        return self._readers[min(self._scan, 1)].list_directories(pattern)

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        return self._readers[min(self._scan, 1)].read_bytes(relative_path, limit)


def _edid_bytes(name: str) -> bytes:
    return edid_fixture(EDID, name)


def _internal_edid() -> bytes:
    return saved_edid(PROFILE_SETUP, "eDP", wildcard_fill="0")


def _sysfs_tree(root: Path) -> Path:
    return write_sysfs_connectors(
        root,
        (
            ("card0-eDP-1", 73, _internal_edid()),
            ("card0-DP-3", 91, _edid_bytes("samsung-broken-captured.hex")),
        ),
    )


def _request(tree: ReadOnlyTree, commands: _FakeCommands) -> TransactionRequest:
    drm = sample_drm(tree)
    topology = derive_canonical_topology(drm, sample_xrandr(commands))
    external = next(
        item
        for item in drm.connectors
        if item.connected
        and item.edid.parsed is not None
        and item.edid.parsed.base_hash
        == parse_edid(_edid_bytes("samsung-broken-captured.hex")).base_hash
    )
    assert external.edid.parsed is not None
    assert external.edid.parsed.base_hash is not None
    return TransactionRequest(
        action_id=_ACTION,
        action_kind=ActionKind.PROBE,
        unit_name=_UNIT,
        physical_epoch=7,
        physical_token=topology.physical_token,
        admitted_event_generation=EventGeneration(41),
        observation_key=ObservationKey("admitted-samsung-invalid-extension"),
        output_mapping=(),
        expected_topology=ExpectedTopology(
            kernel_connected_outputs=topology.kernel_connected_outputs,
            kernel_external_outputs=topology.kernel_external_outputs,
            x_connected_outputs=topology.x_connected_outputs,
            x_active_outputs=topology.x_active_outputs,
        ),
        profile=_PROFILE,
        payload=(
            ("base_identity_hash", external.edid.parsed.base_hash),
            (
                "edid_integrity",
                EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID.value,
            ),
            ("internal_output", "eDP"),
            ("preferred_mode", "5120x2160"),
            ("probe_output", "DisplayPort-9"),
        ),
    )


def _startup(
    tmp_path: Path,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
    *,
    mutate_request: Callable[[TransactionRequest], TransactionRequest] | None = None,
) -> tuple[WorkerStartup, TransactionStore]:
    request = _request(tree, commands)
    commands.read_calls.clear()
    if mutate_request is not None:
        request = mutate_request(request)
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(request)
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.PROBE,
    )
    return startup, store


def test_samsung_invalid_extension_fixture_runs_only_exact_admitted_argv(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands)

    assert execute_probe(startup, drm_tree=tree, commands=commands) == 0

    result = store.read_result(_ACTION)
    assert tuple(commands.read_calls) == _EXPECTED_READ_CALLS
    assert commands.mutations == [_EXPECTED_ARGV]
    assert result.outcome is ActionLifecycle.COMPLETED
    assert result.exit_status == 0
    assert result.request_sha256 == startup.request.request_sha256
    assert startup.execution_claim == store.read_execution_claim(_ACTION)


def test_command_failure_is_preserved_as_the_exact_terminal_result(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands(mutation_status=23)
    startup, store = _startup(tmp_path, tree, commands)

    assert execute_probe(startup, drm_tree=tree, commands=commands) == 23

    result = store.read_result(_ACTION)
    assert commands.mutations == [_EXPECTED_ARGV]
    assert result.outcome is ActionLifecycle.FAILED
    assert result.exit_status == 23
    assert result.detail == "xrandr activation exited with status 23"


def test_command_timeout_is_timed_out_not_failed(tmp_path: Path) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands(mutation_status=124, mutation_timed_out=True)
    startup, store = _startup(tmp_path, tree, commands)

    assert execute_probe(startup, drm_tree=tree, commands=commands) == 124

    result = store.read_result(_ACTION)
    assert commands.mutations == [_EXPECTED_ARGV]
    assert result.outcome is ActionLifecycle.TIMED_OUT
    assert result.exit_status == 124
    assert result.detail == "xrandr activation timed out"


@pytest.mark.parametrize(
    ("case", "change"),
    [
        ("physical-token", _changed_token),
        ("connected-topology", _changed_connected_topology),
        ("active-topology", _changed_active_topology),
    ],
)
def test_immutable_topology_guard_changes_are_structured_stale(
    tmp_path: Path,
    case: str,
    change: Callable[[TransactionRequest], TransactionRequest],
) -> None:
    del case
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands, mutate_request=change)

    assert execute_probe(startup, drm_tree=tree, commands=commands) == STALE_EXIT_STATUS

    result = store.read_result(_ACTION)
    assert result.outcome is ActionLifecycle.FAILED
    assert result.exit_status == STALE_EXIT_STATUS
    assert result.detail.startswith("STALE:")
    assert commands.mutations == []


@pytest.mark.parametrize(
    ("case", "query_change", "properties_change"),
    [
        ("target-became-active", _target_active, _target_active),
        ("internal-became-inactive", _internal_inactive, _internal_inactive),
        ("query-preferred-mode-changed", _preferred_mode_changed, _unchanged),
        ("properties-preferred-mode-changed", _unchanged, _preferred_mode_changed),
        ("query-mode-list-changed", _mode_list_changed, _unchanged),
        ("properties-mode-list-changed", _unchanged, _mode_list_changed),
        ("query-current-marker-changed", _current_marker_changed, _unchanged),
        ("properties-current-marker-changed", _unchanged, _current_marker_changed),
        ("malformed-xrandr", _malformed_query, _unchanged),
    ],
)
def test_fresh_xrandr_guard_changes_are_structured_stale(
    tmp_path: Path,
    case: str,
    query_change: Callable[[str], str],
    properties_change: Callable[[str], str],
) -> None:
    del case
    root = _sysfs_tree(tmp_path / "sysfs")
    admitted = _FakeCommands()
    startup, store = _startup(tmp_path, RootedSysfsReader(root), admitted)
    query = query_change((XRANDR / "inactive.query").read_text(encoding="utf-8"))
    properties = properties_change(
        (XRANDR / "inactive.props").read_text(encoding="utf-8")
    )
    commands = _FakeCommands(query=query, properties=properties)

    assert (
        execute_probe(
            startup,
            drm_tree=RootedSysfsReader(root),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.mutations == []


@pytest.mark.parametrize(
    ("drm_connector_id", "x_connector_id"),
    [
        (None, 91),
        (92, 91),
        (91, None),
        (91, 92),
    ],
)
def test_target_requires_fresh_equal_nonnull_connector_ids_before_mutation(
    tmp_path: Path,
    drm_connector_id: int | None,
    x_connector_id: int | None,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    admitted = _FakeCommands()
    startup, store = _startup(tmp_path, RootedSysfsReader(root), admitted)
    connector_id_path = root / "card0-DP-3" / "connector_id"
    if drm_connector_id is None:
        connector_id_path.unlink()
    else:
        connector_id_path.write_text(f"{drm_connector_id}\n", encoding="ascii")
    properties = (XRANDR / "inactive.props").read_text(encoding="utf-8")
    if x_connector_id is None:
        properties = properties.replace("\tCONNECTOR_ID: 91\n", "", 1)
    else:
        properties = properties.replace(
            "\tCONNECTOR_ID: 91\n",
            f"\tCONNECTOR_ID: {x_connector_id}\n",
            1,
        )
    commands = _FakeCommands(properties=properties)

    assert (
        execute_probe(
            startup,
            drm_tree=RootedSysfsReader(root),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.mutations == []


@pytest.mark.parametrize(
    ("case", "replacement"),
    [
        ("extensions-settled", "samsung-settled-synthetic.hex"),
        ("base-identity-changed", "valid-base.hex"),
    ],
)
def test_fresh_edid_guard_changes_are_structured_stale(
    tmp_path: Path,
    case: str,
    replacement: str,
) -> None:
    del case
    admitted_root = _sysfs_tree(tmp_path / "admitted")
    commands = _FakeCommands()
    startup, store = _startup(
        tmp_path,
        RootedSysfsReader(admitted_root),
        commands,
    )
    current_root = tmp_path / "current"
    shutil.copytree(admitted_root, current_root)
    current_root.joinpath("card0-DP-3", "edid").write_bytes(_edid_bytes(replacement))

    assert (
        execute_probe(
            startup,
            drm_tree=RootedSysfsReader(current_root),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.mutations == []


def test_broken_extension_state_change_is_stale_without_mutation(
    tmp_path: Path,
) -> None:
    admitted_root = _sysfs_tree(tmp_path / "admitted")
    commands = _FakeCommands()
    startup, store = _startup(
        tmp_path,
        RootedSysfsReader(admitted_root),
        commands,
    )
    current_root = tmp_path / "current"
    shutil.copytree(admitted_root, current_root)
    incomplete = _edid_bytes("samsung-settled-synthetic.hex")[:256]
    admitted = parse_edid(_edid_bytes("samsung-broken-captured.hex"))
    changed = parse_edid(incomplete)
    assert changed.base_hash == admitted.base_hash
    assert changed.integrity is EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE
    current_root.joinpath("card0-DP-3", "edid").write_bytes(incomplete)

    assert (
        execute_probe(
            startup,
            drm_tree=RootedSysfsReader(current_root),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.mutations == []


def test_duplicate_live_base_identity_is_stale_without_mutation(tmp_path: Path) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, RootedSysfsReader(root), commands)
    root.joinpath("card0-eDP-1", "edid").write_bytes(
        _edid_bytes("samsung-broken-captured.hex")
    )

    assert (
        execute_probe(
            startup,
            drm_tree=RootedSysfsReader(root),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.mutations == []


def test_drm_change_during_worker_sample_is_stale_without_mutation(
    tmp_path: Path,
) -> None:
    first = _sysfs_tree(tmp_path / "first")
    second = tmp_path / "second"
    shutil.copytree(first, second)
    second.joinpath("card0-DP-3", "status").write_text(
        "disconnected\n", encoding="ascii"
    )
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, RootedSysfsReader(first), commands)

    assert (
        execute_probe(
            startup,
            drm_tree=_SwitchingTree(first, second),
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert "changed during" in store.read_result(_ACTION).detail
    assert commands.mutations == []


def test_missing_execution_claim_is_stale_without_mutation(tmp_path: Path) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands)

    assert (
        execute_probe(
            replace(startup, execution_claim=None),
            drm_tree=tree,
            commands=commands,
        )
        == STALE_EXIT_STATUS
    )
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.read_calls == []
    assert commands.mutations == []


@pytest.mark.parametrize(
    ("lifecycle", "exit_status"),
    [
        (ActionLifecycle.CANCELLED, CANCELLED_EXIT_STATUS),
        (ActionLifecycle.TIMED_OUT, 124),
        (ActionLifecycle.UNKNOWN, 70),
    ],
)
def test_stop_intent_lifecycle_is_preserved_before_and_during_command(
    tmp_path: Path,
    lifecycle: ActionLifecycle,
    exit_status: int,
) -> None:
    before_root = _sysfs_tree(tmp_path / "before" / "sysfs")
    before_tree = RootedSysfsReader(before_root)
    before_commands = _FakeCommands()
    before_startup, before_store = _startup(
        tmp_path / "before", before_tree, before_commands
    )
    before_store.create_stop_intent(_ACTION, lifecycle)

    assert (
        execute_probe(
            before_startup,
            drm_tree=before_tree,
            commands=before_commands,
        )
        == exit_status
    )
    assert before_commands.mutations == []
    assert before_store.read_result(_ACTION).outcome is lifecycle

    during_root = _sysfs_tree(tmp_path / "during" / "sysfs")
    during_tree = RootedSysfsReader(during_root)
    during_commands = _FakeCommands()
    during_startup, during_store = _startup(
        tmp_path / "during", during_tree, during_commands
    )

    def create_intent_during_mutation() -> None:
        _ = during_store.create_stop_intent(_ACTION, lifecycle)
        raise WorkerCancelled

    during_commands.on_activate = create_intent_during_mutation

    assert (
        execute_probe(
            during_startup,
            drm_tree=during_tree,
            commands=during_commands,
        )
        == exit_status
    )
    assert during_commands.mutations == [_EXPECTED_ARGV]
    assert during_store.read_result(_ACTION).outcome is lifecycle


def test_sigterm_without_stop_intent_defers_result_to_exec_stop_post(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands(on_activate=_cancel_during_mutation)
    startup, store = _startup(tmp_path, tree, commands)

    with pytest.raises(WorkerCancelled):
        execute_probe(startup, drm_tree=tree, commands=commands)

    assert commands.mutations == [_EXPECTED_ARGV]
    assert store.result_if_present(_ACTION) is None


def test_repeat_invocation_and_late_result_cannot_replace_first_result(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands(mutation_status=23)
    startup, store = _startup(tmp_path, tree, commands)
    assert execute_probe(startup, drm_tree=tree, commands=commands) == 23
    first = store.read_result(_ACTION)

    with pytest.raises(WorkerStartupError, match=r"terminal result|already claimed"):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.PROBE,
        )
    with pytest.raises(ImmutableTransactionError):
        write_worker_result(
            startup,
            execution=WorkerExecution(ActionLifecycle.CANCELLED, 143, "late cancel"),
            started_monotonic_ms=100,
            finished_monotonic_ms=101,
        )
    assert store.read_result(_ACTION) == first
    assert commands.mutations == [_EXPECTED_ARGV]


def test_startup_rejects_unit_action_and_hash_substitution_before_commands(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    request = _request(tree, commands)
    commands.read_calls.clear()
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(request)
    store.claim_submission(_ACTION)

    with pytest.raises(WorkerStartupError, match="invoked unit"):
        _ = validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name="monitor-probe@substituted.service",
            expected_kind=ActionKind.PROBE,
        )
    with pytest.raises(WorkerStartupError, match="application worker"):
        _ = validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.APPLICATION,
        )

    request_path = store.action_directory(_ACTION) / "request.json"
    raw = json.loads(request_path.read_bytes())
    raw["request_sha256"] = "sha256:" + "0" * 64
    request_path.unlink()
    request_path.write_text(json.dumps(raw), encoding="utf-8")
    request_path.chmod(0o600)
    with pytest.raises(WorkerStartupError, match="immutable worker request"):
        _ = validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.PROBE,
        )
    assert commands.read_calls == []
    assert commands.mutations == []


def test_probe_request_protocol_rejects_missing_or_nonbroken_proof() -> None:
    topology = ExpectedTopology(
        ("DisplayPort-9", "eDP"),
        ("DisplayPort-9",),
        ("DisplayPort-9", "eDP"),
        ("eDP",),
    )

    def protocol_request(
        payload: tuple[tuple[str, str], ...],
    ) -> TransactionRequest:
        return TransactionRequest(
            action_id=_ACTION,
            action_kind=ActionKind.PROBE,
            unit_name=_UNIT,
            physical_epoch=1,
            physical_token=PhysicalToken("proof"),
            admitted_event_generation=EventGeneration(1),
            observation_key=ObservationKey("proof"),
            output_mapping=(),
            expected_topology=topology,
            profile=_PROFILE,
            payload=payload,
        )

    with pytest.raises(TransactionProtocolError, match="five proof fields"):
        protocol_request(())
    with pytest.raises(TransactionProtocolError, match="broken extension"):
        protocol_request(
            (
                ("base_identity_hash", "0" * 64),
                ("edid_integrity", EdidIntegrity.COMPLETE.value),
                ("internal_output", "eDP"),
                ("preferred_mode", "5120x2160"),
                ("probe_output", "DisplayPort-9"),
            )
        )
    incomplete = protocol_request(
        (
            ("base_identity_hash", "0" * 64),
            (
                "edid_integrity",
                EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE.value,
            ),
            ("internal_output", "eDP"),
            ("preferred_mode", "5120x2160"),
            ("probe_output", "DisplayPort-9"),
        )
    )
    assert incomplete.action_kind is ActionKind.PROBE
    with pytest.raises(TransactionProtocolError, match="layout or staged-plan"):
        replace(incomplete, layout="forbidden-layout")


def test_probe_module_has_no_forbidden_orchestration_surface() -> None:
    source = (
        Path(__file__).parents[2] / "monitor_controller" / "workers" / "probe.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "import autorandr",
        "from monitor_controller.observer.autorandr",
        "--auto",
        "--primary",
        "setup-monitor",
        "postswitch",
        "autorandr --",
        "libdpy",
    ):
        assert forbidden not in source
