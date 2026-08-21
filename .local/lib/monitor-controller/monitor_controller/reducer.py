# ruff: noqa: C901, PLR0911, PLR0912, PLR0915
"""Total deterministic policy reducer for the monitor controller.

The reducer is deliberately pure: time, observations, worker identity, and worker
outcomes arrive as values, and every requested side effect is returned as data.
Invalid or stale events are fail-closed no-ops.
"""

from __future__ import annotations

from dataclasses import replace

from .invariants import assert_controller_invariants
from .model import (
    EVENT_TYPES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionRecord,
    ActionTombstone,
    ActivateProbe,
    AdmissionDirtied,
    ApplicationAction,
    ApplicationAttemptKey,
    ApplicationDispatched,
    ApplicationFinished,
    ApplyProfile,
    BootChanged,
    CandidateSelection,
    CanonicalObservation,
    ControllerPhase,
    ControllerStarted,
    Decision,
    DiscardPlan,
    DispatchRejected,
    DrmHintReceived,
    Effect,
    Event,
    EventGeneration,
    FinalizationAction,
    FinalizationDispatched,
    FinalizationFinished,
    FinalizeDesktop,
    MappingProof,
    ObservationCompleted,
    ObservationKey,
    PlanCompleted,
    PlanFailed,
    PlanHash,
    PlanningAction,
    PlanningInputKey,
    PlanningState,
    PlanRequested,
    PreparationAction,
    PreparationDispatched,
    PreparationFinished,
    PreparationState,
    PrepareDesktop,
    ProbeAction,
    ProbeAttemptKey,
    ProbeDispatched,
    ProbeFinished,
    ProfileMatch,
    ProfileScope,
    RequestObservation,
    RequestPlan,
    Schedule,
    State,
    StopAction,
    TimerFired,
    TransitionId,
    TransitionKey,
    UnplugProof,
    WakeReason,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerUnit,
)

AGGRESSIVE_BUDGET_MS: int = 30_000
FAST_DELAYS_MS = (0, 250, 500, 1_000, 2_000)
SLOW_DELAYS_MS = (5_000, 10_000, 20_000, 30_000)
PROFILE_STABILITY_MS: int = 10_000
PREPARATION_STABILITY_MS: int = 2_000
EVENT_QUIET_MS: int = 5_000
UNKNOWN_STABILITY_MS = 10_000
UNPLUG_STABILITY_MS = 1_000
UNPLUG_REQUIRED_SAMPLES = 2
HEALTH_POLL_MS = 60_000


class UnknownEventError(TypeError):
    """Raised when a caller bypasses the closed Event type."""


def _decision(state: State, *effects: Effect) -> Decision:
    assert_controller_invariants(state)
    return Decision(state=state, effects=effects)


def _no_op(state: State) -> Decision:
    return _decision(state)


def _schedule(state: State, deadline_ms: int, *effects: Effect) -> Decision:
    scheduled = replace(state, next_timer_ms=deadline_ms)
    return _decision(scheduled, *effects, Schedule(deadline_ms))


def _allocate_action(state: State, kind: ActionKind) -> tuple[State, ActionId]:
    sequence = state.action_sequence_high_water + 1
    return (
        replace(state, action_sequence_high_water=sequence),
        ActionId(state.controller_instance, kind, sequence),
    )


def _allocate_transition(state: State) -> tuple[State, TransitionId]:
    sequence = state.transition_sequence_high_water + 1
    return (
        replace(state, transition_sequence_high_water=sequence),
        TransitionId(state.controller_instance, sequence),
    )


def _append_tombstone(
    state: State, action: ActionRecord, lifecycle: ActionLifecycle
) -> State:
    tombstone = ActionTombstone(action.action_id, lifecycle)
    retained = tuple(
        item for item in state.action_tombstones if item.action_id != action.action_id
    )
    return replace(state, action_tombstones=(*retained, tombstone))


def _profile_match(
    observation: CanonicalObservation, profile: str
) -> ProfileMatch | None:
    return next(
        (match for match in observation.eligible_profiles if match.profile == profile),
        None,
    )


def _unique_target(observation: CanonicalObservation) -> ProfileMatch | None:
    if observation.exact_profile is not None:
        return _profile_match(observation, observation.exact_profile)
    if len(observation.eligible_profiles) == 1:
        return observation.eligible_profiles[0]
    return None


def _mapping_proof(
    state: State, observation: CanonicalObservation, match: ProfileMatch
) -> MappingProof:
    return MappingProof(
        profile=match.profile,
        physical_epoch=state.physical_epoch,
        observation_key=observation.observation_key,
        outputs=match.mapping,
    )


def _candidate(
    state: State, observation: CanonicalObservation, match: ProfileMatch
) -> CandidateSelection:
    return CandidateSelection(
        profile=match.profile,
        scope=match.scope,
        mapping=_mapping_proof(state, observation, match),
        observation_key=observation.observation_key,
    )


def _transition_key(state: State, profile: str, key: ObservationKey) -> TransitionKey:
    return TransitionKey(f"{state.physical_epoch}|{profile}|{key.value}")


def _planning_input_key(
    state: State, target: ProfileMatch, key: ObservationKey
) -> PlanningInputKey:
    return PlanningInputKey(
        physical_epoch=state.physical_epoch,
        profile=target.profile,
        layout=target.layout,
        observation_key=key,
        mapping=target.mapping,
        configuration_hashes=target.configuration_hashes,
    )


def _clear_proof(state: State) -> State:
    return replace(state, verify_since_ms=None)


def _discard_planning(state: State) -> tuple[State, tuple[Effect, ...]]:
    effects: tuple[Effect, ...] = ()
    if state.planning is not None:
        effects = (DiscardPlan(state.planning.action_id, state.planning.plan_hash),)
        state = _append_tombstone(state, state.planning, ActionLifecycle.CANCELLED)
    return (
        replace(
            state,
            planning_state=PlanningState.PLAN_IDLE,
            preparation_state=PreparationState.PREPARE_IDLE,
            planning=None,
            preparation=None,
        ),
        effects,
    )


def _reset_for_epoch(
    state: State, observation: CanonicalObservation, now_ms: int
) -> tuple[State, tuple[Effect, ...]]:
    state, discard_effects = _discard_planning(state)
    for action in (state.probe, state.application, state.finalization):
        if action is not None and action.lifecycle is ActionLifecycle.ADMITTED:
            state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
    state = replace(
        state,
        phase=ControllerPhase.DISCOVER_FAST,
        physical_epoch=state.physical_epoch + 1,
        physical_token=observation.physical_token,
        reconcile_epoch=state.reconcile_epoch + 1,
        candidate=None,
        aggressive_deadline_ms=now_ms + AGGRESSIVE_BUDGET_MS,
        backoff_index=0,
        verify_since_ms=None,
        stable_x_profile=None,
        attempted_probe_keys=frozenset(),
        probe=None,
        attempted_application_keys=frozenset(),
        application=None,
        finalization=None,
        unknown_key=None,
        unknown_since_ms=None,
        unplug_proof=None,
        baseline_adoption=(
            state.phase is ControllerPhase.RECOVERING
            and state.desktop_finalized_profile is None
        ),
    )
    return state, discard_effects


