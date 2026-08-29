"""Fresh non-contradictory connector identity guards shared by workers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from monitor_controller.observer.autorandr import fingerprint_matches
from monitor_controller.observer.drm import (
    ConnectorKind,
    DrmConnector,
    EvidenceState,
)
from monitor_controller.workers.common import stale as _stale

if TYPE_CHECKING:
    from monitor_controller.observer.topology import CanonicalTopologyEvidence

EDID_BASE_BYTES = 128
EDID_BASE_HEX_CHARS = EDID_BASE_BYTES * 2


def validate_noncontradictory_edids(  # noqa: C901 - one closed evidence policy
    patterns: Mapping[str, str],
    connectors: tuple[DrmConnector, ...],
    topology: CanonicalTopologyEvidence,
    *,
    allow_temporary_absence: bool,
) -> None:
    """Re-prove every available base identity and reject usable contradiction.

    A connected connector whose EDID file is genuinely absent may retain the
    already-admitted mapping only when the immutable request explicitly permits
    that policy. Read errors, malformed bases, changed fixed bytes, complete
    fingerprint mismatches, incomplete coverage, and base collisions always
    fail closed.
    """
    proved_outputs: set[str] = set()
    temporarily_absent: set[str] = set()
    live_bases: list[bytes] = []
    for item in connectors:
        if item.kind is ConnectorKind.VIRTUAL or not item.connected:
            continue
        live_output = topology.live_output_for(item.kernel_name)
        if live_output is None or live_output not in patterns:
            _stale("connected DRM connector lacks its admitted live mapping")
        if item.edid.state in {EvidenceState.MISSING, EvidenceState.EMPTY}:
            if not allow_temporary_absence:
                _stale("fresh connector EDID is absent outside admitted policy")
            temporarily_absent.add(live_output)
            continue
        if item.edid.state is not EvidenceState.AVAILABLE:
            _stale("fresh connector EDID evidence is unreadable")
        raw = item.edid.raw
        parsed = item.edid.parsed
        if raw is None or parsed is None or parsed.base_hash is None:
            _stale("fresh connector base identity cannot be proved")
        value = raw.hex()
        pattern = patterns[live_output]
        prove_fixed_saved_base(pattern, value)
        proved_outputs.add(live_output)
        live_bases.append(raw[:EDID_BASE_BYTES])
        if not parsed.fully_ready:
            continue
        try:
            matches = fingerprint_matches(pattern, value)
        except ValueError as error:
            _stale(f"staged setup fingerprint is invalid: {error}")
        if not matches:
            _stale("fresh complete connector identity contradicts admitted mapping")
    if proved_outputs | temporarily_absent != set(patterns):
        _stale("fresh identity evidence does not cover admitted output mapping")
    if len(live_bases) != len(set(live_bases)):
        _stale("fresh connector base identities collide")


def prove_fixed_saved_base(pattern: str, live_value: str) -> None:
    """Require every EDID base nibble to be fixed and equal in a saved pattern."""
    if len(pattern) < EDID_BASE_HEX_CHARS or len(live_value) < EDID_BASE_HEX_CHARS:
        _stale("saved setup cannot prove a complete fresh base identity")
    saved_base = pattern[:EDID_BASE_HEX_CHARS]
    live_base = live_value[:EDID_BASE_HEX_CHARS]
    if re.fullmatch(r"[0-9a-fA-F]+", saved_base) is None:
        _stale("saved setup cannot prove a complete fresh base identity")
    if saved_base.casefold() != live_base.casefold():
        _stale("fresh usable connector identity contradicts admitted mapping")

