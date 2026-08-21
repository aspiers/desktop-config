# ruff: noqa: EM101, EM102, TRY003, PLR0912, TID252
"""Strict language-neutral JSON scenario loader and reducer runner."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID

from ..model import (
    ActionId,
    ActionKind,
    ActivateProbe,
    AdmissionDirtied,
    ApplicationDispatched,
    ApplicationFinished,
    ApplyProfile,
    BaseIdentityMatch,
    BootChanged,
    BootId,
    CanonicalObservation,
    ConfigurationContentHash,
    ConnectorIdentityEvidence,
    ControllerInstanceId,
    ControllerPhase,
    ControllerStarted,
    Decision,
    DispatchRejected,
    DisplayIdentity,
    DrmHintReceived,
    EdidEvidence,
    EdidIntegrity,
    Effect,
    Event,
    EventGeneration,
    EventMetadata,
    FinalizationDispatched,
    FinalizationFinished,
    FinalizeDesktop,
    Fingerprint,
    ObservationCompleted,
    ObservationGeneration,
    ObservationInvalidityReason,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanCompleted,
    PlanFailed,
    PlanHash,
    PlanRequested,
    PreparationDispatched,
    PreparationFinished,
    PrepareDesktop,
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
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerUnit,
)
from ..reducer import reduce

SCENARIO_SCHEMA_VERSION: int = 1
DEFAULT_BOOT_ID = BootId(UUID("11111111-1111-1111-1111-111111111111"))
DEFAULT_INSTANCE_ID = ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222"))

_TOP_LEVEL_FIELDS = frozenset({"schema_version", "scenarios"})
_SCENARIO_FIELDS = frozenset({"name", "description", "initial", "steps", "covers"})
_INITIAL_FIELDS = frozenset(
    {
        "desktop_finalized_profile",
        "phase",
        "physical_token",
        "stable_x_profile",
        "external_intent",
        "baseline_adoption",
        "next_timer_ms",
        "aggressive_deadline_ms",
        "backoff_index",
    }
)
_STEP_FIELDS = frozenset({"event", "expect"})
_EXPECT_FIELDS = frozenset({"state", "effects", "effect_counts"})
_EXPECTED_STATE_FIELDS = frozenset(
    {
        "phase",
        "planning_state",
        "preparation_state",
        "physical_epoch",
        "candidate_profile",
        "stable_x_profile",
        "desktop_finalized_profile",
        "external_intent",
        "baseline_adoption",
        "next_timer_ms",
        "verify_since_ms",
        "probe_action_id",
        "probe_lifecycle",
        "application_action_id",
        "application_lifecycle",
        "planning_action_id",
        "planning_lifecycle",
        "preparation_action_id",
        "preparation_lifecycle",
        "finalization_action_id",
        "finalization_lifecycle",
        "attempted_probe_count",
        "attempted_application_count",
        "action_sequence_high_water",
        "transition_sequence_high_water",
        "tombstone_count",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "type",
        "at_ms",
        "key",
        "physical_token",
        "external_state",
        "target_profile",
        "target_scope",
        "exact_profile",
        "current_profile",
        "valid",
        "observation_generation",
        "event_generation",
        "identity_profile",
        "probe_output",
        "probe_internal_output",
        "probe_mode",
        "target_layout",
        "configuration_hashes",
        "external_outputs",
        "complete_edid_outputs",
        "base_identity_outputs",
        "internal_edid_complete",
    }
)
_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "drm_hint": frozenset({"type", "at_ms", "event_generation"}),
    "timer": frozenset({"type", "at_ms", "deadline_ms"}),
    "plan_requested": frozenset({"type", "at_ms"}),
    "plan_completed": frozenset({"type", "at_ms", "plan_hash"}),
    "plan_failed": frozenset({"type", "at_ms", "reason", "exit_status"}),
    "dispatch": frozenset({"type", "at_ms", "kind", "unit"}),
    "finished": frozenset(
        {"type", "at_ms", "kind", "outcome", "exit_status", "plan_hash"}
    ),
    "admission_dirtied": frozenset({"type", "at_ms", "kind", "event_generation"}),
    "dispatch_rejected": frozenset({"type", "at_ms", "kind", "reason"}),
    "worker_status_unknown": frozenset({"type", "at_ms", "kind", "reason"}),
    "worker_timed_out": frozenset({"type", "at_ms", "kind", "deadline_ms"}),
    "cancellation_acknowledged": frozenset({"type", "at_ms", "kind"}),
    "controller_started": frozenset({"type", "at_ms", "instance_id"}),
    "boot_changed": frozenset({"type", "at_ms", "previous_boot_id", "new_boot_id"}),
}
_EFFECT_FIELDS: dict[str, frozenset[str]] = {
    "request_observation": frozenset({"type", "reason"}),
    "schedule": frozenset({"type", "deadline_ms"}),
    "request_plan": frozenset(
        {"type", "action_id", "transition_id", "input_key", "profile"}
    ),
    "activate_probe": frozenset(
        {
            "type",
            "action_id",
            "key",
            "output",
            "internal_output",
            "preferred_mode",
            "event_generation",
            "observation_key",
        }
    ),
    "apply_profile": frozenset(
        {
            "type",
            "action_id",
            "key",
            "profile",
            "event_generation",
            "observation_key",
        }
    ),
    "prepare_desktop": frozenset(
        {
            "type",
            "action_id",
            "transition_id",
            "transition_key",
            "profile",
            "plan_hash",
            "event_generation",
            "observation_key",
        }
    ),
    "finalize_desktop": frozenset(
        {
            "type",
            "action_id",
            "transition_id",
            "transition_key",
            "profile",
            "plan_hash",
            "event_generation",
            "observation_key",
        }
    ),
    "stop_action": frozenset({"type", "action_id"}),
    "discard_plan": frozenset({"type", "action_id", "plan_hash"}),
}


class ScenarioFormatError(ValueError):
    """Raised when scenario JSON is ambiguous, incomplete, or out of schema."""


class ScenarioAssertionError(AssertionError):
    """Raised when a reducer decision differs from an explicit expectation."""


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One typed reducer event and its normalized expected decision subset."""

    event_data: Mapping[str, object]
    expected_state: Mapping[str, object]
    expected_effects: tuple[Mapping[str, object], ...]
    expected_effect_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class Scenario:
    """One explicit initial state and ordered event sequence."""

    name: str
    description: str
    covers: tuple[str, ...]
    initial: Mapping[str, object]
    steps: tuple[ScenarioStep, ...]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """Complete decisions produced by one scenario replay."""

    scenario: Scenario
    decisions: tuple[Decision, ...]

    @property
    def final_state(self) -> State:
        """Return the final immutable reducer state."""
        return self.decisions[-1].state


