"""Canonical, generation-fenced monitor observation coordination."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Protocol

from monitor_controller.observer.xrandr import (
    XConnectionState,
    XrandrEvidenceSource,
    XrandrOutput,
    XrandrSnapshot,
    sample_xrandr,
)

from ..model import (  # noqa: TID252
    BROKEN_EXTENSION_EDID_INTEGRITIES,
    BaseIdentityMatch,
    BootId,
    CanonicalObservation,
    ConnectorIdentityEvidence,
    EdidEvidence,
    EdidIntegrity,
    EventGeneration,
    Fingerprint,
    ObservationGeneration,
    ObservationInvalidityReason,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    ProbeCandidate,
    ProfileMatch,
    RawEvidenceReference,
    RawEvidenceSource,
)
from ..runtime.commands import (  # noqa: TID252
    MAX_COMMAND_TIMEOUT_SECONDS,
    CommandRequest,
    CommandRunner,
)
from .autorandr import (
    AutorandrEvidenceSource,
    AutorandrObservation,
    SavedAutorandrProfile,
    fingerprint_matches,
    resolve_output_mapping,
    sample_autorandr,
)
from .drm import (
    ConnectorKind,
    ConnectorStatus,
    DrmConnector,
    DrmSnapshot,
    EvidenceState,
    ReadOnlyTree,
    parse_edid,
    sample_drm,
)
from .evidence import ParseIssue, ParseIssueCode, TextCommandEvidence
from .topology import derive_canonical_topology

DEFAULT_OBSERVER_TIMEOUT_SECONDS: float = 5.0
EDID_BASE_HEX_CHARS: int = 256
MAX_MAPPING_SOLUTIONS: int = 2
ZERO_OBSERVATION_GENERATION = ObservationGeneration(0)
MAX_PLANNING_CAPTURES = 16


class BootIdSource(Protocol):
    """Injected source for the current kernel boot identity."""

    def current_boot_id(self) -> BootId:
        """Return the boot identity for this sample."""
        ...


class MonotonicClock(Protocol):
    """Injected monotonic millisecond clock."""

    def monotonic_ms(self) -> int:
        """Return non-negative milliseconds on the current boot."""
        ...


class EventGenerationSource(Protocol):
    """Injected udev/event generation fence."""

    def current_generation(self) -> EventGeneration:
        """Return the latest generation-sensitive input sequence."""
        ...


class SavedProfileSource(Protocol):
    """Injected immutable saved autorandr profile collection."""

    def saved_profiles(self) -> tuple[SavedAutorandrProfile, ...]:
        """Return every profile available to identity classification."""
        ...


@dataclass(frozen=True, slots=True)
class CanonicalPlanningCapture:
    """Canonical observation paired with its exact parsed XRandR snapshot."""

    observation: CanonicalObservation
    xrandr: XrandrSnapshot


@dataclass(frozen=True, slots=True)
class StaticSavedProfiles:
    """Simple immutable saved-profile source for runtime wiring and fixtures."""

    profiles: tuple[SavedAutorandrProfile, ...]

    def saved_profiles(self) -> tuple[SavedAutorandrProfile, ...]:
        """Return profiles in deterministic name order."""
        return tuple(sorted(self.profiles, key=lambda item: item.name))


@dataclass(frozen=True, slots=True)
class ObserverCommands:
    """Documented command arrays used by the canonical observer."""

    xrandr_query: tuple[str, ...] = ("xrandr", "--query")
    xrandr_properties: tuple[str, ...] = ("xrandr", "--props")
    autorandr_fingerprint: tuple[str, ...] = ("autorandr", "--fingerprint")
    autorandr_detected: tuple[str, ...] = ("autorandr", "--detected")
    autorandr_current: tuple[str, ...] = ("autorandr", "--current")


class _CommandXrandrSource(XrandrEvidenceSource):
    def __init__(
        self,
        runner: CommandRunner,
        commands: ObserverCommands,
        timeout_seconds: float,
    ) -> None:
        self._runner = runner
        self._commands = commands
        self._timeout = timeout_seconds

    def query(self) -> TextCommandEvidence:
        return self._run(
            self._commands.xrandr_query,
            RawEvidenceSource.XRANDR_QUERY,
            "command:xrandr --query",
        )

    def properties(self) -> TextCommandEvidence:
        return self._run(
            self._commands.xrandr_properties,
            RawEvidenceSource.XRANDR_PROPERTIES,
            "command:xrandr --props",
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        source: RawEvidenceSource,
        reference: str,
    ) -> TextCommandEvidence:
        return self._runner.run(
            CommandRequest(arguments, source, reference, self._timeout)
        )


class _CommandAutorandrSource(AutorandrEvidenceSource):
    def __init__(
        self,
        runner: CommandRunner,
        commands: ObserverCommands,
        timeout_seconds: float,
        environment: Mapping[str, str] | None,
    ) -> None:
        self._runner = runner
        self._commands = commands
        self._timeout = timeout_seconds
        self._environment = (
            None if environment is None else tuple(sorted(environment.items()))
        )

    def fingerprint(self) -> TextCommandEvidence:
        return self._run(
            self._commands.autorandr_fingerprint,
            RawEvidenceSource.AUTORANDR_FINGERPRINT,
            "command:autorandr --fingerprint",
        )

    def detected(self) -> TextCommandEvidence:
        return self._run(
            self._commands.autorandr_detected,
            RawEvidenceSource.AUTORANDR_PROFILES,
            "command:autorandr --detected",
        )

    def current(self) -> TextCommandEvidence:
        return self._run(
            self._commands.autorandr_current,
            RawEvidenceSource.AUTORANDR_PROFILES,
            "command:autorandr --current",
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        source: RawEvidenceSource,
        reference: str,
    ) -> TextCommandEvidence:
        return self._runner.run(
            CommandRequest(
                arguments,
                source,
                reference,
                self._timeout,
                self._environment,
            )
        )


DEFAULT_OBSERVER_COMMANDS = ObserverCommands()


@dataclass(frozen=True, slots=True)
class _CanonicalFacts:
    kernel_connected: tuple[str, ...]
    kernel_external: tuple[str, ...]
    x_connected: tuple[str, ...]
    x_active: tuple[str, ...]
    x_external: tuple[str, ...]
    connector_identities: tuple[ConnectorIdentityEvidence, ...]
    edid: tuple[EdidEvidence, ...]
    base_matches: tuple[BaseIdentityMatch, ...]
    eligible: tuple[ProfileMatch, ...]
    current: tuple[str, ...]
    exact: str | None
    probe: ProbeCandidate | None
    physical_token: PhysicalToken
    raw_evidence: tuple[RawEvidenceReference, ...]
    inconsistent: bool


class CanonicalSnapshotCoordinator:
    """Assemble one immutable snapshot without consulting implicit live sources."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        drm_tree: ReadOnlyTree,
        command_runner: CommandRunner,
        profiles: SavedProfileSource,
        boot_id_source: BootIdSource,
        clock: MonotonicClock,
        event_generation_source: EventGenerationSource,
        initial_observation_generation: ObservationGeneration = (
            ZERO_OBSERVATION_GENERATION
        ),
        commands: ObserverCommands = DEFAULT_OBSERVER_COMMANDS,
        command_timeout_seconds: float = DEFAULT_OBSERVER_TIMEOUT_SECONDS,
        autorandr_environment: Mapping[str, str] | None = None,
    ) -> None:
        """Bind every I/O authority explicitly and retain only a local sequence."""
        if not 0 < command_timeout_seconds <= MAX_COMMAND_TIMEOUT_SECONDS:
            msg = "observer command timeout must be positive and bounded"
            raise ValueError(msg)
        self._drm_tree = drm_tree
        self._profiles = profiles
        self._boot_id_source = boot_id_source
        self._clock = clock
        self._event_generation_source = event_generation_source
        self._observation_generation = initial_observation_generation
        self._planning_capture_lock = threading.Lock()
        self._planning_captures: dict[ObservationKey, CanonicalPlanningCapture] = {}
        self._xrandr_source = _CommandXrandrSource(
            command_runner, commands, command_timeout_seconds
        )
        self._autorandr_source = _CommandAutorandrSource(
            command_runner,
            commands,
            command_timeout_seconds,
            autorandr_environment,
        )

    def observe(self) -> CanonicalObservation:
        """Sample both fences, reject tears, and return normalized evidence."""
        begin_generation = self._event_generation_source.current_generation()
        boot_id = self._boot_id_source.current_boot_id()
        begin_drm = sample_drm(self._drm_tree)
        xrandr = sample_xrandr(self._xrandr_source)
        autorandr = sample_autorandr(self._autorandr_source)
        end_drm = sample_drm(self._drm_tree)
        end_generation = self._event_generation_source.current_generation()
        observed_at_ms = self._clock.monotonic_ms()
        self._observation_generation = ObservationGeneration(
            self._observation_generation.value + 1
        )
        profiles = self._normalized_profiles()
        facts = _derive_facts(begin_drm, end_drm, xrandr, autorandr, profiles)

        reason = _invalidity_reason(
            begin_generation,
            end_generation,
            begin_drm,
            end_drm,
            xrandr,
            autorandr,
            facts,
            profiles,
        )
        exact = facts.exact if reason is None else None
        probe = facts.probe if reason is None else None
        validity_value = "valid" if reason is None else "invalid"
        key_payload = _observation_key_payload(
            boot_id=boot_id,
            physical_token=facts.physical_token,
            facts=facts,
            xrandr=xrandr,
            live_fingerprints=autorandr.fingerprints,
            exact=exact,
            probe=probe,
            validity=validity_value,
            invalidity_reason=None if reason is None else reason.value,
        )
        observation = CanonicalObservation(
            observed_at_ms=observed_at_ms,
            observation_generation=self._observation_generation,
            boot_id=boot_id,
            physical_token=facts.physical_token,
            begin_event_generation=begin_generation,
            end_event_generation=end_generation,
            kernel_connected_outputs=facts.kernel_connected,
            kernel_external_outputs=facts.kernel_external,
            x_connected_outputs=facts.x_connected,
            x_active_outputs=facts.x_active,
            x_external_outputs=facts.x_external,
            connector_identities=facts.connector_identities,
            live_fingerprints=autorandr.fingerprints,
            base_identity_profiles=facts.base_matches,
            edid_integrity=facts.edid,
            probe_candidate=probe,
            eligible_profiles=facts.eligible,
            current_profiles=facts.current,
            exact_profile=exact,
            observation_key=ObservationKey(_digest(key_payload)),
            validity=(
                ObservationValidity.VALID
                if reason is None
                else ObservationValidity.INVALID
            ),
            invalidity_reason=reason,
            raw_evidence=facts.raw_evidence,
        )
        with self._planning_capture_lock:
            self._planning_captures[observation.observation_key] = (
                CanonicalPlanningCapture(observation, xrandr)
            )
            while len(self._planning_captures) > MAX_PLANNING_CAPTURES:
                oldest = next(iter(self._planning_captures))
                del self._planning_captures[oldest]
        return observation

    def planning_capture(self, key: ObservationKey) -> CanonicalPlanningCapture:
        """Return retained display evidence for one admitted observation key."""
        with self._planning_capture_lock:
            try:
                return self._planning_captures[key]
            except KeyError as error:
                msg = f"no retained planning capture for observation {key.value!r}"
                raise KeyError(msg) from error

    def _normalized_profiles(self) -> tuple[SavedAutorandrProfile, ...]:
        profiles = tuple(
            sorted(self._profiles.saved_profiles(), key=lambda item: item.name)
        )
        if len({item.name for item in profiles}) != len(profiles):
            msg = "saved profile source returned duplicate names"
            raise ValueError(msg)
        return profiles


