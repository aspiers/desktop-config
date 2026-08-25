"""Composite contracts for canonical monitor snapshots and command execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from monitor_controller.model import (
    BootId,
    EventGeneration,
    ObservationInvalidityReason,
    ProfileScope,
    RawEvidenceSource,
)
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import ReadOnlyTree, RootedSysfsReader
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.snapshot import (
    CanonicalSnapshotCoordinator,
    ObserverCommands,
    StaticSavedProfiles,
)
from monitor_controller.runtime.commands import (
    BoundedCommandRunner,
    CommandRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = FIXTURES / "snapshots"
PROFILES = FIXTURES / "autorandr" / "profiles"
BOOT_ID = BootId(UUID("00000000-0000-4000-8000-000000000007"))


@dataclass
class _FakeClock:
    value: int = 100

    def monotonic_ms(self) -> int:
        self.value += 1
        return self.value


@dataclass
class _FakeGeneration:
    value: int = 4

    def current_generation(self) -> EventGeneration:
        return EventGeneration(self.value)


class _FakeBoot:
    def current_boot_id(self) -> BootId:
        return BOOT_ID


class _FixtureRunner:
    def __init__(
        self,
        commands: dict[str, Path],
        *,
        timed_out: str | None = None,
        generation: _FakeGeneration | None = None,
    ) -> None:
        self.commands = commands
        self.timed_out = timed_out
        self.generation = generation
        self.requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> TextCommandEvidence:
        self.requests.append(request)
        key = " ".join(request.arguments)
        if self.generation is not None and len(self.requests) == 2:
            self.generation.value += 1
        if key == self.timed_out:
            return TextCommandEvidence(
                request.source,
                request.reference,
                "partial output",
                exit_status=124,
                timed_out=True,
            )
        return TextCommandEvidence(
            request.source,
            request.reference,
            self.commands[key].read_text(encoding="utf-8"),
        )


class _SwitchingTree:
    """Switch roots between the beginning and ending DRM scans."""

    def __init__(self, first: Path, second: Path) -> None:
        self._readers = (RootedSysfsReader(first), RootedSysfsReader(second))
        self._scan = -1

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        self._scan += 1
        return self._readers[min(self._scan, 1)].list_directories(pattern)

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        return self._readers[min(self._scan, 1)].read_bytes(relative_path, limit)


def profile_evidence(reference: str, path: Path) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        reference,
        path.read_text(encoding="utf-8"),
    )


def load_profile(name: str) -> SavedAutorandrProfile:
    root = PROFILES / name
    layout = root / "layout"
    parsed = parse_saved_profile(
        name,
        profile_evidence(f"profiles/{name}/config", root / "config"),
        profile_evidence(f"profiles/{name}/setup", root / "setup"),
        profile_evidence(f"profiles/{name}/layout", layout)
        if layout.exists()
        else None,
    )
    assert parsed.profile is not None
    return parsed.profile


def load_manifest(
    tmp_path: Path, name: str
) -> tuple[Path, dict[str, Path], tuple[SavedAutorandrProfile, ...]]:
    raw = json.loads((SNAPSHOTS / f"{name}.json").read_text(encoding="utf-8"))
    data = cast("dict[str, object]", raw)
    connectors = cast("list[dict[str, object]]", data["sysfs"])
    root = tmp_path / name
    for connector in connectors:
        path = root / cast("str", connector["kernel_name"])
        path.mkdir(parents=True)
        path.joinpath("status").write_text("connected\n", encoding="ascii")
        path.joinpath("connector_id").write_text(
            f"{cast('int', connector['connector_id'])}\n", encoding="ascii"
        )
        edid_file = connector.get("edid_file")
        if edid_file is not None:
            edid = bytes.fromhex(
                (FIXTURES / cast("str", edid_file)).read_text(encoding="ascii")
            )
        else:
            source = (FIXTURES / cast("str", connector["edid_from"])).read_text(
                encoding="ascii"
            )
            output = cast("str", connector["fingerprint_output"])
            value = next(
                line.split()[1]
                for line in source.splitlines()
                if line.split()[0] == output
            )
            edid = bytes.fromhex(value.replace("*", "0"))
        path.joinpath("edid").write_bytes(edid)
    command_paths = {
        key: FIXTURES / value
        for key, value in cast("dict[str, str]", data["commands"]).items()
    }
    profiles = tuple(load_profile(item) for item in cast("list[str]", data["profiles"]))
    return root, command_paths, profiles


def coordinator(
    tree: ReadOnlyTree,
    runner: _FixtureRunner,
    profiles: tuple[SavedAutorandrProfile, ...],
    generation: _FakeGeneration | None = None,
    clock: _FakeClock | None = None,
) -> CanonicalSnapshotCoordinator:
    return CanonicalSnapshotCoordinator(
        drm_tree=tree,
        command_runner=runner,
        profiles=StaticSavedProfiles(profiles),
        boot_id_source=_FakeBoot(),
        clock=clock or _FakeClock(),
        event_generation_source=generation or _FakeGeneration(),
    )


def test_observation_key_is_derived_from_exact_x_geometry(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    baseline = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()
    changed_commands: dict[str, Path] = {}
    for index, (name, source) in enumerate(commands.items()):
        destination = tmp_path / "changed" / str(index)
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        if name in {"xrandr --query", "xrandr --props"}:
            assert "3840x2160+2880+0" in text
            text = text.replace("3840x2160+2880+0", "3840x2160+2881+0")
        destination.write_text(text, encoding="utf-8")
        changed_commands[name] = destination
    moved = coordinator(
        RootedSysfsReader(root), _FixtureRunner(changed_commands), profiles
    ).observe()

    assert baseline.valid
    assert moved.valid
    assert baseline.exact_profile == moved.exact_profile
    assert baseline.x_active_outputs == moved.x_active_outputs
    assert baseline.observation_key != moved.observation_key


def test_exact_profile_requires_complete_current_unique_mapping(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    runner = _FixtureRunner(commands)

    observation = coordinator(RootedSysfsReader(root), runner, profiles).observe()

    assert observation.valid
    assert observation.exact_profile == "celtic+AOC-U28G2G6B"
    assert observation.probe_candidate is None
    assert len(observation.eligible_profiles) == 1
    target = observation.eligible_profiles[0]
    assert target.scope is ProfileScope.MIXED
    assert tuple((item.saved_output, item.live_output) for item in target.mapping) == (
        ("DisplayPort-2", "DisplayPort-7"),
        ("eDP", "eDP"),
    )
    assert observation.kernel_connected_outputs == ("DisplayPort-7", "eDP")
    assert observation.x_connected_outputs == observation.kernel_connected_outputs
    assert [request.arguments for request in runner.requests] == [
        ("xrandr", "--query"),
        ("xrandr", "--props"),
        ("autorandr", "--match-edid", "--fingerprint"),
        ("autorandr", "--match-edid", "--detected"),
        ("autorandr", "--match-edid", "--current"),
    ]
    assert all(request.timeout_seconds == 5 for request in runner.requests)


def test_probe_is_derived_only_from_full_safe_composite_evidence(
    tmp_path: Path,
) -> None:
    root, commands, profiles = load_manifest(tmp_path, "probe-samsung")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert observation.exact_profile is None
    assert observation.eligible_profiles == ()
    assert observation.probe_candidate is not None
    assert observation.probe_candidate.profile == "celtic+Samsung-Odyssey-G75F"
    assert observation.probe_candidate.output == "DisplayPort-9"
    assert observation.probe_candidate.internal_output == "eDP"
    assert observation.probe_candidate.preferred_mode == "5120x2160"


def test_generation_change_fences_all_authorizing_derivations(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    generation = _FakeGeneration()
    runner = _FixtureRunner(commands, generation=generation)

    observation = coordinator(
        RootedSysfsReader(root), runner, profiles, generation
    ).observe()

    assert not observation.valid
    assert (
        observation.invalidity_reason
        is ObservationInvalidityReason.EVENT_GENERATION_CHANGED
    )
    assert observation.exact_profile is None
    assert observation.probe_candidate is None


def test_beginning_and_end_topology_change_is_torn(tmp_path: Path) -> None:
    first, commands, profiles = load_manifest(tmp_path / "first", "exact-aoc")
    second, _unused, _profiles = load_manifest(tmp_path / "second", "probe-samsung")

    observation = coordinator(
        _SwitchingTree(first, second), _FixtureRunner(commands), profiles
    ).observe()

    assert not observation.valid
    assert observation.invalidity_reason is ObservationInvalidityReason.TOPOLOGY_CHANGED
    assert observation.exact_profile is None
    assert observation.probe_candidate is None
    drm_references = {
        item.reference
        for item in observation.raw_evidence
        if item.source in {RawEvidenceSource.DRM_CONNECTORS, RawEvidenceSource.DRM_EDID}
    }
    assert any(reference.startswith("drm:begin:") for reference in drm_references)
    assert any(reference.startswith("drm:end:") for reference in drm_references)


def test_command_timeout_is_explicit_invalid_hashed_evidence(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")

    observation = coordinator(
        RootedSysfsReader(root),
        _FixtureRunner(commands, timed_out="autorandr --match-edid --fingerprint"),
        profiles,
    ).observe()

    assert not observation.valid
    assert (
        observation.invalidity_reason is ObservationInvalidityReason.COMMAND_TIMED_OUT
    )
    assert observation.exact_profile is None
    reference = next(
        item
        for item in observation.raw_evidence
        if item.source is RawEvidenceSource.AUTORANDR_FINGERPRINT
    )
    assert len(reference.sha256) == 64


def test_parse_failure_is_explicit_invalid_evidence(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    commands["xrandr --query"] = FIXTURES / "xrandr" / "malformed.query"

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert not observation.valid
    assert observation.invalidity_reason is ObservationInvalidityReason.PARSE_FAILED
    assert observation.exact_profile is None
    assert observation.probe_candidate is None


def test_cross_source_connector_contradiction_fails_closed(tmp_path: Path) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    (root / "card0-DP-3" / "connector_id").write_text("999\n", encoding="ascii")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert not observation.valid
    assert (
        observation.invalidity_reason
        is ObservationInvalidityReason.INCONSISTENT_EVIDENCE
    )
    assert observation.exact_profile is None


def test_observation_key_excludes_time_and_both_generation_sequences(
    tmp_path: Path,
) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    source = coordinator(
        RootedSysfsReader(root),
        _FixtureRunner(commands),
        profiles,
        clock=_FakeClock(),
    )

    first = source.observe()
    second = source.observe()

    assert first.observed_at_ms != second.observed_at_ms
    assert first.observation_generation != second.observation_generation
    assert first.observation_key == second.observation_key


def test_bounded_runner_uses_array_no_shell_and_preserves_stdout() -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def execute(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(cast("list[str]", args), 0, "ok\n", "")

    runner = BoundedCommandRunner(execute)
    evidence = runner.run(
        CommandRequest(
            ("xrandr", "--query"),
            RawEvidenceSource.XRANDR_QUERY,
            "test:xrandr",
            0.25,
        )
    )

    assert evidence.stdout == "ok\n"
    assert calls[0][0] == ("xrandr", "--query")
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["timeout"] == 0.25


def test_autorandr_commands_always_receive_the_explicit_isolated_environment(
    tmp_path: Path,
) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    runner = _FixtureRunner(commands)
    isolated = {
        "HOME": str(tmp_path / "isolated-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "isolated-config"),
        "XDG_CONFIG_DIRS": str(tmp_path / "isolated-config-dirs"),
    }

    CanonicalSnapshotCoordinator(
        drm_tree=RootedSysfsReader(root),
        command_runner=runner,
        profiles=StaticSavedProfiles(profiles),
        boot_id_source=_FakeBoot(),
        clock=_FakeClock(),
        event_generation_source=_FakeGeneration(),
        autorandr_environment=isolated,
    ).observe()

    xrandr_requests = runner.requests[:2]
    autorandr_requests = runner.requests[2:]
    assert all(request.environment is None for request in xrandr_requests)
    assert all(
        dict(request.environment or ()) == isolated for request in autorandr_requests
    )


def test_bounded_runner_kills_the_timed_out_command_process_group(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "child.pid"
    program = tmp_path / "spawn-child.py"
    program.write_text(
        """\
