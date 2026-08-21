"""Central fail-closed invariants for immutable controller state."""

from __future__ import annotations

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionRecord,
    ControllerPhase,
    PlanningAction,
    PlanningState,
    PreparationState,
    ProfileScope,
    State,
    TransitionId,
)

_MUTATING_IN_FLIGHT = {
    ActionLifecycle.DISPATCHED,
    ActionLifecycle.STOPPING,
    ActionLifecycle.RESULT_PENDING,
}
_FAILED_LIFECYCLES = {
    ActionLifecycle.FAILED,
    ActionLifecycle.UNKNOWN,
    ActionLifecycle.TIMED_OUT,
}
_PROFILE_STABILITY_MS = 10_000
_PREPARATION_STABILITY_MS = 2_000

NUMBERED_INVARIANTS: tuple[str, ...] = (
    "no_laptop_fallback_with_external_hardware",
    "probe_is_not_profile_authorization",
    "explicit_applications_only",
    "no_duplicate_probe_or_application",
    "edid_absence_is_uncertainty_not_unplug",
    "continuous_final_proof",
    "durable_action_dispatch",
    "independent_desktop_state",
    "profile_transition_finalization",
    "explicit_unplug_proof",
    "no_implicit_abandonment",
    "preparation_is_subordinate_and_keyed",
    "one_display_mutation_boundary",
)


