"""The generated Fluxbox apps file: layout placement merged into apps.in."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitor_controller.desktop.fluxbox_renderer import (
    FluxboxRenderError,
    render_fluxbox_apps,
)
from monitor_controller.desktop.layout import (
    DisplayScreenSnapshot,
    NamedString,
    ResolvedWindowAction,
    parse_layout,
    resolve_layout,
)

_REPO = Path(__file__).resolve().parents[5]


def _action(matcher: str, *attributes: tuple[str, str]) -> ResolvedWindowAction:
    return ResolvedWindowAction(
        matcher=matcher,
        commands=("Raise",),
        map_command=f"Map {{Raise}} {{Matches {matcher}}}",
        apps_attributes=tuple(NamedString(*item) for item in attributes),
    )


def _render(
    template: str,
    *actions: ResolvedWindowAction,
    non_primary_heads: tuple[int, ...] = (),
) -> str:
    return render_fluxbox_apps(
        template.encode(),
        actions,
        non_primary_heads=non_primary_heads,
        template_label="apps.in",
    ).decode()


def test_latest_rule_comes_first_and_identical_matchers_merge() -> None:
    rendered = _render(
        "",
        _action("(Name=a)", ("Head", "{1}"), ("Sticky", "{no}")),
        _action("(Name=b)", ("Head", "{2}")),
        _action("(Name=a)", ("Sticky", "{yes}")),
        _action("(Name=c)"),
    )
    blocks = rendered.split("[app] ")[1:]
    assert blocks == [
        "(Name=a)\n  [Head]\t{1}\n  [Sticky]\t{yes}\n[end]\n",
        "(Name=b)\n  [Head]\t{2}\n[end]\n",
    ]


def test_template_block_with_the_same_pattern_joins_its_placement() -> None:
    template = (
        "# header\n\n"
        "# about a\n"
        "[app] (Name=a)\n  [Layer]\t{2}\n  [Sticky]\t{no}\n[end]\n"
        "[app] (Name=z)\n  [Hidden]\t{yes}\n[end]\n"
    )
    rendered = _render(template, _action("(Name=a)", ("Sticky", "{yes}")))
    body = rendered.split("\n\n", 1)[1]
    assert body == (
        "# about a\n[app] (Name=a)\n  [Layer]\t{2}\n  [Sticky]\t{yes}\n[end]\n"
        "# header\n\n"
        "[app] (Name=z)\n  [Hidden]\t{yes}\n[end]\n"
    )


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("[app] (Name=a)\n  [Head]\t{1}\n[end]\n", "belongs in"),
        ("[app] (Name=a)\n  [Layer]\t{2}\n", "ends inside a block"),
        ("[app] (Name=a)\n[app] (Name=b)\n[end]\n", "missing"),
    ],
)
def test_malformed_templates_are_rejected(template: str, message: str) -> None:
    with pytest.raises(FluxboxRenderError, match=message):
        _render(template)


_STICKY_TEMPLATE = (
    "[app] (name=chrome-app)\n  [Sticky]\t{no}\n[end]\n"
    "[app] (head=non-primary)\n  [Sticky]\t{yes}\n[end]\n"
    "[app] (name=eog)\n  [Deco]\t{NORMAL}\n[end]\n"
)


@pytest.mark.parametrize(
    ("non_primary_heads", "sticky_blocks"),
    [
        ((), ""),
        ((2,), "[app] (head=2)\n  [Sticky]\t{yes}\n[end]\n"),
        (
            (3, 2),
            (
                "[app] (head=2)\n  [Sticky]\t{yes}\n[end]\n"
                "[app] (head=3)\n  [Sticky]\t{yes}\n[end]\n"
            ),
        ),
    ],
    ids=["one-screen", "two-screens", "three-screens"],
)
def test_non_primary_heads_block_repeats_in_place_per_head(
    non_primary_heads: tuple[int, ...], sticky_blocks: str
) -> None:
    rendered = _render(_STICKY_TEMPLATE, non_primary_heads=non_primary_heads)
    assert rendered.split("\n\n", 1)[1] == (
        "[app] (name=chrome-app)\n  [Sticky]\t{no}\n[end]\n"
        + sticky_blocks
        + "[app] (name=eog)\n  [Deco]\t{NORMAL}\n[end]\n"
    )


def _tracked_apps(layout: str, primary: int) -> str:
    root = _REPO / ".fluxbox/layouts"
    files = tuple(
        (path.relative_to(_REPO).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*.yaml"))
    )
    parsed = parse_layout(layout, files)
    screens = tuple(
        DisplayScreenSnapshot(
            f"OUT{index}",
            2560,
            1440,
            index * 2560,
            0,
            600,
            340,
            primary=index == primary,
        )
        for index in range(len(parsed.screens))
    )
    resolved = resolve_layout(parsed, screens)
    return render_fluxbox_apps(
        (_REPO / ".fluxbox/apps.in").read_bytes(),
        resolved.window_actions,
        non_primary_heads=tuple(
            screen.head for screen in resolved.screens if not screen.primary
        ),
        template_label=".fluxbox/apps.in",
    ).decode()


@pytest.mark.parametrize(
    ("layout", "primary", "laptop_head"),
    [("celtic+external", 1, 2), ("celtic+external-secondary", 0, 1)],
)
def test_agent_browser_lands_on_the_laptop_in_both_external_layouts(
    layout: str, primary: int, laptop_head: int
) -> None:
    rendered = _tracked_apps(layout, primary)
    block = rendered.split("[app] (name=google-chrome.*agent-browser-")[1]
    block = block.split("[end]")[0]
    assert f"[Head]\t{{{laptop_head}}}" in block
    assert "[FocusProtection]\t{Deny, Refuse}" in block


def test_new_btop_never_opens_on_the_hiding_workspace() -> None:
    rendered = _tracked_apps("celtic", 0)
    block = rendered.split("(Title=btop|")[1].split("[end]")[0]
    assert "[Workspace]" not in block
