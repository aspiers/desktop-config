"""Exclusive-authority tests for the active composition root.

The invariant under test is that exactly one dispatch authority can exist at
a time. Everything here is about proving startup fails closed rather than
degrading, because two controllers dispatching to one display reproduces the
colliding-relayout failures this subsystem exists to eliminate.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import pytest

from monitor_controller import active
from monitor_controller.active import (
    CONFLICTING_UNITS,
    CUTOVER_AUTHORIZATION_VALUE,
    CUTOVER_AUTHORIZATION_VARIABLE,
    ActiveAuthorityLock,
    ActivePaths,
    ActiveStartupError,
    GenerationFencePublisher,
    NonStartingDispatcher,
    cutover_authorization_error,
    load_active_state,
    main,
    run_active,
)
from monitor_controller.model import (
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EventGeneration,
    State,
)
from monitor_controller.runtime.dispatcher import (
    DispatchStartResult,
    PreparedDispatch,
    WorkerActivity,
    WorkerCompletion,
)
from monitor_controller.runtime.persistence import AtomicStateStore, StateNamespace
from monitor_controller.runtime.recovery import WorkerNamespaceSnapshot
from monitor_controller.runtime.transactions import TransactionStore
from monitor_controller.workers.finalize import FileSystemdFinalizationFence

_STOP_MARKER = "stop here"
_UNAUTHORIZED_BUILD = "composition built without authorisation"
_STUB_REACHED = "the non-starting wrapper must refuse before delegating"
_INJECTED_FAILURE = "injected monitor failure"


class _NullLock:
    """Stand in for the authority lock so main() can be tested without one.

    main() must hold the real lock for its whole lifetime, which a unit test
    cannot do meaningfully; lock behaviour is covered by
    :class:`TestActiveAuthorityLock` instead.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Accept the real lock's arguments and ignore them."""

    def __enter__(self) -> Self:
        """Acquire nothing."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release nothing."""


def _paths(root: Path) -> ActivePaths:
    """Build paths rooted entirely inside a temporary directory."""
    return ActivePaths(
        data_home=root / "data",
        state_home=root / "state",
        runtime_dir=root / "runtime",
        config_home=root / "config",
        desktop_configuration_root=root / "desktop-config",
    )


class TestActivePaths:
    """Path derivation and its absolute-path precondition."""

    def test_relative_path_is_refused(self, tmp_path: Path) -> None:
        """A relative path would resolve against an unpredictable cwd."""
        with pytest.raises(ActiveStartupError, match="must be absolute"):
            ActivePaths(
                data_home=Path("data"),
                state_home=tmp_path / "state",
                runtime_dir=tmp_path / "runtime",
                config_home=tmp_path / "config",
                desktop_configuration_root=tmp_path / "desktop-config",
            )

    def test_namespaces_never_collide_with_shadow(self, tmp_path: Path) -> None:
        """Active and shadow must not share state, plans, or transactions.

        A shared path would let one controller read or overwrite the other's
        decisions, which is the failure the separate namespaces prevent.
        """
        paths = _paths(tmp_path)
        active_paths = {
            paths.state_file,
            paths.audit_log,
            paths.authority_lock,
            paths.transaction_namespace,
            paths.plan_store,
        }
        for path in active_paths:
            assert "active" in path.parts
            assert "shadow" not in path.parts
        assert paths.shadow_authority_lock not in active_paths

    def test_missing_runtime_dir_is_refused(self, tmp_path: Path) -> None:
        """XDG_RUNTIME_DIR is how authority is scoped to one login session."""
        with pytest.raises(ActiveStartupError, match="XDG_RUNTIME_DIR"):
            ActivePaths.from_environment({"HOME": str(tmp_path)})

    def test_missing_home_is_refused(self, tmp_path: Path) -> None:
        """Without HOME the XDG defaults cannot be derived."""
        with pytest.raises(ActiveStartupError, match="HOME"):
            ActivePaths.from_environment({"XDG_RUNTIME_DIR": str(tmp_path)})

    def test_relative_home_is_refused(self, tmp_path: Path) -> None:
        """A relative HOME silently produces paths under the cwd."""
        with pytest.raises(ActiveStartupError, match="HOME must be absolute"):
            ActivePaths.from_environment(
                {"HOME": "relative", "XDG_RUNTIME_DIR": str(tmp_path)}
            )

    def test_postswitch_path_matches_the_hook_contract(self, tmp_path: Path) -> None:
        """The autorandr hook defaults to this exact path.

        `.config/autorandr/postswitch` writes
        `$XDG_RUNTIME_DIR/monitor-controller/active/autorandr-postswitch`
        under the active policy. If these diverge, manual autorandr changes
        are silently never delivered to the controller.
        """
        paths = _paths(tmp_path)
        assert paths.postswitch_notification == (
            paths.runtime_dir / "monitor-controller" / "active" / "autorandr-postswitch"
        )


