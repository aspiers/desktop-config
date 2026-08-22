"""Immutable domain values for monitor-controller decisions.

This module deliberately contains no adapters and performs no I/O. Runtime values such
as time, boot identity, observation validity, and worker identity enter the domain only
through explicitly typed event fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

SCHEMA_VERSION = 2
# Keep generated state comfortably below the codec's 1,024-record hard ceiling.
ACTION_TOMBSTONE_RETENTION_LIMIT = 768
_MAX_EXIT_STATUS = 255


def _require_uuid(value: object, field: str) -> None:
    if not isinstance(value, UUID):
        msg = f"{field} must be a UUID"
        raise TypeError(msg)


def _require_nonempty(value: str, field: str) -> None:
    if not value or value.isspace():
        msg = f"{field} must not be empty"
        raise ValueError(msg)


def _require_nonnegative(value: int, field: str) -> None:
    if value < 0:
        msg = f"{field} must be non-negative"
        raise ValueError(msg)


def _require_sorted_unique_strings(values: tuple[str, ...], field: str) -> None:
    if values != tuple(sorted(set(values))):
        msg = f"{field} must be sorted and contain no duplicates"
        raise ValueError(msg)
    if any(not value or value.isspace() for value in values):
        msg = f"{field} must contain non-empty values"
        raise ValueError(msg)


def _require_sorted_unique_keys(keys: tuple[str, ...], field: str) -> None:
    if len(set(keys)) != len(keys) or keys != tuple(sorted(keys)):
        msg = f"{field} must be sorted and contain no duplicates"
        raise ValueError(msg)


def _require_unique_keys(keys: tuple[str, ...], field: str) -> None:
    if len(set(keys)) != len(keys):
        msg = f"valid observation requires unique {field}"
        raise ValueError(msg)


class ControllerPhase(StrEnum):
    """Main display-convergence and finalization phases."""

    RECOVERING = "recovering"
    QUIESCENT = "quiescent"
    DISCOVER_FAST = "discover_fast"
    PROBE_PENDING = "probe_pending"
    PROBING = "probing"
    PROBE_FAILED = "probe_failed"
    APPLY_PENDING = "apply_pending"
    APPLYING = "applying"
    APPLY_FAILED = "apply_failed"
    VERIFYING = "verifying"
    WAIT_SLOW = "wait_slow"
    UNSUPPORTED = "unsupported"
    FINALIZE_PENDING = "finalize_pending"
    FINALIZING = "finalizing"
    FINALIZE_STOPPING = "finalize_stopping"
    FINALIZE_FAILED = "finalize_failed"


class PlanningState(StrEnum):
    """Orthogonal pure planning lifecycle."""

    PLAN_IDLE = "plan_idle"
    PLAN_PENDING = "plan_pending"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    PLAN_FAILED = "plan_failed"


class PreparationState(StrEnum):
    """Orthogonal repeatable desktop-preparation lifecycle."""

    PREPARE_IDLE = "prepare_idle"
    PREPARE_PENDING = "prepare_pending"
    PREPARING = "preparing"
    PREPARED = "prepared"
    PREPARE_STOPPING = "prepare_stopping"
    PREPARE_FAILED = "prepare_failed"


class ProfileScope(StrEnum):
    """Hardware scope of an autorandr profile."""

    INTERNAL_ONLY = "internal_only"
    EXTERNAL_ONLY = "external_only"
    MIXED = "mixed"


class EdidIntegrity(StrEnum):
    """Completeness and checksum state of observed EDID bytes."""

    ABSENT = "absent"
    BASE_INVALID = "base_invalid"
    BASE_VALID_EXTENSIONS_INCOMPLETE = "base_valid_extensions_incomplete"
    BASE_VALID_EXTENSIONS_INVALID = "base_valid_extensions_invalid"
    COMPLETE = "complete"


BROKEN_EXTENSION_EDID_INTEGRITIES: frozenset[EdidIntegrity] = frozenset(
    {
        EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE,
        EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
    }
)


class ObservationValidity(StrEnum):
    """Whether a canonical sample may be used for classification."""

    VALID = "valid"
    INVALID = "invalid"


class ObservationInvalidityReason(StrEnum):
    """Typed reasons an observation is retained but cannot authorize work."""

    EVENT_GENERATION_CHANGED = "event_generation_changed"
    TOPOLOGY_CHANGED = "topology_changed"
    COMMAND_TIMED_OUT = "command_timed_out"
    PARSE_FAILED = "parse_failed"
    INCONSISTENT_EVIDENCE = "inconsistent_evidence"


class RawEvidenceSource(StrEnum):
    """Canonical source kinds for diagnostic raw evidence."""

    DRM_CONNECTORS = "drm_connectors"
    DRM_EDID = "drm_edid"
    XRANDR_QUERY = "xrandr_query"
    XRANDR_PROPERTIES = "xrandr_properties"
    AUTORANDR_FINGERPRINT = "autorandr_fingerprint"
    AUTORANDR_PROFILES = "autorandr_profiles"


class ActionKind(StrEnum):
    """Kinds of keyed work which can be admitted by the reducer."""

    PLAN = "plan"
    PROBE = "probe"
    APPLICATION = "application"
    PREPARATION = "preparation"
    FINALIZATION = "finalization"


class ActionLifecycle(StrEnum):
    """Durable action lifecycle, including admission and acknowledgement."""

    ADMITTED = "admitted"
    DISPATCHED = "dispatched"
    STOPPING = "stopping"
    RESULT_PENDING = "result_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    TIMED_OUT = "timed_out"


TERMINAL_ACTION_LIFECYCLES: frozenset[ActionLifecycle] = frozenset(
    {
        ActionLifecycle.COMPLETED,
        ActionLifecycle.FAILED,
        ActionLifecycle.CANCELLED,
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    }
)


class WorkerOutcome(StrEnum):
    """Terminal result reported by a keyed worker."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WakeReason(StrEnum):
    """Reasons for requesting a canonical observation."""

    STARTUP = "startup"
    DRM_HINT = "drm_hint"
    TIMER = "timer"
    WORKER_COMPLETED = "worker_completed"
    RECOVERY = "recovery"
    DIRTY_ADMISSION = "dirty_admission"


