"""Adversarial rule-based state-machine tests for the pure reducer."""

from __future__ import annotations

import unittest
from typing import cast
from uuid import UUID

from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from monitor_controller.codec import decode_state, encode_state
from monitor_controller.invariants import assert_controller_invariants
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionRecord,
    AdmissionDirtied,
    ApplicationAction,
    ApplicationDispatched,
    ApplicationFinished,
    ApplyProfile,
    BootChanged,
    BootId,
    ControllerInstanceId,
    ControllerPhase,
    ControllerStarted,
    Decision,
    DispatchRejected,
    DisplayIdentity,
    DrmHintReceived,
    Event,
    EventGeneration,
    EventMetadata,
    FinalizationAction,
    FinalizationDispatched,
    FinalizationFinished,
    FinalizeDesktop,
    ObservationCompleted,
    ObservationInvalidityReason,
    PlanCompleted,
    PlanFailed,
    PlanHash,
    PlanRequested,
    PreparationAction,
    PreparationDispatched,
    PreparationFinished,
    PrepareDesktop,
    ProbeAction,
    ProbeDispatched,
    ProbeFinished,
    ProfileScope,
    State,
    TimerFired,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerUnit,
)
from monitor_controller.reducer import (
    PROFILE_STABILITY_MS,
    SLOW_DELAYS_MS,
    reduce,
)
from monitor_controller.simulation.replay import (
    ReplayStep,
    ReplayTrace,
    decode_replay,
    encode_replay,
    replay,
)
from monitor_controller.simulation.scenario import event_from_data

_BOOT: BootId = BootId(UUID(int=1))
_INSTANCE = ControllerInstanceId(UUID(int=2))
_MUTATING_EFFECTS = (ApplyProfile, PrepareDesktop, FinalizeDesktop)
_ACKNOWLEDGED = {
    ActionLifecycle.DISPATCHED,
    ActionLifecycle.STOPPING,
    ActionLifecycle.RESULT_PENDING,
}


def _initial_state() -> State:
    return State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":stateful"),
    )


def _actions(state: State) -> tuple[ActionRecord, ...]:
    return tuple(
        action
        for action in (
            state.probe,
            state.application,
            state.planning,
            state.preparation,
            state.finalization,
        )
        if action is not None
    )


def _observation_data(
    state: State,
    *,
    now_ms: int,
    shape: str,
    changed_token: bool,
) -> dict[str, object]:
    token = (
        f"physical-{state.physical_epoch + 1}-{now_ms}"
        if changed_token or state.physical_token is None
        else state.physical_token.value
    )
    generation = state.observation_generation.value + 1
    common: dict[str, object] = {
        "type": "observation",
        "at_ms": now_ms,
        "key": f"{shape}-{token}",
        "physical_token": token,
        "external_state": "none",
        "target_profile": None,
        "target_scope": "external",
        "exact_profile": None,
        "current_profile": None,
        "valid": True,
        "observation_generation": generation,
        "event_generation": state.event_generation.value,
        "identity_profile": None,
        "probe_output": None,
        "probe_internal_output": None,
        "probe_mode": None,
        "target_layout": None,
        "configuration_hashes": {},
        "external_outputs": [],
        "complete_edid_outputs": [],
        "base_identity_outputs": [],
        "internal_edid_complete": False,
    }
    if shape == "internal_exact":
        common.update(
            target_profile="laptop",
            target_scope="internal",
            exact_profile="laptop",
            current_profile="laptop",
            target_layout="layouts/laptop.yaml",
            configuration_hashes={"layouts/laptop.yaml": "sha256:laptop"},
        )
    elif shape == "external_unresolved":
        common.update(
            external_state="unresolved",
            external_outputs=["DP-1"],
        )
    elif shape in {"external_eligible", "external_exact"}:
        exact = shape == "external_exact"
        common.update(
            external_state="known",
            target_profile="dock",
            target_scope="mixed",
            exact_profile="dock" if exact else None,
            current_profile="dock" if exact else None,
            target_layout="layouts/dock.yaml",
            configuration_hashes={"layouts/dock.yaml": "sha256:dock"},
            external_outputs=["DP-1"],
            complete_edid_outputs=["DP-1"],
            base_identity_outputs=["DP-1"],
        )
    elif shape == "probeable":
        common.update(
            external_state="probeable",
            identity_profile="dock",
            probe_output="DP-1",
            probe_internal_output="eDP-1",
            probe_mode="3840x2160",
            external_outputs=["DP-1"],
            base_identity_outputs=["DP-1"],
        )
    elif shape == "invalid_external":
        common.update(
            valid=False,
            external_state="unresolved",
            external_outputs=["DP-1"],
        )
    elif shape == "invalid_internal":
        common["valid"] = False
    else:
        raise AssertionError(shape)
    return common


