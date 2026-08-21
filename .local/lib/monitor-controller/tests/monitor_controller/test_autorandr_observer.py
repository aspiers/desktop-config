"""Contract tests for autorandr grammar and strict identity mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from monitor_controller.model import Fingerprint, ProfileScope, RawEvidenceSource
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    fingerprint_matches,
    parse_autorandr_fingerprint,
    parse_current_profiles,
    parse_detected_profiles,
    parse_saved_profile,
    resolve_output_mapping,
    sample_autorandr,
)
from monitor_controller.observer.evidence import (
    MAX_COMMAND_BYTES,
    MAX_PARSE_ISSUES,
    ParseIssueCode,
    TextCommandEvidence,
)

FIXTURES = Path(__file__).parent / "fixtures" / "autorandr"
COMMANDS = FIXTURES / "commands"
PROFILES = FIXTURES / "profiles"


def command_evidence(name: str, source: RawEvidenceSource) -> TextCommandEvidence:
    return TextCommandEvidence(
        source,
        f"fixture:autorandr/commands/{name}",
        (COMMANDS / name).read_text(encoding="utf-8"),
    )


def text_evidence(reference: str, value: str) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        reference,
        value,
    )


def load_profile(name: str) -> SavedAutorandrProfile:
    root = PROFILES / name
    layout_path = root / "layout"
    result = parse_saved_profile(
        name,
        text_evidence(f"profiles/{name}/config", (root / "config").read_text()),
        text_evidence(f"profiles/{name}/setup", (root / "setup").read_text()),
        (
            text_evidence(f"profiles/{name}/layout", layout_path.read_text())
            if layout_path.exists()
            else None
        ),
    )
    assert result.valid
    assert result.profile is not None
    return result.profile


@dataclass
class InjectedAutorandr:
    """Return only supplied fixtures and prove all command calls are injected."""

    fingerprint_name: str
    detected_name: str
    current_name: str
    calls: list[str]

    def fingerprint(self) -> TextCommandEvidence:
        """Return an injected fingerprint result."""
        self.calls.append("fingerprint")
        return command_evidence(
            self.fingerprint_name,
            RawEvidenceSource.AUTORANDR_FINGERPRINT,
        )

    def detected(self) -> TextCommandEvidence:
        """Return an injected detected-profile result."""
        self.calls.append("detected")
        return command_evidence(
            self.detected_name,
            RawEvidenceSource.AUTORANDR_PROFILES,
        )

    def current(self) -> TextCommandEvidence:
        """Return an injected current-profile result."""
        self.calls.append("current")
        return command_evidence(
            self.current_name,
            RawEvidenceSource.AUTORANDR_PROFILES,
        )


@pytest.mark.parametrize(
    ("name", "scope", "layout", "active"),
    [
        ("celtic", ProfileScope.INTERNAL_ONLY, "celtic", ("eDP",)),
        (
            "celtic+AOC-U28G2G6B",
            ProfileScope.MIXED,
            "celtic+external",
            ("DisplayPort-2", "eDP"),
        ),
        (
            "celtic+Samsung-Odyssey-G75F",
            ProfileScope.MIXED,
            "celtic+ultrawide",
            ("DisplayPort-1", "eDP"),
        ),
        (
            "Level39",
            ProfileScope.MIXED,
            "celtic+external",
            ("DisplayPort-1", "eDP"),
        ),
    ],
)
def test_copied_real_profiles_cover_installed_grammar(
    name: str,
    scope: ProfileScope,
    layout: str,
    active: tuple[str, ...],
) -> None:
    profile = load_profile(name)

    assert profile.scope is scope
    assert profile.layout == layout
    assert profile.active_outputs == active
    assert {item.output for item in profile.setup} == set(active)
    assert tuple(item.path for item in profile.configuration_hashes) == tuple(
        sorted(item.path for item in profile.configuration_hashes)
    )


def test_documented_command_outputs_are_combined_without_live_display() -> None:
    source = InjectedAutorandr(
        "samsung-renamed.fingerprint",
        "detected.out",
        "current.out",
        [],
    )

    observation = sample_autorandr(source)

    assert observation.valid
    assert source.calls == ["fingerprint", "detected", "current"]
    assert tuple(item.output for item in observation.fingerprints) == (
        "DisplayPort-9",
        "eDP",
    )
    assert observation.detected_profiles == (
        "Level39",
        "celtic+Samsung-Odyssey-G75F",
    )
    assert observation.current_profiles == ("celtic+Samsung-Odyssey-G75F",)
    assert len(observation.raw_evidence) == 3
    assert all(len(item.sha256) == 64 for item in observation.raw_evidence)


def test_current_and_detected_allow_a_valid_empty_set() -> None:
    empty = command_evidence("empty.out", RawEvidenceSource.AUTORANDR_PROFILES)

    assert parse_current_profiles(empty).valid
    assert parse_current_profiles(empty).profiles == ()
    assert parse_detected_profiles(empty).valid
    assert parse_detected_profiles(empty).profiles == ()


def test_fingerprint_output_requires_machine_readable_setup_lines() -> None:
    parsed = parse_autorandr_fingerprint(
        command_evidence(
            "diagnostics.out",
            RawEvidenceSource.AUTORANDR_FINGERPRINT,
        )
    )

    assert not parsed.valid
    assert parsed.fingerprints == ()
    assert ParseIssueCode.MALFORMED_LINE in {item.code for item in parsed.issues}


def test_profile_set_explicitly_rejects_rename_diagnostic_and_monitor_variable() -> (
    None
):
    parsed = parse_current_profiles(
        command_evidence("diagnostics.out", RawEvidenceSource.AUTORANDR_PROFILES)
    )

    assert not parsed.valid
    assert parsed.profiles == ()
    assert tuple(item.code for item in parsed.issues) == (
        ParseIssueCode.MALFORMED_LINE,
        ParseIssueCode.MALFORMED_LINE,
        ParseIssueCode.MALFORMED_LINE,
    )


@pytest.mark.parametrize(
    ("pattern", "live", "matches"),
    [
        ("abcdef", "abcdef", True),
        ("abcdef", "abcdeg", False),
        ("abc*xyz", "abc012345xyz", True),
        ("abc*xyz", "abc012345xy", False),
        ("*xyz", "xyz", True),
        ("abc*", "abcdef", True),
    ],
)
def test_documented_single_wildcard_matcher(
    pattern: str, live: str, *, matches: bool
) -> None:
    assert fingerprint_matches(pattern, live) is matches


@pytest.mark.parametrize(
    ("pattern", "live"),
    [("a*b*c", "abc"), ("abc", "a*c")],
)
def test_matcher_rejects_unsupported_wildcard_syntax(pattern: str, live: str) -> None:
    with pytest.raises(ValueError, match="wildcard"):
        fingerprint_matches(pattern, live)


def test_exact_laptop_mapping_is_complete() -> None:
    profile = load_profile("celtic")
    live = parse_autorandr_fingerprint(
        command_evidence("laptop.fingerprint", RawEvidenceSource.AUTORANDR_FINGERPRINT)
    )

    result = resolve_output_mapping(profile, live.fingerprints, ("eDP",))

    assert result.valid
    assert result.mapping is not None
    assert tuple((item.saved_output, item.live_output) for item in result.mapping) == (
        ("eDP", "eDP"),
    )


def test_connector_renamed_aoc_mapping_uses_only_fingerprints() -> None:
    profile = load_profile("celtic+AOC-U28G2G6B")
    live = parse_autorandr_fingerprint(
        command_evidence(
            "aoc-renamed.fingerprint",
            RawEvidenceSource.AUTORANDR_FINGERPRINT,
        )
    )

    result = resolve_output_mapping(
        profile,
        live.fingerprints,
        ("DisplayPort-7", "eDP"),
    )

    assert result.valid
    assert result.mapping is not None
    assert tuple((item.saved_output, item.live_output) for item in result.mapping) == (
        ("DisplayPort-2", "DisplayPort-7"),
        ("eDP", "eDP"),
    )


def test_real_samsung_wildcard_maps_one_renamed_live_connector() -> None:
    profile = load_profile("celtic+Samsung-Odyssey-G75F")
    live = parse_autorandr_fingerprint(
        command_evidence(
            "samsung-renamed.fingerprint",
            RawEvidenceSource.AUTORANDR_FINGERPRINT,
        )
    )

    result = resolve_output_mapping(
        profile,
        live.fingerprints,
        ("DisplayPort-9", "eDP"),
    )

    assert result.valid
    assert result.mapping is not None
    assert tuple((item.saved_output, item.live_output) for item in result.mapping) == (
        ("DisplayPort-1", "DisplayPort-9"),
        ("eDP", "eDP"),
    )


def synthetic_profile(setup: str, outputs: tuple[str, ...]) -> SavedAutorandrProfile:
    config = "".join(f"output {output}\nmode 1920x1080\n" for output in outputs)
    result = parse_saved_profile(
        "synthetic",
        text_evidence("synthetic/config", config),
        text_evidence("synthetic/setup", setup),
    )
    assert result.valid
    assert result.profile is not None
    return result.profile


@pytest.mark.parametrize(
    ("live", "connected", "code"),
    [
        (
            (
                Fingerprint("LIVE-A", "aa"),
                Fingerprint("LIVE-B", "bb"),
                Fingerprint("EXTRA", "ee"),
            ),
            ("EXTRA", "LIVE-A", "LIVE-B"),
            ParseIssueCode.TOPOLOGY_MISMATCH,
        ),
        (
            (Fingerprint("LIVE-A", "aa"),),
            ("LIVE-A",),
            ParseIssueCode.TOPOLOGY_MISMATCH,
        ),
        (
            (Fingerprint("LIVE-A", "aa"), Fingerprint("LIVE-B", "cc")),
            ("LIVE-A", "LIVE-B"),
            ParseIssueCode.UNMATCHED,
        ),
    ],
)
def test_missing_extra_and_unmatched_topologies_are_rejected(
    live: tuple[Fingerprint, ...],
    connected: tuple[str, ...],
    code: ParseIssueCode,
) -> None:
    profile = synthetic_profile("SAVED-A aa\nSAVED-B bb\n", ("SAVED-A", "SAVED-B"))

    result = resolve_output_mapping(profile, live, connected)

    assert not result.valid
    assert result.mapping is None
    assert code in {item.code for item in result.issues}


def test_duplicate_fingerprint_collision_is_rejected_before_assignment() -> None:
    profile = load_profile("collision")
    live = (Fingerprint("LIVE-A", "same"), Fingerprint("LIVE-B", "same"))

    result = resolve_output_mapping(profile, live, ("LIVE-A", "LIVE-B"))

    assert not result.valid
    assert tuple(item.code for item in result.issues) == (ParseIssueCode.COLLISION,)


def test_overlapping_wildcards_with_two_perfect_bijections_are_ambiguous() -> None:
    profile = load_profile("ambiguous")
    live = (Fingerprint("LIVE-A", "ab"), Fingerprint("LIVE-B", "acb"))

    result = resolve_output_mapping(profile, live, ("LIVE-A", "LIVE-B"))

    assert not result.valid
    assert tuple(item.code for item in result.issues) == (ParseIssueCode.AMBIGUOUS,)


def test_profile_parser_rejects_multiple_wildcards_and_inconsistent_files() -> None:
    result = parse_saved_profile(
        "broken",
        text_evidence("broken/config", "output DP-1\nmode 1920x1080\n"),
        text_evidence("broken/setup", "DP-2 a*b*c\n"),
    )

    assert not result.valid
    assert result.profile is None
    codes = {item.code for item in result.issues}
    assert ParseIssueCode.UNSUPPORTED_WILDCARD in codes
    assert ParseIssueCode.INCONSISTENT in codes


def test_profile_parser_bounds_raw_input_and_error_count() -> None:
    too_large = "x" * (MAX_COMMAND_BYTES + 1)
    result = parse_saved_profile(
        "bounded",
        text_evidence("bounded/config", too_large),
        text_evidence("bounded/setup", "\n".join("broken" for _ in range(100))),
    )

    assert not result.valid
    assert len(result.issues) <= MAX_PARSE_ISSUES
    assert ParseIssueCode.TOO_LARGE in {item.code for item in result.issues}
    assert all(len(item.detail) <= 160 for item in result.issues)
    assert all(len(item.sha256) == 64 for item in result.raw_evidence)
