"""Shared exact staged-plan, topology, and connector-identity worker guards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, Protocol

from monitor_controller.model import ActionId, RawEvidenceSource
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import (
    ConnectorKind,
    ConnectorStatus,
    EvidenceState,
    ReadOnlyTree,
    sample_drm,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import XrandrEvidenceSource, sample_xrandr
from monitor_controller.runtime.transactions import ExpectedTopology, TransactionRequest
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerStartupError,
    validate_topology_guard,
)
from monitor_controller.workers.identity import validate_noncontradictory_edids

if TYPE_CHECKING:
    from monitor_controller.desktop.plan_codec import DesktopPlanBundle


class DesktopTopologyCommands(XrandrEvidenceSource, Protocol):
    """Read-only fresh XRandR evidence required at each desktop boundary."""


def validate_plan_request_binding(
    request: TransactionRequest,
    planning_action: ActionId,
    bundle: DesktopPlanBundle,
) -> None:
    """Require one worker request to reproduce every immutable plan guard."""
    guards = bundle.plan.guards
    topology = ExpectedTopology(
        guards.topology.kernel_connected_outputs,
        guards.topology.kernel_external_outputs,
        guards.topology.x_connected_outputs,
        guards.topology.x_active_outputs,
    )
    transition_key = (
        f"{guards.input_key.physical_epoch}|{guards.profile}|"
        f"{guards.observation_key.value}"
    )
    if (
        guards.action_id != planning_action
        or request.transition_id != guards.transition_id
        or request.transition_key is None
        or request.transition_key.value != transition_key
        or request.profile != guards.profile
        or request.layout != guards.layout
        or request.physical_epoch != guards.input_key.physical_epoch
        or request.admitted_event_generation < guards.admitted_event_generation
        or request.physical_token != guards.physical_token
        or request.observation_key != guards.observation_key
        or request.output_mapping != guards.output_mapping
        or request.expected_topology != topology
    ):
        _stale("desktop worker request differs from staged transition guards")


def sample_exact_desktop_topology(
    request: TransactionRequest,
    bundle: DesktopPlanBundle,
    drm_tree: ReadOnlyTree,
    commands: DesktopTopologyCommands,
    *,
    allow_temporary_edid_absence: bool,
) -> CurrentTopology:
    """Re-prove exact connected/active topology and non-contradictory identity."""
    begin_drm = sample_drm(drm_tree)
    xrandr = sample_xrandr(commands)
    end_drm = sample_drm(drm_tree)
    if begin_drm != end_drm:
        _stale("DRM evidence changed during desktop boundary sample")
    if begin_drm.scan_state is not EvidenceState.AVAILABLE:
        _stale("DRM connector scan is not complete")
    if not xrandr.valid:
        _stale("XRandR query and properties evidence is invalid or torn")
    if any(
        item.kind is not ConnectorKind.VIRTUAL
        and (
            item.status_state is not EvidenceState.AVAILABLE
            or item.status is ConnectorStatus.UNKNOWN
        )
        for item in begin_drm.connectors
    ):
        _stale("DRM connector status evidence is uncertain")
    topology = derive_canonical_topology(begin_drm, xrandr)
    if topology.inconsistent:
        _stale("DRM and X connector identity is contradictory or non-unique")
    if set(topology.kernel_connected_outputs) != set(topology.x_connected_outputs):
        _stale("kernel and X connected topologies differ")
    current = CurrentTopology(
        topology.physical_token,
        ExpectedTopology(
            topology.kernel_connected_outputs,
            topology.kernel_external_outputs,
            topology.x_connected_outputs,
            topology.x_active_outputs,
        ),
    )
    validate_topology_guard(request, current)
    profile = staged_profile(request, bundle)
    saved_to_live = {
        item.saved_output: item.live_output for item in request.output_mapping
    }
    if set(saved_to_live) != {item.output for item in profile.setup}:
        _stale("staged setup outputs differ from admitted output mapping")
    patterns = {saved_to_live[item.output]: item.value for item in profile.setup}
    validate_noncontradictory_edids(
        patterns,
        begin_drm.connectors,
        topology,
        allow_temporary_absence=allow_temporary_edid_absence,
    )
    return current


def staged_profile(
    request: TransactionRequest,
    bundle: DesktopPlanBundle,
) -> SavedAutorandrProfile:
    """Parse only the exact autorandr identity artifacts covered by the plan."""
    artifacts = {item.relative_path: item.content for item in bundle.artifacts}
    intent = bundle.plan.autorandr
    try:
        parsed = parse_saved_profile(
            request.profile or "",
            _profile_evidence(
                intent.config_artifact,
                artifacts[intent.config_artifact],
            ),
            _profile_evidence(
                intent.setup_artifact,
                artifacts[intent.setup_artifact],
            ),
            (
                None
                if intent.layout_artifact is None
                else _profile_evidence(
                    intent.layout_artifact,
                    artifacts[intent.layout_artifact],
                )
            ),
        )
    except (KeyError, UnicodeDecodeError) as error:
        _stale(f"staged autorandr identity artifacts are invalid: {error}")
    if not parsed.valid or parsed.profile is None:
        reasons = ",".join(item.code.value for item in parsed.issues)
        _stale(f"staged autorandr identity grammar is invalid: {reasons}")
    return parsed.profile


def _profile_evidence(path: str, content: bytes) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        f"staged-plan:{path}",
        content.decode("utf-8", errors="strict"),
    )


def _stale(detail: str) -> Never:
    raise WorkerStartupError(detail)