class TestActiveAuthorityLock:
    """Single-instance and cross-authority exclusion."""

    def test_lock_is_acquired_and_released(self, tmp_path: Path) -> None:
        """The happy path: acquire, then release for the next process."""
        paths = _paths(tmp_path)
        with ActiveAuthorityLock(paths.authority_lock):
            assert paths.authority_lock.exists()
        # Released: a second acquisition succeeds.
        with ActiveAuthorityLock(paths.authority_lock):
            pass

    def test_second_instance_is_refused(self, tmp_path: Path) -> None:
        """Two active controllers must never run concurrently."""
        paths = _paths(tmp_path)
        with ActiveAuthorityLock(paths.authority_lock):
            second = ActiveAuthorityLock(paths.authority_lock)
            with pytest.raises(ActiveStartupError, match="already held"):
                second.__enter__()

    def test_lock_is_released_on_exception(self, tmp_path: Path) -> None:
        """A crash inside the context must not strand the authority lock."""
        paths = _paths(tmp_path)
        message = "boom"
        with (
            pytest.raises(RuntimeError, match=message),
            ActiveAuthorityLock(paths.authority_lock),
        ):
            raise RuntimeError(message)
        with ActiveAuthorityLock(paths.authority_lock):
            pass

    def test_held_shadow_lock_refuses_startup(self, tmp_path: Path) -> None:
        """A running shadow controller blocks active startup.

        This is the central guarantee: shadow observes into a namespace active
        is about to take authority over, and is one configuration change away
        from dispatching itself.
        """
        paths = _paths(tmp_path)
        paths.shadow_authority_lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            paths.shadow_authority_lock, os.O_WRONLY | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = ActiveAuthorityLock(
                paths.authority_lock, paths.shadow_authority_lock
            )
            with pytest.raises(ActiveStartupError, match="shadow controller"):
                blocked.__enter__()
        finally:
            os.close(descriptor)

    def test_stale_shadow_lock_file_does_not_block(self, tmp_path: Path) -> None:
        """An unheld shadow lock file is normal and must not block startup.

        The file persists after the shadow process exits. Treating its mere
        existence as a conflict would make active permanently unstartable
        after the first shadow run.
        """
        paths = _paths(tmp_path)
        paths.shadow_authority_lock.parent.mkdir(parents=True, exist_ok=True)
        paths.shadow_authority_lock.touch()
        with ActiveAuthorityLock(paths.authority_lock, paths.shadow_authority_lock):
            assert paths.authority_lock.exists()

    def test_absent_shadow_lock_does_not_block(self, tmp_path: Path) -> None:
        """A shadow that has never run leaves no lock file at all."""
        paths = _paths(tmp_path)
        with ActiveAuthorityLock(paths.authority_lock, paths.shadow_authority_lock):
            assert paths.authority_lock.exists()

    def test_lock_directory_is_private(self, tmp_path: Path) -> None:
        """Authority state must not be world- or group-readable."""
        paths = _paths(tmp_path)
        with ActiveAuthorityLock(paths.authority_lock):
            mode = paths.authority_lock.parent.stat().st_mode & 0o777
            assert mode == 0o700
            assert paths.authority_lock.stat().st_mode & 0o777 == 0o600


