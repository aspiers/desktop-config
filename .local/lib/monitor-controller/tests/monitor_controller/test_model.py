"""Tests for immutable domain contracts and the fail-closed scaffold."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from monitor_controller.cli import main
from monitor_controller.invariants import (
    ControllerInvariantError,
    assert_controller_invariants,
)
from monitor_controller.model import (
    EFFECT_TYPES,
    EVENT_TYPES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActionTombstone,
    ActivateProbe,
    AdmissionDirtied,
    ApplicationAction,
    ApplicationAttemptKey,
    ApplicationDispatched,
    ApplicationFinished,
    ApplyProfile,
    BaseIdentityMatch,
    BootChanged,
    BootId,
    CandidateSelection,
    CanonicalObservation,
    ConfigurationContentHash,
    ConnectorIdentityEvidence,
    ControllerInstanceId,
    ControllerPhase,
    ControllerStarted,
    Decision,
    DiscardPlan,
    DispatchRejected,
    DisplayIdentity,
    DrmHintReceived,
    EdidEvidence,
    EdidIntegrity,
    EventEnvelope,
    EventGeneration,
    EventMetadata,
    FinalizationAction,
    FinalizationDispatched,
    FinalizationFinished,
    FinalizeDesktop,
    Fingerprint,
    MappingProof,
    ObservationCompleted,
    ObservationFailed,
    ObservationGeneration,
    ObservationInvalidityReason,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanCompleted,
    PlanFailed,
    PlanHash,
    PlanningAction,
    PlanningInputKey,
    PlanningState,
    PlanRequested,
    PreparationDispatched,
    PreparationFinished,
    PreparationState,
    PrepareDesktop,
    ProbeAction,
    ProbeAttemptKey,
    ProbeCandidate,
    ProbeDispatched,
    ProbeFinished,
    ProfileMatch,
    ProfileScope,
    RawEvidenceReference,
    RawEvidenceSource,
    RequestObservation,
    RequestPlan,
    Schedule,
    State,
    StopAction,
    TimerFired,
    TransitionId,
    TransitionKey,
    WakeReason,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerUnit,
)
from monitor_controller.reducer import UnknownEventError, reduce

_BOOT = BootId(UUID("11111111-1111-1111-1111-111111111111"))
_INSTANCE = ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222"))
_OLD_INSTANCE = ControllerInstanceId(UUID("33333333-3333-3333-3333-333333333333"))
_DISPLAY = DisplayIdentity(":0")
_KEY = ObservationKey("observation-1")
_META = EventMetadata(processed_at_ms=110, boot_id=_BOOT)
_CONFIG_HASHES = (ConfigurationContentHash("layouts/external.yaml", "sha256:layout"),)


def _planning_key(
    profile: str = "external", observation_key: ObservationKey = _KEY
) -> PlanningInputKey:
    return PlanningInputKey(
        physical_epoch=1,
        profile=profile,
        layout=f"layouts/{profile}.yaml",
        observation_key=observation_key,
        mapping=(OutputMapping("DP-1", "DP-3"),),
        active_outputs=("DP-3",),
        configuration_hashes=_CONFIG_HASHES,
    )


def _mapping(
    profile: str = "external",
    *,
    observation_key: ObservationKey = _KEY,
) -> MappingProof:
    return MappingProof(
        profile=profile,
        physical_epoch=1,
        observation_key=observation_key,
        outputs=(OutputMapping("DP-1", "DP-3"),),
    )


def _observation(  # noqa: PLR0913
    *,
    external: bool = True,
    exact_profile: str | None = "external",
    validity: ObservationValidity = ObservationValidity.VALID,
    observation_key: ObservationKey = _KEY,
    observation_generation: int = 2,
    begin_event_generation: int = 2,
    end_event_generation: int = 2,
) -> CanonicalObservation:
    kernel_connected = ("DP-3", "eDP-1") if external else ("eDP-1",)
    kernel_external = ("DP-3",) if external else ()
    eligible = (
        (
            ProfileMatch(
                profile=exact_profile,
                scope=ProfileScope.MIXED,
                layout="layouts/external.yaml",
                mapping=(
                    OutputMapping("DP-1", "DP-3"),
                    OutputMapping("eDP-1", "eDP-1"),
                ),
                active_outputs=kernel_connected,
                configuration_hashes=_CONFIG_HASHES,
            ),
        )
        if exact_profile is not None
        else ()
    )
    return CanonicalObservation(
        observed_at_ms=100,
        observation_generation=ObservationGeneration(observation_generation),
        boot_id=_BOOT,
        physical_token=PhysicalToken("dock-1"),
        begin_event_generation=EventGeneration(begin_event_generation),
        end_event_generation=EventGeneration(end_event_generation),
        kernel_connected_outputs=kernel_connected,
        kernel_external_outputs=kernel_external,
        x_connected_outputs=kernel_connected,
        x_active_outputs=kernel_connected,
        x_external_outputs=kernel_external,
        connector_identities=(ConnectorIdentityEvidence("DP-3", "card0-DP-3", 73, 73),)
        if external
        else (),
        live_fingerprints=(Fingerprint("eDP-1", "internal-edid"),),
        base_identity_profiles=(BaseIdentityMatch("external", "DP-3"),)
        if external
        else (),
        edid_integrity=(EdidEvidence("DP-3", EdidIntegrity.COMPLETE, "base-hash"),)
        if external
        else (),
        probe_candidate=None,
        eligible_profiles=eligible,
        current_profiles=(exact_profile,) if exact_profile is not None else (),
        exact_profile=exact_profile,
        observation_key=observation_key,
        validity=validity,
        invalidity_reason=None
        if validity is ObservationValidity.VALID
        else ObservationInvalidityReason.INCONSISTENT_EVIDENCE,
        raw_evidence=(
            RawEvidenceReference(
                RawEvidenceSource.DRM_CONNECTORS,
                "capture/drm-connectors.json",
                "sha256:drm",
            ),
        ),
    )


def _state(**changes: object) -> State:
    state = State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=_DISPLAY,
    )
    return replace(state, **changes)


def _all_events() -> tuple[EventEnvelope, ...]:
    plan_id = ActionId(_INSTANCE, ActionKind.PLAN, 1)
    probe_id = ActionId(_INSTANCE, ActionKind.PROBE, 2)
    apply_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 3)
    prepare_id = ActionId(_INSTANCE, ActionKind.PREPARATION, 4)
    finalize_id = ActionId(_INSTANCE, ActionKind.FINALIZATION, 5)
    input_key = _planning_key()
    plan_hash = PlanHash("plan")
    return (
        ObservationCompleted(_META, _observation()),
        ObservationFailed(_META, "observer timed out"),
        PlanRequested(_META, plan_id, input_key),
        PlanCompleted(_META, plan_id, input_key, plan_hash),
        PlanFailed(_META, plan_id, input_key, "failed"),
        TimerFired(_META, 100),
        DrmHintReceived(_META, EventGeneration(3)),
        AdmissionDirtied(_META, probe_id, EventGeneration(3)),
        ProbeDispatched(_META, probe_id, WorkerUnit(probe_id, "probe.service")),
        ProbeFinished(_META, probe_id, WorkerOutcome.SUCCEEDED, 0),
        ApplicationDispatched(_META, apply_id, WorkerUnit(apply_id, "apply.service")),
        ApplicationFinished(_META, apply_id, WorkerOutcome.SUCCEEDED, 0),
        PreparationDispatched(
            _META, prepare_id, WorkerUnit(prepare_id, "prepare.service")
        ),
        PreparationFinished(_META, prepare_id, WorkerOutcome.SUCCEEDED, 0, plan_hash),
        FinalizationDispatched(
            _META, finalize_id, WorkerUnit(finalize_id, "finalize.service")
        ),
        FinalizationFinished(_META, finalize_id, WorkerOutcome.SUCCEEDED, 0),
        DispatchRejected(_META, probe_id, "rejected"),
        WorkerStatusUnknown(_META, probe_id, "unknown"),
        WorkerTimedOut(_META, probe_id, 100),
        WorkerCancellationAcknowledged(
            _META,
            probe_id,
            ActionLifecycle.CANCELLED,
            143,
        ),
        ControllerStarted(_META, _INSTANCE),
        BootChanged(
            EventMetadata(
                processed_at_ms=120,
                boot_id=BootId(UUID("44444444-4444-4444-4444-444444444444")),
            ),
            _BOOT,
        ),
    )


def test_domain_values_are_frozen_and_compare_by_value() -> None:
    first = ObservationKey("same")
    second = ObservationKey("same")

    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(FrozenInstanceError):
        first.value = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("domain_type", EVENT_TYPES + EFFECT_TYPES)
def test_closed_union_members_are_frozen_dataclasses(domain_type: type[object]) -> None:
    assert is_dataclass(domain_type)
    params = getattr(domain_type, "__dataclass_params__")  # noqa: B009
    assert params.frozen is True


def test_closed_unions_have_exact_membership() -> None:
    assert {event_type.__name__ for event_type in EVENT_TYPES} == {
        "AdmissionDirtied",
        "ApplicationDispatched",
        "ApplicationFinished",
        "BootChanged",
        "ControllerStarted",
        "DispatchRejected",
        "DrmHintReceived",
        "FinalizationDispatched",
        "FinalizationFinished",
        "ObservationCompleted",
        "ObservationFailed",
        "PlanCompleted",
        "PlanFailed",
        "PlanRequested",
        "PreparationDispatched",
        "PreparationFinished",
        "ProbeDispatched",
        "ProbeFinished",
        "TimerFired",
        "WorkerCancellationAcknowledged",
        "WorkerStatusUnknown",
        "WorkerTimedOut",
    }
    assert set(EFFECT_TYPES) == {
        ActivateProbe,
        ApplyProfile,
        DiscardPlan,
        FinalizeDesktop,
        PrepareDesktop,
        RequestObservation,
        RequestPlan,
        Schedule,
        StopAction,
    }


def test_every_event_uses_the_shared_frozen_metadata_envelope() -> None:
    assert is_dataclass(EventMetadata)
    assert getattr(EventMetadata, "__dataclass_params__").frozen is True  # noqa: B009
    events = _all_events()

    assert {type(event) for event in events} == set(EVENT_TYPES)
    for event_type, event in zip(EVENT_TYPES, events, strict=True):
        assert issubclass(event_type, EventEnvelope)
        assert fields(event_type)[0].name == "metadata"
        assert isinstance(event.metadata, EventMetadata)
        assert isinstance(event.metadata.boot_id, BootId)
        assert event.metadata.processed_at_ms >= 0


def test_generation_types_are_distinct_and_observation_keeps_sample_time() -> None:
    observation = _observation()

    assert ObservationGeneration.__name__ != EventGeneration.__name__
    assert isinstance(observation.observation_generation, ObservationGeneration)
    assert isinstance(observation.event_generation, EventGeneration)
    assert observation.observed_at_ms == 100
    assert _META.processed_at_ms == 110


def test_all_specified_lifecycle_values_exist() -> None:
    assert {phase.name for phase in ControllerPhase} == {
        "RECOVERING",
        "QUIESCENT",
        "DISCOVER_FAST",
        "PROBE_PENDING",
        "PROBING",
        "PROBE_FAILED",
        "APPLY_PENDING",
        "APPLYING",
        "APPLY_FAILED",
        "VERIFYING",
        "WAIT_SLOW",
        "UNSUPPORTED",
        "FINALIZE_PENDING",
        "FINALIZING",
        "FINALIZE_STOPPING",
        "FINALIZE_FAILED",
    }
    assert {state.name for state in PlanningState} == {
        "PLAN_IDLE",
        "PLAN_PENDING",
        "PLANNING",
        "PLAN_READY",
        "PLAN_FAILED",
    }
    assert {state.name for state in PreparationState} == {
        "PREPARE_IDLE",
        "PREPARE_PENDING",
        "PREPARING",
        "PREPARED",
        "PREPARE_STOPPING",
        "PREPARE_FAILED",
    }


def test_action_identity_includes_kind_instance_and_sequence() -> None:
    action_id = ActionId(_INSTANCE, ActionKind.PROBE, 7)

    assert action_id.value == f"probe-{_INSTANCE.value.hex}-7"
    with pytest.raises(ValueError, match="positive"):
        ActionId(_INSTANCE, ActionKind.PROBE, 0)


def test_canonical_observation_rejects_unsafe_or_unsorted_evidence() -> None:
    with pytest.raises(ValueError, match="inactive"):
        replace(
            _observation(exact_profile=None),
            probe_candidate=ProbeCandidate("external", "DP-3", "eDP-1", "4k"),
        )

    with pytest.raises(ValueError, match="sorted"):
        replace(
            _observation(),
            kernel_connected_outputs=("eDP-1", "DP-3"),
        )

    with pytest.raises(ValueError, match="event-generation boundaries"):
        _observation(begin_event_generation=1, end_event_generation=2)

    with pytest.raises(ValueError, match="unique live fingerprint outputs"):
        replace(
            _observation(),
            live_fingerprints=(
                Fingerprint("eDP-1", "contradiction-a"),
                Fingerprint("eDP-1", "contradiction-b"),
            ),
        )


def test_invalid_observation_preserves_contradictory_raw_facts() -> None:
    observation = replace(
        _observation(
            exact_profile=None,
            validity=ObservationValidity.INVALID,
            begin_event_generation=2,
            end_event_generation=3,
        ),
        kernel_connected_outputs=("eDP-1",),
        kernel_external_outputs=("DP-3",),
        x_connected_outputs=("eDP-1",),
        x_active_outputs=("DP-3", "eDP-1"),
        connector_identities=(
            ConnectorIdentityEvidence("DP-3", "card0-DP-3", 73, 72),
            ConnectorIdentityEvidence("DP-3", "card0-DP-3", 73, 73),
        ),
    )

    assert not observation.valid
    assert observation.kernel_external_outputs == ("DP-3",)
    assert observation.x_active_outputs == ("DP-3", "eDP-1")
    assert len(observation.connector_identities) == 2
    assert (
        observation.invalidity_reason
        is ObservationInvalidityReason.INCONSISTENT_EVIDENCE
    )


def test_x_only_external_evidence_blocks_internal_fallback() -> None:
    observation = replace(
        _observation(external=False, exact_profile=None),
        x_connected_outputs=("DP-3", "eDP-1"),
        x_active_outputs=("DP-3", "eDP-1"),
        x_external_outputs=("DP-3",),
    )

    assert observation.has_external_hardware


def test_exact_profile_requires_complete_topology_identity_proof() -> None:
    observation = _observation()
    match = observation.eligible_profiles[0]

    with pytest.raises(ValueError, match="connected/active mapping bijection"):
        replace(
            observation,
            eligible_profiles=(
                replace(
                    match,
                    mapping=match.mapping[:1],
                    active_outputs=("DP-3",),
                ),
            ),
        )

    with pytest.raises(ValueError, match="complete EDID, base identity"):
        replace(
            observation,
            edid_integrity=(
                EdidEvidence(
                    "DP-3",
                    EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE,
                    "base-hash",
                ),
            ),
        )

    with pytest.raises(ValueError, match="complete EDID, base identity"):
        replace(observation, base_identity_profiles=())


def test_probe_candidate_rejects_extra_external_or_active_outputs() -> None:
    probe_observation = replace(
        _observation(exact_profile=None),
        x_active_outputs=("eDP-1",),
        edid_integrity=(
            EdidEvidence(
                "DP-3",
                EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
                "base-hash",
            ),
        ),
        probe_candidate=ProbeCandidate("external", "DP-3", "eDP-1", "4k"),
    )
    assert isinstance(probe_observation.probe_candidate, ProbeCandidate)

    with pytest.raises(ValueError, match="exact external/inactive"):
        replace(
            probe_observation,
            kernel_connected_outputs=("DP-3", "DP-4", "eDP-1"),
            kernel_external_outputs=("DP-3", "DP-4"),
            x_connected_outputs=("DP-3", "DP-4", "eDP-1"),
            x_external_outputs=("DP-3", "DP-4"),
            connector_identities=(
                *probe_observation.connector_identities,
                ConnectorIdentityEvidence("DP-4", "card0-DP-4", 74, 74),
            ),
            edid_integrity=(
                *probe_observation.edid_integrity,
                EdidEvidence("DP-4", EdidIntegrity.ABSENT),
            ),
        )


def test_planning_input_key_covers_topology_mapping_and_configuration() -> None:
    first = _planning_key()
    changed_layout = replace(first, layout="layouts/renamed.yaml")
    changed_mapping = replace(
        first,
        mapping=(OutputMapping("DP-1", "DP-4"),),
        active_outputs=("DP-4",),
    )
    changed_active = replace(first, active_outputs=())
    changed_hash = replace(
        first,
        configuration_hashes=(
            ConfigurationContentHash("layouts/external.yaml", "sha256:changed"),
        ),
    )

    identities = {
        first.value,
        changed_layout.value,
        changed_mapping.value,
        changed_active.value,
        changed_hash.value,
    }
    assert len(identities) == 5
    with pytest.raises(ValueError, match="configuration content hashes"):
        replace(first, configuration_hashes=())


def test_output_mapping_requires_a_nonempty_bijection() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        MappingProof(
            profile="external",
            physical_epoch=1,
            observation_key=_KEY,
            outputs=(),
        )

    with pytest.raises(ValueError, match="bijection"):
        MappingProof(
            profile="external",
            physical_epoch=1,
            observation_key=_KEY,
            outputs=(
                OutputMapping("DP-1", "DP-3"),
                OutputMapping("DP-2", "DP-3"),
            ),
        )


def test_state_has_explicit_display_identity_and_latest_observation() -> None:
    observation = _observation()
    state = _state(
        latest_observation=observation,
        observation_generation=observation.observation_generation,
        event_generation=observation.end_event_generation,
    )

    assert state.display_identity == DisplayIdentity(":0")
    assert state.latest_observation == observation
    assert not hasattr(state, "display")
    assert Decision(state) == Decision(state, ())
    with pytest.raises(FrozenInstanceError):
        state.phase = ControllerPhase.QUIESCENT  # type: ignore[misc]


def test_invalid_observation_retains_prior_candidate_and_mapping_intent() -> None:
    old_key = ObservationKey("prior-valid-proof")
    observation = _observation(
        exact_profile=None,
        validity=ObservationValidity.INVALID,
        observation_key=ObservationKey("torn-current-sample"),
        begin_event_generation=2,
        end_event_generation=3,
    )
    candidate = CandidateSelection(
        profile="external",
        scope=ProfileScope.MIXED,
        mapping=_mapping(observation_key=old_key),
        observation_key=old_key,
    )
    state = _state(
        latest_observation=observation,
        phase=ControllerPhase.DISCOVER_FAST,
        physical_epoch=1,
        candidate=candidate,
        next_timer_ms=1000,
        observation_generation=observation.observation_generation,
        event_generation=EventGeneration(3),
    )

    assert_controller_invariants(state)
    retained_candidate = state.candidate
    assert retained_candidate == candidate
    assert isinstance(retained_candidate, CandidateSelection)
    assert retained_candidate.observation_key.value != observation.observation_key.value


def test_invariants_reject_internal_fallback_with_external_hardware() -> None:
    observation = _observation()
    internal_mapping = MappingProof(
        profile="laptop",
        physical_epoch=1,
        observation_key=_KEY,
        outputs=(OutputMapping("eDP-1", "eDP-1"),),
    )
    state = _state(
        latest_observation=observation,
        phase=ControllerPhase.VERIFYING,
        physical_epoch=1,
        candidate=CandidateSelection(
            profile="laptop",
            scope=ProfileScope.INTERNAL_ONLY,
            mapping=internal_mapping,
            observation_key=_KEY,
        ),
        observation_generation=observation.observation_generation,
        event_generation=observation.end_event_generation,
    )

    with pytest.raises(ControllerInvariantError, match="internal-only"):
        assert_controller_invariants(state)


def test_invariants_reject_internal_application_with_external_evidence() -> None:
    observation = _observation(
        exact_profile=None,
        validity=ObservationValidity.INVALID,
        begin_event_generation=2,
        end_event_generation=3,
    )
    application_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 1)
    key = ApplicationAttemptKey(1, "laptop", ObservationKey("prior-proof"))
    mapping = MappingProof(
        profile="laptop",
        physical_epoch=1,
        observation_key=key.observation_key,
        outputs=(OutputMapping("eDP-1", "eDP-1"),),
    )
    state = _state(
        latest_observation=observation,
        phase=ControllerPhase.APPLY_PENDING,
        physical_epoch=1,
        application=ApplicationAction(
            action_id=application_id,
            key=key,
            admitted_event_generation=EventGeneration(2),
            profile="laptop",
            scope=ProfileScope.INTERNAL_ONLY,
            mapping=mapping,
        ),
        observation_generation=observation.observation_generation,
        event_generation=EventGeneration(3),
        action_sequence_high_water=1,
    )

    with pytest.raises(ControllerInvariantError, match="internal-only application"):
        assert_controller_invariants(state)


def test_finalization_pending_requires_matching_prepared_artifacts() -> None:
    observation = _observation()
    finalization_id = ActionId(_INSTANCE, ActionKind.FINALIZATION, 1)
    state = _state(
        latest_observation=observation,
        phase=ControllerPhase.FINALIZE_PENDING,
        physical_epoch=1,
        finalization=FinalizationAction(
            action_id=finalization_id,
            transition_id=TransitionId(_INSTANCE, 1),
            transition_key=TransitionKey("external-transition"),
            plan_hash=PlanHash("external-plan"),
            admitted_event_generation=observation.end_event_generation,
            observation_key=observation.observation_key,
            profile="external",
        ),
        verify_since_ms=0,
        observation_generation=observation.observation_generation,
        event_generation=observation.end_event_generation,
        action_sequence_high_water=1,
        transition_sequence_high_water=1,
    )

    with pytest.raises(ControllerInvariantError, match="prepared desktop"):
        assert_controller_invariants(state)


def test_recovery_retains_prior_instance_ids_transitions_and_worker_units() -> None:
    old_probe_id = ActionId(_OLD_INSTANCE, ActionKind.PROBE, 900)
    old_plan_id = ActionId(_OLD_INSTANCE, ActionKind.PLAN, 901)
    old_transition_id = TransitionId(_OLD_INSTANCE, 700)
    old_tombstone_id = ActionId(_OLD_INSTANCE, ActionKind.PLAN, 902)
    old_unit = WorkerUnit(old_probe_id, "monitor-probe@old.service")
    old_key = ProbeAttemptKey(1, "external", _KEY)
    state = _state(
        physical_epoch=1,
        probe=ProbeAction(
            action_id=old_probe_id,
            key=old_key,
            admitted_event_generation=EventGeneration(2),
            output="DP-3",
            internal_output="eDP-1",
            preferred_mode="3840x2160",
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=old_unit,
            worker_deadline_ms=1_000,
        ),
        attempted_probe_keys=frozenset({old_key}),
        planning=PlanningAction(
            action_id=old_plan_id,
            transition_id=old_transition_id,
            input_key=_planning_key(),
            profile="external",
        ),
        action_tombstones=(
            ActionTombstone(old_tombstone_id, ActionLifecycle.CANCELLED),
        ),
        recovery_units=(old_unit,),
        event_generation=EventGeneration(2),
        action_sequence_high_water=0,
        transition_sequence_high_water=0,
    )

    assert_controller_invariants(state)
    assert isinstance(state.probe, ProbeAction)
    assert state.probe.action_id == old_probe_id
    assert state.recovery_units == (old_unit,)


def test_high_water_marks_only_constrain_current_instance_allocator() -> None:
    current_id = ActionId(_INSTANCE, ActionKind.PROBE, 2)
    key = ProbeAttemptKey(1, "external", _KEY)
    state = _state(
        physical_epoch=1,
        action_sequence_high_water=1,
        probe=ProbeAction(
            action_id=current_id,
            key=key,
            admitted_event_generation=EventGeneration(2),
            output="DP-3",
            internal_output="eDP-1",
            preferred_mode="3840x2160",
        ),
    )

    with pytest.raises(ControllerInvariantError, match="current-instance action"):
        assert_controller_invariants(state)

    plan_id = ActionId(_INSTANCE, ActionKind.PLAN, 1)
    transition_state = _state(
        action_sequence_high_water=1,
        transition_sequence_high_water=1,
        planning=PlanningAction(
            action_id=plan_id,
            transition_id=TransitionId(_INSTANCE, 2),
            input_key=_planning_key(),
            profile="external",
        ),
    )
    with pytest.raises(ControllerInvariantError, match="current-instance transition"):
        assert_controller_invariants(transition_state)


def test_generation_fences_use_event_generation_not_observation_generation() -> None:
    observation = _observation(observation_generation=50, end_event_generation=2)
    valid_state = _state(
        latest_observation=observation,
        observation_generation=ObservationGeneration(50),
        event_generation=EventGeneration(2),
    )
    assert_controller_invariants(valid_state)

    stale_controller_generation = replace(
        valid_state,
        event_generation=EventGeneration(1),
    )
    with pytest.raises(ControllerInvariantError, match="event generation"):
        assert_controller_invariants(stale_controller_generation)


def test_invariants_reject_overlapping_display_workers() -> None:
    probe_id = ActionId(_INSTANCE, ActionKind.PROBE, 1)
    application_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 2)
    probe_key = ProbeAttemptKey(1, "external", _KEY)
    application_key = ApplicationAttemptKey(1, "external", _KEY)
    state = _state(
        action_sequence_high_water=2,
        event_generation=EventGeneration(2),
        probe=ProbeAction(
            action_id=probe_id,
            key=probe_key,
            admitted_event_generation=EventGeneration(2),
            output="DP-3",
            internal_output="eDP-1",
            preferred_mode="3840x2160",
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=WorkerUnit(probe_id, "monitor-probe@1.service"),
            worker_deadline_ms=1_000,
        ),
        attempted_probe_keys=frozenset({probe_key}),
        application=ApplicationAction(
            action_id=application_id,
            key=application_key,
            admitted_event_generation=EventGeneration(2),
            profile="external",
            scope=ProfileScope.MIXED,
            mapping=_mapping(),
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=WorkerUnit(application_id, "monitor-apply@2.service"),
            worker_deadline_ms=1_000,
        ),
        attempted_application_keys=frozenset({application_key}),
    )

    with pytest.raises(ControllerInvariantError, match="more than one"):
        assert_controller_invariants(state)


def test_placeholder_reducer_is_a_validated_no_op() -> None:
    state = _state()
    event = TimerFired(metadata=_META, deadline_ms=10)

    assert reduce(state, event) == Decision(state=state, effects=())
    with pytest.raises(UnknownEventError):
        reduce(state, cast("TimerFired", object()))


def test_event_and_effect_construction() -> None:
    observation = _observation()
    event = ObservationCompleted(metadata=_META, observation=observation)
    effect = RequestObservation(WakeReason.WORKER_COMPLETED)

    assert event.observation == observation
    assert event.metadata == _META
    assert effect == RequestObservation(WakeReason.WORKER_COMPLETED)
    assert Schedule(42).deadline_ms == 42


def test_boot_change_rejects_identical_boot_ids() -> None:
    with pytest.raises(ValueError, match="distinct"):
        BootChanged(_META, _BOOT)


def test_pure_core_imports_in_an_isolated_interpreter() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = f"""
import sys
sys.path.insert(0, {str(project_root)!r})
import monitor_controller.invariants
import monitor_controller.model
import monitor_controller.reducer
loaded = {{name for name in sys.modules if name.startswith('monitor_controller.')}}
expected = {{
    'monitor_controller.invariants',
    'monitor_controller.model',
    'monitor_controller.reducer',
}}
assert loaded == expected, loaded
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_status_is_read_only_when_authoritative_state_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["status", "--state-home", str(tmp_path)]) == 1
    output = capsys.readouterr().out

    assert '"status": "missing"' in output
    assert not (tmp_path / "monitor-controller").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (":0.0", ":0"),
        (":0", ":0"),
        (":1.0", ":1"),
        (":10.0", ":10"),
        ("host:0.0", "host:0"),
        # A non-default screen is a genuinely different target.
        (":0.1", ":0.1"),
        # Simulation sentinels are not X display addresses.
        (":scenario", ":scenario"),
    ],
)
def test_display_identity_canonicalises_default_screen_suffix(
    raw: str,
    expected: str,
) -> None:
    assert DisplayIdentity(raw).value == expected


def test_display_identity_compares_equal_across_screen_spellings() -> None:
    """A display persisted as ':0.0' must match a service observing ':0'.

    Regression for dc-h9y: the shadow controller refused to start for 29 hours
    because persisted state written under the session's ':0.0' was compared
    verbatim against the systemd user manager's ':0'.
    """
    assert DisplayIdentity(":0.0") == DisplayIdentity(":0")
    assert DisplayIdentity(":0.0") != DisplayIdentity(":1")
    assert DisplayIdentity(":0.0") != DisplayIdentity(":0.1")
