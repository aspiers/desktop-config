"""Exclusive-authority composition root for the deployed active controller.

This is the counterpart to :mod:`monitor_controller.shadow`. Shadow hard-wires
:class:`NullDispatcher` so that no parameter can supply a real one; active
wires :class:`SystemdDispatcher` and therefore *is* the dispatch authority for
the session.

Because exactly one dispatch authority may exist at a time, startup is
fail-closed in three independent ways:

* the active authority lock must be acquired exclusively;
* the shadow authority lock must be *unheld*, proving no shadow controller is
  observing into a namespace that active is about to take over; and
* the legacy shell watchers must not be running.

Any of those failing aborts startup rather than degrading, because a
half-authoritative controller is worse than none: two dispatchers racing on
the same display produce exactly the colliding-relayout failures this whole
subsystem exists to eliminate.

A fourth precondition gates the module entry point rather than the
composition: cutover must have been explicitly authorised. See
:func:`main`.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import uuid4

from monitor_controller.codec import StateCodecError
from monitor_controller.desktop.plan_codec import AtomicPlanStore
from monitor_controller.desktop.planner import (
    AtomicDesktopPlanningAdapter,
    DesktopPlanningInputSource,
    FilesystemDesktopPlanningInputSource,
)
from monitor_controller.model import (
    BootId,
    ControllerInstanceId,
    DisplayIdentity,
    State,
)
from monitor_controller.observer.drm import RootedSysfsReader
from monitor_controller.observer.snapshot import (
    CanonicalSnapshotCoordinator,
    StaticSavedProfiles,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.runtime.commands import BoundedCommandRunner
from monitor_controller.runtime.controller import (
    ObservationAdapter,
    SerializedController,
)
from monitor_controller.runtime.persistence import AtomicStateStore, StateNamespace
from monitor_controller.runtime.recovery import (
    WorkerNamespaceScanner,
    recover_state,
)
from monitor_controller.runtime.scheduler import AsyncioMonotonicClock, SchedulerClock
from monitor_controller.runtime.systemd import (
    SystemdDispatcher,
    SystemdRecoveryScanner,
    SystemdSupervisor,
)
from monitor_controller.runtime.transactions import TransactionStore
from monitor_controller.safeio import read_bounded_text, read_reference_dpi
from monitor_controller.shadow import (
    SHADOW_OBSERVATION_TIMEOUT_SECONDS,
    AsyncSnapshotObserver,
    DrmUeventMonitor,
    GenerationBridge,
    PlanningDisplayBridge,
    ProcBootIdSource,
    ShadowDesktopContextSource,
    SnapshotDesktopDisplaySource,
    UeventMonitor,
    load_saved_profiles,
)

if TYPE_CHECKING:
    from monitor_controller.model import ActionId, ActionLifecycle, WorkerUnit
    from monitor_controller.runtime.dispatcher import (
        ActionDispatcher,
        DispatchEffect,
        DispatchStartResult,
        FinalDispatchFence,
        PreparedDispatch,
        WorkerActivity,
        WorkerCompletion,
        WorkerRequestContext,
    )
    from monitor_controller.runtime.recovery import RecoveryResult

_DIRECTORY_MODE = 0o700
_LOCK_MODE = 0o600

# Units which dispatch display work and therefore must never run beside the
# active controller. Ordered most- to least-recent so diagnostics name the
# likeliest conflict first.
CONFLICTING_UNITS: tuple[str, ...] = (
    "monitor-controller-shadow.service",
    "monitor-watcher-ng.service",
    "monitor-watcher.service",
)

ACTIVE_OBSERVATION_TIMEOUT_SECONDS = SHADOW_OBSERVATION_TIMEOUT_SECONDS

# The deliberate act which authorises this controller to take display
# authority. Deliberately absent from the stowed unit file: stowing, enabling,
# and starting the service must all be insufficient on their own, so that
# authority is only ever taken by someone who meant to take it.
CUTOVER_AUTHORIZATION_VARIABLE = "MONITOR_CONTROLLER_CUTOVER_AUTHORIZED"
CUTOVER_AUTHORIZATION_VALUE = "i-have-run-preflight"


class ActiveStartupError(RuntimeError):
    """Raised when active startup cannot prove exclusive dispatch authority."""


@dataclass(frozen=True, slots=True)
class ActivePaths:
    """All deployed paths, fixed to the authoritative active namespace.

    Deliberately parallel to :class:`monitor_controller.shadow.ShadowPaths`,
    but rooted at ``active/`` rather than ``shadow/`` so the two controllers
    can never read or write each other's state, plans, or transactions.
    """

    data_home: Path
    state_home: Path
    runtime_dir: Path
    config_home: Path
    desktop_configuration_root: Path

    def __post_init__(self) -> None:
        for name, path in (
            ("data home", self.data_home),
            ("state home", self.state_home),
            ("runtime directory", self.runtime_dir),
            ("config home", self.config_home),
            ("desktop configuration root", self.desktop_configuration_root),
        ):
            if not path.is_absolute():
                msg = f"active {name} must be absolute: {path}"
                raise ActiveStartupError(msg)

    @property
    def fixed_venv(self) -> Path:
        """Return the installer-owned service environment."""
        return self.data_home / "monitor-controller" / "venv"

    @property
    def state_file(self) -> Path:
        """Return the active-only authoritative state file."""
        return self.state_home / "monitor-controller" / "active" / "state.json"

    @property
    def audit_log(self) -> Path:
        """Return the active-only rotating decision stream."""
        return self.state_home / "monitor-controller" / "active" / "audit.jsonl"

    @property
    def authority_lock(self) -> Path:
        """Return the lock proving this process is the sole dispatch authority."""
        return self.runtime_dir / "monitor-controller" / "active" / "authority.lock"

    @property
    def shadow_authority_lock(self) -> Path:
        """Return the shadow lock which must be unheld before active may start."""
        return self.runtime_dir / "monitor-controller" / "shadow" / "authority.lock"

    @property
    def transaction_namespace(self) -> Path:
        """Return the active-only namespace for in-flight worker transactions."""
        return self.runtime_dir / "monitor-controller" / "active" / "transactions"

    @property
    def plan_store(self) -> Path:
        """Return the private active namespace for immutable plan bundles."""
        return self.runtime_dir / "monitor-controller" / "active" / "plans"

    @property
    def postswitch_notification(self) -> Path:
        """Return the path manual autorandr writes to notify the controller.

        `.config/autorandr/postswitch` writes here under the active policy
        instead of launching unkeyed desktop work. The path is part of the
        contract with that hook and must match its default.
        """
        return (
            self.runtime_dir / "monitor-controller" / "active" / "autorandr-postswitch"
        )

    @property
    def autorandr_profiles(self) -> Path:
        """Return the explicitly selected read-only saved-profile source."""
        return self.config_home / "autorandr"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Resolve strict XDG paths without consulting service-manager helpers."""
        values = os.environ if environ is None else environ
        home_value = values.get("HOME")
        if not home_value:
            msg = "HOME is required for default XDG paths"
            raise ActiveStartupError(msg)
        home = Path(home_value)
        if not home.is_absolute():
            msg = f"HOME must be absolute: {home}"
            raise ActiveStartupError(msg)
        runtime_value = values.get("XDG_RUNTIME_DIR")
        if not runtime_value:
            msg = "XDG_RUNTIME_DIR is required for active authority isolation"
            raise ActiveStartupError(msg)
        return cls(
            data_home=Path(values.get("XDG_DATA_HOME", home / ".local" / "share")),
            state_home=Path(values.get("XDG_STATE_HOME", home / ".local" / "state")),
            runtime_dir=Path(runtime_value),
            config_home=Path(values.get("XDG_CONFIG_HOME", home / ".config")),
            desktop_configuration_root=Path(
                values.get(
                    "MONITOR_CONTROLLER_DESKTOP_CONFIG_ROOT",
                    home / ".STOW" / "desktop-config",
                )
            ),
        )


