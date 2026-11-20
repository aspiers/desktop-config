"""Composite contracts for canonical monitor snapshots and command execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from monitor_controller.model import (
    BootId,
    CanonicalObservation,
    ConfigurationContentHash,
    DisplayPower,
    EventGeneration,
    ObservationInvalidityReason,
    ProfileScope,
    RawEvidenceSource,
)
from monitor_controller.observer import snapshot as snapshot_module
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import ReadOnlyTree, RootedSysfsReader
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.snapshot import (
    CanonicalSnapshotCoordinator,
    ObserverCommands,
    PlanningConfigurationCapture,
    PlanningConfigurationChangedError,
    StaticSavedProfiles,
)
from monitor_controller.observer.suppression import (
    REPLUG_GRACE_SECONDS,
    OutputSuppression,
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
    command_paths.setdefault("xset q", FIXTURES / "xset" / "awake.txt")
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


def test_planning_manifest_refresh_does_not_change_profile_identity(
    tmp_path: Path,
) -> None:
    @dataclass
    class PlanningConfigurations:
        profile_hashes: tuple[ConfigurationContentHash, ...]
        revision: str = "before"

        def capture_planning_configuration(
            self,
            profile: str,
            layout: str,
        ) -> PlanningConfigurationCapture:
            assert profile == "celtic+AOC-U28G2G6B"
            assert layout == "celtic+external"
            return PlanningConfigurationCapture(
                (
                    ConfigurationContentHash(
                        ".fluxbox/keys.erb",
                        self.revision,
                    ),
                ),
                self.profile_hashes,
            )

    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    planning = PlanningConfigurations(profiles[0].configuration_hashes)
    subject = CanonicalSnapshotCoordinator(
        drm_tree=RootedSysfsReader(root),
        command_runner=_FixtureRunner(commands),
        profiles=StaticSavedProfiles(profiles),
        boot_id_source=_FakeBoot(),
        clock=_FakeClock(),
        event_generation_source=_FakeGeneration(),
        planning_configuration_source=planning,
    )

    before = subject.observe()
    planning.revision = "after"
    after = subject.observe()

    assert before.observation_key == after.observation_key
    assert (
        before.eligible_profiles[0].configuration_hashes
        == after.eligible_profiles[0].configuration_hashes
    )
    assert (
        before.eligible_profiles[0].planning_configuration_hashes
        != after.eligible_profiles[0].planning_configuration_hashes
    )

    planning.profile_hashes = (
        ConfigurationContentHash("autorandr:changed/config", "changed"),
    )
    with pytest.raises(PlanningConfigurationChangedError):
        subject.observe()


def test_unreadable_planning_input_does_not_fail_the_observation(
    tmp_path: Path,
) -> None:
    """One profile's broken desktop config must not blind the controller.

    A symlinked overlay once failed every observation for minutes (dc-ss34).
    The profile stays eligible without planning hashes, so it is still
    applied and planning reports the failure for that profile alone.
    """

    @dataclass
    class PlanningConfigurations:
        profile_hashes: tuple[ConfigurationContentHash, ...]
        error: Exception | None

        def capture_planning_configuration(
            self,
            profile: str,
            layout: str,
        ) -> PlanningConfigurationCapture:
            del profile, layout
            if self.error is not None:
                raise self.error
            return PlanningConfigurationCapture(
                (ConfigurationContentHash(".fluxbox/keys.erb", "fixed"),),
                self.profile_hashes,
            )

    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    for error in (ValueError("cannot safely open"), PermissionError("denied")):
        planning = PlanningConfigurations(profiles[0].configuration_hashes, error)
        subject = CanonicalSnapshotCoordinator(
            drm_tree=RootedSysfsReader(root),
            command_runner=_FixtureRunner(commands),
            profiles=StaticSavedProfiles(profiles),
            boot_id_source=_FakeBoot(),
            clock=_FakeClock(),
            event_generation_source=_FakeGeneration(),
            planning_configuration_source=planning,
        )

        broken = subject.observe()
        planning.error = None
        fixed = subject.observe()

        assert broken.valid
        assert broken.exact_profile == "celtic+AOC-U28G2G6B"
        assert broken.eligible_profiles[0].planning_configuration_hashes == ()
        assert fixed.eligible_profiles[0].planning_configuration_hashes == (
            ConfigurationContentHash(".fluxbox/keys.erb", "fixed"),
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
        ("xset", "q"),
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
    # Base identity now grants eligibility directly (dc-nd0), so the
    # explicit apply path supersedes the activation probe here — matching
    # what monitor-watcher-ng always did for this monitor. The probe path
    # remains for ambiguous or partially covered topologies.
    assert [match.profile for match in observation.eligible_profiles] == [
        "celtic+Samsung-Odyssey-G75F"
    ]
    assert observation.probe_candidate is None
    assert observation.current_profiles == ()


def test_plug_transition_with_stale_current_profile_is_valid(
    tmp_path: Path,
) -> None:
    """A current-but-no-longer-detected profile is a transition, not corruption.

    On every plug the old profile stays current until the new one is applied,
    and under active authority the applier is this controller. Treating that
    state as inconsistent evidence deadlocked the first live replug: every
    observation was invalid, so the reducer could never dispatch the apply
    that would have made observations valid again (dc-733).
    """
    root, commands, profiles = load_manifest(tmp_path, "plug-transition")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert observation.current_profiles == ("celtic",)
    assert observation.exact_profile is None
    # Eligibility now resolves from base identity (dc-nd0): the transition
    # proceeds by explicit application rather than an activation probe.
    assert [match.profile for match in observation.eligible_profiles] == [
        "celtic+Samsung-Odyssey-G75F"
    ]
    assert observation.probe_candidate is None


def test_base_identity_grants_eligibility_and_geometry_proves_current(
    tmp_path: Path,
) -> None:
    """A garbage X EDID cache must not strand a base-identified monitor.

    The Samsung G75F leaves X's EDID property as truncated garbage after
    roughly every link train, so autorandr --match-edid finds nothing: no
    eligibility, no detected, no current — and with the output already
    active no probe is legal either, which stranded a live replug in
    UNSUPPORTED at probe mode (dc-nd0). The sysfs base identity resolves
    the profile every time, and the live geometry equals the saved config,
    so eligibility and currentness must both be derivable without
    autorandr's matching.
    """
    root, commands, profiles = load_manifest(tmp_path, "base-identity-active")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert [match.profile for match in observation.eligible_profiles] == [
        "celtic+Samsung-Odyssey-G75F"
    ]
    assert observation.current_profiles == ("celtic+Samsung-Odyssey-G75F",)
    assert observation.exact_profile == "celtic+Samsung-Odyssey-G75F"


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


def test_cross_source_connector_contradiction_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot_module, "_last_inconsistency", [()])
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    (root / "card0-DP-3" / "connector_id").write_text("999\n", encoding="ascii")
    source = coordinator(RootedSysfsReader(root), _FixtureRunner(commands), profiles)

    observation = source.observe()
    source.observe()

    assert not observation.valid
    assert (
        observation.invalidity_reason
        is ObservationInvalidityReason.INCONSISTENT_EVIDENCE
    )
    assert observation.exact_profile is None
    # The specific cause must reach the journal, not just the reason enum,
    # but only once while it persists.  The journal logger writes straight to
    # stderr and does not propagate.
    err = capsys.readouterr().err
    assert err.count("OBSERVATION_INCONSISTENT causes=drm_topology") == 1


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
    autorandr_requests = runner.requests[2:5]
    assert all(request.environment is None for request in runner.requests[5:])
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


@pytest.mark.parametrize(
    ("edp_flag", "aoc_flag"),
    [("", ""), (" primary", "")],
    ids=["no-primary", "primary-on-wrong-output"],
)
def test_exact_profile_requires_the_saved_primary(
    tmp_path: Path, edp_flag: str, aoc_flag: str
) -> None:
    """A stale primary must make the controller re-apply, not plan (dc-5gfd).

    autorandr --current ignores the primary, so without this a layout whose
    primary was left on an unplugged output counted as already applied and
    desktop planning failed forever for want of a primary.
    """
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    for name in ("xrandr --query", "xrandr --props"):
        text = commands[name].read_text(encoding="utf-8")
        text = text.replace("eDP connected", f"eDP connected{edp_flag}")
        text = text.replace(
            "DisplayPort-7 connected primary", f"DisplayPort-7 connected{aoc_flag}"
        )
        commands[name] = tmp_path / name.replace(" ", "_")
        commands[name].write_text(text, encoding="utf-8")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert observation.exact_profile is None
    assert [item.profile for item in observation.eligible_profiles] == [
        "celtic+AOC-U28G2G6B"
    ]


def test_retained_crtc_after_unplug_is_reapplied_not_frozen(tmp_path: Path) -> None:
    """An unplugged monitor's retained CRTC must lead to an apply (dc-5seo).

    Rejecting it as torn evidence left the controller waiting for xfsettingsd
    to release the CRTC.  autorandr still counts the CRTC as configured, so
    the laptop profile is detected but not current, which makes the
    controller apply it and release the CRTC itself.
    """
    root, commands, profiles = load_manifest(tmp_path, "stale-crtc")

    observation = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert observation.valid
    assert [item.profile for item in observation.eligible_profiles] == ["celtic"]
    assert observation.current_profiles == ()
    assert observation.exact_profile is None


def test_observation_records_blanked_screens_without_changing_its_key(
    tmp_path: Path,
) -> None:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    awake = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()
    commands["xset q"] = FIXTURES / "xset" / "standby.txt"
    asleep = coordinator(
        RootedSysfsReader(root), _FixtureRunner(commands), profiles
    ).observe()

    assert awake.display_power is DisplayPower.ON
    assert asleep.display_power is DisplayPower.ASLEEP
    # Blanking is not a topology change: keying on it would re-verify and
    # re-finalize the desktop every time the screens sleep or wake.
    assert asleep.observation_key == awake.observation_key


@dataclass
class _SuppressionWorld:
    """Fixture sysfs and commands for the AOC being switched off by hand."""

    root: Path
    runner: _FixtureRunner
    profiles: tuple[SavedAutorandrProfile, ...]
    now: float = 1_000.0
    suspends: int | None = 7
    adopt: bool = True
    notes: list[str] = field(default_factory=list[str])

    fallbacks: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def suppression(self, path: Path) -> OutputSuppression:
        return OutputSuppression(
            path,
            may_adopt=lambda: self.adopt,
            realtime=lambda: self.now,
            suspend_count=lambda: self.suspends,
            notify=lambda summary, _body: self.notes.append(summary),
            fallback=lambda fingerprints, _x, _profiles: self.fallbacks.append(
                tuple(item.output for item in fingerprints)
            ),
        )

    def lit(self) -> None:
        self.runner.commands["xrandr --query"] = FIXTURES / "xrandr/aoc-rename.query"
        self.runner.commands["xrandr --props"] = FIXTURES / "xrandr/aoc-rename.props"
        self.runner.commands["autorandr --match-edid --current"] = (
            SNAPSHOTS / "exact-aoc.current"
        )

    def switched_off(self) -> None:
        self.runner.commands["xrandr --query"] = (
            FIXTURES / "xrandr/aoc-switched-off.query"
        )
        self.runner.commands["xrandr --props"] = (
            FIXTURES / "xrandr/aoc-switched-off.props"
        )
        # autorandr still detects the profile with the AOC, and none is current.
        self.runner.commands["autorandr --match-edid --current"] = (
            SNAPSHOTS / "aoc-switched-off.current"
        )

    def unplugged(self) -> None:
        connector = self.root / "card0-DP-3"
        self._saved_edid = connector.joinpath("edid").read_bytes()
        connector.joinpath("status").write_text("disconnected\n", encoding="ascii")
        connector.joinpath("edid").write_bytes(b"")
        self.runner.commands["xrandr --query"] = FIXTURES / "xrandr/aoc-unplugged.query"
        self.runner.commands["xrandr --props"] = FIXTURES / "xrandr/aoc-unplugged.props"
        self.runner.commands["autorandr --match-edid --fingerprint"] = (
            FIXTURES / "autorandr/commands/laptop.fingerprint"
        )
        self.runner.commands["autorandr --match-edid --detected"] = (
            SNAPSHOTS / "laptop.detected"
        )

    def replugged_off(self) -> None:
        connector = self.root / "card0-DP-3"
        connector.joinpath("status").write_text("connected\n", encoding="ascii")
        connector.joinpath("edid").write_bytes(self._saved_edid)
        self.runner.commands["autorandr --match-edid --fingerprint"] = (
            FIXTURES / "autorandr/commands/aoc-renamed.fingerprint"
        )
        self.runner.commands["autorandr --match-edid --detected"] = (
            SNAPSHOTS / "exact-aoc.detected"
        )
        self.switched_off()


def _suppression_world(tmp_path: Path) -> _SuppressionWorld:
    root, commands, profiles = load_manifest(tmp_path, "exact-aoc")
    return _SuppressionWorld(
        root, _FixtureRunner(commands), (*profiles, load_profile("celtic"))
    )


def _observe(
    world: _SuppressionWorld, suppression: OutputSuppression
) -> CanonicalObservation:
    return CanonicalSnapshotCoordinator(
        drm_tree=RootedSysfsReader(world.root),
        command_runner=world.runner,
        profiles=StaticSavedProfiles(world.profiles),
        boot_id_source=_FakeBoot(),
        clock=_FakeClock(),
        event_generation_source=_FakeGeneration(),
        suppression=suppression,
    ).observe()


def test_display_switched_off_by_hand_is_planned_as_unplugged(
    tmp_path: Path,
) -> None:
    """The dark AOC must not keep the laptop profile ineligible (dc-8xns).

    Unmasked, the reducer refuses an internal-only profile while external
    hardware is connected, so the desktop kept the two-screen layout.
    """
    world = _suppression_world(tmp_path)
    state = tmp_path / "suppressed.json"
    suppression = world.suppression(state)
    _observe(world, suppression)
    world.switched_off()

    observation = _observe(world, suppression)

    assert observation.valid
    assert observation.kernel_connected_outputs == ("eDP",)
    assert observation.x_connected_outputs == ("eDP",)
    assert not observation.has_external_hardware
    assert [item.profile for item in observation.eligible_profiles] == ["celtic"]
    assert observation.eligible_profiles[0].scope is ProfileScope.INTERNAL_ONLY
    # eDP lost primary with the AOC, so the controller must apply the profile.
    assert observation.exact_profile is None
    assert world.notes == ["DisplayPort-7 stays off"]
    assert json.loads(state.read_text(encoding="utf-8"))["outputs"]

    # A later controller instance keeps the switch-off without seeing it happen.
    observation = _observe(world, world.suppression(state))
    assert observation.kernel_connected_outputs == ("eDP",)


def test_switch_off_during_controller_action_is_not_adopted(tmp_path: Path) -> None:
    world = _suppression_world(tmp_path)
    world.adopt = False
    suppression = world.suppression(tmp_path / "suppressed.json")
    _observe(world, suppression)
    world.switched_off()

    observation = _observe(world, suppression)

    assert observation.has_external_hardware
    assert not suppression.suppressed


def test_switch_off_seen_by_the_randr_watcher_is_adopted_after_its_wake(
    tmp_path: Path,
) -> None:
    """The watcher's own hint leaves quiescent before the sample is taken.

    Live on 2026-11-20 the Samsung was relit because the sample landed in
    discover_fast, where *may_adopt* refuses; the watcher's latch must win.
    """
    world = _suppression_world(tmp_path)
    suppression = world.suppression(tmp_path / "suppressed.json")
    _observe(world, suppression)
    world.switched_off()
    suppression.expect_manual_change()
    world.adopt = False

    observation = _observe(world, suppression)

    assert not observation.has_external_hardware
    assert world.notes == ["DisplayPort-7 stays off"]
    # The latch covers one sample only; the controller's own changes are not.
    world.lit()
    _observe(world, suppression)
    world.switched_off()
    assert _observe(world, suppression).has_external_hardware


def test_switching_the_display_back_on_ends_suppression(tmp_path: Path) -> None:
    world = _suppression_world(tmp_path)
    suppression = world.suppression(tmp_path / "suppressed.json")
    _observe(world, suppression)
    world.switched_off()
    _observe(world, suppression)
    world.lit()

    observation = _observe(world, suppression)

    assert observation.exact_profile == "celtic+AOC-U28G2G6B"
    assert not suppression.suppressed


@pytest.mark.parametrize(
    ("suspended", "away_seconds", "released"),
    [
        (False, 5.0, True),  # a quick replug while awake
        (True, 5.0, False),  # a resume briefly dropping the link
        (True, REPLUG_GRACE_SECONDS, True),  # undocked across a suspend
    ],
)
def test_replug_ends_suppression_but_resume_does_not(
    tmp_path: Path, *, suspended: bool, away_seconds: float, released: bool
) -> None:
    world = _suppression_world(tmp_path)
    suppression = world.suppression(tmp_path / "suppressed.json")
    _observe(world, suppression)
    world.switched_off()
    _observe(world, suppression)
    world.unplugged()
    _observe(world, suppression)
    if suspended:
        world.suspends = 8
    world.now += away_seconds
    world.replugged_off()

    observation = _observe(world, suppression)

    assert bool(suppression.suppressed) is not released
    assert observation.has_external_hardware is released


@pytest.mark.parametrize("has_laptop_profile", [True, False])
def test_fallback_profile_is_requested_only_when_nothing_fits(
    tmp_path: Path, *, has_laptop_profile: bool
) -> None:
    world = _suppression_world(tmp_path)
    if not has_laptop_profile:
        world.profiles = tuple(item for item in world.profiles if item.name != "celtic")
    suppression = world.suppression(tmp_path / "suppressed.json")
    _observe(world, suppression)
    world.switched_off()

    _observe(world, suppression)

    assert world.fallbacks == ([] if has_laptop_profile else [("eDP",)])
