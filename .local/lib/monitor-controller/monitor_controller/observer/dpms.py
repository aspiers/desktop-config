"""Parse the screens' DPMS power state from injected ``xset q`` evidence."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from monitor_controller.model import DisplayPower

if TYPE_CHECKING:
    from monitor_controller.observer.evidence import TextCommandEvidence

_MONITOR_STATE = re.compile(r"^\s*Monitor is (\w+)\s*$", re.MULTILINE)


def parse_display_power(evidence: TextCommandEvidence) -> DisplayPower:
    """Return ASLEEP only when DPMS positively reports a non-On monitor.

    Missing or unreadable evidence counts as ON. Deferring work on a guess
    would leave a display dark indefinitely, which is the worse failure.
    """
    if evidence.exit_status != 0 or evidence.timed_out:
        return DisplayPower.ON
    match = _MONITOR_STATE.search(evidence.stdout)
    if match is None or match.group(1) == "On":
        return DisplayPower.ON
    return DisplayPower.ASLEEP