def _reject_nonfinite_constant(value: str) -> NoReturn:
    raise ScenarioFormatError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioFormatError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ScenarioFormatError(f"{where} must be a JSON object")
    result = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in result):
        raise ScenarioFormatError(f"{where} must use string object keys")
    return cast("dict[str, object]", result)


def _array(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ScenarioFormatError(f"{where} must be a JSON array")
    return cast("list[object]", value)


def _exact_fields(
    value: Mapping[str, object], allowed: frozenset[str], where: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ScenarioFormatError(f"{where} has unknown fields: {sorted(unknown)}")


def _required_fields(
    value: Mapping[str, object], required: frozenset[str], where: str
) -> None:
    missing = required - set(value)
    if missing:
        raise ScenarioFormatError(f"{where} lacks required fields: {sorted(missing)}")


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScenarioFormatError(f"{where} must be a non-empty string")
    return value


def _nullable_string(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _string(value, where)


def _integer(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScenarioFormatError(f"{where} must be a non-negative integer")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioFormatError(f"{where} must be a boolean")
    return value


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    """Strictly decode all scenarios from *path*, rejecting duplicate fields."""
    try:
        data = json.loads(
            path.read_text(),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite_constant,
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioFormatError(f"cannot decode {path}: {error}") from error
    root = _object(data, "scenario document")
    _exact_fields(root, _TOP_LEVEL_FIELDS, "scenario document")
    _required_fields(root, _TOP_LEVEL_FIELDS, "scenario document")
    if (
        _integer(root["schema_version"], "scenario schema_version")
        != SCENARIO_SCHEMA_VERSION
    ):
        raise ScenarioFormatError(
            f"scenario schema_version must be {SCENARIO_SCHEMA_VERSION}"
        )
    scenarios = tuple(
        _decode_scenario(item, index)
        for index, item in enumerate(_array(root["scenarios"], "scenarios"))
    )
    names = tuple(item.name for item in scenarios)
    if not scenarios or len(set(names)) != len(names):
        raise ScenarioFormatError("scenario names must be non-empty and unique")
    return scenarios


def _decode_scenario(value: object, index: int) -> Scenario:
    where = f"scenarios[{index}]"
    data = _object(value, where)
    _exact_fields(data, _SCENARIO_FIELDS, where)
    _required_fields(data, _SCENARIO_FIELDS, where)
    initial = _object(data["initial"], f"{where}.initial")
    _exact_fields(initial, _INITIAL_FIELDS, f"{where}.initial")
    steps = tuple(
        _decode_step(item, f"{where}.steps[{step_index}]")
        for step_index, item in enumerate(_array(data["steps"], f"{where}.steps"))
    )
    if not steps:
        raise ScenarioFormatError(f"{where}.steps must not be empty")
    covers = tuple(
        _string(item, f"{where}.covers")
        for item in _array(data["covers"], f"{where}.covers")
    )
    if not covers or len(set(covers)) != len(covers):
        raise ScenarioFormatError(f"{where}.covers must be non-empty and unique")
    return Scenario(
        name=_string(data["name"], f"{where}.name"),
        description=_string(data["description"], f"{where}.description"),
        covers=covers,
        initial=initial,
        steps=steps,
    )


def _decode_step(value: object, where: str) -> ScenarioStep:
    data = _object(value, where)
    _exact_fields(data, _STEP_FIELDS, where)
    _required_fields(data, _STEP_FIELDS, where)
    event = _object(data["event"], f"{where}.event")
    event_type = _string(event.get("type"), f"{where}.event.type")
    allowed = (
        _OBSERVATION_FIELDS
        if event_type == "observation"
        else _EVENT_FIELDS.get(event_type)
    )
    if allowed is None:
        raise ScenarioFormatError(f"{where}.event has unknown type: {event_type}")
    _exact_fields(event, allowed, f"{where}.event")
    _required_fields(event, allowed, f"{where}.event")

    expected = _object(data["expect"], f"{where}.expect")
    _exact_fields(expected, _EXPECT_FIELDS, f"{where}.expect")
    _required_fields(expected, _EXPECT_FIELDS, f"{where}.expect")
    expected_state = _object(expected["state"], f"{where}.expect.state")
    _exact_fields(expected_state, _EXPECTED_STATE_FIELDS, f"{where}.expect.state")
    if not expected_state:
        raise ScenarioFormatError(f"{where}.expect.state must not be empty")
    effects = tuple(
        _decode_expected_effect(item, f"{where}.expect.effects[{effect_index}]")
        for effect_index, item in enumerate(
            _array(expected["effects"], f"{where}.expect.effects")
        )
    )
    counts_data = _object(expected["effect_counts"], f"{where}.effect_counts")
    counts: dict[str, int] = {}
    for key, item in counts_data.items():
        if key not in _EFFECT_FIELDS:
            raise ScenarioFormatError(f"{where}.effect_counts has unknown type: {key}")
        counts[key] = _integer(item, f"{where}.effect_counts.{key}")
    return ScenarioStep(event, expected_state, effects, counts)


def _decode_expected_effect(value: object, where: str) -> Mapping[str, object]:
    effect = _object(value, where)
    effect_type = _string(effect.get("type"), f"{where}.type")
    allowed = _EFFECT_FIELDS.get(effect_type)
    if allowed is None:
        raise ScenarioFormatError(f"{where} has unknown type: {effect_type}")
    _exact_fields(effect, allowed, where)
    _required_fields(effect, allowed, where)
    return effect


def initial_state(data: Mapping[str, object]) -> State:
    """Build the strictly bounded scenario initial state shorthand."""
    finalized = _nullable_string(
        data.get("desktop_finalized_profile"), "initial.desktop_finalized_profile"
    )
    phase_value = data.get("phase", ControllerPhase.RECOVERING.value)
    phase = ControllerPhase(_string(phase_value, "initial.phase"))
    physical = data.get("physical_token")
    stable = _nullable_string(data.get("stable_x_profile"), "initial.stable_x_profile")
    return State(
        boot_id=DEFAULT_BOOT_ID,
        controller_instance=DEFAULT_INSTANCE_ID,
        display_identity=DisplayIdentity(":scenario"),
        phase=phase,
        physical_token=None
        if physical is None
        else PhysicalToken(_string(physical, "initial.physical_token")),
        stable_x_profile=stable,
        desktop_finalized_profile=finalized,
        external_intent=_boolean(
            data.get("external_intent", False), "initial.external_intent"
        ),
        baseline_adoption=_boolean(
            data.get("baseline_adoption", finalized is None),
            "initial.baseline_adoption",
        ),
        next_timer_ms=None
        if data.get("next_timer_ms") is None
        else _integer(data["next_timer_ms"], "initial.next_timer_ms"),
        aggressive_deadline_ms=None
        if data.get("aggressive_deadline_ms") is None
        else _integer(data["aggressive_deadline_ms"], "initial.aggressive_deadline_ms"),
        backoff_index=_integer(data.get("backoff_index", 0), "initial.backoff_index"),
    )


def _metadata(data: Mapping[str, object], state: State) -> EventMetadata:
    return EventMetadata(_integer(data["at_ms"], "event.at_ms"), state.boot_id)


def _scope(value: object) -> ProfileScope:
    text = _string(value, "event.target_scope")
    aliases = {
        "internal": ProfileScope.INTERNAL_ONLY,
        "external": ProfileScope.MIXED,
        "mixed": ProfileScope.MIXED,
        "external_only": ProfileScope.EXTERNAL_ONLY,
    }
    try:
        return aliases[text]
    except KeyError as error:
        raise ScenarioFormatError(f"unknown target_scope: {text}") from error


def _observation(data: Mapping[str, object], state: State) -> CanonicalObservation:
    now_ms = _integer(data["at_ms"], "observation.at_ms")
    key = ObservationKey(_string(data["key"], "observation.key"))
    external_state = _string(data["external_state"], "observation.external_state")
    if external_state not in {"none", "unresolved", "probeable", "known", "unknown"}:
        raise ScenarioFormatError(f"unknown external_state: {external_state}")
    valid = _boolean(data["valid"], "observation.valid")
    target_profile = _nullable_string(
        data["target_profile"], "observation.target_profile"
    )
    exact_profile = _nullable_string(data["exact_profile"], "observation.exact_profile")
    current_profile = _nullable_string(
        data["current_profile"], "observation.current_profile"
    )
    scope = None if target_profile is None else _scope(data["target_scope"])
    external_outputs = tuple(
        _string(item, "observation.external_outputs")
        for item in _array(data["external_outputs"], "observation.external_outputs")
    )
    if external_outputs != tuple(sorted(set(external_outputs))):
        raise ScenarioFormatError("observation.external_outputs must be sorted/unique")
    external = bool(external_outputs)
    connected = tuple(sorted((*external_outputs, "eDP-1")))
    active_external = external and exact_profile is not None
    active = connected if active_external else ("eDP-1",)
    mapping = (
        (OutputMapping("eDP-1", "eDP-1"),)
        if scope is ProfileScope.INTERNAL_ONLY
        else tuple(
            sorted(
                (
                    *(
                        OutputMapping(f"DP-SAVED-{index}", output)
                        for index, output in enumerate(external_outputs, start=1)
                    ),
                    OutputMapping("eDP-1", "eDP-1"),
                ),
                key=lambda item: (item.saved_output, item.live_output),
            )
        )
    )
    target_layout = _nullable_string(data["target_layout"], "observation.target_layout")
    hashes_data = _object(
        data["configuration_hashes"], "observation.configuration_hashes"
    )
    configuration_hashes = tuple(
        ConfigurationContentHash(path, _string(digest, "configuration hash"))
        for path, digest in sorted(hashes_data.items())
    )
    eligible = (
        (
            ProfileMatch(
                target_profile,
                scope,
                target_layout,
                mapping,
                active,
                configuration_hashes,
            ),
        )
        if target_profile is not None
        and scope is not None
        and target_layout is not None
        and configuration_hashes
        and valid
        and external_state != "probeable"
        else ()
    )
    identity_profile = _nullable_string(
        data["identity_profile"], "observation.identity_profile"
    )
    probe_output = _nullable_string(data["probe_output"], "observation.probe_output")
    probe_internal = _nullable_string(
        data["probe_internal_output"], "observation.probe_internal_output"
    )
    probe_mode = _nullable_string(data["probe_mode"], "observation.probe_mode")
    probe = None
    if (
        valid
        and external_state == "probeable"
        and None not in {identity_profile, probe_output, probe_internal, probe_mode}
    ):
        probe = ProbeCandidate(
            cast("str", identity_profile),
            cast("str", probe_output),
            cast("str", probe_internal),
            cast("str", probe_mode),
        )
    complete_edid_outputs = {
        _string(item, "observation.complete_edid_outputs")
        for item in _array(
            data["complete_edid_outputs"], "observation.complete_edid_outputs"
        )
    }
    if not complete_edid_outputs <= set(external_outputs):
        raise ScenarioFormatError("complete EDID outputs must be external")
    integrity_items: list[EdidEvidence] = []
    for output in external_outputs:
        if output in complete_edid_outputs:
            edid = EdidIntegrity.COMPLETE
        elif external_state == "probeable":
            edid = EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID
        else:
            edid = EdidIntegrity.ABSENT
        integrity_items.append(
            EdidEvidence(
                output,
                edid,
                None if edid is EdidIntegrity.ABSENT else f"base-hash:{output}",
            )
        )
    if _boolean(data["internal_edid_complete"], "observation.internal_edid_complete"):
        integrity_items.append(
            EdidEvidence("eDP-1", EdidIntegrity.COMPLETE, "base-hash:eDP-1")
        )
    integrity = tuple(sorted(integrity_items, key=lambda item: item.output))
    base_identity_outputs = tuple(
        _string(item, "observation.base_identity_outputs")
        for item in _array(
            data["base_identity_outputs"], "observation.base_identity_outputs"
        )
    )
    base_profile = identity_profile or target_profile
    if not valid:
        exact_profile = None
        probe = None
        eligible = ()
    return CanonicalObservation(
        observed_at_ms=now_ms,
        observation_generation=ObservationGeneration(
            _integer(data["observation_generation"], "observation.generation")
        ),
        boot_id=state.boot_id,
        physical_token=PhysicalToken(
            _string(data["physical_token"], "observation.physical_token")
        ),
        begin_event_generation=EventGeneration(
            _integer(data["event_generation"], "observation.event_generation")
        ),
        end_event_generation=EventGeneration(
            _integer(data["event_generation"], "observation.event_generation")
        ),
        kernel_connected_outputs=connected,
        kernel_external_outputs=external_outputs,
        x_connected_outputs=connected,
        x_active_outputs=active,
        x_external_outputs=external_outputs,
        connector_identities=tuple(
            ConnectorIdentityEvidence(
                output,
                f"card0-{output}",
                index,
                index,
            )
            for index, output in enumerate(external_outputs, start=1)
        ),
        live_fingerprints=(Fingerprint("eDP-1", "internal"),),
        base_identity_profiles=tuple(
            BaseIdentityMatch(base_profile, output) for output in base_identity_outputs
        )
        if base_profile is not None and valid
        else (),
        edid_integrity=integrity,
        probe_candidate=probe,
        eligible_profiles=eligible,
        current_profiles=(current_profile,) if current_profile is not None else (),
        exact_profile=exact_profile,
        observation_key=key,
        validity=ObservationValidity.VALID if valid else ObservationValidity.INVALID,
        invalidity_reason=None
        if valid
        else ObservationInvalidityReason.INCONSISTENT_EVIDENCE,
        raw_evidence=(
            RawEvidenceReference(
                RawEvidenceSource.DRM_CONNECTORS,
                f"scenario:{key.value}",
                f"sha256:{key.value}",
            ),
        ),
    )


def _current_action(state: State, kind_value: object) -> ActionId:
    try:
        kind = ActionKind(_string(kind_value, "event.kind"))
    except ValueError as error:
        raise ScenarioFormatError(f"unknown action kind: {kind_value}") from error
    action = {
        ActionKind.PLAN: state.planning,
        ActionKind.PROBE: state.probe,
        ActionKind.APPLICATION: state.application,
        ActionKind.PREPARATION: state.preparation,
        ActionKind.FINALIZATION: state.finalization,
    }[kind]
    if action is None:
        raise ScenarioFormatError(f"event refers to absent {kind.value} action")
    return action.action_id


def _unit(action_id: ActionId, data: Mapping[str, object]) -> WorkerUnit:
    return WorkerUnit(action_id, _string(data["unit"], "event.unit"))


def event_from_data(data: Mapping[str, object], state: State) -> Event:  # noqa: C901, PLR0911
    """Construct one typed event, resolving named current action identities."""
    event_type = _string(data["type"], "event.type")
    if event_type == "observation":
        return ObservationCompleted(_metadata(data, state), _observation(data, state))
    if event_type == "boot_changed":
        return BootChanged(
            EventMetadata(
                _integer(data["at_ms"], "event.at_ms"),
                BootId(UUID(_string(data["new_boot_id"], "event.new_boot_id"))),
            ),
            BootId(UUID(_string(data["previous_boot_id"], "event.previous_boot_id"))),
        )
    metadata = _metadata(data, state)
    if event_type == "drm_hint":
        return DrmHintReceived(
            metadata,
            EventGeneration(_integer(data["event_generation"], "event.generation")),
        )
    if event_type == "timer":
        deadline = data["deadline_ms"]
        if deadline == "$next_timer":
            if state.next_timer_ms is None:
                raise ScenarioFormatError("$next_timer used without a timer")
            deadline_ms = state.next_timer_ms
        else:
            deadline_ms = _integer(deadline, "event.deadline_ms")
        return TimerFired(metadata, deadline_ms)
    if event_type == "plan_requested":
        if state.planning is None:
            raise ScenarioFormatError("plan_requested requires a current plan")
        return PlanRequested(
            metadata, state.planning.action_id, state.planning.input_key
        )
    if event_type == "plan_completed":
        if state.planning is None:
            raise ScenarioFormatError("plan_completed requires a current plan")
        return PlanCompleted(
            metadata,
            state.planning.action_id,
            state.planning.input_key,
            PlanHash(_string(data["plan_hash"], "event.plan_hash")),
        )
    if event_type == "plan_failed":
        if state.planning is None:
            raise ScenarioFormatError("plan_failed requires a current plan")
        return PlanFailed(
            metadata,
            state.planning.action_id,
            state.planning.input_key,
            _string(data["reason"], "event.reason"),
            None
            if data["exit_status"] is None
            else _integer(data["exit_status"], "event.exit_status"),
        )
    if event_type == "controller_started":
        return ControllerStarted(
            metadata,
            ControllerInstanceId(
                UUID(_string(data["instance_id"], "event.instance_id"))
            ),
        )
    action_id = _current_action(state, data.get("kind"))
    if event_type == "dispatch":
        unit = _unit(action_id, data)
        classes = {
            ActionKind.PROBE: ProbeDispatched,
            ActionKind.APPLICATION: ApplicationDispatched,
            ActionKind.PREPARATION: PreparationDispatched,
            ActionKind.FINALIZATION: FinalizationDispatched,
        }
        event_class = classes.get(action_id.kind)
        if event_class is None:
            raise ScenarioFormatError("planning uses plan_requested, not dispatch")
        return event_class(metadata, action_id, unit)
    if event_type == "finished":
        outcome = WorkerOutcome(_string(data["outcome"], "event.outcome"))
        status = (
            None
            if data["exit_status"] is None
            else _integer(data["exit_status"], "event.exit_status")
        )
        if action_id.kind is ActionKind.PROBE:
            return ProbeFinished(metadata, action_id, outcome, status)
        if action_id.kind is ActionKind.APPLICATION:
            return ApplicationFinished(metadata, action_id, outcome, status)
        if action_id.kind is ActionKind.PREPARATION:
            return PreparationFinished(
                metadata,
                action_id,
                outcome,
                status,
                PlanHash(_string(data["plan_hash"], "event.plan_hash")),
            )
        if action_id.kind is ActionKind.FINALIZATION:
            return FinalizationFinished(metadata, action_id, outcome, status)
        raise ScenarioFormatError("planning uses plan_completed, not finished")
    if event_type == "admission_dirtied":
        return AdmissionDirtied(
            metadata,
            action_id,
            EventGeneration(_integer(data["event_generation"], "event.generation")),
        )
    if event_type == "dispatch_rejected":
        return DispatchRejected(
            metadata, action_id, _string(data["reason"], "event.reason")
        )
    if event_type == "worker_status_unknown":
        return WorkerStatusUnknown(
            metadata, action_id, _string(data["reason"], "event.reason")
        )
    if event_type == "worker_timed_out":
        return WorkerTimedOut(
            metadata,
            action_id,
            _integer(data["deadline_ms"], "event.deadline_ms"),
        )
    if event_type == "cancellation_acknowledged":
        return WorkerCancellationAcknowledged(metadata, action_id)
    raise ScenarioFormatError(f"unsupported event type: {event_type}")


def normalize_state(state: State) -> dict[str, object]:
    """Return the stable language-neutral state assertion surface."""

    def action_id(action: object) -> str | None:
        value = getattr(action, "action_id", None)
        return value.value if isinstance(value, ActionId) else None

    def lifecycle(action: object) -> str | None:
        value = getattr(action, "lifecycle", None)
        return None if value is None else str(value)

    return {
        "phase": state.phase.value,
        "planning_state": state.planning_state.value,
        "preparation_state": state.preparation_state.value,
        "physical_epoch": state.physical_epoch,
        "candidate_profile": None
        if state.candidate is None
        else state.candidate.profile,
        "stable_x_profile": state.stable_x_profile,
        "desktop_finalized_profile": state.desktop_finalized_profile,
        "external_intent": state.external_intent,
        "baseline_adoption": state.baseline_adoption,
        "next_timer_ms": state.next_timer_ms,
        "verify_since_ms": state.verify_since_ms,
        "probe_action_id": action_id(state.probe),
        "probe_lifecycle": lifecycle(state.probe),
        "application_action_id": action_id(state.application),
        "application_lifecycle": lifecycle(state.application),
        "planning_action_id": action_id(state.planning),
        "planning_lifecycle": lifecycle(state.planning),
        "preparation_action_id": action_id(state.preparation),
        "preparation_lifecycle": lifecycle(state.preparation),
        "finalization_action_id": action_id(state.finalization),
        "finalization_lifecycle": lifecycle(state.finalization),
        "attempted_probe_count": len(state.attempted_probe_keys),
        "attempted_application_count": len(state.attempted_application_keys),
        "action_sequence_high_water": state.action_sequence_high_water,
        "transition_sequence_high_water": state.transition_sequence_high_water,
        "tombstone_count": len(state.action_tombstones),
    }


def normalize_effect(effect: Effect) -> dict[str, object]:  # noqa: PLR0911
    """Return a complete JSON-compatible effect representation."""
    if isinstance(effect, RequestObservation):
        return {"type": "request_observation", "reason": effect.reason.value}
    if isinstance(effect, Schedule):
        return {"type": "schedule", "deadline_ms": effect.deadline_ms}
    if isinstance(effect, RequestPlan):
        return {
            "type": "request_plan",
            "action_id": effect.action_id.value,
            "transition_id": effect.transition_id.value,
            "input_key": effect.input_key.value,
            "profile": effect.profile,
        }
    if isinstance(effect, ActivateProbe):
        attempt_key = (
            f"{effect.key.physical_epoch}|{effect.key.profile}|"
            f"{effect.key.observation_key.value}"
        )
        return {
            "type": "activate_probe",
            "action_id": effect.action_id.value,
            "key": attempt_key,
            "output": effect.output,
            "internal_output": effect.internal_output,
            "preferred_mode": effect.preferred_mode,
            "event_generation": effect.admitted_event_generation.value,
            "observation_key": effect.observation_key.value,
        }
    if isinstance(effect, ApplyProfile):
        attempt_key = (
            f"{effect.key.physical_epoch}|{effect.key.profile}|"
            f"{effect.key.observation_key.value}"
        )
        return {
            "type": "apply_profile",
            "action_id": effect.action_id.value,
            "key": attempt_key,
            "profile": effect.profile,
            "event_generation": effect.admitted_event_generation.value,
            "observation_key": effect.observation_key.value,
        }
    if isinstance(effect, PrepareDesktop):
        return {
            "type": "prepare_desktop",
            "action_id": effect.action_id.value,
            "transition_id": effect.transition_id.value,
            "transition_key": effect.transition_key.value,
            "profile": effect.profile,
            "plan_hash": effect.plan_hash.value,
            "event_generation": effect.admitted_event_generation.value,
            "observation_key": effect.observation_key.value,
        }
    if isinstance(effect, FinalizeDesktop):
        return {
            "type": "finalize_desktop",
            "action_id": effect.action_id.value,
            "transition_id": effect.transition_id.value,
            "transition_key": effect.transition_key.value,
            "profile": effect.profile,
            "plan_hash": effect.plan_hash.value,
            "event_generation": effect.admitted_event_generation.value,
            "observation_key": effect.observation_key.value,
        }
    if isinstance(effect, StopAction):
        return {"type": "stop_action", "action_id": effect.action_id.value}
    return {
        "type": "discard_plan",
        "action_id": effect.action_id.value,
        "plan_hash": None if effect.plan_hash is None else effect.plan_hash.value,
    }


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Replay one scenario, checking every intermediate state and ordered effect."""
    state = initial_state(scenario.initial)
    decisions: list[Decision] = []
    cumulative_counts: Counter[str] = Counter()
    for index, step in enumerate(scenario.steps):
        event = event_from_data(step.event_data, state)
        decision = reduce(state, event)
        normalized_state = normalize_state(decision.state)
        for key, expected in step.expected_state.items():
            actual = normalized_state[key]
            if actual != expected:
                raise ScenarioAssertionError(
                    f"{scenario.name} step {index} state.{key}: "
                    f"expected {expected!r}, got {actual!r}"
                )
        normalized_effects = tuple(normalize_effect(item) for item in decision.effects)
        if normalized_effects != step.expected_effects:
            raise ScenarioAssertionError(
                f"{scenario.name} step {index} effects:\n"
                f"expected {step.expected_effects!r}\nactual   {normalized_effects!r}"
            )
        cumulative_counts.update(
            cast("str", item["type"]) for item in normalized_effects
        )
        for key, expected_count in step.expected_effect_counts.items():
            actual_count = cumulative_counts[key]
            if actual_count != expected_count:
                raise ScenarioAssertionError(
                    f"{scenario.name} step {index} cumulative {key}: "
                    f"expected {expected_count}, got {actual_count}"
                )
        decisions.append(decision)
        state = decision.state
    return ScenarioResult(scenario, tuple(decisions))
