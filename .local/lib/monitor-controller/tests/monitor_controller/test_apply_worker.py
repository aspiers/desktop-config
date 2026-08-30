"""Guard, command, evidence, and at-most-once contracts for profile apply."""

from __future__ import annotations

import json
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from worker_evidence import edid_fixture, saved_edid, write_sysfs_connectors

import monitor_controller.workers.apply as apply_module
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EventGeneration,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    RawEvidenceSource,
)
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import ReadOnlyTree, RootedSysfsReader, sample_drm
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import sample_xrandr
from monitor_controller.runtime.transactions import (
    ExpectedTopology,
    ImmutableTransactionError,
    TransactionArtifact,
    TransactionProtocolError,
    TransactionRequest,
    TransactionStore,
)
from monitor_controller.workers.apply import (
    ApplyCommandResult,
    SubprocessApplyCommands,
    execute_application,
    isolated_application_environment,
)
from monitor_controller.workers.autorandr_profile import (
    POSTSWITCH_EVIDENCE_ENVIRONMENT,
    materialize_autorandr_profile,
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

FIXTURES = Path(__file__).parent / "fixtures"
XRANDR = FIXTURES / "xrandr"
EDID = FIXTURES / "edid"
PROFILES = FIXTURES / "autorandr" / "profiles"
_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_ACTION = ActionId(_INSTANCE, ActionKind.APPLICATION, 37)
_UNIT = f"monitor-apply@{_ACTION.value}.service"
_PROFILE = "celtic+Samsung-Odyssey-G75F"
_MAPPING = (
    OutputMapping("DisplayPort-1", "DisplayPort-9"),
    OutputMapping("eDP", "eDP"),
)
_EXPECTED_ARGV: Final = (
    "autorandr",
    "--skip-options",
    "gamma",
    "--load",
    _ACTION.value,
)
_EXPECTED_READ_CALLS: Final = (
    ("xrandr", "--query"),
    ("xrandr", "--props"),
)
_BASE_ENVIRONMENT: Final = {
    "BASH_FUNC_autorandr%%": "() { touch /tmp/escaped; }",
    "DISPLAY": ":77",
    "HOME": "/original/home",
    "LANG": "attacker-locale",
    "LD_PRELOAD": "/forbidden.so",
    "PATH": "/attacker/bin",
    "PYTHONPATH": "/attacker/python",
    "PYTHONUSERBASE": "/attacker/user-site",
    "RUBYOPT": "forbidden",
    "WAYLAND_DISPLAY": "wayland-test",
    "XAUTHORITY": "/etc/hosts",
    "XDG_CONFIG_HOME": "/attacker/config",
}


def _evidence(reference: str, text: str) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        reference,
        text,
    )


def _profile(name: str = _PROFILE) -> SavedAutorandrProfile:
    root = PROFILES / name
    layout = root / "layout"
    parsed = parse_saved_profile(
        name,
        _evidence("profile/config", (root / "config").read_text()),
        _evidence("profile/setup", (root / "setup").read_text()),
        _evidence("profile/layout", layout.read_text()) if layout.exists() else None,
    )
    assert parsed.valid
    assert parsed.profile is not None
    return parsed.profile


def _edid_bytes(name: str) -> bytes:
    return edid_fixture(EDID, name)


def _saved_edid(output: str) -> bytes:
    return saved_edid(PROFILES / _PROFILE / "setup", output, wildcard_fill="00" * 32)


def _sysfs_tree(root: Path) -> Path:
    return write_sysfs_connectors(
        root,
        (
            ("card0-eDP-1", 73, _saved_edid("eDP")),
            ("card0-DP-3", 91, _edid_bytes("samsung-broken-captured.hex")),
        ),
    )


def _single_output_sysfs_tree(root: Path, edid: bytes) -> Path:
    connector = root / "card0-eDP-1"
    connector.mkdir(parents=True)
    connector.joinpath("status").write_text("connected\n", encoding="ascii")
    connector.joinpath("connector_id").write_text("73\n", encoding="ascii")
    connector.joinpath("edid").write_bytes(edid)
    return root


