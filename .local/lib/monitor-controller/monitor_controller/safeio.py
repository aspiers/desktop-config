# ruff: noqa: EM102, TRY003
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

import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# Hardened dirfd primitives.
#
# These existed as three byte-similar private copies in plan_codec.py,
# transactions.py and planner.py — TOCTOU and symlink-attack defence where a
# hardening applied to one copy silently missed the other two (dc-9a0). As
# with read_bounded_text, the caller's exception class is injected because
# each consumer's callers catch its own error type.
# ---------------------------------------------------------------------------

DIRECTORY_OPEN_FLAGS: int = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
FILE_READ_FLAGS: int = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)

_RENAME_NOREPLACE = 1

# The canonical content-hash value grammar every protocol record uses.
SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")


def stable_file_details(details: os.stat_result) -> tuple[int, ...]:
    """Return substitution-sensitive metadata while deliberately excluding atime."""
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def validate_leaf_name(name: str, reference: str, error: type[Exception]) -> None:
    """Refuse any name that could traverse out of its retained directory."""
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise error(f"{reference} is not a safe leaf name")


def rename_noreplace_at(
    directory_fd: int,
    source: str,
    target: str,
    reference: str,
    error: type[Exception],
) -> bool:
    """Atomically publish within one directory without replacing any identity.

    Returns False when the target already exists; raises ``OSError`` for any
    other kernel failure and *error* when ``renameat2`` is unavailable.
    """
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise error(f"{reference}: renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True
    number = ctypes.get_errno()
    if number == errno.EEXIST:
        return False
    raise OSError(number, os.strerror(number), target)


def open_absolute_directory(  # noqa: C901, PLR0913 - safe walk, injected policy
    path: Path,
    *,
    create: bool,
    mode: int,
    reference: str,
    error: type[Exception],
    validate: Callable[[int], None] | None = None,
) -> int:
    """Resolve/create an absolute directory one ``O_NOFOLLOW`` component at a time.

    Returns the retained final descriptor, or -1 when a component is missing
    and *create* is false. When *validate* is given it runs against every
    opened component and the final descriptor, so a caller can enforce its own
    metadata policy along the whole walk.
    """
    parts = path.parts
    if not parts or parts[0] != "/" or any(part in {".", ".."} for part in parts[1:]):
        raise error(f"{reference} must be a canonical absolute directory")
    descriptor = os.open("/", DIRECTORY_OPEN_FLAGS)
    try:
        for part in parts[1:]:
            try:
                child = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return -1
                try:
                    os.mkdir(part, mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    os.fsync(descriptor)
                try:
                    child = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as open_error:
                    raise error(
                        f"created {reference} component cannot be safely opened"
                    ) from open_error
                os.fchmod(child, mode)
            except OSError as open_error:
                raise error(
                    f"{reference} component cannot be safely opened"
                ) from open_error
            if validate is not None:
                validate(child)
            os.close(descriptor)
            descriptor = child
        if validate is not None:
            validate(descriptor)
        return descriptor  # noqa: TRY300
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def read_verified_file_at(  # noqa: PLR0913 - injected per-consumer policy
    directory_fd: int,
    name: str,
    *,
    validate_file: Callable[[os.stat_result], None],
    validate_parent: Callable[[int], None],
    reference: str,
    error: type[Exception],
    changed_error: type[Exception] | None = None,
    wrap_open_errors: bool = False,
) -> bytes:
    """Read one final regular file defensively through a retained parent fd.

    The protocol is fstat/validate, bounded chunked read, over-read check,
    re-fstat/validate, substitution-sensitive metadata comparison, and parent
    re-validation — so a swap, truncation, growth, or replacement during the
    read is refused rather than returned.
    """
    validate_leaf_name(name, reference, error)
    validate_parent(directory_fd)
    try:
        descriptor = os.open(name, FILE_READ_FLAGS, dir_fd=directory_fd)
    except OSError as open_error:
        if not wrap_open_errors or isinstance(open_error, FileNotFoundError):
            raise
        raise error(f"cannot safely open {reference} {name}") from open_error
    try:
        before = os.fstat(descriptor)
        validate_file(before)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise error(f"{reference} {name} was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise error(f"{reference} {name} grew while reading")
        after = os.fstat(descriptor)
        validate_file(after)
        if stable_file_details(before) != stable_file_details(after):
            changed = error if changed_error is None else changed_error
            raise changed(f"{reference} {name} metadata changed while reading")
        validate_parent(directory_fd)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def relative_regular_files_at(
    root_fd: int,
    *,
    validate_directory: Callable[[int], None],
    reference: str,
    error: type[Exception],
    prefix: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """List every regular file below a retained descriptor, refusing surprises.

    Directory metadata is validated at every level, each name must be a safe
    leaf, and any entry that is neither a directory nor a regular file is
    refused rather than skipped.
    """
    values: list[str] = []

    def walk(descriptor: int, parts: tuple[str, ...]) -> None:
        validate_directory(descriptor)
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as walk_error:
            raise error(f"{reference} cannot be enumerated") from walk_error
        for name in names:
            validate_leaf_name(name, reference, error)
            try:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as walk_error:
                raise error(f"{reference} metadata cannot be read") from walk_error
            relative = (*parts, name)
            if stat.S_ISDIR(details.st_mode):
                try:
                    child = os.open(name, DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as walk_error:
                    raise error(f"{reference} cannot be opened") from walk_error
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(details.st_mode):
                values.append("/".join(relative))
            else:
                raise error(f"{reference} contains an unsafe entry")

    walk(root_fd, prefix)
    return tuple(values)
