"""Declarative layout window rules: relayout commands and apps attributes."""

from __future__ import annotations

import pytest

from monitor_controller.desktop.layout import (
    DisplayScreenSnapshot,
    LayoutPlanningError,
    ResolvedLayout,
    parse_layout,
    resolve_layout,
)

_SCREENS = (
    b"screens:\n"
    b"  -\n    name: laptop\n    head: 2\n    assignment: secondary\n"
    b"  -\n    name: external\n    head: 1\n    assignment: primary\n"
)


def _resolve(windows: bytes) -> ResolvedLayout:
    parsed = parse_layout(
        "test",
        ((".fluxbox/layouts/test.yaml", _SCREENS + b"windows:\n" + windows),),
    )
    return resolve_layout(
        parsed,
        (
            DisplayScreenSnapshot("eDP", 1920, 1200, 0, 0, 300, 190, primary=False),
            DisplayScreenSnapshot("DP-1", 2560, 1440, 1920, 0, 600, 340, primary=True),
        ),
    )


def test_fields_derive_matching_relayout_commands_and_apps_attributes() -> None:
    (action,) = _resolve(
        b"  - match: (Name=kitty)\n"
        b"    screen: laptop\n"
        b"    workspace: 4\n"
        b"    position: 0 0 Center\n"
        b"    size: <s_secondary_width> 50%\n"
        b"    sticky: yes\n"
        b"    relayout_command: Activate\n"
    ).window_actions

    assert action.commands == (
        "SetHead 2",
        "SendToWorkspace 4",
        "ResizeTo 1920 50%",
        "MoveTo 0 0 Center",
        "If {Matches (Stuck=no)} {Stick}",
        "Activate",
    )
    assert action.map_command.endswith("{Matches (Name=kitty)}")
    assert [(item.name, item.value) for item in action.apps_attributes] == [
        ("Head", "{2}"),
        ("Workspace", "{3}"),
        ("Dimensions", "{1920 50%}"),
        ("Position", "(Center)\t{0 0}"),
        ("Sticky", "{yes}"),
    ]


def test_screen_names_either_assignment_or_name() -> None:
    by_name, by_assignment = _resolve(
        b"  - match: (Name=a)\n    screen: external\n"
        b"  - match: (Name=b)\n    screen: primary\n"
    ).window_actions
    assert by_name.commands == by_assignment.commands == ("SetHead 1",)


def test_map_no_keeps_relayout_but_drops_apps_attributes() -> None:
    (action,) = _resolve(
        b"  - match: (Name=btop)\n    workspace: 6\n    map: no\n"
    ).window_actions
    assert action.commands == ("SendToWorkspace 6",)
    assert action.apps_attributes == ()


def test_position_without_corner_is_upper_left_in_apps() -> None:
    (action,) = _resolve(b"  - match: (Name=a)\n    position: 5% 30\n").window_actions
    assert action.commands == ("MoveTo 5% 30",)
    assert action.apps_attributes[0].value == "(UpperLeft)\t{5% 30}"


@pytest.mark.parametrize(
    ("windows", "message"),
    [
        (b"  - - (Name=a)\n    - Raise\n", "replaced by declarative fields"),
        (b"  - match: (Name=a)\n    colour: red\n", "unsupported window rule field"),
        (b"  - match: (Name=a)\n    sticky: maybe\n", "yes or no"),
        (b"  - match: (Name=a)\n    map: no\n", "place the window"),
        (b"  - match: (Name=a)\n    screen: a\n    screen: b\n", "repeats a field"),
        (b"  - match: (Name=a)\n    screen: nowhere\n", "names no single screen"),
        (b"  - match: (Name=a)\n    position: 0 0 Middle\n", "corner"),
        (b"  - match: (Name=a)\n    size: 10\n", "malformed"),
        (b"  - match: (Name=a)\n    workspace: 0\n", "positive"),
        (b"  - screen: primary\n", "must start with match:"),
    ],
)
def test_malformed_rules_are_rejected(windows: bytes, message: str) -> None:
    with pytest.raises(LayoutPlanningError, match=message):
        _resolve(windows)