class ActiveAuthorityLock:
    """Hold the active authority lock for the complete process lifetime.

    Beyond the shadow lock's single-instance guarantee, this also refuses to
    start while the *shadow* lock is held. Two controllers observing the same
    display is tolerable; two controllers dispatching to it is not, and the
    shadow controller is one upgrade away from dispatching.
    """

    def __init__(self, path: Path, shadow_path: Path | None = None) -> None:
        """Bind the lock paths without touching the filesystem."""
        self._path = path
        self._shadow_path = shadow_path
        self._descriptor: int | None = None

    def _refuse_if_shadow_is_running(self) -> None:
        """Fail closed when a shadow controller still holds its own lock.

        A shadow lock file that exists but is *unheld* is normal: the file
        persists after the process exits. Only a live exclusive holder is a
        conflict, which is what a failed non-blocking acquisition proves.
        """
        if self._shadow_path is None or not self._shadow_path.exists():
            return
        try:
            descriptor = os.open(self._shadow_path, os.O_WRONLY)
        except OSError:
            # Unreadable for reasons unrelated to locking: treat as absent
            # rather than inventing a conflict from a permissions problem.
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            msg = (
                "shadow controller still holds its authority lock: "
                f"{self._shadow_path}; stop monitor-controller-shadow.service "
                "before starting the active controller"
            )
            raise ActiveStartupError(msg) from error
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> Self:
        """Acquire exclusively, refusing to coexist with any other authority."""
        self._refuse_if_shadow_is_running()
        self._path.parent.mkdir(
            mode=_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        self._path.parent.chmod(_DIRECTORY_MODE)
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT,
            _LOCK_MODE,
        )
        os.fchmod(descriptor, _LOCK_MODE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            msg = f"active authority lock is already held: {self._path}"
            raise ActiveStartupError(msg) from error
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release by closing the descriptor, including exceptional exits."""
        del exception_type, exception, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ActiveControllerAdapters:
    """Adapters accepted by the active composition root.

    Unlike :class:`monitor_controller.shadow.ShadowControllerAdapters`, this
    carries a real ``dispatcher``: supplying one is the entire difference
    between observing and acting.
    """

    store: AtomicStateStore
    observer: ObservationAdapter
    planning_source: DesktopPlanningInputSource
    audit: RotatingAuditLog
    clock: SchedulerClock
    dispatcher: ActionDispatcher
    generation_bridge: GenerationBridge | None = None
    adapter_timeout_seconds: float = ACTIVE_OBSERVATION_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ActiveComposition:
    """Expose the wired dispatch authority for tests and service execution."""

    paths: ActivePaths
    controller: SerializedController
    dispatcher: ActionDispatcher
    planner: AtomicDesktopPlanningAdapter
    plan_store: AtomicPlanStore
    store: AtomicStateStore
    audit: RotatingAuditLog
    # Recovery's authority verdict, carried rather than raised: a controller
    # denied authority must still start, or the desktop has no manager at all.
    recovery: RecoveryResult | None = None
    # Retained so run_active() can release the store's directory descriptors.
    transactions: TransactionStore | None = None


class NonStartingDispatcher:
    """Wrap a real dispatcher so preflight can build one without using it.

    Every method that could mutate the display or the manager refuses; the
    read-only queries delegate, so the wiring is genuinely exercised. Preflight
    must be safe to run speculatively, and building the composition already
    proves what it needs to prove — that the venv, the paths and the wiring are
    sound. Starting a worker would prove that by doing the very thing preflight
    exists to avoid doing prematurely.

    Delegation is written out rather than done through ``__getattr__`` so the
    type checker verifies this really satisfies ``ActionDispatcher``, and so
    that a method added to the protocol later fails loudly here instead of
    silently reaching the real dispatcher.
    """

    def __init__(self, delegate: ActionDispatcher) -> None:
        """Bind the real dispatcher without invoking it."""
        self._delegate = delegate

    _REFUSAL = "dry-run dispatcher refuses to {}; preflight must not act"

    def _refuse(self, operation: str) -> ActiveStartupError:
        """Return the error explaining why a dry run will not act."""
        msg = self._REFUSAL.format(operation)
        return ActiveStartupError(msg)

    async def write_request(
        self,
        effect: DispatchEffect,
        context: WorkerRequestContext,
    ) -> PreparedDispatch:
        """Refuse: writing a request would create durable transaction state."""
        del effect, context
        operation = "write a request"
        raise self._refuse(operation)

    async def start(
        self,
        prepared: PreparedDispatch,
        final_fence: FinalDispatchFence,
    ) -> DispatchStartResult:
        """Refuse: starting a worker is exactly what a dry run must not do."""
        del prepared, final_fence
        operation = "start a worker"
        raise self._refuse(operation)

    async def discard_prepared(self, prepared: PreparedDispatch) -> None:
        """Refuse: nothing can have been prepared by a dry run."""
        del prepared
        operation = "discard a prepared request"
        raise self._refuse(operation)

    async def stop(
        self,
        action_id: ActionId,
        terminal_lifecycle: ActionLifecycle,
    ) -> None:
        """Refuse: a dry run must not cancel a real in-flight worker."""
        del action_id, terminal_lifecycle
        operation = "stop a worker"
        raise self._refuse(operation)

    async def worker_activity(self, unit: WorkerUnit) -> WorkerActivity:
        """Delegate: querying the manager mutates nothing."""
        return await self._delegate.worker_activity(unit)

    async def worker_completion(self, unit: WorkerUnit) -> WorkerCompletion | None:
        """Delegate: reading a terminal result mutates nothing."""
        return await self._delegate.worker_completion(unit)


def compose_active_controller(
    *,
    paths: ActivePaths,
    initial_state: State,
    adapters: ActiveControllerAdapters,
) -> ActiveComposition:
    """Compose active mode around the caller-supplied dispatch authority."""
    plan_store = AtomicPlanStore(paths.plan_store)
    planner = AtomicDesktopPlanningAdapter(adapters.planning_source, plan_store)
    controller = SerializedController(
        initial_state=initial_state,
        store=adapters.store,
        observer=adapters.observer,
        planner=planner,
        dispatcher=adapters.dispatcher,
        audit=adapters.audit,
        clock=adapters.clock,
        adapter_timeout_seconds=adapters.adapter_timeout_seconds,
    )
    if adapters.generation_bridge is not None:
        adapters.generation_bridge.bind(controller.current_generation)
    return ActiveComposition(
        paths=paths,
        controller=controller,
        dispatcher=adapters.dispatcher,
        planner=planner,
        plan_store=plan_store,
        store=adapters.store,
        audit=adapters.audit,
    )


def _require_active_namespace(environ: Mapping[str, str]) -> None:
    """Refuse to run as the authority without the active namespace declared.

    Mirrors shadow's check. Its real value is catching a unit file that
    launched the wrong composition root, which would otherwise present as a
    controller writing to a namespace nobody is reading.
    """
    if environ.get("MONITOR_CONTROLLER_NAMESPACE") != "active":
        msg = "MONITOR_CONTROLLER_NAMESPACE must be exactly 'active'"
        raise ActiveStartupError(msg)


def cutover_authorization_error(environ: Mapping[str, str]) -> str | None:
    """Return why cutover is unauthorised, or None when it is authorised.

    Taking display authority is a deliberate, maintainer-approved act, not a
    consequence of the unit existing. Everything else in this module can be
    installed, stowed, and inspected safely; this is the one step that must
    not happen by accident, so it is gated on evidence of intent rather than
    on the code merely being present.

    The gate is an environment variable the unit does not set. Enabling and
    starting the service is therefore not enough — someone has to add it,
    which cannot happen by stowing a file or by systemd retrying a start.
    """
    if environ.get(CUTOVER_AUTHORIZATION_VARIABLE) == CUTOVER_AUTHORIZATION_VALUE:
        return None
    return (
        "cutover is not authorised: the active controller would take display "
        f"authority from {', '.join(CONFLICTING_UNITS)}. This unit refuses to "
        "start until that switch is deliberate.\n"
        "  Before authorising, confirm readiness:\n"
        "    monitor-controller preflight\n"
        "    shadow-trace-status\n"
        f"  To authorise, set {CUTOVER_AUTHORIZATION_VARIABLE}"
        f"={CUTOVER_AUTHORIZATION_VALUE} in the unit:\n"
        "    systemctl --user edit monitor-controller.service\n"
        "  To roll back to the shell watcher:\n"
        "    systemctl --user disable --now monitor-controller.service\n"
        "    systemctl --user enable --now monitor-watcher-ng.service"
    )


def load_active_state(
    store: AtomicStateStore,
    *,
    boot_id: BootId,
    controller_instance: ControllerInstanceId,
    display_identity: DisplayIdentity,
    scanner: WorkerNamespaceScanner,
) -> RecoveryResult:
    """Load authoritative active state and reconcile it against real workers.

    Diverges from :func:`monitor_controller.shadow.load_shadow_state` in the
    way that matters: shadow cancels every persisted recovery unit, because a
    null-dispatch namespace cannot have created one. Active *can* have created
    them, and one may still be driving xrandr right now, so they must be
    reconciled against the service manager rather than terminalized.

    That reconciliation is :func:`recover_state`'s job, and it may decline
    authority. This function returns its verdict intact rather than raising:
    refusing to start would leave the desktop with no manager at all, which is
    strictly worse than starting into a non-dispatching state that can recover.

    Unreadable state is the one case that is *not* silently discarded. It is
    passed to recovery as a corruption, which denies authority and forces a
    fresh observation, because silently starting from a blank state would
    abandon in-flight transactions the worker namespace still contains.
    """
    if store.namespace is not StateNamespace.ACTIVE:
        msg = "active composition refuses a non-active state namespace"
        raise ActiveStartupError(msg)
    persisted: State | None = None
    corruption: Exception | None = None
    if store.path.exists():
        try:
            persisted = store.load()
        except (OSError, StateCodecError, ValueError) as error:
            # Deliberately not fatal, and deliberately not discarded either.
            # Recovery denies authority on corruption, so the controller starts
            # into RECOVERING and reconciles from the worker namespace.
            corruption = error
    if persisted is not None and persisted.display_identity != display_identity:
        msg = (
            "active state display identity does not match this service: "
            f"{persisted.display_identity.value!r} != {display_identity.value!r}"
        )
        raise ActiveStartupError(msg)
    if persisted is not None and persisted.boot_id != boot_id:
        # Absolute monotonic values are meaningful only on their source boot.
        # Preserve non-temporal identity history, but force recovery through a
        # fresh startup observation before any scheduler deadline is armed.
        persisted = State(
            boot_id=boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            desktop_finalized_profile=persisted.desktop_finalized_profile,
            baseline_adoption=persisted.desktop_finalized_profile is None,
            action_sequence_high_water=persisted.action_sequence_high_water,
            transition_sequence_high_water=persisted.transition_sequence_high_water,
            action_tombstones=persisted.action_tombstones,
        )
    return recover_state(
        persisted,
        current_boot_id=boot_id,
        controller_instance=controller_instance,
        display_identity=display_identity,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
        corruption=corruption,
    )


def active_theme(paths: ActivePaths) -> str:
    """Read the desktop theme, defaulting to dark when unset.

    Uses the guarded reader rather than `Path.read_text()`. An earlier version
    of this function did not, which left the *authoritative* controller
    reading a startup file less carefully than the non-authoritative one
    (`dc-t53`).
    """
    path = paths.config_home / "theme"
    if not path.exists():
        return "dark"
    value = read_bounded_text(path, "desktop:theme", ActiveStartupError).strip()
    if value not in {"dark", "light"}:
        msg = "desktop theme must be exactly dark or light"
        raise ActiveStartupError(msg)
    return value


def _desktop_configuration_root(paths: ActivePaths) -> Path:
    """Resolve the desktop configuration tree, refusing anything unusable."""
    try:
        root = paths.desktop_configuration_root.resolve(strict=True)
    except OSError as error:
        msg = "desktop configuration root cannot be resolved"
        raise ActiveStartupError(msg) from error
    if not root.is_dir():
        msg = "desktop configuration root is not a directory"
        raise ActiveStartupError(msg)
    return root


def build_active_composition(
    paths: ActivePaths,
    environ: Mapping[str, str] | None = None,
    *,
    dispatching: bool = True,
) -> ActiveComposition:
    """Build the real observer, persistence, and systemd dispatch authority.

    Structurally parallel to
    :func:`monitor_controller.shadow.build_shadow_composition`, with three
    deliberate differences:

    * **The dispatcher is real.** ``SystemdDispatcher`` over
      ``SystemdSupervisor``, writing to a ``TransactionStore`` in the active
      namespace. This is the whole difference between observing and acting.
    * **No autorandr isolation namespace.** Shadow copies saved profiles into a
      hook-free tree so its observations cannot trigger anything. Active
      applies for real, so it must read the live configuration; an isolated
      copy would make it plan against profiles it would not then apply.
    * **Recovery may decline authority**, and that verdict is carried on the
      composition rather than raised.

    Observation queries autorandr with ``--match-edid``; application must not.
    See ``ObserverCommands`` for why that asymmetry is load-bearing.

    *dispatching* exists for preflight's dry run: with it false the composition
    is built exactly as it would be, but no worker unit can be started, so the
    build can be exercised speculatively without becoming an authority.
    """
    values = os.environ if environ is None else environ
    display_value = values.get("DISPLAY")
    if not display_value:
        msg = "DISPLAY is required for canonical active observation"
        raise ActiveStartupError(msg)
    boot_source = ProcBootIdSource()
    boot_id = boot_source.current_boot_id()
    instance = ControllerInstanceId(uuid4())
    display = DisplayIdentity(display_value)

    store = AtomicStateStore(paths.state_home, StateNamespace.ACTIVE)
    if store.path != paths.state_file:
        msg = "active state store escaped its declared namespace"
        raise ActiveStartupError(msg)

    transactions = TransactionStore(paths.transaction_namespace)
    supervisor = SystemdSupervisor()
    recovery = load_active_state(
        store,
        boot_id=boot_id,
        controller_instance=instance,
        display_identity=display,
        scanner=SystemdRecoveryScanner(transactions, supervisor),
    )
    initial = recovery.state

    clock = AsyncioMonotonicClock()
    bridge = GenerationBridge(initial.event_generation)
    display_bridge = PlanningDisplayBridge()
    planning_source = FilesystemDesktopPlanningInputSource(
        root=_desktop_configuration_root(paths),
        display=display_bridge,
        context=ShadowDesktopContextSource(
            host_name=socket.gethostname().split(".", maxsplit=1)[0],
            theme=active_theme(paths),
            # Read once here, not during planning: planning must be
            # reproducible, and set-layout-dpi moves this value mid-relayout.
            reference_dpi=read_reference_dpi(),
        ),
    )
    # Read the live profiles, not an isolated copy: these are the profiles this
    # controller will actually apply.
    profiles = StaticSavedProfiles(
        tuple(
            planning_source.complete_profile(profile)
            for profile in load_saved_profiles(paths.autorandr_profiles)
        )
    )
    coordinator = CanonicalSnapshotCoordinator(
        drm_tree=RootedSysfsReader(Path("/sys/class/drm")),
        command_runner=BoundedCommandRunner(),
        profiles=profiles,
        boot_id_source=boot_source,
        clock=clock,
        event_generation_source=bridge,
        initial_observation_generation=initial.observation_generation,
    )
    display_bridge.bind(SnapshotDesktopDisplaySource(coordinator))
    dispatcher: ActionDispatcher = SystemdDispatcher(
        transactions,
        supervisor,
        autorandr_profiles=profiles,
    )
    if not dispatching:
        dispatcher = NonStartingDispatcher(dispatcher)
    composition = compose_active_controller(
        paths=paths,
        initial_state=initial,
        adapters=ActiveControllerAdapters(
            store=store,
            observer=AsyncSnapshotObserver(coordinator),
            planning_source=planning_source,
            audit=RotatingAuditLog(paths.audit_log, initial),
            clock=clock,
            dispatcher=dispatcher,
            generation_bridge=bridge,
        ),
    )
    return replace(composition, recovery=recovery, transactions=transactions)


async def run_active(
    composition: ActiveComposition,
    monitor: UeventMonitor | None = None,
) -> None:
    """Run one controller and one DRM producer, cancelling both on any exit.

    Mirrors :func:`monitor_controller.shadow.run_shadow`. Unlike shadow, this
    can reach a quiescent verified state, because its dispatcher actually
    prepares the desktop.
    """
    producer = DrmUeventMonitor() if monitor is None else monitor
    controller_task = asyncio.create_task(
        composition.controller.run(),
        name="monitor-controller-consumer",
    )
    monitor_task = asyncio.create_task(
        producer.run(composition.controller.notify_drm_hint),
        name="monitor-controller-uevents",
    )
    tasks = (controller_task, monitor_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed = next(iter(done))
        error = completed.exception()
        if error is not None:
            raise error
        msg = f"active runtime task exited unexpectedly: {completed.get_name()}"
        raise ActiveStartupError(msg)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await composition.controller.close()
        composition.planner.close()
        if composition.transactions is not None:
            composition.transactions.close()


# Argument which asks this module to prove it can start, then exit without
# taking authority. Preflight uses it; see cutover.check_entry_point_runs.
DRY_RUN_ARGUMENT = "--dry-run"


def dry_run(environ: Mapping[str, str]) -> int:
    """Prove the entry point can start, without taking authority or dispatching.

    This exists because preflight previously reported six green checks for a
    binary that could not start at all: every check examined the *environment*,
    none asked whether the controller runs. The cutover then stopped both
    watchers and failed, leaving the desktop unmanaged until rollback.

    Deliberately does *not* acquire the authority lock, start a worker, or
    build the dispatcher. Preflight must be safe to run speculatively — a
    preflight that cannot be run speculatively will not be run at all — so
    this proves only what can be proven without side effects:

    * the module imports under the unit's own `python -I`, catching a broken
      or half-installed venv; and
    * the composition root can be built for the resolved paths, catching the
      missing-implementation case that actually bit.

    Everything the lock would prove is already covered by
    `cutover.check_authority_lock_free`.
    """
    try:
        _require_active_namespace(environ)
        paths = ActivePaths.from_environment(environ)
        build_active_composition(paths, environ, dispatching=False).planner.close()
    except (ActiveStartupError, OSError, ValueError) as error:
        print(f"monitor-controller: dry run failed: {error}", file=sys.stderr)
        return 1
    print("monitor-controller: dry run succeeded; the controller can start")
    return 0


def main() -> int:
    """Refuse to take display authority unless cutover was authorised.

    Fails closed and loudly. The failure this exists to prevent is quiet: the
    unit previously ran a module with no entry point, so it imported cleanly,
    exited 0, and systemd recorded a healthy start for a controller that did
    not exist. An inert authority that reports success is worse than one that
    reports failure, because nothing prompts anyone to look.

    Once authorised, this composes the real observer, dispatcher and
    supervisor under the authority lock, mirroring
    :func:`monitor_controller.shadow.main`, and runs until cancelled.
    """
    try:
        _require_active_namespace(os.environ)
    except ActiveStartupError as error:
        print(f"monitor-controller: {error}", file=sys.stderr)
        return 1
    unauthorized = cutover_authorization_error(os.environ)
    if unauthorized is not None:
        print(f"monitor-controller: {unauthorized}", file=sys.stderr)
        return 1
    if DRY_RUN_ARGUMENT in sys.argv[1:]:
        return dry_run(os.environ)
    try:
        paths = ActivePaths.from_environment()
        with ActiveAuthorityLock(paths.authority_lock, paths.shadow_authority_lock):
            composition = build_active_composition(paths)
            recovery = composition.recovery
            if recovery is not None and not recovery.authority_allowed:
                # Deliberately not fatal. Recovery denies authority when it
                # cannot account for the worker namespace; the controller then
                # starts into RECOVERING and dispatches nothing until a fresh
                # observation resolves it. Exiting instead would leave the
                # desktop with no manager at all, which is strictly worse.
                print(
                    "monitor-controller: starting without dispatch authority: "
                    + ("; ".join(recovery.reasons) or "recovery denied authority"),
                    file=sys.stderr,
                )
            asyncio.run(run_active(composition))
    except KeyboardInterrupt:
        return 0
    except Exception as error:  # noqa: BLE001 - service composition boundary
        print(f"monitor-controller: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - fixed-venv module entry point
    raise SystemExit(main())
