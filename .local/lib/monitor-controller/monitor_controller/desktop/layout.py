# ruff: noqa: EM101, EM102, TRY003
"""Strict, bounded layout parsing and deterministic offline expansion.

Only the small YAML subset used by the tracked Fluxbox layouts is accepted.  In
particular, mappings and sequences are parsed structurally rather than by a
permissive YAML implementation, includes are expanded through injected bytes,
and every expansion budget is charged before output is retained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Final

MAX_LAYOUT_FILE_BYTES: Final = 256 * 1024
MAX_LAYOUT_TOTAL_BYTES: Final = 2 * 1024 * 1024
MAX_LAYOUT_FILES: Final = 128
MAX_INCLUDE_DEPTH: Final = 32
MAX_LAYOUT_SCREENS: Final = 16
MAX_LAYOUT_LINES: Final = 20_000
MAX_EXPANDED_NODES: Final = 20_000
MAX_WINDOW_RULES: Final = 4_096
MAX_WINDOW_COMMANDS: Final = 16_384
MAX_SCALAR_CHARS: Final = 16_384
_BLOCK_INDENT: Final = 2
_FIELD_INDENT: Final = 4
_LAYOUT_ROOT: Final = PurePosixPath(".fluxbox/layouts")
_INCLUDE = re.compile(r"^(?P<indent>[ ]*)<INCLUDE[ ]+(?P<path>[^<>]+?)>[ ]*$")
_KEY_VALUE = re.compile(
    r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?:[ ]*(?P<value>.*))?$"
)
_ANCHOR = re.compile(r"^&(?P<name>[A-Za-z_][A-Za-z0-9_-]*)[ ]+(?P<value>.+)$")
_ALIAS = re.compile(r"^\*(?P<name>[A-Za-z_][A-Za-z0-9_-]*)$")
_DECIMAL = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$")
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_]*>")
_SUBLAYOUT_KEY = re.compile(r"^(?P<key>[a-z0-9][a-z0-9-]*):$")
_LAYOUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")
_OUTPUT_NAME = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_HUNDRED: Final = Decimal(100)
_SCREEN_FIELDS: Final = frozenset(
    {
        "assignment",
        "bottom_margin",
        "col1_width_pc_of_active",
        "col2_width_pc_of_active",
        "col3_width_pc_of_active",
        "cols_1_2_margin",
        "cols_2_3_margin",
        "gkrellm_width",
        "head",
        "left",
        "left_margin",
        "logs_height_pc",
        "name",
        "panel_height",
        "right",
        "right_margin",
        "right_margin_OLD",
        "row1_height_pc_of_active",
        "row2_height_pc_of_active",
        "rows_1_2_margin",
        "scale",
        "single_height_pc_of_active",
        "single_width_pc_of_active",
        "top_margin",
    }
)
_STICK = "If {Matches (Stuck=no)} {Stick}"
_UNSTICK = "If {Matches (Stuck=yes)} {Stick}"


class LayoutPlanningError(ValueError):
    """Injected layout bytes cannot produce one safe deterministic layout."""


@dataclass(frozen=True, slots=True)
class DisplayScreenSnapshot:
    """One active output's immutable geometry from the admitted X snapshot."""

    output: str
    width: int
    height: int
    x: int
    y: int
    width_mm: int
    height_mm: int
    primary: bool

    def __post_init__(self) -> None:
        if _OUTPUT_NAME.fullmatch(self.output) is None:
            raise LayoutPlanningError("display output name is malformed")
        if self.width <= 0 or self.height <= 0:
            raise LayoutPlanningError("display screen dimensions must be positive")
        if self.width_mm < 0 or self.height_mm < 0:
            raise LayoutPlanningError("display physical dimensions cannot be negative")


