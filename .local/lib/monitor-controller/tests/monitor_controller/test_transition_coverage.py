"""Meaningful reducer guard coverage, without impossible Cartesian pairs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from uuid import UUID

from monitor_controller.invariants import assert_controller_invariants
from monitor_controller.model import (
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    ControllerStarted,
    EventMetadata,
    State,
    WorkerOutcome,
)
from monitor_controller.reducer import reduce
from monitor_controller.simulation.scenario import (
    event_from_data,
    initial_state,
    load_scenarios,
)

_SCENARIOS: Path = Path(__file__).parent / "scenarios" / "reducer-scenarios.json"
_MUTATOR_KINDS: tuple[ActionKind, ActionKind, ActionKind, ActionKind] = (
    ActionKind.PROBE,
    ActionKind.APPLICATION,
    ActionKind.PREPARATION,
    ActionKind.FINALIZATION,
)
_EVENT_GUARDS = frozenset(
    {
        "admission_dirtied",
        "boot_changed",
        "cancellation_acknowledged",
        "controller_started",
        "dispatch",
        "dispatch_rejected",
        "drm_hint",
        "finished",
        "observation",
        "plan_completed",
        "plan_failed",
        "plan_requested",
        "timer",
        "worker_status_unknown",
        "worker_timed_out",
    }
)
_REQUIRED_TAG_GUARDS = frozenset(
    {
        "invariant:1",
        "invariant:2",
        "invariant:3",
        "invariant:4",
        "invariant:5",
        "invariant:6",
        "invariant:8",
        "invariant:9",
        "invariant:10",
        "invariant:12",
        "invariant:13",
        "production:deterministic-replay",
        "production:dirty-admission",
        "production:dispatch-rejection",
        "production:worker-status-unknown",
        "production:worker-timeout",
        "safety:dirty-resets-proof-and-quietness",
        "safety:timeout-holds-mutation-exclusion",
        "safety:unknown-holds-mutation-exclusion",
    }
)


def _action(state: State, kind: ActionKind) -> object | None:
    return {
        ActionKind.PLAN: state.planning,
        ActionKind.PROBE: state.probe,
        ActionKind.APPLICATION: state.application,
        ActionKind.PREPARATION: state.preparation,
        ActionKind.FINALIZATION: state.finalization,
    }[kind]


def _scenario_report() -> tuple[
    dict[str, set[str]],
    dict[tuple[ActionKind, ActionLifecycle], State],
]:
    report: dict[str, set[str]] = defaultdict(set)
    states: dict[tuple[ActionKind, ActionLifecycle], State] = {}
    for scenario in load_scenarios(_SCENARIOS):
        for tag in scenario.covers:
            report[f"tag:{tag}"].add(scenario.name)
        state = initial_state(scenario.initial)
        for index, step in enumerate(scenario.steps):
            event = event_from_data(step.event_data, state)
            decision = reduce(state, event)
            evidence = f"{scenario.name}[{index}]"
            report[f"event:{step.event_data['type']}"].add(evidence)
            report[f"phase:{state.phase.value}->{decision.state.phase.value}"].add(
                evidence
            )
            for effect in decision.effects:
                report[f"effect:{type(effect).__name__}"].add(evidence)
            for kind in _MUTATOR_KINDS:
                action = _action(decision.state, kind)
                lifecycle = getattr(action, "lifecycle", None)
                if isinstance(lifecycle, ActionLifecycle):
                    states.setdefault((kind, lifecycle), decision.state)
            assert_controller_invariants(decision.state)
            state = decision.state
    return report, states


def _event_data(event_type: str, kind: ActionKind, now_ms: int) -> dict[str, object]:
    common: dict[str, object] = {
        "type": event_type,
        "at_ms": now_ms,
        "kind": kind.value,
    }
    if event_type == "finished":
        common.update(
            outcome=WorkerOutcome.SUCCEEDED.value,
            exit_status=0,
            plan_hash="matrix-plan",
        )
    elif event_type == "admission_dirtied":
        common["event_generation"] = 1_000_000
    elif event_type in {"dispatch_rejected", "worker_status_unknown"}:
        common["reason"] = "matrix injection"
    elif event_type == "worker_timed_out":
        common["deadline_ms"] = now_ms
    return common


def _matrix_report(
    report: dict[str, set[str]],
    states: dict[tuple[ActionKind, ActionLifecycle], State],
) -> None:
    for kind in _MUTATOR_KINDS:
        pending = states[(kind, ActionLifecycle.ADMITTED)]
        running = states[(kind, ActionLifecycle.DISPATCHED)]
        stopping = states[(kind, ActionLifecycle.STOPPING)]
        witnesses = (
            ("admission_dirtied", pending, f"race:dirty:{kind.value}"),
            ("dispatch_rejected", pending, f"failure:reject:{kind.value}"),
            ("worker_status_unknown", running, f"failure:unknown:{kind.value}"),
            ("worker_timed_out", running, f"failure:timeout:{kind.value}"),
            (
                "cancellation_acknowledged",
                stopping,
                f"race:cancel-ack:{kind.value}",
            ),
        )
        for event_type, state, guard in witnesses:
            event = event_from_data(
                _event_data(event_type, kind, 1_000_000),
                state,
            )
            decision = reduce(state, event)
            assert_controller_invariants(decision.state)
            report[guard].add(type(event).__name__)

        for outcome in WorkerOutcome:
            data = _event_data("finished", kind, 1_000_001)
            data["outcome"] = outcome.value
            event = event_from_data(data, running)
            decision = reduce(running, event)
            assert_controller_invariants(decision.state)
            report[f"result:{kind.value}:{outcome.value}"].add(type(event).__name__)

        for lifecycle, state in (
            ("pending", pending),
            ("running", running),
            ("stopping", stopping),
        ):
            event = ControllerStarted(
                EventMetadata(1_000_002, state.boot_id),
                ControllerInstanceId(UUID(int=30_000 + len(report) + len(lifecycle))),
            )
            decision = reduce(state, event)
            assert_controller_invariants(decision.state)
            report[f"restart:{kind.value}:{lifecycle}"].add(type(event).__name__)

        stale_event = event_from_data(
            _event_data("finished", kind, 1_000_003),
            running,
        )
        terminal = reduce(running, stale_event)
        stale = reduce(terminal.state, stale_event)
        assert stale.state == terminal.state
        assert stale.effects == ()
        report[f"stale-result:{kind.value}"].add(type(stale_event).__name__)


def _required_matrix_guards() -> frozenset[str]:
    guards: set[str] = set()
    for kind in _MUTATOR_KINDS:
        guards.update(
            {
                f"race:dirty:{kind.value}",
                f"failure:reject:{kind.value}",
                f"failure:unknown:{kind.value}",
                f"failure:timeout:{kind.value}",
                f"race:cancel-ack:{kind.value}",
                f"restart:{kind.value}:pending",
                f"restart:{kind.value}:running",
                f"restart:{kind.value}:stopping",
                f"stale-result:{kind.value}",
            }
        )
        guards.update(
            f"result:{kind.value}:{outcome.value}" for outcome in WorkerOutcome
        )
    return frozenset(guards)


def test_every_documented_meaningful_guard_class_has_executable_evidence() -> None:
    report, states = _scenario_report()
    _matrix_report(report, states)
    required = {
        *(f"event:{event}" for event in _EVENT_GUARDS),
        *(f"tag:{tag}" for tag in _REQUIRED_TAG_GUARDS),
        *_required_matrix_guards(),
    }
    missing = sorted(guard for guard in required if not report.get(guard))

    assert not missing, f"transition guard classes without evidence: {missing}"


def test_transition_report_uses_guard_names_not_cartesian_state_event_pairs() -> None:
    report, states = _scenario_report()
    _matrix_report(report, states)
    keys = set(report)

    assert "tag:invariant:6" in keys
    assert "effect:FinalizeDesktop" in keys
    assert any(key.startswith("phase:verifying->") for key in keys)
    assert not any("recovering+finished" in key for key in keys)
