"""Null-dispatch-only composition root for the deployed shadow controller."""

from __future__ import annotations

import asyncio
import configparser
import fcntl
import os
import shutil
import socket
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from monitor_controller.codec import StateCodecError
from monitor_controller.desktop.layout import DisplayScreenSnapshot
from monitor_controller.desktop.plan_codec import AtomicPlanStore, PlannedTopology
from monitor_controller.desktop.planner import (
    AtomicDesktopPlanningAdapter,
    DesktopContext,
    DesktopDisplaySnapshot,
    DesktopDisplaySnapshotSource,
    DesktopPlanningError,
    DesktopPlanningInputSource,
    FilesystemDesktopPlanningInputSource,
    ProfileMonitorIdentity,
)
from monitor_controller.model import (
    ActionLifecycle,
    ActionTombstone,
    BootId,
    CanonicalObservation,
    ControllerInstanceId,
    DisplayIdentity,
    EventGeneration,
    RawEvidenceSource,
    RequestPlan,
    State,
    bound_action_tombstones,
)
from monitor_controller.observer.autorandr import (
    MAX_PROFILE_NAME_CHARS,
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import RootedSysfsReader
from monitor_controller.observer.evidence import (
    MAX_COMMAND_BYTES,
    TextCommandEvidence,
)
from monitor_controller.observer.snapshot import (
    DEFAULT_OBSERVER_TIMEOUT_SECONDS,
    CanonicalSnapshotCoordinator,
    StaticSavedProfiles,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.runtime.commands import BoundedCommandRunner
from monitor_controller.runtime.controller import (
    ObservationAdapter,
    SerializedController,
)
from monitor_controller.runtime.dispatcher import NullDispatcher
from monitor_controller.runtime.persistence import AtomicStateStore, StateNamespace
from monitor_controller.runtime.scheduler import AsyncioMonotonicClock, SchedulerClock

_NETLINK_KOBJECT_UEVENT = 15
_UEVENT_GROUP = 1
_MAX_UEVENT_BYTES = 64 * 1024
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_MODE = 0o600
_OBSERVER_COMMAND_COUNT = 5
_OBSERVER_TIMEOUT_MARGIN_SECONDS = 5.0
SHADOW_OBSERVATION_TIMEOUT_SECONDS = (
    _OBSERVER_COMMAND_COUNT * DEFAULT_OBSERVER_TIMEOUT_SECONDS
    + _OBSERVER_TIMEOUT_MARGIN_SECONDS
)
_SAFE_AUTORANDR_SETTINGS = frozenset({"skip-options"})
_AUTORANDR_ENVIRONMENT_DENYLIST = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)


class ShadowStartupError(RuntimeError):
    """Raised when shadow startup cannot prove its isolated safe configuration."""


@dataclass(frozen=True, slots=True)
class ShadowPaths:
    """All deployed paths, fixed to the non-authoritative shadow namespace."""

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
                msg = f"shadow {name} must be absolute: {path}"
                raise ShadowStartupError(msg)

    @property
    def fixed_venv(self) -> Path:
        """Return the installer-owned service environment."""
        return self.data_home / "monitor-controller" / "venv"

    @property
    def state_file(self) -> Path:
        """Return the shadow-only authoritative state file."""
        return self.state_home / "monitor-controller" / "shadow" / "state.json"

    @property
    def audit_log(self) -> Path:
        """Return the shadow-only rotating decision stream."""
        return self.state_home / "monitor-controller" / "shadow" / "audit.jsonl"

    @property
    def authority_lock(self) -> Path:
        """Return the lock which never overlaps active-controller authority."""
        return self.runtime_dir / "monitor-controller" / "shadow" / "authority.lock"

    @property
    def transaction_namespace(self) -> Path:
        """Reserve a shadow-only path which the null dispatcher never creates."""
        return self.runtime_dir / "monitor-controller" / "shadow" / "transactions"

    @property
    def plan_store(self) -> Path:
        """Return the private shadow namespace for immutable plan bundles."""
        return self.runtime_dir / "monitor-controller" / "shadow" / "plans"

    @property
    def autorandr_profiles(self) -> Path:
        """Return the explicitly selected read-only saved-profile source."""
        return self.config_home / "autorandr"

    @property
    def autorandr_isolation_root(self) -> Path:
        """Return the shadow-owned hook-free autorandr command namespace."""
        return self.state_home / "monitor-controller" / "shadow" / "autorandr-observer"

    @property
    def autorandr_isolated_profiles(self) -> Path:
        """Return the only profile tree visible to autorandr subprocesses."""
        return self.autorandr_isolation_root / "config" / "autorandr"

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
            raise ShadowStartupError(msg)
        home = Path(home_value)
        if not home.is_absolute():
            msg = f"HOME must be absolute: {home}"
            raise ShadowStartupError(msg)
        runtime_value = values.get("XDG_RUNTIME_DIR")
        if not runtime_value:
            msg = "XDG_RUNTIME_DIR is required for shadow authority isolation"
            raise ShadowStartupError(msg)
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


class ShadowAuthorityLock:
    """Hold the shadow authority lock for the complete process lifetime."""

    def __init__(self, path: Path) -> None:
        """Bind one shadow lock path without touching the filesystem."""
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        """Acquire exclusively and fail instead of starting a second shadow."""
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
            msg = f"shadow authority lock is already held: {self._path}"
            raise ShadowStartupError(msg) from error
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
class ProcBootIdSource:
    """Read the kernel boot identity from one explicit procfs file."""

    path: Path = Path("/proc/sys/kernel/random/boot_id")

    def current_boot_id(self) -> BootId:
        """Return a strict UUID and reject missing or malformed procfs data."""
        try:
            value = self.path.read_text(encoding="ascii").strip()
            return BootId(UUID(value))
        except (OSError, UnicodeError, ValueError) as error:
            msg = f"cannot read a valid kernel boot ID from {self.path}"
            raise ShadowStartupError(msg) from error


class GenerationBridge:
    """Bind the observer fence after the serialized controller is constructed."""

    def __init__(self, initial: EventGeneration) -> None:
        """Return the persisted generation until the controller is bound."""
        self._source: Callable[[], EventGeneration] = lambda: initial

    def bind(self, source: Callable[[], EventGeneration]) -> None:
        """Use the controller's producer-side generation from now on."""
        self._source = source

    def current_generation(self) -> EventGeneration:
        """Return the current generation without mutating controller state."""
        return self._source()


class AsyncSnapshotObserver:
    """Serialize blocking snapshots even after an asyncio caller times out."""

    def __init__(self, coordinator: CanonicalSnapshotCoordinator) -> None:
        """Bind one synchronous coordinator without starting an observation."""
        self._coordinator = coordinator
        self._lock = asyncio.Lock()
        self._in_flight: asyncio.Task[CanonicalObservation] | None = None

    async def observe(self) -> CanonicalObservation:
        """Await at most one shielded worker-thread observation at a time."""
        async with self._lock:
            task = self._in_flight
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(self._coordinator.observe),
                    name="monitor-controller-shadow-observation",
                )
                task.add_done_callback(self._retrieve_background_exception)
                self._in_flight = task
            try:
                # wait_for() may cancel this coroutine, but it cannot stop a thread.
                # Shielding and retaining the task makes the next request join the
                # same bounded command sequence instead of starting an overlap.
                return await asyncio.shield(task)
            finally:
                if task.done():
                    self._in_flight = None
                    # Retrieve an exception even if cancellation won the race with
                    # task completion, avoiding an orphaned task warning.
                    if not task.cancelled():
                        task.exception()

    @staticmethod
    def _retrieve_background_exception(
        task: asyncio.Task[CanonicalObservation],
    ) -> None:
        """Consume a late failure when no subsequent request joins the task."""
        if not task.cancelled():
            task.exception()


