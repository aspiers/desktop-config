"""Command-line contracts for simulation, replay, and read-only status."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import pytest

from monitor_controller.cli import main
from monitor_controller.model import (
    BootId,
    ControllerInstanceId,
    DisplayIdentity,
    EventMetadata,
    State,
    TimerFired,
)
from monitor_controller.runtime.persistence import AtomicStateStore, StateNamespace
from monitor_controller.simulation.replay import capture_replay, encode_replay


def _state() -> State:
    return State(
        boot_id=BootId(UUID(int=501)),
        controller_instance=ControllerInstanceId(UUID(int=502)),
        display_identity=DisplayIdentity(":cli"),
    )


def test_simulate_runs_the_strict_scenario_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenarios = Path(__file__).parent / "scenarios" / "reducer-scenarios.json"

    assert main(["simulate", str(scenarios)]) == 0
    output = capsys.readouterr().out

    assert '"scenario_count": 57' in output
    assert '"step_count": 322' in output


def test_replay_uses_the_production_reducer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state()
    event = TimerFired(EventMetadata(1, state.boot_id), 1)
    path = tmp_path / "trace.jsonl"
    path.write_bytes(encode_replay(capture_replay(state, (event,))))

    assert main(["replay", str(path)]) == 0
    output = capsys.readouterr().out

    assert '"decision_count": 1' in output
    assert '"verified": true' in output


def test_internal_probe_entry_point_passes_only_typed_worker_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_probe_worker(**arguments: object) -> int:
        seen.update(arguments)
        return 23

    monkeypatch.setattr(
        "monitor_controller.cli.run_probe_worker",
        fake_probe_worker,
    )
    transaction_root = tmp_path / "transactions"
    sysfs_root = tmp_path / "sysfs"

    assert (
        main(
            [
                "internal",
                "probe",
                "--transaction-root",
                str(transaction_root),
                "--action-id",
                "probe-12345678123456781234567812345678-1",
                "--unit",
                "monitor-probe@test.service",
                "--sysfs-root",
                str(sysfs_root),
            ]
        )
        == 23
    )
    assert seen == {
        "transaction_root": transaction_root,
        "action_id_text": "probe-12345678123456781234567812345678-1",
        "unit_name": "monitor-probe@test.service",
        "sysfs_root": sysfs_root,
    }


def test_internal_prepare_entry_point_passes_all_explicit_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_prepare_worker(**arguments: object) -> int:
        seen.update(arguments)
        return 24

    monkeypatch.setattr(
        "monitor_controller.cli.run_prepare_worker",
        fake_prepare_worker,
    )
    roots = {
        name: tmp_path / name
        for name in ("transactions", "plans", "sysfs", "home", "bin")
    }
    assert (
        main(
            [
                "internal",
                "prepare",
                "--transaction-root",
                str(roots["transactions"]),
                "--plan-root",
                str(roots["plans"]),
                "--action-id",
                "preparation-12345678123456781234567812345678-2",
                "--unit",
                "monitor-prepare@test.service",
                "--sysfs-root",
                str(roots["sysfs"]),
                "--home-root",
                str(roots["home"]),
                "--leaf-root",
                str(roots["bin"]),
            ]
        )
        == 24
    )
    assert seen == {
        "transaction_root": roots["transactions"],
        "plan_root": roots["plans"],
        "action_id_text": "preparation-12345678123456781234567812345678-2",
        "unit_name": "monitor-prepare@test.service",
        "sysfs_root": roots["sysfs"],
        "home_root": roots["home"],
        "leaf_root": roots["bin"],
    }


def test_internal_finalize_entry_point_passes_all_guard_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_finalize_worker(**arguments: object) -> int:
        seen.update(arguments)
        return 25

    monkeypatch.setattr(
        "monitor_controller.cli.run_finalize_worker",
        fake_finalize_worker,
    )
    roots = {
        name: tmp_path / name
        for name in ("transactions", "plans", "events", "sysfs", "home", "bin")
    }
    assert (
        main(
            [
                "internal",
                "finalize",
                "--transaction-root",
                str(roots["transactions"]),
                "--plan-root",
                str(roots["plans"]),
                "--event-generation-file",
                str(roots["events"]),
                "--action-id",
                "finalization-12345678123456781234567812345678-3",
                "--unit",
                "monitor-finalize@test.service",
                "--sysfs-root",
                str(roots["sysfs"]),
                "--home-root",
                str(roots["home"]),
                "--leaf-root",
                str(roots["bin"]),
            ]
        )
        == 25
    )
    assert seen == {
        "transaction_root": roots["transactions"],
        "plan_root": roots["plans"],
        "event_generation_file": roots["events"],
        "action_id_text": "finalization-12345678123456781234567812345678-3",
        "unit_name": "monitor-finalize@test.service",
        "sysfs_root": roots["sysfs"],
        "home_root": roots["home"],
        "leaf_root": roots["bin"],
    }


def test_internal_tray_diagnostics_has_no_worker_or_display_discovery_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_diagnostics(**arguments: object) -> int:
        seen.update(arguments)
        return 26

    monkeypatch.setattr(
        "monitor_controller.cli.run_tray_diagnostics",
        fake_diagnostics,
    )
    assert (
        main(
            [
                "internal",
                "tray-diagnostics",
                "--transaction-root",
                str(tmp_path / "transactions"),
                "--action-id",
                "finalization-12345678123456781234567812345678-3",
                "--tray-diag",
                str(tmp_path / "bin" / "tray-diag"),
                "--output-root",
                str(tmp_path / "diagnostics"),
            ]
        )
        == 26
    )
    assert seen == {
        "transaction_root": tmp_path / "transactions",
        "action_id_text": "finalization-12345678123456781234567812345678-3",
        "tray_diag": tmp_path / "bin" / "tray-diag",
        "output_root": tmp_path / "diagnostics",
    }


def test_status_reads_selected_namespace_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = AtomicStateStore(tmp_path, StateNamespace.SHADOW)
    store.save(_state())
    before = store.path.read_bytes()
    before_stat = store.path.stat()

    result = main(
        [
            "status",
            "--namespace",
            "shadow",
            "--state-home",
            str(tmp_path),
        ]
    )
    output = capsys.readouterr().out
    after_stat = store.path.stat()

    assert result == 0
    assert '"namespace": "shadow"' in output
    assert '"phase": "recovering"' in output
    assert store.path.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_ino == before_stat.st_ino
