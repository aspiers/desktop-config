"""Contract tests for injected XRandR query and properties parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from monitor_controller.model import RawEvidenceSource
from monitor_controller.observer.evidence import (
    MAX_COMMAND_BYTES,
    MAX_PARSE_ISSUES,
    ParseIssueCode,
    TextCommandEvidence,
)
from monitor_controller.observer.xrandr import (
    XConnectionState,
    parse_xrandr_properties,
    parse_xrandr_query,
    sample_xrandr,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xrandr"


def evidence(name: str, source: RawEvidenceSource) -> TextCommandEvidence:
    return TextCommandEvidence(
        source,
        f"fixture:xrandr/{name}",
        (FIXTURES / name).read_text(encoding="utf-8"),
    )


@dataclass
class InjectedXrandr:
    """Return only supplied fixture evidence and record the bounded calls."""

    query_name: str
    properties_name: str
    calls: list[str]

    def query(self) -> TextCommandEvidence:
        """Return injected query evidence."""
        self.calls.append("query")
        return evidence(self.query_name, RawEvidenceSource.XRANDR_QUERY)

    def properties(self) -> TextCommandEvidence:
        """Return injected properties evidence."""
        self.calls.append("properties")
        return evidence(self.properties_name, RawEvidenceSource.XRANDR_PROPERTIES)


@pytest.mark.parametrize(
    ("name", "connected", "active", "primary"),
    [
        ("laptop.query", ("eDP",), ("eDP",), "eDP"),
        (
            "samsung.query",
            ("DisplayPort-9", "eDP"),
            ("DisplayPort-9", "eDP"),
            "DisplayPort-9",
        ),
        (
            "aoc-rename.query",
            ("DisplayPort-7", "eDP"),
            ("DisplayPort-7", "eDP"),
            "DisplayPort-7",
        ),
        (
            "inactive.query",
            ("DisplayPort-9", "eDP"),
            ("eDP",),
            "eDP",
        ),
        (
            "extra-output.query",
            ("DisplayPort-7", "HDMI-A-1", "eDP"),
            ("DisplayPort-7", "HDMI-A-1", "eDP"),
            "DisplayPort-7",
        ),
    ],
)
def test_query_fixtures_distinguish_connected_active_and_primary(
    name: str,
    connected: tuple[str, ...],
    active: tuple[str, ...],
    primary: str,
) -> None:
    parsed = parse_xrandr_query(evidence(name, RawEvidenceSource.XRANDR_QUERY))

    assert parsed.valid
    assert tuple(item.name for item in parsed.outputs if item.connected) == connected
    assert tuple(item.name for item in parsed.outputs if item.active) == active
    assert next(item.name for item in parsed.outputs if item.primary) == primary


def test_query_retains_all_rates_and_current_preferred_markers() -> None:
    parsed = parse_xrandr_query(
        evidence("samsung.query", RawEvidenceSource.XRANDR_QUERY)
    )
    samsung = next(item for item in parsed.outputs if item.name == "DisplayPort-9")

    assert samsung.geometry is not None
    assert (samsung.geometry.width, samsung.geometry.height) == (5120, 2160)
    assert samsung.current_modes == ("5120x2160",)
    assert samsung.preferred_modes == ("5120x2160",)
    assert samsung.modes[1].rates == ("60.00", "59.94")


def test_inactive_output_retains_preferred_mode_without_becoming_active() -> None:
    parsed = parse_xrandr_query(
        evidence("inactive.query", RawEvidenceSource.XRANDR_QUERY)
    )
    external = next(item for item in parsed.outputs if item.name == "DisplayPort-9")

    assert external.connected
    assert not external.active
    assert external.current_modes == ()
    assert external.preferred_modes == ("5120x2160",)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("laptop.props", (("DisplayPort-0", 81), ("eDP", 73))),
        (
            "samsung.props",
            (("DisplayPort-8", 90), ("DisplayPort-9", 91), ("eDP", 73)),
        ),
        ("aoc-rename.props", (("DisplayPort-7", 107), ("eDP", 73))),
    ],
)
def test_properties_accept_connector_id_spelling_variants(
    name: str, expected: tuple[tuple[str, int], ...]
) -> None:
    parsed = parse_xrandr_properties(
        evidence(name, RawEvidenceSource.XRANDR_PROPERTIES)
    )

    assert parsed.valid
    assert (
        tuple(
            (item.name, item.connector_id)
            for item in parsed.outputs
            if item.connector_id is not None
        )
        == expected
    )


def test_properties_retain_active_primary_and_mode_markers() -> None:
    parsed = parse_xrandr_properties(
        evidence("laptop.props", RawEvidenceSource.XRANDR_PROPERTIES)
    )
    internal = next(item for item in parsed.outputs if item.name == "eDP")

    assert internal.connected
    assert internal.active
    assert internal.primary
    assert internal.current_modes == ("2880x1920",)
    assert internal.preferred_modes == ("2880x1920",)


def test_injected_pair_is_merged_without_any_live_command() -> None:
    source = InjectedXrandr("samsung.query", "samsung.props", [])

    snapshot = sample_xrandr(source)

    assert snapshot.valid
    assert source.calls == ["query", "properties"]
    assert snapshot.connected_outputs == ("DisplayPort-9", "eDP")
    assert snapshot.active_outputs == ("DisplayPort-9", "eDP")
    assert snapshot.primary_output == "DisplayPort-9"
    assert snapshot.connector_ids() == (
        ("DisplayPort-8", 90),
        ("DisplayPort-9", 91),
        ("eDP", 73),
    )
    assert tuple(item.source for item in snapshot.raw_evidence) == (
        RawEvidenceSource.XRANDR_PROPERTIES,
        RawEvidenceSource.XRANDR_QUERY,
    )
    assert all(len(item.sha256) == 64 for item in snapshot.raw_evidence)


def test_query_and_properties_topology_change_is_explicitly_invalid() -> None:
    snapshot = sample_xrandr(InjectedXrandr("laptop.query", "torn.props", []))

    assert not snapshot.valid
    assert ParseIssueCode.INCONSISTENT in {item.code for item in snapshot.issues}


def test_malformed_query_and_properties_retain_bounded_typed_errors() -> None:
    query = parse_xrandr_query(
        evidence("malformed.query", RawEvidenceSource.XRANDR_QUERY)
    )
    properties = parse_xrandr_properties(
        evidence("malformed.props", RawEvidenceSource.XRANDR_PROPERTIES)
    )

    assert not query.valid
    assert ParseIssueCode.MALFORMED_LINE in {item.code for item in query.issues}
    assert not properties.valid
    assert ParseIssueCode.MALFORMED_LINE in {item.code for item in properties.issues}
    assert len(query.issues) <= MAX_PARSE_ISSUES
    assert len(properties.issues) <= MAX_PARSE_ISSUES
    assert all(len(item.detail) <= 160 for item in (*query.issues, *properties.issues))


@pytest.mark.parametrize(
    ("timed_out", "exit_status", "code"),
    [
        (True, 124, ParseIssueCode.COMMAND_TIMED_OUT),
        (False, 1, ParseIssueCode.COMMAND_FAILED),
    ],
)
def test_command_failures_are_typed_not_guessed_as_empty_topology(
    timed_out: bool,
    exit_status: int,
    code: ParseIssueCode,
) -> None:
    parsed = parse_xrandr_query(
        TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "injected:failure",
            "",
            exit_status=exit_status,
            timed_out=timed_out,
        )
    )

    assert parsed.outputs == ()
    assert tuple(item.code for item in parsed.issues) == (code,)


def test_oversized_raw_output_is_referenced_but_never_partially_parsed() -> None:
    parsed = parse_xrandr_query(
        TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "injected:oversized",
            "x" * (MAX_COMMAND_BYTES + 1),
        )
    )

    assert parsed.outputs == ()
    assert tuple(item.code for item in parsed.issues) == (ParseIssueCode.TOO_LARGE,)
    assert len(parsed.raw_evidence.sha256) == 64


def test_unknown_connection_remains_uncertain_not_disconnected() -> None:
    parsed = parse_xrandr_query(
        TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "injected:unknown",
            "DP-1 unknown connection (normal left inverted right x axis y axis)\n",
        )
    )

    assert parsed.valid
    assert parsed.outputs[0].connection is XConnectionState.UNKNOWN
    assert not parsed.outputs[0].connected
