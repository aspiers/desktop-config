#!/usr/bin/env python3
"""Live compatibility adapters for the canonical desktop layout planner.

Geometry and placeholder expansion live exclusively in
``monitor_controller.desktop.layout``.  This module supplies filesystem and
XRandR adapters for older interactive scripts which still need live layout
queries outside the controller.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.append(os.path.join(os.getenv("HOME", ""), "lib"))

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / ".local/lib/monitor-controller"
if _SOURCE_ROOT.is_dir():
    sys.path.insert(0, str(_SOURCE_ROOT))

from monitor_controller.desktop.layout import (  # noqa: E402
    DisplayScreenSnapshot,
    LayoutPlanningError,
    ParsedLayout,
    ResolvedScreen,
    configuration_include_paths,
    layout_path,
    parse_layout,
    resolve_layout,
)

_LAYOUT_ROOT = Path(".fluxbox/layouts")
libdpy = importlib.import_module("libdpy")


def get_layout_file(
    layout_name_or_path: str,
    directory: str = os.path.expanduser("~/.fluxbox/layouts"),
) -> str:
    """Resolve a layout name using the historic interactive-script convention."""
    if os.path.isabs(layout_name_or_path):
        return layout_name_or_path
    suffix = "" if layout_name_or_path.endswith(".yaml") else ".yaml"
    return os.path.join(directory, layout_name_or_path + suffix)


def get_sublayout_file() -> str:
    return os.path.expanduser("~/.fluxbox/sublayouts.yaml")


def _logical_layout_name(path: Path) -> str:
    return path.stem


def _read_layout_files(root: Path) -> tuple[tuple[str, bytes], ...]:
    """Capture one layout and its includes under canonical logical paths."""
    captured: dict[str, bytes] = {}
    pending = [layout_path(_logical_layout_name(root))]
    directory = root.parent
    while pending:
        logical = pending.pop()
        if logical in captured:
            continue
        relative = Path(logical).relative_to(_LAYOUT_ROOT)
        actual = root if not captured else directory / relative
        content = actual.read_bytes()
        captured[logical] = content
        pending.extend(configuration_include_paths(logical, content))
    return tuple(sorted(captured.items()))


def _parse_layout_file(path: str | Path) -> ParsedLayout:
    root = Path(path).expanduser()
    return parse_layout(_logical_layout_name(root), _read_layout_files(root))


def load_layout(layout_name_or_path: str, directory: str | None = None) -> ParsedLayout:
    """Parse a layout through the canonical bounded grammar."""
    root = get_layout_file(
        layout_name_or_path,
        directory or os.path.expanduser("~/.fluxbox/layouts"),
    )
    return _parse_layout_file(root)


def count_layout_screens(
    layout_name_or_path: str,
    directory: str = os.path.expanduser("~/.fluxbox/layouts"),
) -> int:
    return len(load_layout(layout_name_or_path, directory).screens)


def _integer_field(
    screen: dict[str, Any], name: str, fallback: str | None = None
) -> int:
    try:
        key = name if name in screen or fallback is None else fallback
        value = screen[key]
        return int(value)
    except (KeyError, TypeError, ValueError) as error:
        raise LayoutPlanningError(f"XRandR screen has invalid {name!r}") from error


def _display_screens(
    raw_screens: list[dict[str, Any]],
) -> tuple[DisplayScreenSnapshot, ...]:
    snapshots: list[DisplayScreenSnapshot] = []
    for screen in raw_screens:
        snapshots.append(
            DisplayScreenSnapshot(
                output=str(screen.get("output") or screen.get("name") or "unknown"),
                width=_integer_field(screen, "width"),
                height=_integer_field(screen, "height"),
                x=_integer_field(screen, "x_offset"),
                y=_integer_field(screen, "y_offset"),
                width_mm=_integer_field(screen, "width_mm", "x_mm"),
                height_mm=_integer_field(screen, "height_mm", "y_mm"),
                primary=bool(screen.get("primary")),
            )
        )
    return tuple(snapshots)


def _screen_dict(screen: ResolvedScreen) -> dict[str, Any]:
    source = {item.name: item.value for item in screen.source_parameters}
    result: dict[str, Any] = dict(source)
    result.update({item.name: item.value for item in screen.geometry})
    for direction in ("left", "right"):
        if direction in source:
            result[direction] = source[direction]
    result.update(
        output=screen.output,
        name=screen.name,
        assignment=screen.assignment,
        head=screen.head,
        num=screen.number,
        primary=screen.primary,
        SetHead=f"SetHead {screen.head}",
    )
    return result


def get_layout_params(
    layout_file: str,
    use_cache: bool = False,
    screens_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve live geometry and return the historic script-facing shape."""
    parsed = _parse_layout_file(layout_file)
    raw_screens = libdpy.get_xrandr_screen_geometries(use_cache=use_cache)
    if screens_only:
        if len(raw_screens) != len(parsed.screens):
            raise LayoutPlanningError(
                f"display has {len(raw_screens)} active screens but layout has "
                f"{len(parsed.screens)}"
            )
        return [], {"screens": len(parsed.screens), "windows": []}
    resolved = resolve_layout(parsed, _display_screens(raw_screens))
    screens = [_screen_dict(screen) for screen in resolved.screens]
    windows = [[action.matcher, *action.commands] for action in resolved.window_actions]
    return screens, {"screens": screens, "windows": windows}


def get_adjacent_screen(
    direction: str,
    layout_name_or_path: str | None = None,
    use_cache: bool = False,
    current_x: float | None = None,
) -> dict[str, Any] | None:
    """Return the named layout neighbour of the current screen."""
    if direction not in ("left", "right"):
        raise ValueError("direction must be either 'left' or 'right'")
    if not layout_name_or_path:
        raise ValueError("layout_name_or_path must be provided")
    try:
        current = (
            libdpy.get_screen(int(current_x), use_cache=use_cache)
            if current_x is not None
            else libdpy.get_current_screen_info(use_cache=use_cache)
        )
    except (TypeError, ValueError) as error:
        raise LayoutPlanningError("current X coordinate is invalid") from error
    screens, _ = get_layout_params(
        get_layout_file(layout_name_or_path), use_cache=use_cache
    )
    number = _integer_field(current, "num")
    if not 0 <= number < len(screens):
        return None
    adjacent_name = screens[number].get(direction)
    if adjacent_name is None:
        return None
    return next(
        (
            screen
            for screen in screens
            if adjacent_name in (screen.get("name"), screen.get("assignment"))
        ),
        None,
    )


def get_screen_by_position(
    position: int, layout_name_or_path: str, use_cache: bool = False
) -> dict[str, Any] | None:
    """Return a screen by one-based left-to-right physical position."""
    screens, _ = get_layout_params(
        get_layout_file(layout_name_or_path), use_cache=use_cache
    )
    index = position - 1
    return screens[index] if 0 <= index < len(screens) else None


def _default_layout() -> str:
    get_layout = Path(__file__).resolve().parent.parent / "bin/get-layout"
    completed = subprocess.run(
        (str(get_layout),), capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse and validate layout files")
    parser.add_argument("--check-screen-counts", action="store_true")
    parser.add_argument("layout_name", nargs="?")
    args = parser.parse_args()
    layout_file = (
        get_layout_file(args.layout_name) if args.layout_name else _default_layout()
    )
    screens, layout = get_layout_params(
        layout_file, screens_only=args.check_screen_counts
    )
    print(json.dumps({"screens": screens, "layout": layout}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LayoutPlanningError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
