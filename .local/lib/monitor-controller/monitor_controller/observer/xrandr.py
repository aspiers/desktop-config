"""Typed, bounded parsing of injected XRandR query and property evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from ..model import RawEvidenceReference, RawEvidenceSource  # noqa: TID252
from .evidence import (
    MAX_PARSE_ISSUES,
    IssueCollector,
    ParseIssue,
    ParseIssueCode,
    TextCommandEvidence,
    bounded_lines,
)

MAX_OUTPUTS: int = 128
MAX_MODES_PER_OUTPUT: int = 512
MAX_OUTPUT_NAME_CHARS: int = 128
MAX_CONNECTOR_ID: int = (1 << 32) - 1
MIN_MODE_LINE_PARTS: int = 2
_MODE_INDENT = "   "
_PREFERRED_MARKER = "+"

_OUTPUT_HEADER = re.compile(
    r"^(?P<name>\S+)\s+(?P<state>connected|disconnected|unknown connection)"
    r"(?P<rest>(?:\s+.*)?)$"
)
_GEOMETRY = re.compile(
    r"(?<!\S)(?P<width>[0-9]+)x(?P<height>[0-9]+)"
    r"\+(?P<x>-?[0-9]+)\+(?P<y>-?[0-9]+)(?!\S)"
)
# ``xrandr --query`` renders current/preferred as two fixed columns after each
# rate.  A preferred-only rate consequently splits as ``60.00 +`` when generic
# whitespace tokenization removes the blank current column; current and
# current+preferred remain ``60.00*`` and ``60.00*+``.  Keep that grammar
# narrow: duplicated/reversed markers and a detached current marker are not
# valid XRandR output.
_RATE = re.compile(r"^(?P<rate>[0-9]+(?:\.[0-9]+)?)(?P<markers>\*?\+?)$")
_PROPERTY = re.compile(r"^[ \t]+(?P<name>[^:\n]+):[ \t]*(?P<value>.*?)[ \t]*$")


class XConnectionState(StrEnum):
    """Connection state reported by an XRandR output header."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class XrandrGeometry:
    """Active CRTC geometry copied from an output header."""

    width: int
    height: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            msg = "XRandR geometry dimensions must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class XrandrMode:
    """One advertised mode and all refresh markers shown by XRandR."""

    name: str
    rates: tuple[str, ...]
    current: bool
    preferred: bool

    def __post_init__(self) -> None:
        if not self.name or not self.rates:
            msg = "XRandR mode requires a name and at least one refresh rate"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class XrandrOutput:
    """Connected, active, primary, mode, and connector-ID X facts."""

    name: str
    connection: XConnectionState
    geometry: XrandrGeometry | None
    primary: bool
    modes: tuple[XrandrMode, ...]
    connector_id: int | None = None

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > MAX_OUTPUT_NAME_CHARS:
            msg = "XRandR output name must be bounded and non-empty"
            raise ValueError(msg)
        if self.connection is not XConnectionState.CONNECTED and (
            self.geometry is not None or self.primary or self.modes
        ):
            msg = "non-connected XRandR output cannot be active, primary, or have modes"
            raise ValueError(msg)
        if (
            self.connector_id is not None
            and not 0 <= self.connector_id <= MAX_CONNECTOR_ID
        ):
            msg = "XRandR connector ID is outside the unsigned 32-bit range"
            raise ValueError(msg)

    @property
    def connected(self) -> bool:
        """Return only positive X connection evidence."""
        return self.connection is XConnectionState.CONNECTED

    @property
    def active(self) -> bool:
        """Return whether XRandR reports active CRTC geometry."""
        return self.geometry is not None

    @property
    def current_modes(self) -> tuple[str, ...]:
        """Return mode names carrying at least one current marker."""
        return tuple(mode.name for mode in self.modes if mode.current)

    @property
    def preferred_modes(self) -> tuple[str, ...]:
        """Return mode names carrying at least one preferred marker."""
        return tuple(mode.name for mode in self.modes if mode.preferred)


