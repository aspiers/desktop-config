"""Typed autorandr command/profile parsing and strict output bijections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ..model import (  # noqa: TID252
    ConfigurationContentHash,
    Fingerprint,
    OutputMapping,
    ProfileScope,
    RawEvidenceReference,
    RawEvidenceSource,
)
from .evidence import (
    MAX_PARSE_ISSUES,
    IssueCollector,
    ParseIssue,
    ParseIssueCode,
    TextCommandEvidence,
    bounded_lines,
)

MAX_PROFILE_OUTPUTS: int = 128
MAX_PROFILE_NAME_CHARS: int = 255
MAX_OUTPUT_NAME_CHARS: int = 128
MAX_FINGERPRINT_CHARS: int = 65536
MAX_LAYOUT_CHARS: int = 255
FINGERPRINT_FIELDS: int = 2
KEY_VALUE_FIELDS: int = 2
MAX_MAPPING_SOLUTIONS: int = 2

_OUTPUT_NAME = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_UNAVAILABLE_FINGERPRINT_PREFIX = "--CONNECTED-BUT-EDID-UNAVAILABLE--"


@dataclass(frozen=True, slots=True)
class AutorandrFingerprintResult:
    """Parsed documented ``autorandr --fingerprint`` machine output."""

    fingerprints: tuple[Fingerprint, ...]
    raw_evidence: RawEvidenceReference
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether every fingerprint line was accepted."""
        return not self.issues


@dataclass(frozen=True, slots=True)
class AutorandrProfileSetResult:
    """Parsed detected or current profile-name set."""

    profiles: tuple[str, ...]
    raw_evidence: RawEvidenceReference
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether every profile-name line was accepted."""
        return not self.issues


@dataclass(frozen=True, slots=True)
class AutorandrObservation:
    """Fingerprint, detected, and current evidence from injected commands."""

    fingerprints: tuple[Fingerprint, ...]
    detected_profiles: tuple[str, ...]
    current_profiles: tuple[str, ...]
    raw_evidence: tuple[RawEvidenceReference, ...]
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether all three documented command outputs parsed."""
        return not self.issues


class AutorandrEvidenceSource(Protocol):
    """Injected interface for documented, read-only autorandr commands."""

    def fingerprint(self) -> TextCommandEvidence:
        """Return captured ``autorandr --fingerprint`` output."""
        ...

    def detected(self) -> TextCommandEvidence:
        """Return captured ``autorandr --detected`` output."""
        ...

    def current(self) -> TextCommandEvidence:
        """Return captured ``autorandr --current`` output."""
        ...


@dataclass(frozen=True, slots=True)
class AutorandrConfigOutput:
    """One output block from an autorandr profile ``config`` file."""

    output: str
    options: tuple[tuple[str, str | None], ...]

    @property
    def active(self) -> bool:
        """Return whether the saved profile configures this output on."""
        return all(name != "off" for name, _value in self.options)

    @property
    def primary(self) -> bool:
        """Return whether the output carries the saved primary marker."""
        return any(name == "primary" for name, _value in self.options)

    @property
    def mode(self) -> str | None:
        """Return the saved mode, if this output is active."""
        return next(
            (value for name, value in self.options if name == "mode"),
            None,
        )


@dataclass(frozen=True, slots=True)
class SavedAutorandrProfile:
    """Strict saved profile identity, topology, layout, and configuration."""

    name: str
    setup: tuple[Fingerprint, ...]
    config: tuple[AutorandrConfigOutput, ...]
    layout: str
    scope: ProfileScope
    configuration_hashes: tuple[ConfigurationContentHash, ...]

    @property
    def active_outputs(self) -> tuple[str, ...]:
        """Return active saved outputs which participate in setup identity."""
        setup_outputs = {item.output for item in self.setup}
        return tuple(
            item.output
            for item in self.config
            if item.output in setup_outputs and item.active
        )


