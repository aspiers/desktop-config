"""Bounded injected text evidence shared by command parsers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from monitor_controller.model import RawEvidenceReference, RawEvidenceSource

MAX_COMMAND_BYTES: int = 1 << 20
MAX_PARSE_ISSUES: int = 16
MAX_ISSUE_DETAIL_CHARS: int = 160
MAX_REFERENCE_CHARS: int = 512
MAX_EXIT_STATUS: int = 255


class ParseIssueCode(StrEnum):
    """Closed set of bounded command/profile parsing failures."""

    TOO_LARGE = "too_large"
    COMMAND_FAILED = "command_failed"
    COMMAND_TIMED_OUT = "command_timed_out"
    MALFORMED_LINE = "malformed_line"
    DUPLICATE = "duplicate"
    MISSING_REQUIRED = "missing_required"
    INCONSISTENT = "inconsistent"
    UNSUPPORTED_WILDCARD = "unsupported_wildcard"
    TOPOLOGY_MISMATCH = "topology_mismatch"
    UNMATCHED = "unmatched"
    COLLISION = "collision"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """One bounded error which never embeds unrestricted raw command output."""

    source: RawEvidenceSource
    code: ParseIssueCode
    detail: str
    line: int | None = None

    def __post_init__(self) -> None:
        if not self.detail:
            msg = "parse issue detail must not be empty"
            raise ValueError(msg)
        if len(self.detail) > MAX_ISSUE_DETAIL_CHARS:
            msg = "parse issue detail exceeds its bound"
            raise ValueError(msg)
        if self.line is not None and self.line <= 0:
            msg = "parse issue line must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TextCommandEvidence:
    """Injected, non-executing result of one documented command interface."""

    source: RawEvidenceSource
    reference: str
    stdout: str
    exit_status: int = 0
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not self.reference or len(self.reference) > MAX_REFERENCE_CHARS:
            msg = "command evidence reference must be bounded and non-empty"
            raise ValueError(msg)
        if self.exit_status < 0 or self.exit_status > MAX_EXIT_STATUS:
            msg = "command exit status must be between 0 and 255"
            raise ValueError(msg)
        if self.timed_out and self.exit_status == 0:
            msg = "timed-out command evidence cannot report success"
            raise ValueError(msg)

    @property
    def raw_reference(self) -> RawEvidenceReference:
        """Return the model-level reference and digest without retaining raw text."""
        return RawEvidenceReference(
            source=self.source,
            reference=self.reference,
            sha256=sha256(self.stdout.encode("utf-8")).hexdigest(),
        )


class IssueCollector:
    """Collect only a fixed number of sanitized parser issues."""

    def __init__(self, source: RawEvidenceSource) -> None:
        """Bind every collected issue to one raw evidence source."""
        self._source = source
        self._issues: list[ParseIssue] = []

    @property
    def issues(self) -> tuple[ParseIssue, ...]:
        """Return immutable issues in discovery order."""
        return tuple(self._issues)

    def add(
        self,
        code: ParseIssueCode,
        detail: str,
        line: int | None = None,
    ) -> None:
        """Append a sanitized issue unless the fixed issue budget is exhausted."""
        if len(self._issues) >= MAX_PARSE_ISSUES:
            return
        sanitized = " ".join(detail.split())[:MAX_ISSUE_DETAIL_CHARS]
        self._issues.append(ParseIssue(self._source, code, sanitized, line))


def bounded_lines(
    evidence: TextCommandEvidence,
    expected_source: RawEvidenceSource,
    collector: IssueCollector,
) -> tuple[str, ...] | None:
    """Validate command metadata and split bounded UTF-8 text into lines."""
    if evidence.source is not expected_source:
        collector.add(ParseIssueCode.INCONSISTENT, "unexpected raw evidence source")
        return None
    if evidence.timed_out:
        collector.add(ParseIssueCode.COMMAND_TIMED_OUT, "documented command timed out")
        return None
    if evidence.exit_status != 0:
        collector.add(
            ParseIssueCode.COMMAND_FAILED,
            f"documented command exited with status {evidence.exit_status}",
        )
        return None
    if len(evidence.stdout.encode("utf-8")) > MAX_COMMAND_BYTES:
        collector.add(
            ParseIssueCode.TOO_LARGE,
            f"command output exceeds the {MAX_COMMAND_BYTES}-byte limit",
        )
        return None
    return tuple(evidence.stdout.splitlines())