def _enter_wait(state: State, now_ms: int) -> Decision:
    state = _clear_proof(state)
    deadline = state.aggressive_deadline_ms
    if deadline is None:
        deadline = now_ms + AGGRESSIVE_BUDGET_MS
        state = replace(state, aggressive_deadline_ms=deadline, backoff_index=0)
    if now_ms < deadline:
        index = (
            state.backoff_index if state.phase is ControllerPhase.DISCOVER_FAST else 0
        )
        index = min(index, len(FAST_DELAYS_MS) - 1)
        delay = min(FAST_DELAYS_MS[index], deadline - now_ms)
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            backoff_index=min(index + 1, len(FAST_DELAYS_MS) - 1),
        )
    else:
        index = state.backoff_index if state.phase is ControllerPhase.WAIT_SLOW else 0
        index = min(index, len(SLOW_DELAYS_MS) - 1)
        delay = SLOW_DELAYS_MS[index]
        state = replace(
            state,
            phase=ControllerPhase.WAIT_SLOW,
            backoff_index=min(index + 1, len(SLOW_DELAYS_MS) - 1),
        )
    return _schedule(state, now_ms + delay)


def _admit_probe(state: State, observation: CanonicalObservation) -> Decision:
    candidate = observation.probe_candidate
    if candidate is None:
        return _enter_wait(state, observation.observed_at_ms)
    key = ProbeAttemptKey(
        state.physical_epoch, candidate.profile, observation.observation_key
    )
    if key in state.attempted_probe_keys:
        return _enter_wait(state, observation.observed_at_ms)
    state, discard_effects = _discard_planning(state)
    state, action_id = _allocate_action(state, ActionKind.PROBE)
    action = ProbeAction(
        action_id=action_id,
        key=key,
        admitted_event_generation=observation.event_generation,
        output=candidate.output,
        internal_output=candidate.internal_output,
        preferred_mode=candidate.preferred_mode,
    )
    state = replace(
        state,
        phase=ControllerPhase.PROBE_PENDING,
        candidate=None,
        probe=action,
        baseline_adoption=False,
        verify_since_ms=None,
        next_timer_ms=observation.observed_at_ms,
    )
    return _decision(
        state,
        *discard_effects,
        ActivateProbe(
            action_id=action_id,
            key=key,
            output=action.output,
            internal_output=action.internal_output,
            preferred_mode=action.preferred_mode,
            admitted_event_generation=action.admitted_event_generation,
            observation_key=observation.observation_key,
        ),
    )


def _admit_application(
    state: State, observation: CanonicalObservation, target: ProfileMatch
) -> Decision:
    key = ApplicationAttemptKey(
        state.physical_epoch, target.profile, observation.observation_key
    )
    candidate = _candidate(state, observation, target)
    state = replace(state, candidate=candidate, baseline_adoption=False)
    if key in state.attempted_application_keys:
        return _enter_wait(state, observation.observed_at_ms)
    state, action_id = _allocate_action(state, ActionKind.APPLICATION)
    action = ApplicationAction(
        action_id=action_id,
        key=key,
        admitted_event_generation=observation.event_generation,
        profile=target.profile,
        scope=target.scope,
        mapping=candidate.mapping,
    )
    state = replace(
        state,
        phase=ControllerPhase.APPLY_PENDING,
        application=action,
        next_timer_ms=observation.observed_at_ms,
    )
    return _decision(
        state,
        ApplyProfile(
            action_id=action_id,
            key=key,
            profile=target.profile,
            mapping=action.mapping,
            admitted_event_generation=action.admitted_event_generation,
            observation_key=observation.observation_key,
        ),
    )


def _admit_plan(
    state: State, observation: CanonicalObservation, target: ProfileMatch
) -> tuple[State, RequestPlan]:
    state, transition_id = _allocate_transition(state)
    state, action_id = _allocate_action(state, ActionKind.PLAN)
    input_key = _planning_input_key(state, target, observation.observation_key)
    action = PlanningAction(
        action_id=action_id,
        transition_id=transition_id,
        input_key=input_key,
        profile=target.profile,
    )
    state = replace(
        state,
        planning_state=PlanningState.PLAN_PENDING,
        planning=action,
    )
    return state, RequestPlan(action_id, transition_id, input_key, target.profile)


def _start_verification(
    state: State, observation: CanonicalObservation, target: ProfileMatch
) -> Decision:
    same_proof = (
        state.phase is ControllerPhase.VERIFYING
        and state.candidate is not None
        and state.candidate.profile == target.profile
        and state.candidate.observation_key == observation.observation_key
        and state.verify_since_ms is not None
    )
    if not same_proof:
        state, discard_effects = _discard_planning(state)
        state = replace(
            state,
            verify_since_ms=observation.observed_at_ms,
            candidate=_candidate(state, observation, target),
        )
    else:
        discard_effects = ()
    state = replace(state, phase=ControllerPhase.VERIFYING)
    extra_effects: tuple[Effect, ...] = discard_effects
    changed_desktop = state.desktop_finalized_profile != target.profile
    if (
        changed_desktop
        and not state.baseline_adoption
        and state.planning_state is PlanningState.PLAN_IDLE
    ):
        state, plan_effect = _admit_plan(state, observation, target)
        extra_effects = (*extra_effects, plan_effect)
    return _advance_verification(state, observation, extra_effects)


def _advance_verification(
    state: State,
    observation: CanonicalObservation,
    leading_effects: tuple[Effect, ...] = (),
) -> Decision:
    if state.verify_since_ms is None or state.candidate is None:
        return _enter_wait(state, observation.observed_at_ms)
    now_ms = observation.observed_at_ms
    profile = state.candidate.profile
    proof_age = now_ms - state.verify_since_ms
    quiet_age = (
        EVENT_QUIET_MS
        if state.last_drm_at_ms is None
        else now_ms - state.last_drm_at_ms
    )

    if (
        proof_age >= PREPARATION_STABILITY_MS
        and state.desktop_finalized_profile != profile
        and not state.baseline_adoption
        and state.planning_state is PlanningState.PLAN_READY
        and state.preparation_state is PreparationState.PREPARE_IDLE
        and state.planning is not None
        and state.planning.plan_hash is not None
    ):
        planning = state.planning
        candidate = state.candidate
        plan_hash = planning.plan_hash
        if plan_hash is None:  # narrowed independently for strict type checkers
            return _no_op(state)
        state, action_id = _allocate_action(state, ActionKind.PREPARATION)
        preparation = PreparationAction(
            action_id=action_id,
            transition_id=planning.transition_id,
            transition_key=_transition_key(state, profile, candidate.observation_key),
            plan_hash=plan_hash,
            admitted_event_generation=observation.event_generation,
            observation_key=observation.observation_key,
            profile=profile,
        )
        state = replace(
            state,
            preparation_state=PreparationState.PREPARE_PENDING,
            preparation=preparation,
        )
        leading_effects = (
            *leading_effects,
            PrepareDesktop(
                action_id=action_id,
                transition_id=preparation.transition_id,
                transition_key=preparation.transition_key,
                profile=profile,
                plan_hash=preparation.plan_hash,
                admitted_event_generation=observation.event_generation,
                observation_key=observation.observation_key,
            ),
        )

    proof_complete = proof_age >= PROFILE_STABILITY_MS and quiet_age >= EVENT_QUIET_MS
    if proof_complete and state.desktop_finalized_profile == profile:
        state, discard_effects = _discard_planning(state)
        state = replace(
            state,
            phase=ControllerPhase.QUIESCENT,
            stable_x_profile=profile,
            baseline_adoption=False,
        )
        return _schedule(
            state,
            now_ms + HEALTH_POLL_MS,
            *leading_effects,
            *discard_effects,
        )
    if proof_complete and state.baseline_adoption:
        state = replace(
            state,
            phase=ControllerPhase.QUIESCENT,
            stable_x_profile=profile,
            desktop_finalized_profile=profile,
            baseline_adoption=False,
        )
        return _schedule(state, now_ms + HEALTH_POLL_MS, *leading_effects)
    if (
        proof_complete
        and state.preparation_state is PreparationState.PREPARED
        and state.preparation is not None
        and state.finalization is None
    ):
        preparation = state.preparation
        state, action_id = _allocate_action(state, ActionKind.FINALIZATION)
        finalization = FinalizationAction(
            action_id=action_id,
            transition_id=preparation.transition_id,
            transition_key=preparation.transition_key,
            plan_hash=preparation.plan_hash,
            admitted_event_generation=observation.event_generation,
            observation_key=observation.observation_key,
            profile=profile,
        )
        state = replace(
            state,
            phase=ControllerPhase.FINALIZE_PENDING,
            finalization=finalization,
            next_timer_ms=now_ms,
        )
        return _decision(
            state,
            *leading_effects,
            FinalizeDesktop(
                action_id=action_id,
                transition_id=finalization.transition_id,
                transition_key=finalization.transition_key,
                profile=profile,
                plan_hash=finalization.plan_hash,
                admitted_event_generation=observation.event_generation,
                observation_key=observation.observation_key,
            ),
        )
    return _schedule(state, now_ms + 1_000, *leading_effects)


