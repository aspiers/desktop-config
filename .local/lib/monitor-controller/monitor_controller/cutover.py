"""Preflight and rollback support for taking exclusive display authority.

Cutover is the one irreversible-feeling moment in this subsystem: the shell
watcher stops, the Python controller starts, and if the controller cannot
actually drive the display the user is left with no monitor management at all
— possibly over SSH, with no working display to debug from.

So preflight proves every precondition *before* anything is stopped, and
refuses on the first failure rather than reporting a list and proceeding.
Every check here answers "would starting the controller now leave the desktop
worse than not starting it?".

Nothing in this module stops, starts, enables, or disables anything. It
reports; the operator acts. That separation is deliberate: a preflight that
also performs the cutover cannot be run speculatively, and one that cannot be
run speculatively will not be run at all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from monitor_controller.active import (
    CONFLICTING_UNITS,
    CUTOVER_AUTHORIZATION_VALUE,
    CUTOVER_AUTHORIZATION_VARIABLE,
    DRY_RUN_ARGUMENT,
    ActiveAuthorityLock,
    ActivePaths,
    ActiveStartupError,
)
from monitor_controller.active import (
    CUTOVER_AUTHORIZATION_VALUE as _AUTHORIZATION_VALUE,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


class EntryPointRunner(Protocol):
    """Run the controller's dry run and report its status and output."""

    def __call__(self, python: Path, /) -> tuple[int, str]:
        """Return the exit status and combined output."""
        ...


class UnitActivityProbe(Protocol):
    """Report whether one systemd user unit is currently active."""

    def __call__(self, unit: str) -> bool:
        """Return True when the unit is active."""
        ...


class CheckStatus(StrEnum):
    """Outcome of a single preflight check."""

    OK = "ok"
    FAIL = "fail"
    # Something could not be determined rather than being known-bad. Treated as
    # blocking, because cutover on unverified ground is the thing to avoid, but
    # reported distinctly so the operator can tell "broken" from "unknown".
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One named precondition and why it holds or does not."""

    name: str
    status: CheckStatus
    detail: str

    @property
    def blocking(self) -> bool:
        """Return whether this result should prevent cutover."""
        return self.status is not CheckStatus.OK


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """The full set of checks and the single go/no-go answer."""

    results: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[CheckResult, ...]:
        """Return every result preventing cutover, in check order."""
        return tuple(result for result in self.results if result.blocking)

    @property
    def ready(self) -> bool:
        """Return whether every precondition holds."""
        return not self.blockers

    def render(self) -> str:
        """Return an operator-readable report."""
        lines: list[str] = []
        for result in self.results:
            marker = {
                CheckStatus.OK: "ok",
                CheckStatus.FAIL: "FAIL",
                CheckStatus.UNKNOWN: "????",
            }[result.status]
            lines.append(f"  [{marker:>4}] {result.name}: {result.detail}")
        verdict = (
            "Ready for cutover."
            if self.ready
            else f"NOT ready: {len(self.blockers)} blocking check(s)."
        )
        return "\n".join([*lines, "", verdict])


# The target the stowed units declare in their [Install] sections. Removing a
# unit's symlink from this target's .wants/ directory is what "do not start at
# login" actually means; `systemctl --user disable` does that *and* deletes the
# unit file itself, which for a Stow-managed tree is the repository symlink.
# The module the unit's ExecStart runs. Kept beside the check that invokes it
# so a rename cannot leave preflight testing a module that no longer exists.
ACTIVE_MODULE = "monitor_controller.active"

# Bounded: a dry run builds a composition and exits, so anything slower than
# this is itself a problem worth reporting rather than waiting out.
_DRY_RUN_TIMEOUT_SECONDS = 60.0


def _run_entry_point_dry_run(python: Path) -> tuple[int, str]:
    """Run the unit's own interpreter and module with `--dry-run`.

    Uses `-I` and the same namespace and authorisation the unit sets, so this
    exercises the real deployed entry point rather than an approximation of it.
    The authorisation value is supplied here because the dry run takes no
    authority; withholding it would only test the authorisation gate.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(python), "-I", "-m", ACTIVE_MODULE, DRY_RUN_ARGUMENT],
        capture_output=True,
        text=True,
        timeout=_DRY_RUN_TIMEOUT_SECONDS,
        check=False,
        env={
            **os.environ,
            "MONITOR_CONTROLLER_NAMESPACE": "active",
            "MONITOR_CONTROLLER_CUTOVER_AUTHORIZED": _AUTHORIZATION_VALUE,
        },
    )
    return completed.returncode, completed.stderr or completed.stdout


