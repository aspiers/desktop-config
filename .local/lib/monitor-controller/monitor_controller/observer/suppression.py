"""Remember displays the user switched off by hand, and hide them from planning.

Switching off a monitor that stays plugged in (xfce4-display-settings,
``xrandr --off``) leaves it connected.  The reducer then refuses the topology
that is still lit, because external hardware must never fall back to a
laptop-only profile, so the desktop kept the old layout with windows parked
on the dark screen and no primary output (dc-8xns).

Masking such an output as unplugged hands the reducer an ordinary unplug: it
applies the saved profile for what is still lit, which restores the primary
output, and finalizes the desktop for that profile's layout.  The output stays
masked until the user switches it back on or replugs it.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ..runtime.journal import service_logger  # noqa: TID252
from .autorandr import (
    AutorandrObservation,
    SavedAutorandrProfile,
    resolve_output_mapping,
)
from .drm import ConnectorEdid, ConnectorStatus, DrmSnapshot, EvidenceState
from .xrandr import XConnectionState, XrandrSnapshot

_JOURNAL = service_logger("monitor_controller.journal")

SCHEMA_VERSION = 1
# A resume can leave a DisplayPort link down this long before it retrains;
# matches the shell watcher's POST_RESUME_STABILITY_SECONDS.  A disconnection
# shorter than this which spans a suspend is a resume, not a replug.
REPLUG_GRACE_SECONDS = 90.0
SUSPEND_STATS = Path("/sys/power/suspend_stats/success")


@dataclass(frozen=True, slots=True)
class SuppressedOutput:
    """One output the user switched off, keyed elsewhere by EDID base hash."""

    output: str
    # Wall-clock time, which keeps advancing across suspend, unlike monotonic.
    disconnected_at_s: float | None = None
    suspends_at_disconnect: int | None = None


def read_suspend_count(path: Path = SUSPEND_STATS) -> int | None:
    """Return the kernel's successful-suspend count, or None if unreadable."""
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def desktop_notify(summary: str, body: str) -> None:
    """Show a desktop notification; a missing notifier must not fail sampling."""
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["notify-send", "-a", "monitor-controller", summary, body],  # noqa: S607
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


@dataclass(frozen=True, slots=True)
class _Sample:
    # EDID base hash -> (kernel connector id, live output name) for every
    # kernel-connected output with a checksum-valid base identity.
    present: dict[str, tuple[int, str]]
    lit: frozenset[str]