def _same_exact_finalization(state: State, observation: CanonicalObservation) -> bool:
    action = state.finalization
    return (
        action is not None
        and state.physical_token == observation.physical_token
        and observation.exact_profile == action.profile
        and action.profile in observation.current_profiles
        and observation.observation_key == action.observation_key
    )


def _reclassify_cancelled_admission(
    state: State, observation: CanonicalObservation
) -> Decision:
    epoch_effects: tuple[Effect, ...] = ()
    if state.physical_token != observation.physical_token:
        state, epoch_effects = _reset_for_epoch(
            state, observation, observation.observed_at_ms
        )
    decision = _classify_observation(state, observation)
    return _decision(decision.state, *epoch_effects, *decision.effects)


def _observe_pending_action(
    state: State, observation: CanonicalObservation
) -> Decision | None:
    now_ms = observation.observed_at_ms
    if state.phase is ControllerPhase.PROBE_PENDING and state.probe is not None:
        candidate = observation.probe_candidate
        action = state.probe
        if (
            candidate is not None
            and state.physical_token == observation.physical_token
            and action.key.observation_key == observation.observation_key
            and candidate.profile == action.key.profile
            and candidate.output == action.output
            and candidate.internal_output == action.internal_output
            and candidate.preferred_mode == action.preferred_mode
        ):
            action = replace(
                action, admitted_event_generation=observation.event_generation
            )
            state = replace(state, probe=action, next_timer_ms=now_ms)
            return _decision(
                state,
                ActivateProbe(
                    action.action_id,
                    action.key,
                    action.output,
                    action.internal_output,
                    action.preferred_mode,
                    action.admitted_event_generation,
                    observation.observation_key,
                ),
            )
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, probe=None)
        return _reclassify_cancelled_admission(state, observation)
    if state.phase is ControllerPhase.APPLY_PENDING and state.application is not None:
        action = state.application
        target = _unique_target(observation)
        if (
            target is not None
            and state.physical_token == observation.physical_token
            and target.profile == action.profile
            and observation.observation_key == action.key.observation_key
        ):
            action = replace(
                action, admitted_event_generation=observation.event_generation
            )
            state = replace(state, application=action, next_timer_ms=now_ms)
            return _decision(
                state,
                ApplyProfile(
                    action.action_id,
                    action.key,
                    action.profile,
                    action.mapping,
                    action.admitted_event_generation,
                    observation.observation_key,
                ),
            )
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, application=None)
        return _reclassify_cancelled_admission(state, observation)
    if (
        state.preparation_state is PreparationState.PREPARE_PENDING
        and state.preparation is not None
    ):
        action = state.preparation
        exact = (
            state.physical_token == observation.physical_token
            and observation.exact_profile == action.profile
            and action.profile in observation.current_profiles
            and observation.observation_key == action.observation_key
        )
        if exact:
            action = replace(
                action, admitted_event_generation=observation.event_generation
            )
            state = replace(state, preparation=action, next_timer_ms=now_ms)
            return _decision(
                state,
                PrepareDesktop(
                    action.action_id,
                    action.transition_id,
                    action.transition_key,
                    action.profile,
                    action.plan_hash,
                    action.admitted_event_generation,
                    observation.observation_key,
                ),
            )
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state = replace(
            state,
            preparation_state=PreparationState.PREPARE_IDLE,
            preparation=None,
            verify_since_ms=None,
        )
        return _reclassify_cancelled_admission(state, observation)
    if (
        state.phase is ControllerPhase.FINALIZE_PENDING
        and state.finalization is not None
    ):
        action = state.finalization
        if _same_exact_finalization(state, observation):
            action = replace(
                action, admitted_event_generation=observation.event_generation
            )
            state = replace(state, finalization=action, next_timer_ms=now_ms)
            return _decision(
                state,
                FinalizeDesktop(
                    action.action_id,
                    action.transition_id,
                    action.transition_key,
                    action.profile,
                    action.plan_hash,
                    action.admitted_event_generation,
                    observation.observation_key,
                ),
            )
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            finalization=None,
            verify_since_ms=None,
        )
        return _reclassify_cancelled_admission(state, observation)
    return None


def _handle_unplug(
    state: State, observation: CanonicalObservation
) -> tuple[State, bool]:
    if not state.external_intent:
        return replace(state, unplug_proof=None), True
    proof = state.unplug_proof
    if proof is None:
        proof = UnplugProof(
            observation.observation_key,
            observation.observed_at_ms,
            observation.observation_key,
            observation.observed_at_ms,
            1,
        )
    else:
        proof = replace(
            proof,
            latest_observation_key=observation.observation_key,
            latest_observed_at_ms=observation.observed_at_ms,
            observation_count=proof.observation_count + 1,
        )
    complete = (
        proof.observation_count >= UNPLUG_REQUIRED_SAMPLES
        and proof.latest_observed_at_ms - proof.first_observed_at_ms
        >= UNPLUG_STABILITY_MS
        and observation.event_generation == state.event_generation
    )
    if complete:
        return replace(state, external_intent=False, unplug_proof=None), True
    return replace(state, unplug_proof=proof, verify_since_ms=None), False