class SnapshotDesktopDisplaySource:
    """Convert one retained canonical/XRandR capture into planning evidence."""

    def __init__(self, coordinator: CanonicalSnapshotCoordinator) -> None:
        """Bind the coordinator which retains admitted planning captures."""
        self._coordinator = coordinator

    def display_for(self, request: RequestPlan) -> DesktopDisplaySnapshot:
        """Return geometry and topology from the request's exact observation."""
        try:
            capture = self._coordinator.planning_capture(
                request.input_key.observation_key
            )
        except KeyError as error:
            raise DesktopPlanningError(str(error)) from error
        observation = capture.observation
        if observation.observation_key != request.input_key.observation_key:
            msg = "planning capture observation key differs"
            raise DesktopPlanningError(msg)
        matching = tuple(
            profile
            for profile in observation.eligible_profiles
            if profile.profile == request.profile
            and profile.layout == request.input_key.layout
            and profile.mapping == request.input_key.mapping
            and profile.configuration_hashes == request.input_key.configuration_hashes
        )
        if len(matching) != 1:
            msg = "planning request is absent from its canonical observation"
            raise DesktopPlanningError(msg)
        screens = tuple(
            DisplayScreenSnapshot(
                output=output.name,
                width=output.geometry.width,
                height=output.geometry.height,
                x=output.geometry.x,
                y=output.geometry.y,
                width_mm=output.width_mm,
                height_mm=output.height_mm,
                primary=output.primary,
            )
            for output in capture.xrandr.outputs
            if output.geometry is not None
        )
        return DesktopDisplaySnapshot(
            physical_epoch=request.input_key.physical_epoch,
            physical_token=observation.physical_token,
            admitted_event_generation=observation.end_event_generation,
            observation_key=observation.observation_key,
            topology=PlannedTopology(
                kernel_connected_outputs=observation.kernel_connected_outputs,
                kernel_external_outputs=observation.kernel_external_outputs,
                x_connected_outputs=observation.x_connected_outputs,
                x_active_outputs=observation.x_active_outputs,
            ),
            screens=screens,
        )