SnapshotCoordinator = CanonicalSnapshotCoordinator


def _derive_facts(
    begin_drm: DrmSnapshot,
    drm: DrmSnapshot,
    xrandr: XrandrSnapshot,
    autorandr: AutorandrObservation,
    profiles: tuple[SavedAutorandrProfile, ...],
) -> _CanonicalFacts:
    connected_x = tuple(item for item in xrandr.outputs if item.connected)
    topology = derive_canonical_topology(drm, xrandr)
    translations = {
        item.kernel_connector: item.live_output for item in topology.translations
    }
    kernel_connected = topology.kernel_connected_outputs
    kernel_external = topology.kernel_external_outputs
    x_connected = topology.x_connected_outputs
    x_active = topology.x_active_outputs
    x_external = topology.x_external_outputs
    identities = topology.connector_identities
    edid = topology.edid_integrity
    base_matches = _base_identity_matches(profiles, drm.connectors, translations)
    eligible, mapping_inconsistent = _eligible_profiles(
        profiles, autorandr, x_connected
    )
    current = autorandr.current_profiles
    exact = _exact_profile(
        eligible,
        current,
        kernel_connected,
        kernel_external,
        x_connected,
        x_active,
        identities,
        edid,
        base_matches,
    )
    probe = _probe_candidate(
        profiles,
        eligible,
        autorandr.fingerprints,
        drm.connectors,
        translations,
        kernel_connected,
        kernel_external,
        x_connected,
        x_active,
        x_external,
        identities,
        edid,
        base_matches,
        connected_x,
    )
    raw = _raw_evidence(begin_drm, drm, autorandr, xrandr, profiles)
    known_profiles = {item.name for item in profiles}
    profile_inconsistent = bool(
        (set(autorandr.detected_profiles) | set(autorandr.current_profiles))
        - known_profiles
    ) or not set(autorandr.current_profiles) <= set(autorandr.detected_profiles)
    topology_inconsistent = set(kernel_connected) != set(x_connected)
    fingerprint_inconsistent = {item.output for item in autorandr.fingerprints} != set(
        x_connected
    )
    x_uncertain = any(
        item.connection is XConnectionState.UNKNOWN for item in xrandr.outputs
    )
    return _CanonicalFacts(
        kernel_connected=kernel_connected,
        kernel_external=kernel_external,
        x_connected=x_connected,
        x_active=x_active,
        x_external=x_external,
        connector_identities=identities,
        edid=edid,
        base_matches=base_matches,
        eligible=eligible,
        current=current,
        exact=exact,
        probe=probe,
        physical_token=topology.physical_token,
        raw_evidence=raw,
        inconsistent=(
            topology.inconsistent
            or mapping_inconsistent
            or profile_inconsistent
            or topology_inconsistent
            or fingerprint_inconsistent
            or x_uncertain
        ),
    )