@dataclass(frozen=True, slots=True)
class SavedProfileResult:
    """Bounded parse result for required config/setup and optional layout files."""

    profile: SavedAutorandrProfile | None
    raw_evidence: tuple[RawEvidenceReference, ...]
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether a complete strict profile was produced."""
        return self.profile is not None and not self.issues


@dataclass(frozen=True, slots=True)
class MappingResult:
    """A unique complete mapping, or bounded reasons for rejecting it."""

    mapping: tuple[OutputMapping, ...] | None
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether exactly one complete bijection was proven."""
        return self.mapping is not None and not self.issues


def parse_autorandr_fingerprint(
    evidence: TextCommandEvidence,
) -> AutorandrFingerprintResult:
    """Parse ``autorandr --fingerprint`` using installed ``setup`` grammar."""
    collector = IssueCollector(RawEvidenceSource.AUTORANDR_FINGERPRINT)
    lines = bounded_lines(
        evidence,
        RawEvidenceSource.AUTORANDR_FINGERPRINT,
        collector,
    )
    fingerprints = _parse_fingerprint_lines(lines, collector, allow_wildcard=False)
    return AutorandrFingerprintResult(
        fingerprints,
        evidence.raw_reference,
        collector.issues,
    )


def parse_detected_profiles(
    evidence: TextCommandEvidence,
) -> AutorandrProfileSetResult:
    """Parse line-oriented ``autorandr --detected`` profile names."""
    return _parse_profile_set(evidence)


def parse_current_profiles(
    evidence: TextCommandEvidence,
) -> AutorandrProfileSetResult:
    """Parse line-oriented ``autorandr --current`` profile names."""
    return _parse_profile_set(evidence)


def sample_autorandr(source: AutorandrEvidenceSource) -> AutorandrObservation:
    """Sample only injected command results and combine their bounded evidence."""
    fingerprints = parse_autorandr_fingerprint(source.fingerprint())
    detected = parse_detected_profiles(source.detected())
    current = parse_current_profiles(source.current())
    issues = (*fingerprints.issues, *detected.issues, *current.issues)
    raw = tuple(
        sorted(
            (
                fingerprints.raw_evidence,
                detected.raw_evidence,
                current.raw_evidence,
            ),
            key=lambda item: (item.source.value, item.reference),
        )
    )
    return AutorandrObservation(
        fingerprints=fingerprints.fingerprints,
        detected_profiles=detected.profiles,
        current_profiles=current.profiles,
        raw_evidence=raw,
        issues=issues[:MAX_PARSE_ISSUES],
    )


def parse_saved_profile(
    name: str,
    config_evidence: TextCommandEvidence,
    setup_evidence: TextCommandEvidence,
    layout_evidence: TextCommandEvidence | None = None,
) -> SavedProfileResult:
    """Parse strict installed autorandr profile files without filesystem defaults."""
    collector = IssueCollector(RawEvidenceSource.AUTORANDR_PROFILES)
    if not _valid_profile_name(name):
        collector.add(ParseIssueCode.MALFORMED_LINE, "profile name is malformed")

    config_lines = bounded_lines(
        config_evidence,
        RawEvidenceSource.AUTORANDR_PROFILES,
        collector,
    )
    setup_lines = bounded_lines(
        setup_evidence,
        RawEvidenceSource.AUTORANDR_PROFILES,
        collector,
    )
    layout_lines = (
        bounded_lines(
            layout_evidence,
            RawEvidenceSource.AUTORANDR_PROFILES,
            collector,
        )
        if layout_evidence is not None
        else None
    )
    config = _parse_config(config_lines, collector)
    setup = _parse_fingerprint_lines(setup_lines, collector, allow_wildcard=True)
    layout = _parse_layout(name, layout_lines, collector)

    config_by_output = {item.output: item for item in config}
    setup_outputs = {item.output for item in setup}
    for item in setup:
        if item.output not in config_by_output:
            collector.add(
                ParseIssueCode.INCONSISTENT,
                "setup output is absent from profile config",
            )
    for item in config:
        if item.active and item.output not in setup_outputs:
            collector.add(
                ParseIssueCode.INCONSISTENT,
                "active config output lacks a setup fingerprint",
            )
    if sum(item.primary for item in config if item.active) > 1:
        collector.add(
            ParseIssueCode.INCONSISTENT,
            "profile has multiple primary outputs",
        )

    references = [config_evidence.raw_reference, setup_evidence.raw_reference]
    if layout_evidence is not None:
        references.append(layout_evidence.raw_reference)
    references.sort(key=lambda item: (item.source.value, item.reference))

    profile: SavedAutorandrProfile | None = None
    if not collector.issues and config and setup and layout is not None:
        hashes = tuple(
            ConfigurationContentHash(reference.reference, reference.sha256)
            for reference in references
        )
        profile = SavedAutorandrProfile(
            name=name,
            setup=setup,
            config=config,
            layout=layout,
            scope=_profile_scope(setup),
            configuration_hashes=hashes,
        )
    return SavedProfileResult(profile, tuple(references), collector.issues)


