"""Immutable, hash-bound transaction requests and atomic worker results."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, cast
from uuid import UUID, uuid4

from monitor_controller.model import (
    BROKEN_EXTENSION_EDID_INTEGRITIES,
    TERMINAL_ACTION_LIFECYCLES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ControllerInstanceId,
    EdidIntegrity,
    EventGeneration,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    TransitionId,
    TransitionKey,
)

TRANSACTION_SCHEMA_VERSION: Final = 1
MAX_TRANSACTION_BYTES: Final = 128 * 1024
MAX_EXIT_STATUS: Final = 255
UUID_HEX_LENGTH: Final = 32
_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_EXECUTABLE_FILE_MODE: Final = 0o700
_MIN_DIRECTORY_LINK_COUNT: Final = 2
MAX_TRANSACTION_ARTIFACT_BYTES: Final = 128 * 1024
MAX_TRANSACTION_ARTIFACT_TOTAL_BYTES: Final = 512 * 1024
MAX_TRANSACTION_ARTIFACTS: Final = 32
MAX_TRANSACTION_ARTIFACT_PATH_CHARS: Final = 1_024
MAX_TRANSACTION_ARTIFACT_COMPONENT_CHARS: Final = 255
_ARTIFACT_ROOT: Final = "artifacts"
_MIN_ARTIFACT_PATH_PARTS: Final = 2
_RENAME_NOREPLACE: Final = 1
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTION_ID_PATTERN = re.compile(
    r"^(plan|probe|application|preparation|finalization)-"
    r"([0-9a-f]{32})-([1-9][0-9]*)$"
)
_UNIT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@\\x-]+\.service$")
_BINDING_FILENAMES: Final = {
    "prepared": "prepared.json",
    "submission_claim": "submission-claim.json",
    "execution_claim": "execution-claim.json",
}
_STOPPABLE_LIFECYCLES: Final = frozenset(
    {
        ActionLifecycle.CANCELLED,
        ActionLifecycle.TIMED_OUT,
        ActionLifecycle.UNKNOWN,
    }
)
_DIRECTORY_OPEN_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS: Final = (
    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)

# Action payloads deliberately have a shallow, closed JSON value domain.  The
# protocol grows explicit top-level fields rather than accepting nested ad-hoc data.
type PayloadValue = str | int | bool | None
type Payload = tuple[tuple[str, PayloadValue], ...]


@dataclass(frozen=True, slots=True)
class TransactionArtifact:
    """One immutable non-JSON file published with a transaction request."""

    relative_path: str
    content: bytes
    executable: bool = False

    def __post_init__(self) -> None:
        _validate_artifact_path(self.relative_path)
        if not self.content or len(self.content) > MAX_TRANSACTION_ARTIFACT_BYTES:
            msg = "transaction artifact size is outside the accepted bounds"
            raise TransactionProtocolError(msg)


class TransactionProtocolError(ValueError):
    """A transaction record is malformed, inconsistent, or unsafe to trust."""


class ImmutableTransactionError(TransactionProtocolError):
    """An operation attempted to replace acknowledged transaction evidence."""


class ExecutionAlreadyClaimedError(ImmutableTransactionError):
    """A worker invocation attempted to claim an action more than once."""


class BoundRecordKind(StrEnum):
    """Immutable controller/worker records which independently bind a request."""

    PREPARED = "prepared"
    SUBMISSION_CLAIM = "submission_claim"
    EXECUTION_CLAIM = "execution_claim"


@dataclass(frozen=True, slots=True)
class BoundTransactionRecord:
    """Independent immutable binding of action, unit, and request digest."""

    record_kind: BoundRecordKind
    action_id: ActionId
    action_kind: ActionKind
    unit_name: str
    request_sha256: str
    record_sha256: str = ""

    def __post_init__(self) -> None:
        if self.action_id.kind is not self.action_kind:
            msg = "bound record action kind does not match its action ID"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.PLAN:
            msg = "pure planning tasks cannot have worker binding records"
            raise TransactionProtocolError(msg)
        _validate_unit_name(self.unit_name)
        _validate_sha256(self.request_sha256, "bound record request hash")
        if self.record_sha256:
            _validate_sha256(self.record_sha256, "bound record content hash")
            expected = _content_sha256(_bound_object(self, include_hash=False))
            if self.record_sha256 != expected:
                msg = "bound record content hash does not match canonical content"
                raise TransactionProtocolError(msg)


@dataclass(frozen=True, slots=True)
class StopIntent:
    """Durable terminal intent written before asking systemd to stop a unit."""

    action_id: ActionId
    action_kind: ActionKind
    unit_name: str
    request_sha256: str
    terminal_lifecycle: ActionLifecycle
    intent_sha256: str = ""

    def __post_init__(self) -> None:
        if self.action_id.kind is not self.action_kind:
            msg = "stop intent action kind does not match its action ID"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.PLAN:
            msg = "pure planning tasks cannot have stop intents"
            raise TransactionProtocolError(msg)
        _validate_unit_name(self.unit_name)
        _validate_sha256(self.request_sha256, "stop intent request hash")
        if self.terminal_lifecycle not in _STOPPABLE_LIFECYCLES:
            msg = "stop intent must be cancelled, timed out, or unknown"
            raise TransactionProtocolError(msg)
        if self.intent_sha256:
            _validate_sha256(self.intent_sha256, "stop intent content hash")
            expected = _content_sha256(_stop_intent_object(self, include_hash=False))
            if self.intent_sha256 != expected:
                msg = "stop intent content hash does not match canonical content"
                raise TransactionProtocolError(msg)


@dataclass(frozen=True, slots=True)
class ExpectedTopology:
    """Exact connected and active topology admitted by one observation."""

    kernel_connected_outputs: tuple[str, ...]
    kernel_external_outputs: tuple[str, ...]
    x_connected_outputs: tuple[str, ...]
    x_active_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, values in (
            ("kernel_connected_outputs", self.kernel_connected_outputs),
            ("kernel_external_outputs", self.kernel_external_outputs),
            ("x_connected_outputs", self.x_connected_outputs),
            ("x_active_outputs", self.x_active_outputs),
        ):
            _validate_sorted_unique_strings(values, name)
        if not set(self.kernel_external_outputs) <= set(self.kernel_connected_outputs):
            msg = "expected kernel external outputs must be kernel-connected"
            raise TransactionProtocolError(msg)
        if not set(self.x_active_outputs) <= set(self.x_connected_outputs):
            msg = "expected active X outputs must be X-connected"
            raise TransactionProtocolError(msg)


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    """One immutable worker authorization, including its own content digest."""

    action_id: ActionId
    action_kind: ActionKind
    unit_name: str
    physical_epoch: int
    physical_token: PhysicalToken
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey
    output_mapping: tuple[OutputMapping, ...]
    expected_topology: ExpectedTopology
    profile: str | None = None
    layout: str | None = None
    transition_id: TransitionId | None = None
    transition_key: TransitionKey | None = None
    plan_hash: PlanHash | None = None
    payload: Payload = ()
    request_sha256: str = ""

    def __post_init__(self) -> None:  # noqa: C901, PLR0912
        if self.action_id.kind is not self.action_kind:
            msg = "request action kind does not match its action ID"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.PLAN:
            msg = "pure planning tasks are not systemd workers"
            raise TransactionProtocolError(msg)
        _validate_unit_name(self.unit_name)
        if self.physical_epoch < 0:
            msg = "request physical epoch must be non-negative"
            raise TransactionProtocolError(msg)
        _validate_mapping(self.output_mapping)
        if self.profile is not None:
            _validate_text(self.profile, "request profile")
        if self.layout is not None:
            _validate_text(self.layout, "request layout")
        if (self.transition_id is None) is not (self.transition_key is None):
            msg = "request transition ID and key must either both be set or both absent"
            raise TransactionProtocolError(msg)
        if self.transition_id is not None and (
            self.transition_id.controller_instance != self.action_id.controller_instance
        ):
            msg = "request action and transition must share a controller instance"
            raise TransactionProtocolError(msg)
        if self.action_kind in {ActionKind.PREPARATION, ActionKind.FINALIZATION}:
            if (
                self.transition_id is None
                or self.transition_key is None
                or self.plan_hash is None
                or self.profile is None
                or self.layout is None
            ):
                msg = (
                    "desktop worker request lacks transition, plan, profile, or layout"
                )
                raise TransactionProtocolError(msg)
            if not self.output_mapping:
                msg = "desktop worker request requires an output mapping"
                raise TransactionProtocolError(msg)
        elif self.transition_id is not None or self.transition_key is not None:
            msg = "probe/application request cannot carry a desktop transition"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.APPLICATION and (
            self.profile is None or not self.output_mapping
        ):
            msg = "application request requires a profile and output mapping"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.PROBE:
            if self.output_mapping:
                msg = "probe request cannot claim an unproven profile output mapping"
                raise TransactionProtocolError(msg)
            if self.profile is None:
                msg = "probe request requires its unique base-identity profile"
                raise TransactionProtocolError(msg)
        _validate_payload(self.payload)
        if self.action_kind is ActionKind.PROBE:
            _validate_probe_payload(self)
        if self.request_sha256:
            _validate_sha256(self.request_sha256, "request content hash")
            expected = _content_sha256(_request_object(self, include_hash=False))
            if self.request_sha256 != expected:
                msg = "request content hash does not match its canonical content"
                raise TransactionProtocolError(msg)

    def payload_value(self, name: str) -> PayloadValue:
        """Return one payload value, raising for an absent protocol field."""
        for key, value in self.payload:
            if key == name:
                return value
        msg = f"request payload has no {name!r} field"
        raise TransactionProtocolError(msg)


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """One immutable terminal worker report bound to the request and unit."""

    action_id: ActionId
    action_kind: ActionKind
    unit_name: str
    request_sha256: str
    outcome: ActionLifecycle
    exit_status: int
    started_monotonic_ms: int
    finished_monotonic_ms: int
    detail: str
    plan_hash: PlanHash | None = None
    result_sha256: str = ""

    def __post_init__(self) -> None:  # noqa: C901
        if self.action_id.kind is not self.action_kind:
            msg = "result action kind does not match its action ID"
            raise TransactionProtocolError(msg)
        if self.action_kind is ActionKind.PLAN:
            msg = "pure planning tasks cannot produce systemd worker results"
            raise TransactionProtocolError(msg)
        _validate_unit_name(self.unit_name)
        _validate_sha256(self.request_sha256, "result request hash")
        if not 0 <= self.exit_status <= MAX_EXIT_STATUS:
            msg = "result exit status must be between 0 and 255"
            raise TransactionProtocolError(msg)
        if self.outcome not in TERMINAL_ACTION_LIFECYCLES:
            msg = "result outcome must be an exact terminal action lifecycle"
            raise TransactionProtocolError(msg)
        if self.outcome is ActionLifecycle.COMPLETED and self.exit_status != 0:
            msg = "completed result requires exit status zero"
            raise TransactionProtocolError(msg)
        if self.outcome is not ActionLifecycle.COMPLETED and self.exit_status == 0:
            msg = "non-completed result requires a non-zero exit status"
            raise TransactionProtocolError(msg)
        if self.started_monotonic_ms < 0:
            msg = "result start time must be non-negative"
            raise TransactionProtocolError(msg)
        if self.finished_monotonic_ms < self.started_monotonic_ms:
            msg = "result finish time cannot precede its start time"
            raise TransactionProtocolError(msg)
        _validate_text(self.detail, "result detail", maximum=512)
        if self.result_sha256:
            _validate_sha256(self.result_sha256, "result content hash")
            expected = _content_sha256(_result_object(self, include_hash=False))
            if self.result_sha256 != expected:
                msg = "result content hash does not match its canonical content"
                raise TransactionProtocolError(msg)


class TransactionStore:
    """Operate beneath retained verified directory descriptors, never path walks.

    This eliminates pathname/symlink races between cooperative same-UID processes.
    It is not a sandbox against a malicious process running as the same UID, which
    can inspect this process and rewrite every protocol record it owns.
    """

    def __init__(
        self,
        root: Path,
        *,
        installation_fault: Callable[[str], None] | None = None,
    ) -> None:
        """Bind an absolute active transaction root without creating it."""
        if not root.is_absolute():
            msg = "transaction root must be absolute"
            raise ValueError(msg)
        self._root = root
        self._root_fd = -1
        self._action_fds: dict[ActionId, int] = {}
        self._installation_fault = installation_fault

    def __del__(self) -> None:
        """Release retained descriptors when this capability is collected."""
        self.close()

    def close(self) -> None:
        """Release retained root/action descriptors without changing evidence."""
        for descriptor in self._action_fds.values():
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._action_fds.clear()
        if self._root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._root_fd)
            self._root_fd = -1

    @classmethod
    def from_runtime_dir(cls, runtime_dir: Path) -> TransactionStore:
        """Use the production active namespace below ``XDG_RUNTIME_DIR``."""
        if not runtime_dir.is_absolute():
            msg = "XDG runtime directory must be absolute"
            raise ValueError(msg)
        return cls(runtime_dir / "monitor-system" / "transactions")

    @property
    def root(self) -> Path:
        """Return the configured path for diagnostics and systemd arguments only."""
        return self._root

    def action_directory(self, action_id: ActionId) -> Path:
        """Return a diagnostic reference; protocol I/O never dereferences it."""
        return self._root / action_id.value

    def create_request(
        self,
        request: TransactionRequest,
        artifacts: tuple[TransactionArtifact, ...] = (),
    ) -> TransactionRequest:
        """Atomically install request, metadata, and immutable action artifacts."""
        artifacts = _validated_artifacts(artifacts)
        request = with_request_hash(request)
        prepared = with_bound_record_hash(
            BoundTransactionRecord(
                BoundRecordKind.PREPARED,
                request.action_id,
                request.action_kind,
                request.unit_name,
                request.request_sha256,
            )
        )
        try:
            descriptor = self._action_descriptor(request.action_id)
        except FileNotFoundError:
            self._install_prepared_bundle(request, prepared, artifacts)
            descriptor = self._action_descriptor(request.action_id)
        existing = self._read_request_at(descriptor, request.action_id)
        existing_prepared = self._read_bound_record_at(
            descriptor,
            BoundRecordKind.PREPARED,
        )
        _validate_bound_record(existing, existing_prepared)
        if existing != request or existing_prepared != prepared:
            msg = f"request for {request.action_id.value} is already immutable"
            raise ImmutableTransactionError(msg)
        if artifacts:
            self._validate_artifacts_at(descriptor, artifacts)
        return existing

    def _install_prepared_bundle(
        self,
        request: TransactionRequest,
        prepared: BoundTransactionRecord,
        artifacts: tuple[TransactionArtifact, ...],
    ) -> None:
        """Publish only a fully synced two-file transaction directory."""
        root_descriptor = self._root_descriptor(create=True)
        temporary_name = f".{request.action_id.value}.{uuid4().hex}.prepare"
        temporary_descriptor = -1
        installed = False
        try:
            os.mkdir(temporary_name, _DIRECTORY_MODE, dir_fd=root_descriptor)
            temporary_descriptor = os.open(
                temporary_name,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=root_descriptor,
            )
            os.fchmod(temporary_descriptor, _DIRECTORY_MODE)
            _validate_directory_descriptor(
                temporary_descriptor,
                "temporary transaction action directory",
            )
            self._inject_installation_fault("temporary_directory_created")
            _atomic_create_at(
                temporary_descriptor,
                "request.json",
                encode_request(request),
            )
            self._inject_installation_fault("request_installed")
            _atomic_create_at(
                temporary_descriptor,
                "prepared.json",
                encode_bound_record(prepared),
            )
            self._inject_installation_fault("prepared_installed")
            _install_artifacts_at(temporary_descriptor, artifacts)
            self._inject_installation_fault("artifacts_installed")
            _sync_descriptor(temporary_descriptor)
            self._inject_installation_fault("bundle_synced")
            _rename_noreplace_at(
                root_descriptor,
                temporary_name,
                request.action_id.value,
            )
            installed = True
            self._inject_installation_fault("bundle_published")
            _sync_descriptor(root_descriptor)
            self._inject_installation_fault("parent_synced")
        except FileExistsError:
            # A concurrent or retried creator won the immutable action identity.
            # The caller validates its complete bundle before treating it as equal.
            _remove_temporary_directory_at(
                root_descriptor,
                temporary_descriptor,
                temporary_name,
            )
        except Exception:
            if installed:
                raise
            _remove_temporary_directory_at(
                root_descriptor,
                temporary_descriptor,
                temporary_name,
            )
            raise
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)

    def _inject_installation_fault(self, boundary: str) -> None:
        injector = self._installation_fault
        if injector is not None:
            injector(boundary)

    def read_request(self, action_id: ActionId) -> TransactionRequest:
        """Read request and independently verify the controller-prepared digest."""
        descriptor = self._action_descriptor(action_id)
        request = self._read_request_at(descriptor, action_id)
        prepared = self._read_bound_record_at(descriptor, BoundRecordKind.PREPARED)
        _validate_bound_record(request, prepared)
        return request

    def validate_artifacts(
        self,
        action_id: ActionId,
        artifacts: tuple[TransactionArtifact, ...],
    ) -> None:
        """Validate the exact immutable artifact tree for one bound request."""
        _ = self.read_request(action_id)
        self._validate_artifacts_at(
            self._action_descriptor(action_id),
            _validated_artifacts(artifacts),
        )

    def read_artifact(
        self,
        action_id: ActionId,
        relative_path: str,
        *,
        executable: bool = False,
    ) -> bytes:
        """Read one action artifact through verified no-follow descriptors."""
        _ = self.read_request(action_id)
        descriptor, leaf = _open_artifact_parent_at(
            self._action_descriptor(action_id),
            relative_path,
            create=False,
        )
        try:
            return _read_regular_file_at(
                descriptor,
                leaf,
                expected_mode=(_EXECUTABLE_FILE_MODE if executable else _FILE_MODE),
            )
        finally:
            os.close(descriptor)

    def artifact_directory(self, action_id: ActionId) -> Path:
        """Return the fixed action artifact root used as a subprocess capability."""
        return self.action_directory(action_id) / _ARTIFACT_ROOT

    def _validate_artifacts_at(
        self,
        action_descriptor: int,
        artifacts: tuple[TransactionArtifact, ...],
    ) -> None:
        expected_paths = tuple(item.relative_path for item in artifacts)
        actual_paths = _artifact_file_paths_at(action_descriptor)
        if actual_paths != expected_paths:
            msg = "transaction artifact tree differs from its exact manifest"
            raise TransactionProtocolError(msg)
        for artifact in artifacts:
            descriptor, leaf = _open_artifact_parent_at(
                action_descriptor,
                artifact.relative_path,
                create=False,
            )
            try:
                actual = _read_regular_file_at(
                    descriptor,
                    leaf,
                    expected_mode=(
                        _EXECUTABLE_FILE_MODE if artifact.executable else _FILE_MODE
                    ),
                )
            finally:
                os.close(descriptor)
            if actual != artifact.content:
                msg = f"transaction artifact {artifact.relative_path} changed"
                raise ImmutableTransactionError(msg)

    def read_prepared(self, action_id: ActionId) -> BoundTransactionRecord:
        """Return the independently hash-protected controller preparation record."""
        request = self.read_request(action_id)
        prepared = self._read_bound_record_at(
            self._action_descriptor(action_id),
            BoundRecordKind.PREPARED,
        )
        _validate_bound_record(request, prepared)
        return prepared

    def claim_submission(self, action_id: ActionId) -> BoundTransactionRecord:
        """Claim the sole manager submission after the final controller fence."""
        request = self.read_request(action_id)
        claim = with_bound_record_hash(
            BoundTransactionRecord(
                BoundRecordKind.SUBMISSION_CLAIM,
                request.action_id,
                request.action_kind,
                request.unit_name,
                request.request_sha256,
            )
        )
        descriptor = self._action_descriptor(action_id)
        try:
            _atomic_create_at(
                descriptor,
                "submission-claim.json",
                encode_bound_record(claim),
            )
        except FileExistsError:
            existing = self._read_bound_record_at(
                descriptor,
                BoundRecordKind.SUBMISSION_CLAIM,
            )
            _validate_bound_record(request, existing)
            msg = f"submission for {action_id.value} was already claimed"
            raise ExecutionAlreadyClaimedError(msg) from None
        return claim

    def read_submission_claim(self, action_id: ActionId) -> BoundTransactionRecord:
        """Read and validate the sole durable manager-submission claim."""
        request = self.read_request(action_id)
        claim = self._read_bound_record_at(
            self._action_descriptor(action_id),
            BoundRecordKind.SUBMISSION_CLAIM,
        )
        _validate_bound_record(request, claim)
        return claim

    def submission_claim_if_present(
        self, action_id: ActionId
    ) -> BoundTransactionRecord | None:
        """Return validated submission evidence, or only ``None`` for ENOENT."""
        if not self._record_present(action_id, "submission-claim.json"):
            return None
        return self.read_submission_claim(action_id)

    def claim_execution(self, action_id: ActionId) -> BoundTransactionRecord:
        """Atomically claim worker execution; every repeat is rejected forever."""
        request = self.read_request(action_id)
        self._require_submission(action_id, "execution claim")
        claim = with_bound_record_hash(
            BoundTransactionRecord(
                BoundRecordKind.EXECUTION_CLAIM,
                request.action_id,
                request.action_kind,
                request.unit_name,
                request.request_sha256,
            )
        )
        descriptor = self._action_descriptor(action_id)
        try:
            _atomic_create_at(
                descriptor,
                "execution-claim.json",
                encode_bound_record(claim),
            )
        except FileExistsError:
            existing = self._read_bound_record_at(
                descriptor,
                BoundRecordKind.EXECUTION_CLAIM,
            )
            _validate_bound_record(request, existing)
            msg = f"execution for {action_id.value} was already claimed"
            raise ExecutionAlreadyClaimedError(msg) from None
        return claim

    def read_execution_claim(self, action_id: ActionId) -> BoundTransactionRecord:
        """Read and validate the exact durable worker execution claim."""
        request = self.read_request(action_id)
        self._require_submission(action_id, "execution claim")
        claim = self._read_bound_record_at(
            self._action_descriptor(action_id),
            BoundRecordKind.EXECUTION_CLAIM,
        )
        _validate_bound_record(request, claim)
        return claim

    def execution_claim_if_present(
        self, action_id: ActionId
    ) -> BoundTransactionRecord | None:
        """Return a validated execution claim, or only ``None`` for ENOENT."""
        if not self._record_present(action_id, "execution-claim.json"):
            return None
        return self.read_execution_claim(action_id)

    def create_stop_intent(
        self,
        action_id: ActionId,
        terminal_lifecycle: ActionLifecycle,
    ) -> StopIntent:
        """Persist exact stop semantics before any manager cancellation request."""
        request = self.read_request(action_id)
        self._require_submission(request.action_id, "stop intent")
        intent = with_stop_intent_hash(
            StopIntent(
                request.action_id,
                request.action_kind,
                request.unit_name,
                request.request_sha256,
                terminal_lifecycle,
            )
        )
        descriptor = self._action_descriptor(action_id)
        try:
            _atomic_create_at(
                descriptor,
                "stop-intent.json",
                encode_stop_intent(intent),
            )
        except FileExistsError:
            existing = self.read_stop_intent(action_id)
            if existing != intent:
                msg = f"stop intent for {action_id.value} is already immutable"
                raise ImmutableTransactionError(msg) from None
            return existing
        return intent

    def read_stop_intent(self, action_id: ActionId) -> StopIntent:
        """Read and validate one request-bound stop intent."""
        request = self.read_request(action_id)
        intent = decode_stop_intent(
            _read_regular_file_at(
                self._action_descriptor(action_id),
                "stop-intent.json",
            )
        )
        _validate_stop_intent_binding(request, intent)
        self._require_submission(action_id, "stop intent")
        return intent

    def stop_intent_if_present(self, action_id: ActionId) -> StopIntent | None:
        """Return a validated stop intent, or only ``None`` for ENOENT."""
        if not self._record_present(action_id, "stop-intent.json"):
            return None
        return self.read_stop_intent(action_id)

    def write_result(self, result: TransactionResult) -> TransactionResult:
        """Atomically install a terminal result without replacing an earlier one."""
        request = self.read_request(result.action_id)
        result = with_result_hash(result)
        _validate_result_binding(request, result)
        self._validate_result_authorization(result)
        descriptor = self._action_descriptor(result.action_id)
        try:
            _atomic_create_at(descriptor, "result.json", encode_result(result))
        except FileExistsError:
            existing = self.read_result(result.action_id)
            if existing != result:
                msg = f"result for {result.action_id.value} is already immutable"
                raise ImmutableTransactionError(msg) from None
            return existing
        return result

    def read_result(self, action_id: ActionId) -> TransactionResult:
        """Read one result and verify its exact request/unit/hash binding."""
        request = self.read_request(action_id)
        result = decode_result(
            _read_regular_file_at(self._action_descriptor(action_id), "result.json")
        )
        _validate_result_binding(request, result)
        self._validate_result_authorization(result)
        return result

    def result_if_present(self, action_id: ActionId) -> TransactionResult | None:
        """Return a validated result, or ``None`` only when no file exists."""
        if not self._record_present(action_id, "result.json"):
            return None
        return self.read_result(action_id)

    def discard_unacknowledged(self, request: TransactionRequest) -> None:
        """Remove only request/prepared evidence proven never submitted."""
        existing = self.read_request(request.action_id)
        if existing != with_request_hash(request):
            msg = "prepared request identity changed before cleanup"
            raise ImmutableTransactionError(msg)
        descriptor = self._action_descriptor(request.action_id)
        try:
            names = tuple(sorted(os.listdir(descriptor)))  # noqa: PTH208
        except OSError as error:
            msg = "prepared transaction cannot be safely enumerated"
            raise ImmutableTransactionError(msg) from error
        allowed = {"prepared.json", "request.json", _ARTIFACT_ROOT}
        if set(names) - allowed or not {"prepared.json", "request.json"} <= set(names):
            msg = "cannot discard a transaction containing submission evidence"
            raise ImmutableTransactionError(msg)
        try:
            if _ARTIFACT_ROOT in names:
                _remove_child_directory_at(descriptor, _ARTIFACT_ROOT)
            os.unlink("prepared.json", dir_fd=descriptor)
            os.unlink("request.json", dir_fd=descriptor)
            _sync_descriptor(descriptor)
        except OSError as error:
            msg = "prepared transaction evidence changed during cleanup"
            raise ImmutableTransactionError(msg) from error
        root_descriptor = self._root_descriptor(create=False)
        try:
            os.rmdir(request.action_id.value, dir_fd=root_descriptor)
        except OSError as error:
            msg = "transaction directory contains unexpected evidence"
            raise ImmutableTransactionError(msg) from error
        cached = self._action_fds.pop(request.action_id)
        os.close(cached)
        _sync_descriptor(root_descriptor)

    def action_directories(self) -> tuple[Path, ...]:
        """List beneath the retained root descriptor for fail-closed scanning."""
        descriptor = self._root_descriptor(create=False)
        if descriptor < 0:
            return ()
        _validate_directory_descriptor(descriptor, "transaction root")
        try:
            names = tuple(
                sorted(
                    name
                    for name in os.listdir(descriptor)  # noqa: PTH208
                    if not name.startswith(".")
                )
            )
        except OSError as error:
            msg = "transaction root cannot be safely enumerated"
            raise TransactionProtocolError(msg) from error
        _validate_directory_descriptor(descriptor, "transaction root")
        return tuple(self._root / name for name in names)

    def _record_present(self, action_id: ActionId, name: str) -> bool:
        """Probe only one exact record, propagating all non-ENOENT failures."""
        try:
            descriptor = self._action_descriptor(action_id)
            _read_regular_file_at(descriptor, name)
        except FileNotFoundError:
            return False
        return True

    def _require_submission(self, action_id: ActionId, record_name: str) -> None:
        try:
            self.read_submission_claim(action_id)
        except FileNotFoundError as error:
            msg = f"{record_name} requires a durable manager-submission claim"
            raise TransactionProtocolError(msg) from error

    def _validate_result_authorization(self, result: TransactionResult) -> None:
        self._require_submission(result.action_id, "worker result")
        if result.outcome is not ActionLifecycle.COMPLETED:
            return
        try:
            self.read_execution_claim(result.action_id)
        except FileNotFoundError as error:
            msg = "completed worker result requires a durable execution claim"
            raise TransactionProtocolError(msg) from error

    def _read_request_at(
        self,
        descriptor: int,
        action_id: ActionId,
    ) -> TransactionRequest:
        request = decode_request(_read_regular_file_at(descriptor, "request.json"))
        if request.action_id != action_id:
            msg = "request path and action identity do not match"
            raise TransactionProtocolError(msg)
        return request

    def _read_bound_record_at(
        self,
        descriptor: int,
        kind: BoundRecordKind,
    ) -> BoundTransactionRecord:
        record = decode_bound_record(
            _read_regular_file_at(descriptor, _BINDING_FILENAMES[kind.value])
        )
        if record.record_kind is not kind:
            msg = "bound transaction record has the wrong semantic kind"
            raise TransactionProtocolError(msg)
        return record

    def _root_descriptor(self, *, create: bool) -> int:
        if self._root_fd >= 0:
            _validate_directory_descriptor(self._root_fd, "transaction root")
            return self._root_fd
        descriptor = _open_absolute_directory(self._root, create=create)
        if descriptor < 0:
            return descriptor
        _validate_directory_descriptor(descriptor, "transaction root")
        self._root_fd = descriptor
        return descriptor

    def _action_descriptor(self, action_id: ActionId, *, create: bool = False) -> int:
        cached = self._action_fds.get(action_id)
        if cached is not None:
            _validate_directory_descriptor(cached, "transaction action directory")
            return cached
        root_descriptor = self._root_descriptor(create=create)
        if root_descriptor < 0:
            raise FileNotFoundError(self._root)
        created = False
        if create:
            try:
                os.mkdir(action_id.value, _DIRECTORY_MODE, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            else:
                created = True
                _sync_descriptor(root_descriptor)
        try:
            descriptor = os.open(
                action_id.value,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=root_descriptor,
            )
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise
            msg = "cannot safely open transaction action directory"
            raise TransactionProtocolError(msg) from error
        try:
            if created:
                os.fchmod(descriptor, _DIRECTORY_MODE)
            _validate_directory_descriptor(
                descriptor,
                "transaction action directory",
            )
            _validate_directory_descriptor(root_descriptor, "transaction root")
        except Exception:
            os.close(descriptor)
            raise
        self._action_fds[action_id] = descriptor
        return descriptor


def with_request_hash(request: TransactionRequest) -> TransactionRequest:
    """Return *request* with the digest of all other canonical fields."""
    digest = _content_sha256(_request_object(request, include_hash=False))
    if request.request_sha256 and request.request_sha256 != digest:
        msg = "supplied request hash does not match request content"
        raise TransactionProtocolError(msg)
    return TransactionRequest(
        action_id=request.action_id,
        action_kind=request.action_kind,
        unit_name=request.unit_name,
        physical_epoch=request.physical_epoch,
        physical_token=request.physical_token,
        admitted_event_generation=request.admitted_event_generation,
        observation_key=request.observation_key,
        output_mapping=request.output_mapping,
        expected_topology=request.expected_topology,
        profile=request.profile,
        layout=request.layout,
        transition_id=request.transition_id,
        transition_key=request.transition_key,
        plan_hash=request.plan_hash,
        payload=request.payload,
        request_sha256=digest,
    )


def with_bound_record_hash(
    record: BoundTransactionRecord,
) -> BoundTransactionRecord:
    """Return a prepared/claim record with its independent canonical digest."""
    digest = _content_sha256(_bound_object(record, include_hash=False))
    if record.record_sha256 and record.record_sha256 != digest:
        msg = "supplied bound record hash does not match record content"
        raise TransactionProtocolError(msg)
    return BoundTransactionRecord(
        record_kind=record.record_kind,
        action_id=record.action_id,
        action_kind=record.action_kind,
        unit_name=record.unit_name,
        request_sha256=record.request_sha256,
        record_sha256=digest,
    )


def with_stop_intent_hash(intent: StopIntent) -> StopIntent:
    """Return a stop intent with its canonical independent digest."""
    digest = _content_sha256(_stop_intent_object(intent, include_hash=False))
    if intent.intent_sha256 and intent.intent_sha256 != digest:
        msg = "supplied stop intent hash does not match intent content"
        raise TransactionProtocolError(msg)
    return StopIntent(
        action_id=intent.action_id,
        action_kind=intent.action_kind,
        unit_name=intent.unit_name,
        request_sha256=intent.request_sha256,
        terminal_lifecycle=intent.terminal_lifecycle,
        intent_sha256=digest,
    )


def with_result_hash(result: TransactionResult) -> TransactionResult:
    """Return *result* with the digest of all other canonical fields."""
    digest = _content_sha256(_result_object(result, include_hash=False))
    if result.result_sha256 and result.result_sha256 != digest:
        msg = "supplied result hash does not match result content"
        raise TransactionProtocolError(msg)
    return TransactionResult(
        action_id=result.action_id,
        action_kind=result.action_kind,
        unit_name=result.unit_name,
        request_sha256=result.request_sha256,
        outcome=result.outcome,
        exit_status=result.exit_status,
        started_monotonic_ms=result.started_monotonic_ms,
        finished_monotonic_ms=result.finished_monotonic_ms,
        detail=result.detail,
        plan_hash=result.plan_hash,
        result_sha256=digest,
    )


def encode_request(request: TransactionRequest) -> bytes:
    """Encode one complete request as bounded canonical UTF-8 JSON."""
    return _encode(_request_object(with_request_hash(request), include_hash=True))


def decode_request(payload: bytes) -> TransactionRequest:
    """Strictly decode and hash-validate one complete request."""
    raw = _decode_object(payload)
    _require_keys(
        raw,
        {
            "schema_version",
            "action_id",
            "action_kind",
            "unit_name",
            "physical_epoch",
            "physical_token",
            "admitted_event_generation",
            "observation_key",
            "output_mapping",
            "expected_topology",
            "profile",
            "layout",
            "transition_id",
            "transition_key",
            "plan_hash",
            "payload",
            "request_sha256",
        },
        "request",
    )
    _require_schema(raw)
    action_id = parse_action_id(_string(raw["action_id"], "action_id"))
    action_kind = _action_kind(raw["action_kind"], "action_kind")
    transition_raw = raw["transition_id"]
    transition_id = (
        None
        if transition_raw is None
        else parse_transition_id(_string(transition_raw, "transition_id"))
    )
    return TransactionRequest(
        action_id=action_id,
        action_kind=action_kind,
        unit_name=_string(raw["unit_name"], "unit_name"),
        physical_epoch=_integer(raw["physical_epoch"], "physical_epoch"),
        physical_token=PhysicalToken(_string(raw["physical_token"], "physical_token")),
        admitted_event_generation=EventGeneration(
            _integer(raw["admitted_event_generation"], "admitted_event_generation")
        ),
        observation_key=ObservationKey(
            _string(raw["observation_key"], "observation_key")
        ),
        output_mapping=_decode_mapping(raw["output_mapping"]),
        expected_topology=_decode_topology(raw["expected_topology"]),
        profile=_optional_string(raw["profile"], "profile"),
        layout=_optional_string(raw["layout"], "layout"),
        transition_id=transition_id,
        transition_key=(
            None
            if raw["transition_key"] is None
            else TransitionKey(_string(raw["transition_key"], "transition_key"))
        ),
        plan_hash=(
            None
            if raw["plan_hash"] is None
            else PlanHash(_string(raw["plan_hash"], "plan_hash"))
        ),
        payload=_decode_payload(raw["payload"]),
        request_sha256=_string(raw["request_sha256"], "request_sha256"),
    )


def encode_bound_record(record: BoundTransactionRecord) -> bytes:
    """Encode a controller-prepared or atomic worker-claim binding."""
    return _encode(_bound_object(with_bound_record_hash(record), include_hash=True))


def decode_bound_record(payload: bytes) -> BoundTransactionRecord:
    """Decode and hash-validate one independent request binding."""
    raw = _decode_object(payload)
    _require_keys(
        raw,
        {
            "schema_version",
            "record_kind",
            "action_id",
            "action_kind",
            "unit_name",
            "request_sha256",
            "record_sha256",
        },
        "bound transaction record",
    )
    _require_schema(raw)
    try:
        kind = BoundRecordKind(_string(raw["record_kind"], "record_kind"))
    except ValueError as error:
        msg = "invalid bound transaction record kind"
        raise TransactionProtocolError(msg) from error
    return BoundTransactionRecord(
        record_kind=kind,
        action_id=parse_action_id(_string(raw["action_id"], "action_id")),
        action_kind=_action_kind(raw["action_kind"], "action_kind"),
        unit_name=_string(raw["unit_name"], "unit_name"),
        request_sha256=_string(raw["request_sha256"], "request_sha256"),
        record_sha256=_string(raw["record_sha256"], "record_sha256"),
    )


def encode_stop_intent(intent: StopIntent) -> bytes:
    """Encode one immutable request-bound stop intent."""
    return _encode(
        _stop_intent_object(with_stop_intent_hash(intent), include_hash=True)
    )


def decode_stop_intent(payload: bytes) -> StopIntent:
    """Decode and hash-validate one exact terminal stop intent."""
    raw = _decode_object(payload)
    _require_keys(
        raw,
        {
            "schema_version",
            "action_id",
            "action_kind",
            "unit_name",
            "request_sha256",
            "terminal_lifecycle",
            "intent_sha256",
        },
        "stop intent",
    )
    _require_schema(raw)
    return StopIntent(
        action_id=parse_action_id(_string(raw["action_id"], "action_id")),
        action_kind=_action_kind(raw["action_kind"], "action_kind"),
        unit_name=_string(raw["unit_name"], "unit_name"),
        request_sha256=_string(raw["request_sha256"], "request_sha256"),
        terminal_lifecycle=_terminal_lifecycle(
            raw["terminal_lifecycle"],
            "terminal_lifecycle",
        ),
        intent_sha256=_string(raw["intent_sha256"], "intent_sha256"),
    )


def encode_result(result: TransactionResult) -> bytes:
    """Encode one complete result as bounded canonical UTF-8 JSON."""
    return _encode(_result_object(with_result_hash(result), include_hash=True))


def decode_result(payload: bytes) -> TransactionResult:
    """Strictly decode and hash-validate one complete result."""
    raw = _decode_object(payload)
    _require_keys(
        raw,
        {
            "schema_version",
            "action_id",
            "action_kind",
            "unit_name",
            "request_sha256",
            "outcome",
            "exit_status",
            "started_monotonic_ms",
            "finished_monotonic_ms",
            "detail",
            "plan_hash",
            "result_sha256",
        },
        "result",
    )
    _require_schema(raw)
    return TransactionResult(
        action_id=parse_action_id(_string(raw["action_id"], "action_id")),
        action_kind=_action_kind(raw["action_kind"], "action_kind"),
        unit_name=_string(raw["unit_name"], "unit_name"),
        request_sha256=_string(raw["request_sha256"], "request_sha256"),
        outcome=_terminal_lifecycle(raw["outcome"], "outcome"),
        exit_status=_integer(raw["exit_status"], "exit_status"),
        started_monotonic_ms=_integer(
            raw["started_monotonic_ms"], "started_monotonic_ms"
        ),
        finished_monotonic_ms=_integer(
            raw["finished_monotonic_ms"], "finished_monotonic_ms"
        ),
        detail=_string(raw["detail"], "detail"),
        plan_hash=(
            None
            if raw["plan_hash"] is None
            else PlanHash(_string(raw["plan_hash"], "plan_hash"))
        ),
        result_sha256=_string(raw["result_sha256"], "result_sha256"),
    )


def parse_action_id(value: str) -> ActionId:
    """Parse the sole language-neutral action identity representation."""
    match = _ACTION_ID_PATTERN.fullmatch(value)
    if match is None:
        msg = "malformed action ID"
        raise TransactionProtocolError(msg)
    kind_text, instance_text, sequence_text = match.groups()
    try:
        instance = ControllerInstanceId(UUID(hex=instance_text))
        sequence = int(sequence_text)
    except ValueError as error:
        msg = "malformed action ID"
        raise TransactionProtocolError(msg) from error
    return ActionId(instance, ActionKind(kind_text), sequence)


def parse_transition_id(value: str) -> TransitionId:
    """Parse and validate one transition identity."""
    prefix = "transition-"
    if not value.startswith(prefix):
        msg = "malformed transition ID"
        raise TransactionProtocolError(msg)
    remainder = value.removeprefix(prefix)
    instance_text, separator, sequence_text = remainder.rpartition("-")
    if (
        not separator
        or len(instance_text) != UUID_HEX_LENGTH
        or not sequence_text.isascii()
        or not sequence_text.isdigit()
        or sequence_text.startswith("0")
    ):
        msg = "malformed transition ID"
        raise TransactionProtocolError(msg)
    try:
        instance = ControllerInstanceId(UUID(hex=instance_text))
        sequence = int(sequence_text)
    except ValueError as error:
        msg = "malformed transition ID"
        raise TransactionProtocolError(msg) from error
    return TransitionId(instance, sequence)


def _request_object(
    request: TransactionRequest, *, include_hash: bool
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "action_id": request.action_id.value,
        "action_kind": request.action_kind.value,
        "unit_name": request.unit_name,
        "physical_epoch": request.physical_epoch,
        "physical_token": request.physical_token.value,
        "admitted_event_generation": request.admitted_event_generation.value,
        "observation_key": request.observation_key.value,
        "output_mapping": [
            {"saved_output": item.saved_output, "live_output": item.live_output}
            for item in request.output_mapping
        ],
        "expected_topology": {
            "kernel_connected_outputs": list(
                request.expected_topology.kernel_connected_outputs
            ),
            "kernel_external_outputs": list(
                request.expected_topology.kernel_external_outputs
            ),
            "x_connected_outputs": list(request.expected_topology.x_connected_outputs),
            "x_active_outputs": list(request.expected_topology.x_active_outputs),
        },
        "profile": request.profile,
        "layout": request.layout,
        "transition_id": (
            None if request.transition_id is None else request.transition_id.value
        ),
        "transition_key": (
            None if request.transition_key is None else request.transition_key.value
        ),
        "plan_hash": None if request.plan_hash is None else request.plan_hash.value,
        "payload": dict(request.payload),
    }
    if include_hash:
        result["request_sha256"] = request.request_sha256
    return result


def _bound_object(
    record: BoundTransactionRecord,
    *,
    include_hash: bool,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "record_kind": record.record_kind.value,
        "action_id": record.action_id.value,
        "action_kind": record.action_kind.value,
        "unit_name": record.unit_name,
        "request_sha256": record.request_sha256,
    }
    if include_hash:
        raw["record_sha256"] = record.record_sha256
    return raw


def _stop_intent_object(
    intent: StopIntent,
    *,
    include_hash: bool,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "action_id": intent.action_id.value,
        "action_kind": intent.action_kind.value,
        "unit_name": intent.unit_name,
        "request_sha256": intent.request_sha256,
        "terminal_lifecycle": intent.terminal_lifecycle.value,
    }
    if include_hash:
        raw["intent_sha256"] = intent.intent_sha256
    return raw


def _result_object(
    result: TransactionResult, *, include_hash: bool
) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "action_id": result.action_id.value,
        "action_kind": result.action_kind.value,
        "unit_name": result.unit_name,
        "request_sha256": result.request_sha256,
        "outcome": result.outcome.value,
        "exit_status": result.exit_status,
        "started_monotonic_ms": result.started_monotonic_ms,
        "finished_monotonic_ms": result.finished_monotonic_ms,
        "detail": result.detail,
        "plan_hash": None if result.plan_hash is None else result.plan_hash.value,
    }
    if include_hash:
        raw["result_sha256"] = result.result_sha256
    return raw


def _validate_bound_record(
    request: TransactionRequest,
    record: BoundTransactionRecord,
) -> None:
    if (
        record.action_id != request.action_id
        or record.action_kind is not request.action_kind
        or record.unit_name != request.unit_name
        or record.request_sha256 != request.request_sha256
    ):
        msg = "bound record does not match the exact request action, unit, and digest"
        raise TransactionProtocolError(msg)


def _validate_stop_intent_binding(
    request: TransactionRequest,
    intent: StopIntent,
) -> None:
    if (
        intent.action_id != request.action_id
        or intent.action_kind is not request.action_kind
        or intent.unit_name != request.unit_name
        or intent.request_sha256 != request.request_sha256
    ):
        msg = "stop intent does not match the exact request action, unit, and digest"
        raise TransactionProtocolError(msg)


def _validate_result_binding(
    request: TransactionRequest, result: TransactionResult
) -> None:
    if (
        result.action_id != request.action_id
        or result.action_kind is not request.action_kind
        or result.unit_name != request.unit_name
        or result.request_sha256 != request.request_sha256
    ):
        msg = "result is not bound to the exact request action and unit"
        raise TransactionProtocolError(msg)
    if result.plan_hash != request.plan_hash:
        msg = "result plan hash does not match its request"
        raise TransactionProtocolError(msg)


def _validate_probe_payload(request: TransactionRequest) -> None:
    if request.layout is not None or request.plan_hash is not None:
        msg = "probe request cannot carry layout or staged-plan authority"
        raise TransactionProtocolError(msg)
    expected = {
        "base_identity_hash",
        "edid_integrity",
        "internal_output",
        "preferred_mode",
        "probe_output",
    }
    values = dict(request.payload)
    if set(values) != expected:
        msg = "probe request payload must contain exactly its five proof fields"
        raise TransactionProtocolError(msg)
    base_hash = values["base_identity_hash"]
    if (
        not isinstance(base_hash, str)
        or _SHA256_PATTERN.fullmatch(f"sha256:{base_hash}") is None
    ):
        msg = "probe request base identity must be a lowercase SHA-256 digest"
        raise TransactionProtocolError(msg)
    try:
        integrity = EdidIntegrity(values["edid_integrity"])
    except (TypeError, ValueError) as error:
        msg = "probe request extension integrity is invalid"
        raise TransactionProtocolError(msg) from error
    if integrity not in BROKEN_EXTENSION_EDID_INTEGRITIES:
        msg = "probe request requires broken extension evidence"
        raise TransactionProtocolError(msg)
    for name in ("internal_output", "preferred_mode", "probe_output"):
        value = values[name]
        if not isinstance(value, str):
            msg = f"probe request {name} must be text"
            raise TransactionProtocolError(msg)
        _validate_text(value, f"probe request {name}")
    if values["probe_output"] == values["internal_output"]:
        msg = "probe request target and internal output must differ"
        raise TransactionProtocolError(msg)


def _validate_mapping(mapping: tuple[OutputMapping, ...]) -> None:
    keys = tuple(f"{item.saved_output}\0{item.live_output}" for item in mapping)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        msg = "request output mapping must be sorted and unique"
        raise TransactionProtocolError(msg)
    if len({item.saved_output for item in mapping}) != len(mapping) or len(
        {item.live_output for item in mapping}
    ) != len(mapping):
        msg = "request output mapping must be a bijection"
        raise TransactionProtocolError(msg)


def _validate_payload(payload: Payload) -> None:
    names = tuple(name for name, _value in payload)
    if names != tuple(sorted(names)) or len(names) != len(set(names)):
        msg = "request payload fields must be sorted and unique"
        raise TransactionProtocolError(msg)
    for name, value in payload:
        _validate_text(name, "request payload field", maximum=64)
        _validate_payload_value(value, name)


def _validate_payload_value(value: object, name: str) -> None:
    if not isinstance(value, (str, int, bool, type(None))) or isinstance(value, float):
        msg = "request payload values must be scalar JSON values"
        raise TransactionProtocolError(msg)
    if isinstance(value, str):
        _validate_text(value, f"request payload {name}", maximum=1_024)


def _decode_mapping(raw: object) -> tuple[OutputMapping, ...]:
    if not isinstance(raw, list):
        msg = "output_mapping must be an array"
        raise TransactionProtocolError(msg)
    result: list[OutputMapping] = []
    items = cast("list[object]", raw)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            msg = f"output_mapping[{index}] must be an object"
            raise TransactionProtocolError(msg)
        typed = cast("dict[str, object]", item)
        _require_keys(typed, {"saved_output", "live_output"}, "output mapping")
        result.append(
            OutputMapping(
                _string(typed["saved_output"], "saved_output"),
                _string(typed["live_output"], "live_output"),
            )
        )
    return tuple(result)


def _decode_topology(raw: object) -> ExpectedTopology:
    if not isinstance(raw, dict):
        msg = "expected_topology must be an object"
        raise TransactionProtocolError(msg)
    typed = cast("dict[str, object]", raw)
    names = {
        "kernel_connected_outputs",
        "kernel_external_outputs",
        "x_connected_outputs",
        "x_active_outputs",
    }
    _require_keys(typed, names, "expected topology")
    return ExpectedTopology(
        kernel_connected_outputs=_string_array(
            typed["kernel_connected_outputs"], "kernel_connected_outputs"
        ),
        kernel_external_outputs=_string_array(
            typed["kernel_external_outputs"], "kernel_external_outputs"
        ),
        x_connected_outputs=_string_array(
            typed["x_connected_outputs"], "x_connected_outputs"
        ),
        x_active_outputs=_string_array(typed["x_active_outputs"], "x_active_outputs"),
    )


def _decode_payload(raw: object) -> Payload:
    if not isinstance(raw, dict):
        msg = "payload must be an object"
        raise TransactionProtocolError(msg)
    typed = cast("dict[str, object]", raw)
    result: list[tuple[str, PayloadValue]] = []
    for key in sorted(typed):
        value = typed[key]
        if not isinstance(value, (str, int, bool, type(None))) or isinstance(
            value, float
        ):
            msg = "payload values must be scalar JSON values"
            raise TransactionProtocolError(msg)
        result.append((key, value))
    payload = tuple(result)
    _validate_payload(payload)
    return payload


def _decode_object(payload: bytes) -> dict[str, object]:
    if not payload or len(payload) > MAX_TRANSACTION_BYTES:
        msg = "transaction JSON size is outside the accepted bounds"
        raise TransactionProtocolError(msg)
    try:
        text = payload.decode("utf-8", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        msg = "transaction is not valid UTF-8 JSON"
        raise TransactionProtocolError(msg) from error
    if not isinstance(raw, dict):
        msg = "transaction JSON root must be an object"
        raise TransactionProtocolError(msg)
    return cast("dict[str, object]", raw)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate JSON field: {key}"
            raise TransactionProtocolError(msg)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    msg = f"non-finite JSON number is forbidden: {value}"
    raise TransactionProtocolError(msg)


def _require_keys(
    raw: Mapping[str, object], expected: set[str], record_name: str
) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        msg = f"{record_name} fields differ: missing={missing}, unknown={unknown}"
        raise TransactionProtocolError(msg)


def _require_schema(raw: Mapping[str, object]) -> None:
    version = _integer(raw["schema_version"], "schema_version")
    if version != TRANSACTION_SCHEMA_VERSION:
        msg = f"unsupported transaction schema version: {version}"
        raise TransactionProtocolError(msg)


def _action_kind(value: object, name: str) -> ActionKind:
    text = _enum_text(value, name)
    try:
        return ActionKind(text)
    except ValueError as error:
        msg = f"invalid {name}: {text!r}"
        raise TransactionProtocolError(msg) from error


def _terminal_lifecycle(value: object, name: str) -> ActionLifecycle:
    text = _enum_text(value, name)
    try:
        lifecycle = ActionLifecycle(text)
    except ValueError as error:
        msg = f"invalid {name}: {text!r}"
        raise TransactionProtocolError(msg) from error
    if lifecycle not in TERMINAL_ACTION_LIFECYCLES:
        msg = f"invalid non-terminal {name}: {text!r}"
        raise TransactionProtocolError(msg)
    return lifecycle


def _enum_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a string"
        raise TransactionProtocolError(msg)
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a string"
        raise TransactionProtocolError(msg)
    _validate_text(value, name)
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{name} must be an integer"
        raise TransactionProtocolError(msg)
    return value


def _string_array(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"{name} must be an array"
        raise TransactionProtocolError(msg)
    items = cast("list[object]", value)
    result = tuple(_string(item, name) for item in items)
    _validate_sorted_unique_strings(result, name)
    return result


def _validate_sorted_unique_strings(values: tuple[str, ...], name: str) -> None:
    for value in values:
        _validate_text(value, name)
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        msg = f"{name} must be sorted and unique"
        raise TransactionProtocolError(msg)


def _validate_text(value: str, name: str, *, maximum: int = 4_096) -> None:
    if not value or value.isspace() or "\x00" in value or len(value) > maximum:
        msg = f"{name} must be non-empty, bounded text without NUL bytes"
        raise TransactionProtocolError(msg)


def _validate_unit_name(value: str) -> None:
    if not _UNIT_NAME_PATTERN.fullmatch(value) or "/" in value:
        msg = "request unit name is malformed"
        raise TransactionProtocolError(msg)


def _validate_sha256(value: str, name: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value):
        msg = f"{name} must use canonical sha256:<lowercase-hex> form"
        raise TransactionProtocolError(msg)


def _content_sha256(raw: Mapping[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_encode(raw)).hexdigest()}"


def _encode(raw: Mapping[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                raw,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
    except (TypeError, ValueError) as error:
        msg = "transaction contains a non-canonical JSON value"
        raise TransactionProtocolError(msg) from error
    if len(payload) > MAX_TRANSACTION_BYTES:
        msg = "transaction exceeds the maximum encoded size"
        raise TransactionProtocolError(msg)
    return payload


def _validate_artifact_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        value != path.as_posix()
        or path.is_absolute()
        or not path.parts
        or path.parts[0] != _ARTIFACT_ROOT
        or len(path.parts) < _MIN_ARTIFACT_PATH_PARTS
        or len(value) > MAX_TRANSACTION_ARTIFACT_PATH_CHARS
        or any(
            part in {"", ".", ".."}
            or "\x00" in part
            or len(part) > MAX_TRANSACTION_ARTIFACT_COMPONENT_CHARS
            for part in path.parts
        )
    ):
        msg = "transaction artifact path must be canonical below artifacts/"
        raise TransactionProtocolError(msg)


def _validated_artifacts(
    artifacts: tuple[TransactionArtifact, ...],
) -> tuple[TransactionArtifact, ...]:
    for artifact in artifacts:
        _validate_artifact_path(artifact.relative_path)
    ordered = tuple(sorted(artifacts, key=lambda item: item.relative_path))
    paths = tuple(item.relative_path for item in ordered)
    if len(paths) > MAX_TRANSACTION_ARTIFACTS:
        msg = "transaction artifact count exceeds its limit"
        raise TransactionProtocolError(msg)
    if len(paths) != len(set(paths)):
        msg = "transaction artifact paths must be unique"
        raise TransactionProtocolError(msg)
    total_bytes = sum(len(item.content) for item in ordered)
    if total_bytes > MAX_TRANSACTION_ARTIFACT_TOTAL_BYTES:
        msg = "transaction artifacts exceed their aggregate size limit"
        raise TransactionProtocolError(msg)
    return ordered


def _open_artifact_parent_at(
    action_directory_fd: int,
    relative_path: str,
    *,
    create: bool,
) -> tuple[int, str]:
    """Open an artifact's parent without following any path component."""
    _validate_artifact_path(relative_path)
    parts = PurePosixPath(relative_path).parts
    descriptor = os.dup(action_directory_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, _DIRECTORY_MODE, dir_fd=descriptor)
                _sync_descriptor(descriptor)
                child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                os.fchmod(child, _DIRECTORY_MODE)
            except OSError as error:
                msg = "cannot safely open transaction artifact directory"
                raise TransactionProtocolError(msg) from error
            _validate_directory_descriptor(child, "transaction artifact directory")
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _install_artifacts_at(
    action_directory_fd: int,
    artifacts: tuple[TransactionArtifact, ...],
) -> None:
    for artifact in artifacts:
        descriptor, leaf = _open_artifact_parent_at(
            action_directory_fd,
            artifact.relative_path,
            create=True,
        )
        try:
            _atomic_create_at(
                descriptor,
                leaf,
                artifact.content,
                mode=(_EXECUTABLE_FILE_MODE if artifact.executable else _FILE_MODE),
            )
        finally:
            os.close(descriptor)