@dataclass(frozen=True, slots=True)
class BootId:
    """Kernel boot identity used to fence monotonic values."""

    value: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.value, "boot ID")


@dataclass(frozen=True, slots=True)
class ControllerInstanceId:
    """Fresh controller identity used in every newly allocated action ID."""

    value: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.value, "controller instance ID")


@dataclass(frozen=True, slots=True)
class DisplayIdentity:
    """Explicit X display/seat identity controlled by this state record."""

    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.value, "display identity")


@dataclass(frozen=True, slots=True, order=True)
class ObservationGeneration:
    """Sequence allocated to completed canonical observation samples."""

    value: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.value, "observation generation")


@dataclass(frozen=True, slots=True, order=True)
class EventGeneration:
    """Sequence incremented by generation-sensitive runtime input."""

    value: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.value, "event generation")


@dataclass(frozen=True, slots=True)
class ObservationKey:
    """Content identity for all canonical observation evidence except time."""

    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.value, "observation key")


@dataclass(frozen=True, slots=True)
class PhysicalToken:
    """Stable identity for one observed physical connector topology."""

    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.value, "physical token")


@dataclass(frozen=True, slots=True)
class PlanHash:
    """Content hash of immutable staged desktop artifacts."""

    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.value, "plan hash")


@dataclass(frozen=True, slots=True)
class ConfigurationContentHash:
    """Digest of one configuration input which can change a desktop plan."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        _require_nonempty(self.path, "configuration path")
        _require_nonempty(self.sha256, "configuration content hash")


@dataclass(frozen=True, slots=True)
class PlanningInputKey:
    """Complete identity of all inputs from which a desktop plan is derived."""

    physical_epoch: int
    profile: str
    layout: str
    observation_key: ObservationKey
    mapping: tuple[OutputMapping, ...]
    configuration_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        _require_nonnegative(self.physical_epoch, "planning physical epoch")
        _require_nonempty(self.profile, "planning profile")
        _require_nonempty(self.layout, "planning layout")
        if not self.mapping:
            msg = "planning input mapping must not be empty"
            raise ValueError(msg)
        mapping_keys = tuple(
            f"{item.saved_output}\0{item.live_output}" for item in self.mapping
        )
        _require_sorted_unique_keys(mapping_keys, "planning input mapping")
        saved = tuple(item.saved_output for item in self.mapping)
        live = tuple(item.live_output for item in self.mapping)
        if len(set(saved)) != len(saved) or len(set(live)) != len(live):
            msg = "planning input mapping must be a bijection"
            raise ValueError(msg)
        if not self.configuration_hashes:
            msg = "planning input requires configuration content hashes"
            raise ValueError(msg)
        hash_keys = tuple(
            f"{item.path}\0{item.sha256}" for item in self.configuration_hashes
        )
        _require_sorted_unique_keys(hash_keys, "planning configuration hashes")

    @property
    def value(self) -> str:
        """Return a stable language-neutral representation for diagnostics."""
        mapping = ",".join(
            f"{item.saved_output}>{item.live_output}" for item in self.mapping
        )
        hashes = ",".join(
            f"{item.path}={item.sha256}" for item in self.configuration_hashes
        )
        return (
            f"{self.physical_epoch}|{self.profile}|{self.layout}|"
            f"{self.observation_key.value}|{mapping}|{hashes}"
        )


@dataclass(frozen=True, slots=True)
class TransitionKey:
    """Identity of one exact profile/topology desktop transition."""

    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.value, "transition key")


@dataclass(frozen=True, slots=True)
class ActionId:
    """Never-reused keyed action identity."""

    controller_instance: ControllerInstanceId
    kind: ActionKind
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            msg = "action sequence must be positive"
            raise ValueError(msg)

    @property
    def value(self) -> str:
        """Return the language-neutral identity used by worker requests."""
        return f"{self.kind.value}-{self.controller_instance.value.hex}-{self.sequence}"


@dataclass(frozen=True, slots=True)
class TransitionId:
    """Never-reused desktop transition identity."""

    controller_instance: ControllerInstanceId
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            msg = "transition sequence must be positive"
            raise ValueError(msg)

    @property
    def value(self) -> str:
        """Return the language-neutral transition identity."""
        return f"transition-{self.controller_instance.value.hex}-{self.sequence}"


@dataclass(frozen=True, slots=True)
class OutputMapping:
    """One proven saved-output to live-output mapping pair."""

    saved_output: str
    live_output: str

    def __post_init__(self) -> None:
        _require_nonempty(self.saved_output, "saved output")
        _require_nonempty(self.live_output, "live output")


@dataclass(frozen=True, slots=True)
class MappingProof:
    """Unique profile output bijection proven for one physical epoch."""

    profile: str
    physical_epoch: int
    observation_key: ObservationKey
    outputs: tuple[OutputMapping, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.profile, "mapping profile")
        _require_nonnegative(self.physical_epoch, "physical epoch")
        if not self.outputs:
            msg = "output mapping must not be empty"
            raise ValueError(msg)
        saved = tuple(item.saved_output for item in self.outputs)
        live = tuple(item.live_output for item in self.outputs)
        if len(set(saved)) != len(saved) or len(set(live)) != len(live):
            msg = "output mapping must be a bijection"
            raise ValueError(msg)
        mapping_keys = tuple(
            f"{item.saved_output}\0{item.live_output}" for item in self.outputs
        )
        _require_sorted_unique_keys(mapping_keys, "output mapping")


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Machine-readable live output fingerprint."""

    output: str
    value: str

    def __post_init__(self) -> None:
        _require_nonempty(self.output, "fingerprint output")
        _require_nonempty(self.value, "fingerprint value")


@dataclass(frozen=True, slots=True)
class EdidEvidence:
    """EDID base identity and extension integrity for one connector."""

    output: str
    integrity: EdidIntegrity
    base_hash: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.output, "EDID output")
        if self.integrity is EdidIntegrity.ABSENT and self.base_hash is not None:
            msg = "absent EDID cannot have a base hash"
            raise ValueError(msg)
        if self.integrity not in {EdidIntegrity.ABSENT, EdidIntegrity.BASE_INVALID}:
            if self.base_hash is None:
                msg = "checksum-valid EDID must have a base hash"
                raise ValueError(msg)
            _require_nonempty(self.base_hash, "EDID base hash")