def fingerprint_matches(saved_pattern: str, live_fingerprint: str) -> bool:
    """Match the installed documented single-``*`` setup fingerprint grammar."""
    if saved_pattern.count("*") > 1:
        msg = "saved fingerprint supports at most one wildcard"
        raise ValueError(msg)
    if "*" in live_fingerprint:
        msg = "live fingerprint cannot contain a wildcard"
        raise ValueError(msg)
    if "*" not in saved_pattern:
        return saved_pattern == live_fingerprint
    prefix, suffix = saved_pattern.split("*", maxsplit=1)
    return live_fingerprint.startswith(prefix) and live_fingerprint.endswith(suffix)


def resolve_output_mapping(  # noqa: C901
    profile: SavedAutorandrProfile,
    live_fingerprints: tuple[Fingerprint, ...],
    connected_outputs: tuple[str, ...],
) -> MappingResult:
    """Require one complete unique saved-to-live fingerprint bijection."""
    collector = IssueCollector(RawEvidenceSource.AUTORANDR_PROFILES)
    if len(set(connected_outputs)) != len(connected_outputs):
        collector.add(ParseIssueCode.DUPLICATE, "connected output list has duplicates")
    live_outputs = tuple(item.output for item in live_fingerprints)
    if len(set(live_outputs)) != len(live_outputs):
        collector.add(ParseIssueCode.DUPLICATE, "live fingerprint outputs collide")
    if set(live_outputs) != set(connected_outputs):
        collector.add(
            ParseIssueCode.TOPOLOGY_MISMATCH,
            "live fingerprint outputs do not equal the exact connected topology",
        )
    if len(profile.setup) != len(connected_outputs):
        collector.add(
            ParseIssueCode.TOPOLOGY_MISMATCH,
            "saved and connected output counts differ",
        )
    live_values = tuple(item.value for item in live_fingerprints)
    saved_values = tuple(item.value for item in profile.setup)
    if len(set(live_values)) != len(live_values) or len(set(saved_values)) != len(
        saved_values
    ):
        collector.add(
            ParseIssueCode.COLLISION,
            "duplicate fingerprints cannot prove connector identity",
        )
    if collector.issues:
        return MappingResult(None, collector.issues)

    candidates: dict[str, tuple[str, ...]] = {}
    for saved in profile.setup:
        try:
            matches = tuple(
                live.output
                for live in live_fingerprints
                if fingerprint_matches(saved.value, live.value)
            )
        except ValueError:
            collector.add(
                ParseIssueCode.UNSUPPORTED_WILDCARD,
                "saved fingerprint contains unsupported wildcard syntax",
            )
            return MappingResult(None, collector.issues)
        if not matches:
            collector.add(
                ParseIssueCode.UNMATCHED,
                "saved fingerprint has no live output match",
            )
        candidates[saved.output] = tuple(sorted(matches))
    if collector.issues:
        return MappingResult(None, collector.issues)

    ordered_saved = tuple(
        sorted(candidates, key=lambda output: (len(candidates[output]), output))
    )
    solutions: list[dict[str, str]] = []

    def search(index: int, used: frozenset[str], mapping: dict[str, str]) -> None:
        if len(solutions) >= MAX_MAPPING_SOLUTIONS:
            return
        if index == len(ordered_saved):
            if len(used) == len(connected_outputs):
                solutions.append(mapping.copy())
            return
        saved_output = ordered_saved[index]
        for live_output in candidates[saved_output]:
            if live_output in used:
                continue
            mapping[saved_output] = live_output
            search(index + 1, used | {live_output}, mapping)
            del mapping[saved_output]

    search(0, frozenset(), {})
    if not solutions:
        collector.add(
            ParseIssueCode.UNMATCHED,
            "candidate edges do not form a complete bijection",
        )
        return MappingResult(None, collector.issues)
    if len(solutions) > 1:
        collector.add(
            ParseIssueCode.AMBIGUOUS,
            "multiple complete fingerprint bijections exist",
        )
        return MappingResult(None, collector.issues)
    mapping = tuple(
        OutputMapping(saved_output, solutions[0][saved_output])
        for saved_output in sorted(solutions[0])
    )
    return MappingResult(mapping, ())


