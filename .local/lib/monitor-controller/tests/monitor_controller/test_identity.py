"""Base-block identity is the guard's proof; extensions carry no identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitor_controller.model import PhysicalToken
from monitor_controller.observer.drm import (
    ConnectorEdid,
    ConnectorId,
    ConnectorKind,
    ConnectorStatus,
    DrmConnector,
    EvidenceState,
    parse_edid,
)
from monitor_controller.observer.topology import (
    CanonicalTopologyEvidence,
    ConnectorTranslation,
)
from monitor_controller.workers.common import WorkerStartupError
from monitor_controller.workers.identity import validate_noncontradictory_edids

_FIXTURES = Path(__file__).parent / "fixtures"

# A healthy, complete 512-byte EDID with a valid base block.
_HEALTHY = bytes.fromhex(
    (_FIXTURES / "edid" / "extension-checksum-invalid.hex")
    .read_text(encoding="ascii")
    .strip()
)


def _connector(raw: bytes) -> DrmConnector:
    return DrmConnector(
        kernel_name="card1-DP-2",
        output_name="DisplayPort-1",
        kind=ConnectorKind.EXTERNAL,
        status_state=EvidenceState.AVAILABLE,
        status=ConnectorStatus.CONNECTED,
        connector_id=ConnectorId(EvidenceState.AVAILABLE, 453),
        edid=ConnectorEdid(
            state=EvidenceState.AVAILABLE,
            raw=raw,
            parsed=parse_edid(raw),
        ),
    )


def _topology() -> CanonicalTopologyEvidence:
    return CanonicalTopologyEvidence(
        physical_token=PhysicalToken("0" * 64),
        kernel_connected_outputs=("DisplayPort-1",),
        kernel_external_outputs=("DisplayPort-1",),
        x_connected_outputs=("DisplayPort-1",),
        x_active_outputs=("DisplayPort-1",),
        x_external_outputs=("DisplayPort-1",),
        connector_identities=(),
        edid_integrity=(),
        translations=(
            ConnectorTranslation(
                kernel_connector="card1-DP-2",
                live_output="DisplayPort-1",
            ),
        ),
        inconsistent=False,
    )


def test_matching_base_with_dirty_saved_extensions_is_accepted() -> None:
    """A dirty saved fingerprint must not veto a proven base identity.

    The Samsung G75F serves truncated/garbage extension bytes at link
    train; X snapshots them into its EDID property, and `autorandr --save`
    then records them into the profile's setup fingerprint. The first live
    replug's preparations were all vetoed by comparing that dirt against a
    healthy fresh sysfs read, even though the checksum-valid base block —
    the actual identity — matched exactly (dc-bla).
    """
    live = _HEALTHY
    # Saved pattern: same base block, then truncated garbage extensions —
    # the shape of the live profile's recorded fingerprint (737 hex chars).
    dirty = live[:128].hex() + "70127903badd" + "0" * 100 + "7"
    validate_noncontradictory_edids(
        {"DisplayPort-1": dirty},
        (_connector(live),),
        _topology(),
        allow_temporary_absence=False,
    )


def test_differing_base_block_still_fails_closed() -> None:
    """A different monitor on the admitted output must still be refused."""
    live = bytearray(_HEALTHY)
    live[10] ^= 0xFF  # different product identity in the base block
    live[127] = (-sum(live[:127])) & 0xFF  # keep the base checksum valid
    saved = _HEALTHY.hex()
    with pytest.raises(WorkerStartupError, match="contradicts admitted mapping"):
        validate_noncontradictory_edids(
            {"DisplayPort-1": saved},
            (_connector(bytes(live)),),
            _topology(),
            allow_temporary_absence=False,
        )