@dataclass(frozen=True, slots=True)
class ConnectorIdentityEvidence:
    """Kernel/X connector identity correspondence retained in a sample."""

    output: str
    kernel_connector: str
    kernel_connector_id: int
    x_connector_id: int | None

    def __post_init__(self) -> None:
        _require_nonempty(self.output, "connector identity output")
        _require_nonempty(self.kernel_connector, "kernel connector")
        _require_nonnegative(self.kernel_connector_id, "kernel connector ID")
        if self.x_connector_id is not None:
            _require_nonnegative(self.x_connector_id, "X connector ID")


@dataclass(frozen=True, slots=True)
class RawEvidenceReference:
    """Reference and digest for one preserved raw observer input."""

    source: RawEvidenceSource
    reference: str
    sha256: str

    def __post_init__(self) -> None:
        _require_nonempty(self.reference, "raw evidence reference")
        _require_nonempty(self.sha256, "raw evidence hash")


@dataclass(frozen=True, slots=True)
class ProfileMatch:
    """Eligible profile, exact layout, mapping, and planning configuration inputs."""

    profile: str
    scope: ProfileScope
    layout: str
    mapping: tuple[OutputMapping, ...]
    active_outputs: tuple[str, ...]
    configuration_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.profile, "profile")
        _require_nonempty(self.layout, "profile layout")
        if not self.configuration_hashes:
            msg = "profile match requires configuration content hashes"
            raise ValueError(msg)
        hash_keys = tuple(
            f"{item.path}\0{item.sha256}" for item in self.configuration_hashes
        )
        _require_sorted_unique_keys(hash_keys, "profile configuration hashes")
        if not self.mapping:
            msg = "profile mapping must not be empty"
            raise ValueError(msg)
        _require_sorted_unique_strings(self.active_outputs, "profile active outputs")
        saved = tuple(item.saved_output for item in self.mapping)
        live = tuple(item.live_output for item in self.mapping)
        if len(set(saved)) != len(saved) or len(set(live)) != len(live):
            msg = "profile mapping must be a bijection"
            raise ValueError(msg)
        if not set(self.active_outputs) <= set(live):
            msg = "profile active outputs must be included in its mapping"
            raise ValueError(msg)
        mapping_keys = tuple(
            f"{item.saved_output}\0{item.live_output}" for item in self.mapping
        )
        _require_sorted_unique_keys(mapping_keys, "profile mapping")


