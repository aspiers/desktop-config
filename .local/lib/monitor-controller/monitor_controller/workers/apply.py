"""Guarded, hash-bound, explicit autorandr profile application worker."""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final, Protocol, final

from monitor_controller.model import ActionKind, ActionLifecycle, RawEvidenceSource
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.drm import (
    ReadOnlyTree,
    RootedSysfsReader,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.xrandr import XrandrEvidenceSource
from monitor_controller.runtime.commands import (
    BoundedCommandRunner,
    CommandRequest,
    CommandRunner,
)
from monitor_controller.runtime.transactions import (
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
    COMMAND_NOT_FOUND_EXIT_STATUS,
    COMMAND_TIMEOUT_EXIT_STATUS,
    CommandResult,
    CurrentTopology,
    WorkerExecution,
    WorkerStartup,
    WorkerStartupError,
    execute_worker,
    install_cooperative_sigterm_handler,
    kill_process_group,
    validate_worker_startup,
)
from monitor_controller.workers.common import (
    display_authority_environment as _display_authority_environment,
)
from monitor_controller.workers.common import (
    payload_text as _payload_text,
)
from monitor_controller.workers.common import (
    raise_if_cancelled as _raise_if_cancelled,
)
from monitor_controller.workers.common import (
    sample_exact_topology as _shared_sample_exact_topology,
)
from monitor_controller.workers.common import (
    stale as _stale,
)
from monitor_controller.workers.common import (
    validate_execution_claim as _validate_execution_claim_for,
)
from monitor_controller.workers.identity import validate_noncontradictory_edids

APPLICATION_COMMAND_TIMEOUT_SECONDS: Final = 90.0
POST_ACTION_EVIDENCE_EXIT_STATUS: Final = 65
_XRANDR_QUERY = ("xrandr", "--query")
_XRANDR_PROPERTIES = ("xrandr", "--props")
_AUTORANDR_ARGUMENTS_PREFIX = (
    "autorandr",
    "--skip-options",
    "gamma",
    "--load",
)
_TRUSTED_PATH: Final = "/usr/bin:/bin"


# Shared bounded result shape; retained under the worker's historical name.
ApplyCommandResult = CommandResult


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
                kill_process_group(process)
                process.wait()
                return ApplyCommandResult(
                    COMMAND_TIMEOUT_EXIT_STATUS,
                    timed_out=True,
                )
        except BaseException:
            kill_process_group(process)
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
    """Build the exact autorandr environment and preserve only X authority."""
    artifact_root = startup.store.artifact_directory(startup.request.action_id)
    authority = _display_authority_environment(base_environment, "application")
    profile_directory = artifact_root / "xdg-config" / "autorandr" / action_profile
    values = {
        "DISPLAY": authority["DISPLAY"],
        "HOME": str(artifact_root / "home"),
        "LANG": "C.UTF-8",
        "MONITOR_CONTROLLER_AUTORANDR_ACTION_ID": startup.request.action_id.value,
        "PATH": _TRUSTED_PATH,
        POSTSWITCH_EVIDENCE_ENVIRONMENT: str(
            profile_directory / POSTSWITCH_EVIDENCE_FILENAME
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_DIRS": str(artifact_root / "xdg-config-dirs"),
        "XDG_CONFIG_HOME": str(artifact_root / "xdg-config"),
    }
    values["XAUTHORITY"] = authority["XAUTHORITY"]
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
    sampled = _shared_sample_exact_topology(request, drm_tree, commands)
    current = sampled.current
    validate_noncontradictory_edids(
        {item.output: item.value for item in profile.setup},
        sampled.drm.connectors,
        sampled.topology,
        allow_temporary_absence=False,
    )
    return current


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


def _validate_execution_claim(startup: WorkerStartup) -> None:
    _validate_execution_claim_for(startup, ActionKind.APPLICATION)