class PlanningDisplayBridge:
    """Bind capture-backed display facts after the coordinator is assembled."""

    def __init__(self) -> None:
        """Create an unbound bridge for composition-cycle construction."""
        self._source: DesktopDisplaySnapshotSource | None = None

    def bind(self, source: DesktopDisplaySnapshotSource) -> None:
        """Bind the completed capture-backed source exactly once at startup."""
        if self._source is not None:
            msg = "planning display bridge is already bound"
            raise DesktopPlanningError(msg)
        self._source = source

    def display_for(self, request: RequestPlan) -> DesktopDisplaySnapshot:
        """Delegate to the bound capture-backed display source."""
        if self._source is None:
            msg = "planning display bridge is not bound"
            raise DesktopPlanningError(msg)
        return self._source.display_for(request)


@dataclass(frozen=True, slots=True)
class ShadowDesktopContextSource:
    """Personal host policy captured without shell or mutable display queries."""

    host_name: str
    theme: str

    def context_for(
        self,
        profile: str,
        layout: str,
        monitor_identity: ProfileMonitorIdentity,
    ) -> DesktopContext:
        """Bind host policy only to immutable saved-EDID model evidence."""
        del profile, layout
        primary = monitor_identity.primary
        return DesktopContext(
            host_name=self.host_name,
            is_laptop=self.host_name == "celtic",
            theme=self.theme,
            reference_dpi=96,
            primary_monitor_output=primary.output,
            primary_monitor_model=primary.model,
            primary_monitor_identity_hash=primary.evidence_hash,
            benq_connected=any(
                item.model == "BenQ BL3200" for item in monitor_identity.monitors
            ),
        )


@dataclass(frozen=True, slots=True)
class ShadowControllerAdapters:
    """Non-dispatch adapters accepted by the shadow-only composition root."""

    store: AtomicStateStore
    observer: ObservationAdapter
    planning_source: DesktopPlanningInputSource
    audit: RotatingAuditLog
    clock: SchedulerClock
    generation_bridge: GenerationBridge | None = None
    adapter_timeout_seconds: float = SHADOW_OBSERVATION_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ShadowComposition:
    """Expose the hard-wired null boundary for tests and service execution."""

    paths: ShadowPaths
    controller: SerializedController
    dispatcher: NullDispatcher
    planner: AtomicDesktopPlanningAdapter
    plan_store: AtomicPlanStore
    store: AtomicStateStore
    audit: RotatingAuditLog


