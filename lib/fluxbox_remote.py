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


def send(command: str, timeout: float = TIMEOUT_SECONDS) -> None:
    """Send one command once Fluxbox has read the previous one, then wait
    until Fluxbox has read this one too."""
    if not wait_until_consumed(timeout):
        raise CommandNotConsumed(
            'an earlier command is still unread; not overwriting it')
    subprocess.check_call(['fluxbox-remote', command])
    if not wait_until_consumed(timeout):
        raise CommandNotConsumed('Fluxbox did not read the command')
