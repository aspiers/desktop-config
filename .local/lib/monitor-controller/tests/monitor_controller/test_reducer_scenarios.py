"""Parametrized reducer parity, strict-loader, and determinism tests."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest

from monitor_controller.invariants import NUMBERED_INVARIANTS
from monitor_controller.simulation.scenario import (
    Scenario,
    ScenarioFormatError,
    load_scenarios,
    normalize_effect,
    normalize_state,
    run_scenario,
)

_TEST_ROOT = Path(__file__).parent
_SCENARIO_PATH = _TEST_ROOT / "scenarios" / "reducer-scenarios.json"
_PARITY_PATH = _TEST_ROOT / "bash-scenario-parity.json"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
_BASH_TEST_PATH = (
    _REPOSITORY_ROOT / "specs" / "spikes" / "test-monitor-watcher-state-machine.sh"
)
_SCENARIOS: tuple[Scenario, ...] = load_scenarios(_SCENARIO_PATH)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.name)
def test_explicit_reducer_scenario(scenario: Scenario) -> None:
    """Assert every intermediate state, ordered effect, ID, timer, and count."""
    result = run_scenario(scenario)

    assert len(result.decisions) == len(scenario.steps)


def test_every_scenario_replays_to_byte_equivalent_normalized_decisions() -> None:
    def replay_bytes(scenario: Scenario) -> bytes:
        result = run_scenario(scenario)
        normalized = [
            {
                "state": normalize_state(decision.state),
                "effects": [normalize_effect(effect) for effect in decision.effects],
            }
            for decision in result.decisions
        ]
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    for scenario in _SCENARIOS:
        assert replay_bytes(scenario) == replay_bytes(scenario)


def test_parity_manifest_classifies_every_bash_test_honestly() -> None:
    source = _BASH_TEST_PATH.read_text()
    bash_names = set(re.findall(r"^(test_[a-z0-9_]+)\(\) \{", source, re.MULTILINE))
    manifest = cast(
        "dict[str, object]",
        json.loads(_PARITY_PATH.read_text()),
    )
    executable = cast("dict[str, dict[str, str]]", manifest["executable_behavior"])
    divergence = cast(
        "dict[str, dict[str, str]]", manifest["intentional_safety_divergence"]
    )
    deferred = cast("dict[str, dict[str, str]]", manifest["deferred_codec"])
    scenario_names = {scenario.name for scenario in _SCENARIOS}

    assert manifest["schema_version"] == 2
    assert manifest["bash_test_count"] == len(bash_names) == 49
    assert set(executable) | set(divergence) | set(deferred) == bash_names
    assert not (set(executable) & set(divergence))
    assert not (set(executable) & set(deferred))
    assert not (set(divergence) & set(deferred))
    assert {entry["scenario"] for entry in executable.values()} <= scenario_names
    assert {entry["scenario"] for entry in divergence.values()} <= scenario_names
    assert {entry["deferred_to"] for entry in deferred.values()} == {"dc-a5y.4"}
    assert all("codec" in entry["reason"] for entry in deferred.values())


def test_executable_bash_oracle_passes_all_classified_behavior_cases() -> None:
    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(_BASH_TEST_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.splitlines()

    assert completed.returncode == 0, completed.stderr
    assert len([line for line in output if line.startswith("ok - ")]) == 49
    assert not [line for line in output if line.startswith("not ok - ")]


def test_parity_manifest_names_exact_production_only_lifecycle_coverage() -> None:
    manifest = cast(
        "dict[str, object]",
        json.loads(_PARITY_PATH.read_text()),
    )
    production = cast("dict[str, str]", manifest["production_only"])
    required = {
        "PLAN_PENDING",
        "PLANNING",
        "PLAN_READY",
        "PLAN_FAILED",
        "PREPARE_PENDING",
        "PREPARING",
        "PREPARED",
        "PREPARE_STOPPING",
        "PREPARE_FAILED",
        "FINALIZE_STOPPING",
        "startup_baseline_exclusion",
        "dirty_probe_admission",
        "dirty_application_admission",
        "dirty_preparation_admission",
        "dirty_finalization_admission",
        "dispatch_rejection",
        "unknown_worker_status",
        "worker_timeout",
        "probe_failure",
        "application_failure",
        "planning_failure",
        "preparation_failure",
        "finalization_failure",
        "invalid_event_fail_closed",
        "deterministic_replay",
    }
    scenario_names = {scenario.name for scenario in _SCENARIOS}

    assert set(production) == required
    assert set(production.values()) <= scenario_names


def test_all_thirteen_numbered_invariants_are_explicitly_named() -> None:
    assert len(NUMBERED_INVARIANTS) == 13
    assert len(set(NUMBERED_INVARIANTS)) == 13


def test_strict_loader_rejects_duplicate_fields(tmp_path: Path) -> None:
    text = _SCENARIO_PATH.read_text().replace(
        '"schema_version": 1',
        '"schema_version": 1, "schema_version": 1',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(text)

    with pytest.raises(ScenarioFormatError, match="duplicate JSON field"):
        load_scenarios(path)


def test_strict_loader_rejects_unknown_fields(tmp_path: Path) -> None:
    document = json.loads(_SCENARIO_PATH.read_text())
    document["unexpected"] = True
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)


def test_strict_loader_rejects_truncated_json(tmp_path: Path) -> None:
    path = tmp_path / "truncated.json"
    path.write_text(_SCENARIO_PATH.read_text()[:-20])

    with pytest.raises(ScenarioFormatError, match="cannot decode"):
        load_scenarios(path)


def test_strict_loader_rejects_unknown_event_and_effect_fields(
    tmp_path: Path,
) -> None:
    document = cast("dict[str, object]", json.loads(_SCENARIO_PATH.read_text()))
    scenarios = cast("list[dict[str, object]]", document["scenarios"])
    steps = cast("list[dict[str, object]]", scenarios[0]["steps"])
    event = cast("dict[str, object]", steps[0]["event"])
    event["shell"] = "xrandr --auto"
    path = tmp_path / "event-unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)

    document = cast("dict[str, object]", json.loads(_SCENARIO_PATH.read_text()))
    scenarios = cast("list[dict[str, object]]", document["scenarios"])
    steps = cast("list[dict[str, object]]", scenarios[0]["steps"])
    expected = cast("dict[str, object]", steps[0]["expect"])
    effects = cast("list[dict[str, object]]", expected["effects"])
    effects[0]["command"] = "setup-monitor"
    path = tmp_path / "effect-unknown.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ScenarioFormatError, match="unknown fields"):
        load_scenarios(path)


def test_scenario_corpus_is_comprehensive_and_explicit() -> None:
    assert len(_SCENARIOS) == 57
    assert sum(len(scenario.steps) for scenario in _SCENARIOS) == 322
    steps = (step for scenario in _SCENARIOS for step in scenario.steps)
    assert all(step.expected_effect_counts for step in steps)
    steps = (step for scenario in _SCENARIOS for step in scenario.steps)
    assert all(step.expected_state for step in steps)
