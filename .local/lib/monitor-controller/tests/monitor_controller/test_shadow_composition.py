"""Structural safety, namespace, and restart tests for deployed shadow mode."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

import monitor_controller.shadow as shadow_module

if TYPE_CHECKING:
    from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActivateProbe,
    ApplicationAttemptKey,
    ApplyProfile,
    BaseIdentityMatch,
    BootId,
    CanonicalObservation,
    ConfigurationContentHash,
    ConnectorIdentityEvidence,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EdidEvidence,
    EdidIntegrity,
    EventGeneration,
    EventMetadata,
    FinalizeDesktop,
    Fingerprint,
    MappingProof,
    ObservationCompleted,
    ObservationGeneration,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    PrepareDesktop,
    ProbeAttemptKey,
    ProfileMatch,
    ProfileScope,
    RawEvidenceReference,
    RawEvidenceSource,
    State,
    TransitionId,
    TransitionKey,
)
from monitor_controller.observer.autorandr import (
    parse_autorandr_fingerprint,
    parse_current_profiles,
    parse_detected_profiles,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.runtime.commands import BoundedCommandRunner, CommandRequest
from monitor_controller.runtime.dispatcher import NullDispatcher, WouldDispatchKind
from monitor_controller.runtime.persistence import AtomicStateStore, StateNamespace
from monitor_controller.shadow import (
    SHADOW_OBSERVATION_TIMEOUT_SECONDS,
    AsyncSnapshotObserver,
    ShadowAuthorityLock,
    ShadowControllerAdapters,
    ShadowPaths,
    ShadowStartupError,
    compose_shadow_controller,
    isolated_autorandr_environment,
    load_saved_profiles,
    load_shadow_state,
    prepare_isolated_autorandr_namespace,
    run_shadow,
)

_BOOT = BootId(UUID(int=801))
_INSTANCE = ControllerInstanceId(UUID(int=802))
_CONFIG = (ConfigurationContentHash("layouts/dock.yaml", "sha256:dock"),)


class _Clock:
    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms

    def monotonic_ms(self) -> int:
        return self.now_ms

    async def sleep_until(self, deadline_ms: int) -> None:
        del deadline_ms
        await asyncio.Event().wait()


class _UnusedObserver:
    async def observe(self) -> CanonicalObservation:
        message = "observer should not be called in this test"
        raise AssertionError(message)


def _paths(tmp_path: Path) -> ShadowPaths:
    return ShadowPaths(
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        config_home=tmp_path / "config",
    )


def _state(*, instance: ControllerInstanceId = _INSTANCE) -> State:
    return State(
        boot_id=_BOOT,
        controller_instance=instance,
        display_identity=DisplayIdentity(":shadow-test"),
    )


def _composition(
    tmp_path: Path,
    *,
    initial_state: State | None = None,
    clock: _Clock | None = None,
) -> shadow_module.ShadowComposition:
    paths = _paths(tmp_path)
    state = _state() if initial_state is None else initial_state
    store = AtomicStateStore(paths.state_home, StateNamespace.SHADOW)
    audit = RotatingAuditLog(paths.audit_log, state)
    return compose_shadow_controller(
        paths=paths,
        initial_state=state,
        adapters=ShadowControllerAdapters(
            store=store,
            observer=_UnusedObserver(),
            audit=audit,
            clock=_Clock() if clock is None else clock,
        ),
    )


def _observation() -> CanonicalObservation:
    outputs = ("DP-1", "eDP-1")
    match = ProfileMatch(
        "dock",
        ProfileScope.MIXED,
        "layouts/dock.yaml",
        (OutputMapping("DP-SAVED", "DP-1"), OutputMapping("eDP-1", "eDP-1")),
        outputs,
        _CONFIG,
    )
    return CanonicalObservation(
        observed_at_ms=0,
        observation_generation=ObservationGeneration(1),
        boot_id=_BOOT,
        physical_token=PhysicalToken("physical-dock"),
        begin_event_generation=EventGeneration(0),
        end_event_generation=EventGeneration(0),
        kernel_connected_outputs=outputs,
        kernel_external_outputs=("DP-1",),
        x_connected_outputs=outputs,
        x_active_outputs=("eDP-1",),
        x_external_outputs=("DP-1",),
        connector_identities=(ConnectorIdentityEvidence("DP-1", "card0-DP-1", 1, 1),),
        live_fingerprints=(
            Fingerprint("DP-1", "external"),
            Fingerprint("eDP-1", "internal"),
        ),
        base_identity_profiles=(BaseIdentityMatch("dock", "DP-1"),),
        edid_integrity=(EdidEvidence("DP-1", EdidIntegrity.COMPLETE, "base"),),
        probe_candidate=None,
        eligible_profiles=(match,),
        current_profiles=(),
        exact_profile=None,
        observation_key=ObservationKey("dock-key"),
        validity=ObservationValidity.VALID,
        invalidity_reason=None,
        raw_evidence=(
            RawEvidenceReference(
                RawEvidenceSource.DRM_CONNECTORS,
                "test:shadow",
                "sha256:shadow",
            ),
        ),
    )


def _dispatch_effects() -> tuple[
    ActivateProbe,
    ApplyProfile,
    PrepareDesktop,
    FinalizeDesktop,
]:
    observation_key = ObservationKey("shadow-effects")
    probe_id = ActionId(_INSTANCE, ActionKind.PROBE, 1)
    apply_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 2)
    prepare_id = ActionId(_INSTANCE, ActionKind.PREPARATION, 3)
    finalize_id = ActionId(_INSTANCE, ActionKind.FINALIZATION, 4)
    transition_id = TransitionId(_INSTANCE, 1)
    transition_key = TransitionKey("shadow-transition")
    mapping = MappingProof(
        "dock",
        1,
        observation_key,
        (OutputMapping("DP-SAVED", "DP-1"),),
    )
    return (
        ActivateProbe(
            probe_id,
            ProbeAttemptKey(1, "dock", observation_key),
            "DP-1",
            "eDP-1",
            "3840x2160",
            EventGeneration(0),
            observation_key,
        ),
        ApplyProfile(
            apply_id,
            ApplicationAttemptKey(1, "dock", observation_key),
            "dock",
            mapping,
            EventGeneration(0),
            observation_key,
        ),
        PrepareDesktop(
            prepare_id,
            transition_id,
            transition_key,
            "dock",
            PlanHash("shadow-plan"),
            EventGeneration(0),
            observation_key,
        ),
        FinalizeDesktop(
            finalize_id,
            transition_id,
            transition_key,
            "dock",
            PlanHash("shadow-plan"),
            EventGeneration(0),
            observation_key,
        ),
    )


def test_all_deployed_paths_are_fixed_to_shadow_namespaces(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert paths.state_file == (
        tmp_path / "state" / "monitor-controller" / "shadow" / "state.json"
    )
    assert paths.audit_log == (
        tmp_path / "state" / "monitor-controller" / "shadow" / "audit.jsonl"
    )
    assert paths.authority_lock == (
        tmp_path / "runtime" / "monitor-controller" / "shadow" / "authority.lock"
    )
    assert paths.transaction_namespace == (
        tmp_path / "runtime" / "monitor-controller" / "shadow" / "transactions"
    )
    assert all(
        "active" not in path.parts
        for path in (
            paths.state_file,
            paths.audit_log,
            paths.authority_lock,
            paths.transaction_namespace,
        )
    )


def test_composition_api_cannot_accept_dispatch_or_transaction_adapters() -> None:
    parameters = inspect.signature(compose_shadow_controller).parameters
    adapter_fields = {field.name for field in fields(ShadowControllerAdapters)}

    assert set(parameters) == {"paths", "initial_state", "adapters"}
    assert not adapter_fields & {
        "dispatcher",
        "supervisor",
        "systemd",
        "transaction",
        "transaction_root",
    }
    source = inspect.getsource(shadow_module)
    assert "systemctl" not in source
    assert "request.json" not in source


def test_composition_hard_wires_null_dispatch_and_creates_no_transactions(
    tmp_path: Path,
) -> None:
    composition = _composition(tmp_path)

    assert type(composition.dispatcher) is NullDispatcher
    assert composition.paths.transaction_namespace.parent == (
        composition.paths.runtime_dir / "monitor-controller" / "shadow"
    )
    assert not composition.paths.transaction_namespace.exists()
    assert not hasattr(composition.dispatcher, "write_request")
    assert not hasattr(composition.dispatcher, "start")
    assert not hasattr(composition.dispatcher, "stop")


def test_all_shadow_worker_intents_are_only_would_audit_outcomes(
    tmp_path: Path,
) -> None:
    composition = _composition(tmp_path)
    for index, effect in enumerate(_dispatch_effects()):
        record = composition.dispatcher.record(effect, index)
        composition.audit.append_would_dispatch(record)

    records = [
        json.loads(line)
        for line in composition.audit.path.read_text(encoding="utf-8").splitlines()
    ]
    outcomes = [record for record in records if record["record"] != "header"]

    assert [record["record"] for record in outcomes] == ["would_dispatch"] * 4
    assert [record["kind"] for record in outcomes] == [
        item.value for item in WouldDispatchKind
    ]
    assert not composition.paths.transaction_namespace.exists()


def test_controller_admission_records_would_apply_without_request_or_unit(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        composition = _composition(tmp_path)
        observation = _observation()

        await composition.controller.consume(
            ObservationCompleted(EventMetadata(0, _BOOT), observation)
        )

        assert tuple(record.kind for record in composition.dispatcher.records) == (
            WouldDispatchKind.WOULD_APPLY,
        )
        assert not composition.paths.transaction_namespace.exists()
        assert '"kind":"WOULD_APPLY"' in composition.audit.path.read_text(
            encoding="utf-8"
        )
        await composition.controller.close()

    asyncio.run(exercise())


def test_shadow_authority_lock_rejects_a_concurrent_holder(tmp_path: Path) -> None:
    path = _paths(tmp_path).authority_lock

    with (
        ShadowAuthorityLock(path),
        pytest.raises(ShadowStartupError, match="already held"),
        ShadowAuthorityLock(path),
    ):
        pass

    with ShadowAuthorityLock(path):
        assert path.stat().st_mode & 0o777 == 0o600


def test_restart_loads_persisted_shadow_timer_and_rotates_audit(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        paths = _paths(tmp_path)
        paths.state_home.mkdir(parents=True)
        store = AtomicStateStore(paths.state_home, StateNamespace.SHADOW)
        persisted = replace(_state(), next_timer_ms=500)
        store.save(persisted)
        RotatingAuditLog(paths.audit_log, persisted)
        restarted_instance = ControllerInstanceId(UUID(int=803))

        loaded = load_shadow_state(
            store,
            boot_id=_BOOT,
            controller_instance=restarted_instance,
            display_identity=persisted.display_identity,
        )
        assert loaded.next_timer_ms == 500
        assert loaded.controller_instance == restarted_instance
        audit = RotatingAuditLog(paths.audit_log, loaded)
        controller = compose_shadow_controller(
            paths=paths,
            initial_state=loaded,
            adapters=ShadowControllerAdapters(
                store=store,
                observer=_UnusedObserver(),
                audit=audit,
                clock=_Clock(),
            ),
        )

        await controller.controller.start()

        assert controller.controller.scheduled_deadline_ms == 500
        assert controller.controller.state.next_timer_ms == 500
        assert paths.audit_log.with_name("audit.jsonl.1").is_file()
        assert not paths.transaction_namespace.exists()
        await controller.controller.close()

    asyncio.run(exercise())


def test_corrupt_shadow_state_is_not_discarded_on_restart(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = AtomicStateStore(paths.state_home, StateNamespace.SHADOW)
    store.path.parent.mkdir(parents=True)
    corrupt = b'{"schema_version":'
    store.path.write_bytes(corrupt)

    with pytest.raises(ShadowStartupError, match="will not be discarded"):
        load_shadow_state(
            store,
            boot_id=_BOOT,
            controller_instance=_INSTANCE,
            display_identity=_state().display_identity,
        )

    assert store.path.read_bytes() == corrupt


def test_saved_profile_loader_uses_only_explicit_profile_root(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "autorandr" / "profiles" / "celtic"
    root = tmp_path / "autorandr"
    shutil.copytree(fixture, root / "celtic")

    profiles = load_saved_profiles(root)

    assert tuple(profile.name for profile in profiles) == ("celtic",)
    assert profiles[0].configuration_hashes


def test_isolated_autorandr_preserves_inherited_xauthority_exactly(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "session" / "authority"
    authority.parent.mkdir()
    authority.write_bytes(b"cookie")
    inherited = str(authority.parent / ".." / "session" / "authority")

    environment = isolated_autorandr_environment(
        tmp_path / "isolated",
        {
            "DISPLAY": ":test",
            "HOME": str(tmp_path / "original-home"),
            "XAUTHORITY": inherited,
        },
    )

    assert environment["XAUTHORITY"] == inherited
    assert environment["HOME"] == str(tmp_path / "isolated" / "home")


def test_isolated_autorandr_resolves_original_home_xauthority_before_replacement(
    tmp_path: Path,
) -> None:
    home = tmp_path / "original-home"
    home.mkdir()
    authority = home / ".Xauthority"
    authority.write_bytes(b"cookie")

    environment = isolated_autorandr_environment(
        tmp_path / "isolated",
        {"DISPLAY": ":test", "HOME": str(home)},
    )

    assert environment["XAUTHORITY"] == str(authority.resolve())
    assert Path(environment["XAUTHORITY"]).is_absolute()
    assert environment["HOME"] == str(tmp_path / "isolated" / "home")


@pytest.mark.parametrize("authority_state", ["absent", "unreadable"])
def test_isolated_autorandr_rejects_unavailable_home_xauthority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_state: str,
) -> None:
    home = tmp_path / "original-home"
    home.mkdir()
    authority = home / ".Xauthority"
    if authority_state == "unreadable":
        authority.write_bytes(b"cookie")

        def deny_open(*_args: object, **_kwargs: object) -> None:
            raise PermissionError

        monkeypatch.setattr(Path, "open", deny_open)

    with pytest.raises(
        ShadowStartupError,
        match=r"requires a readable X11 authority file.*set XAUTHORITY",
    ):
        isolated_autorandr_environment(
            tmp_path / "isolated",
            {"DISPLAY": ":test", "HOME": str(home)},
        )


def test_autorandr_observation_uses_only_hook_free_isolated_data(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    autorandr = shutil.which("autorandr")
    if autorandr is None:
        pytest.skip("installed autorandr is required for source-behavior contract")
    source = tmp_path / "source" / "autorandr"
    profile = source / "safe"
    profile.mkdir(parents=True)
    fingerprint = "00" * 128
    (profile / "config").write_text(
        "output eDP\nmode 1920x1080\npos 0x0\nprimary\nrate 60.00\n",
        encoding="utf-8",
    )
    (profile / "setup").write_text(f"eDP {fingerprint}\n", encoding="utf-8")
    (profile / "layout").write_text("safe\n", encoding="utf-8")
    (source / "settings.ini").write_text(
        "[config]\nskip-options=gamma\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "hook-ran"
    hook = "#!/bin/sh\nprintf '%s\\n' hook >> \"$SENTINEL\"\n"
    for path in (source / "predetect", profile / "block"):
        path.write_text(hook, encoding="utf-8")
        path.chmod(0o700)
    host_home = tmp_path / "host-home"
    legacy = host_home / ".autorandr"
    legacy.mkdir(parents=True)
    (legacy / "predetect").write_text(hook, encoding="utf-8")
    (legacy / "predetect").chmod(0o700)
    host_config_dirs = tmp_path / "host-config-dirs" / "autorandr"
    host_config_dirs.mkdir(parents=True)
    (host_config_dirs / "predetect").write_text(hook, encoding="utf-8")
    (host_config_dirs / "predetect").chmod(0o700)

    isolation = tmp_path / "shadow-state" / "autorandr-observer"
    profiles = prepare_isolated_autorandr_namespace(source, isolation)

    assert tuple(item.name for item in profiles) == ("safe",)
    isolated_files = {
        path.relative_to(isolation).as_posix()
        for path in isolation.rglob("*")
        if path.is_file()
    }
    assert isolated_files == {
        "config/autorandr/safe/config",
        "config/autorandr/safe/layout",
        "config/autorandr/safe/setup",
        "config/autorandr/settings.ini",
    }
    assert all(
        not path.stat().st_mode & 0o111
        for path in isolation.rglob("*")
        if path.is_file()
    )

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
    xrandr_authorities = tmp_path / "xrandr-authorities"
    fake_xrandr = fake_bin / "xrandr"
    fake_xrandr.write_text(
        """#!/bin/sh
