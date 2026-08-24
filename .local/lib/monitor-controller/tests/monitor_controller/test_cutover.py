"""Cutover preflight and rollback tests.

Preflight exists to answer one question before anything is stopped: would
starting the controller now leave the desktop worse than not starting it?
These tests are mostly about the *negative* answers, because a preflight that
only passes when everything is fine is indistinguishable from no preflight.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TypedDict

import pytest

from monitor_controller import cli
from monitor_controller.active import CONFLICTING_UNITS, ActivePaths
from monitor_controller.cutover import (
    CheckStatus,
    build_preflight_report,
    check_authority_lock_free,
    check_locked_install,
    check_no_conflicting_authority,
    check_no_surviving_workers,
    check_recovery_authority,
    cutover_commands,
    rollback_commands,
    unit_states,
)

ACTIVE_UNIT = "monitor-controller.service"


def _paths(root: Path) -> ActivePaths:
    return ActivePaths(
        data_home=root / "data",
        state_home=root / "state",
        runtime_dir=root / "runtime",
        config_home=root / "config",
        desktop_configuration_root=root / "desktop-config",
    )


def _install_venv(paths: ActivePaths) -> Path:
    """Create a plausible installed interpreter."""
    python = paths.fixed_venv / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return python


class TestLockedInstall:
    """The controller must run from the installer-owned venv."""

    def test_missing_interpreter_fails(self, tmp_path: Path) -> None:
        """An absent venv means install.sh has not been run."""
        result = check_locked_install(_paths(tmp_path))
        assert result.status is CheckStatus.FAIL
        assert "install.sh" in result.detail

    def test_non_executable_interpreter_fails(self, tmp_path: Path) -> None:
        """A present but unrunnable interpreter is worse than an absent one.

        The unit's ExecCondition would silently skip the start, which looks
        like success in `systemctl status`.
        """
        paths = _paths(tmp_path)
        python = _install_venv(paths)
        python.chmod(0o644)
        result = check_locked_install(paths)
        assert result.status is CheckStatus.FAIL
        assert "not executable" in result.detail

    def test_installed_interpreter_passes(self, tmp_path: Path) -> None:
        """The ordinary post-install case."""
        paths = _paths(tmp_path)
        _install_venv(paths)
        assert check_locked_install(paths).status is CheckStatus.OK


class TestConflictingAuthority:
    """No second dispatcher may be running at cutover."""

    def test_running_watcher_blocks(self) -> None:
        """A live shell watcher is a second dispatcher."""
        result = check_no_conflicting_authority(
            {"monitor-watcher-ng.service": True, "monitor-watcher.service": False}
        )
        assert result.status is CheckStatus.FAIL
        assert "monitor-watcher-ng.service" in result.detail

    def test_undetermined_state_blocks(self) -> None:
        """Unknown must block, not pass.

        "Cannot tell whether the old watcher is running" is exactly when
        starting a second authority causes the damage, so treating unknown as
        absent would defeat the check.
        """
        result = check_no_conflicting_authority(
            {"monitor-watcher-ng.service": None, "monitor-watcher.service": False}
        )
        assert result.status is CheckStatus.UNKNOWN
        assert result.blocking

    def test_running_beats_unknown_in_reporting(self) -> None:
        """A definite conflict is the more actionable message."""
        result = check_no_conflicting_authority(
            {"monitor-watcher-ng.service": True, "monitor-watcher.service": None}
        )
        assert result.status is CheckStatus.FAIL

    def test_all_inactive_passes(self) -> None:
        """Nothing else running is the precondition for cutover."""
        result = check_no_conflicting_authority(dict.fromkeys(CONFLICTING_UNITS, False))
        assert result.status is CheckStatus.OK


class TestSurvivingWorkers:
    """Unaccounted workers may still be driving xrandr."""

    def test_ambiguity_blocks(self) -> None:
        """An unaccounted worker may still be driving xrandr."""
        result = check_no_surviving_workers(("unknown unit monitor-apply@7.service",))
        assert result.status is CheckStatus.FAIL
        assert "monitor-apply@7.service" in result.detail

    def test_clean_namespace_passes(self) -> None:
        """Every worker accounted for is the clean case."""
        assert check_no_surviving_workers(()).status is CheckStatus.OK


class TestRecoveryAuthority:
    """Starting into a denied-authority state is safe but useless."""

    def test_denied_authority_blocks(self) -> None:
        """Recovery refusing authority makes the start useless."""
        result = check_recovery_authority(
            authority_allowed=False,
            reasons=("worker namespace scan failed",),
        )
        assert result.status is CheckStatus.FAIL
        assert "scan failed" in result.detail

    def test_denied_without_reasons_still_explains(self) -> None:
        """An empty reason list must not produce an empty message."""
        result = check_recovery_authority(authority_allowed=False, reasons=())
        assert result.status is CheckStatus.FAIL
        assert result.detail.strip()

    def test_allowed_authority_passes(self) -> None:
        """Clean or reconciled state permits dispatch."""
        result = check_recovery_authority(authority_allowed=True, reasons=())
        assert result.status is CheckStatus.OK


class TestAuthorityLockCheck:
    """Preflight must not report a lock as free when it is held."""

    def test_free_lock_passes(self, tmp_path: Path) -> None:
        """No holder means the controller can take authority."""
        assert check_authority_lock_free(_paths(tmp_path)).status is CheckStatus.OK

    def test_held_lock_fails(self, tmp_path: Path) -> None:
        """Another active instance already holds authority."""
        paths = _paths(tmp_path)
        paths.authority_lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(paths.authority_lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = check_authority_lock_free(paths)
            assert result.status is CheckStatus.FAIL
            assert "already held" in result.detail
        finally:
            os.close(descriptor)

    def test_held_shadow_lock_fails(self, tmp_path: Path) -> None:
        """A running shadow blocks active startup."""
        paths = _paths(tmp_path)
        paths.shadow_authority_lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            paths.shadow_authority_lock, os.O_WRONLY | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = check_authority_lock_free(paths)
            assert result.status is CheckStatus.FAIL
            assert "shadow" in result.detail
        finally:
            os.close(descriptor)

    def test_check_does_not_retain_the_lock(self, tmp_path: Path) -> None:
        """Probing must release, or preflight blocks the very start it clears."""
        paths = _paths(tmp_path)
        assert check_authority_lock_free(paths).status is CheckStatus.OK
        assert check_authority_lock_free(paths).status is CheckStatus.OK


class TestUnitStates:
    """Probe failures must surface as unknown rather than as inactive."""

    def test_probe_exception_becomes_unknown(self) -> None:
        """A failed systemctl must not read as inactive."""

        def probe(unit: str) -> bool:
            msg = f"systemctl unavailable for {unit}"
            raise OSError(msg)

        states = unit_states(("a.service",), probe)
        assert states == {"a.service": None}

    def test_probe_results_are_passed_through(self) -> None:
        """Successful probes report their real state."""
        states = unit_states(
            ("a.service", "b.service"), lambda unit: unit == "a.service"
        )
        assert states == {"a.service": True, "b.service": False}


class _ReadyKwargs(TypedDict):
    """Arguments to build_preflight_report describing a ready system."""

    paths: ActivePaths
    active_units: dict[str, bool | None]
    ambiguities: tuple[str, ...]
    authority_allowed: bool


class TestPreflightReport:
    """The combined go/no-go answer."""

    def _ready_kwargs(self, tmp_path: Path) -> _ReadyKwargs:
        paths = _paths(tmp_path)
        _install_venv(paths)
        return {
            "paths": paths,
            "active_units": dict.fromkeys(CONFLICTING_UNITS, False),
            "ambiguities": (),
            "authority_allowed": True,
        }

    def test_all_clear_is_ready(self, tmp_path: Path) -> None:
        """Every precondition holding is the go signal."""
        report = build_preflight_report(**self._ready_kwargs(tmp_path))
        assert report.ready, report.render()
        assert not report.blockers

    def test_any_blocker_prevents_readiness(self, tmp_path: Path) -> None:
        """One failure is enough to refuse."""
        kwargs = self._ready_kwargs(tmp_path)
        kwargs["ambiguities"] = ("surviving monitor-apply@3.service",)
        report = build_preflight_report(**kwargs)
        assert not report.ready
        assert len(report.blockers) == 1

    def test_render_names_every_blocker(self, tmp_path: Path) -> None:
        """The operator must see what to fix."""
        kwargs = self._ready_kwargs(tmp_path)
        kwargs["ambiguities"] = ("surviving monitor-apply@3.service",)
        kwargs["authority_allowed"] = False
        rendered = build_preflight_report(**kwargs).render()
        assert "monitor-apply@3.service" in rendered
        assert "NOT ready" in rendered
        assert "2 blocking" in rendered

    def test_missing_install_is_reported_first(self, tmp_path: Path) -> None:
        """The first blocker should be the root cause, not a symptom."""
        kwargs = self._ready_kwargs(tmp_path)
        kwargs["paths"] = _paths(tmp_path / "empty")
        report = build_preflight_report(**kwargs)
        assert report.blockers[0].name == "locked install"


class TestCommandSequences:
    """The operator-facing command lists."""

    def test_rollback_does_not_reference_removed_tooling(self) -> None:
        """bin/monitor-system was removed in 1f57823.

        Rollback runs when the display is already broken; referencing a
        script that no longer exists would strand the operator.
        """
        for command in rollback_commands():
            assert "monitor-system" not in command

    def test_rollback_stops_the_controller_before_starting_a_watcher(self) -> None:
        """Ordering prevents two authorities during rollback."""
        commands = rollback_commands()
        stop = next(i for i, c in enumerate(commands) if "stop monitor-controller" in c)
        start = next(
            i for i, c in enumerate(commands) if c.startswith("systemctl --user start")
        )
        assert stop < start

    def test_rollback_target_must_be_a_known_unit(self) -> None:
        """A typo must fail loudly, not produce a broken command."""
        with pytest.raises(ValueError, match="unknown rollback target"):
            rollback_commands("not-a-unit.service")

    @pytest.mark.parametrize(
        "target", ["monitor-watcher-ng.service", "monitor-watcher.service"]
    )
    def test_rollback_supports_both_watchers(self, target: str) -> None:
        """Either watcher is a valid rollback destination."""
        commands = rollback_commands(target)
        assert f"systemctl --user start {target}" in commands

    def test_cutover_enables_only_after_a_proven_start(self) -> None:
        """Enabling before verifying would retry a broken controller at login."""
        commands = cutover_commands()
        start = commands.index(f"systemctl --user start {ACTIVE_UNIT}")
        enable = commands.index(f"systemctl --user enable {ACTIVE_UNIT}")
        assert start < enable

    def test_cutover_stops_every_conflicting_unit(self) -> None:
        """A missed unit leaves a second dispatcher running."""
        commands = " ".join(cutover_commands())
        for unit in CONFLICTING_UNITS:
            assert f"stop {unit}" in commands

    def test_cutover_does_not_disable_shadow(self) -> None:
        """Shadow should be able to resume observing after a rollback.

        Stopping it for the cutover is necessary; disabling it would silently
        remove the observation capability the migration still depends on.
        """
        commands = cutover_commands()
        assert (
            "systemctl --user disable monitor-controller-shadow.service" not in commands
        )

    def test_every_command_is_ssh_safe(self) -> None:
        """No command may need DISPLAY; rollback often happens over SSH."""
        for command in (*cutover_commands(), *rollback_commands()):
            assert command.startswith("systemctl --user ")


class TestCutoverCli:
    """The operator-facing subcommands."""

    def test_preflight_exit_code_reflects_readiness(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Non-zero on blockers makes this usable as a gate in a script."""
        paths = _paths(tmp_path)
        _install_venv(paths)
        monkeypatch.setattr(
            cli.ActivePaths,
            "from_environment",
            classmethod(lambda _cls: paths),  # type: ignore[arg-type]
        )

        def _all_active(_unit: str) -> bool:
            return True

        monkeypatch.setattr(cli, "_systemctl_is_active", _all_active)

        assert cli.main(["preflight"]) == 1
        assert "NOT ready" in capsys.readouterr().out

    def test_preflight_passes_when_nothing_conflicts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A clean system reports ready and exits zero."""
        paths = _paths(tmp_path)
        _install_venv(paths)
        monkeypatch.setattr(
            cli.ActivePaths,
            "from_environment",
            classmethod(lambda _cls: paths),  # type: ignore[arg-type]
        )

        def _none_active(_unit: str) -> bool:
            return False

        monkeypatch.setattr(cli, "_systemctl_is_active", _none_active)

        assert cli.main(["preflight"]) == 0
        assert "Ready for cutover" in capsys.readouterr().out

    def test_command_subcommands_only_print(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """These must never execute anything; printing is the whole contract."""
        assert cli.main(["cutover-commands"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed == list(cutover_commands())

        assert cli.main(["rollback-commands"]) == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed == list(rollback_commands())

    def test_rollback_target_is_restricted_to_known_units(self) -> None:
        """Argparse rejects a bad target before any command is printed."""
        with pytest.raises(SystemExit):
            cli.main(["rollback-commands", "--target", "nonsense.service"])