@dataclass(frozen=True, slots=True)
class XrandrQuery:
    """Parsed result of exactly one injected ``xrandr --query`` call."""

    outputs: tuple[XrandrOutput, ...]
    raw_evidence: RawEvidenceReference
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether query evidence parsed without bounded errors."""
        return not self.issues


@dataclass(frozen=True, slots=True)
class XrandrPropertyOutput:
    """Topology, mode markers, and connector ID from one properties block."""

    name: str
    connection: XConnectionState
    geometry: XrandrGeometry | None
    primary: bool
    modes: tuple[XrandrMode, ...]
    connector_id: int | None

    @property
    def connected(self) -> bool:
        """Return only positive X connection evidence."""
        return self.connection is XConnectionState.CONNECTED

    @property
    def active(self) -> bool:
        """Return whether this properties block reports active geometry."""
        return self.geometry is not None

    @property
    def current_modes(self) -> tuple[str, ...]:
        """Return mode names carrying at least one current marker."""
        return tuple(mode.name for mode in self.modes if mode.current)

    @property
    def preferred_modes(self) -> tuple[str, ...]:
        """Return mode names carrying at least one preferred marker."""
        return tuple(mode.name for mode in self.modes if mode.preferred)


@dataclass(frozen=True, slots=True)
class XrandrProperties:
    """Parsed result of exactly one injected ``xrandr --props`` call."""

    outputs: tuple[XrandrPropertyOutput, ...]
    raw_evidence: RawEvidenceReference
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether property evidence parsed without bounded errors."""
        return not self.issues


@dataclass(frozen=True, slots=True)
class XrandrSnapshot:
    """One merged query/properties sample which never consults the live display."""

    outputs: tuple[XrandrOutput, ...]
    raw_evidence: tuple[RawEvidenceReference, ...]
    issues: tuple[ParseIssue, ...]

    @property
    def valid(self) -> bool:
        """Return whether both commands agree and parsed without error."""
        return not self.issues

    @property
    def connected_outputs(self) -> tuple[str, ...]:
        """Return all positively X-connected outputs."""
        return tuple(output.name for output in self.outputs if output.connected)

    @property
    def active_outputs(self) -> tuple[str, ...]:
        """Return all outputs with active geometry."""
        return tuple(output.name for output in self.outputs if output.active)

    @property
    def primary_output(self) -> str | None:
        """Return the sole primary output, if reported."""
        return next((output.name for output in self.outputs if output.primary), None)

    def connector_ids(self) -> tuple[tuple[str, int], ...]:
        """Return available connector IDs in deterministic output order."""
        return tuple(
            (output.name, output.connector_id)
            for output in self.outputs
            if output.connector_id is not None
        )


class XrandrEvidenceSource(Protocol):
    """Injected interface for documented, read-only XRandR commands."""

    def query(self) -> TextCommandEvidence:
        """Return a captured ``xrandr --query`` result."""
        ...

    def properties(self) -> TextCommandEvidence:
        """Return a captured ``xrandr --props`` result."""
        ...


