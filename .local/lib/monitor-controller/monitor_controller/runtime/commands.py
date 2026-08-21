"""Bounded, non-shell execution for read-only observer commands."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..observer.evidence import (  # noqa: TID252
    RawEvidenceSource,
    TextCommandEvidence,
)

DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 5.0
MAX_COMMAND_TIMEOUT_SECONDS: float = 30.0
TIMEOUT_EXIT_STATUS: int = 124


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """One bounded argument-array command invocation."""

    arguments: tuple[str, ...]
    source: RawEvidenceSource
    reference: str
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.arguments or any(
            not item or "\x00" in item for item in self.arguments
        ):
            msg = "command arguments must be non-empty strings without NUL bytes"
            raise ValueError(msg)
        if not 0 < self.timeout_seconds <= MAX_COMMAND_TIMEOUT_SECONDS:
            msg = (
                "command timeout must be positive and no greater than "
                f"{MAX_COMMAND_TIMEOUT_SECONDS:g} seconds"
            )
            raise ValueError(msg)


class CommandRunner(Protocol):
    """Injected observer command boundary."""

    def run(self, request: CommandRequest) -> TextCommandEvidence:
        """Execute or inject the requested bounded command evidence."""
        ...


class _SubprocessRun(Protocol):
    def __call__(  # noqa: PLR0913
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        shell: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


class BoundedCommandRunner:
    """Run commands with arrays, a fixed timeout ceiling, and ``shell=False``."""

    def __init__(
        self,
        executor: _SubprocessRun = subprocess.run,
    ) -> None:
        """Inject the subprocess primitive so contract tests never execute tools."""
        self._executor = executor

    def run(self, request: CommandRequest) -> TextCommandEvidence:
        """Return bounded parser evidence for success, failure, or timeout."""
        try:
            completed = self._executor(
                request.arguments,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _timeout_stdout(error.stdout)
            return TextCommandEvidence(
                source=request.source,
                reference=request.reference,
                stdout=stdout,
                exit_status=TIMEOUT_EXIT_STATUS,
                timed_out=True,
            )
        except OSError:
            # Do not embed host-specific paths or exception text in canonical evidence.
            # The command/reference already identifies the failed adapter boundary.
            return TextCommandEvidence(
                source=request.source,
                reference=request.reference,
                stdout="",
                exit_status=127,
            )
        return TextCommandEvidence(
            source=request.source,
            reference=request.reference,
            stdout=completed.stdout,
            exit_status=_bounded_exit_status(completed.returncode),
        )


def _timeout_stdout(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _bounded_exit_status(value: int) -> int:
    # Signals are represented as negative return codes by subprocess. Parser evidence
    # deliberately has a small portable status domain, so map them to conventional
    # 128+n.
    if value < 0:
        return min(255, 128 + abs(value))
    return min(value, 255)
