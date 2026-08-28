"""Guarded reads of small configuration files owned by this user.

Both composition roots read a handful of tiny text files at startup: the
desktop theme, autorandr settings, saved profile fragments. A plain
``Path.read_text()`` on those is wrong in three ways that only show up when
something is unusual:

* it follows symlinks, so the file that is read need not be the file that was
  named;
* it happily opens a device node or a FIFO, and reading a FIFO blocks the
  controller forever at startup; and
* it is unbounded, so a file that should hold ``dark`` can hold a gigabyte.

None of this is a privilege boundary — every path involved is under the user's
own ``$HOME``, and anyone who can rewrite these files can do worse directly.
It is about a controller that starts predictably or not at all, rather than
one that hangs on a FIFO with no message.

The reader lives here rather than in either composition root because both need
it, and a copy in each is how the two drifted apart in the first place: the
authoritative controller ended up reading its theme file with none of these
checks while the non-authoritative one kept them (`dc-t53`).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# One mebibyte. Every caller reads a file that should be a few bytes, so this
# is a backstop against pathological input rather than a considered capacity.
MAX_CONFIGURATION_BYTES: int = 1 << 20


def read_bounded_text(
    path: Path,
    reference: str,
    error: type[Exception],
    *,
    max_bytes: int = MAX_CONFIGURATION_BYTES,
) -> str:
    """Read a small UTF-8 file, refusing anything that is not plainly one.

    *reference* names the file in any raised message, in the caller's own
    vocabulary (``"desktop:theme"``, ``"autorandr:settings.ini"``), because the
    path alone rarely says what the file was for.

    *error* is the exception type to raise. Each composition root has its own
    startup error, and callers catch those rather than a shared type, so the
    class is injected instead of fixed here.

    Refuses, in order: a symlink at the final component (``O_NOFOLLOW``),
    anything that is not a regular file, more than *max_bytes* of content, and
    anything that is not valid UTF-8.
    """
    descriptor: int | None = None
    try:
        # O_NONBLOCK matters for the FIFO case specifically: without it the
        # open() itself blocks until a writer appears, so the regular-file
        # check below never runs and startup hangs with no message. It has no
        # effect on a regular file, which is the only kind accepted anyway.
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            msg = f"{reference} is not a regular file: {path}"
            raise error(msg)
        with os.fdopen(descriptor, "rb") as stream:
            # fdopen took ownership; the finally block must not close it again.
            descriptor = None
            # One byte past the limit, so an exactly-limit-sized file passes
            # and a larger one is detectable without reading all of it.
            raw = stream.read(max_bytes + 1)
    except OSError as os_error:
        # Covers the O_NOFOLLOW refusal (ELOOP) as well as absence and
        # permissions, all of which mean the same thing to a caller: this file
        # cannot be trusted to say what it should.
        msg = f"cannot read {reference}: {path}"
        raise error(msg) from os_error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > max_bytes:
        msg = f"{reference} is larger than {max_bytes} bytes: {path}"
        raise error(msg)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as decode_error:
        msg = f"{reference} is not valid UTF-8: {path}"
        raise error(msg) from decode_error


# The dots-per-inch that a scale factor of 1.0 corresponds to, when the live
# value cannot be read. Matches lib/libdpy.py's fallback.
DEFAULT_REFERENCE_DPI: int = 96

# The desktop setting holding the live value. bin/set-layout-dpi writes it, so
# it changes during a relayout.
_XSETTINGS_DPI = ("xfconf-query", "-c", "xsettings", "-p", "/Xft/DPI")

# Generous: this runs once at startup, and a hung query should not wedge the
# controller.
_QUERY_TIMEOUT_SECONDS = 5.0


def read_reference_dpi(default: int = DEFAULT_REFERENCE_DPI) -> int:
    """Read the desktop's current dots-per-inch, falling back to *default*.

    Call this **once at startup**, never during planning. Planning has to be
    reproducible so a plan can be re-checked against reality before it is
    applied, and this value is precisely one that moves: `bin/set-layout-dpi`
    writes it as part of a relayout. Reading it mid-plan would mean the plan
    and its verification disagreed for reasons unrelated to the display.

    Mirrors `calculate_ui_scale_factor` in `lib/libdpy.py`, which reads the
    same setting and falls back the same way. The two must agree, or the shell
    and Python paths compute different font and panel sizes from the same
    display (`dc-qu6`).
    """
    executable = shutil.which(_XSETTINGS_DPI[0])
    if executable is None:
        return default
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, *_XSETTINGS_DPI[1:]],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if completed.returncode != 0:
        return default
    try:
        value = int(completed.stdout.strip())
    except ValueError:
        return default
    # A non-positive reading would make the scale zero or negative, which is
    # worse than falling back.
    return value if value > 0 else default
