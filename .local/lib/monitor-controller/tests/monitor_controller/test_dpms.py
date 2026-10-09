"""Contract tests for reading the screens' DPMS state from ``xset q``."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitor_controller.model import DisplayPower, RawEvidenceSource
from monitor_controller.observer.dpms import parse_display_power
from monitor_controller.observer.evidence import TextCommandEvidence

FIXTURES = Path(__file__).parent / "fixtures" / "xset"


def _evidence(
    stdout: str, *, exit_status: int = 0, timed_out: bool = False
) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.DPMS,
        "command:xset q",
        stdout,
        exit_status=exit_status,
        timed_out=timed_out,
    )


def test_live_capture_with_the_monitor_on_is_awake() -> None:
    text = (FIXTURES / "awake.txt").read_text(encoding="utf-8")

    assert parse_display_power(_evidence(text)) is DisplayPower.ON


@pytest.mark.parametrize("level", ["Standby", "Suspend", "Off"])
def test_every_non_on_dpms_level_is_asleep(level: str) -> None:
    text = (FIXTURES / "awake.txt").read_text(encoding="utf-8")
    text = text.replace("Monitor is On", f"Monitor is {level}")

    assert parse_display_power(_evidence(text)) is DisplayPower.ASLEEP


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence("DPMS (Display Power Management Signaling):\n  DPMS is Disabled\n"),
        _evidence("  Monitor is Standby\n", exit_status=1),
        _evidence("  Monitor is Standby\n", exit_status=124, timed_out=True),
        _evidence(""),
    ],
)
def test_missing_or_failed_evidence_never_defers_work(
    evidence: TextCommandEvidence,
) -> None:
    assert parse_display_power(evidence) is DisplayPower.ON