import pathlib
import subprocess
import sys
import time
child = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding=\"ascii\")
time.sleep(60)
""",
        encoding="utf-8",
    )

    evidence = BoundedCommandRunner().run(
        CommandRequest(
            (sys.executable, str(program), str(child_pid)),
            RawEvidenceSource.AUTORANDR_PROFILES,
            "test:process-group-timeout",
            0.5,
        )
    )

    assert evidence.timed_out
    assert child_pid.is_file()
    pid = int(child_pid.read_text(encoding="ascii"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"timed-out command descendant survived as PID {pid}")


def test_bounded_runner_converts_spawn_failure_to_command_evidence() -> None:
    def missing(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        message = "injected missing command"
        raise FileNotFoundError(message)

    evidence = BoundedCommandRunner(missing).run(
        CommandRequest(
            ("autorandr", "--current"),
            RawEvidenceSource.AUTORANDR_PROFILES,
            "test:missing",
            0.1,
        )
    )

    assert not evidence.timed_out
    assert evidence.exit_status == 127
    assert evidence.stdout == ""


def test_bounded_runner_converts_timeout_without_retry() -> None:
    def timeout(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(cast("list[str]", args), 0.1, output=b"partial")

    evidence = BoundedCommandRunner(timeout).run(
        CommandRequest(
            ("autorandr", "--current"),
            RawEvidenceSource.AUTORANDR_PROFILES,
            "test:timeout",
            0.1,
        )
    )

    assert evidence.timed_out
    assert evidence.exit_status == 124
    assert evidence.stdout == "partial"


@pytest.mark.parametrize("timeout", [0, -1, 30.1])
def test_command_request_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        CommandRequest(
            ("xrandr", "--query"),
            RawEvidenceSource.XRANDR_QUERY,
            "test:bad-timeout",
            timeout,
        )


def test_every_autorandr_query_matches_on_edid() -> None:
    """Connector numbering is not stable; monitor identity is.

    Regression for the live failure this caught: the AOC is saved on
    `DisplayPort-2` and currently sits on `DisplayPort-1`. Without
    `--match-edid`, `autorandr --detected` returns nothing, no profile is
    eligible, and the controller reports an unsupported topology for a
    monitor it can identify perfectly well. `bin/monitor-watcher-ng` has
    always passed the flag; the observer did not.
    """
    commands = ObserverCommands()
    for arguments in (
        commands.autorandr_fingerprint,
        commands.autorandr_detected,
        commands.autorandr_current,
    ):
        assert arguments[0] == "autorandr"
        assert "--match-edid" in arguments


def test_apply_path_does_not_match_on_edid() -> None:
    """Observation matches by EDID; application deliberately must not.

    The apply worker loads a transaction-local copy of the profile whose
    connector names are already rewritten to the proven live outputs and
    hashed into the request. Letting autorandr recompute that bijection at
    apply time would discard the admitted mapping. Asserted here as well as
    in the apply worker's own tests, because the two rules are easy to
    conflate once observation starts passing the flag.
    """
    source = (
        Path(__file__).parents[2] / "monitor_controller" / "workers" / "apply.py"
    ).read_text(encoding="utf-8")
    assert "--match-edid" not in source


def test_renamed_connector_still_resolves_to_an_exact_profile(
    tmp_path: Path,
) -> None:
    """The whole point: a monitor on a different connector is still matched.

    `exact-aoc` fixes the AOC's real EDID against a connector it was not
    saved on, so this fails outright if identity ever regresses to connector
    names.
    """
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert observation.exact_profile == "celtic+AOC-U28G2G6B"
    mapping = observation.eligible_profiles[0].mapping
    saved, live = mapping[0].saved_output, mapping[0].live_output
    assert saved != live, "fixture must exercise a renamed connector"