@dataclass(frozen=True, slots=True)
class BaseIdentityMatch:
    """Exact checksum-valid EDID base identity match to a saved profile."""

    profile: str
    output: str

    def __post_init__(self) -> None:
        _require_nonempty(self.profile, "base identity profile")
        _require_nonempty(self.output, "base identity output")


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    """Fully constrained safe activation probe from canonical evidence."""

    profile: str
    output: str
    internal_output: str
    preferred_mode: str

    def __post_init__(self) -> None:
        _require_nonempty(self.profile, "probe profile")
        _require_nonempty(self.output, "probe output")
        _require_nonempty(self.internal_output, "probe internal output")
        _require_nonempty(self.preferred_mode, "probe preferred mode")
        if self.output == self.internal_output:
            msg = "probe output must differ from its active internal output"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    """One immutable, internally classified hardware/X/profile sample."""

    observed_at_ms: int
    observation_generation: ObservationGeneration
    boot_id: BootId
    physical_token: PhysicalToken
    begin_event_generation: EventGeneration
    end_event_generation: EventGeneration
    kernel_connected_outputs: tuple[str, ...]
    kernel_external_outputs: tuple[str, ...]
    x_connected_outputs: tuple[str, ...]
    x_active_outputs: tuple[str, ...]
    x_external_outputs: tuple[str, ...]
    connector_identities: tuple[ConnectorIdentityEvidence, ...]
    live_fingerprints: tuple[Fingerprint, ...]
    base_identity_profiles: tuple[BaseIdentityMatch, ...]
    edid_integrity: tuple[EdidEvidence, ...]
    probe_candidate: ProbeCandidate | None
    eligible_profiles: tuple[ProfileMatch, ...]
    current_profiles: tuple[str, ...]
    exact_profile: str | None
    observation_key: ObservationKey
    validity: ObservationValidity
    invalidity_reason: ObservationInvalidityReason | None
    raw_evidence: tuple[RawEvidenceReference, ...]

    def __post_init__(self) -> None:
        _require_nonnegative(self.observed_at_ms, "observation sample time")
        for field, values in (
            ("kernel connected outputs", self.kernel_connected_outputs),
            ("kernel external outputs", self.kernel_external_outputs),
            ("X connected outputs", self.x_connected_outputs),
            ("X active outputs", self.x_active_outputs),
            ("X external outputs", self.x_external_outputs),
            ("current profiles", self.current_profiles),
        ):
            _require_sorted_unique_strings(values, field)
        keyed_fields = (
            (
                tuple(
                    "\0".join(
                        (
                            item.output,
                            item.kernel_connector,
                            f"{item.kernel_connector_id:020d}",
                            "-"
                            if item.x_connector_id is None
                            else f"{item.x_connector_id:020d}",
                        )
                    )
                    for item in self.connector_identities
                ),
                "connector identities",
            ),
            (
                tuple(
                    f"{item.output}\0{item.value}" for item in self.live_fingerprints
                ),
                "live fingerprints",
            ),
            (
                tuple(
                    f"{item.profile}\0{item.output}"
                    for item in self.base_identity_profiles
                ),
                "base identity profiles",
            ),
            (
                tuple(
                    "\0".join(
                        (
                            item.output,
                            item.integrity.value,
                            item.base_hash or "",
                        )
                    )
                    for item in self.edid_integrity
                ),
                "EDID integrity evidence",
            ),
            (
                tuple(
                    "\0".join(
                        (
                            item.profile,
                            item.scope.value,
                            item.layout,
                            ";".join(
                                f"{mapping.saved_output}>{mapping.live_output}"
                                for mapping in item.mapping
                            ),
                            ";".join(item.active_outputs),
                            ";".join(
                                f"{config.path}={config.sha256}"
                                for config in item.configuration_hashes
                            ),
                        )
                    )
                    for item in self.eligible_profiles
                ),
                "eligible profiles",
            ),
            (
                tuple(
                    f"{item.source.value}\0{item.reference}\0{item.sha256}"
                    for item in self.raw_evidence
                ),
                "raw evidence references",
            ),
        )
        for keys, field in keyed_fields:
            _require_sorted_unique_keys(keys, field)
        if self.valid:
            self._validate_consistent_sample()
        else:
            if self.invalidity_reason is None:
                msg = "invalid observation requires an invalidity reason"
                raise ValueError(msg)
            if self.probe_candidate is not None or self.exact_profile is not None:
                msg = "invalid observation cannot authorize a probe or exact profile"
                raise ValueError(msg)

    @property
    def valid(self) -> bool:
        """Return whether the sample is coherent enough for classification."""
        return self.validity is ObservationValidity.VALID

    @property
    def event_generation(self) -> EventGeneration:
        """Return the generation captured at successful sample completion."""
        return self.end_event_generation

    @property
    def has_external_hardware(self) -> bool:
        """Return whether either raw topology source reports external hardware."""
        return bool(self.kernel_external_outputs) or bool(self.x_external_outputs)

    def _validate_consistent_sample(self) -> None:  # noqa: C901, PLR0912, PLR0915
        if self.invalidity_reason is not None:
            msg = "valid observation cannot have an invalidity reason"
            raise ValueError(msg)
        for keys, field in (
            (
                tuple(item.output for item in self.connector_identities),
                "connector identity outputs",
            ),
            (
                tuple(item.output for item in self.live_fingerprints),
                "live fingerprint outputs",
            ),
            (
                tuple(item.output for item in self.edid_integrity),
                "EDID evidence outputs",
            ),
            (
                tuple(item.profile for item in self.eligible_profiles),
                "eligible profile names",
            ),
            (
                tuple(
                    f"{item.source.value}\0{item.reference}"
                    for item in self.raw_evidence
                ),
                "raw evidence source/reference pairs",
            ),
        ):
            _require_unique_keys(keys, field)
        if self.begin_event_generation != self.end_event_generation:
            msg = "valid observation requires equal event-generation boundaries"
            raise ValueError(msg)
        if not set(self.kernel_external_outputs) <= set(self.kernel_connected_outputs):
            msg = "kernel external outputs must be kernel-connected"
            raise ValueError(msg)
        if not set(self.x_active_outputs) <= set(self.x_connected_outputs):
            msg = "X active outputs must be X-connected"
            raise ValueError(msg)
        if not set(self.x_external_outputs) <= set(self.x_connected_outputs):
            msg = "X external outputs must be X-connected"
            raise ValueError(msg)
        known_outputs = set(self.kernel_connected_outputs) | set(
            self.x_connected_outputs
        )
        if any(item.output not in known_outputs for item in self.connector_identities):
            msg = "connector identity evidence must reference a connected output"
            raise ValueError(msg)
        if any(
            item.output not in self.x_connected_outputs
            for item in self.live_fingerprints
        ):
            msg = "live fingerprints must reference X-connected outputs"
            raise ValueError(msg)
        if any(
            item.output not in self.kernel_connected_outputs
            for item in self.edid_integrity
        ):
            msg = "EDID evidence must reference kernel-connected outputs"
            raise ValueError(msg)
        if any(
            item.output not in known_outputs for item in self.base_identity_profiles
        ):
            msg = "base identity evidence must reference a connected output"
            raise ValueError(msg)
        for profile in self.eligible_profiles:
            if not {item.live_output for item in profile.mapping} <= set(
                self.x_connected_outputs
            ):
                msg = "eligible profile mappings must reference X-connected outputs"
                raise ValueError(msg)
        if self.exact_profile is not None:
            _require_nonempty(self.exact_profile, "exact profile")
            match = next(
                (
                    item
                    for item in self.eligible_profiles
                    if item.profile == self.exact_profile
                ),
                None,
            )
            if match is None:
                msg = "exact profile must also be eligible"
                raise ValueError(msg)
            connected = set(self.kernel_connected_outputs)
            mapped = {item.live_output for item in match.mapping}
            if not (
                connected == set(self.x_connected_outputs) == mapped
                and set(self.x_active_outputs) == set(match.active_outputs)
            ):
                msg = "exact profile requires a full connected/active mapping bijection"
                raise ValueError(msg)
            if self.exact_profile not in self.current_profiles:
                msg = "exact profile must be reported current"
                raise ValueError(msg)
            external = set(self.kernel_external_outputs) | set(self.x_external_outputs)
            complete_edid = {
                item.output
                for item in self.edid_integrity
                if item.integrity is EdidIntegrity.COMPLETE
            }
            base_matches = {
                item.output
                for item in self.base_identity_profiles
                if item.profile == self.exact_profile
            }
            identified = {
                item.output
                for item in self.connector_identities
                if item.x_connector_id is not None
            }
            if not external <= complete_edid & base_matches & identified:
                msg = (
                    "exact external outputs require complete EDID, base identity, "
                    "and connector correspondence"
                )
                raise ValueError(msg)
        if self.probe_candidate is not None:
            probe = self.probe_candidate
            external = {probe.output}
            internal = {probe.internal_output}
            matching_base = tuple(
                item
                for item in self.base_identity_profiles
                if item.profile == probe.profile and item.output == probe.output
            )
            probe_edid = next(
                (item for item in self.edid_integrity if item.output == probe.output),
                None,
            )
            identified = any(
                item.output == probe.output and item.x_connector_id is not None
                for item in self.connector_identities
            )
            if not (
                set(self.kernel_external_outputs)
                == set(self.x_external_outputs)
                == external
                and set(self.kernel_connected_outputs)
                == set(self.x_connected_outputs)
                == external | internal
                and set(self.x_active_outputs) == internal
                and len(matching_base) == 1
                and probe_edid is not None
                and probe_edid.integrity in BROKEN_EXTENSION_EDID_INTEGRITIES
                and identified
            ):
                msg = (
                    "probe candidate requires one exact base identity and exact "
                    "external/inactive plus internal/active topology"
                )
                raise ValueError(msg)
            if self.eligible_profiles:
                msg = "probe candidate requires no full eligible profile"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CandidateSelection:
    """Persisted exact target selection and proof identity."""

    profile: str
    scope: ProfileScope
    mapping: MappingProof
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        _require_nonempty(self.profile, "candidate profile")
        if self.mapping.profile != self.profile:
            msg = "candidate and mapping profiles must match"
            raise ValueError(msg)
        if self.mapping.observation_key != self.observation_key:
            msg = "candidate and mapping observation keys must match"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProbeAttemptKey:
    """Deduplication identity for a safe activation probe."""

    physical_epoch: int
    profile: str
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        _require_nonnegative(self.physical_epoch, "physical epoch")
        _require_nonempty(self.profile, "probe attempt profile")


