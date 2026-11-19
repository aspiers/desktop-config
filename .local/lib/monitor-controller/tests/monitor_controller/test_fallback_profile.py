"""Best-guess profiles for screens left over after a manual switch-off (dc-8xns)."""

from __future__ import annotations

from pathlib import Path

from monitor_controller.model import Fingerprint, RawEvidenceSource
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
    resolve_output_mapping,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.fallback_profile import FallbackProfileWriter
from monitor_controller.observer.xrandr import (
    XConnectionState,
    XrandrGeometry,
    XrandrMode,
    XrandrOutput,
    XrandrSnapshot,
)

PROFILES = Path(__file__).parent / "fixtures" / "autorandr" / "profiles"

EDP = Fingerprint("eDP", "00ffffffffffff0009e5b40c" + "00" * 20)
UNKNOWN = Fingerprint("DisplayPort-7", "00ffffffffffff00abcd1234" + "11" * 20)

LIVE_CONFIG = """\
output DisplayPort-1
off
output DisplayPort-7
crtc 0
gamma 1.25:1.0:0.833
mode 3840x2160
pos 2880x0
rate 60.00
output eDP
crtc 1
mode 2880x1920
pos 0x0
primary
rate 60.00
"""


def _evidence(reference: str, path: Path) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES, reference, path.read_text("utf-8")
    )


def _load(directory: Path) -> SavedAutorandrProfile:
    layout = directory / "layout"
    result = parse_saved_profile(
        directory.name,
        _evidence("config", directory / "config"),
        _evidence("setup", directory / "setup"),
        _evidence("layout", layout) if layout.exists() else None,
    )
    assert result.profile is not None, result.issues
    return result.profile


def _output(name: str, width: int, height: int, x: int) -> XrandrOutput:
    return XrandrOutput(
        name,
        XConnectionState.CONNECTED,
        XrandrGeometry(width, height, x, 0),
        primary=False,
        modes=(
            XrandrMode(f"{width}x{height}", ("60.00",), current=True, preferred=True),
        ),
    )


XRANDR = XrandrSnapshot(
    outputs=(
        _output("eDP", 2880, 1920, 0),
        _output("DisplayPort-7", 3840, 2160, 2880),
        # The display switched off by hand, masked as unplugged.
        XrandrOutput(
            "DisplayPort-1",
            XConnectionState.DISCONNECTED,
            None,
            primary=False,
            modes=(),
        ),
    ),
    raw_evidence=(),
    issues=(),
)
TEMPLATES = tuple(
    _load(PROFILES / name)
    for name in (
        "Level39",
        "celtic+AOC-U28G2G6B",
        "celtic+Samsung-Odyssey-G75F",
        "celtic",
    )
)


def _writer(root: Path, notes: list[str]) -> FallbackProfileWriter:
    return FallbackProfileWriter(
        root,
        host="celtic",
        notify=lambda summary, _body: notes.append(summary),
        dump_config=lambda: LIVE_CONFIG,
    )


def test_unmatched_screens_get_the_shared_layout_and_its_primary_role(
    tmp_path: Path,
) -> None:
    notes: list[str] = []
    writer = _writer(tmp_path, notes)

    writer((EDP, UNKNOWN), XRANDR, TEMPLATES)

    [directory] = tmp_path.iterdir()
    saved = _load(directory)
    # Two saved profiles share celtic+external; the Samsung's own layout loses.
    assert saved.layout == "celtic+external"
    # Those profiles make the external screen primary, so this one does too.
    assert [item.output for item in saved.config if item.primary] == ["DisplayPort-7"]
    assert "gamma" not in (directory / "config").read_text("utf-8")
    assert not (directory / ".config.tmp").exists()
    # The saved profile matches the screens it was made for.
    connected = ("eDP", "DisplayPort-7")
    assert resolve_output_mapping(saved, (EDP, UNKNOWN), connected).mapping is not None
    assert notes == [f"Saved display profile {directory.name}"]

    # One attempt per screen set: no second notification, no rewrite.
    writer((EDP, UNKNOWN), XRANDR, TEMPLATES)
    assert len(notes) == 1


def test_screen_count_no_profile_has_only_notifies(tmp_path: Path) -> None:
    notes: list[str] = []

    _writer(tmp_path, notes)((UNKNOWN,), XRANDR, TEMPLATES[:3])

    assert not list(tmp_path.iterdir())
    assert notes == ["No display profile for these screens"]


def test_existing_profile_directory_is_never_overwritten(tmp_path: Path) -> None:
    notes: list[str] = []
    writer = _writer(tmp_path, notes)
    writer((EDP, UNKNOWN), XRANDR, TEMPLATES)
    [directory] = tmp_path.iterdir()
    directory.joinpath("config").write_text("mine\n", encoding="utf-8")

    _writer(tmp_path, notes)((EDP, UNKNOWN), XRANDR, TEMPLATES)

    assert directory.joinpath("config").read_text("utf-8") == "mine\n"
    assert len(notes) == 1
