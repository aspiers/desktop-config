# ruff: noqa: C901, EM101, EM102, PLR0911, PLR0912, TRY003
"""Closed deterministic renderer for the tracked Fluxbox keys template."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

_PRELUDE_SHA256: Final = (
    "e3da53410fd705f3975056efdb8fe36bc46b151c12c89836b454625fd6f73a4b"
)
_TAG = re.compile(r"<%(=)?(.*?)%>", re.DOTALL)
_HOST_CONDITION: Final = "if false && %w(aegean).include?(ENV['localhost_nickname'])"
_MONITOR_CONDITION: Final = "if monitors_connected.to_i > 1"
_PREFIX_LOOP: Final = "['', 'reorg: '].each do |prefix|"
_PREFIX_VALUES: Final = ("", "reorg: ")
# Characters which would change how a rendered fluxbox command parses: shell
# substitution and chaining metacharacters, quoting, braces, and control
# bytes. The template is a tracked repository file, so this guards against a
# malformed edit rather than an adversary; exact argument values are proven
# against real erb by the renderer parity test, not pinned here.
_UNSAFE_HELPER_CHARACTERS: Final = frozenset('$`"\';&{}\\') | frozenset(
    chr(value) for value in range(0x20)
)
_MODE_NAME: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_DELAY_MS: Final = 600_000


class FluxboxRenderError(ValueError):
    """The template contains syntax outside the closed tracked grammar."""


@dataclass(frozen=True, slots=True)
class _Text:
    value: str


@dataclass(frozen=True, slots=True)
class _Expression:
    value: str


@dataclass(frozen=True, slots=True)
class _If:
    condition: str
    body: tuple[_Node, ...]


@dataclass(frozen=True, slots=True)
class _Prefixes:
    body: tuple[_Node, ...]


type _Node = _Text | _Expression | _If | _Prefixes


def render_fluxbox_keys(
    template: bytes,
    *,
    monitor_count: int,
    host_name: str,
    template_label: str,
    generator_label: str,
) -> bytes:
    """Render only the allowlisted tracked ERB grammar to exact UTF-8 bytes."""
    if monitor_count <= 0:
        raise FluxboxRenderError("monitor count must be positive")
    if not host_name or host_name.isspace() or "\x00" in host_name:
        raise FluxboxRenderError("host name must be bounded non-empty text")
    for value, field in (
        (template_label, "template label"),
        (generator_label, "generator label"),
    ):
        if not value or value.isspace() or "\x00" in value or "\n" in value:
            raise FluxboxRenderError(f"{field} must be one line")
    try:
        source = template.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise FluxboxRenderError("Fluxbox template is not UTF-8") from error
    tokens = _tokens(source)
    if not tokens or not isinstance(tokens[0], _Expression):
        raise FluxboxRenderError("Fluxbox template lacks its allowlisted prelude")
    prelude = tokens[0].value.encode("utf-8")
    if hashlib.sha256(prelude).hexdigest() != _PRELUDE_SHA256:
        raise FluxboxRenderError("Fluxbox helper prelude differs from the allowlist")
    nodes, index = _parse(tokens, 1, nested=False)
    if index != len(tokens):
        raise FluxboxRenderError("Fluxbox template has an unexpected trailing block")
    body = _render(nodes, monitor_count=monitor_count, host_name=host_name, prefix=None)
    warning = (
        "# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "# WARNING: This file is auto-generated. DO NOT EDIT IT MANUALLY.\n"
        "# Edit the template instead:\n"
        f"#   {template_label}\n"
        "# Then regenerate by running:\n"
        f"#   {generator_label}\n"
        "# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    )
    return (warning + body).encode("utf-8")


def _tokens(source: str) -> tuple[_Text | _Expression, ...]:
    values: list[_Text | _Expression] = []
    position = 0
    for match in _TAG.finditer(source):
        if match.start() != position:
            text = source[position : match.start()]
            if "<%" in text or "%>" in text:
                raise FluxboxRenderError(
                    "Fluxbox template has malformed ERB delimiters"
                )
            values.append(_Text(text))
        values.append(_Expression(match.group(2)))
        if match.group(1):
            values[-1] = _Expression("=" + match.group(2))
        position = match.end()
    tail = source[position:]
    if "<%" in tail or "%>" in tail:
        raise FluxboxRenderError("Fluxbox template has malformed ERB delimiters")
    if tail:
        values.append(_Text(tail))
    return tuple(values)


def _parse(
    tokens: tuple[_Text | _Expression, ...],
    start: int,
    *,
    nested: bool,
) -> tuple[tuple[_Node, ...], int]:
    nodes: list[_Node] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, _Text):
            nodes.append(token)
            index += 1
            continue
        raw = token.value
        if raw.startswith("="):
            nodes.append(_Expression(raw[1:].strip()))
            index += 1
            continue
        control = raw.strip()
        if control == "end":
            if not nested:
                raise FluxboxRenderError("Fluxbox template has an unmatched end")
            return tuple(nodes), index + 1
        if control in {_HOST_CONDITION, _MONITOR_CONDITION, _PREFIX_LOOP}:
            body, index = _parse(tokens, index + 1, nested=True)
            if control == _PREFIX_LOOP:
                nodes.append(_Prefixes(body))
            else:
                nodes.append(_If(control, body))
            continue
        raise FluxboxRenderError(
            f"Fluxbox template contains unknown Ruby control: {control!r}"
        )
    if nested:
        raise FluxboxRenderError("Fluxbox template has an unterminated block")
    return tuple(nodes), index


def _render(
    nodes: tuple[_Node, ...],
    *,
    monitor_count: int,
    host_name: str,
    prefix: str | None,
) -> str:
    output: list[str] = []
    for node in nodes:
        if isinstance(node, _Text):
            output.append(node.value)
        elif isinstance(node, _Expression):
            output.append(
                _evaluate(
                    node.value,
                    monitor_count=monitor_count,
                    prefix=prefix,
                )
            )
        elif isinstance(node, _If):
            enabled = False if node.condition == _HOST_CONDITION else monitor_count > 1
            if enabled:
                output.append(
                    _render(
                        node.body,
                        monitor_count=monitor_count,
                        host_name=host_name,
                        prefix=prefix,
                    )
                )
        else:
            output.extend(
                _render(
                    node.body,
                    monitor_count=monitor_count,
                    host_name=host_name,
                    prefix=value,
                )
                for value in _PREFIX_VALUES
            )
    return "".join(output)


def _evaluate(expression: str, *, monitor_count: int, prefix: str | None) -> str:
    if expression == "reconfigure":
        return (
            "Exec fluxbox-reconfigure && notify-send -t 3000 'Reloaded fluxbox config'"
        )
    if expression == "layout":
        return "Exec ly --debug && notify-send -t 3000 'Applied auto-layout'"
    if expression == "focus_active":
        return _focus_active()
    if expression == "monitors_connected":
        return str(monitor_count)
    if expression == "prefix":
        return _required_prefix(prefix)
    if expression == "prefix.empty? ? '' : \" for KeyMode #{prefix}\"":
        value = _required_prefix(prefix)
        return "" if not value else f" for KeyMode {value}"

    match = re.fullmatch(r"notify '([^']*)'", expression)
    if match:
        return _notify(_validate_helper_text(match.group(1), "notify arguments"))
    match = re.fullmatch(r"keymode '([^']*)'(?:, '([^']*)')?", expression)
    if match:
        mode = _validate_helper_text(match.group(1), "keymode arguments")
        body = _validate_helper_text(match.group(2) or "", "keymode arguments")
        return _keymode(mode, body)
    match = re.fullmatch(r"keymode_done '([^']*)'", expression)
    if match:
        mode = _validate_mode_name(match.group(1), "keymode_done argument")
        if not mode:
            raise FluxboxRenderError("Fluxbox keymode_done argument is empty")
        return _keymode_done(mode)
    match = re.fullmatch(r"notify_transient '([^']*)', '([^']*)'", expression)
    if match:
        message = _validate_helper_text(match.group(1), "notify_transient arguments")
        mode = _validate_mode_name(match.group(2), "notify_transient mode")
        return _notify(message, timeout=3000, mode=mode)
    match = re.fullmatch(r"delay\('([^']*)', ([1-9][0-9]*)\)", expression)
    if match:
        action = _validate_helper_text(match.group(1), "delay arguments")
        return _delay(action, _validated_delay(match.group(2)))
    match = re.fullmatch(r"delay\(notify\('([^']*)'\), ([1-9][0-9]*)\)", expression)
    if match:
        message = _validate_helper_text(match.group(1), "delayed notify arguments")
        return _delay(_notify(message), _validated_delay(match.group(2)))
    match = re.fullmatch(r'next_unhidden "([^"]*)"(.*)', expression)
    if match:
        selector = _validate_helper_text(
            match.group(1), "next_unhidden arguments"
        )
        options = _keyword_options(match.group(2))
        return _next_unhidden(selector, **options)
    raise FluxboxRenderError(
        f"Fluxbox template contains unknown Ruby expression: {expression!r}"
    )


def _validated_delay(value: str) -> int:
    delay_ms = int(value)
    if delay_ms > _MAX_DELAY_MS:
        raise FluxboxRenderError("Fluxbox delay exceeds its bound")
    return delay_ms


def _validate_helper_text(value: str, context: str) -> str:
    if any(character in _UNSAFE_HELPER_CHARACTERS for character in value):
        raise FluxboxRenderError(f"Fluxbox {context} contain unsafe characters")
    return value


def _validate_mode_name(value: str, context: str) -> str:
    if value and _MODE_NAME.fullmatch(value) is None:
        raise FluxboxRenderError(f"Fluxbox {context} is not a plain mode name")
    return value


def _keyword_options(value: str) -> dict[str, bool]:
    options = {"focus": True, "prev": False, "native": False}
    if not value:
        return options
    if not value.startswith(", "):
        raise FluxboxRenderError("Fluxbox helper arguments are malformed")
    seen: set[str] = set()
    for item in value[2:].split(", "):
        match = re.fullmatch(r"(focus|prev|native): (true|false)", item)
        if match is None:
            raise FluxboxRenderError("Fluxbox helper keyword is outside the allowlist")
        name = match.group(1)
        if name in seen:
            raise FluxboxRenderError("Fluxbox helper keyword is repeated")
        seen.add(name)
        options[name] = match.group(2) == "true"
    return options


def _next_unhidden(
    selector: str,
    *,
    focus: bool,
    prev: bool,
    native: bool,
) -> str:
    command = "PrevWindow" if prev else "NextWindow"
    arguments = f"{command} {selector} (FocusHidden=no) (Minimized=no)"
    if not focus:
        return f":{arguments}"
    if native:
        return f":MacroCmd {{{_focus_active()}}} {{{arguments}}}"
    return f":Exec fluxbox-focus-window '{arguments}'"


def _focus_active(delay: int = 300, *, sync: bool = False) -> str:
    option = " --sync" if sync else ""
    return f"Delay {{Exec focus-active-window{option}}} {delay * 1000}"


def _notify(
    message: str,
    *,
    timeout: int = 3000,
    mode: str | None = None,
    body: str = "",
) -> str:
    value = f'Exec notify-send -t {timeout} "{message}" "{body}"'
    if mode is not None:
        value += f" -p >~/tmp/.fluxbox-{mode}-id"
    return value


def _notify_replace(message: str, mode: str, timeout: int = 3000) -> str:
    return (
        f"{{Exec notify-send -t {timeout} -r "
        f"$(cat ~/tmp/.fluxbox-{mode}-id) '{message}'}}"
    )


def _keymode(mode: str, body: str = "") -> str:
    notification = _notify(
        f"fluxbox {mode} mode",
        timeout=999999,
        mode=mode,
        body=body,
    )
    return f"{{KeyMode {mode}}} {{{notification}}}"


def _keymode_done(mode: str) -> str:
    return "{KeyMode default} " + _notify_replace(f"fluxbox {mode} done", mode)


def _delay(command: str, milliseconds: int) -> str:
    return f"Delay {{{command}}} {milliseconds * 1000}"


def _required_prefix(prefix: str | None) -> str:
    if prefix is None:
        raise FluxboxRenderError("prefix expression appeared outside its loop")
    return prefix