def _current_action(state: State, kind: ActionKind) -> ActionRecord | None:
    return {
        ActionKind.PLAN: state.planning,
        ActionKind.PROBE: state.probe,
        ActionKind.APPLICATION: state.application,
        ActionKind.PREPARATION: state.preparation,
        ActionKind.FINALIZATION: state.finalization,
    }[kind]


def _worker_unit(action_id: ActionId) -> WorkerUnit:
    return WorkerUnit(action_id, f"monitor-{action_id.kind.value}@stateful.service")


def _dispatch_event(state: State, action: ActionRecord, now_ms: int) -> Event:
    metadata = EventMetadata(now_ms, state.boot_id)
    unit = _worker_unit(action.action_id)
    if isinstance(action, ProbeAction):
        return ProbeDispatched(metadata, action.action_id, unit)
    if isinstance(action, ApplicationAction):
        return ApplicationDispatched(metadata, action.action_id, unit)
    if isinstance(action, PreparationAction):
        return PreparationDispatched(metadata, action.action_id, unit)
    if isinstance(action, FinalizationAction):
        return FinalizationDispatched(metadata, action.action_id, unit)
    return PlanRequested(metadata, action.action_id, action.input_key)


def _finish_event(
    state: State,
    action: ActionRecord,
    now_ms: int,
    outcome: WorkerOutcome,
) -> Event:
    metadata = EventMetadata(now_ms, state.boot_id)
    if isinstance(action, ProbeAction):
        return ProbeFinished(metadata, action.action_id, outcome, 0)
    if isinstance(action, ApplicationAction):
        return ApplicationFinished(metadata, action.action_id, outcome, 0)
    if isinstance(action, PreparationAction):
        return PreparationFinished(
            metadata,
            action.action_id,
            outcome,
            0,
            action.plan_hash,
        )
    if isinstance(action, FinalizationAction):
        return FinalizationFinished(metadata, action.action_id, outcome, 0)
    if outcome is WorkerOutcome.SUCCEEDED:
        return PlanCompleted(
            metadata,
            action.action_id,
            action.input_key,
            PlanHash(f"plan-{action.action_id.sequence}"),
        )
    return PlanFailed(
        metadata,
        action.action_id,
        action.input_key,
        "generated failure",
        1,
    )


def _mutators(state: State) -> tuple[ActionRecord, ...]:
    actions = (
        state.probe,
        state.application,
        state.preparation,
        state.finalization,
    )
    return tuple(
        action
        for action in actions
        if action is not None and action.lifecycle in _ACKNOWLEDGED
    )