class TestConflictingUnits:
    """The declared set of units that must never run beside active."""

    def test_every_dispatching_watcher_is_listed(self) -> None:
        """Omitting a unit here is how two authorities end up coexisting."""
        assert "monitor-controller-shadow.service" in CONFLICTING_UNITS
        assert "monitor-watcher-ng.service" in CONFLICTING_UNITS
        assert "monitor-watcher.service" in CONFLICTING_UNITS

    def test_active_unit_is_not_self_conflicting(self) -> None:
        """A unit conflicting with itself never starts."""
        assert "monitor-controller.service" not in CONFLICTING_UNITS


REPOSITORY = Path(__file__).parents[5]
UNIT_DIR = REPOSITORY / ".config" / "systemd" / "user"

ACTIVE_UNIT = "monitor-controller.service"


def _directives(unit: str, key: str) -> set[str]:
    """Return every value assigned to one directive in a unit file.

    systemd merges repeated directives, so a unit may legitimately declare
    `Conflicts=` several times; this collects them all.
    """
    text = (UNIT_DIR / unit).read_text(encoding="utf-8")
    values: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            values.update(stripped.split("=", 1)[1].split())
    return values


class TestUnitConflictContract:
    """The unit files must encode exclusivity from both sides.

    systemd treats Conflicts= as bidirectional, so a single declaration is
    enough at runtime. It is declared on both sides anyway: if one unit is
    later masked, replaced, or has its file edited, a one-sided declaration
    silently stops protecting anything, and the failure mode is two
    dispatchers racing on the same display.
    """

    def test_active_unit_exists(self) -> None:
        """Everything else here is vacuous if the file is missing."""
        assert (UNIT_DIR / ACTIVE_UNIT).is_file()

    @pytest.mark.parametrize("unit", CONFLICTING_UNITS)
    def test_active_declares_every_conflict(self, unit: str) -> None:
        """The controller names every dispatcher it must displace."""
        assert unit in _directives(ACTIVE_UNIT, "Conflicts")

    @pytest.mark.parametrize("unit", CONFLICTING_UNITS)
    def test_every_conflict_declares_active(self, unit: str) -> None:
        """The reciprocal half, which is the one easily forgotten."""
        assert ACTIVE_UNIT in _directives(unit, "Conflicts")

    @pytest.mark.parametrize("unit", CONFLICTING_UNITS)
    def test_active_is_ordered_after_every_conflict(self, unit: str) -> None:
        """Conflicts= alone permits a brief overlap during changeover.

        Without After=, systemd may start the controller while the outgoing
        watcher is still stopping. The controller's lock would refuse to
        start, which presents as a spurious startup failure.
        """
        assert unit in _directives(ACTIVE_UNIT, "After")

    def test_active_runs_the_active_module(self) -> None:
        """A copy-paste from the shadow unit would launch the wrong root."""
        exec_start = _directives(ACTIVE_UNIT, "ExecStart")
        assert "monitor_controller.active" in " ".join(exec_start)
        assert "monitor_controller.shadow" not in " ".join(exec_start)

    def test_active_declares_the_active_postswitch_policy(self) -> None:
        """The autorandr hook only notifies the controller under this policy.

        Without it the hook takes its legacy branch and launches unkeyed
        desktop work, which is exactly what the active controller exists to
        prevent.
        """
        environment = _directives(ACTIVE_UNIT, "Environment")
        assert "MONITOR_CONTROLLER_POSTSWITCH_POLICY=active" in environment
        assert "MONITOR_CONTROLLER_NAMESPACE=active" in environment

    def test_active_cannot_reach_the_shadow_namespace(self) -> None:
        """Active must not read or overwrite shadow's recorded decisions."""
        inaccessible = " ".join(_directives(ACTIVE_UNIT, "InaccessiblePaths"))
        assert "monitor-controller/shadow" in inaccessible

    def test_shadow_cannot_reach_the_active_namespace(self) -> None:
        """The exclusion must be symmetric, or shadow could corrupt active."""
        inaccessible = " ".join(
            _directives("monitor-controller-shadow.service", "InaccessiblePaths")
        )
        assert "monitor-controller/active" in inaccessible