def _parse_profile_set(evidence: TextCommandEvidence) -> AutorandrProfileSetResult:
    collector = IssueCollector(RawEvidenceSource.AUTORANDR_PROFILES)
    lines = bounded_lines(
        evidence,
        RawEvidenceSource.AUTORANDR_PROFILES,
        collector,
    )
    profiles: set[str] = set()
    if lines is not None:
        for line_number, line in enumerate(lines, start=1):
            if not line:
                continue
            if not _valid_profile_name(line):
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "autorandr profile-set line is not a profile name",
                    line_number,
                )
                continue
            if line in profiles:
                collector.add(
                    ParseIssueCode.DUPLICATE,
                    "duplicate autorandr profile name",
                    line_number,
                )
                continue
            profiles.add(line)
    return AutorandrProfileSetResult(
        tuple(sorted(profiles)),
        evidence.raw_reference,
        collector.issues,
    )


def _parse_fingerprint_lines(  # noqa: C901
    lines: tuple[str, ...] | None,
    collector: IssueCollector,
    *,
    allow_wildcard: bool,
) -> tuple[Fingerprint, ...]:
    fingerprints: list[Fingerprint] = []
    outputs: set[str] = set()
    if lines is None:
        return ()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != FINGERPRINT_FIELDS:
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "fingerprint line must contain exactly output and fingerprint",
                line_number,
            )
            continue
        output, value = parts
        if not _valid_output_name(output):
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "fingerprint output name is malformed",
                line_number,
            )
            continue
        if output in outputs:
            collector.add(
                ParseIssueCode.DUPLICATE,
                "duplicate fingerprint output",
                line_number,
            )
            continue
        if not value.isascii() or len(value) > MAX_FINGERPRINT_CHARS:
            collector.add(
                ParseIssueCode.TOO_LARGE,
                "fingerprint value is non-ASCII or exceeds its limit",
                line_number,
            )
            continue
        wildcard_count = value.count("*")
        if wildcard_count > (1 if allow_wildcard else 0):
            collector.add(
                ParseIssueCode.UNSUPPORTED_WILDCARD,
                "fingerprint wildcard syntax is unsupported",
                line_number,
            )
            continue
        if not _valid_fingerprint_value(value):
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "fingerprint value is not EDID/setup syntax",
                line_number,
            )
            continue
        if len(fingerprints) >= MAX_PROFILE_OUTPUTS:
            collector.add(
                ParseIssueCode.TOO_LARGE,
                f"fingerprints exceed the {MAX_PROFILE_OUTPUTS}-output limit",
                line_number,
            )
            break
        outputs.add(output)
        fingerprints.append(Fingerprint(output, value))
    if not fingerprints:
        collector.add(ParseIssueCode.MISSING_REQUIRED, "no fingerprints found")
    fingerprints.sort(key=lambda item: item.output)
    return tuple(fingerprints)