def _classify_observation(state: State, observation: CanonicalObservation) -> Decision:
    now_ms = observation.observed_at_ms
    pending = _observe_pending_action(state, observation)
    if pending is not None:
        return pending

    if observation.has_external_hardware:
        internal_candidate = (
            state.candidate is not None
            and state.candidate.scope is ProfileScope.INTERNAL_ONLY
        )
        state = replace(
            state,
            external_intent=True,
            unplug_proof=None,
            candidate=None if internal_candidate else state.candidate,
            verify_since_ms=None if internal_candidate else state.verify_since_ms,
        )
    else:
        state, unplug_complete = _handle_unplug(state, observation)
        if not unplug_complete:
            return _schedule(
                replace(state, phase=ControllerPhase.DISCOVER_FAST),
                max(now_ms + 1, state.unplug_proof.first_observed_at_ms + 1_000)
                if state.unplug_proof is not None
                else now_ms + 1,
            )

    if state.phase is ControllerPhase.PROBE_FAILED and state.probe is not None:
        same = (
            observation.probe_candidate is not None
            and observation.probe_candidate.profile == state.probe.key.profile
            and observation.observation_key == state.probe.key.observation_key
        )
        if same:
            return _schedule(state, now_ms + HEALTH_POLL_MS)
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, probe=None)
    if state.phase is ControllerPhase.APPLY_FAILED and state.application is not None:
        target = _unique_target(observation)
        same = (
            target is not None
            and target.profile == state.application.profile
            and observation.observation_key == state.application.key.observation_key
        )
        if same:
            return _schedule(state, now_ms + HEALTH_POLL_MS)
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, application=None)
    if (
        state.phase is ControllerPhase.FINALIZE_FAILED
        and state.finalization is not None
    ):
        if _same_exact_finalization(state, observation):
            return _schedule(state, now_ms + HEALTH_POLL_MS)
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, finalization=None)

    if observation.probe_candidate is not None:
        state = replace(state, unknown_key=None, unknown_since_ms=None)
        return _admit_probe(state, observation)

    target = _unique_target(observation)
    if (
        observation.has_external_hardware
        and target is not None
        and target.scope is ProfileScope.INTERNAL_ONLY
    ):
        target = None
    if (
        target is not None
        and observation.exact_profile == target.profile
        and target.profile in observation.current_profiles
    ):
        state = replace(state, unknown_key=None, unknown_since_ms=None)
        return _start_verification(state, observation, target)
    if target is not None:
        state = replace(state, unknown_key=None, unknown_since_ms=None)
        return _admit_application(state, observation, target)

    external_outputs = set(observation.kernel_external_outputs) | set(
        observation.x_external_outputs
    )
    complete_external_outputs = {
        item.output
        for item in observation.edid_integrity
        if item.integrity.value == "complete"
    }
    identified_external_outputs = {
        item.output
        for item in observation.connector_identities
        if item.x_connector_id is not None
    }
    complete_unknown = bool(external_outputs) and external_outputs <= (
        complete_external_outputs & identified_external_outputs
    )
    if complete_unknown:
        since = state.unknown_since_ms
        if state.unknown_key != observation.observation_key or since is None:
            since = now_ms
        state = replace(
            state,
            unknown_key=observation.observation_key,
            unknown_since_ms=since,
            verify_since_ms=None,
        )
        if now_ms - since >= UNKNOWN_STABILITY_MS:
            return _schedule(
                replace(state, phase=ControllerPhase.UNSUPPORTED),
                now_ms + HEALTH_POLL_MS,
            )
        return _schedule(
            replace(state, phase=ControllerPhase.DISCOVER_FAST), now_ms + 1_000
        )
    return _enter_wait(state, now_ms)