def parse_xrandr_query(  # noqa: C901, PLR0912, PLR0915
    evidence: TextCommandEvidence,
) -> XrandrQuery:
    """Parse bounded ``xrandr --query`` text without running a command."""
    collector = IssueCollector(RawEvidenceSource.XRANDR_QUERY)
    lines = bounded_lines(evidence, RawEvidenceSource.XRANDR_QUERY, collector)
    outputs: list[XrandrOutput] = []
    current_index: int | None = None
    mode_names: set[str] = set()

    if lines is not None:
        for line_number, line in enumerate(lines, start=1):
            if not line.strip() or line.startswith("Screen "):
                continue
            header = _OUTPUT_HEADER.fullmatch(line)
            if header is not None:
                if len(outputs) >= MAX_OUTPUTS:
                    collector.add(
                        ParseIssueCode.TOO_LARGE,
                        f"XRandR query exceeds the {MAX_OUTPUTS}-output limit",
                        line_number,
                    )
                    current_index = None
                    continue
                name = header.group("name")
                if len(name) > MAX_OUTPUT_NAME_CHARS:
                    collector.add(
                        ParseIssueCode.TOO_LARGE,
                        "XRandR output name exceeds its limit",
                        line_number,
                    )
                    current_index = None
                    continue
                if any(output.name == name for output in outputs):
                    collector.add(
                        ParseIssueCode.DUPLICATE,
                        "duplicate XRandR output header",
                        line_number,
                    )
                    current_index = None
                    continue
                connection = _connection_state(header.group("state"))
                rest = header.group("rest")
                geometry_match = _GEOMETRY.search(rest)
                geometry = (
                    _parse_geometry(geometry_match, line_number, collector)
                    if geometry_match is not None
                    else None
                )
                primary = re.search(r"(?<!\S)primary(?!\S)", rest) is not None
                if connection is not XConnectionState.CONNECTED and (
                    geometry is not None or primary
                ):
                    collector.add(
                        ParseIssueCode.INCONSISTENT,
                        "non-connected output carries active or primary facts",
                        line_number,
                    )
                    geometry = None
                    primary = False
                outputs.append(XrandrOutput(name, connection, geometry, primary, ()))
                current_index = len(outputs) - 1
                mode_names = set()
                continue
            if current_index is None or not _has_mode_indentation(line):
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "line is not an XRandR output header or mode",
                    line_number,
                )
                continue
            output = outputs[current_index]
            if output.connection is not XConnectionState.CONNECTED:
                collector.add(
                    ParseIssueCode.INCONSISTENT,
                    "non-connected output has an indented mode line",
                    line_number,
                )
                continue
            mode = _parse_mode_line(line, line_number, collector)
            if mode is None:
                continue
            if len(output.modes) >= MAX_MODES_PER_OUTPUT:
                collector.add(
                    ParseIssueCode.TOO_LARGE,
                    f"output exceeds the {MAX_MODES_PER_OUTPUT}-mode limit",
                    line_number,
                )
                continue
            if mode.name in mode_names:
                collector.add(
                    ParseIssueCode.DUPLICATE,
                    "duplicate XRandR mode name",
                    line_number,
                )
                continue
            mode_names.add(mode.name)
            outputs[current_index] = replace(output, modes=(*output.modes, mode))

    if lines is not None and not outputs:
        collector.add(ParseIssueCode.MISSING_REQUIRED, "no XRandR output headers found")
    if sum(output.primary for output in outputs) > 1:
        collector.add(ParseIssueCode.INCONSISTENT, "multiple primary outputs reported")
    outputs.sort(key=lambda output: output.name)
    return XrandrQuery(tuple(outputs), evidence.raw_reference, collector.issues)