class TestCutoverAuthorization:
    """The gate on taking display authority.

    The defect these guard is quiet rather than loud: the unit used to run a
    module with no entry point, so it imported cleanly, exited 0, and systemd
    recorded a healthy start for a controller that did not exist. Nothing
    prompts anyone to investigate a service that reports success.
    """

    def test_unauthorized_environment_is_refused(self) -> None:
        """The default state must be refusal, not authority."""
        assert cutover_authorization_error({}) is not None

    def test_authorization_names_what_to_do(self) -> None:
        """A refusal nobody can act on merely relocates the confusion."""
        message = cutover_authorization_error({})
        assert message is not None
        assert CUTOVER_AUTHORIZATION_VARIABLE in message
        assert "monitor-controller preflight" in message
        # Rollback must be reachable from the failure itself: whoever reads
        # this may have no working display to go looking with.
        assert "monitor-watcher-ng.service" in message

    def test_exact_authorization_is_accepted(self) -> None:
        """Otherwise the gate could never be passed deliberately either."""
        environ = {CUTOVER_AUTHORIZATION_VARIABLE: CUTOVER_AUTHORIZATION_VALUE}
        assert cutover_authorization_error(environ) is None

    @pytest.mark.parametrize("value", ["", "1", "true", "yes", "I-HAVE-RUN-PREFLIGHT"])
    def test_near_miss_values_are_refused(self, value: str) -> None:
        """A truthy-looking value must not authorise a display takeover.

        Accepting "1" or "true" would make authority reachable by the kind of
        blanket environment setting that gets applied to a whole session.
        """
        environ = {CUTOVER_AUTHORIZATION_VARIABLE: value}
        assert cutover_authorization_error(environ) is not None

    def test_unit_file_does_not_authorize_itself(self) -> None:
        """Stowing, enabling, and starting must all be insufficient.

        If the unit carried the authorisation, the gate would be satisfied by
        the file's mere presence, which is exactly the accident it exists to
        prevent.
        """
        text = (UNIT_DIR / ACTIVE_UNIT).read_text(encoding="utf-8")
        assert CUTOVER_AUTHORIZATION_VARIABLE not in text