class OutputSuppression:
    """Track manual switch-offs across samples and mask them out of evidence."""

    def __init__(
        self,
        path: Path,
        *,
        may_adopt: Callable[[], bool],
        realtime: Callable[[], float] = time.time,
        suspend_count: Callable[[], int | None] = read_suspend_count,
        notify: Callable[[str, str], None] = desktop_notify,
    ) -> None:
        """Load persisted suppressions; *may_adopt* says no action is running."""
        self._path = path
        self._may_adopt = may_adopt
        self._realtime = realtime
        self._suspend_count = suspend_count
        self._notify = notify
        self._entries = self._load()
        self._last_lit: frozenset[str] | None = None

    @property
    def suppressed(self) -> dict[str, SuppressedOutput]:
        """Return a copy of the current suppressions by EDID base hash."""
        return dict(self._entries)

    def mask(
        self,
        begin_drm: DrmSnapshot,
        end_drm: DrmSnapshot,
        xrandr: XrandrSnapshot,
        autorandr: AutorandrObservation,
        profiles: tuple[SavedAutorandrProfile, ...],
    ) -> tuple[DrmSnapshot, DrmSnapshot, XrandrSnapshot, AutorandrObservation]:
        """Update suppressions from one sample, then return it masked."""
        # A torn or unreadable sample decides nothing; it is only masked.
        if begin_drm == end_drm and xrandr.valid and autorandr.valid:
            self._update(_sample(end_drm, xrandr))
        begin_drm, end_drm, masked_x, hidden = mask_hardware(
            self._entries, begin_drm, end_drm, xrandr
        )
        if not hidden:
            return begin_drm, end_drm, xrandr, autorandr
        return (
            begin_drm,
            end_drm,
            masked_x,
            _mask_autorandr(autorandr, hidden, masked_x, profiles),
        )

    def _update(self, sample: _Sample) -> None:
        before = dict(self._entries)
        now = self._realtime()
        for key, entry in before.items():
            self._age(key, entry, sample, now)
        if self._last_lit is not None and sample.lit and self._may_adopt():
            for key in (self._last_lit & sample.present.keys()) - sample.lit:
                if key not in self._entries:
                    self._suppress(key, sample.present[key][1])
        self._last_lit = sample.lit
        if self._entries != before:
            self._save()

    def _age(
        self, key: str, entry: SuppressedOutput, sample: _Sample, now: float
    ) -> None:
        """Release *key* if it was switched on or replugged, else track absence."""
        if key in sample.lit:
            self._release(key, "switched back on")
        elif key in sample.present:
            if entry.disconnected_at_s is None:
                return
            if self._was_replug(entry, now):
                self._release(key, "replugged")
            else:
                # A resume dropped the link; the switch-off stands.
                self._entries[key] = SuppressedOutput(entry.output)
        elif entry.disconnected_at_s is None:
            self._entries[key] = replace(
                entry,
                disconnected_at_s=now,
                suspends_at_disconnect=self._suspend_count(),
            )
        elif now - entry.disconnected_at_s >= REPLUG_GRACE_SECONDS:
            self._release(key, "unplugged")

    def _was_replug(self, entry: SuppressedOutput, now: float) -> bool:
        assert entry.disconnected_at_s is not None  # noqa: S101 - caller checked
        if now - entry.disconnected_at_s >= REPLUG_GRACE_SECONDS:
            return True
        before = entry.suspends_at_disconnect
        after = self._suspend_count()
        # Unknown suspend history falls back to the grace period alone.
        return before is not None and after is not None and before == after

    def _suppress(self, key: str, output: str) -> None:
        self._entries[key] = SuppressedOutput(output)
        _JOURNAL.info(f"display {output} switched off by hand; masking it as unplugged")
        self._notify(
            f"{output} stays off",
            "Rearranging the desktop for the remaining displays. Switch it back "
            "on, or unplug and replug it, to use it again.",
        )

    def _release(self, key: str, reason: str) -> None:
        entry = self._entries.pop(key)
        _JOURNAL.info(f"display {entry.output} {reason}; no longer masked")

    def _load(self) -> dict[str, SuppressedOutput]:
        return load_suppressions(self._path)

    def _save(self) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "outputs": {
                key: {
                    "output": entry.output,
                    "disconnected_at_s": entry.disconnected_at_s,
                    "suspends_at_disconnect": entry.suspends_at_disconnect,
                }
                for key, entry in sorted(self._entries.items())
            },
        }
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._path)
        except OSError as error:
            # The in-memory state still masks until the controller restarts.
            _JOURNAL.warning(f"could not persist {self._path}: {error}")


def suppression_path(state_home: Path | None = None) -> Path:
    """Return where the active controller keeps displays switched off by hand."""
    if state_home is None:
        configured = os.environ.get("XDG_STATE_HOME")
        state_home = (
            Path(configured) if configured else Path.home() / ".local" / "state"
        )
    return state_home / "monitor-controller" / "active" / "suppressed-outputs.json"


