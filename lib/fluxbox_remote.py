"""Deliver fluxbox-remote commands without losing any.

fluxbox-remote stores its command in the root window's _FLUXBOX_ACTION
property, replacing any command Fluxbox has not read yet, and exits 0
either way.  A busy Fluxbox therefore silently drops all but the last of a
quick series of commands (dc-yhr0).  Fluxbox deletes the property as it
reads it, so waiting for the deletion before and after each send makes
delivery reliable without guessing a delay.
"""

import subprocess
import time

ACTION_PROPERTY = '_FLUXBOX_ACTION'
RESULT_PROPERTY = '_FLUXBOX_ACTION_RESULT'
POLL_SECONDS = 0.005
TIMEOUT_SECONDS = 5.0


class CommandNotConsumed(RuntimeError):
    """Fluxbox did not read a pending command before the timeout."""


def command_pending() -> bool:
    output = subprocess.run(
        ['xprop', '-root', ACTION_PROPERTY],
        capture_output=True, check=True, text=True,
    ).stdout
    # An absent property prints "_FLUXBOX_ACTION:  not found."; match the
    # typed prefix instead, since the command text could say "not found".
    return output.startswith(ACTION_PROPERTY + '(')


def wait_until_consumed(timeout: float = TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while command_pending():
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def matching_windows(pattern: str, timeout: float = TIMEOUT_SECONDS) -> list[int]:
    """Client window IDs Fluxbox says match a client pattern."""
    subprocess.check_call(['xprop', '-root', '-remove', RESULT_PROPERTY])
    send('ClientPatternTest ' + pattern, timeout)
    # Fluxbox writes the result while executing, just after it reads the
    # command, so the result can lag the send's completion slightly.
    deadline = time.monotonic() + timeout
    while True:
        output = subprocess.run(
            ['xprop', '-root', RESULT_PROPERTY],
            capture_output=True, check=True, text=True,
        ).stdout
        if output.startswith(RESULT_PROPERTY + '('):
            break
        if time.monotonic() >= deadline:
            raise CommandNotConsumed('no ClientPatternTest result')
        time.sleep(POLL_SECONDS)
    # xprop prints the "0x<id>\t<title>\n" lines with literal escapes.
    result = output.split('=', 1)[1].strip().strip('"')
    if result.startswith('-1'):
        raise ValueError('invalid client pattern: ' + pattern)
    return [int(line.split('\\t')[0], 16)
            for line in result.split('\\n') if line.startswith('0x')]


def send(command: str, timeout: float = TIMEOUT_SECONDS) -> None:
    """Send one command once Fluxbox has read the previous one, then wait
    until Fluxbox has read this one too."""
    if not wait_until_consumed(timeout):
        raise CommandNotConsumed(
            'an earlier command is still unread; not overwriting it')
    subprocess.check_call(['fluxbox-remote', command])
    if not wait_until_consumed(timeout):
        raise CommandNotConsumed('Fluxbox did not read the command')
