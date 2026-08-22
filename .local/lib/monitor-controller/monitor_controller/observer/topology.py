"""Shared canonical DRM/XRandR topology derivation.

This module combines already-sampled, read-only adapter evidence.  It has no
command, filesystem, profile, or mutation authority, so keyed workers can repeat
the same connector correspondence and physical-token rules as the full observer
without importing autorandr policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from ..model import (  # noqa: TID252
    ConnectorIdentityEvidence,
    EdidEvidence,
    PhysicalToken,
)
from .drm import ConnectorKind, DrmConnector, DrmSnapshot

if TYPE_CHECKING:
    from .xrandr import XrandrOutput, XrandrSnapshot


@dataclass(frozen=True, slots=True)
class ConnectorTranslation:
    """One uniquely proven kernel-connector to live-X-output correspondence."""

    kernel_connector: str
    live_output: str


@dataclass(frozen=True, slots=True)
class CanonicalTopologyEvidence:
    """Canonical topology facts shared by observation and guarded workers."""

    physical_token: PhysicalToken
    kernel_connected_outputs: tuple[str, ...]
    kernel_external_outputs: tuple[str, ...]
    x_connected_outputs: tuple[str, ...]
    x_active_outputs: tuple[str, ...]
    x_external_outputs: tuple[str, ...]
    connector_identities: tuple[ConnectorIdentityEvidence, ...]
    edid_integrity: tuple[EdidEvidence, ...]
    translations: tuple[ConnectorTranslation, ...]
    inconsistent: bool

    def live_output_for(self, kernel_connector: str) -> str | None:
        """Return the uniquely translated live output, if correspondence exists."""
        return next(
            (
                item.live_output
                for item in self.translations
                if item.kernel_connector == kernel_connector
            ),
            None,
        )


def derive_canonical_topology(
    drm: DrmSnapshot,
    xrandr: XrandrSnapshot,
) -> CanonicalTopologyEvidence:
    """Derive exact connector correspondence and normalized topology evidence."""
    connected_x = tuple(item for item in xrandr.outputs if item.connected)
    translations, correspondence_inconsistent = _connector_translations(
        drm.connectors, connected_x
    )
    kernel_connected = tuple(
        sorted(
            {
                translations.get(item.kernel_name, item.output_name)
                for item in drm.connectors
                if item.connected and item.kind is not ConnectorKind.VIRTUAL
            }
        )
    )
    kernel_external = tuple(
        sorted(
            {
                translations.get(item.kernel_name, item.output_name)
                for item in drm.connectors
                if item.connected and item.kind is ConnectorKind.EXTERNAL
            }
        )
    )
    x_connected = xrandr.connected_outputs
    x_active = xrandr.active_outputs
    x_external_set = {
        translations[item.kernel_name]
        for item in drm.connectors
        if item.connected
        and item.kind is ConnectorKind.EXTERNAL
        and item.kernel_name in translations
    }
    translated_x = set(translations.values())
    x_external_set.update(
        item.name
        for item in connected_x
        if item.name not in translated_x and _x_name_is_external(item.name)
    )
    x_external = tuple(sorted(x_external_set & set(x_connected)))
    identities = _connector_identities(drm.connectors, connected_x, translations)
    edid = _edid_evidence(drm.connectors, translations)
    return CanonicalTopologyEvidence(
        physical_token=_physical_token(drm, translations),
        kernel_connected_outputs=kernel_connected,
        kernel_external_outputs=kernel_external,
        x_connected_outputs=x_connected,
        x_active_outputs=x_active,
        x_external_outputs=x_external,
        connector_identities=identities,
        edid_integrity=edid,
        translations=tuple(
            ConnectorTranslation(kernel, live)
            for kernel, live in sorted(translations.items())
        ),
        inconsistent=correspondence_inconsistent,
    )


def _connector_translations(
    connectors: tuple[DrmConnector, ...],
    connected_x: tuple[XrandrOutput, ...],
) -> tuple[dict[str, str], bool]:
    translations: dict[str, str] = {}
    claimed: set[str] = set()
    inconsistent = False
    for connector in connectors:
        if not connector.connected or connector.kind is ConnectorKind.VIRTUAL:
            continue
        connector_id = connector.connector_id.value
        matches = tuple(
            output
            for output in connected_x
            if connector_id is not None and output.connector_id == connector_id
        )
        if not matches:
            same_name = tuple(
                output for output in connected_x if output.name == connector.output_name
            )
            if len(same_name) == 1 and (
                connector_id is None or same_name[0].connector_id is None
            ):
                matches = same_name
        if len(matches) != 1 or matches[0].name in claimed:
            inconsistent = True
            continue
        translations[connector.kernel_name] = matches[0].name
        claimed.add(matches[0].name)
    return translations, inconsistent


def _x_name_is_external(output: str) -> bool:
    family = output.split("-", maxsplit=1)[0].casefold()
    return family not in {"edp", "lvds", "dsi", "writeback"}


def _connector_identities(
    connectors: tuple[DrmConnector, ...],
    connected_x: tuple[XrandrOutput, ...],
    translations: dict[str, str],
) -> tuple[ConnectorIdentityEvidence, ...]:
    x_by_name = {item.name: item for item in connected_x}
    values: dict[str, ConnectorIdentityEvidence] = {}
    for connector in connectors:
        output = translations.get(connector.kernel_name)
        if output is None or connector.connector_id.value is None:
            continue
        x_output = x_by_name.get(output)
        values[output] = ConnectorIdentityEvidence(
            output=output,
            kernel_connector=connector.kernel_name,
            kernel_connector_id=connector.connector_id.value,
            x_connector_id=None if x_output is None else x_output.connector_id,
        )
    return tuple(sorted(values.values(), key=_identity_key))


def _identity_key(item: ConnectorIdentityEvidence) -> str:
    x_id = "-" if item.x_connector_id is None else f"{item.x_connector_id:020d}"
    return (
        f"{item.output}\0{item.kernel_connector}\0"
        f"{item.kernel_connector_id:020d}\0{x_id}"
    )


def _edid_evidence(
    connectors: tuple[DrmConnector, ...], translations: dict[str, str]
) -> tuple[EdidEvidence, ...]:
    values: dict[str, EdidEvidence] = {}
    for connector in connectors:
        if not connector.connected or connector.kind is ConnectorKind.VIRTUAL:
            continue
        output = translations.get(connector.kernel_name, connector.output_name)
        evidence = connector.edid_evidence()
        values[output] = EdidEvidence(output, evidence.integrity, evidence.base_hash)
    return tuple(sorted(values.values(), key=_edid_key))


def _edid_key(item: EdidEvidence) -> str:
    return f"{item.output}\0{item.integrity.value}\0{item.base_hash or ''}"


def _physical_token(
    drm: DrmSnapshot,
    translations: dict[str, str],
) -> PhysicalToken:
    payload = {
        "scan_state": drm.scan_state.value,
        "connectors": [
            {
                "kernel_name": item.kernel_name,
                "output": translations.get(item.kernel_name, item.output_name),
                "kind": item.kind.value,
                "status_state": item.status_state.value,
                "status": item.status.value,
                "connector_id_state": item.connector_id.state.value,
                "connector_id": item.connector_id.value,
            }
            for item in drm.connectors
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return PhysicalToken(sha256(encoded).hexdigest())