def compose_shadow_controller(
    *,
    paths: ShadowPaths,
    initial_state: State,
    adapters: ShadowControllerAdapters,
) -> ShadowComposition:
    """Compose shadow mode with no parameter capable of supplying a dispatcher."""
    dispatcher = NullDispatcher()
    plan_store = AtomicPlanStore(paths.plan_store)
    planner = AtomicDesktopPlanningAdapter(adapters.planning_source, plan_store)
    controller = SerializedController(
        initial_state=initial_state,
        store=adapters.store,
        observer=adapters.observer,
        planner=planner,
        dispatcher=dispatcher,
        audit=adapters.audit,
        clock=adapters.clock,
        adapter_timeout_seconds=adapters.adapter_timeout_seconds,
    )
    if adapters.generation_bridge is not None:
        adapters.generation_bridge.bind(controller.current_generation)
    return ShadowComposition(
        paths=paths,
        controller=controller,
        dispatcher=dispatcher,
        planner=planner,
        plan_store=plan_store,
        store=adapters.store,
        audit=adapters.audit,
    )


def _bounded_text_evidence(path: Path, reference: str) -> TextCommandEvidence:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            msg = f"saved autorandr input is not a regular file: {reference}"
            raise ShadowStartupError(msg)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_COMMAND_BYTES + 1)
    except OSError as error:
        msg = f"cannot read saved autorandr profile input {reference}"
        raise ShadowStartupError(msg) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_COMMAND_BYTES:
        msg = f"saved autorandr profile input is too large: {reference}"
        raise ShadowStartupError(msg)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        msg = f"saved autorandr profile input is not UTF-8: {reference}"
        raise ShadowStartupError(msg) from error
    return TextCommandEvidence(
        source=RawEvidenceSource.AUTORANDR_PROFILES,
        reference=reference,
        stdout=text,
    )


def _validated_autorandr_settings(root: Path) -> str | None:
    settings = root / "settings.ini"
    if not settings.exists():
        return None
    text = _bounded_text_evidence(settings, "autorandr:settings.ini").stdout
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as error:
        msg = "saved autorandr settings.ini is invalid"
        raise ShadowStartupError(msg) from error
    sections = set(parser.sections())
    keys: set[str] = (
        {str(key) for key in parser["config"]} if "config" in parser else set()
    )
    if sections - {"config"} or parser.defaults() or keys - _SAFE_AUTORANDR_SETTINGS:
        msg = "saved autorandr settings.ini contains observer-unsafe options"
        raise ShadowStartupError(msg)
    return text


