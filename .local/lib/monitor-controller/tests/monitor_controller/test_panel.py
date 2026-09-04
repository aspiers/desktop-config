"""Exact XFCE panel geometry, strut, process, and X11 evidence checks."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

import monitor_controller.desktop.panel as panel_module
from monitor_controller.desktop.layout import DisplayScreenSnapshot
from monitor_controller.desktop.panel import (
    PanelEvidenceError,
    PanelExpectation,
    PanelProbe,
    PanelSnapshot,
    PanelWindowEvidence,
    assess_panel_snapshot,
    derive_expected_panel_signatures,
    journal_panel_diagnostic,
)
from monitor_controller.desktop.plan_codec import PanelIntent
from monitor_controller.model import ConfigurationContentHash

_HASH = ConfigurationContentHash("bin/setup-panels", f"sha256:{'0' * 64}")
_STRUT_EXTERNAL = (0, 0, 0, 37, 0, 0, 0, 0, 0, 0, 2880, 7999)
_STRUT_INTERNAL = (0, 0, 0, 277, 0, 0, 0, 0, 0, 0, 0, 2879)


def _screens() -> tuple[DisplayScreenSnapshot, ...]:
    return (
        DisplayScreenSnapshot("eDP", 2880, 1920, 0, 0, 286, 191, primary=False),
        DisplayScreenSnapshot(
            "DisplayPort-9", 5120, 2160, 2880, 0, 930, 400, primary=True
        ),
    )


def _panels(*, primary_size: int | None = 36) -> tuple[PanelIntent, ...]:
    return (
        PanelIntent(1, "Primary", "p=8;x=0;y=0", 100, primary_size, (_HASH,)),
        PanelIntent(2, "eDP", "p=8;x=0;y=0", 100, 36, (_HASH,)),
        PanelIntent(3, "none", "p=8;x=0;y=0", 100, None, (_HASH,)),
    )


def _window(  # noqa: PLR0913
    window_id: int,
    geometry: tuple[int, int, int, int],
    strut: tuple[int, ...] | None,
    *,
    pid: int = 2394373,
    mapped: bool = True,
    wm_class: tuple[str, str] | None = ("xfce4-panel", "Xfce4-panel"),
    window_type: tuple[str, ...] | None = ("_NET_WM_WINDOW_TYPE_DOCK",),
    process_comm: str | None = "xfce4-panel",
) -> PanelWindowEvidence:
    return PanelWindowEvidence(
        window_id=window_id,
        mapped=mapped,
        wm_class=wm_class,
        window_type=window_type,
        pid=pid,
        process_comm=process_comm,
        geometry=geometry,
        strut=strut,
    )


def _healthy_windows() -> tuple[PanelWindowEvidence, ...]:
    return (
        _window(0x2300003, (2880, 2123, 5120, 37), _STRUT_EXTERNAL),
        _window(0x230000E, (0, 1883, 2880, 37), _STRUT_INTERNAL),
    )


def test_samsung_expected_panel_signatures_are_exact() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())

    assert expected == (
        PanelExpectation(0, 2880, 0, 1920, 2160, 37),
        PanelExpectation(2880, 5120, 0, 2160, 2160, 37),
    )


def test_anonymous_panel_windows_match_as_an_exact_multiset() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())

    health = assess_panel_snapshot(
        PanelSnapshot(tuple(reversed(_healthy_windows()))), expected
    )

    assert health.healthy
    assert health.common_pid == 2394373


@pytest.mark.parametrize(
    ("windows", "reason"),
    [
        (
            (
                _window(1, (0, 0, 5120, 37), _STRUT_EXTERNAL),
                _window(2, (0, 0, 2880, 37), _STRUT_INTERNAL),
            ),
            "geometry",
        ),
        ((_healthy_windows()[0],), "count"),
        ((*_healthy_windows(), _healthy_windows()[0]), "count"),
        (
            (
                _healthy_windows()[0],
                _window(2, (0, 1883, 2880, 37), _STRUT_INTERNAL, pid=551475),
            ),
            "PID",
        ),
        (
            (
                _window(1, (2880, 2123, 5120, 37), None),
                _healthy_windows()[1],
            ),
            "strut",
        ),
        (
            (
                _window(1, (2880, 2123, 5120, 37), (*_STRUT_EXTERNAL[:-1], 8000)),
                _healthy_windows()[1],
            ),
            "strut",
        ),
        (
            (
                _window(
                    1,
                    (2880, 2123, 5120, 37),
                    _STRUT_EXTERNAL,
                    window_type=None,
                ),
                _healthy_windows()[1],
            ),
            "type",
        ),
        (
            (
                _window(
                    1,
                    (2880, 2123, 5120, 37),
                    _STRUT_EXTERNAL,
                    process_comm=None,
                ),
                _healthy_windows()[1],
            ),
            "process",
        ),
    ],
)
def test_panel_health_fails_closed_on_bad_or_malformed_evidence(
    windows: tuple[PanelWindowEvidence, ...], reason: str
) -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())

    health = assess_panel_snapshot(PanelSnapshot(windows), expected)

    assert not health.healthy
    assert reason.lower() in health.reason.lower()


def test_geometry_mismatch_records_bounded_expected_and_observed_evidence() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())
    unrelated = _window(
        0xDEADBEEF,
        (123, 456, 789, 101),
        None,
        wm_class=("private-app", "Private-app"),
        window_type=None,
        process_comm="private-browser-process",
    )
    misplaced = (
        _window(0x2300003, (0, 0, 5120, 37), _STRUT_EXTERNAL),
        unrelated,
        _healthy_windows()[1],
    )

    health = assess_panel_snapshot(PanelSnapshot(misplaced), expected)
    diagnostic = json.loads(health.diagnostic)

    assert not health.healthy
    assert diagnostic["expected"] == [
        {
            "bottom": 1920,
            "height": 37,
            "root_height": 2160,
            "top": 0,
            "width": 2880,
            "x": 0,
        },
        {
            "bottom": 2160,
            "height": 37,
            "root_height": 2160,
            "top": 0,
            "width": 5120,
            "x": 2880,
        },
    ]
    assert diagnostic["observed_count"] == 2
    assert diagnostic["truncated"] is False
    assert "private-app" not in health.diagnostic
    assert "private-browser-process" not in health.diagnostic
    assert "deadbeef" not in health.diagnostic
    assert diagnostic["observed"][0] == {
        "geometry": [0, 0, 5120, 37],
        "id": "0x2300003",
        "mapped": True,
        "pid": 2394373,
        "process": "xfce4-panel",
        "strut": list(_STRUT_EXTERNAL),
        "type": ["_NET_WM_WINDOW_TYPE_DOCK"],
    }


def test_mismatch_diagnostic_is_bounded() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())
    windows = tuple(
        _window(
            index,
            (0, 0, 100, 10),
            _STRUT_INTERNAL,
            process_comm="p" * 10_000,
            window_type=tuple("type-" + "x" * 10_000 for _ in range(10)),
        )
        for index in range(10)
    )

    health = assess_panel_snapshot(PanelSnapshot(windows), expected)
    diagnostic = json.loads(health.diagnostic)

    assert diagnostic["observed_count"] == 10
    assert len(diagnostic["observed"]) == 6
    assert diagnostic["truncated"] is True
    assert "p" * 65 not in health.diagnostic
    assert "x" * 65 not in health.diagnostic
    assert len(health.diagnostic.encode()) <= 4_096


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [
        ("", "unavailable"),
        ('{"value":NaN}', "discarded-invalid-or-over-limit"),
        ('["not-an-object"]', "discarded-invalid-or-over-limit"),
        ('{"value":"' + "x" * 5_000 + '"}', "discarded-invalid-or-over-limit"),
    ],
)
def test_journal_diagnostic_rejects_invalid_or_oversized_values(
    value: str,
    expected_status: str,
) -> None:
    diagnostic = journal_panel_diagnostic(value)

    assert "\n" not in diagnostic
    assert len(diagnostic.encode()) <= 4_096
    assert json.loads(diagnostic)["diagnostic"] == expected_status


def test_journal_diagnostic_normalizes_valid_multiline_json() -> None:
    diagnostic = journal_panel_diagnostic('{\n  "observed": "exact"\n}')

    assert diagnostic == '{"observed":"exact"}'


def test_replacement_proof_requires_exact_geometry_from_a_new_pid() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())
    replacement_windows = tuple(
        replace(window, pid=551475) for window in _healthy_windows()
    )
    zero_zero_windows = (
        _window(1, (0, 0, 5120, 37), _STRUT_EXTERNAL, pid=551475),
        _window(2, (0, 0, 2880, 37), _STRUT_INTERNAL, pid=551475),
    )

    replacement = assess_panel_snapshot(
        PanelSnapshot(replacement_windows),
        expected,
        excluded_pids=(2394373,),
    )
    old_process = assess_panel_snapshot(
        PanelSnapshot(replacement_windows),
        expected,
        excluded_pids=(551475,),
    )
    wrong_geometry = assess_panel_snapshot(
        PanelSnapshot(zero_zero_windows),
        expected,
        excluded_pids=(2394373,),
    )

    assert replacement.healthy
    assert not old_process.healthy
    assert "old PID" in old_process.reason
    assert not wrong_geometry.healthy
    assert "geometry" in wrong_geometry.reason


def test_probe_health_fails_closed_when_libx11_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_x11() -> ctypes.CDLL:
        message = "injected missing libX11"
        raise OSError(message)

    monkeypatch.setattr(panel_module, "_load_x11", missing_x11)

    health = PanelProbe().health(_panels(), _screens())

    assert not health.healthy
    assert "missing libX11" in health.reason


def test_hidden_unmapped_panel_leader_is_ignored() -> None:
    expected = derive_expected_panel_signatures(_panels(), _screens())
    leader = _window(
        0x2300001,
        (10, 10, 10, 10),
        None,
        mapped=False,
        window_type=None,
    )

    assert assess_panel_snapshot(
        PanelSnapshot((*_healthy_windows(), leader)), expected
    ).healthy


def test_active_panel_with_unconstrained_size_matches_exact_bottom_placement() -> None:
    expected = derive_expected_panel_signatures(_panels(primary_size=None), _screens())
    external = _window(
        1,
        (2880, 2111, 5120, 49),
        (0, 0, 0, 49, 0, 0, 0, 0, 0, 0, 2880, 7999),
    )

    health = assess_panel_snapshot(
        PanelSnapshot((external, _healthy_windows()[1])), expected
    )

    assert health.healthy


@pytest.mark.parametrize(
    "external",
    [
        _window(
            1,
            (2880, 2110, 5120, 49),
            (0, 0, 0, 50, 0, 0, 0, 0, 0, 0, 2880, 7999),
        ),
        _window(
            1,
            (2880, 2111, 5120, 49),
            (0, 0, 0, 49, 0, 0, 0, 0, 0, 0, 2880, 7998),
        ),
        _window(
            1,
            (2880, -1, 5120, 2161),
            (0, 0, 0, 2161, 0, 0, 0, 0, 0, 0, 2880, 7999),
        ),
    ],
)
def test_unconstrained_size_rejects_displacement_or_wrong_strut(
    external: PanelWindowEvidence,
) -> None:
    expected = derive_expected_panel_signatures(_panels(primary_size=None), _screens())

    health = assess_panel_snapshot(
        PanelSnapshot((external, _healthy_windows()[1])), expected
    )

    assert not health.healthy
    assert "geometry or strut" in health.reason


def test_panel_process_enumeration_ignores_vanished_entries_and_fails_on_root(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.joinpath("41").mkdir(parents=True)
    proc_root.joinpath("41", "comm").write_text("xfce4-panel\n", encoding="ascii")
    proc_root.joinpath("42").mkdir()

    assert PanelProbe(proc_root=proc_root).panel_process_pids() == (41,)
    with pytest.raises(PanelEvidenceError, match="enumerate procfs"):
        PanelProbe(proc_root=tmp_path / "missing").panel_process_pids()


def test_bounded_child_turns_hung_sample_into_unhealthy_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(_probe: PanelProbe) -> PanelSnapshot:
        time.sleep(10)
        return PanelSnapshot(())

    monkeypatch.setattr(PanelProbe, "_sample_direct", hang)
    started = time.monotonic()

    health = PanelProbe(snapshot_timeout_seconds=0.05).health(_panels(), _screens())

    assert time.monotonic() - started < 1
    assert not health.healthy
    assert "timed out" in health.reason


@contextmanager
def _xvfb(tmp_path: Path) -> Generator[str]:
    executable = shutil.which("Xvfb")
    if executable is None:
        pytest.skip("Xvfb is unavailable")
    display = f":{200 + os.getpid() % 300}"
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir(mode=0o700)
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    process = subprocess.Popen(  # noqa: S603
        (executable, display, "-screen", "0", "8000x2160x24", "-nolisten", "tcp"),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    connection = None
    try:
        for _attempt in range(100):
            connection = x11.XOpenDisplay(display.encode())
            if connection:
                break
            if process.poll() is not None:
                pytest.fail("Xvfb exited before accepting connections")
            time.sleep(0.01)
        if not connection:
            pytest.fail("Xvfb did not accept connections")
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay(connection)
        yield display
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _synthetic_panels(
    display: str,
    pid: int,
    *,
    ordinary_client_count: int = 0,
    stale_client: bool = False,
) -> tuple[ctypes.CDLL, ctypes.c_void_p, tuple[int, int], tuple[int, int]]:
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XCreateSimpleWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    x11.XCreateSimpleWindow.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XChangeProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_int,
    ]
    x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XMoveWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
    ]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    connection = ctypes.c_void_p(x11.XOpenDisplay(display.encode()))
    assert connection.value
    root = x11.XDefaultRootWindow(connection)
    frames = (
        int(x11.XCreateSimpleWindow(connection, root, 2880, 2123, 5120, 37, 0, 0, 0)),
        int(x11.XCreateSimpleWindow(connection, root, 0, 1883, 2880, 37, 0, 0, 0)),
    )
    windows = (
        int(x11.XCreateSimpleWindow(connection, frames[0], 0, 0, 5120, 37, 0, 0, 0)),
        int(x11.XCreateSimpleWindow(connection, frames[1], 0, 0, 2880, 37, 0, 0, 0)),
    )

    def atom(name: str) -> int:
        return int(x11.XInternAtom(connection, name.encode(), 0))

    def property8(window: int, name: str, property_type: int, value: bytes) -> None:
        data = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        x11.XChangeProperty(
            connection, window, atom(name), property_type, 8, 0, data, len(value)
        )

    def property32(
        window: int, name: str, property_type: int, values: tuple[int, ...]
    ) -> None:
        data = (ctypes.c_ulong * len(values))(*values)
        x11.XChangeProperty(
            connection,
            window,
            atom(name),
            property_type,
            32,
            0,
            ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte)),
            len(values),
        )

    ordinary_clients = tuple(
        int(x11.XCreateSimpleWindow(connection, root, 0, 0, 1, 1, 0, 0, 0))
        for _index in range(ordinary_client_count)
    )
    stale_clients: tuple[int, ...] = ()
    if stale_client:
        stale = int(x11.XCreateSimpleWindow(connection, root, 0, 0, 1, 1, 0, 0, 0))
        x11.XDestroyWindow(connection, stale)
        x11.XSync(connection, 0)
        stale_clients = (stale,)
    for frame, window, strut in zip(
        frames,
        windows,
        (_STRUT_EXTERNAL, _STRUT_INTERNAL),
        strict=True,
    ):
        property8(window, "WM_CLASS", 31, b"xfce4-panel\0Xfce4-panel\0")
        property32(
            window,
            "_NET_WM_WINDOW_TYPE",
            4,
            (atom("_NET_WM_WINDOW_TYPE_DOCK"),),
        )
        property32(window, "_NET_WM_PID", 6, (pid,))
        property32(window, "_NET_WM_STRUT_PARTIAL", 6, strut)
        x11.XMapWindow(connection, window)
        x11.XMapWindow(connection, frame)
    for window in ordinary_clients:
        x11.XMapWindow(connection, window)
    property32(
        root,
        "_NET_CLIENT_LIST",
        33,
        (*windows, *ordinary_clients, *stale_clients),
    )
    x11.XSync(connection, 0)
    return x11, connection, frames, windows


def test_xvfb_probe_handles_more_than_64_root_clients(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    pid = os.getpid()
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    process.joinpath("comm").write_text("xfce4-panel\n", encoding="ascii")

    with _xvfb(tmp_path) as display:
        x11, connection, _frames, _windows = _synthetic_panels(
            display,
            pid,
            ordinary_client_count=65,
        )
        try:
            assert (
                PanelProbe(display=display, proc_root=proc_root)
                .health(
                    _panels(),
                    _screens(),
                )
                .healthy
            )
        finally:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(connection)


def test_xvfb_stale_client_is_unhealthy_without_killing_parent(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    pid = os.getpid()
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    process.joinpath("comm").write_text("xfce4-panel\n", encoding="ascii")

    with _xvfb(tmp_path) as display:
        x11, connection, _frames, _windows = _synthetic_panels(
            display, pid, stale_client=True
        )
        try:
            health = PanelProbe(display=display, proc_root=proc_root).health(
                _panels(), _screens()
            )
            assert not health.healthy
            assert "snapshot child" in health.reason
        finally:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(connection)


def test_xvfb_probe_accepts_exact_windows_and_rejects_zero_zero_geometry(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    pid = os.getpid()
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    process.joinpath("comm").write_text("xfce4-panel\n", encoding="ascii")

    with _xvfb(tmp_path) as display:
        x11, connection, frames, windows = _synthetic_panels(display, pid)
        try:
            probe = PanelProbe(display=display, proc_root=proc_root)
            snapshot = probe.sample()
            assert tuple(window.window_id for window in snapshot.windows) == windows
            assert probe.health(_panels(), _screens()).healthy

            for frame in frames:
                x11.XMoveWindow(connection, frame, 0, 0)
            x11.XSync(connection, 0)

            health = probe.health(_panels(), _screens())
            assert not health.healthy
            assert "geometry" in health.reason
        finally:
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay(connection)