def _artifact_file_paths_at(action_directory_fd: int) -> tuple[str, ...]:
    """Enumerate the exact regular-file tree while rejecting unsafe metadata."""
    try:
        root = os.open(
            _ARTIFACT_ROOT,
            _DIRECTORY_OPEN_FLAGS,
            dir_fd=action_directory_fd,
        )
    except FileNotFoundError:
        return ()
    except OSError as error:
        msg = "cannot safely open transaction artifact root"
        raise TransactionProtocolError(msg) from error
    paths: list[str] = []

    def walk(descriptor: int, prefix: tuple[str, ...]) -> None:
        _validate_directory_descriptor(descriptor, "transaction artifact directory")
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as error:
            msg = "transaction artifact directory cannot be enumerated"
            raise TransactionProtocolError(msg) from error
        for name in names:
            _validate_leaf_name(name)
            try:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                msg = "transaction artifact metadata cannot be read"
                raise TransactionProtocolError(msg) from error
            relative = (*prefix, name)
            if stat.S_ISDIR(details.st_mode):
                child = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(details.st_mode):
                paths.append("/".join(relative))
            else:
                msg = "transaction artifact tree contains a non-regular entry"
                raise TransactionProtocolError(msg)

    try:
        walk(root, (_ARTIFACT_ROOT,))
    finally:
        os.close(root)
    return tuple(paths)


