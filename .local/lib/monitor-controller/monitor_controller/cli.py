"""Read-only status and deterministic simulation/replay command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from monitor_controller.codec import StateCodecError, decode_state
from monitor_controller.runtime.persistence import StateNamespace
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


def _status(state_home: Path, namespace: StateNamespace) -> int:
    path = state_home / "monitor-controller" / namespace.value / "state.json"
    if not path.is_file():
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
    print(json.dumps(status, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run only deterministic simulation/replay or read persisted state."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "simulate":
            return _simulate(args.scenario)
        if args.command == "replay":
            return _replay(args.trace)
        if args.command == "internal":
            worker = (
                run_probe_worker
                if args.internal_command == "probe"
                else run_apply_worker
            )
            return worker(
                transaction_root=args.transaction_root,
                action_id_text=args.action_id,
                unit_name=args.unit,
                sysfs_root=args.sysfs_root,
            )
        state_home = args.state_home or _default_state_home()
        return _status(state_home, StateNamespace(args.namespace))
    except (
        OSError,
        ReplayFormatError,
        ReplayMismatchError,
        ScenarioAssertionError,
        ScenarioFormatError,
        StateCodecError,
        WorkerStartupError,
    ) as error:
        print(f"monitor-controller {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - console-script path is tested
    raise SystemExit(main())
