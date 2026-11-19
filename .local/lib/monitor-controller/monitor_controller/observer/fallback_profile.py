"""Save a best-guess autorandr profile when none fits the displays still lit.

After the user switches a display off by hand, the screens left over may match
no saved profile.  The reducer then has nothing to apply, so the desktop keeps
the layout for the old screens and possibly no primary output (dc-8xns).

This saves one into the live autorandr profile directory, where the next
observation finds it like any other.  It keeps the arrangement the user left,
borrows the window layout and the primary output's role (laptop or external)
from the saved profile with the same number of screens, and tells the user how
to replace it with an arrangement of their own.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime.journal import service_logger  # noqa: TID252
from .autorandr import is_internal_output, profile_scope

if TYPE_CHECKING:
    from ..model import Fingerprint  # noqa: TID252
    from .autorandr import SavedAutorandrProfile
    from .xrandr import XrandrSnapshot

_JOURNAL = service_logger("monitor_controller.journal")

# autorandr's own rendering, because it treats a profile as current only when
# every option, rate and x-prop included, equals what it reads back.
DUMP_CONFIG_COMMAND = ("autorandr", "--config")
DUMP_TIMEOUT_SECONDS = 10.0
# settings.ini skips gamma, which Redshift owns, so saved profiles never hold it.
_DROPPED_OPTIONS = frozenset({"primary", "gamma"})


def dump_live_config() -> str:
    """Return the live X configuration in autorandr profile syntax."""
    return subprocess.run(  # noqa: S603 - fixed argument vector
        DUMP_CONFIG_COMMAND,
        capture_output=True,
        text=True,
        check=True,
        timeout=DUMP_TIMEOUT_SECONDS,
    ).stdout


class FallbackProfileWriter:
    """Save one best-guess profile per unmatched set of screens."""

    def __init__(
        self,
        root: Path,
        *,
        host: str,
        notify: Callable[[str, str], None],
        dump_config: Callable[[], str] = dump_live_config,
    ) -> None:
        """Write under *root*, naming profiles after *host*."""
        self._root = root
        self._host = host
        self._notify = notify
        self._dump_config = dump_config
        # One attempt per screen set, so a failure is not retried every second.
        self._attempted: set[str] = set()

    def __call__(
        self,
        fingerprints: Sequence[Fingerprint],
        xrandr: XrandrSnapshot,
        profiles: Sequence[SavedAutorandrProfile],
    ) -> None:
        """Save a profile for *fingerprints*, which no saved profile matches."""
        name = profile_name(self._host, fingerprints)
        if name in self._attempted:
            return
        self._attempted.add(name)
        template = choose_template(profiles, fingerprints)
        if template is None:
            _JOURNAL.info(f"no saved profile has {len(fingerprints)} screens to copy")
            self._notify(
                "No display profile for these screens",
                "Arrange them, then run: autorandr --save <name>",
            )
            return
        primary = choose_primary(template, fingerprints, xrandr)
        if primary is None:
            return
        try:
            config = with_primary(self._dump_config(), primary)
            write_profile(self._root / name, fingerprints, config, template.layout)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _JOURNAL.warning(f"could not save fallback profile {name}: {error}")
            return
        _JOURNAL.info(
            f"saved fallback profile {name}: {primary} primary,"
            f" layout {template.layout} from {template.name}"
        )
        self._notify(
            f"Saved display profile {name}",
            f"No saved profile fitted the screens still on, so {primary} is"
            f" primary with the {template.layout} window layout. To keep another"
            f" arrangement, set it up and run: autorandr --save --force {name}",
        )


def profile_name(host: str, fingerprints: Sequence[Fingerprint]) -> str:
    """Name the profile after the exact screens, so a rerun finds it again."""
    # shortcut: a hash, not monitor models; rename the directory to taste.
    identity = "\n".join(sorted(f"{item.output} {item.value}" for item in fingerprints))
    return f"{host}+auto-{hashlib.sha256(identity.encode()).hexdigest()[:8]}"


def choose_template(
    profiles: Sequence[SavedAutorandrProfile], fingerprints: Sequence[Fingerprint]
) -> SavedAutorandrProfile | None:
    """Pick the saved profile whose window layout best fits these screens."""
    same_count = [item for item in profiles if len(item.setup) == len(fingerprints)]
    scope = profile_scope(tuple(fingerprints))
    candidates = [item for item in same_count if item.scope is scope] or same_count
    # The layout most profiles share is the generic one, not one monitor's own.
    usage = Counter(item.layout for item in candidates)
    return min(
        candidates,
        key=lambda item: (-usage[item.layout], item.layout, item.name),
        default=None,
    )


def choose_primary(
    template: SavedAutorandrProfile,
    fingerprints: Sequence[Fingerprint],
    xrandr: XrandrSnapshot,
) -> str | None:
    """Give primary to the screen in the template primary's role, largest first."""
    geometry = {
        output.name: output.geometry
        for output in xrandr.outputs
        if output.geometry is not None
    }
    outputs = [item.output for item in fingerprints]
    if any(output not in geometry for output in outputs):
        return None
    template_primary = next(
        (item.output for item in template.config if item.active and item.primary),
        None,
    )
    internal = template_primary is not None and is_internal_output(template_primary)
    preferred = [
        output for output in outputs if is_internal_output(output) == internal
    ] or outputs

    def area(output: str) -> int:
        box = geometry[output]
        assert box is not None  # noqa: S101 - filtered above
        return box.width * box.height

    return max(sorted(preferred), key=area)


def with_primary(config: str, primary: str) -> str:
    """Return *config* with *primary* as its only primary output."""
    lines: list[str] = []
    for line in config.splitlines():
        fields = line.split(maxsplit=1)
        if fields and fields[0] in _DROPPED_OPTIONS:
            continue
        lines.append(line)
        if fields == ["output", primary]:
            lines.append("primary")
    if "primary" not in lines:
        msg = f"live config has no output {primary}"
        raise ValueError(msg)
    return "\n".join(lines) + "\n"


def write_profile(
    directory: Path,
    fingerprints: Sequence[Fingerprint],
    config: str,
    layout: str,
) -> None:
    """Create a new profile directory; never overwrite an existing one."""
    directory.mkdir()
    setup = "".join(
        f"{item.output} {item.value}\n"
        for item in sorted(fingerprints, key=lambda item: item.output)
    )
    directory.joinpath("setup").write_text(setup, encoding="utf-8")
    directory.joinpath("layout").write_text(f"{layout}\n", encoding="utf-8")
    # Loaders skip a directory without config, so it goes in last, atomically.
    staged = directory / ".config.tmp"
    staged.write_text(config, encoding="utf-8")
    staged.replace(directory / "config")