def _base_identity_matches(
    profiles: tuple[SavedAutorandrProfile, ...],
    connectors: tuple[DrmConnector, ...],
    translations: dict[str, str],
) -> tuple[BaseIdentityMatch, ...]:
    live_hashes = {
        translations.get(item.kernel_name, item.output_name): item.edid.parsed.base_hash
        for item in connectors
        if item.connected
        and item.kind is not ConnectorKind.VIRTUAL
        and item.edid.parsed is not None
        and item.edid.parsed.base_hash is not None
    }
    matches: set[tuple[str, str]] = set()
    for profile in profiles:
        saved_hashes = tuple(
            value
            for fingerprint in profile.setup
            if (value := _saved_base_hash(fingerprint.value)) is not None
        )
        for output, live_hash in live_hashes.items():
            if sum(value == live_hash for value in saved_hashes) == 1:
                matches.add((profile.name, output))
    return tuple(BaseIdentityMatch(*item) for item in sorted(matches))


def _saved_base_hash(value: str) -> str | None:
    base_hex = value[:EDID_BASE_HEX_CHARS]
    if len(base_hex) != EDID_BASE_HEX_CHARS or any(
        character not in "0123456789abcdefABCDEF" for character in base_hex
    ):
        return None
    return parse_edid(bytes.fromhex(base_hex)).base_hash


