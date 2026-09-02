"""Tray readiness and harmless persistent desktop process ownership contracts."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
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
    setup_monitor = (_REPOSITORY / "bin" / "setup-monitor").read_text(
        encoding="utf-8"
    )
    tray_diag = (_REPOSITORY / "bin" / "tray-diag").read_text(encoding="utf-8")

    assert "KillMode=mixed" in finalizer
    assert "TimeoutStopSec=130s" in finalizer
    assert "setup-monitor" not in finalizer
    assert "Type=exec" in applet
    assert "ExecStart=/usr/bin/nm-applet --indicator" in applet
    assert "PartOf=fluxbox-session.target" in applet
    assert "KillMode=control-group" in applet
    assert "Type=oneshot" in diagnostics
    assert "TimeoutStartSec=45s" in diagnostics
    assert "internal tray-diagnostics" in diagnostics
    assert "ExecStartPre=-%h/bin/panel-debug-status --mark-boundary %I" in panel
    assert "ExecStart=/usr/bin/xfce4-panel -r" in panel
    assert "KillMode=control-group" in panel
    assert "TimeoutStartSec=30s" in panel
    assert "systemd-run --user" in restart
    assert "-p KillMode=process" in restart
    assert (
        "    wait_for_stable_tray\n"
        "    systemctl --user restart nm-applet.service\n" in setup_monitor
    )
    assert "xfce4-panel-debug-ensure >/dev/null 2>&1 || true" in setup_monitor
    assert "pkill nm-applet" not in setup_monitor
    assert "nohup nm-applet" not in setup_monitor
    assert "RegisteredStatusNotifierItems" in tray_diag
    assert "org.kde.StatusNotifierItem" in tray_diag
    assert "NO NM-APPLET STATUSNOTIFIER ITEM REGISTERED" in tray_diag


def test_panel_debug_service_owns_panel_but_not_its_log_pipeline() -> None:
    panel_unit = (_UNITS / "xfce4-panel-debug.service").read_text(encoding="utf-8")
    log_unit = (_UNITS / "xfce4-panel-debug-log.service").read_text(encoding="utf-8")
    hook = (
        _REPOSITORY
        / ".xsession-progs.d"
        / "person-adam.spiers"
        / "00-xfce4-panel-debug"
    ).read_text(encoding="utf-8")
    ensure = (_REPOSITORY / "bin" / "xfce4-panel-debug-ensure").read_text(
        encoding="utf-8"
    )
    runner = (_REPOSITORY / "bin" / "xfce4-panel-debug-run").read_text(
        encoding="utf-8"
    )
    logger = (_REPOSITORY / "bin" / "xfce4-panel-debug-log").read_text(
        encoding="utf-8"
    )

    assert "exec xfce4-panel-debug-ensure" in hook
    assert 'grep -Fq "/$unit" "/proc/$pid/cgroup"' in ensure
    assert 'systemctl --user is-active --quiet "$unit"' in ensure
    assert "ExecStart=%h/bin/xfce4-panel-debug-run" in panel_unit
    assert "Wants=xfce4-panel-debug-log.service" in panel_unit
    assert "Restart=always" in panel_unit
    assert "KillMode=process" in panel_unit
    assert "ExecStart=%h/bin/xfce4-panel-debug-log" in log_unit
    assert "KillMode=control-group" in log_unit
    assert "PANEL_DEBUG=$panel_debug xfce4-panel &" in runner
    assert 'wait "$panel_pid"' in runner
    assert "pids_in_own_cgroup wrapper-2.0" in runner
    assert "exec journal-follow-cursor" in logger
    assert (
        "xfce4-panel-debug.service \\\n"
        '    "$cursor_file" \\\n'
        '    "$log_file"' in logger
    )
    assert "2097152" in logger


def test_journal_follower_durably_resumes_and_recovers_invalid_cursor(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    journalctl = fake_bin / "journalctl"
    journalctl.write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == *"--cursor="* ]]; then\n'
        '  [ "${JOURNAL_CURSOR_INVALID:-0}" = 1 ] && exit 1\n'
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$*\" > \"$JOURNAL_ARGS\"\n"
        "emit() {\n"
        "  printf '{\"MESSAGE\":\"%s\",\"_COMM\":\"%s\","
        "\"__CURSOR\":\"%s\"}\\n' \"$1\" \"$2\" \"$3\"\n"
        "}\n"
        'case "${JOURNAL_BATCH:-1}" in\n'
        '  1) emit one xfce4-panel cursor-1\n'
        '     emit "child exited with status 99" application cursor-1a\n'
        '     emit two wrapper-2.0 cursor-2 ;;\n'
        '  2) emit three xfce4-panel cursor-3 ;;\n'
        '  3) emit recovered xfce4-panel cursor-4 ;;\n'
        '  4) emit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa xfce4-panel a\n'
        '     emit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb wrapper-2.0 b\n'
        '     emit cccccccccccccccccccccccccccccc xfce4-panel c ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    journalctl.chmod(0o700)
    cursor = tmp_path / "cursor"
    log = tmp_path / "bounded.log"
    arguments = tmp_path / "arguments"
    environment = os.environ.copy()
    environment.update(
        {
            "JOURNAL_ARGS": str(arguments),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )
    command = str(_REPOSITORY / "bin" / "journal-follow-cursor")

    def follow(
        batch: str,
        *,
        invalid_cursor: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment["JOURNAL_BATCH"] = batch
        environment["JOURNAL_CURSOR_INVALID"] = "1" if invalid_cursor else "0"
        return subprocess.run(  # noqa: S603
            (command, "test.service", str(cursor), str(log), "1000", "3"),
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=5,
        )

    first = follow("1")
    assert first.returncode == 0, first.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["one", "two"]
    assert cursor.read_text(encoding="utf-8").strip() == "cursor-2"
    assert "--lines 0" in arguments.read_text(encoding="utf-8")

    second = follow("2")
    assert second.returncode == 0, second.stderr
    assert log.read_text(encoding="utf-8").splitlines()[-1] == "three"
    assert cursor.read_text(encoding="utf-8").strip() == "cursor-3"
    assert "--after-cursor=cursor-2" in arguments.read_text(encoding="utf-8")

    recovered = follow("3", invalid_cursor=True)
    assert recovered.returncode == 0, recovered.stderr
    assert log.read_text(encoding="utf-8").splitlines()[-1] == "recovered"
    assert cursor.read_text(encoding="utf-8").strip() == "cursor-4"
    assert "--since=-10min" in arguments.read_text(encoding="utf-8")
    assert list(tmp_path.glob("cursor.invalid-*"))

    bounded_cursor = tmp_path / "bounded.cursor"
    bounded_log = tmp_path / "rotating.log"
    environment["JOURNAL_BATCH"] = "4"
    environment["JOURNAL_CURSOR_INVALID"] = "0"
    bounded = subprocess.run(  # noqa: S603
        (
            command,
            "test.service",
            str(bounded_cursor),
            str(bounded_log),
            "20",
            "2",
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )
    assert bounded.returncode == 0, bounded.stderr
    retained = sorted(tmp_path.glob("rotating.log*"))
    assert [path.name for path in retained] == ["rotating.log", "rotating.log.1"]
    assert all(path.stat().st_size <= 20 for path in retained)


def test_panel_debug_status_reports_outstanding_and_observed_evidence(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    control_dir = runtime / "xfce4-panel-debug"
    control_dir.mkdir(parents=True)
    control_fifo = control_dir / "control"
    os.mkfifo(control_fifo)
    environment = os.environ.copy()
    environment.update(
        {
            "PANEL_DEBUG_LOG_DIR": str(tmp_path),
            "XDG_RUNTIME_DIR": str(runtime),
        }
    )
    command = str(_REPOSITORY / "bin" / "panel-debug-status")

    outstanding = subprocess.run(  # noqa: S603
        (command,),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )

    assert outstanding.returncode == 0
    assert "respawn_path=outstanding" in outstanding.stdout
    assert "tray_allocation=outstanding" in outstanding.stdout

    tmp_path.joinpath("xfce4-panel-debug.log.1").write_text(
        "plugin unrealized; quitting child\n"
        "allocate rows=1, icon_size=21\n",
        encoding="utf-8",
    )

    marker_file = control_dir / "last-marker"

    def collect_boundary() -> None:
        with control_fifo.open(encoding="utf-8") as source:
            marker_file.write_text(source.readline(), encoding="utf-8")

    collector = threading.Thread(target=collect_boundary)
    collector.start()
    boundary = subprocess.run(  # noqa: S603
        (command, "--mark-boundary", "test-relayout"),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )
    collector.join(timeout=2)
    assert not collector.is_alive()
    assert boundary.returncode == 0
    assert "boundary=PANEL_DEBUG_RELAYOUT_BOUNDARY" in boundary.stdout
    assert "respawn_path=outstanding" in boundary.stdout

    awaiting = subprocess.run(  # noqa: S603
        (command,),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )
    assert awaiting.returncode == 0
    assert "respawn_path=outstanding" in awaiting.stdout
    assert "plugin-unrealized" not in awaiting.stdout

    with tmp_path.joinpath("xfce4-panel-debug.log").open("a", encoding="utf-8") as log:
        log.write(marker_file.read_text(encoding="utf-8"))
        log.write("child exited with status 1\n")
        log.write("allocate rows=1, icon_size=31\n")
    observed = subprocess.run(  # noqa: S603
        (command,),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )

    assert observed.returncode == 0
    assert "respawn_path=child-exited" in observed.stdout
    assert "plugin-unrealized" not in observed.stdout
    assert "tray_allocation=observed" in observed.stdout
    assert "icon_size=31" in observed.stdout

    with tmp_path.joinpath("xfce4-panel-debug.log").open("a", encoding="utf-8") as log:
        log.write("plugin unrealized; quitting child\n")
    mixed = subprocess.run(  # noqa: S603
        (command,),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )

    assert mixed.returncode == 0
    assert "respawn_path=mixed" in mixed.stdout
    assert "plugin_unrealized_excerpt=" in mixed.stdout
    assert "child_exit_excerpt=" in mixed.stdout


def test_panel_debug_runner_reaps_panel_without_waiting_on_inherited_output(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    panel = fake_bin / "xfce4-panel"
    panel.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' $$ > \"$PANEL_PID_FILE\"\n"
        "(sleep 0.8) &\n"
        "sleep 5 &\n"
        "printf '%s\\n' $! > \"$APP_PID_FILE\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    panel.chmod(0o700)
    panel_pid_file = tmp_path / "panel.pid"
    app_pid_file = tmp_path / "application.pid"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_PID_FILE": str(app_pid_file),
            "HOME": str(tmp_path / "home"),
            "PANEL_PID_FILE": str(panel_pid_file),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
    )
    process = subprocess.Popen(  # noqa: S603
        (str(_REPOSITORY / "bin" / "xfce4-panel-debug-run"),),
        env=environment,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 2
        while not panel_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert panel_pid_file.is_file()
        panel_pid = int(panel_pid_file.read_text(encoding="utf-8"))
        while Path(f"/proc/{panel_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not Path(f"/proc/{panel_pid}").exists()
        assert process.wait(timeout=2) == 1
        app_pid = int(app_pid_file.read_text(encoding="utf-8"))
        assert Path(f"/proc/{app_pid}").exists()
        os.kill(app_pid, signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=2)
        assert process.returncode == 1, stderr
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=2)
        if app_pid_file.exists():
            app_pid = int(app_pid_file.read_text(encoding="utf-8"))
            if Path(f"/proc/{app_pid}").exists():
                os.kill(app_pid, signal.SIGTERM)


def test_tray_capture_is_bounded_nonblocking_and_skips_on_prune_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    log_dir = home / ".log" / "tray-diag"
    log_dir.mkdir(parents=True)
    library = home / "lib" / "libhost.sh"
    library.parent.mkdir()
    library.write_text("read_localhost_nickname() { :; }\n", encoding="utf-8")
    for index in range(1, 206):
        capture = log_dir / f"{index:03}.txt"
        capture.write_text("old\n", encoding="utf-8")
        os.utime(capture, (index, index))

    environment = os.environ.copy()
    environment.update({"HOME": str(home)})
    completed = subprocess.run(  # noqa: S603
        (
            "/bin/bash",
            "-c",
            r"""
source "$1"
sleep() { :; }
tray-diag() {
    touch "$HOME/started"
    while [ ! -e "$HOME/release" ]; do /bin/sleep 0.01; done
    printf 'partial\n' > "$1"
    return 1
}
layout=capture-fails
capture_tray_diag
touch "$HOME/returned"
touch "$HOME/release"
wait || exit 20
find "$HOME/.log/tray-diag" -maxdepth 1 -name '*.txt' | wc -l > "$HOME/count"
rm -f "$HOME/started"
find() { return 1; }
layout=prune-fails
capture_tray_diag
wait || exit 21
[ ! -e "$HOME/started" ] || exit 22
""",
            "setup-monitor-retention-test",
            str(_REPOSITORY / "bin" / "setup-monitor"),
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert (home / "returned").is_file()
    assert home.joinpath("count").read_text(encoding="utf-8").strip() == "200"
    assert not any(log_dir.glob("00[1-6].txt"))
    assert (log_dir / "007.txt").is_file()
    assert not any(log_dir.glob("*.prune-fails.txt"))


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
