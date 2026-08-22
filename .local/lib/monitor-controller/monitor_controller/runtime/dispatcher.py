"""Typed worker-dispatch boundary and structurally inert null implementation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from monitor_controller.model import (
    BROKEN_EXTENSION_EDID_INTEGRITIES,
    TERMINAL_ACTION_LIFECYCLES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActivateProbe,
    ApplyProfile,
    ConfigurationContentHash,
    EdidIntegrity,
    FinalizeDesktop,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    PrepareDesktop,
    WorkerUnit,
)

if TYPE_CHECKING:
    from monitor_controller.runtime.transactions import ExpectedTopology

type DispatchEffect = ActivateProbe | ApplyProfile | PrepareDesktop | FinalizeDesktop
type FinalDispatchFence = Callable[[], bool]

NULL_RECORD_RETENTION = 1_024
MAX_EXIT_STATUS = 255
SHA256_HEX_LENGTH = 64


class DispatchFailureStage(StrEnum):
    """Adapter boundary at which an admitted operation definitely failed."""

    REQUEST_WRITE = "request_write"
    UNIT_START = "unit_start"
    PREPARED_REQUEST_CLEANUP = "prepared_request_cleanup"


class DispatchAdapterError(RuntimeError):
    """A bounded adapter operation with a definite non-running outcome."""

    def __init__(
        self,
        stage: DispatchFailureStage,
        detail: str,
        *,
        completion: WorkerCompletion | None = None,
    ) -> None:
        """Retain a typed stage, diagnostic detail, and optional exact result."""
        clean_detail = " ".join(detail.split())[:512]
        if not clean_detail:
            clean_detail = type(self).__name__
        super().__init__(f"{stage.value}: {clean_detail}")
        self.stage = stage
        self.detail = clean_detail
        self.completion = completion


class DispatchStartResult(StrEnum):
    """Definite result of the adapter's generation-guarded submission point."""

    ACCEPTED = "accepted"
    FENCE_REJECTED = "fence_rejected"


class WorkerActivity(StrEnum):
    """Supervisor evidence about whether a keyed unit may still mutate."""

    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class WorkerRequestContext:
    """Persisted observation proof needed to materialize a worker request."""

    physical_epoch: int
    physical_token: PhysicalToken
    output_mapping: tuple[OutputMapping, ...]
    expected_topology: ExpectedTopology
    layout: str | None = None
    planning_action_id: ActionId | None = None
    probe_base_hash: str | None = None
    probe_edid_integrity: EdidIntegrity | None = None
    profile_configuration_hashes: tuple[ConfigurationContentHash, ...] = ()

    def __post_init__(self) -> None:
        if self.physical_epoch < 0:
            msg = "worker request physical epoch must be non-negative"
            raise ValueError(msg)
        if self.layout is not None and (not self.layout or self.layout.isspace()):
            msg = "worker request layout must not be empty"
            raise ValueError(msg)
        if (
            self.planning_action_id is not None
            and self.planning_action_id.kind is not ActionKind.PLAN
        ):
            msg = "worker request planning identity has the wrong action kind"
            raise ValueError(msg)
        if (self.probe_base_hash is None) is not (self.probe_edid_integrity is None):
            msg = "worker probe identity hash and integrity must be supplied together"
            raise ValueError(msg)
        if (
            self.probe_edid_integrity is not None
            and self.probe_edid_integrity not in BROKEN_EXTENSION_EDID_INTEGRITIES
        ):
            msg = "worker probe proof requires broken extensions"
            raise ValueError(msg)
        hash_keys = tuple(
            f"{item.path}\0{item.sha256}" for item in self.profile_configuration_hashes
        )
        if hash_keys != tuple(sorted(hash_keys)) or len(hash_keys) != len(
            set(hash_keys)
        ):
            msg = "worker profile configuration hashes must be sorted and unique"
            raise ValueError(msg)
        if self.probe_base_hash is not None and (
            len(self.probe_base_hash) != SHA256_HEX_LENGTH
            or any(
                character not in "0123456789abcdef"
                for character in self.probe_base_hash
            )
        ):
            msg = "worker probe base identity must be a lowercase SHA-256 digest"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class WorkerCompletion:
    """Validated terminal output bound to an exact non-active worker identity."""

    action_id: ActionId
    terminal_lifecycle: ActionLifecycle
    exit_status: int
    plan_hash: PlanHash | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.exit_status <= MAX_EXIT_STATUS:
            msg = "worker completion exit status must be between zero and 255"
            raise ValueError(msg)
        if self.terminal_lifecycle not in TERMINAL_ACTION_LIFECYCLES:
            msg = "worker completion requires an exact terminal lifecycle"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is ActionLifecycle.COMPLETED
            and self.exit_status != 0
        ):
            msg = "completed worker completion requires exit status zero"
            raise ValueError(msg)
        if (
            self.terminal_lifecycle is not ActionLifecycle.COMPLETED
            and self.exit_status == 0
        ):
            msg = "non-completed worker completion requires a non-zero exit status"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    """Opaque request plus the worker identity fixed before submission."""

    action_id: ActionId
    unit: WorkerUnit
    reference: str
    request_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.unit.action_id != self.action_id:
            msg = "prepared request and worker unit action IDs must match"
            raise ValueError(msg)
        if not self.reference or self.reference.isspace():
            msg = "prepared dispatch reference must not be empty"
            raise ValueError(msg)
        if self.request_sha256 is not None and not self.request_sha256.startswith(
            "sha256:"
        ):
            msg = "prepared dispatch request hash must use sha256 form"
            raise ValueError(msg)