def _eligible_profiles(
    profiles: tuple[SavedAutorandrProfile, ...],
    autorandr: AutorandrObservation,
    x_connected: tuple[str, ...],
) -> tuple[tuple[ProfileMatch, ...], bool]:
    by_name = {item.name: item for item in profiles}
    values: list[ProfileMatch] = []
    inconsistent = False
    for name in autorandr.detected_profiles:
        profile = by_name.get(name)
        if profile is None:
            inconsistent = True
            continue
        result = resolve_output_mapping(profile, autorandr.fingerprints, x_connected)
        if result.mapping is None:
            continue
        mapping_by_saved = {
            item.saved_output: item.live_output for item in result.mapping
        }
        active = tuple(
            sorted(mapping_by_saved[item] for item in profile.active_outputs)
        )
        values.append(
            ProfileMatch(
                profile=profile.name,
                scope=profile.scope,
                layout=profile.layout,
                mapping=result.mapping,
                active_outputs=active,
                configuration_hashes=profile.configuration_hashes,
            )
        )
    values.sort(key=_profile_match_key)
    return tuple(values), inconsistent


def _profile_match_key(item: ProfileMatch) -> str:
    return "\0".join(
        (
            item.profile,
            item.scope.value,
            item.layout,
            ";".join(
                f"{entry.saved_output}>{entry.live_output}" for entry in item.mapping
            ),
            ";".join(item.active_outputs),
            ";".join(
                f"{entry.path}={entry.sha256}" for entry in item.configuration_hashes
            ),
        )
    )


