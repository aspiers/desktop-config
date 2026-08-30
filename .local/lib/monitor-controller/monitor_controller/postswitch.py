"""Read manual-autorandr notifications left by the postswitch hook.

Under the active policy, `.config/autorandr/postswitch` does not launch
desktop work. A manual `autorandr --load celtic` would otherwise run the whole
unkeyed pipeline behind the controller's back, which is precisely the
uncoordinated relayout the controller exists to eliminate. Instead the hook
atomically writes a small record and exits, and the controller picks it up as
evidence that the display changed outside its own dispatch.

The hook writes, via a temporary file and `mv -f`:

    profile=celtic+Samsung-Odyssey-G75F
    monitors=DP-9:eDP

Everything here treats that file as untrusted input. It is written by a shell
script that runs whenever the user types an autorandr command, so it can be
truncated, empty, stale, or absent, and none of those may crash the
controller or be mistaken for a real topology change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# A notification is a couple of short lines. Anything larger is not something
# this hook wrote, so it is refused rather than parsed.
MAX_NOTIFICATION_BYTES = 4096

# autorandr profile names are bounded by the observer for the same reason.
MAX_PROFILE_CHARS = 256


class PostswitchNotificationError(ValueError):
    """Raised when a notification file exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class PostswitchNotification:
    """One manual autorandr application, as reported by the hook."""

    profile: str
    monitors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile or self.profile.isspace():
            msg = "postswitch notification profile must not be empty"
            raise PostswitchNotificationError(msg)
        if len(self.profile) > MAX_PROFILE_CHARS:
            msg = f"postswitch notification profile is too long: {len(self.profile)}"
            raise PostswitchNotificationError(msg)
        if any(character in self.profile for character in "\n\r\x00"):
            msg = "postswitch notification profile contains a control character"
            raise PostswitchNotificationError(msg)


def parse_notification(text: str) -> PostswitchNotification:
    """Parse the hook's ``key=value`` record.

    Unknown keys are ignored rather than rejected, so adding a field to the
    hook does not require a synchronised controller deployment. A missing or
    empty ``profile`` is fatal, because that is the only field carrying
    meaning.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()

    profile = values.get("profile", "")
    if not profile:
        msg = "postswitch notification has no profile"
        raise PostswitchNotificationError(msg)

    # `monitors` is the hook's verbatim AUTORANDR_MONITORS, colon-separated
    # and legitimately empty when autorandr did not export it.
    raw_monitors = values.get("monitors", "")
    monitors = tuple(item for item in raw_monitors.split(":") if item)
    return PostswitchNotification(profile=profile, monitors=monitors)


def read_notification(path: Path) -> PostswitchNotification | None:
    """Read and consume one notification, or return None when there is none.

    The file is removed once read, so a single manual change is acted on
    exactly once. Removal happens only after a successful parse: a malformed
    file is left in place for diagnosis rather than silently discarded.

    Absence is the overwhelmingly common case and is not an error.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        msg = f"cannot read postswitch notification {path}: {error}"
        raise PostswitchNotificationError(msg) from error

    try:
        # Refuse anything oversized without reading it all into memory.
        if os.fstat(descriptor).st_size > MAX_NOTIFICATION_BYTES:
            msg = f"postswitch notification is implausibly large: {path}"
            raise PostswitchNotificationError(msg)
        raw = os.read(descriptor, MAX_NOTIFICATION_BYTES)
    finally:
        os.close(descriptor)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        msg = f"postswitch notification is not valid UTF-8: {path}"
        raise PostswitchNotificationError(msg) from error

    notification = parse_notification(text)

    # Consume only after the parse succeeded.  A FileNotFoundError here means
    # another reader or the hook itself raced us; the record we parsed is
    # still valid, so it is not an error.
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return notification


# Manual changes are rare and a stat of a missing file is nearly free, so a
# short fixed poll keeps the producer dependency-free (no inotify binding)
# while still delivering a manual change well inside the reducer's own
# stability windows.
POLL_SECONDS = 2.0


class PostswitchNotificationMonitor:
    """Poll for manual-autorandr notifications and wake the controller.

    Satisfies the same producer protocol as the DRM uevent monitor: ``run``
    loops until cancelled and invokes *notify* once per delivered
    notification. The controller treats the wake-up exactly like a DRM hint —
    a fresh observation then reconciles whatever the manual change did — so
    the notification's content is only reported, never trusted as topology.
    """

    def __init__(
        self,
        path: Path,
        *,
        on_notification: Callable[[PostswitchNotification], None],
        on_failure: Callable[[str], None],
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        """Watch *path*, reporting deliveries and quarantines to the callbacks."""
        self._path = path
        self._on_notification = on_notification
        self._on_failure = on_failure
        self._poll_seconds = poll_seconds

    async def run(self, notify: Callable[[], object]) -> None:
        """Run until cancelled, waking the controller once per notification."""
        while True:
            self.poll_once(notify)
            await asyncio.sleep(self._poll_seconds)

    def poll_once(self, notify: Callable[[], object]) -> None:
        """Consume at most one notification; never raise into the run loop."""
        try:
            notification = read_notification(self._path)
        except PostswitchNotificationError as error:
            self._quarantine(str(error))
            return
        if notification is not None:
            self._on_notification(notification)
            notify()

    def _quarantine(self, detail: str) -> None:
        # A malformed file is left in place by read_notification for
        # diagnosis, but the poller would then report it forever. Move it
        # aside (bounded: later quarantines overwrite) and report once.
        quarantine = self._path.with_name(self._path.name + ".malformed")
        with contextlib.suppress(OSError):
            self._path.replace(quarantine)
        self._on_failure(f"{detail}; quarantined to {quarantine.name}")