def load_suppressions(path: Path) -> dict[str, SuppressedOutput]:
    """Read suppressions by EDID base hash; unreadable means none."""
    try:
        return _decode(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        # Losing a suppression relights a monitor; refusing to start is worse.
        _JOURNAL.warning(f"ignoring unreadable {path}: {error}")
        return {}


def mask_hardware(
    suppressed: Collection[str],
    begin_drm: DrmSnapshot,
    end_drm: DrmSnapshot,
    xrandr: XrandrSnapshot,
) -> tuple[DrmSnapshot, DrmSnapshot, XrandrSnapshot, set[str]]:
    """Show connected-but-dark suppressed displays as unplugged.

    Workers sample the hardware again before they mutate it, so they must
    mask it exactly as the observation they were admitted from did.
    Returns the masked snapshots and the X names of the hidden outputs.
    """
    sample = _sample(end_drm, xrandr)
    hidden = {
        sample.present[key]
        for key in suppressed
        if key in sample.present and key not in sample.lit
    }
    if not hidden:
        return begin_drm, end_drm, xrandr, set()
    ids = {connector_id for connector_id, _ in hidden}
    return (
        _mask_drm(begin_drm, ids),
        _mask_drm(end_drm, ids),
        _mask_xrandr(xrandr, ids),
        {output for _, output in hidden},
    )


def _decode(data: dict[str, object]) -> dict[str, SuppressedOutput]:
    if data.get("schema_version") != SCHEMA_VERSION:
        msg = f"unsupported schema {data.get('schema_version')!r}"
        raise ValueError(msg)
    outputs = cast("dict[str, dict[str, object]]", data["outputs"])
    decoded: dict[str, SuppressedOutput] = {}
    for key, value in outputs.items():
        output = value["output"]
        since = value.get("disconnected_at_s")
        suspends = value.get("suspends_at_disconnect")
        if (
            not isinstance(output, str)
            or not isinstance(since, int | float | None)
            or not isinstance(suspends, int | None)
        ):
            msg = f"malformed entry {key!r}"
            raise TypeError(msg)
        decoded[str(key)] = SuppressedOutput(
            output, None if since is None else float(since), suspends
        )
    return decoded


def _sample(drm: DrmSnapshot, xrandr: XrandrSnapshot) -> _Sample:
    by_id = {
        output.connector_id: output
        for output in xrandr.outputs
        if output.connector_id is not None
    }
    present: dict[str, tuple[int, str]] = {}
    lit: set[str] = set()
    for connector in drm.connectors:
        parsed = connector.edid.parsed
        connector_id = connector.connector_id.value
        if (
            not connector.connected
            or parsed is None
            or parsed.base_hash is None
            or connector_id is None
        ):
            continue
        output = by_id.get(connector_id)
        # Without an X output, or while X disagrees, nothing is known yet.
        if output is None or not output.connected:
            continue
        present[parsed.base_hash] = (connector_id, output.name)
        if output.active:
            lit.add(parsed.base_hash)
    return _Sample(present, frozenset(lit))


def _mask_drm(snapshot: DrmSnapshot, ids: set[int]) -> DrmSnapshot:
    return replace(
        snapshot,
        connectors=tuple(
            replace(
                item,
                status=ConnectorStatus.DISCONNECTED,
                edid=ConnectorEdid(EvidenceState.EMPTY),
            )
            if item.connector_id.value in ids
            else item
            for item in snapshot.connectors
        ),
    )


def _mask_xrandr(snapshot: XrandrSnapshot, ids: set[int]) -> XrandrSnapshot:
    return replace(
        snapshot,
        outputs=tuple(
            replace(
                item,
                connection=XConnectionState.DISCONNECTED,
                geometry=None,
                primary=False,
                modes=(),
                width_mm=0,
                height_mm=0,
            )
            if item.connector_id in ids
            else item
            for item in snapshot.outputs
        ),
    )


def _mask_autorandr(
    observation: AutorandrObservation,
    outputs: set[str],
    xrandr: XrandrSnapshot,
    profiles: tuple[SavedAutorandrProfile, ...],
) -> AutorandrObservation:
    """Re-run autorandr's detection as if the hidden outputs were unplugged."""
    fingerprints = tuple(
        item for item in observation.fingerprints if item.output not in outputs
    )
    connected = xrandr.connected_outputs
    detected = tuple(
        profile.name
        for profile in profiles
        if len(profile.setup) == len(fingerprints)
        and resolve_output_mapping(profile, fingerprints, connected).mapping is not None
    )
    return replace(
        observation,
        fingerprints=fingerprints,
        detected_profiles=detected,
        current_profiles=tuple(
            name for name in observation.current_profiles if name in detected
        ),
    )