class ActionDispatcher(Protocol):
    """Authoritative worker path implemented by the later systemd integration."""

    async def write_request(
        self,
        effect: DispatchEffect,
        context: WorkerRequestContext,
    ) -> PreparedDispatch:
        """Durably write one immutable request without starting its worker."""
        ...

    async def start(
        self,
        prepared: PreparedDispatch,
        final_fence: FinalDispatchFence,
    ) -> DispatchStartResult:
        """Submit only if a final non-yielding generation fence still passes.

        The adapter may await before invoking *final_fence*. Once it invokes the
        callback, it must not await or otherwise yield between a true result and
        the actual supervisor submission. ``FENCE_REJECTED`` guarantees that no
        submission occurred. The worker identity is fixed by *prepared*, so an
        acknowledgement cannot substitute a different unit.

        ``DispatchAdapterError`` without a completion guarantees no submission.
        With a completion it proves an immutable exact terminal result and that the
        bound worker identity is not active; the caller must persist both identity
        and completion rather than treating it as a pre-submission rejection.
        """
        ...

    async def discard_prepared(self, prepared: PreparedDispatch) -> None:
        """Remove a definitely never-started generation-invalid request."""
        ...

    async def stop(
        self,
        action_id: ActionId,
        terminal_lifecycle: ActionLifecycle,
    ) -> None:
        """Persist exact stop intent, then request keyed manager cancellation."""
        ...

    async def worker_activity(self, unit: WorkerUnit) -> WorkerActivity:
        """Return supervisor evidence about whether *unit* may still mutate."""
        ...

    async def worker_completion(self, unit: WorkerUnit) -> WorkerCompletion | None:
        """Return exact terminal output, or ``None`` when no safe result exists."""
        ...


class WouldDispatchKind(StrEnum):
    """The only worker intents which shadow mode can record."""

    WOULD_PROBE = "WOULD_PROBE"
    WOULD_APPLY = "WOULD_APPLY"
    WOULD_PREPARE = "WOULD_PREPARE"
    WOULD_FINALIZE = "WOULD_FINALIZE"


@dataclass(frozen=True, slots=True)
class WouldDispatch:
    """Audit-only representation of one admitted worker intent."""

    kind: WouldDispatchKind
    action_id: ActionId
    effect: DispatchEffect
    recorded_at_ms: int

    def __post_init__(self) -> None:
        if self.recorded_at_ms < 0:
            msg = "would-dispatch time must be non-negative"
            raise ValueError(msg)
        if self.effect.action_id != self.action_id:
            msg = "would-dispatch action and effect IDs must match"
            raise ValueError(msg)


class NullDispatcher:
    """Audit-only dispatcher with no request, supervisor, or unit-start surface."""

    def __init__(self) -> None:
        """Create an in-memory record list; accept no side-effect adapter."""
        self._records: deque[WouldDispatch] = deque(maxlen=NULL_RECORD_RETENTION)

    @property
    def records(self) -> tuple[WouldDispatch, ...]:
        """Return immutable audit-only records in admission order."""
        return tuple(self._records)

    def record(self, effect: DispatchEffect, recorded_at_ms: int) -> WouldDispatch:
        """Record an admitted intent without constructing a transaction or unit."""
        if isinstance(effect, ActivateProbe):
            kind = WouldDispatchKind.WOULD_PROBE
        elif isinstance(effect, ApplyProfile):
            kind = WouldDispatchKind.WOULD_APPLY
        elif isinstance(effect, PrepareDesktop):
            kind = WouldDispatchKind.WOULD_PREPARE
        else:
            kind = WouldDispatchKind.WOULD_FINALIZE
        record = WouldDispatch(kind, effect.action_id, effect, recorded_at_ms)
        self._records.append(record)
        return record
