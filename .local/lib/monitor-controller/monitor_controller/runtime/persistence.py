"""Persist authoritative controller state in crash-safe namespaces."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from monitor_controller.codec import State, decode_state, encode_state

_STATE_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700


class StateNamespace(StrEnum):
    """Namespaces which must never share authoritative state."""

    ACTIVE = "active"
    SHADOW = "shadow"


class PersistenceBoundary(StrEnum):
    """Observable completed boundaries used by deterministic crash tests."""

    ROOT_CREATED = "root_created"
    STATE_HOME_SYNCED = "state_home_synced"
    NAMESPACE_CREATED = "namespace_created"
    ROOT_SYNCED = "root_synced"
    TEMP_CREATED = "temp_created"
    DATA_WRITTEN = "data_written"
    FILE_FLUSHED = "file_flushed"
    FILE_SYNCED = "file_synced"
    RENAMED = "renamed"
    DIRECTORY_SYNCED = "directory_synced"


type CrashHook = Callable[[PersistenceBoundary], None]


class AtomicStateStore:
    """Store one namespace using durable sibling-file replacement."""

    def __init__(
        self,
        state_home: Path,
        namespace: StateNamespace,
        *,
        crash_hook: CrashHook | None = None,
    ) -> None:
        """Select one namespace below an injected XDG state home."""
        self._root = state_home / "monitor-controller"
        self._namespace = namespace
        self._directory = self._root / namespace.value
        self._path = self._directory / "state.json"
        self._crash_hook = crash_hook

    @property
    def namespace(self) -> StateNamespace:
        """Return the isolated authority namespace."""
        return self._namespace

    @property
    def path(self) -> Path:
        """Return the authoritative state path."""
        return self._path

    def load(self) -> State:
        """Read and strictly decode the complete current record."""
        return decode_state(self._path.read_bytes())

    def sweep_stale_temporaries(self) -> tuple[Path, ...]:
        """Remove orphaned save temporaries left by a hard kill mid-replace.

        A process dying between ``mkstemp`` and ``replace`` leaks its
        temporary; the rename never happened, so removal cannot lose state.
        Any concurrent writer is excluded by the namespace authority lock.
        """
        if not self._directory.is_dir():
            return ()
        removed: list[Path] = []
        for candidate in self._directory.glob(".state.json.*.tmp"):
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)
                removed.append(candidate)
        return tuple(removed)

    def save(self, state: State) -> None:
        """Durably replace state and return only after directory persistence."""
        payload = encode_state(state)
        self._ensure_directories()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".state.json.", suffix=".tmp", dir=self._directory
        )
        temporary_path = Path(temporary_name)
        renamed = False
        try:
            os.fchmod(descriptor, _STATE_FILE_MODE)
            self._boundary(PersistenceBoundary.TEMP_CREATED)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                self._boundary(PersistenceBoundary.DATA_WRITTEN)
                stream.flush()
                self._boundary(PersistenceBoundary.FILE_FLUSHED)
                os.fsync(stream.fileno())
                self._boundary(PersistenceBoundary.FILE_SYNCED)
            temporary_path.replace(self._path)
            renamed = True
            self._boundary(PersistenceBoundary.RENAMED)
            self._sync_directory(self._directory)
            self._boundary(PersistenceBoundary.DIRECTORY_SYNCED)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not renamed:
                temporary_path.unlink(missing_ok=True)

    def _ensure_directories(self) -> None:
        if not self._root.exists():
            self._root.mkdir(mode=_DIRECTORY_MODE)
            self._root.chmod(_DIRECTORY_MODE)
            self._boundary(PersistenceBoundary.ROOT_CREATED)
            self._sync_directory(self._root.parent)
            self._boundary(PersistenceBoundary.STATE_HOME_SYNCED)
        if not self._directory.exists():
            self._directory.mkdir(mode=_DIRECTORY_MODE)
            self._directory.chmod(_DIRECTORY_MODE)
            self._boundary(PersistenceBoundary.NAMESPACE_CREATED)
            self._sync_directory(self._directory.parent)
            self._boundary(PersistenceBoundary.ROOT_SYNCED)
        self._directory.chmod(_DIRECTORY_MODE)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _boundary(self, boundary: PersistenceBoundary) -> None:
        if self._crash_hook is not None:
            self._crash_hook(boundary)