def _parse_config(  # noqa: C901
    lines: tuple[str, ...] | None,
    collector: IssueCollector,
) -> tuple[AutorandrConfigOutput, ...]:
    if lines is None:
        return ()
    blocks: list[AutorandrConfigOutput] = []
    current_output: str | None = None
    current_options: list[tuple[str, str | None]] = []
    output_names: set[str] = set()

    def finish_block(line_number: int | None = None) -> None:
        nonlocal current_output, current_options
        if current_output is None:
            return
        names = tuple(name for name, _value in current_options)
        if len(set(names)) != len(names):
            collector.add(
                ParseIssueCode.DUPLICATE,
                "profile output block repeats an option",
                line_number,
            )
        active = "off" not in names
        mode = next(
            (value for name, value in current_options if name == "mode"),
            None,
        )
        if active and mode is None:
            collector.add(
                ParseIssueCode.MISSING_REQUIRED,
                "active profile output lacks a mode",
                line_number,
            )
        if not active and any(name in {"mode", "primary"} for name in names):
            collector.add(
                ParseIssueCode.INCONSISTENT,
                "off profile output carries mode or primary",
                line_number,
            )
        blocks.append(
            AutorandrConfigOutput(
                current_output,
                tuple(sorted(current_options, key=lambda item: item[0])),
            )
        )
        current_output = None
        current_options = []

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key_value = stripped.split(maxsplit=1)
        key = key_value[0]
        value = key_value[1] if len(key_value) == KEY_VALUE_FIELDS else None
        if key == "output":
            finish_block(line_number)
            if value is None or not _valid_output_name(value):
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "profile output directive is malformed",
                    line_number,
                )
                continue
            if value in output_names:
                collector.add(
                    ParseIssueCode.DUPLICATE,
                    "duplicate profile output block",
                    line_number,
                )
            output_names.add(value)
            current_output = value
            continue
        if current_output is None:
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "profile option appears before an output directive",
                line_number,
            )
            continue
        if key in {"mode", "crtc", "pos", "rate"} and value is None:
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "profile option requires a value",
                line_number,
            )
        if key in {"off", "primary"} and value is not None:
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "flag profile option cannot carry a value",
                line_number,
            )
        current_options.append((key, value))
    finish_block()
    if not blocks:
        collector.add(ParseIssueCode.MISSING_REQUIRED, "profile config has no outputs")
    if len(blocks) > MAX_PROFILE_OUTPUTS:
        collector.add(
            ParseIssueCode.TOO_LARGE,
            f"profile config exceeds the {MAX_PROFILE_OUTPUTS}-output limit",
        )
    blocks.sort(key=lambda item: item.output)
    return tuple(blocks[:MAX_PROFILE_OUTPUTS])


def _parse_layout(
    profile_name: str,
    lines: tuple[str, ...] | None,
    collector: IssueCollector,
) -> str | None:
    if lines is None:
        return profile_name
    values = tuple(line.strip() for line in lines if line.strip())
    if len(values) != 1 or len(values[0]) > MAX_LAYOUT_CHARS:
        collector.add(
            ParseIssueCode.MALFORMED_LINE,
            "optional layout file must contain one bounded non-empty line",
        )
        return None
    return values[0]


def _profile_scope(setup: tuple[Fingerprint, ...]) -> ProfileScope:
    internal = tuple(item for item in setup if _is_internal_output(item.output))
    if len(internal) == len(setup):
        return ProfileScope.INTERNAL_ONLY
    if not internal:
        return ProfileScope.EXTERNAL_ONLY
    return ProfileScope.MIXED


def _is_internal_output(output: str) -> bool:
    family = output.split("-", maxsplit=1)[0].casefold()
    return family in {"edp", "lvds", "dsi"}


def _valid_output_name(value: str) -> bool:
    return bool(_OUTPUT_NAME.fullmatch(value)) and len(value) <= MAX_OUTPUT_NAME_CHARS


def _valid_profile_name(value: str) -> bool:
    return (
        len(value) <= MAX_PROFILE_NAME_CHARS
        and _PROFILE_NAME.fullmatch(value) is not None
    )


def _valid_fingerprint_value(value: str) -> bool:
    if value.startswith(_UNAVAILABLE_FINGERPRINT_PREFIX):
        output = value.removeprefix(_UNAVAILABLE_FINGERPRINT_PREFIX)
        return _valid_output_name(output)
    return all(character in "0123456789abcdefABCDEF*" for character in value)