INSTALL_TARGET = "fluxbox-session.target"

# Every unit whose login start-up this module may suppress. The controller's
# own unit belongs here as well as the ones it displaces: rollback has to stop
# *it* starting at login, and it is stow-managed identically.
SUPPRESSIBLE_UNITS: tuple[str, ...] = (
    *CONFLICTING_UNITS,
    "monitor-controller.service",
)


def suppress_at_login_command(unit: str) -> str:
    """Return the command stopping *unit* starting at login, without disabling it.

    `systemctl --user disable` cannot be used here. These unit files are GNU
    Stow symlinks into the repository, and disable removes the unit symlink as
    well as the `.wants/` link:

        Removed '/home/adam/.config/systemd/user/monitor-watcher-ng.service'.

    That deletion is what broke the 2026-08-25 rollback: `enable` then failed
    with "Unit monitor-watcher-ng.service does not exist", at the exact moment
    the display was already unmanaged. Recovery needed the symlinks recreated
    by hand.

    `systemctl --user mask` is not an alternative: it refuses outright on a
    unit that is already a symlink, with "File ... already exists and is a
    symlink".

    Removing only the `.wants/` link is precisely the half of disable that is
    wanted. It leaves `is-enabled` reporting `linked` rather than `disabled`,
    and `enable` restores it, so the cutover/rollback round trip is lossless.
    """
    if unit not in SUPPRESSIBLE_UNITS:
        msg = f"unknown unit: {unit}"
        raise ValueError(msg)
    return (
        f"rm -f ${{XDG_CONFIG_HOME:-$HOME/.config}}/systemd/user/"
        f"{INSTALL_TARGET}.wants/{unit}"
    )


def check_locked_install(paths: ActivePaths) -> CheckResult:
    """Confirm the controller runs from the installer-owned locked venv.

    Running from the Stow source tree instead would make the deployed
    behaviour depend on an uncommitted working copy.
    """
    python = paths.fixed_venv / "bin" / "python"
    if not python.is_file():
        return CheckResult(
            name="locked install",
            status=CheckStatus.FAIL,
            detail=f"missing {python}; run .local/lib/monitor-controller/install.sh",
        )
    if not python.stat().st_mode & 0o111:
        return CheckResult(
            name="locked install",
            status=CheckStatus.FAIL,
            detail=f"{python} is not executable",
        )
    return CheckResult(
        name="locked install",
        status=CheckStatus.OK,
        detail=str(python),
    )


def check_no_conflicting_authority(
    active_units: Mapping[str, bool | None],
) -> CheckResult:
    """Confirm no other dispatcher is running.

    An unknown state blocks: "cannot tell whether the old watcher is running"
    is precisely when starting a second authority does the damage.
    """
    running = sorted(unit for unit, state in active_units.items() if state is True)
    unknown = sorted(unit for unit, state in active_units.items() if state is None)
    if running:
        return CheckResult(
            name="no conflicting authority",
            status=CheckStatus.FAIL,
            detail=f"still active: {', '.join(running)}",
        )
    if unknown:
        return CheckResult(
            name="no conflicting authority",
            status=CheckStatus.UNKNOWN,
            detail=f"state undetermined: {', '.join(unknown)}",
        )
    return CheckResult(
        name="no conflicting authority",
        status=CheckStatus.OK,
        detail="no other dispatcher is running",
    )