def parse_xrandr_properties(  # noqa: C901, PLR0912, PLR0915
    evidence: TextCommandEvidence,
) -> XrandrProperties:
    """Parse topology and connector IDs from bounded ``xrandr --props`` text."""
    collector = IssueCollector(RawEvidenceSource.XRANDR_PROPERTIES)
    lines = bounded_lines(evidence, RawEvidenceSource.XRANDR_PROPERTIES, collector)
    outputs: list[XrandrPropertyOutput] = []
    current_index: int | None = None
    mode_names: set[str] = set()

    if lines is not None:
        for line_number, line in enumerate(lines, start=1):
            if not line.strip() or line.startswith("Screen "):
                continue
            header = _OUTPUT_HEADER.fullmatch(line)
            if header is not None:
                if len(outputs) >= MAX_OUTPUTS:
                    collector.add(
                        ParseIssueCode.TOO_LARGE,
                        f"XRandR properties exceed the {MAX_OUTPUTS}-output limit",
                        line_number,
                    )
                    current_index = None
                    continue
                name = header.group("name")
                if len(name) > MAX_OUTPUT_NAME_CHARS:
                    collector.add(
                        ParseIssueCode.TOO_LARGE,
                        "XRandR output name exceeds its limit",
                        line_number,
                    )
                    current_index = None
                    continue
                if any(output.name == name for output in outputs):
                    collector.add(
                        ParseIssueCode.DUPLICATE,
                        "duplicate XRandR properties output header",
                        line_number,
                    )
                    current_index = None
                    continue
                connection = _connection_state(header.group("state"))
                rest = header.group("rest")
                geometry_match = _GEOMETRY.search(rest)
                geometry = (
                    _parse_geometry(geometry_match, line_number, collector)
                    if geometry_match is not None
                    else None
                )
                primary = re.search(r"(?<!\S)primary(?!\S)", rest) is not None
                if connection is not XConnectionState.CONNECTED and (
                    geometry is not None or primary
                ):
                    collector.add(
                        ParseIssueCode.INCONSISTENT,
                        "non-connected properties output carries active facts",
                        line_number,
                    )
                    geometry = None
                    primary = False
                outputs.append(
                    XrandrPropertyOutput(
                        name=name,
                        connection=connection,
                        geometry=geometry,
                        primary=primary,
                        modes=(),
                        connector_id=None,
                    )
                )
                current_index = len(outputs) - 1
                mode_names = set()
                continue
            if current_index is None:
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "property appeared before an output header",
                    line_number,
                )
                continue
            prop = _PROPERTY.fullmatch(line)
            if prop is None:
                current = outputs[current_index]
                # Property values and continuations are tab-indented.  XRandR
                # output modes use exactly three leading spaces.  Distinguish
                # those lexical forms before inspecting numeric tokens so CTM
                # matrix rows cannot masquerade as duplicate mode names.
                if not current.connected or not _has_mode_indentation(line):
                    continue
                if len(current.modes) >= MAX_MODES_PER_OUTPUT:
                    collector.add(
                        ParseIssueCode.TOO_LARGE,
                        f"output exceeds the {MAX_MODES_PER_OUTPUT}-mode limit",
                        line_number,
                    )
                    continue
                mode = _parse_mode_line(line, line_number, collector)
                if mode is not None and mode.name in mode_names:
                    collector.add(
                        ParseIssueCode.DUPLICATE,
                        "duplicate XRandR properties mode name",
                        line_number,
                    )
                elif mode is not None:
                    mode_names.add(mode.name)
                    outputs[current_index] = replace(
                        current,
                        modes=(*current.modes, mode),
                    )
                continue
            normalized_name = re.sub(r"[ _-]", "", prop.group("name")).casefold()
            if normalized_name != "connectorid":
                continue
            current = outputs[current_index]
            if current.connector_id is not None:
                collector.add(
                    ParseIssueCode.DUPLICATE,
                    "duplicate connector-ID property",
                    line_number,
                )
                continue
            value = prop.group("value")
            if not value.isascii() or not value.isdecimal():
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "connector-ID property is not unsigned decimal",
                    line_number,
                )
                continue
            connector_id = int(value, 10)
            if connector_id > MAX_CONNECTOR_ID:
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "connector-ID property exceeds unsigned 32-bit range",
                    line_number,
                )
                continue
            outputs[current_index] = replace(current, connector_id=connector_id)

    if lines is not None and not outputs:
        collector.add(
            ParseIssueCode.MISSING_REQUIRED,
            "no properties output headers found",
        )
    outputs.sort(key=lambda output: output.name)
    return XrandrProperties(tuple(outputs), evidence.raw_reference, collector.issues)


