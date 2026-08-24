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
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self

from monitor_controller.desktop.plan_codec import AtomicPlanStore
from monitor_controller.desktop.planner import (
    AtomicDesktopPlanningAdapter,
    DesktopPlanningInputSource,
)
from monitor_controller.runtime.controller import (
    ObservationAdapter,
    SerializedController,
)
from monitor_controller.shadow import (
    SHADOW_OBSERVATION_TIMEOUT_SECONDS,
    GenerationBridge,
)

if TYPE_CHECKING:
    from monitor_controller.model import State
    from monitor_controller.runtime.audit import RotatingAuditLog
    from monitor_controller.runtime.dispatcher import ActionDispatcher
    from monitor_controller.runtime.persistence import AtomicStateStore
    from monitor_controller.runtime.scheduler import SchedulerClock

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