@dataclass(frozen=True, slots=True)
class ApplicationAttemptKey:
    """Deduplication identity for an explicit profile application."""

    physical_epoch: int
    profile: str
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        _require_nonnegative(self.physical_epoch, "physical epoch")
        _require_nonempty(self.profile, "application attempt profile")


@dataclass(frozen=True, slots=True)
class WorkerUnit:
    """Recoverable systemd worker unit identity."""

    action_id: ActionId
    unit_name: str

    def __post_init__(self) -> None:
        _require_nonempty(self.unit_name, "worker unit name")


@dataclass(frozen=True, slots=True)
class ProbeAction:
    """Persisted safe activation-probe action."""

    action_id: ActionId
    key: ProbeAttemptKey
    admitted_event_generation: EventGeneration
    output: str
    internal_output: str
    preferred_mode: str
    lifecycle: ActionLifecycle = ActionLifecycle.ADMITTED
    unit: WorkerUnit | None = None
    worker_deadline_ms: int | None = None
    exit_status: int | None = None
    terminal_after_stop: ActionLifecycle | None = None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PROBE:
            msg = "probe action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.output, "probe output")
        _require_nonempty(self.internal_output, "probe internal output")
        _require_nonempty(self.preferred_mode, "probe preferred mode")
        _validate_action_worker(
            self.action_id, self.lifecycle, self.unit, self.worker_deadline_ms
        )
        _validate_terminal_after_stop(self.lifecycle, self.terminal_after_stop)


@dataclass(frozen=True, slots=True)
class ApplicationAction:
    """Persisted explicit autorandr application action."""

    action_id: ActionId
    key: ApplicationAttemptKey
    admitted_event_generation: EventGeneration
    profile: str
    scope: ProfileScope
    mapping: MappingProof
    lifecycle: ActionLifecycle = ActionLifecycle.ADMITTED
    unit: WorkerUnit | None = None
    worker_deadline_ms: int | None = None
    exit_status: int | None = None
    terminal_after_stop: ActionLifecycle | None = None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.APPLICATION:
            msg = "application action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "application profile")
        if self.key.profile != self.profile or self.mapping.profile != self.profile:
            msg = "application identity profiles must match"
            raise ValueError(msg)
        _validate_action_worker(
            self.action_id, self.lifecycle, self.unit, self.worker_deadline_ms
        )
        _validate_terminal_after_stop(self.lifecycle, self.terminal_after_stop)


@dataclass(frozen=True, slots=True)
class PlanningAction:
    """Persisted pure desktop-planning action."""

    action_id: ActionId
    transition_id: TransitionId
    input_key: PlanningInputKey
    profile: str
    lifecycle: ActionLifecycle = ActionLifecycle.ADMITTED
    plan_hash: PlanHash | None = None
    exit_status: int | None = None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "planning action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "planning profile")
        if self.input_key.profile != self.profile:
            msg = "planning action and input-key profiles must match"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PreparationAction:
    """Persisted repeatable desktop-preparation action."""

    action_id: ActionId
    transition_id: TransitionId
    transition_key: TransitionKey
    plan_hash: PlanHash
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey
    profile: str
    lifecycle: ActionLifecycle = ActionLifecycle.ADMITTED
    unit: WorkerUnit | None = None
    worker_deadline_ms: int | None = None
    exit_status: int | None = None
    terminal_after_stop: ActionLifecycle | None = None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PREPARATION:
            msg = "preparation action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "preparation profile")
        _validate_action_worker(
            self.action_id, self.lifecycle, self.unit, self.worker_deadline_ms
        )
        _validate_terminal_after_stop(self.lifecycle, self.terminal_after_stop)


@dataclass(frozen=True, slots=True)
class FinalizationAction:
    """Persisted disruptive desktop-finalization action."""

    action_id: ActionId
    transition_id: TransitionId
    transition_key: TransitionKey
    plan_hash: PlanHash
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey
    profile: str
    lifecycle: ActionLifecycle = ActionLifecycle.ADMITTED
    unit: WorkerUnit | None = None
    worker_deadline_ms: int | None = None
    exit_status: int | None = None
    terminal_after_stop: ActionLifecycle | None = None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.FINALIZATION:
            msg = "finalization action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "finalization profile")
        _validate_action_worker(
            self.action_id, self.lifecycle, self.unit, self.worker_deadline_ms
        )
        _validate_terminal_after_stop(self.lifecycle, self.terminal_after_stop)


def _validate_terminal_after_stop(
    lifecycle: ActionLifecycle, terminal_after_stop: ActionLifecycle | None
) -> None:
    if terminal_after_stop is not None and terminal_after_stop not in {
        ActionLifecycle.UNKNOWN,
        ActionLifecycle.TIMED_OUT,
    }:
        msg = "post-stop terminal lifecycle must be unknown or timed out"
        raise ValueError(msg)
    if terminal_after_stop is not None and lifecycle is not ActionLifecycle.STOPPING:
        msg = "post-stop terminal lifecycle requires stopping exclusion"
        raise ValueError(msg)