class TestActiveEntryPoint:
    """`python -m monitor_controller.active` must fail closed and say why."""

    def test_wrong_namespace_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A unit launching the wrong composition root must not run."""
        monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", "shadow")
        assert main() == 1
        assert "must be exactly 'active'" in capsys.readouterr().err

    def test_unauthorized_start_exits_non_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The regression: an accidentally enabled unit must fail visibly."""
        monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", "active")
        monkeypatch.delenv(CUTOVER_AUTHORIZATION_VARIABLE, raising=False)
        assert main() == 1
        assert "not authorised" in capsys.readouterr().err

    def test_authorized_start_builds_the_real_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Past the gate, main() must actually compose and run the controller.

        Replaces an earlier test asserting main() refused even when authorised,
        which was correct only while the composition was unimplemented. That
        refusal is what silently stopped the 2026-08-25 cutover after both
        watchers had already been stopped.
        """
        monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", "active")
        monkeypatch.setenv(
            CUTOVER_AUTHORIZATION_VARIABLE,
            CUTOVER_AUTHORIZATION_VALUE,
        )
        composed: list[str] = []

        def _fail_before_running(*_args: object, **_kwargs: object) -> object:
            # Proves main() got as far as composing, without needing a display,
            # a lock, or a real event loop in a unit test.
            composed.append("built")
            raise ActiveStartupError(_STOP_MARKER)

        monkeypatch.setattr(active, "build_active_composition", _fail_before_running)
        monkeypatch.setattr(active, "ActiveAuthorityLock", _NullLock)

        assert main() == 1
        assert composed == ["built"], "main() must build the live composition"

    def test_unauthorized_start_never_builds_the_composition(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The gate must come before any composition work."""
        monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", "active")
        monkeypatch.delenv(CUTOVER_AUTHORIZATION_VARIABLE, raising=False)

        def _must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(_UNAUTHORIZED_BUILD)

        monkeypatch.setattr(active, "build_active_composition", _must_not_run)
        assert main() == 1
        assert "not authorised" in capsys.readouterr().err

    def test_no_unauthorized_environment_returns_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 0 is what systemd reads as "the controller is running".

        No environment lacking both the active namespace and the authorisation
        may produce it. The authorised combination is excluded because it now
        legitimately runs, which a unit test cannot do.
        """
        for namespace in ("active", "shadow", ""):
            for authorization in ("", "1"):
                monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", namespace)
                monkeypatch.setenv(CUTOVER_AUTHORIZATION_VARIABLE, authorization)
                assert main() != 0
                capsys.readouterr()
        # Authorised but in the wrong namespace must still refuse.
        monkeypatch.setenv("MONITOR_CONTROLLER_NAMESPACE", "shadow")
        monkeypatch.setenv(CUTOVER_AUTHORIZATION_VARIABLE, CUTOVER_AUTHORIZATION_VALUE)
        assert main() != 0
        capsys.readouterr()


class _StubDispatcher:
    """Satisfy ActionDispatcher without doing anything.

    Only the read-only methods can ever be reached through
    :class:`NonStartingDispatcher`; the rest exist to satisfy the protocol.
    """

    async def write_request(
        self, *_args: object, **_kwargs: object
    ) -> PreparedDispatch:
        """Unreachable through the non-starting wrapper."""
        raise AssertionError(_STUB_REACHED)

    async def start(self, *_args: object, **_kwargs: object) -> DispatchStartResult:
        """Unreachable through the non-starting wrapper."""
        raise AssertionError(_STUB_REACHED)

    async def discard_prepared(self, *_args: object, **_kwargs: object) -> None:
        """Unreachable through the non-starting wrapper."""
        raise AssertionError(_STUB_REACHED)

    async def stop(self, *_args: object, **_kwargs: object) -> None:
        """Unreachable through the non-starting wrapper."""
        raise AssertionError(_STUB_REACHED)

    async def worker_activity(
        self, *_args: object, **_kwargs: object
    ) -> WorkerActivity:
        """Report the safest possible answer; the wrapper delegates this."""
        return WorkerActivity.INACTIVE

    async def worker_completion(
        self, *_args: object, **_kwargs: object
    ) -> WorkerCompletion | None:
        """Report no terminal evidence; the wrapper delegates this."""
        return None


class _StubScanner:
    """Return a fixed worker-namespace snapshot, or raise."""

    def __init__(
        self,
        snapshot: WorkerNamespaceSnapshot | None = None,
        error: Exception | None = None,
    ) -> None:
        """Bind the canned outcome without touching systemd."""
        self._snapshot = snapshot or WorkerNamespaceSnapshot()
        self._error = error
        self.namespaces: list[StateNamespace] = []

    def scan(self, namespace: StateNamespace) -> WorkerNamespaceSnapshot:
        """Record the namespace asked for, then return or raise."""
        self.namespaces.append(namespace)
        if self._error is not None:
            raise self._error
        return self._snapshot


def _active_store(root: Path) -> AtomicStateStore:
    """Build an active-namespace store whose state home already exists."""
    state_home = root / "state"
    state_home.mkdir(parents=True, exist_ok=True)
    return AtomicStateStore(state_home, StateNamespace.ACTIVE)


def _identity() -> tuple[BootId, ControllerInstanceId, DisplayIdentity]:
    """Return a fixed, deterministic controller identity."""
    return (
        BootId(UUID("11111111-1111-1111-1111-111111111111")),
        ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222")),
        DisplayIdentity(":0"),
    )


class TestLoadActiveState:
    """State loading, boot-id handling, and the authority verdict."""

    def test_non_active_namespace_is_refused(self, tmp_path: Path) -> None:
        """Loading shadow state into the authority would cross namespaces."""
        boot, instance, display = _identity()
        store = AtomicStateStore(tmp_path / "state", StateNamespace.SHADOW)
        with pytest.raises(ActiveStartupError, match="non-active state namespace"):
            load_active_state(
                store,
                boot_id=boot,
                controller_instance=instance,
                display_identity=display,
                scanner=_StubScanner(),
            )

    def test_absent_state_recovers_before_taking_authority(
        self,
        tmp_path: Path,
    ) -> None:
        """A first run has no state, so it must observe before dispatching.

        Recovery reports "authoritative state is missing" and withholds
        authority until a fresh observation arrives. That is deliberate: with
        no record of what was last applied, dispatching immediately could fight
        a display the controller has never looked at.
        """
        boot, instance, display = _identity()
        store = _active_store(tmp_path)
        scanner = _StubScanner()

        result = load_active_state(
            store,
            boot_id=boot,
            controller_instance=instance,
            display_identity=display,
            scanner=scanner,
        )

        assert not result.authority_allowed
        assert result.requires_fresh_observation
        assert result.state.boot_id == boot
        assert result.state.display_identity == display
        # Recovery must scan the active namespace, never shadow's.
        assert scanner.namespaces == [StateNamespace.ACTIVE]

    def test_mismatched_display_identity_is_refused(self, tmp_path: Path) -> None:
        """State from a different X display describes a different session."""
        boot, instance, display = _identity()
        store = _active_store(tmp_path)
        store.save(
            State(
                boot_id=boot,
                controller_instance=instance,
                display_identity=DisplayIdentity(":9"),
            )
        )
        with pytest.raises(ActiveStartupError, match="display identity"):
            load_active_state(
                store,
                boot_id=boot,
                controller_instance=instance,
                display_identity=display,
                scanner=_StubScanner(),
            )

    def test_boot_change_keeps_identity_but_drops_temporal_state(
        self,
        tmp_path: Path,
    ) -> None:
        """Monotonic values are meaningless across boots; identity is not.

        Discarding the whole record would lose the finalized profile and the
        sequence high-water marks, so a reboot would re-apply work already
        done and could reuse action IDs.
        """
        boot, instance, display = _identity()
        previous_boot = BootId(UUID("33333333-3333-3333-3333-333333333333"))
        store = _active_store(tmp_path)
        store.save(
            State(
                boot_id=previous_boot,
                controller_instance=instance,
                display_identity=display,
                desktop_finalized_profile="celtic+external",
                action_sequence_high_water=7,
                transition_sequence_high_water=4,
            )
        )

        result = load_active_state(
            store,
            boot_id=boot,
            controller_instance=instance,
            display_identity=display,
            scanner=_StubScanner(),
        )

        assert result.state.boot_id == boot
        assert result.state.desktop_finalized_profile == "celtic+external"
        assert result.state.action_sequence_high_water >= 7
        assert result.state.transition_sequence_high_water >= 4

    def test_unreadable_state_denies_authority_rather_than_discarding(
        self,
        tmp_path: Path,
    ) -> None:
        """Corrupt state must not be silently replaced by a blank one.

        In-flight transactions may still exist in the worker namespace, so the
        controller has to start into recovery rather than assume a clean slate.
        """
        boot, instance, display = _identity()
        store = _active_store(tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{ not json", encoding="utf-8")

        result = load_active_state(
            store,
            boot_id=boot,
            controller_instance=instance,
            display_identity=display,
            scanner=_StubScanner(),
        )

        assert not result.authority_allowed
        assert result.state.phase is ControllerPhase.RECOVERING

    def test_scanner_failure_denies_authority_without_raising(
        self,
        tmp_path: Path,
    ) -> None:
        """A controller that cannot see the workers must not dispatch.

        It must still *start*, though: exiting would leave the desktop with no
        manager at all, which is strictly worse than a recovering one.
        """
        boot, instance, display = _identity()
        store = _active_store(tmp_path)

        result = load_active_state(
            store,
            boot_id=boot,
            controller_instance=instance,
            display_identity=display,
            scanner=_StubScanner(error=OSError("systemctl unreachable")),
        )

        assert not result.authority_allowed
        assert result.state.phase is ControllerPhase.RECOVERING
        assert result.reasons

    def test_surviving_worker_denies_authority(self, tmp_path: Path) -> None:
        """An unaccounted worker may still be driving xrandr."""
        boot, instance, display = _identity()
        store = _active_store(tmp_path)

        result = load_active_state(
            store,
            boot_id=boot,
            controller_instance=instance,
            display_identity=display,
            scanner=_StubScanner(
                WorkerNamespaceSnapshot(ambiguities=("unknown worker survived",)),
            ),
        )

        assert not result.authority_allowed


class TestNonStartingDispatcher:
    """The dry-run dispatcher must refuse everything that acts."""

    @staticmethod
    def _dispatcher() -> NonStartingDispatcher:
        """Wrap a stub delegate; the delegate is never reached by these tests."""
        return NonStartingDispatcher(_StubDispatcher())

    @pytest.mark.parametrize(
        ("method", "arguments"),
        [
            ("write_request", (None, None)),
            ("start", (None, None)),
            ("discard_prepared", (None,)),
            ("stop", (None, None)),
        ],
    )
    def test_acting_methods_refuse(
        self,
        method: str,
        arguments: tuple[object, ...],
    ) -> None:
        """Preflight must never write, start, discard, or stop anything."""
        dispatcher = self._dispatcher()
        with pytest.raises(ActiveStartupError, match="dry-run dispatcher refuses"):
            asyncio.run(getattr(dispatcher, method)(*arguments))

    def test_it_satisfies_the_dispatcher_protocol(self) -> None:
        """Every protocol method must exist, or a dry run would crash late."""
        for name in (
            "write_request",
            "start",
            "discard_prepared",
            "stop",
            "worker_activity",
            "worker_completion",
        ):
            assert callable(getattr(self._dispatcher(), name))


class _RecordingController:
    """Run until cancelled, recording every lifecycle call run_active makes."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.notifications = 0
        self.cancelled = False
        self.closed = False

    async def run(self) -> None:
        """Block until cancelled, noting that cancellation arrived."""
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def notify_drm_hint(self) -> None:
        """Count one forwarded DRM wake-up hint."""
        self.notifications += 1

    async def close(self) -> None:
        """Record that the controller was closed."""
        self.closed = True