@dataclass(frozen=True, slots=True)
class NamedString:
    """One normalized source-layout scalar retained in the staged plan."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name or not self.value:
            raise LayoutPlanningError("named source value must not be empty")


@dataclass(frozen=True, slots=True)
class NamedInteger:
    """One deterministic integer made available to Fluxbox layout expansion."""

    name: str
    value: int

    def __post_init__(self) -> None:
        if not self.name or self.name.isspace():
            raise LayoutPlanningError("geometry value name must not be empty")


@dataclass(frozen=True, slots=True)
class LayoutScreen:
    """Strict semantic screen declaration before observed geometry is applied."""

    name: str
    assignment: str | None
    head: int
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.name or self.name.isspace():
            raise LayoutPlanningError("layout screen name must not be empty")
        if self.assignment is not None and (
            not self.assignment or self.assignment.isspace()
        ):
            raise LayoutPlanningError("layout screen assignment must not be empty")
        if self.head <= 0:
            raise LayoutPlanningError("Fluxbox head numbers must be positive")
        keys = tuple(key for key, _value in self.parameters)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise LayoutPlanningError(
                "layout screen parameters must be sorted and unique"
            )

    def parameter(self, name: str) -> str | None:
        """Return one normalized scalar parameter if present."""
        return next((value for key, value in self.parameters if key == name), None)


@dataclass(frozen=True, slots=True)
class WindowRule:
    """One structurally parsed matcher and non-empty command array."""

    matcher: str
    commands: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_scalar(self.matcher, "window matcher")
        if not self.commands:
            raise LayoutPlanningError("window rule must contain at least one command")
        for command in self.commands:
            _bounded_scalar(command, "window command")


@dataclass(frozen=True, slots=True)
class ResolvedWindowAction:
    """Exact artifact-safe Fluxbox action requiring no later layout discovery."""

    matcher: str
    commands: tuple[str, ...]
    map_command: str

    def __post_init__(self) -> None:
        _bounded_scalar(self.matcher, "resolved window matcher")
        if not self.commands:
            raise LayoutPlanningError("resolved window action has no commands")
        _bounded_scalar(self.map_command, "resolved Fluxbox map command")
        if _PLACEHOLDER.search(self.matcher) or any(
            _PLACEHOLDER.search(item) for item in (*self.commands, self.map_command)
        ):
            raise LayoutPlanningError("resolved window action retains a placeholder")


@dataclass(frozen=True, slots=True)
class ResolvedScreen:
    """One layout screen paired with exact observed geometry and derived values."""

    number: int
    output: str
    name: str
    assignment: str
    head: int
    primary: bool
    x: int
    y: int
    width_mm: int
    height_mm: int
    source_parameters: tuple[NamedString, ...]
    geometry: tuple[NamedInteger, ...]

    def __post_init__(self) -> None:
        if self.number < 0:
            raise LayoutPlanningError("resolved screen number cannot be negative")
        names = tuple(item.name for item in self.geometry)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise LayoutPlanningError("resolved geometry must be sorted and unique")

    def value(self, name: str) -> int:
        """Return one required calculated geometry value."""
        for item in self.geometry:
            if item.name == name:
                return item.value
        raise LayoutPlanningError(f"resolved screen has no {name!r} geometry")


@dataclass(frozen=True, slots=True)
class ParsedLayout:
    """Expanded and strictly parsed layout independent of an observed display."""

    layout: str
    source_path: str
    consumed_paths: tuple[str, ...]
    expanded_yaml: str
    dpi: int | None
    ui_scale: str | None
    screens: tuple[LayoutScreen, ...]
    window_rules: tuple[WindowRule, ...]

    @property
    def window_rule_count(self) -> int:
        """Return the exact number of structurally parsed rules."""
        return len(self.window_rules)


@dataclass(frozen=True, slots=True)
class ResolvedLayout:
    """Complete immutable layout, geometry, and exact Fluxbox actions."""

    layout: str
    source_path: str
    consumed_paths: tuple[str, ...]
    expanded_yaml: str
    dpi: int | None
    ui_scale: str | None
    screens: tuple[ResolvedScreen, ...]
    window_actions: tuple[ResolvedWindowAction, ...]

    @property
    def window_rule_count(self) -> int:
        """Return the exact number of resolved window actions."""
        return len(self.window_actions)


@dataclass(slots=True)
class _ExpansionBudget:
    bytes: int = 0
    lines: int = 0

    def append(self, output: list[str], line: str) -> None:
        encoded_size = len(line.encode("utf-8"))
        if self.bytes + encoded_size > MAX_LAYOUT_TOTAL_BYTES:
            raise LayoutPlanningError("expanded layout exceeds the byte limit")
        if self.lines + 1 > MAX_LAYOUT_LINES:
            raise LayoutPlanningError("expanded layout exceeds the line limit")
        self.bytes += encoded_size
        self.lines += 1
        output.append(line)


@dataclass(slots=True)
class _ResolvedTextBudget:
    bytes: int = 0
    values: int = 0

    def charge(self, *items: str) -> None:
        encoded_size = sum(len(item.encode("utf-8")) for item in items)
        if self.bytes + encoded_size > MAX_LAYOUT_TOTAL_BYTES:
            raise LayoutPlanningError("resolved layout exceeds the byte limit")
        if self.values + len(items) > MAX_WINDOW_COMMANDS:
            raise LayoutPlanningError("resolved layout exceeds the value-count limit")
        self.bytes += encoded_size
        self.values += len(items)


def layout_path(layout: str) -> str:
    """Return the canonical logical path for an autorandr layout value."""
    clean = layout.strip()
    if not clean or "\x00" in clean or "\\" in clean:
        raise LayoutPlanningError("layout name is malformed")
    candidate = PurePosixPath(clean)
    if candidate.as_posix() != clean:
        raise LayoutPlanningError("layout name is not canonical")
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LayoutPlanningError("layout name escapes the layout root")
    if len(candidate.parts) == 1:
        if _LAYOUT_NAME.fullmatch(candidate.name) is None:
            raise LayoutPlanningError("layout name contains unsupported characters")
        candidate = _LAYOUT_ROOT / candidate
    elif candidate.parts[0] == "layouts":
        candidate = PurePosixPath(".fluxbox") / candidate
    elif candidate.parts[:2] != (".fluxbox", "layouts"):
        raise LayoutPlanningError("layout path is outside .fluxbox/layouts")
    if candidate.suffix != ".yaml":
        candidate = candidate.with_suffix(".yaml")
    return candidate.as_posix()


def configuration_include_paths(path: str, content: bytes) -> tuple[str, ...]:
    """Return direct canonical include dependencies from one captured file."""
    text = _decode_layout_file(path, content)
    lines = text.splitlines()
    if len(lines) > MAX_LAYOUT_LINES:
        raise LayoutPlanningError(f"layout input {path!r} exceeds its line limit")
    includes: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if "<INCLUDE" not in line:
            continue
        match = _INCLUDE.fullmatch(line)
        if match is None:
            raise LayoutPlanningError(
                f"{path}:{line_number}: malformed layout include directive"
            )
        includes.add(_include_path(match.group("path")))
    return tuple(sorted(includes))


def parse_layout(layout: str, files: tuple[tuple[str, bytes], ...]) -> ParsedLayout:
    """Expand recursive includes and parse the complete accepted grammar."""
    by_path = _validated_file_mapping(files)
    source_path = layout_path(layout)
    output: list[str] = []
    consumed: set[str] = set()
    _expand_file(
        source_path,
        by_path,
        stack=(),
        depth=0,
        inherited_indent="",
        output=output,
        consumed=consumed,
        budget=_ExpansionBudget(),
    )
    expanded = "".join(output)
    dpi, ui_scale, screens, window_rules = _parse_expanded(source_path, expanded)
    return ParsedLayout(
        layout=layout,
        source_path=source_path,
        consumed_paths=tuple(sorted(consumed)),
        expanded_yaml=expanded,
        dpi=dpi,
        ui_scale=ui_scale,
        screens=screens,
        window_rules=window_rules,
    )


def resolve_layout(
    parsed: ParsedLayout,
    display_screens: tuple[DisplayScreenSnapshot, ...],
) -> ResolvedLayout:
    """Pair left-to-right geometry with screens and resolve every action."""
    ordered = tuple(
        sorted(display_screens, key=lambda item: (item.x, item.y, item.output))
    )
    if len(ordered) != len(parsed.screens):
        display_count = len(ordered)
        layout_count = len(parsed.screens)
        raise LayoutPlanningError(
            f"display has {display_count} active screens but layout has {layout_count}"
        )
    if not ordered:
        raise LayoutPlanningError(
            "desktop planning requires at least one active screen"
        )
    if len({item.output for item in ordered}) != len(ordered):
        raise LayoutPlanningError("display snapshot contains duplicate outputs")
    if sum(item.primary for item in ordered) != 1:
        raise LayoutPlanningError(
            "display snapshot requires exactly one primary output"
        )

    resolved: list[ResolvedScreen] = []
    assignments: set[str] = set()
    heads: set[int] = set()
    for number, (screen, live) in enumerate(zip(parsed.screens, ordered, strict=True)):
        assignment = "primary" if len(ordered) == 1 else screen.assignment
        if assignment is None:
            assignment = screen.name
        if assignment in assignments:
            raise LayoutPlanningError("layout screen assignments must be unique")
        if screen.head in heads:
            raise LayoutPlanningError("layout Fluxbox head numbers must be unique")
        assignments.add(assignment)
        heads.add(screen.head)
        if (assignment == "primary") != live.primary:
            raise LayoutPlanningError(
                f"layout screen {screen.name!r} primary assignment differs from X"
            )
        resolved.append(_resolve_screen(number, screen, assignment, live))
    actions = _resolve_window_actions(parsed.window_rules, tuple(resolved))
    return ResolvedLayout(
        layout=parsed.layout,
        source_path=parsed.source_path,
        consumed_paths=parsed.consumed_paths,
        expanded_yaml=parsed.expanded_yaml,
        dpi=parsed.dpi,
        ui_scale=parsed.ui_scale,
        screens=tuple(resolved),
        window_actions=actions,
    )


def parse_sublayouts(  # noqa: C901, PLR0912 - strict stateful grammar
    content: bytes,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse the complete tracked sublayout mapping/command-array grammar."""
    text = _decode_layout_file(".fluxbox/sublayouts.yaml", content)
    result: list[tuple[str, tuple[str, ...]]] = []
    current_name: str | None = None
    commands: list[str] = []
    command_count = 0
    nodes = 0
    for line_number, original in enumerate(text.splitlines(), start=1):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        line = _without_comment(original)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            match = _SUBLAYOUT_KEY.fullmatch(line)
            if match is None:
                raise LayoutPlanningError(
                    f".fluxbox/sublayouts.yaml:{line_number}: malformed sublayout key"
                )
            if current_name is not None:
                if not commands:
                    raise LayoutPlanningError("sublayout command array is empty")
                result.append((current_name, tuple(commands)))
            current_name = match.group("key")
            commands = []
        elif indent == _BLOCK_INDENT and line.strip().startswith("- "):
            if current_name is None:
                raise LayoutPlanningError("sublayout command precedes its key")
            command = line.strip()[2:].strip()
            _plain_scalar(command, ".fluxbox/sublayouts.yaml", line_number)
            commands.append(command)
            command_count += 1
            if command_count > MAX_WINDOW_COMMANDS:
                raise LayoutPlanningError("sublayout command count exceeds its limit")
        else:
            raise LayoutPlanningError(
                f".fluxbox/sublayouts.yaml:{line_number}: malformed nesting"
            )
        nodes += 1
        if nodes > MAX_EXPANDED_NODES:
            raise LayoutPlanningError("sublayout expanded-node limit exceeded")
    if current_name is not None:
        if not commands:
            raise LayoutPlanningError("sublayout command array is empty")
        result.append((current_name, tuple(commands)))
    if not result or len({name for name, _commands in result}) != len(result):
        raise LayoutPlanningError("sublayout keys must be non-empty and unique")
    return tuple(result)