def _validate_action_worker(
    action_id: ActionId,
    lifecycle: ActionLifecycle,
    unit: WorkerUnit | None,
    worker_deadline_ms: int | None,
) -> None:
    if unit is not None and unit.action_id != action_id:
        msg = "worker unit and action IDs must match"
        raise ValueError(msg)
    if worker_deadline_ms is not None:
        _require_nonnegative(worker_deadline_ms, "worker deadline")
    if lifecycle is ActionLifecycle.ADMITTED and worker_deadline_ms is not None:
        msg = "admitted action cannot already have a worker deadline"
        raise ValueError(msg)


def _validate_action_unit(action_id: ActionId, unit: WorkerUnit | None) -> None:
    if unit is not None and unit.action_id != action_id:
        msg = "worker unit and action IDs must match"
        raise ValueError(msg)


type ActionRecord = (
    ProbeAction
    | ApplicationAction
    | PlanningAction
    | PreparationAction
    | FinalizationAction
)


@dataclass(frozen=True, slots=True)
class UnplugProof:
    """Two-sided no-external-output proof accumulated over time."""

    first_observation_key: ObservationKey
    first_observed_at_ms: int
    latest_observation_key: ObservationKey
    latest_observed_at_ms: int
    observation_count: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.first_observed_at_ms, "first unplug time")
        _require_nonnegative(self.latest_observed_at_ms, "latest unplug time")
        if self.latest_observed_at_ms < self.first_observed_at_ms:
            msg = "latest unplug proof cannot precede its first sample"
            raise ValueError(msg)
        if self.observation_count < 1:
            msg = "unplug observation count must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ActionTombstone:
    """Recovery-relevant terminal action identity."""

    action_id: ActionId
    lifecycle: ActionLifecycle

    def __post_init__(self) -> None:
        if self.lifecycle not in TERMINAL_ACTION_LIFECYCLES:
            msg = "action tombstone must have a terminal lifecycle"
            raise ValueError(msg)


def bound_action_tombstones(
    tombstones: tuple[ActionTombstone, ...],
    *,
    protected_action_ids: frozenset[ActionId] = frozenset(),
) -> tuple[ActionTombstone, ...]:
    """Retain a deterministic recent subset below the codec's hard ceiling.

    Sequence high-water marks summarize pruned terminal identities. Tombstones for
    terminal actions still represented in ``State`` remain protected until that
    action record is cleared.
    """
    excess = len(tombstones) - ACTION_TOMBSTONE_RETENTION_LIMIT
    if excess <= 0:
        return tombstones
    retained: list[ActionTombstone] = []
    for tombstone in tombstones:
        if excess and tombstone.action_id not in protected_action_ids:
            excess -= 1
        else:
            retained.append(tombstone)
    if excess:
        msg = "protected action tombstones exceed the retention limit"
        raise ValueError(msg)
    return tuple(retained)


