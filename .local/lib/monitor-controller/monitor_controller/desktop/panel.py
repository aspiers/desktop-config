# ruff: noqa: EM101, EM102, TRY003
"""Fail-closed XFCE panel geometry and strut proof from X11 evidence."""

from __future__ import annotations

import contextlib
import ctypes
import multiprocessing
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import permutations
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .layout import DisplayScreenSnapshot
    from .plan_codec import PanelIntent

_DOCK_TYPE: Final = "_NET_WM_WINDOW_TYPE_DOCK"
_PANEL_CLASS: Final = ("xfce4-panel", "Xfce4-panel")
_PANEL_COMM: Final = "xfce4-panel"
_IS_VIEWABLE: Final = 2
_XA_ATOM: Final = 4
_XA_CARDINAL: Final = 6
_XA_STRING: Final = 31
_XA_WINDOW: Final = 33
_PROPERTY_LIMIT: Final = 4096
_PANEL_LENGTH: Final = 100
_PANEL_STRUT_VALUES: Final = 12
_PANEL_GEOMETRY_VALUES: Final = 4
_WM_CLASS_VALUES: Final = 2
_FORMAT_8: Final = 8
_MAX_ACTIVE_PANELS: Final = 3
_DEFAULT_SNAPSHOT_TIMEOUT_SECONDS: Final = 2.0
_CHILD_STOP_GRACE_SECONDS: Final = 0.2
type PanelGeometry = tuple[int, int, int, int]
type PanelStrut = tuple[int, ...]


class PanelEvidenceError(RuntimeError):
    """X or process evidence could not prove one coherent panel state."""


class PanelReadinessError(RuntimeError):
    """Panel windows did not reach the required state before the deadline."""

    def __init__(
        self,
        message: str,
        latest_health: PanelHealth | None = None,
    ) -> None:
        """Retain the final sampled evidence for guarded fallback decisions."""
        super().__init__(message)
        self.latest_health = latest_health


@dataclass(frozen=True, slots=True)
class PanelWindowEvidence:
    """One root client with enough evidence to prove or reject panel health."""

    window_id: int
    mapped: bool
    wm_class: tuple[str, str] | None
    window_type: tuple[str, ...] | None
    pid: int | None
    process_comm: str | None
    geometry: PanelGeometry | None
    strut: PanelStrut | None


@dataclass(frozen=True, slots=True)
class PanelSnapshot:
    """One server-consistent snapshot of root-client panel candidates."""

    windows: tuple[PanelWindowEvidence, ...]


@dataclass(frozen=True, slots=True)
class PanelExpectation:
    """Exact output placement, with optional intentionally unconstrained height."""

    x: int
    width: int
    top: int
    bottom: int
    root_height: int
    height: int | None