class ControllerInvariantError(ValueError):
    """Raised when state could authorize unsafe or ambiguous behavior."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControllerInvariantError(message)


def _check_phase_relationships(state: State) -> None:
    if state.phase is ControllerPhase.RECOVERING:
        return

    expected_probe_lifecycles = {
        ControllerPhase.PROBE_PENDING: {ActionLifecycle.ADMITTED},
        ControllerPhase.PROBING: _MUTATING_IN_FLIGHT,
        ControllerPhase.PROBE_FAILED: _FAILED_LIFECYCLES,
    }
    expected_application_lifecycles = {
        ControllerPhase.APPLY_PENDING: {ActionLifecycle.ADMITTED},
        ControllerPhase.APPLYING: _MUTATING_IN_FLIGHT,
        ControllerPhase.APPLY_FAILED: _FAILED_LIFECYCLES,
    }
    expected_finalization_lifecycles = {
        ControllerPhase.FINALIZE_PENDING: {ActionLifecycle.ADMITTED},
        ControllerPhase.FINALIZING: {
            ActionLifecycle.DISPATCHED,
            ActionLifecycle.RESULT_PENDING,
        },
        ControllerPhase.FINALIZE_STOPPING: {ActionLifecycle.STOPPING},
        ControllerPhase.FINALIZE_FAILED: _FAILED_LIFECYCLES,
    }

    for action, expected, label in (
        (state.probe, expected_probe_lifecycles.get(state.phase), "probe"),
        (
            state.application,
            expected_application_lifecycles.get(state.phase),
            "application",
        ),
        (
            state.finalization,
            expected_finalization_lifecycles.get(state.phase),
            "finalization",
        ),
    ):
        if expected is None:
            _require(action is None, f"{label} identity requires its matching phase")
        else:
            _require(action is not None, f"{label} phase requires a persisted identity")
            _require(
                action is not None and action.lifecycle in expected,
                f"{label} lifecycle does not match controller phase",
            )


def _check_planning_relationships(state: State) -> None:
    if state.phase is ControllerPhase.RECOVERING:
        return

    expected_planning = {
        PlanningState.PLAN_PENDING: {ActionLifecycle.ADMITTED},
        PlanningState.PLANNING: {ActionLifecycle.DISPATCHED},
        PlanningState.PLAN_READY: {ActionLifecycle.COMPLETED},
        PlanningState.PLAN_FAILED: _FAILED_LIFECYCLES,
    }
    planning_lifecycles = expected_planning.get(state.planning_state)
    if planning_lifecycles is None:
        _require(state.planning is None, "idle planning state cannot retain an action")
    else:
        _require(
            state.planning is not None,
            "non-idle planning state requires a persisted planning identity",
        )
        _require(
            state.planning is not None
            and state.planning.lifecycle in planning_lifecycles,
            "planning lifecycle does not match planning state",
        )
    if state.planning_state is PlanningState.PLAN_READY:
        _require(
            state.planning is not None and state.planning.plan_hash is not None,
            "ready planning state requires a staged plan hash",
        )

    expected_preparation = {
        PreparationState.PREPARE_PENDING: {ActionLifecycle.ADMITTED},
        PreparationState.PREPARING: {
            ActionLifecycle.DISPATCHED,
            ActionLifecycle.RESULT_PENDING,
        },
        PreparationState.PREPARED: {ActionLifecycle.COMPLETED},
        PreparationState.PREPARE_STOPPING: {ActionLifecycle.STOPPING},
        PreparationState.PREPARE_FAILED: _FAILED_LIFECYCLES,
    }
    preparation_lifecycles = expected_preparation.get(state.preparation_state)
    if preparation_lifecycles is None:
        _require(
            state.preparation is None,
            "idle preparation state cannot retain an action",
        )
    else:
        _require(
            state.preparation is not None,
            "non-idle preparation state requires a persisted identity",
        )
        _require(
            state.preparation is not None
            and state.preparation.lifecycle in preparation_lifecycles,
            "preparation lifecycle does not match preparation state",
        )

    if state.preparation is not None:
        _require(
            state.planning is not None
            and state.planning_state is PlanningState.PLAN_READY,
            "preparation requires a ready persisted plan",
        )
        _require(
            state.planning is not None
            and state.preparation.plan_hash == state.planning.plan_hash,
            "preparation must use the currently completed plan hash",
        )
        _require(
            state.planning is not None
            and state.preparation.transition_id == state.planning.transition_id
            and state.preparation.profile == state.planning.profile,
            "preparation and planning transition identities must match",
        )
    if state.baseline_adoption:
        _require(
            state.planning_state is PlanningState.PLAN_IDLE
            and state.preparation_state is PreparationState.PREPARE_IDLE,
            "startup baseline adoption forbids planning and preparation",
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


def _check_action_identities(state: State) -> None:
    actions = _actions(state)
    action_ids = tuple(action.action_id for action in actions)
    _require(
        len(set(action_ids)) == len(action_ids),
        "active action IDs must be unique",
    )
    for action_id in action_ids:
        _check_action_id(state, action_id)

    tombstone_ids = tuple(item.action_id for item in state.action_tombstones)
    _require(
        len(set(tombstone_ids)) == len(tombstone_ids),
        "action tombstone IDs must be unique",
    )
    for action_id in tombstone_ids:
        _check_action_id(state, action_id)

    unit_ids = tuple(unit.action_id for unit in state.recovery_units)
    _require(
        len(set(unit_ids)) == len(unit_ids),
        "recovery worker unit action IDs must be unique",
    )
    for action_id in unit_ids:
        _check_action_id(state, action_id)

    transitions = tuple(
        action.transition_id
        for action in (state.planning, state.preparation, state.finalization)
        if action is not None
    )
    for transition_id in transitions:
        _check_transition_id(state, transition_id)


def _check_action_id(state: State, action_id: ActionId) -> None:
    # Recovery must retain surviving IDs from previous controller instances. The
    # persisted high-water mark belongs only to the current instance's allocator.
    if action_id.controller_instance == state.controller_instance:
        _require(
            action_id.sequence <= state.action_sequence_high_water,
            "current-instance action ID exceeds its sequence high-water mark",
        )


def _check_transition_id(state: State, transition_id: TransitionId) -> None:
    if transition_id.controller_instance == state.controller_instance:
        _require(
            transition_id.sequence <= state.transition_sequence_high_water,
            "current-instance transition ID exceeds its sequence high-water mark",
        )


def _check_worker_acknowledgements(state: State) -> None:
    mutating_actions = tuple(
        action
        for action in (
            state.probe,
            state.application,
            state.preparation,
            state.finalization,
        )
        if action is not None and action.lifecycle in _MUTATING_IN_FLIGHT
    )
    _require(
        len(mutating_actions) <= 1,
        "more than one display mutation is acknowledged in flight",
    )
    for action in _actions(state):
        lifecycle = action.lifecycle
        unit = None if isinstance(action, PlanningAction) else action.unit
        if (
            lifecycle in _MUTATING_IN_FLIGHT
            and action.action_id.kind is not ActionKind.PLAN
        ):
            _require(
                unit is not None,
                "acknowledged worker must retain its unit identity",
            )


def _check_observation_authority(state: State) -> None:
    observation = state.latest_observation
    if observation is None:
        return
    _require(
        observation.boot_id == state.boot_id,
        "latest observation belongs to another boot",
    )
    _require(
        state.observation_generation >= observation.observation_generation,
        "state observation generation precedes its latest observation",
    )
    _require(
        state.event_generation >= observation.end_event_generation,
        "observation event generation exceeds the controller event generation",
    )
    if state.candidate is not None:
        _require(
            state.candidate.mapping.physical_epoch == state.physical_epoch,
            "candidate mapping proof belongs to another physical epoch",
        )
        # A torn/invalid sample is diagnostic evidence, not a revocation. The
        # candidate remains bound to the observation which proved its mapping,
        # rather than being rebound to the latest observation key.
        _require(
            not (
                observation.has_external_hardware
                and state.candidate.scope is ProfileScope.INTERNAL_ONLY
            ),
            "internal-only candidate is forbidden by external hardware evidence",
        )
    unresolved_external = (
        observation.valid
        and observation.has_external_hardware
        and observation.exact_profile is None
        and state.phase
        in {
            ControllerPhase.DISCOVER_FAST,
            ControllerPhase.WAIT_SLOW,
            ControllerPhase.PROBE_FAILED,
            ControllerPhase.APPLY_FAILED,
        }
    )
    _require(
        not unresolved_external or state.next_timer_ms is not None,
        "unresolved external topology requires a future observation timer",
    )


def _check_action_authorization(state: State) -> None:
    observation = state.latest_observation

    if state.application is not None:
        application = state.application
        _require(
            application.admitted_event_generation <= state.event_generation,
            "application admission comes from a future event generation",
        )
        if observation is not None:
            _require(
                not (
                    observation.has_external_hardware
                    and application.scope is ProfileScope.INTERNAL_ONLY
                ),
                "internal-only application is forbidden by external hardware evidence",
            )
        if (
            state.phase is ControllerPhase.APPLY_PENDING
            and application.admitted_event_generation == state.event_generation
        ):
            _require(
                observation is not None
                and observation.valid
                and observation.observation_key == application.key.observation_key
                and observation.end_event_generation
                == application.admitted_event_generation,
                "fresh application admission requires matching valid evidence",
            )
            _require(
                application.mapping.physical_epoch == state.physical_epoch
                and application.key.physical_epoch == state.physical_epoch,
                "application admission belongs to another physical epoch",
            )

    if state.probe is not None:
        probe = state.probe
        _require(
            probe.admitted_event_generation <= state.event_generation,
            "probe admission comes from a future event generation",
        )
        if (
            state.phase is ControllerPhase.PROBE_PENDING
            and probe.admitted_event_generation == state.event_generation
        ):
            candidate = None if observation is None else observation.probe_candidate
            _require(
                observation is not None
                and observation.valid
                and observation.observation_key == probe.key.observation_key
                and observation.end_event_generation == probe.admitted_event_generation,
                "fresh probe admission requires matching valid evidence",
            )
            _require(
                probe.key.physical_epoch == state.physical_epoch,
                "probe admission belongs to another physical epoch",
            )
            _require(
                candidate is not None
                and candidate.profile == probe.key.profile
                and candidate.output == probe.output
                and candidate.internal_output == probe.internal_output
                and candidate.preferred_mode == probe.preferred_mode,
                "probe admission must match the exact canonical candidate",
            )

    if state.preparation is not None:
        preparation = state.preparation
        _require(
            preparation.admitted_event_generation <= state.event_generation,
            "preparation admission comes from a future event generation",
        )
        if (
            state.preparation_state is PreparationState.PREPARE_PENDING
            and preparation.admitted_event_generation == state.event_generation
        ):
            _require(
                observation is not None
                and observation.valid
                and observation.observation_key == preparation.observation_key
                and observation.end_event_generation
                == preparation.admitted_event_generation
                and observation.exact_profile == preparation.profile
                and preparation.profile in observation.current_profiles,
                "fresh preparation admission requires exact current evidence",
            )
            _require(
                state.verify_since_ms is not None
                and observation is not None
                and observation.observed_at_ms - state.verify_since_ms
                >= _PREPARATION_STABILITY_MS,
                "preparation requires two seconds of continuous proof",
            )

    if state.finalization is not None:
        finalization = state.finalization
        preparation = state.preparation
        _require(
            preparation is not None
            and state.preparation_state is PreparationState.PREPARED
            and preparation.lifecycle is ActionLifecycle.COMPLETED,
            "finalization requires matching prepared desktop artifacts",
        )
        _require(
            preparation is not None
            and preparation.transition_id == finalization.transition_id
            and preparation.transition_key == finalization.transition_key
            and preparation.plan_hash == finalization.plan_hash
            and preparation.profile == finalization.profile,
            "finalization and preparation identities must match",
        )
        _require(
            finalization.admitted_event_generation <= state.event_generation,
            "finalization admission comes from a future event generation",
        )
        if (
            state.phase is ControllerPhase.FINALIZE_PENDING
            and finalization.admitted_event_generation == state.event_generation
        ):
            _require(
                observation is not None
                and observation.valid
                and observation.observation_key == finalization.observation_key
                and observation.end_event_generation
                == finalization.admitted_event_generation
                and observation.exact_profile == finalization.profile
                and finalization.profile in observation.current_profiles,
                "fresh finalization admission requires exact current evidence",
            )
            _require(
                state.verify_since_ms is not None
                and observation is not None
                and observation.observed_at_ms - state.verify_since_ms
                >= _PROFILE_STABILITY_MS,
                "finalization requires ten seconds of continuous proof",
            )
            _require(
                not state.baseline_adoption,
                "startup baseline adoption cannot authorize finalization",
            )


def _check_numbered_safety_rules(state: State) -> None:  # noqa: C901
    """Enforce the cross-cutting parts of all thirteen numbered v2 rules."""
    observation = state.latest_observation

    # 1 and 5: external evidence/intent can never authorize internal fallback.
    if observation is not None and observation.has_external_hardware:
        if state.candidate is not None:
            _require(
                state.candidate.scope is not ProfileScope.INTERNAL_ONLY,
                "external hardware forbids an internal-only candidate",
            )
        if state.application is not None:
            _require(
                state.application.scope is not ProfileScope.INTERNAL_ONLY,
                "external hardware forbids an internal-only application",
            )

    # 2: a probe remains a hardware-only action and cannot coexist with a
    # selected candidate or any desktop lifecycle.
    if state.probe is not None and state.phase is not ControllerPhase.RECOVERING:
        _require(state.candidate is None, "probe cannot select a profile candidate")
        _require(state.planning is None, "probe cannot authorize desktop planning")
        _require(
            state.preparation is None and state.finalization is None,
            "probe cannot authorize desktop mutation",
        )

    # 3 and 4: every application is a selected, mapped, uniquely keyed load.
    if state.application is not None:
        _require(
            state.application.profile == state.application.mapping.profile
            and state.application.key.profile == state.application.profile,
            "application must explicitly load its selected mapped profile",
        )
    _require(
        len(state.attempted_probe_keys) == len(set(state.attempted_probe_keys)),
        "probe attempt keys must be unique",
    )
    _require(
        len(state.attempted_application_keys)
        == len(set(state.attempted_application_keys)),
        "application attempt keys must be unique",
    )

    # 6: verification is bound continuously to the latest exact/current proof.
    if state.phase is ControllerPhase.VERIFYING and state.verify_since_ms is not None:
        _require(
            observation is not None
            and observation.valid
            and state.candidate is not None
            and observation.exact_profile == state.candidate.profile
            and state.candidate.profile in observation.current_profiles
            and observation.observation_key == state.candidate.observation_key,
            "verification requires continuous exact/current matching evidence",
        )

    # 7: durable dispatch acknowledgement retains keyed worker identity. The
    # detailed lifecycle/unit relationships are checked separately below.
    actions = (
        state.probe,
        state.application,
        state.preparation,
        state.finalization,
    )
    for action in actions:
        if action is not None and action.lifecycle in _MUTATING_IN_FLIGHT:
            _require(action.unit is not None, "mutating worker must retain its unit")

    # 8 and 9: desktop completion is independent, and same-profile transitions
    # never reach finalization.
    if state.finalization is not None:
        _require(
            state.finalization.profile != state.desktop_finalized_profile,
            "already-finalized profile cannot be finalized again",
        )

    # 10: while explicit unplug proof is incomplete, external intent remains.
    if state.unplug_proof is not None:
        _require(
            state.external_intent,
            "unplug proof requires retained external intent",
        )
        _require(
            state.unplug_proof.observation_count >= 1,
            "unplug proof requires at least one observation",
        )

    # 11 is checked in _check_observation_authority. 12 is checked in
    # _check_planning_relationships/_check_action_authorization. 13 is checked
    # in _check_worker_acknowledgements, including stopping/result-pending.


def _check_attempt_history(state: State) -> None:
    if (
        state.probe is not None
        and state.probe.lifecycle is not ActionLifecycle.ADMITTED
        and state.probe.unit is not None
    ):
        _require(
            state.probe.key in state.attempted_probe_keys,
            "acknowledged probe must retain its attempted key",
        )
    if (
        state.application is not None
        and state.application.lifecycle is not ActionLifecycle.ADMITTED
        and state.application.unit is not None
    ):
        _require(
            state.application.key in state.attempted_application_keys,
            "acknowledged application must retain its attempted key",
        )


def assert_controller_invariants(state: State) -> None:
    """Reject state which could violate central safety or recovery rules."""
    checks = (
        _check_phase_relationships,
        _check_planning_relationships,
        _check_action_identities,
        _check_worker_acknowledgements,
        _check_observation_authority,
        _check_action_authorization,
        _check_numbered_safety_rules,
        _check_attempt_history,
    )
    for check in checks:
        check(state)