@dataclass(frozen=True, slots=True)
class State:
    """Complete immutable reducer state and recovery identity."""

    boot_id: BootId
    controller_instance: ControllerInstanceId
    display_identity: DisplayIdentity
    schema_version: int = SCHEMA_VERSION
    latest_observation: CanonicalObservation | None = None
    phase: ControllerPhase = ControllerPhase.RECOVERING
    planning_state: PlanningState = PlanningState.PLAN_IDLE
    preparation_state: PreparationState = PreparationState.PREPARE_IDLE
    physical_epoch: int = 0
    physical_token: PhysicalToken | None = None
    reconcile_epoch: int = 0
    candidate: CandidateSelection | None = None
    aggressive_deadline_ms: int | None = None
    next_timer_ms: int | None = None
    backoff_index: int = 0
    verify_since_ms: int | None = None
    last_drm_at_ms: int | None = None
    stable_x_profile: str | None = None
    desktop_finalized_profile: str | None = None
    external_intent: bool = False
    baseline_adoption: bool = False
    attempted_probe_keys: frozenset[ProbeAttemptKey] = frozenset()
    probe: ProbeAction | None = None
    attempted_application_keys: frozenset[ApplicationAttemptKey] = frozenset()
    application: ApplicationAction | None = None
    planning: PlanningAction | None = None
    preparation: PreparationAction | None = None
    finalization: FinalizationAction | None = None
    unknown_key: ObservationKey | None = None
    unknown_since_ms: int | None = None
    unplug_proof: UnplugProof | None = None
    observation_generation: ObservationGeneration = ObservationGeneration(0)
    event_generation: EventGeneration = EventGeneration(0)
    action_sequence_high_water: int = 0
    transition_sequence_high_water: int = 0
    action_tombstones: tuple[ActionTombstone, ...] = ()
    recovery_units: tuple[WorkerUnit, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            msg = f"schema version must be {SCHEMA_VERSION}"
            raise ValueError(msg)
        for field, value in (
            ("physical epoch", self.physical_epoch),
            ("reconcile epoch", self.reconcile_epoch),
            ("backoff index", self.backoff_index),
            ("action sequence high-water mark", self.action_sequence_high_water),
            (
                "transition sequence high-water mark",
                self.transition_sequence_high_water,
            ),
        ):
            _require_nonnegative(value, field)
        for field, value in (
            ("aggressive deadline", self.aggressive_deadline_ms),
            ("next timer", self.next_timer_ms),
            ("verification start", self.verify_since_ms),
            ("last DRM time", self.last_drm_at_ms),
            ("unknown start", self.unknown_since_ms),
        ):
            if value is not None:
                _require_nonnegative(value, field)
        for field, value in (
            ("stable X profile", self.stable_x_profile),
            ("desktop finalized profile", self.desktop_finalized_profile),
        ):
            if value is not None:
                _require_nonempty(value, field)


@dataclass(frozen=True, slots=True)
class EventMetadata:
    """Shared processing context carried by every reducer event."""

    processed_at_ms: int
    boot_id: BootId

    def __post_init__(self) -> None:
        _require_nonnegative(self.processed_at_ms, "event processing time")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Frozen base envelope requiring metadata on every event."""

    metadata: EventMetadata


# Reducer inputs. Sample time belongs to observations; processing time belongs here.
@dataclass(frozen=True, slots=True)
class ObservationCompleted(EventEnvelope):
    """A canonical observation completed."""

    observation: CanonicalObservation

    def __post_init__(self) -> None:
        if self.observation.boot_id != self.metadata.boot_id:
            msg = "observation and event metadata boot IDs must match"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ObservationFailed(EventEnvelope):
    """The canonical observation adapter failed or exceeded its deadline."""

    reason: str

    def __post_init__(self) -> None:
        _require_nonempty(self.reason, "observation failure reason")


@dataclass(frozen=True, slots=True)
class PlanRequested(EventEnvelope):
    """The runtime accepted a planning task."""

    action_id: ActionId
    input_key: PlanningInputKey

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "plan request action ID has the wrong kind"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PlanCompleted(EventEnvelope):
    """A planning task returned immutable staged artifacts."""

    action_id: ActionId
    input_key: PlanningInputKey
    plan_hash: PlanHash

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "plan completion action ID has the wrong kind"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PlanFailed(EventEnvelope):
    """A planning task failed."""

    action_id: ActionId
    input_key: PlanningInputKey
    reason: str
    exit_status: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.reason, "failure reason")
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "plan failure action ID has the wrong kind"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TimerFired(EventEnvelope):
    """A persisted monotonic deadline fired."""

    deadline_ms: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.deadline_ms, "timer deadline")


@dataclass(frozen=True, slots=True)
class DrmHintReceived(EventEnvelope):
    """A coalescible DRM wake hint incremented event generation."""

    event_generation: EventGeneration


@dataclass(frozen=True, slots=True)
class AdmissionDirtied(EventEnvelope):
    """Queued hardware input invalidated a persisted action admission."""

    action_id: ActionId
    event_generation: EventGeneration


@dataclass(frozen=True, slots=True)
class ProbeDispatched(EventEnvelope):
    """The supervisor accepted a keyed probe worker."""

    action_id: ActionId
    unit: WorkerUnit

    def __post_init__(self) -> None:
        _validate_dispatched_event(self.action_id, self.unit, ActionKind.PROBE)


@dataclass(frozen=True, slots=True)
class ProbeFinished(EventEnvelope):
    """A keyed probe worker reached a terminal result."""

    action_id: ActionId
    outcome: WorkerOutcome
    exit_status: int | None

    def __post_init__(self) -> None:
        _validate_finished_event(self.action_id, ActionKind.PROBE)


@dataclass(frozen=True, slots=True)
class ApplicationDispatched(EventEnvelope):
    """The supervisor accepted a keyed profile-application worker."""

    action_id: ActionId
    unit: WorkerUnit

    def __post_init__(self) -> None:
        _validate_dispatched_event(self.action_id, self.unit, ActionKind.APPLICATION)


@dataclass(frozen=True, slots=True)
class ApplicationFinished(EventEnvelope):
    """A keyed profile-application worker reached a terminal result."""

    action_id: ActionId
    outcome: WorkerOutcome
    exit_status: int | None

    def __post_init__(self) -> None:
        _validate_finished_event(self.action_id, ActionKind.APPLICATION)


@dataclass(frozen=True, slots=True)
class PreparationDispatched(EventEnvelope):
    """The supervisor accepted a keyed preparation worker."""

    action_id: ActionId
    unit: WorkerUnit

    def __post_init__(self) -> None:
        _validate_dispatched_event(self.action_id, self.unit, ActionKind.PREPARATION)


@dataclass(frozen=True, slots=True)
class PreparationFinished(EventEnvelope):
    """A keyed preparation worker reached a terminal result."""

    action_id: ActionId
    outcome: WorkerOutcome
    exit_status: int | None
    plan_hash: PlanHash

    def __post_init__(self) -> None:
        _validate_finished_event(self.action_id, ActionKind.PREPARATION)


@dataclass(frozen=True, slots=True)
class FinalizationDispatched(EventEnvelope):
    """The supervisor accepted a keyed finalization worker."""

    action_id: ActionId
    unit: WorkerUnit

    def __post_init__(self) -> None:
        _validate_dispatched_event(self.action_id, self.unit, ActionKind.FINALIZATION)


@dataclass(frozen=True, slots=True)
class FinalizationFinished(EventEnvelope):
    """A keyed finalization worker reached a terminal result."""

    action_id: ActionId
    outcome: WorkerOutcome
    exit_status: int | None

    def __post_init__(self) -> None:
        _validate_finished_event(self.action_id, ActionKind.FINALIZATION)


@dataclass(frozen=True, slots=True)
class DispatchRejected(EventEnvelope):
    """Writing or launching an admitted action was rejected."""

    action_id: ActionId
    reason: str

    def __post_init__(self) -> None:
        _require_nonempty(self.reason, "dispatch rejection reason")


@dataclass(frozen=True, slots=True)
class WorkerStatusUnknown(EventEnvelope):
    """Supervisor truth for an acknowledged worker is indeterminate."""

    action_id: ActionId
    reason: str

    def __post_init__(self) -> None:
        _require_nonempty(self.reason, "unknown worker reason")


@dataclass(frozen=True, slots=True)
class WorkerTimedOut(EventEnvelope):
    """A controller deadline or exact manager timeout became terminal evidence."""

    action_id: ActionId
    deadline_ms: int
    manager_confirmed: bool = False

    def __post_init__(self) -> None:
        _require_nonnegative(self.deadline_ms, "worker deadline")
        if (
            not self.manager_confirmed
            and self.deadline_ms > self.metadata.processed_at_ms
        ):
            msg = "worker deadline cannot be later than event processing time"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WorkerCancellationAcknowledged(EventEnvelope):
    """Manager inactivity and the exact terminal transaction result agree."""

    action_id: ActionId
    terminal_lifecycle: ActionLifecycle
    exit_status: int

    def __post_init__(self) -> None:
        if self.terminal_lifecycle not in TERMINAL_ACTION_LIFECYCLES:
            msg = "cancellation reconciliation requires a terminal lifecycle"
            raise ValueError(msg)
        if not 0 <= self.exit_status <= _MAX_EXIT_STATUS:
            msg = "cancellation reconciliation exit status is outside 0..255"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is ActionLifecycle.COMPLETED
            and self.exit_status != 0
        ):
            msg = "completed cancellation reconciliation requires status zero"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is not ActionLifecycle.COMPLETED
            and self.exit_status == 0
        ):
            msg = "non-completed cancellation reconciliation requires non-zero status"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ControllerStarted(EventEnvelope):
    """A fresh serialized controller authority started."""

    controller_instance: ControllerInstanceId


@dataclass(frozen=True, slots=True)
class BootChanged(EventEnvelope):
    """The boot identity changed, invalidating monotonic waits."""

    previous_boot_id: BootId

    def __post_init__(self) -> None:
        if self.previous_boot_id == self.metadata.boot_id:
            msg = "boot change requires distinct boot IDs"
            raise ValueError(msg)


def _validate_dispatched_event(
    action_id: ActionId, unit: WorkerUnit, kind: ActionKind
) -> None:
    if action_id.kind is not kind:
        msg = f"dispatch action ID must have kind {kind.value}"
        raise ValueError(msg)
    _validate_action_unit(action_id, unit)


def _validate_finished_event(action_id: ActionId, kind: ActionKind) -> None:
    if action_id.kind is not kind:
        msg = f"completion action ID must have kind {kind.value}"
        raise ValueError(msg)


type Event = (
    ObservationCompleted
    | ObservationFailed
    | PlanRequested
    | PlanCompleted
    | PlanFailed
    | TimerFired
    | DrmHintReceived
    | AdmissionDirtied
    | ProbeDispatched
    | ProbeFinished
    | ApplicationDispatched
    | ApplicationFinished
    | PreparationDispatched
    | PreparationFinished
    | FinalizationDispatched
    | FinalizationFinished
    | DispatchRejected
    | WorkerStatusUnknown
    | WorkerTimedOut
    | WorkerCancellationAcknowledged
    | ControllerStarted
    | BootChanged
)

EVENT_TYPES: tuple[type[EventEnvelope], ...] = (
    ObservationCompleted,
    ObservationFailed,
    PlanRequested,
    PlanCompleted,
    PlanFailed,
    TimerFired,
    DrmHintReceived,
    AdmissionDirtied,
    ProbeDispatched,
    ProbeFinished,
    ApplicationDispatched,
    ApplicationFinished,
    PreparationDispatched,
    PreparationFinished,
    FinalizationDispatched,
    FinalizationFinished,
    DispatchRejected,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerCancellationAcknowledged,
    ControllerStarted,
    BootChanged,
)


# Symbolic reducer outputs. Adapters, never these values, perform I/O.
@dataclass(frozen=True, slots=True)
class RequestObservation:
    """Request one canonical observation."""

    reason: WakeReason


@dataclass(frozen=True, slots=True)
class Schedule:
    """Persist a monotonic wake deadline."""

    deadline_ms: int

    def __post_init__(self) -> None:
        _require_nonnegative(self.deadline_ms, "schedule deadline")


@dataclass(frozen=True, slots=True)
class RequestPlan:
    """Compute and stage a pure desktop plan."""

    action_id: ActionId
    transition_id: TransitionId
    input_key: PlanningInputKey
    profile: str

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "plan effect action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "plan effect profile")


@dataclass(frozen=True, slots=True)
class ActivateProbe:
    """Activate only one admitted preferred mode on one external output."""

    action_id: ActionId
    key: ProbeAttemptKey
    output: str
    internal_output: str
    preferred_mode: str
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PROBE:
            msg = "probe effect action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.output, "probe effect output")
        _require_nonempty(self.internal_output, "probe effect internal output")
        _require_nonempty(self.preferred_mode, "probe effect preferred mode")


@dataclass(frozen=True, slots=True)
class ApplyProfile:
    """Explicitly apply one immutable remapped autorandr profile."""

    action_id: ActionId
    key: ApplicationAttemptKey
    profile: str
    mapping: MappingProof
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.APPLICATION:
            msg = "application effect action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "application effect profile")
        if self.mapping.profile != self.profile or self.key.profile != self.profile:
            msg = "application effect profiles must match"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PrepareDesktop:
    """Apply repeatable desktop preparation from an immutable plan."""

    action_id: ActionId
    transition_id: TransitionId
    transition_key: TransitionKey
    profile: str
    plan_hash: PlanHash
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PREPARATION:
            msg = "preparation effect action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "preparation effect profile")


@dataclass(frozen=True, slots=True)
class FinalizeDesktop:
    """Commit disruptive desktop work from the matching prepared plan."""

    action_id: ActionId
    transition_id: TransitionId
    transition_key: TransitionKey
    profile: str
    plan_hash: PlanHash
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.FINALIZATION:
            msg = "finalization effect action ID has the wrong kind"
            raise ValueError(msg)
        _require_nonempty(self.profile, "finalization effect profile")


@dataclass(frozen=True, slots=True)
class StopAction:
    """Request keyed, idempotent cancellation of one worker."""

    action_id: ActionId


@dataclass(frozen=True, slots=True)
class DiscardPlan:
    """Remove stale transaction-local staged plan artifacts."""

    action_id: ActionId
    plan_hash: PlanHash | None

    def __post_init__(self) -> None:
        if self.action_id.kind is not ActionKind.PLAN:
            msg = "discard-plan action ID has the wrong kind"
            raise ValueError(msg)


type Effect = (
    RequestObservation
    | Schedule
    | RequestPlan
    | ActivateProbe
    | ApplyProfile
    | PrepareDesktop
    | FinalizeDesktop
    | StopAction
    | DiscardPlan
)

EFFECT_TYPES: tuple[type[object], ...] = (
    RequestObservation,
    Schedule,
    RequestPlan,
    ActivateProbe,
    ApplyProfile,
    PrepareDesktop,
    FinalizeDesktop,
    StopAction,
    DiscardPlan,
)


@dataclass(frozen=True, slots=True)
class Decision:
    """Pure reducer result."""

    state: State
    effects: tuple[Effect, ...] = ()