class _FailingMonitor:
    """Forward exactly one hint, then fail, to force the shutdown path."""

    def __init__(self) -> None:
        """Start not yet shut down."""
        self.shutdown = False

    async def run(self, notify: object) -> None:
        """Notify once, then raise, recording that shutdown was reached."""
        try:
            assert callable(notify)
            notify()
            raise RuntimeError(_INJECTED_FAILURE)
        finally:
            self.shutdown = True


class _RecordingTransactions:
    """Record whether the transaction store's descriptors were released."""

    def __init__(self) -> None:
        """Start open."""
        self.closed = False

    def close(self) -> None:
        """Record release."""
        self.closed = True


class TestRunActive:
    """The asyncio run loop, and what it must release on the way out."""

    def test_it_forwards_uevents_and_releases_every_resource(self) -> None:
        """A DRM hint must reach the controller, and nothing may be left open.

        Active has one resource shadow does not: the transaction store holds
        retained directory descriptors, so failing to close it leaks them for
        the life of the session.
        """

        async def exercise() -> None:
            controller = _RecordingController()
            monitor = _FailingMonitor()
            transactions = _RecordingTransactions()
            planner_closed: list[bool] = []
            composition = SimpleNamespace(
                controller=controller,
                planner=SimpleNamespace(close=lambda: planner_closed.append(True)),
                transactions=transactions,
                generation_fence=None,
            )

            with pytest.raises(RuntimeError, match="injected monitor failure"):
                await run_active(composition, monitor)  # type: ignore[arg-type]

            assert controller.notifications == 1
            assert controller.cancelled
            assert controller.closed
            assert monitor.shutdown
            assert planner_closed == [True]
            assert transactions.closed, "transaction descriptors must be released"

        asyncio.run(exercise())

    def test_a_controller_exiting_on_its_own_is_an_error(self) -> None:
        """The controller must run until cancelled; a clean return is a bug.

        Returning would leave systemd believing the unit succeeded while the
        display has no manager — the same silent-success failure mode as the
        entry point that exited 0 without composing anything.
        """

        class QuietController:
            async def run(self) -> None:
                return

            def notify_drm_hint(self) -> None:
                return

            async def close(self) -> None:
                return

        class IdleMonitor:
            async def run(self, notify: object) -> None:
                del notify
                await asyncio.Event().wait()

        async def exercise() -> None:
            composition = SimpleNamespace(
                controller=QuietController(),
                planner=SimpleNamespace(close=lambda: None),
                transactions=None,
                generation_fence=None,
            )
            with pytest.raises(ActiveStartupError, match="exited unexpectedly"):
                await run_active(composition, IdleMonitor())  # type: ignore[arg-type]

        asyncio.run(exercise())