def _rename_noreplace_at(directory_fd: int, source: str, target: str) -> None:
    """Atomically publish a directory without replacing any existing identity."""
    _validate_leaf_name(source)
    _validate_leaf_name(target)
    _validate_directory_descriptor(directory_fd, "transaction root")
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        msg = "renameat2(RENAME_NOREPLACE) is unavailable"
        raise TransactionProtocolError(msg)
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
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), target)


def _remove_child_directory_at(parent_directory_fd: int, name: str) -> None:
    """Recursively remove one verified private child directory without symlinks."""
    _validate_leaf_name(name)
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_directory_fd)
    except OSError as error:
        msg = "transaction child directory cannot be safely opened"
        raise TransactionProtocolError(msg) from error
    try:
        _validate_directory_descriptor(descriptor, "transaction child directory")
        try:
            names = tuple(sorted(os.listdir(descriptor)))  # noqa: PTH208
        except OSError as error:
            msg = "transaction child directory cannot be enumerated"
            raise TransactionProtocolError(msg) from error
        for child_name in names:
            _validate_leaf_name(child_name)
            try:
                details = os.stat(
                    child_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(details.st_mode):
                    _remove_child_directory_at(descriptor, child_name)
                elif stat.S_ISREG(details.st_mode):
                    os.unlink(child_name, dir_fd=descriptor)
                else:
                    msg = "transaction child directory contains an unsafe entry"
                    raise TransactionProtocolError(msg)
            except OSError as error:
                msg = "transaction child entry cannot be removed"
                raise TransactionProtocolError(msg) from error
        _sync_descriptor(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_directory_fd)
    except OSError as error:
        msg = "transaction child directory cannot be removed"
        raise TransactionProtocolError(msg) from error
    _sync_descriptor(parent_directory_fd)


def _remove_temporary_directory_at(
    root_directory_fd: int,
    temporary_directory_fd: int,
    temporary_name: str,
) -> None:
    """Remove one unpublished installation directory through retained FDs."""
    if temporary_directory_fd < 0:
        try:
            os.rmdir(temporary_name, dir_fd=root_directory_fd)
        except FileNotFoundError:
            return
        except OSError as error:
            msg = "unopened temporary transaction directory cannot be removed"
            raise TransactionProtocolError(msg) from error
        _sync_descriptor(root_directory_fd)
        return
    _validate_directory_descriptor(
        temporary_directory_fd,
        "temporary transaction action directory",
    )
    try:
        names = tuple(os.listdir(temporary_directory_fd))
    except OSError as error:
        msg = "temporary transaction directory cannot be enumerated"
        raise TransactionProtocolError(msg) from error
    for name in names:
        _validate_leaf_name(name)
        try:
            details = os.stat(
                name,
                dir_fd=temporary_directory_fd,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(details.st_mode):
                _remove_child_directory_at(temporary_directory_fd, name)
            elif stat.S_ISREG(details.st_mode):
                os.unlink(name, dir_fd=temporary_directory_fd)
            else:
                msg = "temporary transaction contains an unsafe entry"
                raise TransactionProtocolError(msg)
        except OSError as error:
            msg = "temporary transaction entry cannot be removed"
            raise TransactionProtocolError(msg) from error
    _sync_descriptor(temporary_directory_fd)
    try:
        os.rmdir(temporary_name, dir_fd=root_directory_fd)
    except OSError as error:
        msg = "temporary transaction directory cannot be removed"
        raise TransactionProtocolError(msg) from error
    _sync_descriptor(root_directory_fd)


def _atomic_create_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int = _FILE_MODE,
) -> None:
    """Install one immutable file through an already-verified directory FD."""
    _validate_leaf_name(name)
    if mode not in {_FILE_MODE, _EXECUTABLE_FILE_MODE}:
        msg = "transaction file mode is outside the closed protocol"
        raise TransactionProtocolError(msg)
    _validate_directory_descriptor(directory_fd, "transaction action directory")
    temporary_name = f".{name}.{uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
    installed = False
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                msg = "transaction temporary write made no progress"
                raise OSError(msg)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        installed = True
        _sync_descriptor(directory_fd)
        final_descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
        try:
            _validate_regular_file_details(
                os.fstat(final_descriptor),
                name,
                expected_mode=mode,
            )
        finally:
            os.close(final_descriptor)
        _validate_directory_descriptor(directory_fd, "transaction action directory")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not installed:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)


