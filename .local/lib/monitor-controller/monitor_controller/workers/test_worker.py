"""Harmless worker used only to exercise the real systemd contract."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from monitor_controller.model import ActionKind, ActionLifecycle, PhysicalToken
from monitor_controller.runtime.transactions import (
    TransactionProtocolError,
    TransactionRequest,
)
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerExecution,
    WorkerStartup,
    execute_worker,
    install_cooperative_sigterm_handler,
    validate_worker_startup,
    write_worker_result,
)

MAX_TEST_DELAY_MS = 300_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harmless systemd contract worker")
    parser.add_argument("--transaction-root", required=True, type=Path)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument(
        "--action-kind",
        required=True,
        choices=tuple(kind.value for kind in ActionKind if kind is not ActionKind.PLAN),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate one request, perform no desktop I/O, and report test behavior."""
    arguments = _parser().parse_args(argv)
    kind = ActionKind(arguments.action_kind)
    startup = validate_worker_startup(
        transaction_root=arguments.transaction_root,
        action_id_text=arguments.action_id,
        unit_name=arguments.unit,
        expected_kind=kind,
    )
    # Make accidental display access impossible even if future test code changes.
    for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
        os.environ.pop(name, None)
    if (
        _optional_string(startup.request, "test_behavior", default="success")
        == "completed_result_barrier"
    ):
        return _completed_result_barrier(startup)
    return execute_worker(
        startup,
        topology_reader=_test_topology,
        implementation=_run_behavior,
    )


def _completed_result_barrier(startup: WorkerStartup) -> int:
    """Install success, then remain alive so a harmless stop can win the race."""
    request = startup.request
    ready_path = Path(_optional_string(request, "ready_path", default=""))
    release_path = Path(_optional_string(request, "release_path", default=""))
    if not ready_path.is_absolute() or not release_path.is_absolute():
        msg = "completed-result barrier paths must be absolute"
        raise TransactionProtocolError(msg)
    now_ms = time.monotonic_ns() // 1_000_000
    write_worker_result(
        startup,
        execution=WorkerExecution(
            ActionLifecycle.COMPLETED,
            0,
            "harmless completion installed before process exit",
        ),
        started_monotonic_ms=now_ms,
        finished_monotonic_ms=now_ms,
    )
    ready_path.write_text("ready\n", encoding="ascii")
    deadline = time.monotonic() + MAX_TEST_DELAY_MS / 1_000
    while not release_path.exists():
        if time.monotonic() >= deadline:
            msg = "completed-result barrier timed out"
            raise TransactionProtocolError(msg)
        time.sleep(0.01)
    return 0


def _test_topology(request: TransactionRequest) -> CurrentTopology:
    mismatch = _optional_bool(request, "topology_mismatch", default=False)
    token = request.physical_token
    if mismatch:
        token = PhysicalToken(f"{token.value}-changed")
    return CurrentTopology(token, request.expected_topology)


def _run_behavior(request: TransactionRequest) -> WorkerExecution:
    behavior = _optional_string(request, "test_behavior", default="success")
    delay_ms = _optional_int(request, "delay_ms", default=0)
    spawn_child = _optional_bool(request, "spawn_child", default=False)
    if behavior == "fail":
        return WorkerExecution(ActionLifecycle.FAILED, 23, "requested harmless failure")
    if behavior == "cooperative":
        install_cooperative_sigterm_handler()
    elif behavior == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif behavior not in {"success", "rapid", "sleep"}:
        msg = f"unknown harmless test behavior: {behavior}"
        raise TransactionProtocolError(msg)
    child: subprocess.Popen[bytes] | None = None
    if spawn_child:
        child = subprocess.Popen(
            (
                sys.executable,
                "-I",
                "-c",
                (
                    "import signal,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(300)"
                ),
            ),
            shell=False,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    try:
        if delay_ms:
            time.sleep(delay_ms / 1_000)
    finally:
        # A normal test-worker exit cleans up its helper.  The ignore-term contract
        # intentionally never reaches here before systemd kills the entire cgroup.
        if child is not None and behavior != "ignore_term":
            child.terminate()
            child.wait(timeout=5)
    return WorkerExecution(ActionLifecycle.COMPLETED, 0, "harmless contract completed")


def _optional_string(request: TransactionRequest, name: str, *, default: str) -> str:
    value = _payload_or_default(request, name, default)
    if not isinstance(value, str):
        msg = f"test payload {name} must be a string"
        raise TransactionProtocolError(msg)
    return value


def _optional_int(request: TransactionRequest, name: str, *, default: int) -> int:
    value = _payload_or_default(request, name, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= MAX_TEST_DELAY_MS
    ):
        msg = f"test payload {name} must be a bounded non-negative integer"
        raise TransactionProtocolError(msg)
    return value


def _optional_bool(request: TransactionRequest, name: str, *, default: bool) -> bool:
    value = _payload_or_default(request, name, default)
    if not isinstance(value, bool):
        msg = f"test payload {name} must be Boolean"
        raise TransactionProtocolError(msg)
    return value


def _payload_or_default(
    request: TransactionRequest, name: str, default: object
) -> object:
    for key, value in request.payload:
        if key == name:
            return value
    return default


if __name__ == "__main__":
    raise SystemExit(main())
