"""Typed worker-dispatch boundary and structurally inert null implementation."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from monitor_controller.model import (
    ActionId,
    ActivateProbe,
    ApplyProfile,
    FinalizeDesktop,
    PrepareDesktop,
    WorkerUnit,
)

type DispatchEffect = ActivateProbe | ApplyProfile | PrepareDesktop | FinalizeDesktop
type FinalDispatchFence = Callable[[], bool]

NULL_RECORD_RETENTION = 1_024


class DispatchFailureStage(StrEnum):
    """Adapter boundary at which an admitted operation definitely failed."""

    REQUEST_WRITE = "request_write"
    UNIT_START = "unit_start"
    PREPARED_REQUEST_CLEANUP = "prepared_request_cleanup"


class DispatchAdapterError(RuntimeError):
    """A bounded adapter operation definitely failed before worker submission."""

    def __init__(self, stage: DispatchFailureStage, detail: str) -> None:
        """Retain a typed stage and bounded diagnostic detail."""
        clean_detail = " ".join(detail.split())[:512]
        if not clean_detail:
            clean_detail = type(self).__name__
        super().__init__(f"{stage.value}: {clean_detail}")
        self.stage = stage
        self.detail = clean_detail


class DispatchStartResult(StrEnum):
    """Definite result of the adapter's generation-guarded submission point."""

    ACCEPTED = "accepted"
    FENCE_REJECTED = "fence_rejected"


class WorkerActivity(StrEnum):
    """Supervisor evidence about whether a keyed unit may still mutate."""

    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class PreparedDispatch:
    """Opaque request plus the worker identity fixed before submission."""

    action_id: ActionId
    unit: WorkerUnit
    reference: str

    def __post_init__(self) -> None:
        if self.unit.action_id != self.action_id:
            msg = "prepared request and worker unit action IDs must match"
            raise ValueError(msg)
        if not self.reference or self.reference.isspace():
            msg = "prepared dispatch reference must not be empty"
            raise ValueError(msg)


class ActionDispatcher(Protocol):
    """Authoritative worker path implemented by the later systemd integration."""

    async def write_request(self, effect: DispatchEffect) -> PreparedDispatch:
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
        """
        ...

    async def discard_prepared(self, prepared: PreparedDispatch) -> None:
        """Remove a definitely never-started generation-invalid request."""
        ...

    async def stop(self, action_id: ActionId) -> None:
        """Request keyed idempotent cancellation without claiming inactivity."""
        ...

    async def worker_activity(self, unit: WorkerUnit) -> WorkerActivity:
        """Return supervisor evidence about whether *unit* may still mutate."""
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
