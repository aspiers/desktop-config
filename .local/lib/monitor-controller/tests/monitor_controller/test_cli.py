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