@dataclass(frozen=True, slots=True)
class PanelHealth:
    """Exact health verdict plus observed PIDs needed by restart readiness."""

    healthy: bool
    reason: str
    observed_pids: tuple[int, ...] = ()
    common_pid: int | None = None


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("window_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


def derive_expected_panel_signatures(  # noqa: PLR0911
    panels: tuple[PanelIntent, ...],
    screens: tuple[DisplayScreenSnapshot, ...],
) -> tuple[PanelExpectation, ...] | None:
    """Derive anonymous output placement constraints from immutable intent."""
    if not screens:
        return None
    root_height = max(screen.y + screen.height for screen in screens)
    primary = tuple(screen for screen in screens if screen.primary)
    if len(primary) != 1:
        return None
    by_output = {screen.output: screen for screen in screens}
    expected: list[PanelExpectation] = []
    claimed_outputs: set[str] = set()
    for panel in panels:
        if panel.output == "none":
            continue
        if panel.position != "p=8;x=0;y=0" or panel.length != _PANEL_LENGTH:
            return None
        screen = (
            primary[0] if panel.output == "Primary" else by_output.get(panel.output)
        )
        if screen is None or screen.output in claimed_outputs:
            return None
        height = None if panel.size is None else panel.size + 1
        bottom = screen.y + screen.height
        if (
            screen.x < 0
            or screen.width <= 0
            or bottom <= 0
            or root_height < bottom
            or (height is not None and (height <= 0 or bottom - height < screen.y))
        ):
            return None
        expected.append(
            PanelExpectation(
                x=screen.x,
                width=screen.width,
                top=screen.y,
                bottom=bottom,
                root_height=root_height,
                height=height,
            )
        )
        claimed_outputs.add(screen.output)
    if not expected or len(expected) > _MAX_ACTIVE_PANELS:
        return None
    return tuple(
        sorted(
            expected,
            key=lambda item: (
                item.x,
                item.width,
                item.top,
                item.bottom,
                item.root_height,
                -1 if item.height is None else item.height,
            ),
        )
    )


def assess_panel_snapshot(  # noqa: C901, PLR0911
    snapshot: PanelSnapshot,
    expected: tuple[PanelExpectation, ...] | None,
    *,
    excluded_pids: tuple[int, ...] = (),
) -> PanelHealth:
    """Match mapped panel clients through one unambiguous one-to-one assignment."""
    if expected is None:
        return _unhealthy("expected panel geometry is unprovable")
    candidates = tuple(
        window
        for window in snapshot.windows
        if window.mapped and window.wm_class == _PANEL_CLASS
    )
    pids = tuple(
        sorted(
            {
                window.pid
                for window in candidates
                if isinstance(window.pid, int)
                and not isinstance(window.pid, bool)
                and window.pid > 0
            }
        )
    )
    if len(candidates) != len(expected):
        return _unhealthy("mapped panel window count differs", pids)
    if len(candidates) > _MAX_ACTIVE_PANELS:
        return _unhealthy("mapped panel window count exceeds proof bound", pids)
    if any(window.window_type != (_DOCK_TYPE,) for window in candidates):
        return _unhealthy("panel window type is missing or malformed", pids)
    if any(
        not isinstance(window.pid, int)
        or isinstance(window.pid, bool)
        or window.pid <= 0
        for window in candidates
    ):
        return _unhealthy("panel PID is missing or malformed", pids)
    if len(pids) != 1:
        return _unhealthy("panel windows have mixed PIDs", pids)
    common_pid = pids[0]
    if common_pid in excluded_pids:
        return _unhealthy("panel replacement still uses an old PID", pids)
    if any(window.process_comm != _PANEL_COMM for window in candidates):
        return _unhealthy("panel process evidence is missing or stale", pids)
    if any(not _valid_geometry(window.geometry) for window in candidates):
        return _unhealthy("panel geometry is missing or malformed", pids)
    if any(not _valid_strut(window.strut) for window in candidates):
        return _unhealthy("panel strut is missing or malformed", pids)

    assignments = tuple(
        assignment
        for assignment in permutations(expected)
        if all(
            _matches_expectation(window, constraint)
            for window, constraint in zip(candidates, assignment, strict=True)
        )
    )
    if not assignments:
        return _unhealthy(
            "panel geometry or strut assignment differs", pids, common_pid
        )
    if len(assignments) != 1:
        return _unhealthy(
            "panel geometry or strut assignment is ambiguous", pids, common_pid
        )
    return PanelHealth(
        healthy=True,
        reason="exact panel evidence matches",
        observed_pids=pids,
        common_pid=common_pid,
    )


def _matches_expectation(
    window: PanelWindowEvidence,
    expected: PanelExpectation,
) -> bool:
    geometry = window.geometry
    strut = window.strut
    if geometry is None or strut is None:
        return False
    x, y, width, height = geometry
    if (
        x != expected.x
        or width != expected.width
        or y < expected.top
        or y + height != expected.bottom
        or (expected.height is not None and height != expected.height)
    ):
        return False
    expected_strut = (
        0,
        0,
        0,
        expected.root_height - y,
        0,
        0,
        0,
        0,
        0,
        0,
        x,
        x + width - 1,
    )
    return strut == expected_strut


def _unhealthy(
    reason: str,
    pids: tuple[int, ...] = (),
    common_pid: int | None = None,
) -> PanelHealth:
    return PanelHealth(
        healthy=False,
        reason=reason,
        observed_pids=pids,
        common_pid=common_pid,
    )


def wait_for_panel_health(  # noqa: PLR0913
    sample: Callable[[], PanelHealth],
    *,
    timeout_seconds: float = 25.0,
    interval_seconds: float = 0.5,
    required_stable_samples: int = 2,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PanelHealth:
    """Require an identical healthy panel verdict for consecutive samples."""
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("panel wait timeout and interval must be positive")
    if required_stable_samples <= 0:
        raise ValueError("panel wait stable-sample count must be positive")
    deadline = monotonic() + timeout_seconds
    previous: PanelHealth | None = None
    stable = 0
    latest = _unhealthy("panel was not sampled")
    while monotonic() < deadline:
        latest = sample()
        if latest.healthy:
            stable = stable + 1 if latest == previous else 1
            if stable >= required_stable_samples:
                return latest
        else:
            stable = 0
        previous = latest
        sleep(interval_seconds)
    raise PanelReadinessError(
        f"panel readiness timed out: {latest.reason}",
        latest,
    )


def _stop_snapshot_child(process: BaseProcess) -> None:
    process.join(_CHILD_STOP_GRACE_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_CHILD_STOP_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_CHILD_STOP_GRACE_SECONDS)


class PanelProbe:
    """Production X11 and procfs panel probe with bounded Xlib isolation."""

    def __init__(
        self,
        *,
        display: str | None = None,
        proc_root: Path = Path("/proc"),
        snapshot_timeout_seconds: float = _DEFAULT_SNAPSHOT_TIMEOUT_SECONDS,
    ) -> None:
        """Bind exact evidence roots and the child snapshot deadline."""
        if snapshot_timeout_seconds <= 0:
            raise ValueError("panel snapshot timeout must be positive")
        self._display = display
        self._proc_root = proc_root
        self._snapshot_timeout_seconds = snapshot_timeout_seconds

    def health(
        self,
        panels: tuple[PanelIntent, ...],
        screens: tuple[DisplayScreenSnapshot, ...],
        *,
        excluded_pids: tuple[int, ...] = (),
    ) -> PanelHealth:
        """Return a fail-closed verdict from a bounded child snapshot."""
        expected = derive_expected_panel_signatures(panels, screens)
        try:
            snapshot = self.sample()
        except PanelEvidenceError as error:
            return _unhealthy(f"panel X evidence failed: {error}")
        return assess_panel_snapshot(
            snapshot,
            expected,
            excluded_pids=excluded_pids,
        )

    def panel_process_pids(self) -> tuple[int, ...]:
        """Enumerate every live xfce4-panel PID directly from procfs."""
        found: set[int] = set()
        try:
            with os.scandir(self._proc_root) as entries:
                for entry in entries:
                    if not entry.name.isascii() or not entry.name.isdigit():
                        continue
                    pid = int(entry.name)
                    if pid <= 0:
                        continue
                    try:
                        comm = Path(entry.path, "comm").read_text(
                            encoding="ascii", errors="strict"
                        )
                    except FileNotFoundError:
                        # Processes may vanish at any point during procfs traversal.
                        continue
                    except (OSError, UnicodeError) as error:
                        raise PanelEvidenceError(
                            f"cannot read process {pid} comm"
                        ) from error
                    if comm.strip() == _PANEL_COMM:
                        found.add(pid)
        except PanelEvidenceError:
            raise
        except OSError as error:
            raise PanelEvidenceError("cannot enumerate procfs process root") from error
        return tuple(sorted(found))

    def sample(self) -> PanelSnapshot:
        """Capture one X snapshot in a bounded forked child process."""
        context = multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=self._sample_child,
            args=(sender,),
            daemon=True,
        )
        started = False
        try:
            process.start()
            started = True
            sender.close()
            ready = wait(
                (receiver, process.sentinel),
                timeout=self._snapshot_timeout_seconds,
            )
            if receiver not in ready:
                if process.sentinel in ready:
                    raise PanelEvidenceError(
                        f"panel X snapshot child crashed with exit status "
                        f"{process.exitcode}"
                    )
                raise PanelEvidenceError(
                    f"panel X snapshot timed out after "
                    f"{self._snapshot_timeout_seconds:g} seconds"
                )
            try:
                succeeded, payload = receiver.recv()
            except (EOFError, OSError) as error:
                raise PanelEvidenceError(
                    "panel X snapshot child closed without evidence"
                ) from error
            if not succeeded:
                raise PanelEvidenceError(f"panel X snapshot child failed: {payload}")
            if not isinstance(payload, PanelSnapshot):
                raise PanelEvidenceError(
                    "panel X snapshot child returned malformed data"
                )
            return payload  # noqa: TRY300
        except (OSError, RuntimeError) as error:
            if isinstance(error, PanelEvidenceError):
                raise
            raise PanelEvidenceError("cannot start panel X snapshot child") from error
        finally:
            sender.close()
            receiver.close()
            if started:
                _stop_snapshot_child(process)

    def _sample_child(self, sender: Connection) -> None:
        """Run unsafe Xlib sampling where a fatal X error cannot kill the parent."""
        try:
            sender.send((True, self._sample_direct()))
        except Exception as error:  # noqa: BLE001 - report any child probe failure
            with contextlib.suppress(OSError, BrokenPipeError):
                sender.send((False, f"{type(error).__name__}: {error}"))
        finally:
            sender.close()

    def _sample_direct(self) -> PanelSnapshot:
        """Capture root clients under an X server grab inside the child only."""
        x11 = _load_x11()
        selected = (
            self._display
            if self._display is not None
            else os.environ.get("DISPLAY", ":0")
        )
        display = x11.XOpenDisplay(selected.encode())
        if not display:
            raise PanelEvidenceError(f"cannot open X display {selected!r}")
        try:
            root = int(x11.XDefaultRootWindow(display))
            x11.XGrabServer(display)
            try:
                before = _root_clients(x11, display, root)
                windows = tuple(
                    self._window_evidence(x11, display, root, window)
                    for window in before
                )
                after = _root_clients(x11, display, root)
                if before != after:
                    raise PanelEvidenceError("root client list changed during snapshot")
            finally:
                x11.XUngrabServer(display)
                x11.XSync(display, 0)
        finally:
            x11.XCloseDisplay(display)
        return PanelSnapshot(windows)

    def _window_evidence(
        self,
        x11: ctypes.CDLL,
        display: int,
        root: int,
        window: int,
    ) -> PanelWindowEvidence:
        attributes = _XWindowAttributes()
        if not x11.XGetWindowAttributes(display, window, ctypes.byref(attributes)):
            raise PanelEvidenceError("root client disappeared during snapshot")
        mapped = attributes.map_state == _IS_VIEWABLE
        wm_class = _wm_class(x11, display, window)
        if not mapped or wm_class != _PANEL_CLASS:
            return PanelWindowEvidence(
                window, mapped, wm_class, None, None, None, None, None
            )
        translated_x = ctypes.c_int()
        translated_y = ctypes.c_int()
        child = ctypes.c_ulong()
        if not x11.XTranslateCoordinates(
            display,
            window,
            root,
            0,
            0,
            ctypes.byref(translated_x),
            ctypes.byref(translated_y),
            ctypes.byref(child),
        ):
            geometry = None
        else:
            geometry = (
                translated_x.value,
                translated_y.value,
                attributes.width,
                attributes.height,
            )
        raw_types = _property32(x11, display, window, "_NET_WM_WINDOW_TYPE", _XA_ATOM)
        dock = _atom(x11, display, "_NET_WM_WINDOW_TYPE_DOCK", only_if_exists=True)
        window_type = (
            None
            if raw_types is None
            else tuple(
                _DOCK_TYPE if value == dock else f"atom:{value}" for value in raw_types
            )
        )
        raw_pid = _property32(x11, display, window, "_NET_WM_PID", _XA_CARDINAL)
        pid = raw_pid[0] if raw_pid is not None and len(raw_pid) == 1 else None
        process_comm: str | None = None
        if pid is not None and pid > 0:
            try:
                process_comm = (
                    (self._proc_root / str(pid) / "comm")
                    .read_text(encoding="ascii", errors="strict")
                    .strip()
                )
            except (OSError, UnicodeError):
                process_comm = None
        raw_strut = _property32(
            x11, display, window, "_NET_WM_STRUT_PARTIAL", _XA_CARDINAL
        )
        strut = (
            raw_strut
            if raw_strut is not None and len(raw_strut) == _PANEL_STRUT_VALUES
            else None
        )
        return PanelWindowEvidence(
            window,
            mapped,
            wm_class,
            window_type,
            pid,
            process_comm,
            geometry,
            strut,
        )


def _valid_geometry(value: PanelGeometry | None) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == _PANEL_GEOMETRY_VALUES
        and all(not isinstance(item, bool) for item in value)
        and value[2] > 0
        and value[3] > 0
    )