def _observe(state: State, event: ObservationCompleted) -> Decision:
    observation = event.observation
    now_ms = observation.observed_at_ms
    prior_observation = state.latest_observation
    if prior_observation is not None and (
        observation.observation_generation <= state.observation_generation
        or observation.observed_at_ms < prior_observation.observed_at_ms
        or observation.event_generation < state.event_generation
    ):
        return _no_op(state)
    state = replace(
        state,
        latest_observation=observation,
        observation_generation=max(
            state.observation_generation, observation.observation_generation
        ),
        event_generation=max(state.event_generation, observation.event_generation),
    )
    if not observation.valid:
        invalid_external = observation.has_external_hardware
        internal_candidate = (
            state.candidate is not None
            and state.candidate.scope is ProfileScope.INTERNAL_ONLY
        )
        state = replace(
            state,
            external_intent=state.external_intent or invalid_external,
            candidate=(
                None if invalid_external and internal_candidate else state.candidate
            ),
            unplug_proof=None,
            verify_since_ms=None,
        )
        invalid_effects: tuple[Effect, ...] = ()
        if (
            state.probe is not None
            and state.probe.lifecycle is ActionLifecycle.ADMITTED
        ):
            state = _append_tombstone(state, state.probe, ActionLifecycle.CANCELLED)
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                probe=None,
            )
        if (
            state.application is not None
            and state.application.lifecycle is ActionLifecycle.ADMITTED
        ):
            state = _append_tombstone(
                state, state.application, ActionLifecycle.CANCELLED
            )
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                application=None,
            )
        if (
            state.preparation is not None
            and state.preparation.lifecycle is ActionLifecycle.ADMITTED
        ):
            state = _append_tombstone(
                state, state.preparation, ActionLifecycle.CANCELLED
            )
            state, invalid_effects = _discard_planning(
                replace(
                    state,
                    phase=ControllerPhase.DISCOVER_FAST,
                    preparation_state=PreparationState.PREPARE_IDLE,
                    preparation=None,
                )
            )
        if (
            state.finalization is not None
            and state.finalization.lifecycle is ActionLifecycle.ADMITTED
        ):
            state = _append_tombstone(
                state, state.finalization, ActionLifecycle.CANCELLED
            )
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                finalization=None,
            )
        if (
            invalid_external
            and state.application is not None
            and state.application.scope is ProfileScope.INTERNAL_ONLY
            and state.application.lifecycle
            in {ActionLifecycle.DISPATCHED, ActionLifecycle.STOPPING}
        ):
            action = replace(state.application, lifecycle=ActionLifecycle.STOPPING)
            state = replace(state, application=action)
            return _schedule(
                state,
                now_ms + 1_000,
                *invalid_effects,
                StopAction(action.action_id),
            )
        if state.phase not in {
            ControllerPhase.PROBING,
            ControllerPhase.PROBE_FAILED,
            ControllerPhase.APPLYING,
            ControllerPhase.APPLY_FAILED,
            ControllerPhase.FINALIZING,
            ControllerPhase.FINALIZE_STOPPING,
            ControllerPhase.FINALIZE_FAILED,
        }:
            state = replace(state, phase=ControllerPhase.DISCOVER_FAST)
        return _schedule(state, now_ms + 1_000, *invalid_effects)

    if (
        observation.has_external_hardware
        and state.application is not None
        and state.application.scope is ProfileScope.INTERNAL_ONLY
        and state.application.lifecycle is ActionLifecycle.DISPATCHED
    ):
        action = replace(state.application, lifecycle=ActionLifecycle.STOPPING)
        state = replace(
            state,
            phase=ControllerPhase.APPLYING,
            application=action,
            candidate=None,
            verify_since_ms=None,
        )
        return _schedule(state, now_ms + 1_000, StopAction(action.action_id))

    # Stopping is a display-mutation exclusion. No observation may erase it
    # or admit another mutation before cancellation is acknowledged.
    stopping = next(
        (
            action
            for action in (
                state.probe,
                state.application,
                state.preparation,
                state.finalization,
            )
            if action is not None and action.lifecycle is ActionLifecycle.STOPPING
        ),
        None,
    )
    if stopping is not None:
        return _schedule(state, now_ms + 1_000)

    token_changed = state.physical_token != observation.physical_token
    if token_changed and state.phase is ControllerPhase.PROBING and state.probe:
        action = state.probe
        if action.lifecycle is ActionLifecycle.DISPATCHED:
            action = replace(action, lifecycle=ActionLifecycle.STOPPING)
            state = replace(state, probe=action)
            return _schedule(state, now_ms + 1_000, StopAction(action.action_id))
    if token_changed and state.phase is ControllerPhase.APPLYING and state.application:
        action = state.application
        if action.lifecycle is ActionLifecycle.DISPATCHED:
            action = replace(action, lifecycle=ActionLifecycle.STOPPING)
            state = replace(state, application=action)
            return _schedule(state, now_ms + 1_000, StopAction(action.action_id))
    if (
        token_changed
        and state.phase is ControllerPhase.FINALIZING
        and state.finalization
    ):
        action = state.finalization
        if action.lifecycle is ActionLifecycle.RESULT_PENDING:
            action = replace(action, lifecycle=ActionLifecycle.FAILED)
            state = _append_tombstone(state, action, ActionLifecycle.FAILED)
            state = replace(
                state,
                phase=ControllerPhase.FINALIZE_FAILED,
                finalization=action,
                verify_since_ms=None,
            )
            return _schedule(state, now_ms + HEALTH_POLL_MS)
        action = replace(action, lifecycle=ActionLifecycle.STOPPING)
        state = replace(
            state,
            phase=ControllerPhase.FINALIZE_STOPPING,
            finalization=action,
            verify_since_ms=None,
        )
        return _schedule(state, now_ms + 1_000, StopAction(action.action_id))
    if (
        token_changed
        and state.preparation_state is PreparationState.PREPARING
        and state.preparation is not None
    ):
        action = replace(state.preparation, lifecycle=ActionLifecycle.STOPPING)
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            preparation_state=PreparationState.PREPARE_STOPPING,
            preparation=action,
            verify_since_ms=None,
        )
        return _schedule(state, now_ms + 1_000, StopAction(action.action_id))

    epoch_effects: tuple[Effect, ...] = ()
    if token_changed:
        state, epoch_effects = _reset_for_epoch(state, observation, now_ms)
    elif (
        state.phase is ControllerPhase.WAIT_SLOW
        and prior_observation is not None
        and prior_observation.observation_key != observation.observation_key
    ):
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            aggressive_deadline_ms=now_ms + AGGRESSIVE_BUDGET_MS,
            backoff_index=0,
        )

    if state.phase is ControllerPhase.PROBING and state.probe is not None:
        return _schedule(state, now_ms + 1_000, *epoch_effects)
    if state.phase is ControllerPhase.APPLYING and state.application is not None:
        return _schedule(state, now_ms + 1_000, *epoch_effects)

    if state.phase is ControllerPhase.FINALIZING and state.finalization is not None:
        action = state.finalization
        if action.lifecycle is ActionLifecycle.RESULT_PENDING:
            if _same_exact_finalization(state, observation):
                state = _append_tombstone(state, action, ActionLifecycle.COMPLETED)
                state, discard_effects = _discard_planning(state)
                state = replace(
                    state,
                    phase=ControllerPhase.QUIESCENT,
                    stable_x_profile=action.profile,
                    desktop_finalized_profile=action.profile,
                    finalization=None,
                )
                return _schedule(
                    state,
                    now_ms + HEALTH_POLL_MS,
                    *epoch_effects,
                    *discard_effects,
                )
            action = replace(action, lifecycle=ActionLifecycle.FAILED)
            state = _append_tombstone(state, action, ActionLifecycle.FAILED)
            state = replace(
                state,
                phase=ControllerPhase.FINALIZE_FAILED,
                finalization=action,
            )
            return _schedule(state, now_ms + HEALTH_POLL_MS, *epoch_effects)
        if _same_exact_finalization(state, observation):
            return _schedule(state, now_ms + 1_000, *epoch_effects)
        action = replace(action, lifecycle=ActionLifecycle.STOPPING)
        state = replace(
            state,
            phase=ControllerPhase.FINALIZE_STOPPING,
            finalization=action,
            verify_since_ms=None,
        )
        return _schedule(
            state, now_ms + 1_000, *epoch_effects, StopAction(action.action_id)
        )

    if (
        state.preparation_state is PreparationState.PREPARING
        and state.preparation is not None
    ):
        action = state.preparation
        exact = (
            state.physical_token == observation.physical_token
            and observation.exact_profile == action.profile
            and action.profile in observation.current_profiles
            and observation.observation_key == action.observation_key
            and action.plan_hash
            == (state.planning.plan_hash if state.planning is not None else None)
        )
        if action.lifecycle is ActionLifecycle.RESULT_PENDING:
            if exact:
                action = replace(action, lifecycle=ActionLifecycle.COMPLETED)
                state = replace(
                    state,
                    preparation_state=PreparationState.PREPARED,
                    preparation=action,
                )
            else:
                action = replace(action, lifecycle=ActionLifecycle.FAILED)
                state = _append_tombstone(state, action, ActionLifecycle.FAILED)
                state = replace(
                    state,
                    preparation_state=PreparationState.PREPARE_FAILED,
                    preparation=action,
                    verify_since_ms=None,
                )
                return _schedule(state, now_ms + HEALTH_POLL_MS, *epoch_effects)
        elif not exact:
            action = replace(action, lifecycle=ActionLifecycle.STOPPING)
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                preparation_state=PreparationState.PREPARE_STOPPING,
                preparation=action,
                verify_since_ms=None,
            )
            return _schedule(
                state,
                now_ms + 1_000,
                *epoch_effects,
                StopAction(action.action_id),
            )
        else:
            return _schedule(state, now_ms + 1_000, *epoch_effects)

    decision = _classify_observation(state, observation)
    if not epoch_effects:
        return decision
    return _decision(decision.state, *epoch_effects, *decision.effects)


def _drm_hint(state: State, event: DrmHintReceived) -> Decision:
    if event.event_generation <= state.event_generation:
        return _no_op(state)
    state = replace(
        state,
        event_generation=event.event_generation,
        last_drm_at_ms=event.metadata.processed_at_ms,
        verify_since_ms=None,
        unplug_proof=None,
    )
    effects: tuple[Effect, ...] = ()
    if (
        state.preparation_state is PreparationState.PREPARE_PENDING
        and state.preparation is not None
    ):
        state = _append_tombstone(state, state.preparation, ActionLifecycle.CANCELLED)
        state, effects = _discard_planning(
            replace(
                state,
                preparation_state=PreparationState.PREPARE_IDLE,
                preparation=None,
            )
        )
    if (
        state.phase is ControllerPhase.FINALIZE_PENDING
        and state.finalization is not None
    ):
        state = _append_tombstone(state, state.finalization, ActionLifecycle.CANCELLED)
        state = replace(state, finalization=None)
    if state.phase in {
        ControllerPhase.QUIESCENT,
        ControllerPhase.VERIFYING,
        ControllerPhase.UNSUPPORTED,
        ControllerPhase.FINALIZE_PENDING,
    }:
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST)
    return _schedule(
        state,
        event.metadata.processed_at_ms,
        *effects,
        RequestObservation(WakeReason.DRM_HINT),
    )