def _exact_profile(  # noqa: PLR0913, PLR0917
    eligible: tuple[ProfileMatch, ...],
    current: tuple[str, ...],
    kernel_connected: tuple[str, ...],
    kernel_external: tuple[str, ...],
    x_connected: tuple[str, ...],
    x_active: tuple[str, ...],
    identities: tuple[ConnectorIdentityEvidence, ...],
    edid: tuple[EdidEvidence, ...],
    base_matches: tuple[BaseIdentityMatch, ...],
) -> str | None:
    if len(eligible) != 1:
        return None
    target = eligible[0]
    mapped = {item.live_output for item in target.mapping}
    external = set(kernel_external)
    complete = {
        item.output for item in edid if item.integrity is EdidIntegrity.COMPLETE
    }
    identified = {item.output for item in identities if item.x_connector_id is not None}
    base = {item.output for item in base_matches if item.profile == target.profile}
    if (
        set(kernel_connected) == set(x_connected) == mapped
        and set(x_active) == set(target.active_outputs)
        and current == (target.profile,)
        and external <= complete & identified & base
    ):
        return target.profile
    return None


def _probe_candidate(  # noqa: PLR0911, PLR0913, PLR0917
    profiles: tuple[SavedAutorandrProfile, ...],
    eligible: tuple[ProfileMatch, ...],
    live_fingerprints: tuple[Fingerprint, ...],
    connectors: tuple[DrmConnector, ...],
    translations: dict[str, str],
    kernel_connected: tuple[str, ...],
    kernel_external: tuple[str, ...],
    x_connected: tuple[str, ...],
    x_active: tuple[str, ...],
    x_external: tuple[str, ...],
    identities: tuple[ConnectorIdentityEvidence, ...],
    edid: tuple[EdidEvidence, ...],
    base_matches: tuple[BaseIdentityMatch, ...],
    connected_x: tuple[XrandrOutput, ...],
) -> ProbeCandidate | None:
    if eligible or len(kernel_external) != 1:
        return None
    external = kernel_external[0]
    internal = tuple(
        translations.get(item.kernel_name, item.output_name)
        for item in connectors
        if item.connected and item.kind is ConnectorKind.INTERNAL
    )
    if (
        len(internal) != 1
        or not kernel_connected
        or set(kernel_connected) != set(x_connected)
        or set(x_external) != {external}
        or set(x_active) != {internal[0]}
        or external in x_active
    ):
        return None
    external_edid = next((item for item in edid if item.output == external), None)
    if (
        external_edid is None
        or external_edid.integrity not in BROKEN_EXTENSION_EDID_INTEGRITIES
    ):
        return None
    if not any(
        item.output == external and item.x_connector_id is not None
        for item in identities
    ):
        return None
    x_output = next((item for item in connected_x if item.name == external), None)
    if x_output is None or len(x_output.preferred_modes) != 1:
        return None
    matching_profiles = tuple(
        item.profile for item in base_matches if item.output == external
    )
    candidates: list[str] = []
    by_name = {item.name: item for item in profiles}
    for name in matching_profiles:
        profile = by_name[name]
        mapping = _resolve_probe_mapping(
            profile, live_fingerprints, connectors, translations, kernel_connected
        )
        if mapping is not None and len(profile.setup) == len(kernel_connected):
            candidates.append(name)
    if len(set(candidates)) != 1:
        return None
    return ProbeCandidate(
        profile=candidates[0],
        output=external,
        internal_output=internal[0],
        preferred_mode=x_output.preferred_modes[0],
    )


