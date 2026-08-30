"""Provenance, live evidence, and policy replay checks for dc-a5y.11."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest

from monitor_controller.invariants import NUMBERED_INVARIANTS
from monitor_controller.model import Event, ObservationCompleted, Schedule
from monitor_controller.simulation.replay import (
    ReplayTrace,
    capture_replay,
    decode_replay,
    encode_replay,
    replay,
)
from monitor_controller.simulation.scenario import (
    Scenario,
    event_from_data,
    initial_state,
    load_scenarios,
    normalize_effect,
    run_scenario,
)

_TEST_ROOT = Path(__file__).parent
_SCENARIO_PATH = _TEST_ROOT / "scenarios" / "shadow-trace-scenarios.json"
_TRACE_ROOT = _TEST_ROOT / "fixtures" / "traces"
_MANIFEST_PATH = _TRACE_ROOT / "manifest.json"
_LIVE_AUDIT_PATH = _TRACE_ROOT / "live_samsung_restart_steady.audit.jsonl"
_LIVE_EVIDENCE_PATH = _TRACE_ROOT / "live_samsung_restart_steady.evidence.json"
_NG_EVIDENCE_PATH = _TRACE_ROOT / "ng-retained-excerpts.json"
_XRANDR_ROOT = _TEST_ROOT / "fixtures" / "xrandr"
_SCENARIOS = load_scenarios(_SCENARIO_PATH)
_LIVE_PHYSICAL_CASES = (
    "aoc_connector_rename",
    "controller_restart",
    "genuine_unplug",
    "same_profile_suspend_resume",
    "samsung_broken_edid_beyond_30_seconds",
    "samsung_plug",
)
_DISPATCH_EVENT_TYPES = {
    "ProbeDispatched",
    "ApplicationDispatched",
    "PreparationDispatched",
    "FinalizationDispatched",
}
_SCENARIO_BY_NAME = {scenario.name: scenario for scenario in _SCENARIOS}
_EXPECTED_CASES = {
    "aoc_connector_rename",
    "genuine_unplug",
    "laptop_startup",
    "restart_unresolved",
    "restart_verification",
    "same_profile_suspend_resume",
    "samsung_broken_edid_beyond_30_seconds",
    "samsung_plug",
}
_EXPECTED_PHYSICAL_CASES = {
    "aoc_connector_rename",
    "controller_restart",
    "genuine_unplug",
    "laptop_startup",
    "same_profile_suspend_resume",
    "samsung_broken_edid_beyond_30_seconds",
    "samsung_plug",
}
_AOC_RENAME_PHYSICAL_ID = "aoc-saved-DisplayPort-2-live-DisplayPort-1"
_EFFECT_NAMES = (
    "activate_probe",
    "apply_profile",
    "discard_plan",
    "finalize_desktop",
    "prepare_desktop",
    "request_observation",
    "request_plan",
    "schedule",
    "stop_action",
)
_ALLOWED_WOULD_KINDS = {
    "WOULD_APPLY",
    "WOULD_FINALIZE",
    "WOULD_PREPARE",
    "WOULD_PROBE",
}


def _load_object(path: Path) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(path.read_text()))


def _manifest() -> dict[str, object]:
    return _load_object(_MANIFEST_PATH)


def _cases() -> tuple[dict[str, object], ...]:
    manifest = _manifest()
    return tuple(cast("list[dict[str, object]]", manifest["cases"]))


def _jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return tuple(
        cast("dict[str, object]", json.loads(line))
        for line in path.read_text().splitlines()
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _scenario_trace(scenario: Scenario) -> ReplayTrace:
    result = run_scenario(scenario)
    state = initial_state(scenario.initial)
    events: list[Event] = []
    for step, decision in zip(scenario.steps, result.decisions, strict=True):
        events.append(event_from_data(step.event_data, state))
        state = decision.state
    return capture_replay(
        initial_state(scenario.initial),
        events,
        provenance="synthetic_policy",
        trace_semantics="scenario_replay_not_production_audit",
    )


def _nested_dicts(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        item = cast("dict[str, object]", value)
        yield item
        for child in item.values():
            yield from _nested_dicts(child)
    elif isinstance(value, list):
        for child in cast("list[object]", value):
            yield from _nested_dicts(child)


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.name)
def test_synthetic_policy_scenario_checks_every_step(scenario: Scenario) -> None:
    result = run_scenario(scenario)

    assert len(result.decisions) == len(scenario.steps)
    timestamps = [cast("int", step.event_data["at_ms"]) for step in scenario.steps]
    assert timestamps == sorted(timestamps)
    assert all(step.expected_effect_counts for step in scenario.steps)
    assert any(
        tag.startswith("policy-regression:dc-a5y.11:") for tag in scenario.covers
    )
    assert not any(tag.startswith("acceptance:dc-a5y.11:") for tag in scenario.covers)


def test_manifest_separates_provenance_and_keeps_live_acceptance_honest() -> None:
    manifest = _manifest()
    cases = _cases()
    provenance = cast("dict[str, object]", manifest["provenance_classes"])
    acceptance = cast("dict[str, object]", manifest["acceptance"])

    assert manifest["schema_version"] == 2
    assert manifest["bead"] == "dc-a5y.11"
    assert set(provenance) == {"live", "retained_raw_derived", "synthetic_policy"}
    assert {cast("str", case["case"]) for case in cases} == _EXPECTED_CASES
    assert set(_SCENARIO_BY_NAME) == {cast("str", case["scenario"]) for case in cases}
    assert set(cast("list[str]", acceptance["live_satisfied_cases"])) == {
        "samsung_restart_steady",
        *_LIVE_PHYSICAL_CASES,
    }
    assert set(
        cast("list[str]", acceptance["live_unsatisfied_physical_transition_cases"])
    ) == {"laptop_startup"}
    assert acceptance["required_physical_transition_case_count"] == 7
    assert acceptance["status"] == "AWAITING_LAPTOP_STARTUP_CAPTURE"


def test_every_policy_trace_replays_with_exact_effect_counts_and_timers() -> None:
    for case in _cases():
        scenario = _SCENARIO_BY_NAME[cast("str", case["scenario"])]
        fixture = _TRACE_ROOT / cast("str", case["trace"])
        encoded = fixture.read_bytes()
        decoded = decode_replay(encoded)
        assert decoded.provenance == "synthetic_policy"
        assert decoded.trace_semantics == "scenario_replay_not_production_audit"
        first = replay(decoded)
        second = replay(decoded)
        regenerated = _scenario_trace(scenario)
        effect_counts: Counter[str] = Counter()
        deadlines: list[int] = []
        phases: list[str] = []
        for decision in first:
            phases.append(decision.state.phase.value)
            for effect in decision.effects:
                normalized = normalize_effect(effect)
                effect_counts[cast("str", normalized["type"])] += 1
                if isinstance(effect, Schedule):
                    deadlines.append(effect.deadline_ms)

        assert first == second
        assert encode_replay(regenerated) == encoded
        assert phases == case["phases"]
        assert deadlines == case["timer_deadlines_ms"]
        assert {name: effect_counts[name] for name in _EFFECT_NAMES} == case[
            "effect_counts"
        ]


def test_policy_cases_name_invariants_without_claiming_live_acceptance() -> None:
    accepted = set(NUMBERED_INVARIANTS)
    for case in _cases():
        invariants = set(cast("list[str]", case["superseded_by_invariants"]))
        comparison = cast("str", case["comparison"])
        sources = cast("dict[str, object]", case["source_evidence"])
        shadow_source = cast("dict[str, object]", sources["shadow_transition"])

        assert invariants
        assert invariants <= accepted
        assert comparison in {
            "intentional_supersession_policy_only",
            "synthetic_policy_only",
        }
        assert case["provenance"] == "synthetic_policy"
        assert case["trace_semantics"] == "scenario_replay_not_production_audit"
        assert case["live_acceptance"] == "NOT_SATISFIED"
        assert shadow_source["status"] == "unavailable"

        scenario = _SCENARIO_BY_NAME[cast("str", case["scenario"])]
        scenario_invariants = {
            tag.removeprefix("invariant:")
            for tag in scenario.covers
            if tag.startswith("invariant:")
        }
        assert invariants <= scenario_invariants


def test_slow_broken_edid_policy_outlives_ng_budget_without_duplicate_probe() -> None:
    case = next(
        item
        for item in _cases()
        if item["case"] == "samsung_broken_edid_beyond_30_seconds"
    )
    trace = decode_replay((_TRACE_ROOT / cast("str", case["trace"])).read_bytes())
    decisions = replay(trace)
    effect_counts = cast("dict[str, int]", case["effect_counts"])

    assert case["timer_deadlines_ms"] == [2, 35_000, 35_000]
    assert effect_counts["activate_probe"] == 1
    assert effect_counts["apply_profile"] == 1
    assert decisions[-1].state.next_timer_ms == 35_000
    assert decisions[-1].state.external_intent


def test_aoc_policy_records_real_saved_to_live_connector_mapping() -> None:
    scenario = _SCENARIO_BY_NAME["shadow_aoc_connector_rename"]
    result = run_scenario(scenario)
    first_event = scenario.steps[0].event_data
    trace = decode_replay((_TRACE_ROOT / "aoc_connector_rename.jsonl").read_bytes())
    replay_event = trace.steps[0].event

    assert first_event["physical_token"] == _AOC_RENAME_PHYSICAL_ID
    assert first_event["target_profile"] == "celtic+AOC-U28G2G6B"
    assert first_event["external_outputs"] == ["DisplayPort-1"]
    assert first_event["output_mappings"] == [
        {"live_output": "DisplayPort-1", "saved_output": "DisplayPort-2"},
        {"live_output": "eDP-1", "saved_output": "eDP"},
    ]
    assert isinstance(replay_event, ObservationCompleted)
    mapping = replay_event.observation.eligible_profiles[0].mapping
    assert ("DisplayPort-2", "DisplayPort-1") in {
        (item.saved_output, item.live_output) for item in mapping
    }
    assert not any(
        normalized["type"] in {"apply_profile", "finalize_desktop"}
        for decision in result.decisions
        for normalized in (normalize_effect(effect) for effect in decision.effects)
    )


def test_retained_xrandr_fixture_is_derived_and_keeps_only_edid_hashes() -> None:
    properties = (_XRANDR_ROOT / "live-samsung-20260822.props").read_text()
    provenance = _load_object(_XRANDR_ROOT / "live-samsung-20260822.provenance.json")
    edids = cast("dict[str, dict[str, object]]", provenance["edids"])

    assert provenance["provenance"] == "retained_raw_derived"
    assert "not a physical transition trace" in cast(
        "str", provenance["acceptance_limit"]
    )
    assert not re.search(r"(?m)^\s+[0-9a-f]{32}\s*$", properties)
    assert properties.count("EDID_SHA256:") == 2
    assert all(
        isinstance(item["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None
        and cast("int", item["bytes"]) >= 128
        for item in edids.values()
    )


def test_policy_jsonl_is_explicitly_scenario_replay_not_production_audit() -> None:
    for case in _cases():
        path = _TRACE_ROOT / cast("str", case["trace"])
        records = _jsonl(path)

        assert {record["record"] for record in records} == {"header", "decision"}
        header = records[0]
        assert header["provenance"] == "synthetic_policy"
        assert header["trace_semantics"] == "scenario_replay_not_production_audit"
        assert all("audit" not in record for record in records)
        assert case["provenance"] == "synthetic_policy"
        assert case["live_acceptance"] == "NOT_SATISFIED"


def test_live_audit_capture_retains_source_bounds_and_hash_length_redaction() -> None:
    payload = _LIVE_AUDIT_PATH.read_bytes()
    evidence = _load_object(_LIVE_EVIDENCE_PATH)
    audit = cast("dict[str, object]", evidence["audit"])
    records = _jsonl(_LIVE_AUDIT_PATH)
    source_lines = [cast("dict[str, object]", record["capture"]) for record in records]

    assert evidence["provenance"] == "live"
    assert len(payload) == audit["fixture_bytes"]
    assert _sha256(payload) == audit["fixture_sha256"]
    assert [item["source_line"] for item in source_lines] == list(range(1, 19))
    assert all(item["provenance"] == "live" for item in source_lines)
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", cast("str", item["source_line_sha256"]))
        is not None
        for item in source_lines
    )
    assert Counter(record["record"] for record in records) == {
        "header": 1,
        "decision": 16,
        "would_dispatch": 1,
    }
    assert max(map(len, re.findall(r"[0-9a-f]+", payload.decode()))) == 64

    redactions = {
        (
            cast("str", item["sha256"]),
            cast("int", item["bytes"]),
        )
        for record in records
        for item in _nested_dicts(record)
        if set(item) == {"bytes", "sha256"}
    }
    assert redactions == {
        ("0dc9ed31bd18093d4e8feda96f2cfd10eb4e96eff08fccc15cb9a17f8eaa52fc", 400),
        ("64abdc84a830f9b8d8bc0a1496b251ea28c03f1942cb86d90e3889d03f23436b", 384),
    }


def test_live_audit_has_exact_samsung_observations_and_allowed_would_kinds() -> None:
    records = _jsonl(_LIVE_AUDIT_PATH)
    decisions = [record for record in records if record["record"] == "decision"]
    audits = [cast("dict[str, object]", record["audit"]) for record in decisions]
    observations = [
        cast(
            "dict[str, object]",
            cast("dict[str, object]", record["event"])["observation"],
        )
        for record in decisions
        if "observation" in cast("dict[str, object]", record["event"])
    ]
    profile = "celtic+Samsung-Odyssey-G75F"

    assert all(
        set(audit)
        == {
            "monotonic_ms",
            "prior_state_key",
            "resulting_state_key",
            "timing",
            "wake_reason",
        }
        for audit in audits
    )
    assert all(
        set(cast("dict[str, object]", audit["timing"]))
        == {
            "command_duration_ms",
            "observation_duration_ms",
            "persistence_finished_ms",
            "processing_started_ms",
            "reduction_finished_ms",
            "worker_duration_ms",
        }
        for audit in audits
    )
    assert all(
        isinstance(audit["wake_reason"], str) and audit["wake_reason"]
        for audit in audits
    )
    assert all(
        first["resulting_state_key"] == second["prior_state_key"]
        for first, second in pairwise(audits)
    )
    observation_durations = [
        cast("dict[str, object]", audit["timing"])["observation_duration_ms"]
        for audit in audits
        if audit["wake_reason"] == "ObservationCompleted"
    ]
    assert observation_durations == [240, 221, 190, 207, 195]
    assert len(observations) == 5
    assert all(observation["validity"] == "valid" for observation in observations)
    assert all(observation["exact_profile"] == profile for observation in observations)
    assert all(
        observation["current_profiles"] == [profile] for observation in observations
    )

    would_kinds = [
        cast("str", record["kind"])
        for record in records
        if record["record"] == "would_dispatch"
    ]
    assert set(would_kinds) <= _ALLOWED_WOULD_KINDS
    assert would_kinds == ["WOULD_PREPARE"]
    admitted = [
        cast("str", effect["effect_type"])
        for record in decisions
        for effect in cast("list[dict[str, object]]", record["effects"])
        if effect["effect_type"] in {"RequestPlan", "PrepareDesktop"}
    ]
    assert admitted == ["RequestPlan", "PrepareDesktop"]


def test_zero_side_effect_claims_come_from_preserved_live_evidence() -> None:
    evidence = _load_object(_LIVE_EVIDENCE_PATH)
    zero = cast("dict[str, object]", evidence["zero_side_effect_evidence"])
    services = cast("dict[str, dict[str, object]]", evidence["service_evidence"])
    xrandr = cast("dict[str, object]", evidence["xrandr_query"])

    assert zero["shadow_transaction_path_state"] == "absent"
    assert zero["shadow_transaction_path_query_exit_status"] == 0
    assert zero["monitor_worker_unit_count"] == 0
    assert zero["display_mutation_by_collection"] is False
    unit_stdout = cast("str", zero["unit_query_stdout"]).encode()
    assert len(unit_stdout) == zero["unit_query_stdout_bytes"]
    assert _sha256(unit_stdout) == zero["unit_query_stdout_sha256"]
    assert unit_stdout.decode().splitlines() == [
        (
            "monitor-controller-shadow.service loaded active running "
            "Non-authoritative monitor controller shadow"
        )
    ]

    shadow = services["shadow"]
    ng = services["authoritative_ng"]
    assert (shadow["active_state"], shadow["sub_state"], shadow["main_pid"]) == (
        "active",
        "running",
        3975780,
    )
    assert shadow["n_restarts"] == 0
    assert (ng["active_state"], ng["sub_state"], ng["main_pid"]) == (
        "active",
        "running",
        2734905,
    )
    assert ng["n_restarts"] == 0
    assert xrandr["unchanged"] is True
    assert xrandr["before_restart_sha256"] == xrandr["after_restart_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", cast("str", xrandr["after_restart_sha256"]))


def test_retained_ng_excerpts_have_exact_hashes_and_explicit_gaps() -> None:
    evidence = _load_object(_NG_EVIDENCE_PATH)
    cases = cast("dict[str, dict[str, object]]", evidence["cases"])

    assert evidence["provenance"] == "retained_raw_derived"
    assert set(cases) == _EXPECTED_PHYSICAL_CASES
    for case in cases.values():
        chunks = cast(
            "list[dict[str, object]]",
            case.get("excerpts", case.get("retained_analogue_not_case_evidence", [])),
        )
        for chunk in chunks:
            lines = cast("list[str]", chunk["lines"])
            payload = ("\n".join(lines) + "\n").encode()
            assert len(lines) == chunk["line_count"]
            assert _sha256(payload) == chunk["sha256"]
            assert cast("list[str]", chunk["selected_monotonic_range"])[0] in lines[0]
            assert cast("list[str]", chunk["selected_monotonic_range"])[1] in lines[-1]

    same_profile = cases["same_profile_suspend_resume"]
    restart = cases["controller_restart"]
    assert same_profile["case_source_status"] == "unavailable"
    assert "retained_analogue_not_case_evidence" in same_profile
    assert restart["case_source_status"] == "unavailable"
    assert "excerpts" not in restart

    aoc_lines = "\n".join(
        line
        for excerpt in cast(
            "list[dict[str, object]]", cases["aoc_connector_rename"]["excerpts"]
        )
        for line in cast("list[str]", excerpt["lines"])
    )
    assert "renaming display DisplayPort-2 to DisplayPort-1" in aoc_lines
    assert '"mapped": "DisplayPort-1"' in aoc_lines


def _live_records(case: str) -> list[dict[str, object]]:
    path = _TRACE_ROOT / f"live_{case}.audit.jsonl"
    return [
        cast("dict[str, object]", json.loads(line))
        for line in path.read_text(encoding="ascii").splitlines()
    ]


def _live_evidence(case: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads((_TRACE_ROOT / f"live_{case}.evidence.json").read_text()),
    )


@pytest.mark.parametrize("case", _LIVE_PHYSICAL_CASES)
def test_live_physical_capture_is_redacted_and_dispatch_free(case: str) -> None:
    """Every accepted live capture keeps EDIDs hashed and proves null dispatch.

    Shadow mode must never have started a worker, so a live trace may admit
    effects but every admission must have been rejected by NullDispatcher and
    no worker-dispatch acknowledgement may appear.
    """
    records = _live_records(case)
    assert records, case
    event_types: set[str] = set()
    for record in records:
        for holder in (
            cast("dict[str, object]", record.get("state") or {}),
            cast("dict[str, object]", record.get("initial_state") or {}),
            cast("dict[str, object]", record.get("event") or {}),
        ):
            observation = cast(
                "dict[str, object]", holder.get("observation") or holder
            )
            for item in cast(
                "list[dict[str, object]]",
                observation.get("live_fingerprints") or [],
            ):
                assert "value" not in item, case
                redaction = cast("dict[str, object]", item["value_redaction"])
                assert re.fullmatch(
                    r"[0-9a-f]{64}", cast("str", redaction["sha256"])
                )
                assert cast("int", redaction["bytes"]) > 0
        if record.get("record") == "decision":
            event_types.add(cast("str", record["event_type"]))
    assert not event_types & _DISPATCH_EVENT_TYPES, case

    evidence = _live_evidence(case)
    assert evidence["shadow_transactions_directory_absent"] is True
    provenance = cast("list[dict[str, object]]", evidence["record_provenance"])
    assert len(provenance) == len(records)


@pytest.mark.parametrize(
    ("case", "would_kinds", "profile"),
    [
        ("samsung_plug", {"WOULD_PREPARE"}, "celtic+Samsung-Odyssey-G75F"),
        (
            "samsung_broken_edid_beyond_30_seconds",
            {"WOULD_PREPARE"},
            "celtic+Samsung-Odyssey-G75F",
        ),
        ("genuine_unplug", {"WOULD_APPLY"}, "celtic"),
        (
            "same_profile_suspend_resume",
            cast("set[str]", set()),
            "celtic+Samsung-Odyssey-G75F",
        ),
        ("controller_restart", {"WOULD_PREPARE"}, "celtic+Samsung-Odyssey-G75F"),
        ("aoc_connector_rename", {"WOULD_PREPARE"}, "celtic+AOC-U28G2G6B"),
    ],
)
def test_live_physical_capture_policy_agrees_with_ng(
    case: str, would_kinds: set[str], profile: str
) -> None:
    """Each accepted capture's policy outcome matches the retained ng window.

    Agreement means the ng journal shows the same profile being applied (or
    already current); the one intentional supersession is same-profile
    resume, where the controller emits no desktop intent while ng reloads
    unconditionally.
    """
    records = _live_records(case)
    seen_kinds = {
        cast("str", record["kind"])
        for record in records
        if record.get("record") == "would_dispatch"
    }
    assert seen_kinds == would_kinds, case

    evidence = _live_evidence(case)
    journal = cast("dict[str, object]", evidence["ng_journal"])
    lines = [
        cast("str", cast("dict[str, object]", item)["line"])
        for item in cast("list[object]", journal["lines"])
    ]
    if case == "genuine_unplug":
        needle = "Reloading previously matched autorandr profile celtic "
    else:
        needle = f"autorandr profile {profile}"
    assert any(needle in line for line in lines), case

    if case == "same_profile_suspend_resume":
        mutating = {"RequestPlan", "PrepareDesktop", "ApplyProfile", "ActivateProbe"}
        admitted = {
            cast("str", cast("dict[str, object]", effect)["effect_type"])
            for record in records
            if record.get("record") == "decision"
            for effect in cast("list[object]", record.get("effects") or [])
        }
        assert not admitted & mutating, admitted


def test_live_capture_manifest_hashes_are_current() -> None:
    """The manifest's live capture hashes must match the checked-in fixtures."""
    manifest = _manifest()
    captures = cast(
        "dict[str, dict[str, object]]", manifest["live_physical_captures"]
    )
    assert set(captures) == set(_LIVE_PHYSICAL_CASES)
    for case, entry in captures.items():
        audit = _TRACE_ROOT / cast("str", entry["audit"])
        assert (
            hashlib.sha256(audit.read_bytes()).hexdigest() == entry["audit_sha256"]
        ), case
        assert (_TRACE_ROOT / cast("str", entry["evidence"])).is_file()
        assert cast("str", entry["reconciliation"]), case
