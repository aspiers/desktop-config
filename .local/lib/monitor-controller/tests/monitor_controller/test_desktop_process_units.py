"""Tray readiness and harmless persistent desktop process ownership contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from monitor_controller.desktop.tray import (
    TrayState,
    systray_wrapper_pids,
    wait_for_stable_tray,
)

_REPOSITORY = Path(__file__).parents[5]
_UNITS = _REPOSITORY / ".config" / "systemd" / "user"


def test_tray_readiness_requires_unchanged_selection_and_live_wrapper() -> None:
    states = iter(
        (
            TrayState(None, ()),
            TrayState(0x10, (1,)),
            TrayState(0x11, (2,)),
            TrayState(0x11, (2,)),
            TrayState(0x11, (2,)),
            TrayState(0x11, (2,)),
        )
    )
    now = [0.0]

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    stable = wait_for_stable_tray(
        lambda: next(states),
        timeout_seconds=5,
        interval_seconds=0.1,
        required_stable_samples=3,
        monotonic=monotonic,
        sleep=sleep,
    )

    assert stable.state == TrayState(0x11, (2,))
    assert stable.stable_samples == 3


def test_systray_wrappers_must_be_parented_by_a_live_panel(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    panel = proc / "100"
    panel.mkdir(parents=True)
    panel.joinpath("comm").write_text("xfce4-panel\n", encoding="utf-8")
    current = proc / "200"
    current.mkdir()
    current.joinpath("cmdline").write_bytes(
        b"/usr/lib/xfce4/panel/wrapper-2.0\0/usr/lib/xfce4/panel/plugins/libsystray.so"
    )
    current.joinpath("status").write_text(
        "Name:\twrapper\nPPid:\t100\n", encoding="ascii"
    )
    stale_parent = proc / "1"
    stale_parent.mkdir()
    stale_parent.joinpath("comm").write_text("systemd\n", encoding="utf-8")
    stale = proc / "300"
    stale.mkdir()
    stale.joinpath("cmdline").write_bytes(
        b"/usr/lib/xfce4/panel/wrapper-2.0\0/usr/lib/xfce4/panel/plugins/libsystray.so"
    )
    stale.joinpath("status").write_text("Name:\twrapper\nPPid:\t1\n", encoding="ascii")

    assert systray_wrapper_pids(proc) == (200,)


def test_tray_source_uses_selection_not_root_property() -> None:
    source = (
        _REPOSITORY / ".local/lib/monitor-controller/monitor_controller/desktop/tray.py"
    ).read_text(encoding="utf-8")

    assert "XGetSelectionOwner" in source
    assert "_NET_SYSTEM_TRAY_S0" in source
    assert "xprop" not in source
    assert "root property" not in source.casefold()


def test_ly_exact_payload_mode_never_discovers_layout(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "arguments"
    desktop_layout = fake_bin / "desktop-layout"
    desktop_layout.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$LY_TEST_LOG"\n',
        encoding="utf-8",
    )
    desktop_layout.chmod(0o700)
    payload = tmp_path / "actions.json"
    payload.write_text("[]", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "LY_TEST_LOG": str(log),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ZDOTDIR": str(tmp_path / "missing-zdotdir"),
        }
    )

    completed = subprocess.run(  # noqa: S603
        (str(_REPOSITORY / "bin" / "ly"), "--resolved-actions", str(payload)),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "--resolved-actions",
        str(payload),
    ]


def test_persistent_process_units_and_finalizer_have_separate_ownership() -> None:
    finalizer = (_UNITS / "monitor-finalize@.service").read_text(encoding="utf-8")
    applet = (_UNITS / "nm-applet.service").read_text(encoding="utf-8")
    diagnostics = (_UNITS / "monitor-tray-diagnostics@.service").read_text(
        encoding="utf-8"
    )
    panel = (_UNITS / "monitor-panel-restart@.service").read_text(encoding="utf-8")
    restart = (_REPOSITORY / "bin" / "fluxbox-restart").read_text(encoding="utf-8")

    assert "KillMode=mixed" in finalizer
    assert "TimeoutStopSec=130s" in finalizer
    assert "setup-monitor" not in finalizer
    assert "Type=exec" in applet
    assert "ExecStart=/usr/bin/nm-applet" in applet
    assert "PartOf=fluxbox-session.target" in applet
    assert "KillMode=control-group" in applet
    assert "Type=oneshot" in diagnostics
    assert "TimeoutStartSec=45s" in diagnostics
    assert "internal tray-diagnostics" in diagnostics
    assert "ExecStart=/usr/bin/xfce4-panel -r" in panel
    assert "KillMode=control-group" in panel
    assert "TimeoutStartSec=30s" in panel
    assert "systemd-run --user" in restart
    assert "-p KillMode=process" in restart


@pytest.fixture
def harmless_process_units() -> Iterator[tuple[str, str]]:
    systemctl = shutil.which("systemctl")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if systemctl is None or runtime is None:
        pytest.skip("systemd user manager is unavailable")
    available = subprocess.run(  # noqa: S603
        (systemctl, "--user", "show-environment"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if available.returncode != 0:
        pytest.skip("systemd user manager is unavailable")
    suffix = uuid4().hex
    child = f"monitor-harmless-child-{suffix}.service"
    launcher = f"monitor-harmless-finalizer-{suffix}.service"
    unit_root = Path(runtime) / "systemd" / "user"
    unit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    child_path = unit_root / child
    launcher_path = unit_root / launcher
    child_path.write_text(
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/usr/bin/sleep 30\n"
        "KillMode=control-group\n"
        "TimeoutStopSec=2s\n",
        encoding="utf-8",
    )
    launcher_path.write_text(
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={systemctl} --user start {child}\n"
        "KillMode=mixed\n"
        "TimeoutStartSec=5s\n"
        "TimeoutStopSec=2s\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603
        (systemctl, "--user", "daemon-reload"),
        check=True,
        timeout=10,
    )
    try:
        yield launcher, child
    finally:
        subprocess.run(  # noqa: S603
            (systemctl, "--user", "stop", child, launcher),
            check=False,
            timeout=10,
        )
        subprocess.run(  # noqa: S603
            (systemctl, "--user", "reset-failed", child, launcher),
            check=False,
            timeout=10,
        )
        child_path.unlink(missing_ok=True)
        launcher_path.unlink(missing_ok=True)
        subprocess.run(  # noqa: S603
            (systemctl, "--user", "daemon-reload"),
            check=False,
            timeout=10,
        )


def test_harmless_manager_child_survives_oneshot_cgroup_exit(
    harmless_process_units: tuple[str, str],
) -> None:
    systemctl = shutil.which("systemctl")
    assert systemctl is not None
    launcher, child = harmless_process_units

    subprocess.run(  # noqa: S603
        (systemctl, "--user", "start", launcher),
        check=True,
        timeout=10,
    )
    child_state = subprocess.run(  # noqa: S603
        (
            systemctl,
            "--user",
            "show",
            child,
            "--property=ActiveState,MainPID,ControlGroup",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    launcher_state = subprocess.run(  # noqa: S603
        (systemctl, "--user", "show", launcher, "--property=ActiveState"),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout

    assert "ActiveState=active" in child_state
    assert f"/{child}" in child_state
    assert "MainPID=0" not in child_state
    assert "ActiveState=inactive" in launcher_state