def _resolve_probe_mapping(  # noqa: C901
    profile: SavedAutorandrProfile,
    live_fingerprints: tuple[Fingerprint, ...],
    connectors: tuple[DrmConnector, ...],
    translations: dict[str, str],
    connected: tuple[str, ...],
) -> tuple[OutputMapping, ...] | None:
    live_by_output = {item.output: item.value for item in live_fingerprints}
    base_by_output = {
        translations.get(item.kernel_name, item.output_name): item.edid.parsed.base_hash
        for item in connectors
        if item.connected
        and item.edid.parsed is not None
        and item.edid.parsed.base_hash is not None
    }
    candidates: dict[str, tuple[str, ...]] = {}
    for saved in profile.setup:
        saved_base = _saved_base_hash(saved.value)
        matches: list[str] = []
        for output in connected:
            fingerprint_match = False
            if output in live_by_output:
                try:
                    fingerprint_match = fingerprint_matches(
                        saved.value, live_by_output[output]
                    )
                except ValueError:
                    return None
            if fingerprint_match or (
                saved_base is not None and saved_base == base_by_output.get(output)
            ):
                matches.append(output)
        if not matches:
            return None
        candidates[saved.output] = tuple(sorted(matches))
    solutions: list[dict[str, str]] = []
    ordered = tuple(sorted(candidates, key=lambda item: (len(candidates[item]), item)))

    def search(index: int, used: frozenset[str], value: dict[str, str]) -> None:
        if len(solutions) >= MAX_MAPPING_SOLUTIONS:
            return
        if index == len(ordered):
            if used == frozenset(connected):
                solutions.append(value.copy())
            return
        saved = ordered[index]
        for live in candidates[saved]:
            if live in used:
                continue
            value[saved] = live
            search(index + 1, used | {live}, value)
            del value[saved]

    search(0, frozenset(), {})
    if len(solutions) != 1:
        return None
    return tuple(
        OutputMapping(saved, solutions[0][saved]) for saved in sorted(solutions[0])
    )


def _invalidity_reason(  # noqa: PLR0913, PLR0917
    begin_generation: EventGeneration,
    end_generation: EventGeneration,
    begin_drm: DrmSnapshot,
    end_drm: DrmSnapshot,
    xrandr: XrandrSnapshot,
    autorandr: AutorandrObservation,
    facts: _CanonicalFacts,
    profiles: tuple[SavedAutorandrProfile, ...],
) -> ObservationInvalidityReason | None:
    del profiles
    if begin_generation != end_generation:
        return ObservationInvalidityReason.EVENT_GENERATION_CHANGED
    if begin_drm != end_drm:
        return ObservationInvalidityReason.TOPOLOGY_CHANGED
    issues = (*xrandr.issues, *autorandr.issues)
    if any(item.code is ParseIssueCode.COMMAND_TIMED_OUT for item in issues):
        return ObservationInvalidityReason.COMMAND_TIMED_OUT
    non_torn_issues = tuple(item for item in issues if not _is_xrandr_torn_issue(item))
    if non_torn_issues:
        return (
            ObservationInvalidityReason.INCONSISTENT_EVIDENCE
            if any(item.code is ParseIssueCode.INCONSISTENT for item in non_torn_issues)
            else ObservationInvalidityReason.PARSE_FAILED
        )
    if _xrandr_torn(xrandr.issues):
        return ObservationInvalidityReason.TOPOLOGY_CHANGED
    return (
        ObservationInvalidityReason.INCONSISTENT_EVIDENCE
        if not _drm_certain(end_drm) or facts.inconsistent
        else None
    )


def _is_xrandr_torn_issue(issue: ParseIssue) -> bool:
    return issue.code is ParseIssueCode.INCONSISTENT and issue.detail in {
        "query and properties output topologies differ",
        "query and properties mode lists or markers differ",
    }


def _xrandr_torn(issues: tuple[ParseIssue, ...]) -> bool:
    return any(_is_xrandr_torn_issue(item) for item in issues)


def _drm_certain(snapshot: DrmSnapshot) -> bool:
    return snapshot.scan_state is EvidenceState.AVAILABLE and all(
        item.kind is ConnectorKind.VIRTUAL
        or (
            item.status_state is EvidenceState.AVAILABLE
            and item.status is not ConnectorStatus.UNKNOWN
        )
        for item in snapshot.connectors
    )