def sample_xrandr(source: XrandrEvidenceSource) -> XrandrSnapshot:
    """Merge two injected command results and reject any torn X sample."""
    query = parse_xrandr_query(source.query())
    properties = parse_xrandr_properties(source.properties())
    issues = [*query.issues, *properties.issues]
    query_topology = tuple(
        (item.name, item.connection, item.geometry, item.primary)
        for item in query.outputs
    )
    property_topology = tuple(
        (item.name, item.connection, item.geometry, item.primary)
        for item in properties.outputs
    )
    if query_topology != property_topology and len(issues) < MAX_PARSE_ISSUES:
        issues.append(
            ParseIssue(
                RawEvidenceSource.XRANDR_PROPERTIES,
                ParseIssueCode.INCONSISTENT,
                "query and properties output topologies differ",
            )
        )
    query_modes = tuple(
        (item.name, _normalized_mode_evidence(item.modes)) for item in query.outputs
    )
    property_modes = tuple(
        (item.name, _normalized_mode_evidence(item.modes))
        for item in properties.outputs
    )
    if query_modes != property_modes and len(issues) < MAX_PARSE_ISSUES:
        issues.append(
            ParseIssue(
                RawEvidenceSource.XRANDR_PROPERTIES,
                ParseIssueCode.INCONSISTENT,
                "query and properties mode lists or markers differ",
            )
        )
    connector_ids = {item.name: item.connector_id for item in properties.outputs}
    outputs = tuple(
        replace(output, connector_id=connector_ids.get(output.name))
        for output in query.outputs
    )
    raw_evidence = tuple(
        sorted(
            (query.raw_evidence, properties.raw_evidence),
            key=lambda item: (item.source.value, item.reference),
        )
    )
    return XrandrSnapshot(outputs, raw_evidence, tuple(issues[:MAX_PARSE_ISSUES]))


def _normalized_mode_evidence(
    modes: tuple[XrandrMode, ...],
) -> tuple[tuple[str, bool, bool], ...]:
    """Compare semantic mode names/markers, not presentation-only rate grammar."""
    return tuple(sorted((mode.name, mode.current, mode.preferred) for mode in modes))


def _connection_state(value: str) -> XConnectionState:
    if value == XConnectionState.CONNECTED.value:
        return XConnectionState.CONNECTED
    if value == XConnectionState.DISCONNECTED.value:
        return XConnectionState.DISCONNECTED
    return XConnectionState.UNKNOWN


def _parse_geometry(
    match: re.Match[str],
    line_number: int,
    collector: IssueCollector,
) -> XrandrGeometry | None:
    """Convert one bounded geometry token and retain conversion failures."""
    try:
        return XrandrGeometry(
            width=int(match.group("width")),
            height=int(match.group("height")),
            x=int(match.group("x")),
            y=int(match.group("y")),
        )
    except ValueError:
        collector.add(
            ParseIssueCode.MALFORMED_LINE,
            "XRandR output geometry is outside the accepted integer range",
            line_number,
        )
        return None


def _has_mode_indentation(line: str) -> bool:
    """Recognize the fixed three-space prefix used for XRandR mode rows."""
    return (
        len(line) > len(_MODE_INDENT)
        and line.startswith(_MODE_INDENT)
        and not line[len(_MODE_INDENT)].isspace()
    )


def _parse_mode_line(
    line: str,
    line_number: int,
    collector: IssueCollector,
) -> XrandrMode | None:
    parts = line.split()
    if len(parts) < MIN_MODE_LINE_PARTS:
        collector.add(
            ParseIssueCode.MALFORMED_LINE,
            "XRandR mode line lacks refresh rates",
            line_number,
        )
        return None
    rates: list[str] = []
    current = False
    preferred = False
    previous_rate_has_marker = False
    for token in parts[1:]:
        if token == _PREFERRED_MARKER:
            if not rates or previous_rate_has_marker:
                collector.add(
                    ParseIssueCode.MALFORMED_LINE,
                    "XRandR mode refresh marker is misplaced",
                    line_number,
                )
                return None
            preferred = True
            previous_rate_has_marker = True
            continue
        match = _RATE.fullmatch(token)
        if match is None:
            collector.add(
                ParseIssueCode.MALFORMED_LINE,
                "XRandR mode refresh token is malformed",
                line_number,
            )
            return None
        rates.append(match.group("rate"))
        markers = match.group("markers")
        current = current or "*" in markers
        preferred = preferred or "+" in markers
        previous_rate_has_marker = bool(markers)
    return XrandrMode(parts[0], tuple(rates), current, preferred)