class _FakeCommands:
    def __init__(  # noqa: PLR0913
        self,
        *,
        query: str | None = None,
        properties: str | None = None,
        status: int = 0,
        timed_out: bool = False,
        evidence: str | None = "DisplayPort-9:eDP\n",
        on_load: Callable[[], None] | None = None,
    ) -> None:
        self.query_text = query or (XRANDR / "inactive.query").read_text()
        self.properties_text = properties or (XRANDR / "inactive.props").read_text()
        self.status = status
        self.timed_out = timed_out
        self.evidence = evidence
        self.on_load = on_load
        self.read_calls: list[tuple[str, ...]] = []
        self.loads: list[tuple[tuple[str, ...], dict[str, str]]] = []

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

    def load(
        self,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> ApplyCommandResult:
        copied = dict(environment)
        self.loads.append((arguments, copied))
        if self.on_load is not None:
            self.on_load()
        if self.evidence is not None and self.status == 0 and not self.timed_out:
            path = Path(copied[POSTSWITCH_EVIDENCE_ENVIRONMENT])
            path.write_text(self.evidence, encoding="utf-8")
            path.chmod(0o600)
        return ApplyCommandResult(self.status, timed_out=self.timed_out)


def _request(
    tree: ReadOnlyTree,
    commands: _FakeCommands,
    profile: SavedAutorandrProfile | None = None,
    mapping: tuple[OutputMapping, ...] = _MAPPING,
) -> tuple[TransactionRequest, tuple[TransactionArtifact, ...]]:
    topology = derive_canonical_topology(sample_drm(tree), sample_xrandr(commands))
    profile = _profile() if profile is None else profile
    materialized = materialize_autorandr_profile(profile, mapping, _ACTION.value)
    return (
        TransactionRequest(
            action_id=_ACTION,
            action_kind=ActionKind.APPLICATION,
            unit_name=_UNIT,
            physical_epoch=7,
            physical_token=topology.physical_token,
            admitted_event_generation=EventGeneration(41),
            observation_key=ObservationKey("admitted-samsung-application"),
            output_mapping=mapping,
            expected_topology=ExpectedTopology(
                topology.kernel_connected_outputs,
                topology.kernel_external_outputs,
                topology.x_connected_outputs,
                topology.x_active_outputs,
            ),
            profile=profile.name,
            payload=materialized.payload,
        ),
        materialized.artifacts,
    )


def _startup(  # noqa: PLR0913
    tmp_path: Path,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
    *,
    mutate_request: Callable[[TransactionRequest], TransactionRequest] | None = None,
    profile: SavedAutorandrProfile | None = None,
    mapping: tuple[OutputMapping, ...] = _MAPPING,
) -> tuple[WorkerStartup, TransactionStore]:
    request, artifacts = _request(tree, commands, profile, mapping)
    commands.read_calls.clear()
    if mutate_request is not None:
        request = mutate_request(request)
    store = TransactionStore(tmp_path / "transactions")
    request = store.create_request(request, artifacts)
    store.claim_submission(request.action_id)
    startup = validate_worker_startup(
        transaction_root=store.root,
        action_id_text=request.action_id.value,
        unit_name=request.unit_name,
        expected_kind=ActionKind.APPLICATION,
    )
    return startup, store


def _execute(
    startup: WorkerStartup,
    tree: ReadOnlyTree,
    commands: _FakeCommands,
) -> int:
    return execute_application(
        startup,
        drm_tree=tree,
        commands=commands,
        base_environment=_BASE_ENVIRONMENT,
    )


def test_exact_transaction_environment_argv_and_post_action_evidence(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands)

    assert _execute(startup, tree, commands) == 0

    assert tuple(commands.read_calls) == _EXPECTED_READ_CALLS
    assert len(commands.loads) == 1
    arguments, environment = commands.loads[0]
    artifact_root = store.artifact_directory(_ACTION)
    assert arguments == _EXPECTED_ARGV
    assert set(environment) == {
        "DISPLAY",
        "HOME",
        "LANG",
        "MONITOR_CONTROLLER_AUTORANDR_ACTION_ID",
        "MONITOR_CONTROLLER_AUTORANDR_POSTSWITCH_EVIDENCE",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "XAUTHORITY",
        "XDG_CONFIG_DIRS",
        "XDG_CONFIG_HOME",
    }
    assert environment["DISPLAY"] == ":77"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["XAUTHORITY"] == "/etc/hosts"
    assert environment["HOME"] == str(artifact_root / "home")
    assert environment["XDG_CONFIG_HOME"] == str(artifact_root / "xdg-config")
    assert environment["XDG_CONFIG_DIRS"] == str(artifact_root / "xdg-config-dirs")
    assert environment["MONITOR_CONTROLLER_AUTORANDR_ACTION_ID"] == _ACTION.value
    assert environment[POSTSWITCH_EVIDENCE_ENVIRONMENT].endswith(
        f"/{_ACTION.value}/enabled-outputs"
    )
    assert "LD_PRELOAD" not in environment
    assert "WAYLAND_DISPLAY" not in environment
    result = store.read_result(_ACTION)
    assert result.outcome is ActionLifecycle.COMPLETED
    assert result.exit_status == 0
    assert "DisplayPort-9,eDP" in result.detail


@pytest.mark.parametrize(
    "environment",
    [
        {"XAUTHORITY": "/authority"},
        {"DISPLAY": ":0"},
        {"DISPLAY": ":0\nINJECTED=1", "XAUTHORITY": "/authority"},
        {"DISPLAY": ":0", "XAUTHORITY": "relative"},
    ],
)
def test_application_requires_safe_x_environment(
    tmp_path: Path,
    environment: Mapping[str, str],
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, _store = _startup(tmp_path, tree, commands)

    with pytest.raises(WorkerStartupError, match=r"DISPLAY|HOME|XAUTHORITY"):
        isolated_application_environment(startup, _ACTION.value, environment)


def test_application_resolves_manager_home_authority_before_isolation(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands)
    original_home = tmp_path / "manager-home"
    original_home.mkdir()
    authority = original_home / ".Xauthority"
    authority.write_bytes(b"session-cookie")

    environment = isolated_application_environment(
        startup,
        _ACTION.value,
        {
            "DISPLAY": ":0",
            "HOME": str(original_home),
            "HOME_INJECTION": "/attacker",
            "LD_PRELOAD": "/attacker/library.so",
        },
    )

    assert environment["XAUTHORITY"] == str(authority.resolve())
    assert environment["HOME"] == str(store.artifact_directory(_ACTION) / "home")
    assert "HOME_INJECTION" not in environment
    assert "LD_PRELOAD" not in environment


def test_application_preserves_validated_inherited_temp_authority(
    tmp_path: Path,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, _store = _startup(tmp_path, tree, commands)
    authority = tmp_path / "session" / "Xauthority"
    authority.parent.mkdir()
    authority.write_bytes(b"session-cookie")
    inherited = str(authority.parent / ".." / "session" / authority.name)

    environment = isolated_application_environment(
        startup,
        _ACTION.value,
        {
            "DISPLAY": ":0",
            "HOME": str(tmp_path / "unused-home"),
            "XAUTHORITY": inherited,
        },
    )

    assert environment["XAUTHORITY"] == inherited


def test_application_rejects_unreadable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, _store = _startup(tmp_path, tree, commands)
    authority = tmp_path / "Xauthority"
    authority.write_bytes(b"session-cookie")

    def deny_authority_open(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(Path, "open", deny_authority_open)

    with pytest.raises(WorkerStartupError, match="readable regular X11 authority"):
        isolated_application_environment(
            startup,
            _ACTION.value,
            {"DISPLAY": ":0", "XAUTHORITY": str(authority)},
        )


@pytest.mark.parametrize("home", ["relative/home", "/safe/home\nINJECTED=value"])
def test_application_rejects_home_fallback_escape_or_injection(
    tmp_path: Path,
    home: str,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, _store = _startup(tmp_path, tree, commands)

    with pytest.raises(WorkerStartupError, match="safe HOME"):
        isolated_application_environment(
            startup,
            _ACTION.value,
            {"DISPLAY": ":0", "HOME": home},
        )


def test_installed_autorandr_115_accepts_isolated_materialized_profile(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_autorandr = shutil.which("autorandr")
    if real_autorandr is None:
        pytest.skip("installed autorandr is unavailable")
    if '__version__ = "1.15"' not in Path(real_autorandr).read_text(
        encoding="utf-8",
        errors="replace",
    ):
        pytest.skip("integration contract is specific to installed autorandr 1.15")

    profile = _profile("celtic")
    fingerprint = next(item.value for item in profile.setup if item.output == "eDP")
    edid = bytes.fromhex(fingerprint)
    tree = RootedSysfsReader(_single_output_sysfs_tree(tmp_path / "sysfs", edid))
    query = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384
eDP connected primary 1920x1080+0+0 (normal left inverted right x axis y axis)
   1920x1080     60.00*+
"""
    properties = """\
eDP connected primary 1920x1080+0+0 (normal left inverted right x axis y axis)
\tCONNECTOR_ID: 73
   1920x1080     60.00*+
"""
    admitted_commands = _FakeCommands(query=query, properties=properties)
    startup, store = _startup(
        tmp_path,
        tree,
        admitted_commands,
        profile=profile,
        mapping=(OutputMapping("eDP", "eDP"),),
    )
    artifact_root = store.artifact_directory(_ACTION)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    verbose = tmp_path / "xrandr.verbose"
    connection = (
        "eDP connected primary 1920x1080+0+0 (0x1) normal "
        "(normal left inverted right x axis y axis) 300mm x 200mm"
    )
    verbose.write_text(
        f"""\
Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767
{connection}
\tCRTC: 0
\tEDID:
\t\t{fingerprint}
  1920x1080 (0x1) 148.500MHz +HSync +VSync *current +preferred
        h: width  1920 start 2008 end 2052 total 2200 clock 67.50KHz
        v: height 1080 start 1084 end 1089 total 1125 clock 60.00Hz
""",
        encoding="utf-8",
    )
    query_file = tmp_path / "xrandr.query"
    query_file.write_text(query, encoding="utf-8")
    properties_file = tmp_path / "xrandr.props"
    properties_file.write_text(properties, encoding="utf-8")
    xrandr_log = tmp_path / "xrandr.log"
    fake_xrandr = fake_bin / "xrandr"
    fake_xrandr.write_text(
        f"""#!/bin/sh
set -eu
printf 'args=%s\\n' "$*" >> {shlex.quote(str(xrandr_log))}
if [ "$#" -eq 1 ] && [ "$1" = --query ]; then
    exec /bin/cat {shlex.quote(str(query_file))}
fi
if [ "$#" -eq 1 ] && [ "$1" = --props ]; then
    exec /bin/cat {shlex.quote(str(properties_file))}
fi
if [ "$#" -eq 2 ] && [ "$1" = -q ] && [ "$2" = --verbose ]; then
    exec /bin/cat {shlex.quote(str(verbose))}
fi
if [ "$#" -eq 1 ] && [ "$1" = -v ]; then
    printf '%s\\n' 'xrandr program version 1.5.2'
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_xrandr.chmod(0o700)

    autorandr_log = tmp_path / "autorandr.log"
    shell_escape = tmp_path / "application-shell-environment-escaped"
    python_escape = tmp_path / "application-python-environment-escaped"
    malicious_python = tmp_path / "host-python"
    malicious_python.mkdir()
    malicious_python.joinpath("sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(python_escape)!r}).touch()\n",
        encoding="utf-8",
    )
    wrapper = fake_bin / "autorandr"
    wrapper.write_text(
        f"""#!/bin/sh
set -eu
if command -v malicious-environment-function >/dev/null 2>&1; then
    malicious-environment-function
fi
{{
    printf 'argv=%s\\n' "$*"
    printf 'HOME=%s\\n' "$HOME"
    printf 'XDG_CONFIG_HOME=%s\\n' "$XDG_CONFIG_HOME"
    printf 'XDG_CONFIG_DIRS=%s\\n' "$XDG_CONFIG_DIRS"
    printf 'DISPLAY=%s\\n' "$DISPLAY"
    printf 'XAUTHORITY=%s\\n' "$XAUTHORITY"
}} > {shlex.quote(str(autorandr_log))}
exec {shlex.quote(real_autorandr)} "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    host_sentinel = tmp_path / "host-hook-ran"
    hook = f"""#!/bin/sh
: > {shlex.quote(str(host_sentinel))}
exit 0
"""
    host_home = tmp_path / "host-home"
    host_legacy = host_home / ".autorandr"
    host_legacy.mkdir(parents=True)
    host_config = tmp_path / "host-config" / "autorandr"
    host_config.mkdir(parents=True)
    for path in (host_legacy / "postswitch", host_config / "postswitch"):
        path.write_text(hook, encoding="utf-8")
        path.chmod(0o700)
    authority = tmp_path / "Xauthority"
    authority.write_bytes(b"test-cookie")

    environment = {
        "BASH_FUNC_malicious-environment-function%%": (
            f"() {{ touch {shlex.quote(str(shell_escape))}; }}"
        ),
        "DISPLAY": ":88",
        "HOME": str(host_home),
        "LANG": "attacker-locale",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PYTHONPATH": str(malicious_python),
        "RUBYOPT": "forbidden",
        "XAUTHORITY": str(authority),
        "XDG_CONFIG_DIRS": str(tmp_path / "host-config-dirs"),
        "XDG_CONFIG_HOME": str(tmp_path / "host-config"),
    }
    monkeypatch.setattr(apply_module, "_TRUSTED_PATH", environment["PATH"])
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert (
        execute_application(
            startup,
            drm_tree=tree,
            commands=SubprocessApplyCommands(),
            base_environment=environment,
        )
        == 0
    )

    expected_profile = _ACTION.value
    invocation = autorandr_log.read_text(encoding="utf-8")
    assert f"argv=--skip-options gamma --load {expected_profile}\n" in invocation
    assert f"HOME={artifact_root / 'home'}\n" in invocation
    assert f"XDG_CONFIG_HOME={artifact_root / 'xdg-config'}\n" in invocation
    assert f"XDG_CONFIG_DIRS={artifact_root / 'xdg-config-dirs'}\n" in invocation
    assert "DISPLAY=:88\n" in invocation
    assert f"XAUTHORITY={authority}\n" in invocation
    xrandr_calls = xrandr_log.read_text(encoding="utf-8").splitlines()
    assert xrandr_calls[:2] == ["args=--query", "args=--props"]
    assert any(
        "--output eDP" in item and "--mode 2880x1920" in item for item in xrandr_calls
    )
    evidence = (
        artifact_root
        / "xdg-config"
        / "autorandr"
        / expected_profile
        / "enabled-outputs"
    )
    assert evidence.read_text(encoding="utf-8") == "eDP\n"
    assert not host_sentinel.exists()
    assert not python_escape.exists()
    assert not shell_escape.exists()
    result = store.read_result(_ACTION)
    assert result.outcome is ActionLifecycle.COMPLETED
    # AUTORANDR_MONITORS is target-profile hook metadata.  Fresh DRM/XRandR
    # identity was proved before this subprocess and is not derived from it.
    assert "enabled outputs eDP" in result.detail


def test_subprocess_apply_timeout_kills_hook_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = tmp_path / "late-hook"
    command = tmp_path / "autorandr-hang"
    command.write_text(
        """#!/bin/sh
(sleep 0.15; : > "$TIMEOUT_SENTINEL") &
wait
""",
        encoding="utf-8",
    )
    command.chmod(0o700)
    monkeypatch.setattr(apply_module, "APPLICATION_COMMAND_TIMEOUT_SECONDS", 0.02)

    result = SubprocessApplyCommands().load(
        (str(command),),
        {"PATH": "/usr/bin:/bin", "TIMEOUT_SENTINEL": str(sentinel)},
    )

    assert result.timed_out
    assert result.exit_status == 124
    time.sleep(0.2)
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("status", "timed_out", "outcome", "detail"),
    [
        (23, False, ActionLifecycle.FAILED, "status 23"),
        (124, True, ActionLifecycle.TIMED_OUT, "timed out"),
    ],
)
def test_failure_and_timeout_are_exact_terminal_results(
    tmp_path: Path,
    status: int,
    *,
    timed_out: bool,
    outcome: ActionLifecycle,
    detail: str,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands(status=status, timed_out=timed_out)
    startup, store = _startup(tmp_path, tree, commands)

    assert _execute(startup, tree, commands) == status
    result = store.read_result(_ACTION)
    assert result.outcome is outcome
    assert detail in result.detail
    assert [item[0] for item in commands.loads] == [_EXPECTED_ARGV]


def _changed_physical_token(request: TransactionRequest) -> TransactionRequest:
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


@pytest.mark.parametrize(
    ("case", "change"),
    [
        ("physical-token", _changed_physical_token),
        ("connected-topology", _changed_connected_topology),
        ("active-topology", _changed_active_topology),
    ],
)
def test_immutable_topology_changes_are_stale_without_autorandr(
    tmp_path: Path,
    case: str,
    change: Callable[[TransactionRequest], TransactionRequest],
) -> None:
    del case
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands, mutate_request=change)

    assert _execute(startup, tree, commands) == STALE_EXIT_STATUS
    assert store.read_result(_ACTION).detail.startswith("STALE:")
    assert commands.loads == []


@pytest.mark.parametrize(("drm_id", "x_id"), [(92, 91), (91, 92)])
def test_contradictory_connector_identity_is_stale_without_autorandr(
    tmp_path: Path,
    drm_id: int,
    x_id: int,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    admitted = _FakeCommands()
    startup, store = _startup(tmp_path, RootedSysfsReader(root), admitted)
    root.joinpath("card0-DP-3", "connector_id").write_text(
        f"{drm_id}\n",
        encoding="ascii",
    )
    properties = (
        (XRANDR / "inactive.props")
        .read_text()
        .replace(
            "\tCONNECTOR_ID: 91\n",
            f"\tCONNECTOR_ID: {x_id}\n",
            1,
        )
    )
    commands = _FakeCommands(properties=properties)

    assert _execute(startup, RootedSysfsReader(root), commands) == STALE_EXIT_STATUS
    assert "identity" in store.read_result(_ACTION).detail
    assert commands.loads == []


@pytest.mark.parametrize("extension_state", ["incomplete", "invalid"])
def test_apply_accepts_matching_valid_base_with_broken_extensions(
    tmp_path: Path,
    extension_state: str,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    external = root / "card0-DP-3" / "edid"
    if extension_state == "incomplete":
        external.write_bytes(external.read_bytes()[:128])
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    startup, store = _startup(tmp_path, tree, commands)

    assert _execute(startup, tree, commands) == 0
    assert len(commands.loads) == 1
    assert store.read_result(_ACTION).outcome is ActionLifecycle.COMPLETED


@pytest.mark.parametrize("different_monitor", [False, True])
def test_wildcard_inside_saved_base_cannot_authorize_fresh_identity(
    tmp_path: Path,
    *,
    different_monitor: bool,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)
    commands = _FakeCommands()
    profile = _profile()
    saved_edp = next(item for item in profile.setup if item.output == "eDP")
    broad_base_wildcard = replace(saved_edp, value=saved_edp.value[:16] + "*")
    changed_profile = replace(
        profile,
        setup=tuple(
            broad_base_wildcard if item.output == "eDP" else item
            for item in profile.setup
        ),
    )
    startup, store = _startup(
        tmp_path,
        tree,
        commands,
        profile=changed_profile,
    )
    if different_monitor:
        root.joinpath("card0-eDP-1", "edid").write_bytes(_edid_bytes("valid-base.hex"))

    assert _execute(startup, RootedSysfsReader(root), commands) == STALE_EXIT_STATUS
    assert "cannot prove" in store.read_result(_ACTION).detail
    assert commands.loads == []


def test_changed_and_colliding_fresh_edid_identity_are_stale(
    tmp_path: Path,
) -> None:
    changed_root = _sysfs_tree(tmp_path / "changed" / "sysfs")
    changed_commands = _FakeCommands()
    changed_startup, changed_store = _startup(
        tmp_path / "changed",
        RootedSysfsReader(changed_root),
        changed_commands,
    )
    changed_root.joinpath("card0-DP-3", "edid").write_bytes(
        _edid_bytes("valid-base.hex")
    )
    assert (
        _execute(
            changed_startup,
            RootedSysfsReader(changed_root),
            changed_commands,
        )
        == STALE_EXIT_STATUS
    )
    assert "contradicts" in changed_store.read_result(_ACTION).detail
    assert changed_commands.loads == []

    collision_root = _sysfs_tree(tmp_path / "collision" / "sysfs")
    collision_root.joinpath("card0-eDP-1", "edid").write_bytes(
        _saved_edid("DisplayPort-1")
    )
    collision_commands = _FakeCommands()
    collision_profile = _profile()
    external_fingerprint = next(
        item for item in collision_profile.setup if item.output == "DisplayPort-1"
    )
    collision_profile = replace(
        collision_profile,
        setup=tuple(
            replace(item, value=external_fingerprint.value)
            if item.output == "eDP"
            else item
            for item in collision_profile.setup
        ),
    )
    collision_startup, collision_store = _startup(
        tmp_path / "collision",
        RootedSysfsReader(collision_root),
        collision_commands,
        profile=collision_profile,
    )
    assert (
        _execute(
            collision_startup,
            RootedSysfsReader(collision_root),
            collision_commands,
        )
        == STALE_EXIT_STATUS
    )
    assert "identities collide" in collision_store.read_result(_ACTION).detail
    assert collision_commands.loads == []


def test_bad_hash_malformed_manifest_and_extra_hook_are_rejected_before_load(
    tmp_path: Path,
) -> None:
    for name in ("bad-hash", "bad-manifest", "extra-hook"):
        root = _sysfs_tree(tmp_path / name / "sysfs")
        tree = RootedSysfsReader(root)
        commands = _FakeCommands()
        startup, store = _startup(tmp_path / name, tree, commands)
        if name == "bad-hash":
            config_path = next(
                item.relative_path
                for item in materialize_autorandr_profile(
                    _profile(), _MAPPING, _ACTION.value
                ).artifacts
                if item.relative_path.endswith("/config")
            )
            path = store.action_directory(_ACTION) / config_path
            path.unlink()
            path.write_bytes(b"output eDP\nmode 1x1\n")
            path.chmod(0o600)
        elif name == "bad-manifest":
            request_path = store.action_directory(_ACTION) / "request.json"
            raw = json.loads(request_path.read_bytes())
            raw["payload"]["config_sha256"] = "sha256:" + "0" * 64
            request_path.unlink()
            request_path.write_text(json.dumps(raw), encoding="utf-8")
            request_path.chmod(0o600)
        else:
            profile_root = (
                store.artifact_directory(_ACTION)
                / "xdg-config"
                / "autorandr"
                / _ACTION.value
            )
            extra = profile_root / "preswitch"
            extra.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            extra.chmod(0o700)

        if name == "bad-manifest":
            # Startup itself revalidates the independently bound request digest.
            with pytest.raises(
                TransactionProtocolError,
                match=r"hash|immutable|canonical",
            ):
                _ = store.read_request(_ACTION)
        else:
            assert _execute(startup, tree, commands) == STALE_EXIT_STATUS
            assert store.read_result(_ACTION).detail.startswith("STALE:")
        assert commands.loads == []


@pytest.mark.parametrize(
    ("evidence", "status", "outcome"),
    [
        (None, 0, ActionLifecycle.COMPLETED),
        ("eDP\n", 65, ActionLifecycle.FAILED),
        ("eDP:eDP\n", 65, ActionLifecycle.FAILED),
        ("eDP\nextra\n", 65, ActionLifecycle.FAILED),
    ],
)
def test_postswitch_evidence_is_optional_but_never_a_rename_mapping(
    tmp_path: Path,
    evidence: str | None,
    status: int,
    outcome: ActionLifecycle,
) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands(evidence=evidence)
    startup, store = _startup(tmp_path, tree, commands)

    assert _execute(startup, tree, commands) == status
    result = store.read_result(_ACTION)
    assert result.outcome is outcome
    assert len(commands.loads) == 1


@pytest.mark.parametrize(
    ("lifecycle", "exit_status"),
    [
        (ActionLifecycle.CANCELLED, CANCELLED_EXIT_STATUS),
        (ActionLifecycle.TIMED_OUT, 124),
        (ActionLifecycle.UNKNOWN, 70),
    ],
)
def test_stop_intent_before_and_during_load_preserves_terminal_lifecycle(
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
    assert _execute(before_startup, before_tree, before_commands) == exit_status
    assert before_commands.loads == []
    assert before_store.read_result(_ACTION).outcome is lifecycle

    during_root = _sysfs_tree(tmp_path / "during" / "sysfs")
    during_tree = RootedSysfsReader(during_root)
    during_commands = _FakeCommands(evidence=None)
    during_startup, during_store = _startup(
        tmp_path / "during", during_tree, during_commands
    )

    def cancel() -> None:
        _ = during_store.create_stop_intent(_ACTION, lifecycle)
        raise WorkerCancelled

    during_commands.on_load = cancel
    assert _execute(during_startup, during_tree, during_commands) == exit_status
    assert [item[0] for item in during_commands.loads] == [_EXPECTED_ARGV]
    assert during_store.read_result(_ACTION).outcome is lifecycle


def test_sigterm_without_intent_and_repeat_invocation_preserve_at_most_once(
    tmp_path: Path,
) -> None:
    root = _sysfs_tree(tmp_path / "sysfs")
    tree = RootedSysfsReader(root)

    def cancel_without_intent() -> None:
        raise WorkerCancelled

    commands = _FakeCommands(evidence=None, on_load=cancel_without_intent)
    startup, store = _startup(tmp_path, tree, commands)
    with pytest.raises(WorkerCancelled):
        _execute(startup, tree, commands)
    assert store.result_if_present(_ACTION) is None
    assert len(commands.loads) == 1
    with pytest.raises(WorkerStartupError, match="already claimed"):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.APPLICATION,
        )


def test_apply_module_has_no_implicit_selection_or_desktop_orchestration() -> None:
    source = (
        Path(__file__).parents[2] / "monitor_controller" / "workers" / "apply.py"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "--change",
        "--match-edid",
        "--force",
        "setup-monitor",
        "shell=True",
    ):
        assert forbidden not in source


def test_terminal_result_cannot_be_replaced_or_reexecuted(tmp_path: Path) -> None:
    tree = RootedSysfsReader(_sysfs_tree(tmp_path / "sysfs"))
    commands = _FakeCommands(status=23)
    startup, store = _startup(tmp_path, tree, commands)
    assert _execute(startup, tree, commands) == 23
    first = store.read_result(_ACTION)

    with pytest.raises(
        WorkerStartupError,
        match=r"terminal result|already claimed",
    ):
        validate_worker_startup(
            transaction_root=store.root,
            action_id_text=_ACTION.value,
            unit_name=_UNIT,
            expected_kind=ActionKind.APPLICATION,
        )
    with pytest.raises(ImmutableTransactionError):
        write_worker_result(
            startup,
            execution=WorkerExecution(ActionLifecycle.CANCELLED, 143, "late"),
            started_monotonic_ms=1,
            finished_monotonic_ms=2,
        )
    assert store.read_result(_ACTION) == first
    assert len(commands.loads) == 1
