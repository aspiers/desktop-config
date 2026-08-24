"""Exclusive-authority tests for the active composition root.

The invariant under test is that exactly one dispatch authority can exist at
a time. Everything here is about proving startup fails closed rather than
degrading, because two controllers dispatching to one display reproduces the
colliding-relayout failures this subsystem exists to eliminate.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from monitor_controller.active import (
    CONFLICTING_UNITS,
    ActiveAuthorityLock,
    ActivePaths,
    ActiveStartupError,
)


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
