"""Guarded, hash-bound, explicit autorandr profile application worker."""

from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final, Never, Protocol, final

from monitor_controller.model import ActionKind, ActionLifecycle, RawEvidenceSource
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    fingerprint_matches,
    parse_saved_profile,
)
from monitor_controller.observer.drm import (
    ConnectorKind,
    ConnectorStatus,
    DrmConnector,
    EvidenceState,
    ReadOnlyTree,
    RootedSysfsReader,
    sample_drm,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.topology import (
    CanonicalTopologyEvidence,
    derive_canonical_topology,
)
from monitor_controller.observer.xrandr import XrandrEvidenceSource, sample_xrandr
from monitor_controller.runtime.commands import (
    BoundedCommandRunner,
    CommandRequest,
    CommandRunner,
)
from monitor_controller.runtime.transactions import (
    BoundRecordKind,
    ExpectedTopology,
    TransactionArtifact,
    TransactionProtocolError,
    TransactionRequest,
)
from monitor_controller.workers.autorandr_profile import (
    APPLICATION_PAYLOAD_FIELDS,
    POSTSWITCH_CONTENT,
    POSTSWITCH_EVIDENCE_ENVIRONMENT,
    POSTSWITCH_EVIDENCE_FILENAME,
    artifact_hash_matches,
    profile_artifact_path,
)
from monitor_controller.workers.common import (
    CurrentTopology,
    WorkerCancelled,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    execute_worker,
    install_cooperative_sigterm_handler,
    validate_topology_guard,
    validate_worker_startup,
)

APPLICATION_COMMAND_TIMEOUT_SECONDS: Final = 90.0
EDID_BASE_BYTES: Final = 128
EDID_BASE_HEX_CHARS: Final = EDID_BASE_BYTES * 2
COMMAND_NOT_FOUND_EXIT_STATUS: Final = 127
COMMAND_TIMEOUT_EXIT_STATUS: Final = 124
POST_ACTION_EVIDENCE_EXIT_STATUS: Final = 65
MAX_COMMAND_EXIT_STATUS: Final = 255
_XRANDR_QUERY = ("xrandr", "--query")
_XRANDR_PROPERTIES = ("xrandr", "--props")
_AUTORANDR_ARGUMENTS_PREFIX = (
    "autorandr",
    "--skip-options",
    "gamma",
    "--load",
)
_ENVIRONMENT_DENYLIST: Final = frozenset(
    {
        "AUTORANDR_CURRENT_PROFILE",
        "AUTORANDR_MONITORS",
        "AUTORANDR_PROFILE_FOLDER",
        "BASH_ENV",
        "ENV",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "WAYLAND_DISPLAY",
    }
)


@dataclass(frozen=True, slots=True)
class ApplyCommandResult:
    """Bounded terminal status from the sole autorandr mutation command."""

    exit_status: int
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_COMMAND_EXIT_STATUS:
            msg = "apply command exit status must be between zero and 255"
            raise ValueError(msg)
        if self.timed_out and self.exit_status != COMMAND_TIMEOUT_EXIT_STATUS:
            msg = "timed-out apply command requires status 124"
            raise ValueError(msg)


class ApplyCommands(XrandrEvidenceSource, Protocol):
    """Injected fresh XRandR evidence and one exact autorandr load boundary."""

    def load(
        self,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> ApplyCommandResult:
        """Execute the already-validated argument array and environment once."""
        ...


@final
class SubprocessApplyCommands:
    """Production argument-array autorandr adapter with process-group timeout."""

    def __init__(self, reader: CommandRunner | None = None) -> None:
        """Inject only the read-command runner used by parser evidence."""
        self._reader = BoundedCommandRunner() if reader is None else reader

    def query(self) -> TextCommandEvidence:
        """Fresh-sample the documented XRandR topology query."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_QUERY,
                RawEvidenceSource.XRANDR_QUERY,
                "apply:xrandr --query",
            )
        )

    def properties(self) -> TextCommandEvidence:
        """Fresh-sample connector IDs from XRandR properties."""
        return self._reader.run(
            CommandRequest(
                _XRANDR_PROPERTIES,
                RawEvidenceSource.XRANDR_PROPERTIES,
                "apply:xrandr --props",
            )
        )

    def load(
        self,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> ApplyCommandResult:
        """Run only the exact explicit profile-load array in a killable session."""
        try:
            process = subprocess.Popen(  # noqa: S603
                arguments,
                env=dict(environment),
                shell=False,
                start_new_session=True,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        except OSError:
            return ApplyCommandResult(COMMAND_NOT_FOUND_EXIT_STATUS)
        try:
            try:
                status = process.wait(timeout=APPLICATION_COMMAND_TIMEOUT_SECONDS)
            except TimeoutExpired:
                _kill_process_group(process)
                process.wait()
                return ApplyCommandResult(
                    COMMAND_TIMEOUT_EXIT_STATUS,
                    timed_out=True,
                )
        except BaseException:
            _kill_process_group(process)
            with contextlib.suppress(OSError):
                process.wait()
            raise
        if status < 0:
            status = min(255, 128 + abs(status))
        return ApplyCommandResult(min(status, 255))


def run_apply_worker(  # noqa: PLR0913
    *,
    transaction_root: Path,
    action_id_text: str,
    unit_name: str,
    sysfs_root: Path,
    commands: ApplyCommands | None = None,
    drm_tree: ReadOnlyTree | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> int:
    """Validate, claim, guard, and run one exact keyed autorandr application."""
    startup = validate_worker_startup(
        transaction_root=transaction_root,
        action_id_text=action_id_text,
        unit_name=unit_name,
        expected_kind=ActionKind.APPLICATION,
    )
    install_cooperative_sigterm_handler()
    selected_commands = SubprocessApplyCommands() if commands is None else commands
    selected_tree = RootedSysfsReader(sysfs_root) if drm_tree is None else drm_tree
    selected_environment = os.environ if base_environment is None else base_environment
    return execute_application(
        startup,
        drm_tree=selected_tree,
        commands=selected_commands,
        base_environment=selected_environment,
    )


def execute_application(
    startup: WorkerStartup,
    *,
    drm_tree: ReadOnlyTree,
    commands: ApplyCommands,
    base_environment: Mapping[str, str],
) -> int:
    """Execute one dependency-injected worker without implicit profile selection."""
    validated_profile: SavedAutorandrProfile | None = None

    def topology_reader(request: TransactionRequest) -> CurrentTopology:
        nonlocal validated_profile
        _validate_execution_claim(startup)
        _raise_if_cancelled(startup)
        validated_profile = _validate_materialized_profile(startup)
        current = _sample_exact_application_topology(
            request,
            validated_profile,
            drm_tree,
            commands,
        )
        _raise_if_cancelled(startup)
        return current

    def implementation(request: TransactionRequest) -> WorkerExecution:
        profile = validated_profile
        if profile is None:
            _stale("materialized profile was not validated before application")
        _raise_if_cancelled(startup)
        action_profile = _payload_text(request, "action_profile")
        arguments = (*_AUTORANDR_ARGUMENTS_PREFIX, action_profile)
        environment = isolated_application_environment(
            startup,
            action_profile,
            base_environment,
        )
        command = commands.load(arguments, environment)
        if command.timed_out:
            return WorkerExecution(
                ActionLifecycle.TIMED_OUT,
                command.exit_status,
                "explicit autorandr profile load timed out",
            )
        if command.exit_status != 0:
            return WorkerExecution(
                ActionLifecycle.FAILED,
                command.exit_status,
                f"autorandr profile load exited with status {command.exit_status}",
            )
        _raise_if_cancelled(startup)
        try:
            enabled = _read_enabled_output_evidence(startup, action_profile)
        except WorkerStartupError as error:
            detail = " ".join(str(error).split())[:400]
            return WorkerExecution(
                ActionLifecycle.FAILED,
                POST_ACTION_EVIDENCE_EXIT_STATUS,
                f"autorandr postswitch evidence failed validation: {detail}",
            )
        expected_enabled = profile.active_outputs
        if enabled is not None and enabled != expected_enabled:
            return WorkerExecution(
                ActionLifecycle.FAILED,
                POST_ACTION_EVIDENCE_EXIT_STATUS,
                "autorandr postswitch enabled-output evidence differed from profile",
            )
        evidence_detail = (
            "without postswitch enabled-output evidence"
            if enabled is None
            else "for enabled outputs " + ",".join(enabled)
        )
        detail = (
            "explicit transaction-local autorandr profile load completed "
            f"{evidence_detail}"
        )
        return WorkerExecution(ActionLifecycle.COMPLETED, 0, detail)

    return execute_worker(
        startup,
        topology_reader=topology_reader,
        implementation=implementation,
    )


def isolated_application_environment(
    startup: WorkerStartup,
    action_profile: str,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Replace every autorandr configuration root while preserving X authority."""
    artifact_root = startup.store.artifact_directory(startup.request.action_id)
    values = dict(base_environment)
    authority = values.get("XAUTHORITY")
    original_home = values.get("HOME")
    if not authority and values.get("DISPLAY") and original_home:
        candidate = Path(original_home) / ".Xauthority"
        try:
            details = candidate.stat()
        except OSError:
            pass
        else:
            if stat.S_ISREG(details.st_mode) and os.access(candidate, os.R_OK):
                authority = str(candidate)
    for name in _ENVIRONMENT_DENYLIST:
        values.pop(name, None)
    profile_directory = artifact_root / "xdg-config" / "autorandr" / action_profile
    values.update(
        {
            "HOME": str(artifact_root / "home"),
            "MONITOR_CONTROLLER_AUTORANDR_ACTION_ID": startup.request.action_id.value,
            POSTSWITCH_EVIDENCE_ENVIRONMENT: str(
                profile_directory / POSTSWITCH_EVIDENCE_FILENAME
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "XDG_CONFIG_DIRS": str(artifact_root / "xdg-config-dirs"),
            "XDG_CONFIG_HOME": str(artifact_root / "xdg-config"),
        }
    )
    if authority is not None:
        values["XAUTHORITY"] = authority
    return values


def _validate_materialized_profile(  # noqa: C901, PLR0912
    startup: WorkerStartup,
) -> SavedAutorandrProfile:
    request = startup.request
    values = dict(request.payload)
    if frozenset(values) != APPLICATION_PAYLOAD_FIELDS:
        _stale("application request artifact manifest differs from the closed protocol")
    action_profile = _payload_text(request, "action_profile")
    if action_profile != request.action_id.value:
        _stale("application action profile differs from its action ID")

    contents: dict[str, bytes] = {}
    artifacts: list[TransactionArtifact] = []
    for name in ("config", "setup", "postswitch"):
        executable = name == "postswitch"
        path = profile_artifact_path(action_profile, name)
        try:
            content = startup.store.read_artifact(
                request.action_id,
                path,
                executable=executable,
            )
        except (OSError, TransactionProtocolError) as error:
            _stale(f"cannot read immutable {name} artifact: {error}")
        digest = _payload_text(request, f"{name}_sha256")
        if not artifact_hash_matches(content, digest):
            _stale(f"materialized autorandr {name} content hash changed")
        contents[name] = content
        artifacts.append(TransactionArtifact(path, content, executable=executable))

    layout_digest = values["layout_sha256"]
    if layout_digest is not None:
        if not isinstance(layout_digest, str):
            _stale("materialized autorandr layout hash is not text or null")
        path = profile_artifact_path(action_profile, "layout")
        try:
            layout = startup.store.read_artifact(request.action_id, path)
        except (OSError, TransactionProtocolError) as error:
            _stale(f"cannot read immutable layout artifact: {error}")
        if not artifact_hash_matches(layout, layout_digest):
            _stale("materialized autorandr layout content hash changed")
        contents["layout"] = layout
        artifacts.append(TransactionArtifact(path, layout))

    if contents["postswitch"] != POSTSWITCH_CONTENT:
        _stale("materialized postswitch is not the controller no-op evidence hook")
    try:
        startup.store.validate_artifacts(
            request.action_id,
            tuple(sorted(artifacts, key=lambda item: item.relative_path)),
        )
        parsed = parse_saved_profile(
            action_profile,
            _profile_evidence(action_profile, "config", contents["config"]),
            _profile_evidence(action_profile, "setup", contents["setup"]),
            (
                _profile_evidence(action_profile, "layout", contents["layout"])
                if "layout" in contents
                else None
            ),
        )
    except (OSError, TransactionProtocolError, UnicodeDecodeError) as error:
        _stale(f"materialized autorandr artifact validation failed: {error}")
    if not parsed.valid or parsed.profile is None:
        reasons = ",".join(item.code.value for item in parsed.issues)
        _stale(f"materialized autorandr profile grammar is invalid: {reasons}")
    profile = parsed.profile
    mapped_live = {item.live_output for item in request.output_mapping}
    setup_outputs = {item.output for item in profile.setup}
    config_outputs = {item.output for item in profile.config}
    if mapped_live != setup_outputs or setup_outputs != config_outputs:
        _stale("materialized config/setup do not equal the admitted live bijection")
    return profile


def _sample_exact_application_topology(
    request: TransactionRequest,
    profile: SavedAutorandrProfile,
    drm_tree: ReadOnlyTree,
    commands: ApplyCommands,
) -> CurrentTopology:
    begin_drm = sample_drm(drm_tree)
    xrandr = sample_xrandr(commands)
    end_drm = sample_drm(drm_tree)
    if begin_drm != end_drm:
        _stale("DRM evidence changed during the worker-local sample")
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
        physical_token=topology.physical_token,
        topology=ExpectedTopology(
            kernel_connected_outputs=topology.kernel_connected_outputs,
            kernel_external_outputs=topology.kernel_external_outputs,
            x_connected_outputs=topology.x_connected_outputs,
            x_active_outputs=topology.x_active_outputs,
        ),
    )
    validate_topology_guard(request, current)
    _validate_noncontradictory_edids(profile, begin_drm.connectors, topology)
    return current


def _validate_noncontradictory_edids(
    profile: SavedAutorandrProfile,
    connectors: tuple[DrmConnector, ...],
    topology: CanonicalTopologyEvidence,
) -> None:
    # Re-prove every mapped physical identity from the fresh worker-local EDID.
    # A setup wildcard may hide extension churn, but it cannot hide any byte of
    # the 128-byte base block: that would turn a broad autorandr match into
    # authority to mutate an unproved monitor.
    patterns = {item.output: item.value for item in profile.setup}
    proved_outputs: set[str] = set()
    live_bases: list[bytes] = []
    for item in connectors:
        if item.kind is ConnectorKind.VIRTUAL or not item.connected:
            continue
        live_output = topology.live_output_for(item.kernel_name)
        if live_output is None or live_output not in patterns:
            _stale("connected DRM connector lacks its admitted live mapping")
        raw = item.edid.raw
        parsed = item.edid.parsed
        if raw is None or parsed is None or parsed.base_hash is None:
            _stale("fresh connector base identity cannot be proved")
        value = raw.hex()
        pattern = patterns[live_output]
        _prove_fixed_saved_base(pattern, value)
        proved_outputs.add(live_output)
        live_bases.append(raw[:EDID_BASE_BYTES])
        if not parsed.fully_ready:
            continue
        try:
            matches = fingerprint_matches(pattern, value)
        except ValueError as error:
            _stale(f"materialized setup fingerprint is invalid: {error}")
        if not matches:
            _stale("fresh complete connector identity contradicts the admitted mapping")
    if proved_outputs != set(patterns):
        _stale("fresh identity proof does not cover the admitted output mapping")
    if len(live_bases) != len(set(live_bases)):
        _stale("fresh connector base identities collide")


def _prove_fixed_saved_base(pattern: str, live_value: str) -> None:
    """Require every base nibble to be fixed and equal in the saved pattern."""
    if pattern.count("*") > 1 or len(live_value) < EDID_BASE_HEX_CHARS:
        _stale("saved setup cannot prove a complete fresh base identity")
    if "*" in pattern:
        prefix, suffix = pattern.split("*", maxsplit=1)
    else:
        prefix, suffix = pattern, ""
    suffix_start = len(live_value) - len(suffix)
    if suffix_start < 0:
        _stale("saved setup fingerprint contradicts the fresh connector identity")

    fixed: dict[int, str] = {}
    for start, value in ((0, prefix), (suffix_start, suffix)):
        for offset, character in enumerate(value):
            position = start + offset
            previous = fixed.get(position)
            if previous is not None and previous.casefold() != character.casefold():
                _stale(
                    "fresh usable connector identity contradicts the saved fixed bytes"
                )
            fixed[position] = character

    fixed_base_positions = {
        position for position in fixed if 0 <= position < EDID_BASE_HEX_CHARS
    }
    for position in fixed_base_positions:
        if fixed[position].casefold() != live_value[position].casefold():
            _stale("fresh usable connector identity contradicts the admitted mapping")
    if fixed_base_positions != set(range(EDID_BASE_HEX_CHARS)):
        _stale("saved setup cannot prove a complete fresh base identity")


def _read_enabled_output_evidence(
    startup: WorkerStartup,
    action_profile: str,
) -> tuple[str, ...] | None:
    relative_path = (
        profile_artifact_path(action_profile, "postswitch").rsplit("/", maxsplit=1)[0]
        + f"/{POSTSWITCH_EVIDENCE_FILENAME}"
    )
    try:
        content = startup.store.read_artifact(startup.request.action_id, relative_path)
    except FileNotFoundError:
        return None
    except (OSError, TransactionProtocolError) as error:
        _stale(f"postswitch evidence is unsafe: {error}")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _stale("postswitch evidence is not UTF-8")
    if not text.endswith("\n") or "\n" in text[:-1]:
        _stale("postswitch evidence is not one line")
    value = text[:-1]
    outputs = () if not value else tuple(value.split(":"))
    malformed = any(
        not output or any(character.isspace() for character in output)
        for output in outputs
    )
    if malformed or len(outputs) != len(set(outputs)):
        _stale("postswitch enabled-output evidence is malformed")
    return tuple(sorted(outputs))


def _profile_evidence(
    action_profile: str,
    name: str,
    content: bytes,
) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        f"transaction:{action_profile}/{name}",
        content.decode("utf-8", errors="strict"),
    )


def _payload_text(request: TransactionRequest, name: str) -> str:
    try:
        value = request.payload_value(name)
    except TransactionProtocolError as error:
        raise WorkerStartupError(str(error)) from error
    if not isinstance(value, str) or not value:
        _stale(f"application request {name} is not non-empty text")
    return value


def _validate_execution_claim(startup: WorkerStartup) -> None:
    request = startup.request
    claim = startup.execution_claim
    if (
        claim is None
        or claim.record_kind is not BoundRecordKind.EXECUTION_CLAIM
        or claim.action_id != request.action_id
        or claim.action_kind is not ActionKind.APPLICATION
        or claim.unit_name != request.unit_name
        or claim.request_sha256 != request.request_sha256
    ):
        _stale("worker lacks its exact durable execution claim")


def _raise_if_cancelled(startup: WorkerStartup) -> None:
    if startup.store.stop_intent_if_present(startup.request.action_id) is not None:
        raise WorkerCancelled


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _stale(detail: str) -> Never:
    raise WorkerStartupError(detail)