def resolve_sublayouts(
    parsed: tuple[tuple[str, tuple[str, ...]], ...],
    screens: tuple[ResolvedScreen, ...],
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    """Resolve each sublayout once per possible current screen, never from X later."""
    base = _placeholder_values(screens)
    budget = _ResolvedTextBudget()
    result: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
    for name, commands in parsed:
        per_screen: list[tuple[str, ...]] = []
        for current in screens:
            values = dict(base)
            values.update(_current_screen_placeholders(current))
            expanded = tuple(
                _expand_placeholders(command, values) for command in commands
            )
            budget.charge(*expanded)
            per_screen.append(expanded)
        result.append((name, tuple(per_screen)))
    return tuple(result)


def _validated_file_mapping(files: tuple[tuple[str, bytes], ...]) -> dict[str, bytes]:
    if not files or len(files) > MAX_LAYOUT_FILES:
        raise LayoutPlanningError("layout input file count is outside accepted bounds")
    total = 0
    result: dict[str, bytes] = {}
    for path, content in files:
        candidate = PurePosixPath(path)
        canonical = candidate.as_posix()
        if (
            canonical != path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.parts[:2] != (".fluxbox", "layouts")
            or candidate.suffix != ".yaml"
        ):
            raise LayoutPlanningError(
                "layout input path is not canonical beneath .fluxbox/layouts"
            )
        if path in result:
            raise LayoutPlanningError("layout input paths contain duplicates")
        if not content or len(content) > MAX_LAYOUT_FILE_BYTES:
            raise LayoutPlanningError(f"layout input {path!r} is empty or too large")
        total += len(content)
        result[path] = content
    if total > MAX_LAYOUT_TOTAL_BYTES:
        raise LayoutPlanningError("layout input bytes exceed the aggregate limit")
    return result


def _expand_file(  # noqa: PLR0913
    path: str,
    files: dict[str, bytes],
    *,
    stack: tuple[str, ...],
    depth: int,
    inherited_indent: str,
    output: list[str],
    consumed: set[str],
    budget: _ExpansionBudget,
) -> None:
    if depth > MAX_INCLUDE_DEPTH:
        raise LayoutPlanningError("layout include depth exceeds its limit")
    if path in stack:
        cycle = " -> ".join((*stack, path))
        raise LayoutPlanningError(f"layout include cycle: {cycle}")
    try:
        content = files[path]
    except KeyError as error:
        raise LayoutPlanningError(f"missing injected layout input: {path}") from error
    text = _decode_layout_file(path, content)
    consumed.add(path)
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        candidate = line.removesuffix("\n")
        if candidate.endswith("\r"):
            raise LayoutPlanningError(f"{path}:{line_number}: CRLF is not accepted")
        if "<INCLUDE" not in candidate:
            budget.append(output, inherited_indent + line)
            continue
        match = _INCLUDE.fullmatch(candidate)
        if match is None:
            raise LayoutPlanningError(
                f"{path}:{line_number}: malformed layout include directive"
            )
        _expand_file(
            _include_path(match.group("path")),
            files,
            stack=(*stack, path),
            depth=depth + 1,
            inherited_indent=inherited_indent + match.group("indent"),
            output=output,
            consumed=consumed,
            budget=budget,
        )


def _decode_layout_file(path: str, content: bytes) -> str:
    if len(content) > MAX_LAYOUT_FILE_BYTES:
        raise LayoutPlanningError(f"layout input {path!r} exceeds its size limit")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LayoutPlanningError(f"layout input {path!r} is not UTF-8") from error
    if "\x00" in text or "\t" in text:
        raise LayoutPlanningError(f"layout input {path!r} contains NUL or tab")
    if len(text.splitlines()) > MAX_LAYOUT_LINES:
        raise LayoutPlanningError(f"layout input {path!r} exceeds its line limit")
    return text


def _include_path(value: str) -> str:
    clean = value.strip()
    candidate = PurePosixPath(clean)
    if (
        not clean
        or candidate.as_posix() != clean
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
        or "\\" in clean
        or candidate.parts[:2] == (".fluxbox", "layouts")
    ):
        raise LayoutPlanningError("layout include path is malformed")
    if candidate.suffix != ".yaml":
        candidate = candidate.with_suffix(".yaml")
    return (_LAYOUT_ROOT / candidate).as_posix()


def _parse_expanded(  # noqa: C901, PLR0912, PLR0915
    path: str, expanded: str
) -> tuple[int | None, str | None, tuple[LayoutScreen, ...], tuple[WindowRule, ...]]:
    section: str | None = None
    seen_sections: set[str] = set()
    dpi: int | None = None
    ui_scale: str | None = None
    screens: list[dict[str, str]] = []
    current_screen: dict[str, str] | None = None
    anchors: dict[str, str] = {}
    windows: list[WindowRule] = []
    current_matcher: str | None = None
    current_commands: list[str] = []
    nodes = 0
    commands = 0

    def finish_screen() -> None:
        nonlocal current_screen
        if current_screen is not None:
            screens.append(current_screen)
            current_screen = None

    def finish_window() -> None:
        nonlocal current_matcher, current_commands
        if current_matcher is not None:
            windows.append(WindowRule(current_matcher, tuple(current_commands)))
            current_matcher = None
            current_commands = []

    for line_number, original in enumerate(expanded.splitlines(), start=1):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        line = _without_comment(original)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        nodes += 1
        if nodes > MAX_EXPANDED_NODES:
            raise LayoutPlanningError("expanded layout exceeds the node limit")
        if indent == 0:
            match = _KEY_VALUE.fullmatch(line)
            if match is None:
                raise LayoutPlanningError(
                    f"{path}:{line_number}: malformed top-level line"
                )
            key = match.group("key")
            value = (match.group("value") or "").strip()
            if key in {"screens", "windows"}:
                if value:
                    raise LayoutPlanningError(
                        f"{path}:{line_number}: {key} must be a block"
                    )
                if key in seen_sections:
                    raise LayoutPlanningError(f"layout repeats {key}")
                if key == "screens" and section is not None:
                    raise LayoutPlanningError("screens must precede windows")
                if key == "windows" and section != "screens":
                    raise LayoutPlanningError("windows must follow screens")
                finish_screen()
                seen_sections.add(key)
                section = key
                continue
            if section is not None:
                raise LayoutPlanningError(
                    f"{path}:{line_number}: scalar appears after blocks"
                )
            if key == "dpi":
                if dpi is not None:
                    raise LayoutPlanningError("layout repeats dpi")
                dpi = _positive_integer(value, "layout dpi")
            elif key == "ui_scale":
                if ui_scale is not None:
                    raise LayoutPlanningError("layout repeats ui_scale")
                ui_scale = _positive_decimal(value, "layout ui_scale")
            else:
                raise LayoutPlanningError(f"unsupported top-level layout key: {key}")
            continue

        if section == "screens":
            if indent == _BLOCK_INDENT and stripped == "-":
                finish_screen()
                if len(screens) >= MAX_LAYOUT_SCREENS:
                    raise LayoutPlanningError("layout screen count exceeds its limit")
                current_screen = {}
                continue
            if current_screen is None or indent != _FIELD_INDENT:
                raise LayoutPlanningError(
                    f"{path}:{line_number}: malformed screen list indentation"
                )
            match = _KEY_VALUE.fullmatch(line)
            if match is None or len(match.group("indent")) != _FIELD_INDENT:
                raise LayoutPlanningError(
                    f"{path}:{line_number}: malformed screen field"
                )
            key = match.group("key")
            if key not in _SCREEN_FIELDS:
                raise LayoutPlanningError(f"unsupported layout screen field: {key}")
            value = (match.group("value") or "").strip()
            if not value:
                raise LayoutPlanningError(f"{path}:{line_number}: empty screen field")
            value = _resolve_anchor(value, anchors, path, line_number)
            # Tracked includes deliberately provide defaults which the parent then
            # overrides.  This is the sole accepted duplicate-key behavior.
            current_screen[key] = value
            continue

        if section == "windows":
            if indent == _BLOCK_INDENT and stripped.startswith("- - "):
                finish_window()
                if len(windows) >= MAX_WINDOW_RULES:
                    raise LayoutPlanningError(
                        "layout window-rule count exceeds its limit"
                    )
                matcher = stripped[4:].strip()
                _plain_scalar(matcher, path, line_number)
                current_matcher = matcher
                continue
            if indent == _FIELD_INDENT and stripped.startswith("- "):
                if current_matcher is None:
                    raise LayoutPlanningError(
                        f"{path}:{line_number}: window command precedes matcher"
                    )
                command = stripped[2:].strip()
                _plain_scalar(command, path, line_number)
                current_commands.append(command)
                commands += 1
                if commands > MAX_WINDOW_COMMANDS:
                    raise LayoutPlanningError(
                        "layout window-command count exceeds its limit"
                    )
                continue
            raise LayoutPlanningError(
                f"{path}:{line_number}: malformed windows nesting"
            )
        raise LayoutPlanningError(f"{path}:{line_number}: content precedes a block")

    finish_screen()
    finish_window()
    if not screens:
        raise LayoutPlanningError("layout has no screens")
    if seen_sections != {"screens", "windows"}:
        raise LayoutPlanningError("layout requires screens and windows sections")
    if not windows:
        raise LayoutPlanningError("layout has no window rules")
    return (
        dpi,
        ui_scale,
        tuple(_screen_from_mapping(item) for item in screens),
        tuple(windows),
    )


def _without_comment(line: str) -> str:
    marker = re.search(r"[ ]+#", line)
    return line if marker is None else line[: marker.start()].rstrip()


def _resolve_anchor(
    value: str, anchors: dict[str, str], path: str, line_number: int
) -> str:
    anchor = _ANCHOR.fullmatch(value)
    if anchor is not None:
        name = anchor.group("name")
        resolved = anchor.group("value").strip()
        if not resolved or name in anchors:
            raise LayoutPlanningError(
                f"{path}:{line_number}: invalid or repeated YAML anchor"
            )
        _plain_scalar(resolved, path, line_number, aliases_allowed=True)
        anchors[name] = resolved
        return resolved
    alias = _ALIAS.fullmatch(value)
    if alias is not None:
        try:
            return anchors[alias.group("name")]
        except KeyError as error:
            raise LayoutPlanningError(
                f"{path}:{line_number}: unknown YAML alias {value}"
            ) from error
    _plain_scalar(value, path, line_number, aliases_allowed=True)
    if value.startswith(("&", "*")) or " <<:" in value:
        raise LayoutPlanningError(
            f"{path}:{line_number}: unsupported YAML alias syntax"
        )
    return value


def _plain_scalar(
    value: str,
    path: str,
    line_number: int,
    *,
    aliases_allowed: bool = False,
) -> None:
    _bounded_scalar(value, f"{path}:{line_number}: scalar")
    if (
        value in {"null", "Null", "NULL", "~", "[]", "{}", "|", ">", "---", "..."}
        or value.startswith(('"', "'", "[", "{", "!", "|", ">", "@", "`"))
        or ": " in value
    ):
        raise LayoutPlanningError(
            f"{path}:{line_number}: non-plain scalar is forbidden"
        )
    if not aliases_allowed and (value.startswith(("&", "*")) or "<<:" in value):
        raise LayoutPlanningError(f"{path}:{line_number}: aliases are forbidden here")


def _bounded_scalar(value: str, field: str) -> None:
    if not value or value.isspace() or "\x00" in value or len(value) > MAX_SCALAR_CHARS:
        raise LayoutPlanningError(f"{field} must be bounded non-empty text")


def _screen_from_mapping(values: dict[str, str]) -> LayoutScreen:
    try:
        name = values["name"]
        head = _positive_integer(values["head"], "screen head")
    except KeyError as error:
        raise LayoutPlanningError(f"layout screen lacks {error.args[0]!r}") from error
    assignment = values.get("assignment")
    return LayoutScreen(name, assignment, head, tuple(sorted(values.items())))


def _resolve_screen(  # noqa: PLR0915
    number: int, layout: LayoutScreen, assignment: str, live: DisplayScreenSnapshot
) -> ResolvedScreen:
    width = live.width
    height = live.height
    left_margin = _dimension(layout.parameter("left_margin") or "0", width)
    right_margin = _dimension(layout.parameter("right_margin") or "0", width)
    top_margin = _dimension(layout.parameter("top_margin") or "0", height)
    bottom_margin = _dimension(layout.parameter("bottom_margin") or "0", height)
    panel_height = _nonnegative_integer(
        layout.parameter("panel_height") or "0", "panel_height"
    )
    logs_height_pc = _decimal(
        layout.parameter("logs_height_pc") or "0", "logs_height_pc"
    )
    cols_1_2_margin = _dimension(layout.parameter("cols_1_2_margin") or "0", width)
    cols_2_3_margin = _dimension(
        layout.parameter("cols_2_3_margin")
        or layout.parameter("cols_1_2_margin")
        or "0",
        width,
    )
    rows_1_2_margin = _dimension(layout.parameter("rows_1_2_margin") or "0", height)
    logs_height = _decimal_to_int(logs_height_pc * height / 100, "logs_height")
    active_width = width - left_margin - right_margin
    # Preserve liblayout's historic behavior: bottom_margin is retained for
    # placeholder expansion but does not reduce the active vertical region.
    active_height = height - top_margin - panel_height - logs_height
    if active_width <= 0 or active_height <= 0:
        raise LayoutPlanningError("layout margins leave no active screen area")

    values: dict[str, int] = {
        "width": width,
        "height": height,
        "x_offset": live.x,
        "y_offset": live.y,
        "right": live.x + width,
        "num": number,
        "head": layout.head,
        "left_margin": left_margin,
        "right_margin": right_margin,
        "top_margin": top_margin,
        "bottom_margin": bottom_margin,
        "panel_height": panel_height,
        "logs_height_pc": _decimal_to_int(logs_height_pc, "logs_height_pc"),
        "logs_height": logs_height,
        "cols_1_2_margin": cols_1_2_margin,
        "cols_2_3_margin": cols_2_3_margin,
        "rows_1_2_margin": rows_1_2_margin,
        "active_left": left_margin,
        "active_top": top_margin,
        "active_width": active_width,
        "active_height": active_height,
        "active_width_pc": _percent(active_width, width),
        "active_height_pc": _percent(active_height, height),
        "active_middle_x": left_margin + active_width // 2,
        "active_middle_y": top_margin + active_height // 2,
        "full_left": 0,
        "full_top": 0,
        "full_width": width,
        "full_height": height - panel_height,
    }
    single_width_pc = _decimal(
        layout.parameter("single_width_pc_of_active") or "100",
        "single_width_pc_of_active",
    )
    single_height_pc = _decimal(
        layout.parameter("single_height_pc_of_active") or "100",
        "single_height_pc_of_active",
    )
    if not (0 < single_width_pc <= _HUNDRED and 0 < single_height_pc <= _HUNDRED):
        raise LayoutPlanningError("single-window percentages must be within (0, 100]")
    single_width = _decimal_to_int(single_width_pc * active_width / 100, "single_width")
    single_height = _decimal_to_int(
        single_height_pc * active_height / 100, "single_height"
    )
    values.update(
        single_width_pc_of_active=_decimal_to_int(
            single_width_pc, "single_width_pc_of_active"
        ),
        single_height_pc_of_active=_decimal_to_int(
            single_height_pc, "single_height_pc_of_active"
        ),
        single_width=single_width,
        single_height=single_height,
        single_left=left_margin + (active_width - single_width) // 2,
        single_top=top_margin + (active_height - single_height) // 2,
        single_middle_x=values["active_middle_x"],
        single_middle_y=values["active_middle_y"],
    )
    col12_margin_pc = _percent(cols_1_2_margin, active_width)
    col23_margin_pc = _percent(cols_2_3_margin, active_width)
    row_margin_pc = _percent(rows_1_2_margin, active_height)
    col1_pc = _decimal(
        layout.parameter("col1_width_pc_of_active") or "50", "col1_width_pc_of_active"
    )
    col3_pc = _decimal(
        layout.parameter("col3_width_pc_of_active") or "0", "col3_width_pc_of_active"
    )
    col2_default = (
        _HUNDRED - col1_pc - col3_pc - col12_margin_pc - col23_margin_pc
        if col3_pc
        else _HUNDRED - col1_pc - col12_margin_pc
    )
    col2_pc = _decimal(
        layout.parameter("col2_width_pc_of_active") or str(col2_default),
        "col2_width_pc_of_active",
    )
    row1_pc = _decimal(
        layout.parameter("row1_height_pc_of_active") or "50", "row1_height_pc_of_active"
    )
    row2_default = _HUNDRED - row1_pc - row_margin_pc
    row2_pc = _decimal(
        layout.parameter("row2_height_pc_of_active") or str(row2_default),
        "row2_height_pc_of_active",
    )
    for name, value in (
        ("col1_width_pc_of_active", col1_pc),
        ("col2_width_pc_of_active", col2_pc),
        ("col3_width_pc_of_active", col3_pc),
        ("row1_height_pc_of_active", row1_pc),
        ("row2_height_pc_of_active", row2_pc),
    ):
        if value < 0 or value > _HUNDRED:
            raise LayoutPlanningError(f"{name} must be within [0, 100]")
    col1_width = _decimal_to_int(col1_pc * active_width / 100, "col1_width")
    col2_width = _decimal_to_int(col2_pc * active_width / 100, "col2_width")
    col3_width = _decimal_to_int(col3_pc * active_width / 100, "col3_width")
    horizontal_gutters = cols_1_2_margin + (cols_2_3_margin if col3_width else 0)
    if col1_width + col2_width + col3_width + horizontal_gutters > active_width:
        raise LayoutPlanningError("column widths and gutters exceed active width")
    col1_left = left_margin
    col1_right = col1_left + col1_width
    col2_left = col1_right + cols_1_2_margin
    col2_right = col2_left + col2_width
    col3_left = col2_right + cols_2_3_margin
    col3_right = col3_left + col3_width
    row1_height = _decimal_to_int(row1_pc * active_height / 100, "row1_height")
    # Preserve liblayout's historic truncation/row calculation exactly.  The
    # separate row2 percentage is retained as input evidence, but the emitted
    # geometry has always used row1's percentage for row2.
    row2_height = _decimal_to_int(row1_pc * active_height / 100, "row2_height")
    if row1_height + rows_1_2_margin + row2_height > active_height:
        raise LayoutPlanningError("legacy row heights and gutter exceed active height")
    row1_top = top_margin
    row1_bottom = row1_top + row1_height
    row2_top = row1_bottom + rows_1_2_margin
    row2_bottom = row2_top + row2_height
    values.update(
        cols_1_2_margin_pc_of_active=col12_margin_pc,
        cols_2_3_margin_pc_of_active=col23_margin_pc,
        rows_1_2_margin_pc_of_active=row_margin_pc,
        col1_width_pc_of_active=_decimal_to_int(col1_pc, "col1_width_pc_of_active"),
        col2_width_pc_of_active=_decimal_to_int(col2_pc, "col2_width_pc_of_active"),
        col3_width_pc_of_active=_decimal_to_int(col3_pc, "col3_width_pc_of_active"),
        row1_height_pc_of_active=_decimal_to_int(row1_pc, "row1_height_pc_of_active"),
        row2_height_pc_of_active=_decimal_to_int(row2_pc, "row2_height_pc_of_active"),
        col1_width=col1_width,
        col2_width=col2_width,
        col3_width=col3_width,
        col1_left=col1_left,
        col1_right=col1_right,
        col2_left=col2_left,
        col2_right=col2_right,
        col3_left=col3_left,
        col3_right=col3_right,
        col1_middle=col1_left + col1_width // 2,
        col2_middle=col2_left + col2_width // 2,
        col3_middle=col3_left + col3_width // 2,
        cols_1_2_width=col2_right - col1_left,
        cols_1_2_middle=col1_left + (col2_right - col1_left) // 2,
        row1_height=row1_height,
        row2_height=row2_height,
        row1_top=row1_top,
        row1_bottom=row1_bottom,
        row2_top=row2_top,
        row2_bottom=row2_bottom,
        row1_middle=row1_top + row1_height // 2,
        row2_middle=row2_top + row2_height // 2,
    )
    return ResolvedScreen(
        number=number,
        output=live.output,
        name=layout.name,
        assignment=assignment,
        head=layout.head,
        primary=live.primary,
        x=live.x,
        y=live.y,
        width_mm=live.width_mm,
        height_mm=live.height_mm,
        source_parameters=tuple(
            NamedString(name, value) for name, value in layout.parameters
        ),
        geometry=tuple(
            NamedInteger(name, value) for name, value in sorted(values.items())
        ),
    )


def _placeholder_values(screens: tuple[ResolvedScreen, ...]) -> dict[str, str]:
    values = {"<stick>": _STICK, "<unstick>": _UNSTICK}
    for screen in screens:
        fields = {item.name: item.value for item in screen.geometry}
        text_fields: dict[str, str] = {
            **{item.name: item.value for item in screen.source_parameters},
            **{name: str(value) for name, value in fields.items()},
            "SetHead": f"SetHead {screen.head}",
            "assignment": screen.assignment,
            "name": screen.name,
            "output": screen.output,
        }
        for prefix in (f"s{screen.number}_", f"s_{screen.assignment}_"):
            for name, value in text_fields.items():
                rendered = value + ("%" if name.endswith("_pc") else "")
                values[f"<{prefix}{name}>"] = rendered
    primary = next((item for item in screens if item.assignment == "primary"), None)
    if primary is None:
        raise LayoutPlanningError("resolved layout has no primary assignment")
    if primary.value("col3_width") > 0:
        col3_left = primary.value("col3_left")
        row1_top = primary.value("row1_top")
        col3_width = primary.value("col3_width")
        active_height = primary.value("active_height")
        values["<place_RHS>"] = (
            f"MacroCmd {{SetHead {primary.head}}} "
            f"{{MoveTo {col3_left} {row1_top}}} "
            f"{{ResizeTo {col3_width} {active_height}}} "
            f"{{{_STICK}}}"
        )
    else:
        secondary = next(
            (item for item in screens if item.assignment == "secondary"), None
        )
        if secondary is not None:
            values["<place_RHS>"] = (
                f"MacroCmd {{SetHead {secondary.head}}} {{{_STICK}}}"
            )
    return values


def _current_screen_placeholders(screen: ResolvedScreen) -> dict[str, str]:
    fields = {item.name: item.value for item in screen.geometry}
    values = {item.name: item.value for item in screen.source_parameters}
    values.update({name: str(value) for name, value in fields.items()})
    values["SetHead"] = f"SetHead {screen.head}"
    return {
        f"<sX_{name}>": value + ("%" if name.endswith("_pc") else "")
        for name, value in values.items()
    }


def _resolve_window_actions(
    rules: tuple[WindowRule, ...], screens: tuple[ResolvedScreen, ...]
) -> tuple[ResolvedWindowAction, ...]:
    values = _placeholder_values(screens)
    budget = _ResolvedTextBudget()
    result: list[ResolvedWindowAction] = []
    for rule in rules:
        matcher = _expand_placeholders(rule.matcher, values)
        commands = tuple(
            _expand_placeholders(command, values) for command in rule.commands
        )
        combined = _combine_commands(commands)
        map_command = f"Map {{{combined}}} {{Matches {matcher}}}"
        budget.charge(matcher, *commands, map_command)
        result.append(
            ResolvedWindowAction(
                matcher=matcher,
                commands=commands,
                map_command=map_command,
            )
        )
    return tuple(result)


def _expand_placeholders(value: str, replacements: dict[str, str]) -> str:
    result = value
    for placeholder in sorted(replacements, key=lambda item: (-len(item), item)):
        result = result.replace(placeholder, replacements[placeholder])
    unresolved = _PLACEHOLDER.search(result)
    if unresolved is not None:
        raise LayoutPlanningError(f"unknown layout placeholder: {unresolved.group(0)}")
    _bounded_scalar(result, "expanded Fluxbox scalar")
    return result


def _combine_commands(commands: tuple[str, ...]) -> str:
    if len(commands) == 1:
        return commands[0]
    parts = [
        command.removeprefix("MacroCmd ")
        if command.startswith("MacroCmd ")
        else f"{{{command}}}"
        for command in commands
    ]
    return f"MacroCmd {' '.join(parts)}"


def _dimension(value: str, reference: int) -> int:
    if value.endswith("%"):
        percentage = _decimal(value[:-1], "percentage dimension")
        if percentage < 0 or percentage > _HUNDRED:
            raise LayoutPlanningError("percentage dimension must be within [0, 100]")
        return _decimal_to_int(percentage * reference / 100, "percentage dimension")
    return _nonnegative_integer(value, "pixel dimension")


def _positive_integer(value: str, field: str) -> int:
    result = _nonnegative_integer(value, field)
    if result == 0:
        raise LayoutPlanningError(f"{field} must be positive")
    return result


def _nonnegative_integer(value: str, field: str) -> int:
    if _INTEGER.fullmatch(value) is None:
        raise LayoutPlanningError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise LayoutPlanningError(f"{field} must be an integer") from error
    if result < 0:
        raise LayoutPlanningError(f"{field} cannot be negative")
    return result


def _decimal(value: str, field: str) -> Decimal:
    if _DECIMAL.fullmatch(value) is None:
        raise LayoutPlanningError(f"{field} must be a plain decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise LayoutPlanningError(f"{field} is not a decimal") from error
    if not result.is_finite():
        raise LayoutPlanningError(f"{field} must be finite")
    return result


def _decimal_to_int(value: Decimal, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LayoutPlanningError(f"{field} is outside the integer range") from error
    if not -(2**63) <= result < 2**63:
        raise LayoutPlanningError(f"{field} is outside the signed 64-bit range")
    return result


def _positive_decimal(value: str, field: str) -> str:
    result = _decimal(value, field)
    if result <= 0:
        raise LayoutPlanningError(f"{field} must be positive")
    return _format_decimal(result)


def _format_decimal(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _percent(value: int, reference: int) -> int:
    if reference <= 0:
        raise LayoutPlanningError("percentage reference must be positive")
    return round(Fraction(value * 100, reference))
