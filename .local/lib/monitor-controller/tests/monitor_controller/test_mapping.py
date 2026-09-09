"""Shared exact output-bijection search contracts."""

import pytest

from monitor_controller.observer.mapping import (
    AmbiguousBijectionError,
    unique_bijection,
)


def test_unique_bijection_distinguishes_unique_missing_and_ambiguous() -> None:
    assert unique_bijection(
        {"saved-a": ("live-a",), "saved-b": ("live-b",)},
        frozenset({"live-a", "live-b"}),
    ) == {"saved-a": "live-a", "saved-b": "live-b"}

    assert (
        unique_bijection(
            {"saved-a": ("live-a",), "saved-b": ("live-a",)},
            frozenset({"live-a", "live-b"}),
        )
        is None
    )

    with pytest.raises(AmbiguousBijectionError):
        unique_bijection(
            {
                "saved-a": ("live-a", "live-b"),
                "saved-b": ("live-a", "live-b"),
            },
            frozenset({"live-a", "live-b"}),
        )
