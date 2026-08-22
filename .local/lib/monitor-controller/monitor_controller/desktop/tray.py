# ruff: noqa: EM101, EM102, TRY003
"""System-tray readiness from its X selection and live panel wrapper process."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_TRAY_SELECTION: Final = b"_NET_SYSTEM_TRAY_S0"
_SYSTRAY_MARKERS: Final = (b"xfce4/panel/wrapper-2.0", b"libsystray")
_PANEL_COMM: Final = "xfce4-panel"


class TrayReadinessError(RuntimeError):
    """The tray could not be proved stable before its bounded deadline."""


@dataclass(frozen=True, slots=True)
class TrayState:
    """Both signals required before an applet may safely dock."""

    selection_owner: int | None
    wrapper_pids: tuple[int, ...]

    @property
    def ready(self) -> bool:
        """Return whether both an owner and current-panel wrapper exist."""
        return self.selection_owner is not None and bool(self.wrapper_pids)


@dataclass(frozen=True, slots=True)
class StableTray:
    """The ready state and number of consecutive unchanged comparisons."""

    state: TrayState
    stable_samples: int


class TrayProbe:
    """Production read-only tray probe with injectable filesystem/display roots."""

    def __init__(
        self,
        *,
        display: str | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        """Bind an optional exact display and procfs root."""
        self._display = display
        self._proc_root = proc_root

    def sample(self) -> TrayState:
        """Sample the selection owner and wrappers parented by a live panel."""
        return TrayState(
            selection_owner=tray_selection_owner(self._display),
            wrapper_pids=systray_wrapper_pids(self._proc_root),
        )


def tray_selection_owner(display: str | None = None) -> int | None:
    """Return the X system-tray selection owner without querying root properties."""
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XGetSelectionOwner.restype = ctypes.c_ulong
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    selected = display if display is not None else os.environ.get("DISPLAY", ":0")
    connection = x11.XOpenDisplay(selected.encode())
    if not connection:
        raise TrayReadinessError(f"cannot open X display {selected!r}")
    try:
        atom = x11.XInternAtom(connection, _TRAY_SELECTION, 1)
        owner = x11.XGetSelectionOwner(connection, atom) if atom else 0
    finally:
        x11.XCloseDisplay(connection)
    if not owner:
        return None
    try:
        return int(owner)
    except (TypeError, ValueError, OverflowError) as error:
        raise TrayReadinessError("X returned an invalid tray owner") from error


def systray_wrapper_pids(proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    """Return systray wrappers whose immediate parent is a live XFCE panel."""
    wrappers: list[int] = []
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as error:
        message = f"cannot enumerate process evidence: {error}"
        raise TrayReadinessError(message) from error
    for entry in entries:
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        try:
            command = (entry / "cmdline").read_bytes()
            if not all(marker in command for marker in _SYSTRAY_MARKERS):
                continue
            parent = _parent_pid(entry / "status")
            parent_comm = (
                (proc_root / str(parent) / "comm")
                .read_text(
                    encoding="utf-8",
                    errors="strict",
                )
                .strip()
            )
        except (OSError, UnicodeError, ValueError):
            # Processes may disappear between any two procfs reads. Such a
            # wrapper is not stable evidence and is deliberately ignored.
            continue
        if parent > 0 and parent_comm == _PANEL_COMM:
            wrappers.append(pid)
    return tuple(sorted(set(wrappers)))


def wait_for_stable_tray(  # noqa: PLR0913
    sample: Callable[[], TrayState],
    *,
    timeout_seconds: float = 25.0,
    interval_seconds: float = 0.5,
    required_stable_samples: int = 6,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StableTray:
    """Require both tray signals to remain identical for consecutive samples."""
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("tray wait timeout and interval must be positive")
    if required_stable_samples <= 0:
        raise ValueError("tray wait stable-sample count must be positive")
    deadline = monotonic() + timeout_seconds
    previous: TrayState | None = None
    stable = 0
    latest = TrayState(None, ())
    while monotonic() < deadline:
        latest = sample()
        if latest.ready:
            stable = stable + 1 if latest == previous else 0
            if stable >= required_stable_samples:
                return StableTray(latest, stable)
        else:
            stable = 0
        previous = latest
        sleep(interval_seconds)
    owner = "none" if latest.selection_owner is None else hex(latest.selection_owner)
    wrappers = ",".join(str(item) for item in latest.wrapper_pids) or "none"
    raise TrayReadinessError(
        f"system tray did not stabilize: owner={owner} wrappers={wrappers}"
    )


def _parent_pid(status: Path) -> int:
    for line in status.read_text(encoding="ascii", errors="strict").splitlines():
        if line.startswith("PPid:"):
            value = line.split(":", maxsplit=1)[1].strip()
            try:
                return int(value)
            except ValueError as error:
                raise ValueError("process status has invalid parent PID") from error
    raise ValueError("process status has no parent PID")