def _dispatched(
    state: State,
    action_id: ActionId,
    unit: WorkerUnit,
) -> Decision:
    if action_id.kind is ActionKind.PROBE:
        action = state.probe
        if (
            state.phase is not ControllerPhase.PROBE_PENDING
            or action is None
            or action.action_id != action_id
            or action.lifecycle is not ActionLifecycle.ADMITTED
        ):
            return _no_op(state)
        action = replace(action, lifecycle=ActionLifecycle.DISPATCHED, unit=unit)
        state = replace(
            state,
            phase=ControllerPhase.PROBING,
            probe=action,
            attempted_probe_keys=state.attempted_probe_keys | {action.key},
            next_timer_ms=None,
        )
    elif action_id.kind is ActionKind.APPLICATION:
        action = state.application
        if (
            state.phase is not ControllerPhase.APPLY_PENDING
            or action is None
            or action.action_id != action_id
            or action.lifecycle is not ActionLifecycle.ADMITTED
        ):
            return _no_op(state)
        action = replace(action, lifecycle=ActionLifecycle.DISPATCHED, unit=unit)
        state = replace(
            state,
            phase=ControllerPhase.APPLYING,
            application=action,
            attempted_application_keys=state.attempted_application_keys | {action.key},
            next_timer_ms=None,
        )
    elif action_id.kind is ActionKind.PREPARATION:
        action = state.preparation
        if (
            state.preparation_state is not PreparationState.PREPARE_PENDING
            or action is None
            or action.action_id != action_id
            or action.lifecycle is not ActionLifecycle.ADMITTED
        ):
            return _no_op(state)
        action = replace(action, lifecycle=ActionLifecycle.DISPATCHED, unit=unit)
        state = replace(
            state,
            preparation_state=PreparationState.PREPARING,
            preparation=action,
        )
    elif action_id.kind is ActionKind.FINALIZATION:
        action = state.finalization
        if (
            state.phase is not ControllerPhase.FINALIZE_PENDING
            or action is None
            or action.action_id != action_id
            or action.lifecycle is not ActionLifecycle.ADMITTED
        ):
            return _no_op(state)
        action = replace(action, lifecycle=ActionLifecycle.DISPATCHED, unit=unit)
        state = replace(
            state,
            phase=ControllerPhase.FINALIZING,
            finalization=action,
        )
    else:
        return _no_op(state)
    return _decision(state)


def _finished(
    state: State,
    action_id: ActionId,
    outcome: WorkerOutcome,
    exit_status: int | None,
    plan_hash: PlanHash | None = None,
) -> Decision:
    now_ms = state.latest_observation.observed_at_ms if state.latest_observation else 0
    if action_id.kind is ActionKind.PROBE:
        action = state.probe
        if (
            action is None
            or action.action_id != action_id
            or state.phase is not ControllerPhase.PROBING
        ):
            return _no_op(state)
        if outcome is WorkerOutcome.SUCCEEDED:
            action = replace(
                action,
                lifecycle=ActionLifecycle.COMPLETED,
                exit_status=exit_status,
                terminal_after_stop=None,
            )
            state = _append_tombstone(state, action, ActionLifecycle.COMPLETED)
            state = replace(state, phase=ControllerPhase.DISCOVER_FAST, probe=None)
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        lifecycle = (
            ActionLifecycle.CANCELLED
            if outcome is WorkerOutcome.CANCELLED
            else ActionLifecycle.FAILED
        )
        action = replace(
            action,
            lifecycle=lifecycle,
            exit_status=exit_status,
            terminal_after_stop=None,
        )
        state = _append_tombstone(state, action, lifecycle)
        if lifecycle is ActionLifecycle.CANCELLED:
            state = replace(state, phase=ControllerPhase.DISCOVER_FAST, probe=None)
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        state = replace(state, phase=ControllerPhase.PROBE_FAILED, probe=action)
        return _schedule(state, now_ms + HEALTH_POLL_MS)
    if action_id.kind is ActionKind.APPLICATION:
        action = state.application
        if (
            action is None
            or action.action_id != action_id
            or state.phase is not ControllerPhase.APPLYING
        ):
            return _no_op(state)
        if outcome is WorkerOutcome.SUCCEEDED:
            action = replace(
                action,
                lifecycle=ActionLifecycle.COMPLETED,
                exit_status=exit_status,
                terminal_after_stop=None,
            )
            state = _append_tombstone(state, action, ActionLifecycle.COMPLETED)
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                application=None,
                verify_since_ms=None,
            )
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        lifecycle = (
            ActionLifecycle.CANCELLED
            if outcome is WorkerOutcome.CANCELLED
            else ActionLifecycle.FAILED
        )
        action = replace(
            action,
            lifecycle=lifecycle,
            exit_status=exit_status,
            terminal_after_stop=None,
        )
        state = _append_tombstone(state, action, lifecycle)
        if lifecycle is ActionLifecycle.CANCELLED:
            state = replace(
                state, phase=ControllerPhase.DISCOVER_FAST, application=None
            )
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        state = replace(state, phase=ControllerPhase.APPLY_FAILED, application=action)
        return _schedule(state, now_ms + HEALTH_POLL_MS)
    if action_id.kind is ActionKind.PREPARATION:
        action = state.preparation
        if (
            action is None
            or action.action_id != action_id
            or action.lifecycle
            not in {ActionLifecycle.DISPATCHED, ActionLifecycle.STOPPING}
            or state.preparation_state
            not in {PreparationState.PREPARING, PreparationState.PREPARE_STOPPING}
        ):
            return _no_op(state)
        if outcome is WorkerOutcome.SUCCEEDED and plan_hash == action.plan_hash:
            if state.preparation_state is PreparationState.PREPARE_STOPPING:
                action = replace(
                    action,
                    lifecycle=ActionLifecycle.CANCELLED,
                    exit_status=exit_status,
                    terminal_after_stop=None,
                )
                state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
                state, discard_effects = _discard_planning(
                    replace(
                        state,
                        preparation_state=PreparationState.PREPARE_IDLE,
                        preparation=None,
                    )
                )
                return _schedule(
                    replace(state, phase=ControllerPhase.DISCOVER_FAST),
                    now_ms,
                    *discard_effects,
                    RequestObservation(WakeReason.WORKER_COMPLETED),
                )
            action = replace(
                action,
                lifecycle=ActionLifecycle.RESULT_PENDING,
                exit_status=exit_status,
                terminal_after_stop=None,
            )
            state = replace(state, preparation=action)
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        lifecycle = (
            ActionLifecycle.CANCELLED
            if outcome is WorkerOutcome.CANCELLED
            else ActionLifecycle.FAILED
        )
        action = replace(
            action,
            lifecycle=lifecycle,
            exit_status=exit_status,
            terminal_after_stop=None,
        )
        state = _append_tombstone(state, action, lifecycle)
        if lifecycle is ActionLifecycle.CANCELLED:
            state = replace(
                state,
                preparation_state=PreparationState.PREPARE_IDLE,
                preparation=None,
                phase=ControllerPhase.DISCOVER_FAST,
            )
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        state = replace(
            state,
            preparation_state=PreparationState.PREPARE_FAILED,
            preparation=action,
        )
        return _schedule(state, now_ms + HEALTH_POLL_MS)
    if action_id.kind is ActionKind.FINALIZATION:
        action = state.finalization
        if (
            action is None
            or action.action_id != action_id
            or action.lifecycle
            not in {ActionLifecycle.DISPATCHED, ActionLifecycle.STOPPING}
            or state.phase
            not in {ControllerPhase.FINALIZING, ControllerPhase.FINALIZE_STOPPING}
        ):
            return _no_op(state)
        if outcome is WorkerOutcome.SUCCEEDED:
            action = replace(
                action,
                lifecycle=ActionLifecycle.RESULT_PENDING,
                exit_status=exit_status,
                terminal_after_stop=None,
            )
            state = replace(
                state, phase=ControllerPhase.FINALIZING, finalization=action
            )
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        lifecycle = (
            ActionLifecycle.CANCELLED
            if outcome is WorkerOutcome.CANCELLED
            else ActionLifecycle.FAILED
        )
        action = replace(
            action,
            lifecycle=lifecycle,
            exit_status=exit_status,
            terminal_after_stop=None,
        )
        state = _append_tombstone(state, action, lifecycle)
        if lifecycle is ActionLifecycle.CANCELLED:
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                finalization=None,
                verify_since_ms=None,
            )
            return _schedule(
                state, now_ms, RequestObservation(WakeReason.WORKER_COMPLETED)
            )
        state = replace(
            state,
            phase=ControllerPhase.FINALIZE_FAILED,
            finalization=action,
        )
        return _schedule(state, now_ms + HEALTH_POLL_MS)
    return _no_op(state)


