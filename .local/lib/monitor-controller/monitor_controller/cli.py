"""Non-mutating command-line skeleton for the monitor controller."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


class _Parser(argparse.ArgumentParser):
    """Argument parser which reports errors without any domain side effects."""


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="monitor-controller")
    subparsers = parser.add_subparsers(dest="command", required=True)

    simulate = subparsers.add_parser("simulate", help="simulate a scenario")
    simulate.add_argument("scenario")

    replay = subparsers.add_parser("replay", help="replay a JSONL trace")
    replay.add_argument("trace")

    subparsers.add_parser("status", help="show controller status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Report the selected future command without touching the live display."""
    args = _parser().parse_args(argv)
    command = str(args.command)
    print(f"monitor-controller {command}: not implemented")
    return 2


if __name__ == "__main__":  # pragma: no cover - console-script path is tested
    raise SystemExit(main())
