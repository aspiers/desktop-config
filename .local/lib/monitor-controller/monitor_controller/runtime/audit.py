"""Bounded rotating JSONL audit streams which remain reducer-replayable."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from monitor_controller.codec import encode_schema_value, encode_state
from monitor_controller.simulation.replay import (
    MAX_REPLAY_BYTES,
    REPLAY_SCHEMA_VERSION,
    decode_replay,
    replay,
)

if TYPE_CHECKING:
    from monitor_controller.model import Decision, Effect, Event, State
    from monitor_controller.runtime.dispatcher import WouldDispatch

DEFAULT_MAX_AUDIT_BYTES = 4 * 1_048_576
# Decision records embed full state (~45KB), so retention is time-bounded:
# eight 4MiB segments hold roughly six hours of 60s health ticks, enough for
# the shadow-trace snapshotter to capture a scenario before rotation (dc-czj).
DEFAULT_AUDIT_FILES = 8
_AUDIT_FILE_MODE = 0o600
_AUDIT_DIRECTORY_MODE = 0o700


class AuditWriteError(RuntimeError):
    """Raised when a complete bounded audit record cannot be retained."""


@dataclass(frozen=True, slots=True)
class DecisionAuditTiming:
    """Monotonic boundaries around reduction and authoritative persistence."""

    processing_started_ms: int
    reduction_finished_ms: int
    persistence_finished_ms: int
    observation_duration_ms: int | None = None
    command_duration_ms: int | None = None
    worker_duration_ms: int | None = None

    def __post_init__(self) -> None:
        if not (
            0
            <= self.processing_started_ms
            <= self.reduction_finished_ms
            <= self.persistence_finished_ms
        ):
            msg = "audit timing boundaries must be non-negative and ordered"
            raise ValueError(msg)
        if any(
            value is not None and value < 0
            for value in (
                self.observation_duration_ms,
                self.command_duration_ms,
                self.worker_duration_ms,
            )
        ):
            msg = "audit adapter durations must be non-negative"
            raise ValueError(msg)


class RotatingAuditLog:
    """Append complete JSONL records while bounding size and retained file count."""

    def __init__(
        self,
        path: Path,
        initial_state: State,
        *,
        max_bytes: int = DEFAULT_MAX_AUDIT_BYTES,
        max_files: int = DEFAULT_AUDIT_FILES,
    ) -> None:
        """Start a new independently replayable segment for this runtime."""
        if isinstance(max_bytes, bool):
            msg = "audit max_bytes must be an integer"
            raise TypeError(msg)
        if not 0 < max_bytes <= MAX_REPLAY_BYTES:
            msg = f"audit max_bytes must be between 1 and {MAX_REPLAY_BYTES}"
            raise ValueError(msg)
        if isinstance(max_files, bool):
            msg = "audit max_files must be an integer"
            raise TypeError(msg)
        if max_files <= 0:
            msg = "audit max_files must be positive"
            raise ValueError(msg)
        self._path = path
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._current_state = initial_state
        self._ensure_directory()
        self._clean_existing_segments()
        if self._path.exists():
            self._rotate_files()
        self._write_new_segment(initial_state)

    @property
    def path(self) -> Path:
        """Return the active audit segment path."""
        return self._path

    @property
    def retained_paths(self) -> tuple[Path, ...]:
        """Return active and existing rotated segments in newest-first order."""
        paths = [self._path]
        paths.extend(
            candidate
            for index in range(1, self._max_files)
            if (candidate := self._rotated(index)).exists()
        )
        return tuple(path for path in paths if path.exists())

    def append_decision(
        self,
        prior_state: State,
        event: Event,
        decision: Decision,
        timing: DecisionAuditTiming,
    ) -> None:
        """Append one complete reducer input, result, effects, and timing record."""
        if prior_state != self._current_state:
            msg = "audit prior state does not continue the active replay segment"
            raise AuditWriteError(msg)
        record = {
            "record": "decision",
            "event_type": type(event).__name__,
            "event": encode_schema_value(event),
            "state": encode_schema_value(decision.state),
            "effects": [
                {
                    "effect_type": type(effect).__name__,
                    "effect": encode_schema_value(effect),
                }
                for effect in decision.effects
            ],
            "audit": {
                "monotonic_ms": event.metadata.processed_at_ms,
                "wake_reason": _wake_reason(event, decision.effects),
                "prior_state_key": _state_key(prior_state),
                "resulting_state_key": _state_key(decision.state),
                "timing": {
                    "processing_started_ms": timing.processing_started_ms,
                    "reduction_finished_ms": timing.reduction_finished_ms,
                    "persistence_finished_ms": timing.persistence_finished_ms,
                    "observation_duration_ms": timing.observation_duration_ms,
                    "command_duration_ms": timing.command_duration_ms,
                    "worker_duration_ms": timing.worker_duration_ms,
                },
            },
        }
        self._append_record(record, segment_state=prior_state)
        self._current_state = decision.state

    def append_would_dispatch(self, record: WouldDispatch) -> None:
        """Append a shadow-only annotation ignored by deterministic replay."""
        self._append_record(
            {
                "record": "would_dispatch",
                "schema_version": REPLAY_SCHEMA_VERSION,
                "kind": record.kind.value,
                "action_id": record.action_id.value,
                "effect_type": type(record.effect).__name__,
                "effect": encode_schema_value(record.effect),
                "recorded_at_ms": record.recorded_at_ms,
            },
            segment_state=self._current_state,
        )

    def append_runtime_failure(
        self,
        *,
        boundary: str,
        detail: str,
        recorded_at_ms: int,
        action_id: str | None = None,
    ) -> None:
        """Append a bounded adapter diagnostic without changing reducer history."""
        self._append_record(
            {
                "record": "runtime_failure",
                "schema_version": REPLAY_SCHEMA_VERSION,
                "boundary": _bounded_text(boundary),
                "detail": _bounded_text(detail),
                "action_id": action_id,
                "recorded_at_ms": recorded_at_ms,
            },
            segment_state=self._current_state,
        )

    def _append_record(self, record: object, *, segment_state: State) -> None:
        encoded = _encode_record(record)
        if len(encoded) > self._max_bytes:
            msg = f"one audit record exceeds the {self._max_bytes}-byte segment limit"
            raise AuditWriteError(msg)
        current_size = self._path.stat().st_size
        if current_size + len(encoded) > self._max_bytes:
            self._rotate_files()
            self._write_new_segment(segment_state)
            current_size = self._path.stat().st_size
        if current_size + len(encoded) > self._max_bytes:
            msg = "audit header and record cannot fit in one bounded segment"
            raise AuditWriteError(msg)
        self._append_bytes(encoded)

    def _write_new_segment(self, state: State) -> None:
        header = _encode_record(
            {
                "record": "header",
                "schema_version": REPLAY_SCHEMA_VERSION,
                "initial_state": encode_schema_value(state),
            }
        )
        if len(header) > self._max_bytes:
            msg = "audit replay header exceeds the segment size limit"
            raise AuditWriteError(msg)
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _AUDIT_FILE_MODE,
        )
        try:
            _write_all(descriptor, header)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_bytes(self, data: bytes) -> None:
        descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND)
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ensure_directory(self) -> None:
        self._path.parent.mkdir(
            mode=_AUDIT_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        self._path.parent.chmod(_AUDIT_DIRECTORY_MODE)

    def _clean_existing_segments(self) -> None:
        """Retain only canonical, bounded segments which replay completely."""
        candidates = (self._path, *self._path.parent.glob(f"{self._path.name}.*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            retain = candidate == self._path
            if candidate != self._path:
                suffix = candidate.name.removeprefix(f"{self._path.name}.")
                if suffix.isdecimal():
                    try:
                        index = int(suffix)
                    except ValueError:
                        retain = False
                    else:
                        retain = suffix == str(index) and 1 <= index < self._max_files
            if retain:
                retain = self._segment_replays(candidate)
            if not retain:
                candidate.unlink()

    def _segment_replays(self, candidate: Path) -> bool:
        try:
            if not 0 < candidate.stat().st_size <= self._max_bytes:
                return False
            replay(decode_replay(candidate.read_bytes()))
        except (OSError, ValueError, AssertionError):
            return False
        return True

    def _rotate_files(self) -> None:
        if self._max_files == 1:
            self._path.unlink(missing_ok=True)
            return
        oldest = self._rotated(self._max_files - 1)
        oldest.unlink(missing_ok=True)
        for index in range(self._max_files - 2, 0, -1):
            source = self._rotated(index)
            if source.exists():
                source.replace(self._rotated(index + 1))
        if self._path.exists():
            self._path.replace(self._rotated(1))

    def _rotated(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            msg = "audit write made no progress"
            raise AuditWriteError(msg)
        view = view[written:]


def _state_key(state: State) -> str:
    return sha256(encode_state(state)).hexdigest()


def _wake_reason(event: Event, effects: tuple[Effect, ...]) -> str:
    request = next(
        (effect for effect in effects if type(effect).__name__ == "RequestObservation"),
        None,
    )
    reason = getattr(request, "reason", None)
    value = getattr(reason, "value", None)
    return value if isinstance(value, str) else type(event).__name__


def _bounded_text(value: str) -> str:
    clean = " ".join(value.split())[:512]
    return clean or "unspecified"


def _encode_record(record: object) -> bytes:
    return (
        json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