def _read_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    expected_mode: int = _FILE_MODE,
) -> bytes:
    """Read one final file through a retained parent descriptor and O_NOFOLLOW."""
    _validate_leaf_name(name)
    _validate_directory_descriptor(directory_fd, "transaction action directory")
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        msg = f"cannot safely open transaction file {name}"
        raise TransactionProtocolError(msg) from error
    try:
        before = os.fstat(descriptor)
        _validate_regular_file_details(before, name, expected_mode=expected_mode)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                msg = f"transaction file {name} was truncated while reading"
                raise TransactionProtocolError(msg)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            msg = f"transaction file {name} grew while reading"
            raise TransactionProtocolError(msg)
        after = os.fstat(descriptor)
        _validate_regular_file_details(after, name, expected_mode=expected_mode)
        if _stable_file_details(before) != _stable_file_details(after):
            msg = f"transaction file {name} metadata changed while reading"
            raise TransactionProtocolError(msg)
        _validate_directory_descriptor(
            directory_fd,
            "transaction action directory",
        )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_absolute_directory(path: Path, *, create: bool) -> int:  # noqa: C901
    """Resolve/create an absolute directory one O_NOFOLLOW component at a time."""
    parts = path.parts
    if not parts or parts[0] != "/" or any(part in {".", ".."} for part in parts[1:]):
        msg = "transaction root path is not a canonical absolute directory"
        raise TransactionProtocolError(msg)
    descriptor = os.open("/", _DIRECTORY_OPEN_FLAGS)
    try:
        for part in parts[1:]:
            try:
                child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return -1
                try:
                    os.mkdir(part, _DIRECTORY_MODE, dir_fd=descriptor)
                except FileExistsError:
                    pass
                else:
                    _sync_descriptor(descriptor)
                try:
                    child = os.open(part, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    msg = "created transaction directory cannot be safely opened"
                    raise TransactionProtocolError(msg) from error
                os.fchmod(child, _DIRECTORY_MODE)
            except OSError as error:
                msg = "transaction root component cannot be safely opened"
                raise TransactionProtocolError(msg) from error
            os.close(descriptor)
            descriptor = child
        return descriptor  # noqa: TRY300
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _validate_regular_file_details(
    details: os.stat_result,
    name: str,
    *,
    expected_mode: int = _FILE_MODE,
) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != expected_mode
        or details.st_nlink != 1
        or details.st_size <= 0
        or details.st_size > MAX_TRANSACTION_BYTES
    ):
        msg = f"transaction file {name} has unsafe metadata"
        raise TransactionProtocolError(msg)


def _stable_file_details(details: os.stat_result) -> tuple[int, ...]:
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


def _validate_directory_descriptor(descriptor: int, name: str) -> None:
    try:
        details = os.fstat(descriptor)
    except OSError as error:
        msg = f"{name} is unavailable"
        raise TransactionProtocolError(msg) from error
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != _DIRECTORY_MODE
        or details.st_nlink < _MIN_DIRECTORY_LINK_COUNT
    ):
        msg = f"{name} has unsafe metadata"
        raise TransactionProtocolError(msg)


def _validate_leaf_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        msg = "transaction file name is not a safe leaf"
        raise TransactionProtocolError(msg)


def _sync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)