@settings(
    max_examples=40,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
class ReducerStateMachine(RuleBasedStateMachine):
    """Generate observations, lifecycle events, races, and durable restarts."""

    def __init__(self) -> None:
        """Start from one empty recovering authority."""
        super().__init__()
        self.initial = _initial_state()
        self.state = self.initial
        self.now_ms = 0
        self.steps: list[ReplayStep] = []
        self.known_actions: dict[ActionId, ActionRecord] = {}
        self.retired_actions: dict[ActionId, ActionRecord] = {}
        self.seen_allocations: set[ActionId] = set()
        self.seen_transitions: set[object] = set()

    def _tick(self, amount: int = 1) -> int:
        self.now_ms += amount
        return self.now_ms

    def _apply(self, event: Event) -> Decision:
        before = self.state
        try:
            first = reduce(before, event)
        except Exception:
            # The valid replay prefix plus Hypothesis's printed final rule call
            # reproduces reducer exceptions after shrinking.
            note(encode_replay(ReplayTrace(self.initial, tuple(self.steps))).decode())
            raise
        second = reduce(before, event)
        assert first == second
        assert_controller_invariants(first.state)
        self._assert_properties(before, event, first)
        self.steps.append(ReplayStep(event, first))
        self.state = first.state
        self._track_identities()
        return first

    def _track_identities(self) -> None:
        current = {action.action_id: action for action in _actions(self.state)}
        for action_id, action in current.items():
            if action_id in self.retired_actions:
                assert self.retired_actions[action_id] == action
            self.known_actions[action_id] = action
            self.seen_allocations.add(action_id)
            transition = getattr(action, "transition_id", None)
            if transition is not None:
                self.seen_transitions.add(transition)
        for action_id, action in tuple(self.known_actions.items()):
            if action_id not in current:
                self.retired_actions[action_id] = action
        all_ids = [
            *(action.action_id for action in _actions(self.state)),
            *(item.action_id for item in self.state.action_tombstones),
        ]
        assert len(set(all_ids)) == len({*all_ids})
        assert all(
            action_id.sequence <= self.state.action_sequence_high_water
            or action_id.controller_instance != self.state.controller_instance
            for action_id in all_ids
        )

    def _assert_properties(
        self, before: State, event: Event, decision: Decision
    ) -> None:
        after = decision.state
        assert len(_mutators(after)) <= 1
        if after.external_intent:
            assert not (
                after.application is not None
                and after.application.scope is ProfileScope.INTERNAL_ONLY
            )
        if isinstance(event, ObservationCompleted) and not event.observation.valid:
            assert after.physical_epoch == before.physical_epoch
            assert after.physical_token == before.physical_token
            unavailable_reasons = {
                ObservationInvalidityReason.COMMAND_TIMED_OUT,
                ObservationInvalidityReason.PARSE_FAILED,
            }
            prior = before.latest_observation
            finalized_profile = before.desktop_finalized_profile
            finalized_profile_returned = (
                finalized_profile is not None
                and event.observation.invalidity_reason not in unavailable_reasons
                and prior is not None
                and (
                    prior.valid
                    or prior.invalidity_reason not in unavailable_reasons
                )
                and prior.physical_token == event.observation.physical_token
                and finalized_profile in event.observation.current_profiles
                and finalized_profile not in prior.current_profiles
            )
            assert after.desktop_finalized_profile == (
                None if finalized_profile_returned else finalized_profile
            )
            mutating = any(
                isinstance(effect, _MUTATING_EFFECTS) for effect in decision.effects
            )
            assert not mutating
        for effect in decision.effects:
            if isinstance(effect, FinalizeDesktop):
                observation = after.latest_observation
                assert observation is not None
                assert observation.valid
                assert observation.exact_profile == effect.profile
                assert effect.profile in observation.current_profiles
                assert after.verify_since_ms is not None
                assert (
                    observation.observed_at_ms - after.verify_since_ms
                    >= PROFILE_STABILITY_MS
                )
        assert after.action_sequence_high_water >= before.action_sequence_high_water
        assert (
            after.transition_sequence_high_water
            >= before.transition_sequence_high_water
        )

    @rule(
        shape=st.sampled_from(
            (
                "internal_exact",
                "external_unresolved",
                "external_eligible",
                "external_exact",
                "probeable",
                "invalid_external",
                "invalid_internal",
            )
        ),
        changed_token=st.booleans(),
        elapsed=st.integers(min_value=0, max_value=15_000),
    )
    def _observation(self, shape: str, changed_token: bool, elapsed: int) -> None:
        now = self._tick(elapsed)
        data = _observation_data(
            self.state,
            now_ms=now,
            shape=shape,
            changed_token=changed_token,
        )
        self._apply(event_from_data(data, self.state))

    @rule(overdue=st.booleans())
    def _timer(self, overdue: bool) -> None:
        now = self._tick()
        deadline = self.state.next_timer_ms
        if deadline is None:
            deadline = now + 10
        processed = max(now, deadline) if overdue else now
        self.now_ms = processed
        self._apply(TimerFired(EventMetadata(processed, self.state.boot_id), deadline))

    @rule(fresh=st.booleans())
    def _drm_hint(self, fresh: bool) -> None:
        generation = self.state.event_generation.value + (1 if fresh else 0)
        self._apply(
            DrmHintReceived(
                EventMetadata(self._tick(), self.state.boot_id),
                EventGeneration(generation),
            )
        )

    @rule()
    def _progress_current_lifecycle(self) -> None:
        action = next(
            (
                item
                for item in _actions(self.state)
                if item.lifecycle is ActionLifecycle.ADMITTED
            ),
            None,
        )
        if action is not None:
            self._apply(_dispatch_event(self.state, action, self._tick()))
            return
        action = next(
            (
                item
                for item in _actions(self.state)
                if item.lifecycle is ActionLifecycle.DISPATCHED
            ),
            None,
        )
        if action is not None:
            self._apply(
                _finish_event(
                    self.state,
                    action,
                    self._tick(),
                    WorkerOutcome.SUCCEEDED,
                )
            )
            return
        if self.state.latest_observation is None:
            self._observation("external_exact", changed_token=True, elapsed=0)
            return
        shape = "external_exact" if self.state.external_intent else "internal_exact"
        self._observation(shape, changed_token=False, elapsed=10_000)

    @rule(
        kind=st.sampled_from(tuple(ActionKind)),
        failure=st.sampled_from(("reject", "unknown", "timeout", "cancel", "dirty")),
    )
    def _lifecycle_failure_or_race(self, kind: ActionKind, failure: str) -> None:
        action = _current_action(self.state, kind)
        if action is None:
            return
        metadata = EventMetadata(self._tick(), self.state.boot_id)
        if failure == "reject":
            event: Event = DispatchRejected(metadata, action.action_id, "generated")
        elif failure == "unknown":
            event = WorkerStatusUnknown(metadata, action.action_id, "generated")
        elif failure == "timeout":
            event = WorkerTimedOut(metadata, action.action_id, self.now_ms)
        elif failure == "cancel":
            event = WorkerCancellationAcknowledged(
                metadata,
                action.action_id,
                ActionLifecycle.CANCELLED,
                143,
            )
        else:
            event = AdmissionDirtied(
                metadata,
                action.action_id,
                EventGeneration(self.state.event_generation.value + 1),
            )
        self._apply(event)

    @rule(
        kind=st.sampled_from(tuple(ActionKind)),
        outcome=st.sampled_from(tuple(WorkerOutcome)),
    )
    def _worker_or_plan_result(self, kind: ActionKind, outcome: WorkerOutcome) -> None:
        action = _current_action(self.state, kind)
        if action is not None:
            self._apply(_finish_event(self.state, action, self._tick(), outcome))

    @rule()
    def _controller_restart_with_persistence_round_trip(self) -> None:
        self.state = decode_state(encode_state(self.state))
        instance = ControllerInstanceId(UUID(int=10_000 + len(self.steps)))
        self._apply(
            ControllerStarted(
                EventMetadata(self._tick(), self.state.boot_id),
                instance,
            )
        )

    @rule()
    def _boot_change(self) -> None:
        previous = self.state.boot_id
        new_boot = BootId(UUID(int=20_000 + len(self.steps)))
        self._apply(BootChanged(EventMetadata(self._tick(), new_boot), previous))

    @rule()
    def _late_stale_completion(self) -> None:
        if not self.retired_actions:
            return
        action = next(iter(self.retired_actions.values()))
        before = self.state
        decision = self._apply(
            _finish_event(
                self.state,
                action,
                self._tick(),
                WorkerOutcome.SUCCEEDED,
            )
        )
        assert decision.state == before
        assert decision.effects == ()

    @invariant()
    def _state_is_always_persistable(self) -> None:
        assert decode_state(encode_state(self.state)) == self.state

    def teardown(self) -> None:
        """Emit and round-trip the final shrunken sequence as canonical JSONL."""
        trace = ReplayTrace(self.initial, tuple(self.steps))
        encoded = encode_replay(trace)
        note(encoded.decode())
        assert decode_replay(encoded) == trace
        assert replay(trace) == tuple(step.expected for step in self.steps)


TestReducerStateMachine = cast(
    "type[unittest.TestCase]",
    ReducerStateMachine.TestCase,  # pyright: ignore[reportUnknownMemberType]
)


def test_capped_wait_slow_tick_repeats_an_attempted_application() -> None:
    """Evidence that regresses to an attempted key must not starve forever.

    The Samsung G75F drops its DisplayPort link after a successful load, so
    the next observation hashes to an already-attempted ApplicationAttemptKey.
    Strict at-most-once then blocked every retry within the epoch and the
    monitor stayed dark until manual autorandr kicks (dc-v0r). Once slow
    backoff is capped, each tick may re-admit one application.
    """
    state = _initial_state()
    now = 1

    def observe(advance: int) -> Decision:
        nonlocal now, state
        now += advance
        data = _observation_data(
            state,
            now_ms=now,
            shape="external_eligible",
            changed_token=False,
        )
        data["key"] = "regressed-evidence"
        decision = reduce(state, event_from_data(data, state))
        assert_controller_invariants(decision.state)
        # The codec validator is stricter than the runtime invariants and
        # refused the first live slow-retry re-admission (dc-2eh); every
        # decision must survive the same round trip persistence performs.
        decode_state(encode_state(decision.state))
        state = decision.state
        return decision

    first = observe(0)
    assert any(isinstance(effect, ApplyProfile) for effect in first.effects)
    action = state.application
    assert action is not None
    for event in (
        _dispatch_event(state, action, now + 1),
        _finish_event(state, action, now + 2, WorkerOutcome.SUCCEEDED),
    ):
        state = reduce(state, event).state
    now += 2

    backoffs: list[tuple[ControllerPhase, int]] = []
    while state.backoff_index < len(SLOW_DELAYS_MS) - 1 or state.phase is not (
        ControllerPhase.WAIT_SLOW
    ):
        decision = observe(100_000)
        assert not any(isinstance(e, ApplyProfile) for e in decision.effects)
        backoffs.append((state.phase, state.backoff_index))
        assert len(backoffs) < 10, backoffs

    retried = observe(100_000)
    assert any(isinstance(effect, ApplyProfile) for effect in retried.effects)
    assert state.phase is ControllerPhase.APPLY_PENDING


def test_external_evidence_revokes_internal_candidate_during_preparation() -> None:
    """External hardware must clear a laptop candidate mid-preparation.

    Found twice by the property machine: an internal-only candidate with an
    in-flight preparation survived a valid external observation (via the
    preparation-stop branch, then via the epoch transition), violating the
    no-laptop-fallback invariant. reduce() asserts invariants on every
    decision, so completing this sequence without raising is the regression.
    """
    state = _initial_state()
    now = 1

    def observe(shape: str) -> None:
        nonlocal now, state
        now += 1
        data = _observation_data(state, now_ms=now, shape=shape, changed_token=False)
        state = reduce(state, event_from_data(data, state)).state

    observe("internal_exact")
    for _ in range(5):
        action = next(iter(_actions(state)), None)
        if action is None:
            break
        now += 1
        if action.lifecycle is ActionLifecycle.ADMITTED:
            event = _dispatch_event(state, action, now)
        else:
            event = _finish_event(state, action, now, WorkerOutcome.SUCCEEDED)
        state = reduce(state, event).state
    observe("external_unresolved")

    assert state.candidate is None or (
        state.candidate.scope is not ProfileScope.INTERNAL_ONLY
    )
