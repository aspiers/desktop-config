# ruff: noqa: EM101, EM102, TRY003
"""Startup scaffolding shared by the shadow and active composition roots.

The two roots are deliberately separate programs — supplying a real
dispatcher is the entire difference between observing and acting, and the
spec forbids reducing that to a boolean. What they share here is the
mode-independent plumbing: XDG resolution, the authority-lock protocol, the
theme and configuration-root readers, and the two-task run loop. These were
previously near-verbatim copies whose only differences were the word
shadow/active and the exception class, and nothing pinned them together, so
they drifted (`dc-t53`, `dc-gab`). Each function takes the caller's
exception class, matching the package's injected-error convention.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from monitor_controller.safeio import read_bounded_text

_DIRECTORY_MODE = 0o700
_LOCK_MODE = 0o600


@dataclass(frozen=True, slots=True)
class ResolvedXdgPaths:
    """The five strict XDG roots both composition roots resolve identically."""

    data_home: Path
    state_home: Path
    runtime_dir: Path
    config_home: Path
    desktop_configuration_root: Path


def resolve_xdg_paths(
    environ: Mapping[str, str] | None,
    *,
    namespace: str,
    error: type[Exception],
) -> ResolvedXdgPaths:
    """Resolve strict XDG paths without consulting service-manager helpers."""
    values = os.environ if environ is None else environ
    home_value = values.get("HOME")
    if not home_value:
        raise error("HOME is required for default XDG paths")
    home = Path(home_value)
    if not home.is_absolute():
        raise error(f"HOME must be absolute: {home}")
    runtime_value = values.get("XDG_RUNTIME_DIR")
    if not runtime_value:
        raise error(f"XDG_RUNTIME_DIR is required for {namespace} authority isolation")
    return ResolvedXdgPaths(
        data_home=Path(values.get("XDG_DATA_HOME", home / ".local" / "share")),
        state_home=Path(values.get("XDG_STATE_HOME", home / ".local" / "state")),
        runtime_dir=Path(runtime_value),
        config_home=Path(values.get("XDG_CONFIG_HOME", home / ".config")),
        desktop_configuration_root=Path(
            values.get(
                "MONITOR_CONTROLLER_DESKTOP_CONFIG_ROOT",
                home / ".STOW" / "desktop-config",
            )
        ),
    )


def validate_absolute_paths(
    pairs: tuple[tuple[str, Path], ...],
    *,
    namespace: str,
    error: type[Exception],
) -> None:
    """Refuse any relative deployment path before it can be dereferenced."""
    for name, path in pairs:
        if not path.is_absolute():
            raise error(f"{namespace} {name} must be absolute: {path}")


def require_namespace(
    environ: Mapping[str, str],
    namespace: str,
    error: type[Exception],
) -> None:
    """Refuse to run under a unit file that launched the wrong composition root."""
    if environ.get("MONITOR_CONTROLLER_NAMESPACE") != namespace:
        raise error(f"MONITOR_CONTROLLER_NAMESPACE must be exactly {namespace!r}")


def desktop_theme(config_home: Path, error: type[Exception]) -> str:
    """Read the desktop theme with the guarded reader, defaulting to dark."""
    path = config_home / "theme"
    if not path.exists():
        return "dark"
    value = read_bounded_text(path, "desktop:theme", error).strip()
    if value not in {"dark", "light"}:
        raise error("desktop theme must be exactly dark or light")
    return value


def desktop_configuration_root(path: Path, error: type[Exception]) -> Path:
    """Resolve the desktop configuration tree, refusing anything unusable."""
    try:
        root = path.resolve(strict=True)
    except OSError as resolve_error:
        raise error("desktop configuration root cannot be resolved") from resolve_error
    if not root.is_dir():
        raise error("desktop configuration root is not a directory")
    return root


class NamespaceAuthorityLock:
    """Hold one namespace's authority lock for the complete process lifetime."""

    def __init__(self, path: Path, *, namespace: str, error: type[Exception]) -> None:
        """Bind one lock path without touching the filesystem."""
        self._path = path
        self._namespace = namespace
        self._error = error
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        """Acquire exclusively and fail instead of starting a second authority."""
        self._path.parent.mkdir(
            mode=_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        self._path.parent.chmod(_DIRECTORY_MODE)
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT,
            _LOCK_MODE,
        )
        os.fchmod(descriptor, _LOCK_MODE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as lock_error:
            os.close(descriptor)
            raise self._error(
                f"{self._namespace} authority lock is already held: {self._path}"
            ) from lock_error
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release by closing the descriptor, including exceptional exits."""
        del exception_type, exception, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)


class _RunnableController(Protocol):
    def run(self) -> Coroutine[object, object, object]: ...

    async def close(self) -> None: ...

    def notify_drm_hint(self, *, observed_at_ms: int | None = None) -> object: ...


class _HintProducer(Protocol):
    def run(
        self, notify: Callable[..., object]
    ) -> Coroutine[object, object, object]: ...


async def run_controller_with_producer(
    controller: _RunnableController,
    producer: _HintProducer,
    *,
    task_prefix: str,
    error: type[Exception],
    close: Callable[[], None] | None = None,
) -> None:
    """Run one controller and one DRM producer, cancelling both on any exit.

    *close* runs after the controller has shut down, for whatever synchronous
    resources the composition retains (planner, transaction store, fence).
    """
    controller_task = asyncio.create_task(
        controller.run(),
        name=f"{task_prefix}-consumer",
    )
    monitor_task = asyncio.create_task(
        producer.run(controller.notify_drm_hint),
        name=f"{task_prefix}-uevents",
    )
    tasks = (controller_task, monitor_task)
    try:
        done, _pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed = next(iter(done))
        failure = completed.exception()
        if failure is not None:
            raise failure
        raise error(f"runtime task exited unexpectedly: {completed.get_name()}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await controller.close()
        if close is not None:
            close()