def _plan_requested(state: State, event: PlanRequested) -> Decision:
    action = state.planning
    if (
        state.planning_state is not PlanningState.PLAN_PENDING
        or action is None
        or action.action_id != event.action_id
        or action.input_key != event.input_key
    ):
        return _no_op(state)
    return _decision(
        replace(
            state,
            planning_state=PlanningState.PLANNING,
            planning=replace(action, lifecycle=ActionLifecycle.DISPATCHED),
        )
    )


def _plan_completed(state: State, event: PlanCompleted) -> Decision:
    action = state.planning
    if (
        state.planning_state is not PlanningState.PLANNING
        or action is None
        or action.action_id != event.action_id
        or action.input_key != event.input_key
    ):
        return _no_op(state)
    action = replace(
        action, lifecycle=ActionLifecycle.COMPLETED, plan_hash=event.plan_hash
    )
    state = replace(state, planning_state=PlanningState.PLAN_READY, planning=action)
    return _schedule(
        state,
        event.metadata.processed_at_ms,
        RequestObservation(WakeReason.WORKER_COMPLETED),
    )


def _plan_failed(state: State, event: PlanFailed) -> Decision:
    action = state.planning
    if (
        state.planning_state not in {PlanningState.PLAN_PENDING, PlanningState.PLANNING}
        or action is None
        or action.action_id != event.action_id
        or action.input_key != event.input_key
    ):
        return _no_op(state)
    action = replace(
        action, lifecycle=ActionLifecycle.FAILED, exit_status=event.exit_status
    )
    state = _append_tombstone(state, action, ActionLifecycle.FAILED)
    return _schedule(
        replace(state, planning_state=PlanningState.PLAN_FAILED, planning=action),
        event.metadata.processed_at_ms + HEALTH_POLL_MS,
    )


def _action_for_id(state: State, action_id: ActionId) -> ActionRecord | None:
    return next(
        (
            action
            for action in (
                state.probe,
                state.application,
                state.planning,
                state.preparation,
                state.finalization,
            )
            if action is not None and action.action_id == action_id
        ),
        None,
    )


def _fail_action(
    state: State, action_id: ActionId, lifecycle: ActionLifecycle, now_ms: int
) -> Decision:
    action = _action_for_id(state, action_id)
    if action is None or action.lifecycle not in {
        ActionLifecycle.ADMITTED,
        ActionLifecycle.DISPATCHED,
        ActionLifecycle.STOPPING,
    }:
        return _no_op(state)
    failed = replace(action, lifecycle=lifecycle)
    state = _append_tombstone(state, failed, lifecycle)
    if action_id.kind is ActionKind.PLAN:
        state = replace(
            state, planning_state=PlanningState.PLAN_FAILED, planning=failed
        )
    elif action_id.kind is ActionKind.PROBE:
        state = replace(state, phase=ControllerPhase.PROBE_FAILED, probe=failed)
    elif action_id.kind is ActionKind.APPLICATION:
        state = replace(state, phase=ControllerPhase.APPLY_FAILED, application=failed)
    elif action_id.kind is ActionKind.PREPARATION:
        state = replace(
            state,
            preparation_state=PreparationState.PREPARE_FAILED,
            preparation=failed,
        )
    elif action_id.kind is ActionKind.FINALIZATION:
        state = replace(
            state, phase=ControllerPhase.FINALIZE_FAILED, finalization=failed
        )
    return _schedule(state, now_ms + HEALTH_POLL_MS)


def _admission_dirtied(state: State, event: AdmissionDirtied) -> Decision:
    action = _action_for_id(state, event.action_id)
    if (
        action is None
        or action.lifecycle is not ActionLifecycle.ADMITTED
        or event.event_generation <= state.event_generation
    ):
        return _no_op(state)
    state = replace(
        state,
        event_generation=event.event_generation,
        last_drm_at_ms=event.metadata.processed_at_ms,
        verify_since_ms=None,
        unplug_proof=None,
    )
    effects: tuple[Effect, ...] = ()
    if action.action_id.kind is ActionKind.PREPARATION:
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state, effects = _discard_planning(
            replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                preparation_state=PreparationState.PREPARE_IDLE,
                preparation=None,
            )
        )
    elif action.action_id.kind is ActionKind.FINALIZATION:
        state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            finalization=None,
        )
    return _schedule(
        state,
        event.metadata.processed_at_ms,
        *effects,
        RequestObservation(WakeReason.DIRTY_ADMISSION),
    )


def _stop_uncertain_mutator(
    state: State, action: ActionRecord, terminal: ActionLifecycle, now_ms: int
) -> Decision:
    """Hold mutation exclusion until the supervisor confirms the worker stopped."""
    if isinstance(action, PlanningAction):
        return _fail_action(state, action.action_id, terminal, now_ms)
    stopping = replace(
        action,
        lifecycle=ActionLifecycle.STOPPING,
        terminal_after_stop=terminal,
    )
    if action.action_id.kind is ActionKind.PROBE:
        state = replace(state, phase=ControllerPhase.PROBING, probe=stopping)
    elif action.action_id.kind is ActionKind.APPLICATION:
        state = replace(state, phase=ControllerPhase.APPLYING, application=stopping)
    elif action.action_id.kind is ActionKind.PREPARATION:
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            preparation_state=PreparationState.PREPARE_STOPPING,
            preparation=stopping,
            verify_since_ms=None,
        )
    elif action.action_id.kind is ActionKind.FINALIZATION:
        state = replace(
            state,
            phase=ControllerPhase.FINALIZE_STOPPING,
            finalization=stopping,
            verify_since_ms=None,
        )
    else:
        return _fail_action(state, action.action_id, terminal, now_ms)
    return _schedule(state, now_ms + 1_000, StopAction(action.action_id))


def _cancellation_acknowledged(
    state: State, event: WorkerCancellationAcknowledged
) -> Decision:
    action = _action_for_id(state, event.action_id)
    if (
        action is None
        or isinstance(action, PlanningAction)
        or action.lifecycle is not ActionLifecycle.STOPPING
    ):
        return _no_op(state)
    terminal_after_stop = action.terminal_after_stop
    if terminal_after_stop is not None:
        terminal = replace(
            action,
            lifecycle=terminal_after_stop,
            terminal_after_stop=None,
        )
        state = _append_tombstone(state, terminal, terminal_after_stop)
        if action.action_id.kind is ActionKind.PROBE:
            state = replace(state, phase=ControllerPhase.PROBE_FAILED, probe=terminal)
        elif action.action_id.kind is ActionKind.APPLICATION:
            state = replace(
                state, phase=ControllerPhase.APPLY_FAILED, application=terminal
            )
        elif action.action_id.kind is ActionKind.PREPARATION:
            state = replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                preparation_state=PreparationState.PREPARE_FAILED,
                preparation=terminal,
            )
        elif action.action_id.kind is ActionKind.FINALIZATION:
            state = replace(
                state,
                phase=ControllerPhase.FINALIZE_FAILED,
                finalization=terminal,
            )
        else:
            return _no_op(state)
        return _schedule(state, event.metadata.processed_at_ms + HEALTH_POLL_MS)

    state = _append_tombstone(state, action, ActionLifecycle.CANCELLED)
    effects: tuple[Effect, ...] = ()
    if action.action_id.kind is ActionKind.PROBE:
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, probe=None)
    elif action.action_id.kind is ActionKind.APPLICATION:
        state = replace(state, phase=ControllerPhase.DISCOVER_FAST, application=None)
    elif action.action_id.kind is ActionKind.PREPARATION:
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            preparation_state=PreparationState.PREPARE_IDLE,
            preparation=None,
        )
        if state.planning is not None:
            effects = (DiscardPlan(state.planning.action_id, state.planning.plan_hash),)
    elif action.action_id.kind is ActionKind.FINALIZATION:
        state = replace(
            state,
            phase=ControllerPhase.DISCOVER_FAST,
            finalization=None,
            verify_since_ms=None,
        )
    else:
        return _no_op(state)
    return _schedule(
        state,
        event.metadata.processed_at_ms,
        *effects,
        RequestObservation(WakeReason.WORKER_COMPLETED),
    )