def _raw_evidence(
    begin_drm: DrmSnapshot,
    end_drm: DrmSnapshot,
    autorandr: AutorandrObservation,
    xrandr: XrandrSnapshot,
    profiles: tuple[SavedAutorandrProfile, ...],
) -> tuple[RawEvidenceReference, ...]:
    values = [*autorandr.raw_evidence, *xrandr.raw_evidence]
    for boundary, drm in (("begin", begin_drm), ("end", end_drm)):
        connector_payload = [
            {
                "kernel_name": item.kernel_name,
                "output_name": item.output_name,
                "kind": item.kind.value,
                "status_state": item.status_state.value,
                "status": item.status.value,
                "connector_id_state": item.connector_id.state.value,
                "connector_id": item.connector_id.value,
            }
            for item in drm.connectors
        ]
        values.append(
            RawEvidenceReference(
                RawEvidenceSource.DRM_CONNECTORS,
                f"drm:{boundary}:connectors",
                _digest(connector_payload),
            )
        )
        for item in drm.connectors:
            if item.edid.raw is not None and item.edid.parsed is not None:
                digest = item.edid.parsed.raw_hash
            else:
                digest = _digest({"state": item.edid.state.value})
            values.append(
                RawEvidenceReference(
                    RawEvidenceSource.DRM_EDID,
                    f"drm:{boundary}:edid:{item.kernel_name}",
                    digest,
                )
            )
    values.extend(
        RawEvidenceReference(
            RawEvidenceSource.AUTORANDR_PROFILES,
            config.path,
            config.sha256,
        )
        for profile in profiles
        for config in profile.configuration_hashes
    )
    unique = {(item.source.value, item.reference): item for item in values}
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: f"{item.source.value}\0{item.reference}\0{item.sha256}",
        )
    )


def _observation_key_payload(  # noqa: PLR0913
    *,
    boot_id: BootId,
    physical_token: PhysicalToken,
    facts: _CanonicalFacts,
    xrandr: XrandrSnapshot,
    live_fingerprints: tuple[Fingerprint, ...],
    exact: str | None,
    probe: ProbeCandidate | None,
    validity: str,
    invalidity_reason: str | None,
) -> object:
    return {
        "boot_id": str(boot_id.value),
        "physical_token": physical_token.value,
        "kernel_connected_outputs": facts.kernel_connected,
        "kernel_external_outputs": facts.kernel_external,
        "x_connected_outputs": facts.x_connected,
        "x_active_outputs": facts.x_active,
        "x_external_outputs": facts.x_external,
        "x_geometry": [
            {
                "output": item.name,
                "width": item.geometry.width,
                "height": item.geometry.height,
                "x": item.geometry.x,
                "y": item.geometry.y,
                "primary": item.primary,
                "width_mm": item.width_mm,
                "height_mm": item.height_mm,
            }
            for item in xrandr.outputs
            if item.geometry is not None
        ],
        "connector_identities": [asdict(item) for item in facts.connector_identities],
        "live_fingerprints": [asdict(item) for item in live_fingerprints],
        "base_identity_profiles": [asdict(item) for item in facts.base_matches],
        "edid_integrity": [
            {
                "output": item.output,
                "integrity": item.integrity.value,
                "base_hash": item.base_hash,
            }
            for item in facts.edid
        ],
        "probe_candidate": None if probe is None else asdict(probe),
        "eligible_profiles": [
            {
                "profile": item.profile,
                "scope": item.scope.value,
                "layout": item.layout,
                "mapping": [asdict(mapping) for mapping in item.mapping],
                "active_outputs": item.active_outputs,
                "configuration_hashes": [
                    asdict(config) for config in item.configuration_hashes
                ],
            }
            for item in facts.eligible
        ],
        "current_profiles": facts.current,
        "exact_profile": exact,
        "validity": validity,
        "invalidity_reason": invalidity_reason,
        "raw_evidence": [
            {
                "source": item.source.value,
                "reference": item.reference,
                "sha256": item.sha256,
            }
            for item in facts.raw_evidence
        ],
    }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