def _valid_strut(value: PanelStrut | None) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == _PANEL_STRUT_VALUES
        and all(not isinstance(item, bool) and item >= 0 for item in value)
    )


def _load_x11() -> ctypes.CDLL:
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    x11.XGetWindowProperty.restype = ctypes.c_int
    x11.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_XWindowAttributes),
    ]
    x11.XGetWindowAttributes.restype = ctypes.c_int
    x11.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    x11.XTranslateCoordinates.restype = ctypes.c_int
    x11.XGrabServer.argtypes = [ctypes.c_void_p]
    x11.XUngrabServer.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XFree.restype = ctypes.c_int
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    return x11


def _atom(
    x11: ctypes.CDLL,
    display: int,
    name: str,
    *,
    only_if_exists: bool,
) -> int:
    try:
        return int(x11.XInternAtom(display, name.encode(), int(only_if_exists)))
    except (TypeError, UnicodeError, ValueError, OverflowError) as error:
        raise PanelEvidenceError(f"cannot intern X atom {name!r}") from error


def _root_clients(x11: ctypes.CDLL, display: int, root: int) -> tuple[int, ...]:
    values = _property32(x11, display, root, "_NET_CLIENT_LIST", _XA_WINDOW)
    if values is None or len(values) != len(set(values)):
        raise PanelEvidenceError("root client list is missing or malformed")
    return values