def check_no_surviving_workers(ambiguities: Sequence[str]) -> CheckResult:
    """Confirm recovery found no worker it cannot account for.

    A surviving worker from a previous run may still be driving xrandr. Taking
    authority beside it recreates the colliding-relayout failure the whole
    controller exists to prevent.
    """
    if ambiguities:
        return CheckResult(
            name="no surviving ambiguous worker",
            status=CheckStatus.FAIL,
            detail="; ".join(ambiguities),
        )
    return CheckResult(
        name="no surviving ambiguous worker",
        status=CheckStatus.OK,
        detail="worker namespace is fully accounted for",
    )


def check_recovery_authority(
    *,
    authority_allowed: bool,
    reasons: Sequence[str],
) -> CheckResult:
    """Confirm recovery itself would grant authority.

    If recovery fails closed, the controller will start into RECOVERING and
    dispatch nothing. That is safe but useless, and doing it *after* stopping
    the shell watcher leaves the desktop unmanaged.
    """
    if not authority_allowed:
        detail = "; ".join(reasons) if reasons else "recovery denied authority"
        return CheckResult(
            name="recovery grants authority",
            status=CheckStatus.FAIL,
            detail=detail,
        )
    return CheckResult(
        name="recovery grants authority",
        status=CheckStatus.OK,
        detail="clean state or reconciled recovery",
    )


def check_authority_lock_free(paths: ActivePaths) -> CheckResult:
    """Confirm the active authority lock is not already held."""
    lock = ActiveAuthorityLock(paths.authority_lock, paths.shadow_authority_lock)
    try:
        with lock:
            pass
    except ActiveStartupError as error:
        return CheckResult(
            name="authority lock is free",
            status=CheckStatus.FAIL,
            detail=str(error),
        )
    except OSError as error:
        return CheckResult(
            name="authority lock is free",
            status=CheckStatus.UNKNOWN,
            detail=f"cannot test the lock: {error}",
        )
    return CheckResult(
        name="authority lock is free",
        status=CheckStatus.OK,
        detail=str(paths.authority_lock),
    )


def check_entry_point_runs(
    paths: ActivePaths,
    *,
    runner: EntryPointRunner | None = None,
) -> CheckResult:
    """Confirm the controller can actually start, by starting it.

    Every other check here examines the *environment*. None of them asked
    whether the controller runs, and on 2026-08-25 all six reported green for a
    binary that could not start at all: the cutover stopped both watchers, the
    controller refused, and the desktop was unmanaged until rollback.

    The near miss was `check_locked_install`, which proves the venv's python
    exists and is executable — not that the module starts.

    So this runs the unit's own interpreter and module, exactly as the unit
    would, with `--dry-run`. That mode builds the full composition and then
    exits without acquiring the authority lock or starting any worker, so this
    check remains safe to run speculatively.
    """
    python = paths.fixed_venv / "bin" / "python"
    if not python.is_file():
        return CheckResult(
            name="controller starts",
            status=CheckStatus.FAIL,
            detail=f"missing {python}; see the locked install check",
        )
    invoke = runner or _run_entry_point_dry_run
    try:
        status, output = invoke(python)
    except OSError as error:
        return CheckResult(
            name="controller starts",
            status=CheckStatus.UNKNOWN,
            detail=f"cannot run the entry point: {error}",
        )
    if status != 0:
        return CheckResult(
            name="controller starts",
            status=CheckStatus.FAIL,
            detail=(
                f"{python} -m {ACTIVE_MODULE} {DRY_RUN_ARGUMENT} exited "
                f"{status}: {output.strip() or '<no output>'}"
            ),
        )
    return CheckResult(
        name="controller starts",
        status=CheckStatus.OK,
        detail="entry point composed and exited cleanly",
    )


