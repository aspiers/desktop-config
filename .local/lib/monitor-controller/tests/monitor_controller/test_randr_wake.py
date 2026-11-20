"""RandR wake-ups for displays switched off or on by hand (dc-8xns)."""

from __future__ import annotations

import asyncio

import pytest

from monitor_controller.runtime.randr_wake import RandrWakeMonitor

# Two bursts, shaped like `xev -root -event randr` output, separated by more
# than the settle time.
_EVENTS = (
    "printf 'RRScreenChangeNotify event, serial 18\\n    root 0x39f\\n'"
    "; printf 'RRNotify event, serial 18\\n    subtype XRRCrtcChangeNotifyEvent\\n'"
    "; sleep 0.3"
    "; printf 'RRNotify event, serial 19\\n    subtype XRROutputChangeNotifyEvent\\n'"
)


def _hints(*, may_hint: bool) -> list[str]:
    calls: list[str] = []
    monitor = RandrWakeMonitor(
        may_hint=lambda: may_hint,
        on_hint=lambda: calls.append("on_hint"),
        command=("sh", "-c", _EVENTS),
        settle_seconds=0.1,
    )
    asyncio.run(monitor.follow(lambda: calls.append("hint")))
    return calls


def test_each_burst_of_randr_events_hints_once() -> None:
    # on_hint must run first: the hint moves the controller out of quiescent.
    assert _hints(may_hint=True) == ["on_hint", "hint"] * 2


def test_busy_controller_is_not_hinted_by_its_own_changes() -> None:
    assert _hints(may_hint=False) == []


def test_missing_listener_is_reported_as_os_error() -> None:
    monitor = RandrWakeMonitor(
        may_hint=lambda: True, command=("/nonexistent/xev",), settle_seconds=0.1
    )
    with pytest.raises(OSError, match="nonexistent"):
        asyncio.run(monitor.follow(lambda: None))