def _wm_class(
    x11: ctypes.CDLL,
    display: int,
    window: int,
) -> tuple[str, str] | None:
    raw = _property8(x11, display, window, "WM_CLASS", _XA_STRING)
    if raw is None or not raw.endswith(b"\0"):
        return None
    fields = raw[:-1].split(b"\0")
    if len(fields) != _WM_CLASS_VALUES:
        return None
    try:
        values = tuple(item.decode("utf-8", errors="strict") for item in fields)
    except UnicodeDecodeError:
        return None
    if len(values) != _WM_CLASS_VALUES:
        return None
    return values[0], values[1]


def _property8(
    x11: ctypes.CDLL,
    display: int,
    window: int,
    name: str,
    expected_type: int,
) -> bytes | None:
    value = _property(x11, display, window, name, expected_type, expected_format=8)
    return None if value is None else bytes(value)


def _property32(
    x11: ctypes.CDLL,
    display: int,
    window: int,
    name: str,
    expected_type: int,
) -> tuple[int, ...] | None:
    value = _property(x11, display, window, name, expected_type, expected_format=32)
    return None if value is None else tuple(value)


def _property(  # noqa: PLR0913
    x11: ctypes.CDLL,
    display: int,
    window: int,
    name: str,
    expected_type: int,
    *,
    expected_format: int,
) -> bytes | tuple[int, ...] | None:
    property_atom = _atom(x11, display, name, only_if_exists=True)
    if not property_atom:
        return None
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    count = ctypes.c_ulong()
    remaining = ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    status = x11.XGetWindowProperty(
        display,
        window,
        property_atom,
        0,
        _PROPERTY_LIMIT,
        0,
        expected_type,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(count),
        ctypes.byref(remaining),
        ctypes.byref(data),
    )
    if status != 0:
        raise PanelEvidenceError(f"cannot read X property {name}")
    try:
        if (
            actual_type.value != expected_type
            or actual_format.value != expected_format
            or remaining.value != 0
            or count.value > _PROPERTY_LIMIT
            or not data
        ):
            return None
        if expected_format == _FORMAT_8:
            return ctypes.string_at(data, count.value)
        values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
        return tuple(int(values[index]) for index in range(count.value))
    finally:
        if data:
            x11.XFree(data)
