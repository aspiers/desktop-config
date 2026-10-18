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
import time
from dataclasses import dataclass, field

import fluxbox_remote

STICK = 'If {Matches (Stuck=no)} {Stick}'
UNSTICK = 'If {Matches (Stuck=yes)} {Stick}'
ATTEMPTS = 3
# Clients such as Emacs snap to their own size increments just after a
# resize, so wait for geometry to stop changing before judging it.
SETTLE_POLL_SECONDS = 0.03
SETTLE_TIMEOUT_SECONDS = 1.0


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
    # What the first attempt found when a repair was needed, for diagnosis.
    repaired: tuple[str, ...] = ()


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


def bring_onto_head(windows: list[int], head: int,
                    heads: list[tuple[int, int, int, int]]) -> None:
    """Move windows whose centre is on no head to the given head's corner.

    Fluxbox's SetHead keeps a window's offset from its nearest head, so a
    window left where a removed monitor used to be stays off-screen
    (dc-359t)."""
    states = window_states()
    x, y = heads[head - 1][:2]
    for window in windows:
        state = states.get(window)
        if state and head_of(state, heads) is None:
            subprocess.call(['wmctrl', '-i', '-r', hex(window),
                             '-e', '0,%d,%d,-1,-1' % (x, y)])


def settled_states(windows: list[int]) -> dict[int, WindowState]:
    """Window states once the given windows stop changing, or at the timeout."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    previous = window_states()
    while True:
        time.sleep(SETTLE_POLL_SECONDS)
        current = window_states()
        settled = all(previous.get(w) == current.get(w) for w in windows)
        if settled or time.monotonic() >= deadline:
            return current
        previous = current


def apply_rule(matcher: str, map_command: str, commands: list[str],
               heads: list[tuple[int, int, int, int]]) -> RuleOutcome:
    # Ask before applying: the rule may move windows out of its own pattern.
    windows = fluxbox_remote.matching_windows(matcher)
    expected = expectation(commands)
    if windows and expected.head is not None and expected.head <= len(heads):
        bring_onto_head(windows, expected.head, heads)
    fluxbox_remote.send(map_command)
    if not windows:
        return RuleOutcome(matcher, 0, 0, ())
    first: list[str] = []
    found: list[str] = []
    for attempt in range(ATTEMPTS):
        before = settled_states(windows)
        found = [p for w in windows for p in problems(w, before.get(w), expected, heads)]
        # Repeating the rule must change nothing if it fully took effect.
        fluxbox_remote.send(map_command)
        after = settled_states(windows)
        found += ['0x%08x %r moved from %s to %s when the rule was repeated'
                  % (w, after[w].title, _geometry(before[w]), _geometry(after[w]))
                  for w in windows
                  if w in before and w in after and before[w] != after[w]]
        if not found:
            return RuleOutcome(matcher, len(windows), attempt, (), tuple(first))
        if attempt == 0:
            first = found
    return RuleOutcome(matcher, len(windows), ATTEMPTS, tuple(found), tuple(first))


def _geometry(state: WindowState) -> str:
    return '%dx%d%+d%+d@%d' % (state.width, state.height, state.x, state.y,
                               state.desktop)