class TestGenerationFencePublisher:
    """The finalizer's file fence must always be publishable or absent."""

    def test_round_trips_through_the_finalize_worker_reader(
        self, tmp_path: Path
    ) -> None:
        """The published record must satisfy the worker's strict reader."""
        path = tmp_path / "runtime" / "event-generation"
        publisher = GenerationFencePublisher(path)
        publisher.publish(EventGeneration(42))

        store = TransactionStore(tmp_path / "transactions")
        try:
            fence = FileSystemdFinalizationFence(
                generation_file=path,
                transaction_store=store,
                environment={},
            )
            assert fence.current_event_generation() == EventGeneration(42)
        finally:
            store.close()
        assert path.read_bytes() == b"42\n"
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_republish_replaces_the_previous_generation(self, tmp_path: Path) -> None:
        """Each increment must supersede the last record atomically."""
        path = tmp_path / "event-generation"
        publisher = GenerationFencePublisher(path)
        publisher.publish(EventGeneration(1))
        publisher.publish(EventGeneration(7))
        assert path.read_bytes() == b"7\n"

    def test_write_failure_withdraws_rather_than_going_stale(
        self, tmp_path: Path
    ) -> None:
        """A stale fence would pass a boundary new hints should reject."""
        parent = tmp_path / "blocked"
        parent.write_text("a file, not a directory")
        failures: list[str] = []
        publisher = GenerationFencePublisher(
            parent / "event-generation", on_failure=failures.append
        )
        publisher.publish(EventGeneration(3))
        assert failures
        assert "event-generation" in failures[0]
        assert not (parent / "event-generation").exists()

    def test_withdraw_removes_the_fence_and_its_temporary(self, tmp_path: Path) -> None:
        """After authority exits, any surviving finalizer must refuse."""
        path = tmp_path / "event-generation"
        publisher = GenerationFencePublisher(path)
        publisher.publish(EventGeneration(5))
        publisher.withdraw()
        assert not path.exists()
        assert not path.with_name(path.name + ".tmp").exists()

    def test_relative_path_is_refused(self) -> None:
        """The unit contract names an absolute runtime path."""
        with pytest.raises(ActiveStartupError):
            GenerationFencePublisher(Path("relative/event-generation"))
