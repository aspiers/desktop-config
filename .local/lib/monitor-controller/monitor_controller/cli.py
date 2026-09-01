"""Read-only status and deterministic simulation/replay command line."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from monitor_controller.active import CONFLICTING_UNITS, ActivePaths
from monitor_controller.codec import StateCodecError, decode_state
from monitor_controller.cutover import (
    build_preflight_report,
    cutover_commands,
    rollback_commands,
    unit_states,
)
from monitor_controller.runtime.journal import service_logger
from monitor_controller.runtime.persistence import StateNamespace

if TYPE_CHECKING:
    from monitor_controller.model import State
from monitor_controller.simulation.replay import (
    ReplayFormatError,
    ReplayMismatchError,
    decode_replay,
    replay,
)
from monitor_controller.simulation.scenario import (
    ScenarioAssertionError,
    ScenarioFormatError,
    load_scenarios,
    normalize_state,
    run_scenario,
)
from monitor_controller.workers.apply import run_apply_worker
from monitor_controller.workers.common import WorkerStartupError
from monitor_controller.workers.finalize import (
    run_finalize_worker,
    run_tray_diagnostics,
)
from monitor_controller.workers.prepare import run_prepare_worker
from monitor_controller.workers.probe import run_probe_worker


class _Parser(argparse.ArgumentParser):
    """Argument parser which reports errors without any domain side effects."""


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="monitor-controller")
    subparsers = parser.add_subparsers(dest="command", required=True)

    simulate = subparsers.add_parser("simulate", help="simulate a scenario document")
    simulate.add_argument("scenario", type=Path)

    replay_parser = subparsers.add_parser(
        "replay", help="replay and verify a JSONL decision trace"
    )
    replay_parser.add_argument("trace", type=Path)

    status = subparsers.add_parser("status", help="show persisted controller status")
    status.add_argument(
        "--namespace",
        choices=tuple(item.value for item in StateNamespace),
        default=StateNamespace.ACTIVE.value,
    )
    status.add_argument("--state-home", type=Path)
    status.add_argument(
        "--json",
        action="store_true",
        help="emit the full machine-readable state record",
    )

    subparsers.add_parser(
        "preflight",
        help="check whether the active controller can safely take authority",
    )

    subparsers.add_parser(
        "cutover-commands",
        help="print the cutover command sequence without running anything",
    )

    rollback = subparsers.add_parser(
        "rollback-commands",
        help="print the commands restoring the previous watcher",
    )
    rollback.add_argument(
        "--target",
        default="monitor-watcher-ng.service",
        choices=CONFLICTING_UNITS,
    )

    internal = subparsers.add_parser(
        "internal",
        help=argparse.SUPPRESS,
        description="Typed internal worker entry points; not an operator interface",
    )
    workers = internal.add_subparsers(dest="internal_command", required=True)
    probe = workers.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument("--transaction-root", required=True, type=Path)
    probe.add_argument("--action-id", required=True)
    probe.add_argument("--unit", required=True)
    probe.add_argument(
        "--sysfs-root",
        type=Path,
        default=Path("/sys/class/drm"),
    )
    apply = workers.add_parser("apply", help=argparse.SUPPRESS)
    apply.add_argument("--transaction-root", required=True, type=Path)
    apply.add_argument("--action-id", required=True)
    apply.add_argument("--unit", required=True)
    apply.add_argument(
        "--sysfs-root",
        type=Path,
        default=Path("/sys/class/drm"),
    )
    prepare = workers.add_parser("prepare", help=argparse.SUPPRESS)
    prepare.add_argument("--transaction-root", required=True, type=Path)
    prepare.add_argument("--plan-root", required=True, type=Path)
    prepare.add_argument("--action-id", required=True)
    prepare.add_argument("--unit", required=True)
    prepare.add_argument("--sysfs-root", required=True, type=Path)
    prepare.add_argument("--home-root", required=True, type=Path)
    prepare.add_argument("--leaf-root", required=True, type=Path)
    finalize = workers.add_parser("finalize", help=argparse.SUPPRESS)
    finalize.add_argument("--transaction-root", required=True, type=Path)
    finalize.add_argument("--plan-root", required=True, type=Path)
    finalize.add_argument("--event-generation-file", required=True, type=Path)
    finalize.add_argument("--action-id", required=True)
    finalize.add_argument("--unit", required=True)
    finalize.add_argument("--sysfs-root", required=True, type=Path)
    finalize.add_argument("--home-root", required=True, type=Path)
    finalize.add_argument("--leaf-root", required=True, type=Path)
    diagnostics = workers.add_parser("tray-diagnostics", help=argparse.SUPPRESS)
    diagnostics.add_argument("--transaction-root", required=True, type=Path)
    diagnostics.add_argument("--action-id", required=True)
    diagnostics.add_argument("--tray-diag", required=True, type=Path)
    diagnostics.add_argument("--output-root", required=True, type=Path)
    return parser


def _default_state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "state"


def _simulate(path: Path) -> int:
    scenarios = load_scenarios(path)
    results = tuple(run_scenario(scenario) for scenario in scenarios)
    print(
        json.dumps(
            {
                "scenario_count": len(results),
                "step_count": sum(len(result.decisions) for result in results),
                "scenarios": [
                    {
                        "name": result.scenario.name,
                        "steps": len(result.decisions),
                        "final_state": normalize_state(result.final_state),
                    }
                    for result in results
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _replay(path: Path) -> int:
    trace = decode_replay(path.read_bytes())
    decisions = replay(trace)
    final_state = trace.initial_state if not decisions else decisions[-1].state
    print(
        json.dumps(
            {
                "decision_count": len(decisions),
                "final_state": normalize_state(final_state),
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _status(state_home: Path, namespace: StateNamespace, *, as_json: bool) -> int:
    path = state_home / "monitor-controller" / namespace.value / "state.json"
    if not path.is_file():
        if as_json:
            print(
                json.dumps(
                    {
                        "namespace": namespace.value,
                        "path": str(path),
                        "status": "missing",
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"No persisted {namespace.value} controller state at {path}")
        return 1
    state = decode_state(path.read_bytes())
    status = normalize_state(state)
    status.update(
        {
            "boot_id": str(state.boot_id.value),
            "controller_instance": str(state.controller_instance.value),
            "display_identity": state.display_identity.value,
            "namespace": namespace.value,
            "path": str(path),
            "schema_version": state.schema_version,
            "status": "ok",
        }
    )
    if as_json:
        print(json.dumps(status, sort_keys=True))
        return 0
    print(_human_status(state, namespace, path))
    return 0


def _human_status(state: State, namespace: StateNamespace, path: Path) -> str:
    """Render the state a person actually asks about, one fact per line."""

    def profile(value: str | None) -> str:
        return value if value is not None else "(none)"

    candidate = state.candidate.profile if state.candidate is not None else None
    in_flight = [
        f"{action.action_id.kind.value} {action.action_id.value}"
        f" [{action.lifecycle.value}]"
        for action in (
            state.probe,
            state.application,
            state.planning,
            state.preparation,
            state.finalization,
        )
        if action is not None
    ]
    lines = [
        f"Namespace:        {namespace.value}",
        f"Phase:            {state.phase.value}",
        f"Candidate:        {profile(candidate)}",
        f"Stable X profile: {profile(state.stable_x_profile)}",
        f"Desktop:          finalized for {profile(state.desktop_finalized_profile)}",
        (
            f"Planning:         {state.planning_state.value}"
            f" | Preparation: {state.preparation_state.value}"
        ),
        (
            "In-flight:        " + "; ".join(in_flight)
            if in_flight
            else "In-flight:        none"
        ),
        f"External intent:  {'yes' if state.external_intent else 'no'}",
        f"Physical epoch:   {state.physical_epoch}",
        f"State file:       {path}",
    ]
    return "\n".join(lines)


def _systemctl_is_active(unit: str) -> bool:
    """Return whether a systemd user unit is active.

    Raises on failure rather than returning False, so unit_states can record
    the difference between "not running" and "could not tell".
    """
    completed = subprocess.run(  # noqa: S603
        ["systemctl", "--user", "is-active", unit],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.stdout.strip() == "active"


def _print_commands(args: argparse.Namespace) -> int:
    """Print a command sequence without executing any of it."""
    commands = (
        cutover_commands()
        if args.command == "cutover-commands"
        else rollback_commands(args.target)
    )
    for command in commands:
        print(command)
    return 0


def _preflight() -> int:
    """Report whether the active controller can safely take authority.

    Read-only: nothing is stopped, started, enabled, or disabled. Exits
    non-zero when any precondition blocks, so it is usable as a gate in a
    cutover script.
    """
    report = build_preflight_report(
        paths=ActivePaths.from_environment(),
        active_units=unit_states(CONFLICTING_UNITS, _systemctl_is_active),
        ambiguities=(),
        authority_allowed=True,
    )
    print(report.render())
    return 0 if report.ready else 1


# Cutover-related subcommands, dispatched by table so main() stays simple.
_CUTOVER_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "preflight": lambda _args: _preflight(),
    "cutover-commands": _print_commands,
    "rollback-commands": _print_commands,
}


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0911
    """Run only deterministic simulation/replay or read persisted state."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "simulate":
            return _simulate(args.scenario)
        if args.command == "replay":
            return _replay(args.trace)
        if args.command in _CUTOVER_COMMANDS:
            return _CUTOVER_COMMANDS[args.command](args)
        if args.command == "internal":
            if args.internal_command == "tray-diagnostics":
                return run_tray_diagnostics(
                    transaction_root=args.transaction_root,
                    action_id_text=args.action_id,
                    tray_diag=args.tray_diag,
                    output_root=args.output_root,
                )
            common = {
                "transaction_root": args.transaction_root,
                "action_id_text": args.action_id,
                "unit_name": args.unit,
                "sysfs_root": args.sysfs_root,
            }
            if args.internal_command == "probe":
                return run_probe_worker(**common)
            if args.internal_command == "apply":
                return run_apply_worker(**common)
            desktop = {
                **common,
                "plan_root": args.plan_root,
                "home_root": args.home_root,
                "leaf_root": args.leaf_root,
            }
            if args.internal_command == "prepare":
                return run_prepare_worker(**desktop)
            return run_finalize_worker(
                **desktop,
                event_generation_file=args.event_generation_file,
            )
        state_home = args.state_home or _default_state_home()
        return _status(
            state_home,
            StateNamespace(args.namespace),
            as_json=bool(args.json),
        )
    except (
        OSError,
        ReplayFormatError,
        ReplayMismatchError,
        ScenarioAssertionError,
        ScenarioFormatError,
        StateCodecError,
        WorkerStartupError,
    ) as error:
        if args.command == "internal":
            # Worker units capture stderr into journald under their own
            # SyslogIdentifier; the logger adds a real error priority and no
            # misattributed prefix (the old one claimed monitor-controller
            # regardless of which worker failed).
            service_logger("monitor_controller.journal").error(str(error))
        else:
            print(f"monitor-controller {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - console-script path is tested
    raise SystemExit(main())
