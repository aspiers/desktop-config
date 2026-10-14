"""Apply one Fluxbox layout rule and prove its windows ended up placed.

fluxbox-remote reports nothing back, so a layout rule can fail silently
(dc-yhr0).  For each rule this asks Fluxbox which windows it matches,
applies it, then checks the windows against what the rule asked for: head,
workspace and stuck state, plus geometry by applying the rule a second time,
since one correctly applied rule changes nothing when repeated.  A rule that
needed resending counts as a repair, which callers must report rather than
hide: every repair is a bug still to be root-caused.
"""

import re
import subprocess
from dataclasses import dataclass, field

import fluxbox_remote

STICK = 'If {Matches (Stuck=no)} {Stick}'
UNSTICK = 'If {Matches (Stuck=yes)} {Stick}'
ATTEMPTS = 3


@dataclass(frozen=True)
class Expectation:
    head: int | None = None
    workspace: int | None = None
    stuck: bool | None = None


@dataclass(frozen=True)
class WindowState:
    desktop: int
    x: int
    y: int
    width: int
    height: int
    # Titles change on their own (terminals, browsers); placement does not.
    title: str = field(compare=False)


@dataclass(frozen=True)
class RuleOutcome:
    matcher: str
    windows: int
    repairs: int
    problems: tuple[str, ...]


def top_level_commands(command: str) -> list[str]:
    """Split "MacroCmd {a} {b {c}}" into ["a", "b {c}"]."""
    if not command.startswith('MacroCmd '):
        return [command]
    parts, depth, start = [], 0, 0
    for index, char in enumerate(command):
        if char == '{':
            if depth == 0:
                start = index + 1
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                parts.append(command[start:index])
    return parts


def expectation(commands: list[str]) -> Expectation:
    """What a rule's own commands promise; later commands win."""
    head = workspace = stuck = None
    for command in (part for cmd in commands for part in top_level_commands(cmd)):
        if match := re.fullmatch(r'SetHead (\d+)', command):
            head = int(match[1])
        elif match := re.fullmatch(r'SendToWorkspace (\d+)', command):
            workspace = int(match[1])
        elif command == STICK:
            stuck = True
        elif command == UNSTICK:
            stuck = False
    return Expectation(head, workspace, stuck)


def monitor_heads() -> list[tuple[int, int, int, int]]:
    """(x, y, width, height) per Fluxbox head, head 1 first.

    shortcut: assumes Fluxbox numbers heads in xrandr --listmonitors order
    (primary first), which holds with RandR; revisit if heads mismatch."""
    heads = []
    for line in subprocess.check_output(
            ['xrandr', '--listmonitors'], text=True).splitlines()[1:]:
        match = re.search(r' (\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+) ', line)
        if match:
            width, height, x, y = map(int, match.groups())
            heads.append((x, y, width, height))
    return heads


def window_states() -> dict[int, WindowState]:
    states = {}
    for line in subprocess.check_output(['wmctrl', '-lG'], text=True).splitlines():
        fields = line.split(None, 7)
        if len(fields) >= 7:
            states[int(fields[0], 16)] = WindowState(
                int(fields[1]), *map(int, fields[2:6]),
                fields[7] if len(fields) > 7 else '')
    return states


def head_of(state: WindowState, heads: list[tuple[int, int, int, int]]) -> int | None:
    cx, cy = state.x + state.width // 2, state.y + state.height // 2
    for number, (x, y, width, height) in enumerate(heads, 1):
        if x <= cx < x + width and y <= cy < y + height:
            return number
    return None


def problems(window: int, state: WindowState | None, expected: Expectation,
             heads: list[tuple[int, int, int, int]]) -> list[str]:
    if state is None:
        return []  # closed meanwhile: nothing left to place
    found = []
    label = '0x%08x %r' % (window, state.title)
    if expected.head is not None and head_of(state, heads) != expected.head:
        found.append('%s on head %s, wanted %d'
                     % (label, head_of(state, heads), expected.head))
    sticky = state.desktop == -1
    if expected.stuck is not None and sticky != expected.stuck:
        found.append('%s stuck=%s, wanted %s' % (label, sticky, expected.stuck))
    if (expected.workspace is not None and not sticky
            and state.desktop != expected.workspace - 1):
        found.append('%s on workspace %d, wanted %d'
                     % (label, state.desktop + 1, expected.workspace))
    return found


def apply_rule(matcher: str, map_command: str, commands: list[str],
               heads: list[tuple[int, int, int, int]]) -> RuleOutcome:
    # Ask before applying: the rule may move windows out of its own pattern.
    windows = fluxbox_remote.matching_windows(matcher)
    expected = expectation(commands)
    fluxbox_remote.send(map_command)
    found: list[str] = []
    for attempt in range(ATTEMPTS):
        before = window_states()
        found = [p for w in windows for p in problems(w, before.get(w), expected, heads)]
        # Repeating the rule must change nothing if it fully took effect.
        fluxbox_remote.send(map_command)
        after = window_states()
        found += ['0x%08x %r moved when the rule was repeated' % (w, after[w].title)
                  for w in windows
                  if w in before and w in after and before[w] != after[w]]
        if not found:
            return RuleOutcome(matcher, len(windows), attempt, ())
    return RuleOutcome(matcher, len(windows), ATTEMPTS, tuple(found))