def check_rollback_available() -> CheckResult:
    """Confirm the rollback path is usable before it is needed.

    Rollback runs when the display is already misbehaving, quite possibly over
    SSH. Discovering then that systemctl is unreachable is too late.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return CheckResult(
            name="rollback available",
            status=CheckStatus.FAIL,
            detail="systemctl not found on PATH",
        )
    return CheckResult(
        name="rollback available",
        status=CheckStatus.OK,
        detail=f"{systemctl}; see rollback_commands()",
    )


def rollback_commands(target: str = "monitor-watcher-ng.service") -> tuple[str, ...]:
    """Return the exact commands restoring the previous watcher.

    Deliberately explicit `systemctl --user` invocations. An earlier design
    routed this through `bin/monitor-system`, which was removed in `1f57823`;
    rollback instructions must not depend on state that no longer exists.

    Every command is safe to run over SSH with no DISPLAY, which is the
    situation rollback exists for.
    """
    if target not in CONFLICTING_UNITS:
        msg = f"unknown rollback target: {target}"
        raise ValueError(msg)
    return (
        "systemctl --user stop monitor-controller.service",
        # Suppress rather than disable, for the same reason as cutover: the
        # controller's own unit file is a Stow symlink too.
        suppress_at_login_command("monitor-controller.service"),
        "systemctl --user daemon-reload",
        f"systemctl --user enable {target}",
        f"systemctl --user start {target}",
        f"systemctl --user status --no-pager {target}",
    )


def build_preflight_report(  # noqa: PLR0913 - keyword-only report inputs
    *,
    paths: ActivePaths,
    active_units: Mapping[str, bool | None],
    ambiguities: Sequence[str],
    authority_allowed: bool,
    recovery_reasons: Sequence[str] = (),
    entry_point_runner: EntryPointRunner | None = None,
) -> PreflightReport:
    """Run every precondition and return the combined go/no-go report.

    Ordered cheapest-and-most-fundamental first, so the first blocker named is
    usually the root cause rather than a downstream symptom.
    """
    return PreflightReport(
        results=(
            check_locked_install(paths),
            # Immediately after the install check, and before anything about
            # the environment: a controller that cannot start makes every
            # later verdict irrelevant.
            check_entry_point_runs(paths, runner=entry_point_runner),
            check_no_conflicting_authority(active_units),
            check_authority_lock_free(paths),
            check_no_surviving_workers(ambiguities),
            check_recovery_authority(
                authority_allowed=authority_allowed,
                reasons=recovery_reasons,
            ),
            check_rollback_available(),
        )
    )


def cutover_commands() -> tuple[str, ...]:
    """Return the cutover sequence, in the order preflight assumes.

    Enablement comes last and only after verification, so a failed start
    cannot leave a broken controller to be retried automatically at the next
    login.

    The authorisation drop-in comes first, because without it the controller
    refuses to start — and by then the shell watcher has already been stopped.
    """
    stops = tuple(f"systemctl --user stop {unit}" for unit in CONFLICTING_UNITS)
    # Not `systemctl --user disable`: that deletes the stowed unit symlink and
    # leaves rollback unable to re-enable it. See suppress_at_login_command().
    suppressions = tuple(
        suppress_at_login_command(unit)
        for unit in CONFLICTING_UNITS
        if unit != "monitor-controller-shadow.service"
    )
    return (
        # Authorise before stopping anything: the controller will not start
        # without this, and discovering that after the watcher has stopped
        # leaves the desktop unmanaged for no reason.
        (
            "systemctl --user edit monitor-controller.service"
            f"  # add [Service] Environment={CUTOVER_AUTHORIZATION_VARIABLE}"
            f"={CUTOVER_AUTHORIZATION_VALUE}"
        ),
        "systemctl --user daemon-reload",
        *stops,
        *suppressions,
        "systemctl --user daemon-reload",
        "systemctl --user start monitor-controller.service",
        "systemctl --user status --no-pager monitor-controller.service",
        # Only now, with the controller proven running, make it survive login.
        "systemctl --user enable monitor-controller.service",
    )


def unit_states(
    units: Iterable[str],
    probe: UnitActivityProbe,
) -> dict[str, bool | None]:
    """Map each unit to active/inactive/unknown using an injected probe."""
    states: dict[str, bool | None] = {}
    for unit in units:
        try:
            states[unit] = probe(unit)
        except Exception:  # noqa: BLE001 - injected trust boundary
            states[unit] = None
    return states