def _write_isolated_data(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(_FILE_MODE)


def prepare_isolated_autorandr_namespace(
    source_root: Path,
    isolation_root: Path,
) -> tuple[SavedAutorandrProfile, ...]:
    """Copy only validated non-executable data into autorandr's private XDG tree."""
    profiles = load_saved_profiles(source_root)
    settings = _validated_autorandr_settings(source_root)
    try:
        if isolation_root.is_symlink():
            isolation_root.unlink()
        elif isolation_root.exists():
            shutil.rmtree(isolation_root)
    except OSError as error:
        msg = f"cannot replace shadow autorandr namespace: {isolation_root}"
        raise ShadowStartupError(msg) from error
    profile_root = isolation_root / "config" / "autorandr"
    system_config = isolation_root / "config-dirs"
    isolated_home = isolation_root / "home"
    for directory in (profile_root, system_config, isolated_home):
        directory.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(_DIRECTORY_MODE)
    if settings is not None:
        _write_isolated_data(profile_root / "settings.ini", settings)
    for profile in profiles:
        source = source_root / profile.name
        destination = profile_root / profile.name
        destination.mkdir(mode=_DIRECTORY_MODE)
        for name in ("config", "setup", "layout"):
            candidate = source / name
            if name == "layout" and not candidate.exists():
                continue
            evidence = _bounded_text_evidence(
                candidate,
                f"autorandr:{profile.name}/{name}",
            )
            _write_isolated_data(destination / name, evidence.stdout)
    # Parse the exact copies consumed by subprocesses, not a separate source view.
    return load_saved_profiles(profile_root)


def _xauthority_for_isolated_home(environ: Mapping[str, str]) -> str | None:
    """Resolve readable X11 authority before replacing the command's home."""
    display = environ.get("DISPLAY")
    if not display:
        return None
    inherited = environ.get("XAUTHORITY")
    if inherited:
        authority = Path(inherited)
        value = inherited
        source = "inherited XAUTHORITY"
    else:
        home_value = environ.get("HOME")
        if not home_value:
            msg = (
                f"DISPLAY {display!r} requires X11 authority, but XAUTHORITY is "
                "empty and HOME is unavailable for the .Xauthority fallback"
            )
            raise ShadowStartupError(msg)
        home = Path(home_value)
        if not home.is_absolute():
            msg = f"HOME must be absolute to resolve X11 authority: {home}"
            raise ShadowStartupError(msg)
        try:
            authority = (home / ".Xauthority").resolve()
        except OSError as error:
            msg = f"cannot resolve HOME .Xauthority fallback: {home / '.Xauthority'}"
            raise ShadowStartupError(msg) from error
        value = str(authority)
        source = "HOME .Xauthority fallback"
    error_message = (
        f"DISPLAY {display!r} requires a readable X11 authority file, but the "
        f"{source} is unusable: {authority}; set XAUTHORITY to a readable file"
    )
    try:
        authority_stat = authority.stat()
    except OSError as error:
        raise ShadowStartupError(error_message) from error
    if not stat.S_ISREG(authority_stat.st_mode):
        raise ShadowStartupError(error_message)
    try:
        with authority.open("rb") as stream:
            stream.read(1)
    except OSError as error:
        raise ShadowStartupError(error_message) from error
    return value


def isolated_autorandr_environment(
    isolation_root: Path,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Return an inherited command environment with all autorandr roots replaced."""
    values = dict(environ)
    xauthority = _xauthority_for_isolated_home(values)
    for name in _AUTORANDR_ENVIRONMENT_DENYLIST:
        values.pop(name, None)
    values.update(
        {
            "HOME": str(isolation_root / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "XDG_CONFIG_HOME": str(isolation_root / "config"),
            "XDG_CONFIG_DIRS": str(isolation_root / "config-dirs"),
        }
    )
    if xauthority is not None:
        values["XAUTHORITY"] = xauthority
    return values


def load_saved_profiles(root: Path) -> tuple[SavedAutorandrProfile, ...]:
    """Strictly load immutable autorandr config/setup/layout files once."""
    try:
        candidates = tuple(
            sorted(
                path
                for path in root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        )
    except OSError as error:
        msg = f"cannot enumerate saved autorandr profiles under {root}"
        raise ShadowStartupError(msg) from error
    profiles: list[SavedAutorandrProfile] = []
    for directory in candidates:
        config = directory / "config"
        setup = directory / "setup"
        if not config.is_file() or not setup.is_file():
            continue
        name = directory.name
        if len(name) > MAX_PROFILE_NAME_CHARS:
            msg = f"saved autorandr profile name is too long: {name[:32]}"
            raise ShadowStartupError(msg)
        prefix = f"autorandr:{name}"
        layout = directory / "layout"
        result = parse_saved_profile(
            name,
            _bounded_text_evidence(config, f"{prefix}/config"),
            _bounded_text_evidence(setup, f"{prefix}/setup"),
            (
                _bounded_text_evidence(layout, f"{prefix}/layout")
                if layout.is_file()
                else None
            ),
        )
        if not result.valid or result.profile is None:
            reasons = ",".join(issue.code.value for issue in result.issues)
            msg = f"saved autorandr profile {name!r} is invalid: {reasons}"
            raise ShadowStartupError(msg)
        profiles.append(result.profile)
    return tuple(sorted(profiles, key=lambda item: item.name))


def _resolve_shadow_recovery_exclusions(state: State) -> State:
    """Terminalize stale units which a null-dispatch namespace cannot create."""
    if not state.recovery_units:
        return state
    cancellations = tuple(
        ActionTombstone(unit.action_id, ActionLifecycle.CANCELLED)
        for unit in state.recovery_units
    )
    return replace(
        state,
        action_tombstones=bound_action_tombstones(
            (*state.action_tombstones, *cancellations),
            protected_action_ids=frozenset(item.action_id for item in cancellations),
        ),
        recovery_units=(),
    )


def load_shadow_state(
    store: AtomicStateStore,
    *,
    boot_id: BootId,
    controller_instance: ControllerInstanceId,
    display_identity: DisplayIdentity,
) -> State:
    """Load strict shadow state or create a non-authoritative recovery baseline."""
    if store.namespace is not StateNamespace.SHADOW:
        msg = "shadow composition refuses a non-shadow state namespace"
        raise ShadowStartupError(msg)
    if not store.path.exists():
        return State(
            boot_id=boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
        )
    try:
        persisted = store.load()
    except (OSError, StateCodecError, ValueError) as error:
        msg = f"shadow state is unreadable and will not be discarded: {store.path}"
        raise ShadowStartupError(msg) from error
    if persisted.display_identity != display_identity:
        msg = (
            "shadow state display identity does not match this service: "
            f"{persisted.display_identity.value!r} != {display_identity.value!r}"
        )
        raise ShadowStartupError(msg)
    persisted = _resolve_shadow_recovery_exclusions(persisted)
    if persisted.boot_id != boot_id:
        # Absolute monotonic values are meaningful only on their source boot.
        # Preserve non-temporal identity history, but force recovery through a
        # fresh startup observation before any scheduler deadline is armed.
        return State(
            boot_id=boot_id,
            controller_instance=controller_instance,
            display_identity=display_identity,
            desktop_finalized_profile=persisted.desktop_finalized_profile,
            baseline_adoption=persisted.desktop_finalized_profile is None,
            action_sequence_high_water=persisted.action_sequence_high_water,
            transition_sequence_high_water=(persisted.transition_sequence_high_water),
            action_tombstones=persisted.action_tombstones,
        )
    return replace(persisted, controller_instance=controller_instance)


def _shadow_theme(paths: ShadowPaths) -> str:
    path = paths.config_home / "theme"
    if not path.exists():
        return "dark"
    value = _bounded_text_evidence(path, "desktop:theme").stdout.strip()
    if value not in {"dark", "light"}:
        msg = "desktop theme must be exactly dark or light"
        raise ShadowStartupError(msg)
    return value


def _desktop_configuration_root(paths: ShadowPaths) -> Path:
    try:
        root = paths.desktop_configuration_root.resolve(strict=True)
    except OSError as error:
        msg = "desktop configuration root cannot be resolved"
        raise ShadowStartupError(msg) from error
    if not root.is_dir():
        msg = "desktop configuration root is not a directory"
        raise ShadowStartupError(msg)
    return root


def build_shadow_composition(
    paths: ShadowPaths,
    environ: Mapping[str, str] | None = None,
) -> ShadowComposition:
    """Build real read-only observation plus isolated persistence and null dispatch."""
    values = os.environ if environ is None else environ
    display_value = values.get("DISPLAY")
    if not display_value:
        msg = "DISPLAY is required for canonical shadow observation"
        raise ShadowStartupError(msg)
    boot_source = ProcBootIdSource()
    boot_id = boot_source.current_boot_id()
    instance = ControllerInstanceId(uuid4())
    display = DisplayIdentity(display_value)
    store = AtomicStateStore(paths.state_home, StateNamespace.SHADOW)
    if store.path != paths.state_file:
        msg = "shadow state store escaped its declared namespace"
        raise ShadowStartupError(msg)
    initial = load_shadow_state(
        store,
        boot_id=boot_id,
        controller_instance=instance,
        display_identity=display,
    )
    clock = AsyncioMonotonicClock()
    bridge = GenerationBridge(initial.event_generation)
    display_bridge = PlanningDisplayBridge()
    planning_source = FilesystemDesktopPlanningInputSource(
        root=_desktop_configuration_root(paths),
        display=display_bridge,
        context=ShadowDesktopContextSource(
            host_name=socket.gethostname().split(".", maxsplit=1)[0],
            theme=_shadow_theme(paths),
        ),
    )
    profiles = StaticSavedProfiles(
        tuple(
            planning_source.complete_profile(profile)
            for profile in prepare_isolated_autorandr_namespace(
                paths.autorandr_profiles,
                paths.autorandr_isolation_root,
            )
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
        autorandr_environment=isolated_autorandr_environment(
            paths.autorandr_isolation_root,
            values,
        ),
    )
    display_bridge.bind(SnapshotDesktopDisplaySource(coordinator))
    audit = RotatingAuditLog(paths.audit_log, initial)
    return compose_shadow_controller(
        paths=paths,
        initial_state=initial,
        adapters=ShadowControllerAdapters(
            store=store,
            observer=AsyncSnapshotObserver(coordinator),
            planning_source=planning_source,
            audit=audit,
            clock=clock,
            generation_bridge=bridge,
        ),
    )


class UeventMonitor(Protocol):
    """A producer which converts only DRM kernel events into wake-up hints."""

    async def run(self, notify: Callable[[], object]) -> None:
        """Run until cancelled, invoking *notify* once per relevant event."""
        ...


class DrmUeventMonitor:
    """Observe kernel DRM uevents directly without a helper process or dependency."""

    async def run(self, notify: Callable[[], object]) -> None:
        """Receive read-only netlink events and enqueue coalescible DRM hints."""
        monitor = socket.socket(
            socket.AF_NETLINK,
            socket.SOCK_DGRAM,
            _NETLINK_KOBJECT_UEVENT,
        )
        try:
            monitor.setblocking(False)  # noqa: FBT003
            monitor.bind((0, _UEVENT_GROUP))
            loop = asyncio.get_running_loop()
            while True:
                payload = await loop.sock_recv(monitor, _MAX_UEVENT_BYTES)
                if is_drm_uevent(payload):
                    notify()
        finally:
            monitor.close()


def is_drm_uevent(payload: bytes) -> bool:
    """Return whether a bounded kernel payload is a relevant DRM wake-up hint."""
    fields = frozenset(payload.split(b"\0"))
    return b"SUBSYSTEM=drm" in fields and any(
        f"ACTION={action}".encode() in fields
        for action in ("add", "bind", "change", "move", "remove", "unbind")
    )


async def run_shadow(
    composition: ShadowComposition,
    monitor: UeventMonitor | None = None,
) -> None:
    """Run one controller and one DRM producer, cancelling both on any exit."""
    producer = DrmUeventMonitor() if monitor is None else monitor
    controller_task = asyncio.create_task(
        composition.controller.run(),
        name="monitor-controller-shadow-consumer",
    )
    monitor_task = asyncio.create_task(
        producer.run(composition.controller.notify_drm_hint),
        name="monitor-controller-shadow-uevents",
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
        msg = f"shadow runtime task exited unexpectedly: {completed.get_name()}"
        raise ShadowStartupError(msg)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await composition.controller.close()
        composition.planner.close()


def _require_shadow_namespace(environ: Mapping[str, str]) -> None:
    if environ.get("MONITOR_CONTROLLER_NAMESPACE") != "shadow":
        msg = "MONITOR_CONTROLLER_NAMESPACE must be exactly 'shadow'"
        raise ShadowStartupError(msg)


def main() -> int:
    """Acquire shadow-only authority and run the null-dispatch composition."""
    try:
        _require_shadow_namespace(os.environ)
        paths = ShadowPaths.from_environment()
        with ShadowAuthorityLock(paths.authority_lock):
            composition = build_shadow_composition(paths)
            asyncio.run(run_shadow(composition))
    except KeyboardInterrupt:
        return 0
    except Exception as error:  # noqa: BLE001 - service composition boundary
        print(f"monitor-controller-shadow: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - fixed-venv module entry point
    raise SystemExit(main())
