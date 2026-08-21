"""Tests for atomic, namespaced state persistence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.codec import StateCodecError, encode_state
from monitor_controller.model import (
    BootId,
    ControllerInstanceId,
    DisplayIdentity,
    State,
)
from monitor_controller.runtime.persistence import (
    AtomicStateStore,
    PersistenceBoundary,
    StateNamespace,
)

_BOOT = BootId(UUID("11111111-1111-1111-1111-111111111111"))
_INSTANCE = ControllerInstanceId(UUID("22222222-2222-2222-2222-222222222222"))
_DIRECTORY_BOUNDARIES: tuple[PersistenceBoundary, ...] = (
    PersistenceBoundary.ROOT_CREATED,
    PersistenceBoundary.STATE_HOME_SYNCED,
    PersistenceBoundary.NAMESPACE_CREATED,
    PersistenceBoundary.ROOT_SYNCED,
)
_REPLACEMENT_BOUNDARIES: tuple[PersistenceBoundary, ...] = (
    PersistenceBoundary.TEMP_CREATED,
    PersistenceBoundary.DATA_WRITTEN,
    PersistenceBoundary.FILE_FLUSHED,
    PersistenceBoundary.FILE_SYNCED,
    PersistenceBoundary.RENAMED,
    PersistenceBoundary.DIRECTORY_SYNCED,
)


def _state(epoch: int = 0) -> State:
    return State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":0"),
        physical_epoch=epoch,
    )


def test_active_and_shadow_state_are_physically_namespaced(tmp_path: Path) -> None:
    active = AtomicStateStore(tmp_path, StateNamespace.ACTIVE)
    shadow = AtomicStateStore(tmp_path, StateNamespace.SHADOW)

    active.save(_state(1))
    shadow.save(_state(2))

    assert active.path == tmp_path / "monitor-controller/active/state.json"
    assert shadow.path == tmp_path / "monitor-controller/shadow/state.json"
    assert active.load().physical_epoch == 1
    assert shadow.load().physical_epoch == 2
    assert active.path.read_bytes() != shadow.path.read_bytes()


def test_successful_write_completes_flush_fsync_rename_and_directory_fsync(
    tmp_path: Path,
) -> None:
    boundaries: list[PersistenceBoundary] = []
    store = AtomicStateStore(
        tmp_path,
        StateNamespace.ACTIVE,
        crash_hook=boundaries.append,
    )

    store.save(_state(4))

    assert boundaries == list(PersistenceBoundary)
    assert boundaries.index(PersistenceBoundary.ROOT_CREATED) < boundaries.index(
        PersistenceBoundary.STATE_HOME_SYNCED
    )
    assert boundaries.index(PersistenceBoundary.NAMESPACE_CREATED) < boundaries.index(
        PersistenceBoundary.ROOT_SYNCED
    )
    assert boundaries.index(PersistenceBoundary.ROOT_SYNCED) < boundaries.index(
        PersistenceBoundary.TEMP_CREATED
    )
    assert boundaries.index(PersistenceBoundary.FILE_FLUSHED) < boundaries.index(
        PersistenceBoundary.FILE_SYNCED
    )
    assert boundaries.index(PersistenceBoundary.FILE_SYNCED) < boundaries.index(
        PersistenceBoundary.RENAMED
    )
    assert boundaries.index(PersistenceBoundary.RENAMED) < boundaries.index(
        PersistenceBoundary.DIRECTORY_SYNCED
    )
    assert store.load() == _state(4)
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_existing_state_is_atomically_replaced(tmp_path: Path) -> None:
    store = AtomicStateStore(tmp_path, StateNamespace.ACTIVE)
    store.save(_state(1))
    first_inode = store.path.stat().st_ino

    store.save(_state(2))

    assert store.load() == _state(2)
    assert store.path.stat().st_ino != first_inode
    assert not list(store.path.parent.glob(".state.json.*.tmp"))


@pytest.mark.parametrize("crash_at", _REPLACEMENT_BOUNDARIES)
def test_crash_injection_covers_every_replacement_boundary(
    tmp_path: Path,
    crash_at: PersistenceBoundary,
) -> None:
    stable = AtomicStateStore(tmp_path, StateNamespace.ACTIVE)
    old_state = _state(1)
    new_state = _state(2)
    stable.save(old_state)

    class InjectedCrashError(RuntimeError):
        pass

    def crash(boundary: PersistenceBoundary) -> None:
        if boundary is crash_at:
            raise InjectedCrashError(boundary.value)

    crashing = AtomicStateStore(
        tmp_path,
        StateNamespace.ACTIVE,
        crash_hook=crash,
    )
    with pytest.raises(InjectedCrashError, match=crash_at.value):
        crashing.save(new_state)

    expected = (
        new_state
        if crash_at
        in {PersistenceBoundary.RENAMED, PersistenceBoundary.DIRECTORY_SYNCED}
        else old_state
    )
    assert stable.load() == expected
    assert not list(stable.path.parent.glob(".state.json.*.tmp"))


@pytest.mark.parametrize("crash_at", _DIRECTORY_BOUNDARIES)
def test_first_write_exposes_each_durable_directory_boundary(
    tmp_path: Path,
    crash_at: PersistenceBoundary,
) -> None:
    class InjectedCrashError(RuntimeError):
        pass

    def crash(boundary: PersistenceBoundary) -> None:
        if boundary is crash_at:
            raise InjectedCrashError(boundary.value)

    store = AtomicStateStore(
        tmp_path,
        StateNamespace.ACTIVE,
        crash_hook=crash,
    )

    with pytest.raises(InjectedCrashError, match=crash_at.value):
        store.save(_state())

    assert not store.path.exists()


def test_invalid_state_is_rejected_before_touching_existing_record(
    tmp_path: Path,
) -> None:
    store = AtomicStateStore(tmp_path, StateNamespace.ACTIVE)
    old_state = _state(1)
    store.save(old_state)
    invalid = replace(_state(2), phase="probe_pending")  # type: ignore[arg-type]

    with pytest.raises(StateCodecError, match="relationships are invalid"):
        store.save(invalid)

    assert store.path.read_bytes() == encode_state(old_state)