def _controller_started(state: State, event: ControllerStarted) -> Decision:
    state = replace(state, controller_instance=event.controller_instance)
    if (
        state.preparation_state is PreparationState.PREPARE_PENDING
        and state.preparation is not None
    ):
        state = _append_tombstone(state, state.preparation, ActionLifecycle.CANCELLED)
        state, discard_effects = _discard_planning(
            replace(
                state,
                phase=ControllerPhase.DISCOVER_FAST,
                preparation_state=PreparationState.PREPARE_IDLE,
                preparation=None,
                verify_since_ms=None,
            )
        )
        return _schedule(
            state,
            event.metadata.processed_at_ms,
            *discard_effects,
            RequestObservation(WakeReason.STARTUP),
        )
    if state.phase is ControllerPhase.VERIFYING:
        state = replace(state, verify_since_ms=None)
    if state.phase is ControllerPhase.PROBING and state.probe is not None:
        return _schedule(state, event.metadata.processed_at_ms + 1_000)
    if state.phase is ControllerPhase.APPLYING and state.application is not None:
        return _schedule(state, event.metadata.processed_at_ms + 1_000)
    if (
        state.preparation_state
        in {PreparationState.PREPARING, PreparationState.PREPARE_STOPPING}
        and state.preparation is not None
    ):
        return _schedule(state, event.metadata.processed_at_ms + 1_000)
    if (
        state.phase
        in {
            ControllerPhase.FINALIZING,
            ControllerPhase.FINALIZE_STOPPING,
        }
        and state.finalization is not None
    ):
        return _schedule(state, event.metadata.processed_at_ms + 1_000)
    return _schedule(
        state,
        event.metadata.processed_at_ms,
        RequestObservation(WakeReason.STARTUP),
    )


def _boot_changed(state: State, event: BootChanged) -> Decision:
    if event.previous_boot_id != state.boot_id:
        return _no_op(state)
    state = replace(
        state,
        boot_id=event.metadata.boot_id,
        phase=ControllerPhase.RECOVERING,
        planning_state=PlanningState.PLAN_IDLE,
        preparation_state=PreparationState.PREPARE_IDLE,
        latest_observation=None,
        physical_token=None,
        candidate=None,
        aggressive_deadline_ms=None,
        next_timer_ms=event.metadata.processed_at_ms,
        backoff_index=0,
        verify_since_ms=None,
        last_drm_at_ms=None,
        stable_x_profile=None,
        external_intent=False,
        baseline_adoption=state.desktop_finalized_profile is None,
        attempted_probe_keys=frozenset(),
        probe=None,
        attempted_application_keys=frozenset(),
        application=None,
        planning=None,
        preparation=None,
        finalization=None,
        unknown_key=None,
        unknown_since_ms=None,
        unplug_proof=None,
        observation_generation=type(state.observation_generation)(0),
        event_generation=EventGeneration(0),
        recovery_units=(),
    )
    return _decision(
        state,
        RequestObservation(WakeReason.RECOVERY),
        Schedule(event.metadata.processed_at_ms),
    )


def reduce(state: State, event: Event) -> Decision:
    """Return the deterministic state/effect decision for one closed-union event.

    Stale, mismatched, or contextually invalid events preserve state and emit no
    effects. Structurally impossible state is rejected by the central invariants.
    """
    assert_controller_invariants(state)
    if not isinstance(event, EVENT_TYPES):
        msg = f"event is outside the closed Event union: {type(event).__name__}"
        raise UnknownEventError(msg)
    if isinstance(event, BootChanged):
        return _boot_changed(state, event)
    if event.metadata.boot_id != state.boot_id:
        return _no_op(state)

    if isinstance(event, ObservationCompleted):
        return _observe(state, event)
    if isinstance(event, DrmHintReceived):
        return _drm_hint(state, event)
    if isinstance(event, TimerFired):
        if (
            state.next_timer_ms is None
            or event.deadline_ms < state.next_timer_ms
            or event.metadata.processed_at_ms < event.deadline_ms
        ):
            return _no_op(state)
        state = replace(state, next_timer_ms=event.metadata.processed_at_ms)
        return _decision(
            state,
            RequestObservation(WakeReason.TIMER),
            Schedule(event.metadata.processed_at_ms),
        )
    if isinstance(event, AdmissionDirtied):
        return _admission_dirtied(state, event)
    if isinstance(
        event,
        ProbeDispatched
        | ApplicationDispatched
        | PreparationDispatched
        | FinalizationDispatched,
    ):
        return _dispatched(state, event.action_id, event.unit)
    if isinstance(event, ProbeFinished | ApplicationFinished | FinalizationFinished):
        return _finished(state, event.action_id, event.outcome, event.exit_status)
    if isinstance(event, PreparationFinished):
        return _finished(
            state,
            event.action_id,
            event.outcome,
            event.exit_status,
            event.plan_hash,
        )
    if isinstance(event, PlanRequested):
        return _plan_requested(state, event)
    if isinstance(event, PlanCompleted):
        return _plan_completed(state, event)
    if isinstance(event, PlanFailed):
        return _plan_failed(state, event)
    if isinstance(event, DispatchRejected):
        action = _action_for_id(state, event.action_id)
        if action is None or action.lifecycle is not ActionLifecycle.ADMITTED:
            return _no_op(state)
        return _fail_action(
            state,
            event.action_id,
            ActionLifecycle.FAILED,
            event.metadata.processed_at_ms,
        )
    if isinstance(event, WorkerStatusUnknown):
        action = _action_for_id(state, event.action_id)
        if action is None or action.lifecycle not in {
            ActionLifecycle.DISPATCHED,
            ActionLifecycle.STOPPING,
        }:
            return _no_op(state)
        return _stop_uncertain_mutator(
            state,
            action,
            ActionLifecycle.UNKNOWN,
            event.metadata.processed_at_ms,
        )
    if isinstance(event, WorkerTimedOut):
        action = _action_for_id(state, event.action_id)
        if action is None or action.lifecycle not in {
            ActionLifecycle.DISPATCHED,
            ActionLifecycle.STOPPING,
        }:
            return _no_op(state)
        return _stop_uncertain_mutator(
            state,
            action,
            ActionLifecycle.TIMED_OUT,
            event.metadata.processed_at_ms,
        )
    if isinstance(event, WorkerCancellationAcknowledged):
        return _cancellation_acknowledged(state, event)
    return _controller_started(state, event)