printf '%s\\n' "$XAUTHORITY" >> "$XRANDR_AUTHORITIES"
if [ "${1:-}" = -v ]; then
    printf '%s\\n' 'xrandr program version 1.5.2'
else
    /bin/cat "$FAKE_XRANDR_OUTPUT"
fi
""",
        encoding="utf-8",
    )
    fake_xrandr.chmod(0o700)
    autorandr_authorities = tmp_path / "autorandr-authorities"
    fake_autorandr = fake_bin / "autorandr"
    fake_autorandr.write_text(
        """#!/bin/sh
printf '%s\\n' "$XAUTHORITY" >> "$AUTORANDR_AUTHORITIES"
exec "$REAL_AUTORANDR" "$@"
""",
        encoding="utf-8",
    )
    fake_autorandr.chmod(0o700)
    authority = tmp_path / "session" / "Xauthority"
    authority.parent.mkdir()
    authority.write_bytes(b"cookie")
    base_environment = os.environ.copy()
    base_environment.update(
        {
            "AUTORANDR_AUTHORITIES": str(autorandr_authorities),
            "DISPLAY": ":isolated-test",
            "FAKE_XRANDR_OUTPUT": str(verbose),
            "HOME": str(host_home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PYTHONPATH": str(tmp_path / "host-python"),
            "REAL_AUTORANDR": autorandr,
            "SENTINEL": str(sentinel),
            "XAUTHORITY": str(authority),
            "XDG_CONFIG_DIRS": str(host_config_dirs.parent),
            "XDG_CONFIG_HOME": str(source.parent),
            "XRANDR_AUTHORITIES": str(xrandr_authorities),
        }
    )
    base_environment.pop("WAYLAND_DISPLAY", None)
    environment = isolated_autorandr_environment(isolation, base_environment)
    assert environment["HOME"] == str(isolation / "home")
    assert environment["XDG_CONFIG_HOME"] == str(isolation / "config")
    assert environment["XDG_CONFIG_DIRS"] == str(isolation / "config-dirs")
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in environment

    runner = BoundedCommandRunner()
    evidence: dict[str, TextCommandEvidence] = {}
    for option, evidence_source in (
        ("--fingerprint", RawEvidenceSource.AUTORANDR_FINGERPRINT),
        ("--detected", RawEvidenceSource.AUTORANDR_PROFILES),
        ("--current", RawEvidenceSource.AUTORANDR_PROFILES),
    ):
        evidence[option] = runner.run(
            CommandRequest(
                (str(fake_autorandr), option),
                evidence_source,
                f"test:autorandr {option}",
                2,
                tuple(sorted(environment.items())),
            )
        )

    assert parse_autorandr_fingerprint(evidence["--fingerprint"]).valid
    detected = parse_detected_profiles(evidence["--detected"])
    current = parse_current_profiles(evidence["--current"])
    assert detected.valid
    assert detected.profiles == ("safe",)
    assert current.valid
    assert (
        autorandr_authorities.read_text(encoding="utf-8").splitlines()
        == [
            str(authority),
        ]
        * 3
    )
    assert set(xrandr_authorities.read_text(encoding="utf-8").splitlines()) == {
        str(authority)
    }
    assert not sentinel.exists()


def test_autorandr_isolation_rejects_settings_that_could_trigger_change(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "autorandr" / "profiles" / "celtic"
    source = tmp_path / "autorandr"
    shutil.copytree(fixture, source / "celtic")
    (source / "settings.ini").write_text("[config]\nchange=true\n", encoding="utf-8")

    with pytest.raises(ShadowStartupError, match="observer-unsafe"):
        prepare_isolated_autorandr_namespace(source, tmp_path / "isolated")


def test_async_snapshot_observer_never_overlaps_after_caller_timeout() -> None:
    class SlowCoordinator:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.guard = threading.Lock()

        def observe(self) -> CanonicalObservation:
            with self.guard:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.started.set()
            try:
                assert self.release.wait(timeout=2)
                return _observation()
            finally:
                with self.guard:
                    self.active -= 1

    async def exercise() -> None:
        coordinator = SlowCoordinator()
        observer = AsyncSnapshotObserver(coordinator)  # type: ignore[arg-type]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(observer.observe(), timeout=0.02)
        assert coordinator.started.is_set()

        second = asyncio.create_task(observer.observe())
        await asyncio.sleep(0.02)
        assert coordinator.calls == 1
        assert coordinator.max_active == 1
        coordinator.release.set()
        assert await second == _observation()
        assert await observer.observe() == _observation()
        assert coordinator.calls == 2
        assert coordinator.max_active == 1

    asyncio.run(exercise())


def test_shadow_timeout_covers_the_complete_bounded_command_sequence() -> None:
    timeout_field = next(
        field
        for field in fields(ShadowControllerAdapters)
        if field.name == "adapter_timeout_seconds"
    )

    assert SHADOW_OBSERVATION_TIMEOUT_SECONDS == 30
    assert timeout_field.default == SHADOW_OBSERVATION_TIMEOUT_SECONDS


def test_cross_boot_state_resets_monotonic_deadlines_before_startup(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        paths = _paths(tmp_path)
        paths.state_home.mkdir(parents=True)
        store = AtomicStateStore(paths.state_home, StateNamespace.SHADOW)
        old_boot = BootId(UUID(int=800))
        persisted = replace(_state(), boot_id=old_boot, next_timer_ms=987_654)
        store.save(persisted)
        instance = ControllerInstanceId(UUID(int=804))
        loaded = load_shadow_state(
            store,
            boot_id=_BOOT,
            controller_instance=instance,
            display_identity=persisted.display_identity,
        )

        assert loaded.boot_id == _BOOT
        assert loaded.phase is ControllerPhase.RECOVERING
        assert loaded.next_timer_ms is None
        assert loaded.aggressive_deadline_ms is None
        assert loaded.latest_observation is None

        class StartupObserver:
            calls = 0

            async def observe(self) -> CanonicalObservation:
                self.calls += 1
                message = "fresh startup observation attempted"
                raise OSError(message)

        observer = StartupObserver()
        composition = compose_shadow_controller(
            paths=paths,
            initial_state=loaded,
            adapters=ShadowControllerAdapters(
                store=store,
                observer=observer,
                audit=RotatingAuditLog(paths.audit_log, loaded),
                clock=_Clock(),
                adapter_timeout_seconds=0.05,
            ),
        )

        await composition.controller.start()

        assert composition.controller.scheduled_deadline_ms is None
        assert composition.controller.pending_event_count == 1
        await composition.controller.process_next()
        assert observer.calls == 1
        assert composition.controller.state.next_timer_ms == 1_000
        await composition.controller.close()

    asyncio.run(exercise())


def test_run_shadow_forwards_uevent_notification_and_cleans_up_tasks() -> None:
    class Controller:
        def __init__(self) -> None:
            self.notifications = 0
            self.cancelled = False
            self.closed = False

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        def notify_drm_hint(self) -> None:
            self.notifications += 1

        async def close(self) -> None:
            self.closed = True

    class Monitor:
        def __init__(self) -> None:
            self.shutdown = False

        async def run(self, notify: object) -> None:
            try:
                assert callable(notify)
                notify()
                msg = "injected monitor failure"
                raise RuntimeError(msg)
            finally:
                self.shutdown = True

    async def exercise() -> None:
        controller = Controller()
        monitor = Monitor()
        composition = SimpleNamespace(controller=controller)

        with pytest.raises(RuntimeError, match="monitor failure"):
            await run_shadow(composition, monitor)  # type: ignore[arg-type]

        assert controller.notifications == 1
        assert controller.cancelled
        assert controller.closed
        assert monitor.shutdown

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"change@/devices/card0\0ACTION=change\0SUBSYSTEM=drm\0", True),
        (b"change@/devices/input0\0ACTION=change\0SUBSYSTEM=input\0", False),
        (b"change@/devices/card0\0ACTION=online\0SUBSYSTEM=drm\0", False),
    ],
)
def test_uevent_filter_accepts_only_relevant_drm_events(
    payload: bytes,
    expected: bool,
) -> None:
    assert shadow_module.is_drm_uevent(payload) is expected


def test_shadow_service_runs_fixed_venv_without_conflicting_with_watchers() -> None:
    repository = Path(__file__).parents[5]
    unit = repository / ".config/systemd/user/monitor-controller-shadow.service"
    directives = tuple(
        line.strip()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    exec_start = next(line for line in directives if line.startswith("ExecStart="))

    assert exec_start == (
        "ExecStart=%h/.local/share/monitor-controller/venv/bin/python "
        "-I -m monitor_controller.shadow"
    )
    assert "uv" not in exec_start
    assert "/bin/sh" not in exec_start
    assert "systemctl" not in exec_start
    assert not any(line.startswith("Conflicts=") for line in directives)
    assert "PartOf=fluxbox-session.target" in directives
    assert "After=fluxbox-session.target" in directives
    assert (
        "ExecCondition=/usr/bin/test -x "
        "%h/.local/share/monitor-controller/venv/bin/python"
    ) in directives
    assert "WantedBy=fluxbox-session.target" in directives
    assert "Environment=MONITOR_CONTROLLER_NAMESPACE=shadow" in directives
    assert "Environment=XDG_RUNTIME_DIR=%t" in directives
    unset = next(line for line in directives if line.startswith("UnsetEnvironment="))
    assert "LD_PRELOAD" in unset
    assert "PYTHONPATH" in unset
    assert "ProtectSystem=strict" in directives
    assert "ProtectHome=read-only" in directives
    assert "PrivateUsers=true" in directives
    assert not any(line.startswith("PrivateDevices=") for line in directives)
    assert not any(line.startswith("PrivateIPC=") for line in directives)
    assert "StateDirectory=monitor-controller/shadow" in directives
    assert "RuntimeDirectory=monitor-controller/shadow" in directives
    assert (
        "ReadWritePaths=%h/.local/state/monitor-controller/shadow "
        "%t/monitor-controller/shadow"
    ) in directives
    assert (
        "ReadOnlyPaths=/sys/class/drm /proc/sys/kernel/random/boot_id"
    ) in directives
    inaccessible = next(
        line for line in directives if line.startswith("InaccessiblePaths=")
    )
    assert "%h/.local/state/monitor-controller/active" in inaccessible
    assert "%t/monitor-controller/active" in inaccessible
    assert "%t/systemd/private" in inaccessible
    assert "%t/bus" in inaccessible
    assert "NoExecPaths=%h %t /tmp /var/tmp" in directives
    assert "ExecPaths=%h/.local/share/monitor-controller/venv" in directives
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in directives
    assert not any(line.startswith("IPAddressDeny=") for line in directives)
    assert "PrivateTmp=true" not in directives
    assert inaccessible == (
        "InaccessiblePaths=-%h/.local/state/monitor-controller/active "
        "-%t/monitor-controller/active -%t/systemd/private -%t/bus"
    )


def test_user_manager_launches_harmless_python_with_shadow_sandbox() -> None:
    """Exercise namespace setup, which static systemd verification cannot."""
    runner = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    python = Path("/usr/bin/python3")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runner is None or systemctl is None or not python.is_file():
        pytest.skip("systemd-run, systemctl, and fixed /usr/bin/python3 are required")
    if runtime is None:
        pytest.skip("XDG_RUNTIME_DIR is required for a user-manager launch")

    manager = subprocess.run(  # noqa: S603
        (systemctl, "--user", "show-environment"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if manager.returncode != 0:
        pytest.skip(f"systemd user manager is unavailable: {manager.stderr.strip()}")

    repository = Path(__file__).parents[5]
    unit = repository / ".config/systemd/user/monitor-controller-shadow.service"
    # Pull the static unit's actual compatible hardening values into a transient
    # launch.  State/RuntimeDirectory and ReadWritePaths are deliberately absent:
    # this contract process has no state and must not touch either display or the
    # deployed shadow namespace.  ExecPaths is only the exception that lets the
    # real venv run below NoExecPaths=%h; fixed /usr/bin/python3 needs no exception.
    property_names = {
        "AmbientCapabilities",
        "CapabilityBoundingSet",
        "InaccessiblePaths",
        "LockPersonality",
        "MemoryDenyWriteExecute",
        "NoExecPaths",
        "NoNewPrivileges",
        "PrivateUsers",
        "ProtectClock",
        "ProtectHome",
        "ProtectHostname",
        "ProtectKernelLogs",
        "ProtectKernelModules",
        "ProtectKernelTunables",
        "ProtectProc",
        "ProtectSystem",
        "ReadOnlyPaths",
        "RestrictAddressFamilies",
        "RestrictNamespaces",
        "RestrictRealtime",
        "RestrictSUIDSGID",
        "SystemCallArchitectures",
        "SystemCallErrorNumber",
        "SystemCallFilter",
    }
    properties: list[str] = []
    found: set[str] = set()
    for raw_line in unit.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        name, separator, _value = line.partition("=")
        if separator and name in property_names:
            found.add(name)
            properties.append(
                "--property="
                + line.replace("%h", str(Path.home())).replace("%t", runtime)
            )
    assert found == property_names

    marker = "monitor-controller-sandbox-contract-ok"
    command = (
        runner,
        "--user",
        "--wait",
        "--collect",
        "--pipe",
        f"--unit=monitor-controller-shadow-contract-{os.getpid()}",
        "--property=Type=exec",
        "--setenv=DISPLAY=",
        "--setenv=XAUTHORITY=",
        "--setenv=WAYLAND_DISPLAY=",
        *properties,
        str(python),
        "-I",
        "-c",
        (
            "import os; "
            "assert not os.environ.get('DISPLAY'); "
            f"print('{marker}', flush=True)"
        ),
    )
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, (
        "user-manager sandbox launch failed; this catches runtime setup errors "
        "such as status 226/NAMESPACE that systemd-analyze verify cannot detect\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert marker in completed.stdout


def test_systemd_analyze_directly_verifies_shadow_unit(tmp_path: Path) -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is unavailable")
    repository = Path(__file__).parents[5]
    source = repository / ".config/systemd/user/monitor-controller-shadow.service"
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    python = home / ".local/share/monitor-controller/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    runtime.mkdir()
    unit = tmp_path / "monitor-controller-shadow.service"
    unit.write_text(
        source.read_text(encoding="utf-8")
        .replace("%h", str(home))
        .replace("%t", str(runtime)),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["XDG_RUNTIME_DIR"] = str(runtime)

    completed = subprocess.run(  # noqa: S603
        (analyzer, "verify", "--user", str(unit)),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
