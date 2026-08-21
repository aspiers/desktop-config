"""Typed, read-only DRM sysfs and EDID observation.

The adapter has no implicit ``/sys`` default. Callers must inject a rooted reader,
which makes production authority explicit and keeps tests away from the host DRM
tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePath
from typing import Protocol, final

from ..model import (  # noqa: TID252
    ConnectorIdentityEvidence,
    EdidEvidence,
    EdidIntegrity,
)

EDID_BLOCK_BYTES: int = 128
EDID_HEADER: bytes = bytes.fromhex("00ffffffffffff00")
MAX_EDID_BYTES: int = EDID_BLOCK_BYTES * 256
MAX_CONNECTOR_ID: int = (1 << 32) - 1
MAX_TEXT_BYTES: int = 64


class EvidenceState(StrEnum):
    """Availability and syntax state of one sysfs field."""

    AVAILABLE = "available"
    MISSING = "missing"
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"


class ConnectorStatus(StrEnum):
    """Kernel connector status, retaining unknown evidence."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class ConnectorKind(StrEnum):
    """Connector family relevant to laptop-fallback policy."""

    INTERNAL = "internal"
    EXTERNAL = "external"
    VIRTUAL = "virtual"


class ExternalPresence(StrEnum):
    """Three-valued external connector presence proof."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class EdidBlockState(StrEnum):
    """Length state of one advertised EDID block."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING = "missing"


class EdidIssue(StrEnum):
    """Closed set of fail-closed EDID validation failures."""

    TOO_SHORT = "too_short"
    TOO_LARGE = "too_large"
    HEADER_INVALID = "header_invalid"
    BASE_CHECKSUM_INVALID = "base_checksum_invalid"
    ADVERTISED_BLOCK_MISSING = "advertised_block_missing"
    ADVERTISED_BLOCK_INCOMPLETE = "advertised_block_incomplete"
    EXTENSION_CHECKSUM_INVALID = "extension_checksum_invalid"
    TRAILING_DATA = "trailing_data"


class EdidHexError(ValueError):
    """A textual EDID fixture is not strict hexadecimal input."""


class EvidenceReadError(OSError):
    """A bounded sysfs read could not be completed safely."""


@dataclass(frozen=True, slots=True)
class EdidBlockValidation:
    """Independent length/checksum result for one advertised block."""

    index: int
    state: EdidBlockState
    checksum_valid: bool | None


@dataclass(frozen=True, slots=True)
class ParsedEdid:
    """Validated EDID identity and readiness evidence."""

    integrity: EdidIntegrity
    actual_length: int
    advertised_extension_count: int | None
    expected_length: int | None
    base_hash: str | None
    raw_hash: str
    blocks: tuple[EdidBlockValidation, ...]
    issues: tuple[EdidIssue, ...]

    @property
    def base_identity_available(self) -> bool:
        """Return whether the exact checksum-valid base identity is usable."""
        return self.base_hash is not None

    @property
    def fully_ready(self) -> bool:
        """Return whether all advertised bytes and checksums are valid."""
        return self.integrity is EdidIntegrity.COMPLETE


@dataclass(frozen=True, slots=True)
class ConnectorId:
    """Typed connector-ID field without conflating failure and absence."""

    state: EvidenceState
    value: int | None = None


@dataclass(frozen=True, slots=True)
class ConnectorEdid:
    """Raw and parsed EDID evidence for one connector."""

    state: EvidenceState
    raw: bytes | None = None
    parsed: ParsedEdid | None = None

    def __post_init__(self) -> None:
        if self.state is EvidenceState.AVAILABLE:
            if self.raw is None or self.parsed is None:
                msg = "available EDID requires raw and parsed evidence"
                raise ValueError(msg)
        elif self.raw is not None or self.parsed is not None:
            msg = "unavailable EDID cannot carry raw or parsed evidence"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DrmConnector:
    """One immutable DRM connector sample."""

    kernel_name: str
    output_name: str
    kind: ConnectorKind
    status_state: EvidenceState
    status: ConnectorStatus
    connector_id: ConnectorId
    edid: ConnectorEdid

    @property
    def connected(self) -> bool:
        """Return only positive kernel connection evidence."""
        return self.status is ConnectorStatus.CONNECTED

    @property
    def unresolved_connected(self) -> bool:
        """Return whether connected hardware lacks fully ready EDID evidence."""
        return self.connected and (
            self.edid.parsed is None or not self.edid.parsed.fully_ready
        )

    def edid_evidence(self) -> EdidEvidence:
        """Convert the rich adapter result to the canonical domain evidence."""
        parsed = self.edid.parsed
        if parsed is None:
            return EdidEvidence(self.output_name, EdidIntegrity.ABSENT)
        return EdidEvidence(self.output_name, parsed.integrity, parsed.base_hash)

    def identity_evidence(
        self, x_connector_id: int | None = None
    ) -> ConnectorIdentityEvidence | None:
        """Build canonical connector identity when the kernel ID is usable."""
        if self.connector_id.value is None:
            return None
        return ConnectorIdentityEvidence(
            output=self.output_name,
            kernel_connector=self.kernel_name,
            kernel_connector_id=self.connector_id.value,
            x_connector_id=x_connector_id,
        )


@dataclass(frozen=True, slots=True)
class DrmSnapshot:
    """Deterministically ordered kernel connector evidence."""

    scan_state: EvidenceState
    connectors: tuple[DrmConnector, ...]

    @property
    def connected_outputs(self) -> tuple[str, ...]:
        """Return all positively connected non-virtual outputs."""
        return tuple(
            item.output_name
            for item in self.connectors
            if item.connected and item.kind is not ConnectorKind.VIRTUAL
        )

    @property
    def connected_external_outputs(self) -> tuple[str, ...]:
        """Return positively connected external outputs."""
        return tuple(
            item.output_name
            for item in self.connectors
            if item.connected and item.kind is ConnectorKind.EXTERNAL
        )

    @property
    def unresolved_connected_outputs(self) -> tuple[str, ...]:
        """Return connected outputs whose EDID readiness is uncertain."""
        return tuple(
            item.output_name
            for item in self.connectors
            if item.unresolved_connected and item.kind is not ConnectorKind.VIRTUAL
        )

    @property
    def external_presence(self) -> ExternalPresence:
        """Prove presence/absence without treating failed reads as unplug."""
        external = tuple(
            item for item in self.connectors if item.kind is ConnectorKind.EXTERNAL
        )
        if any(item.connected for item in external):
            return ExternalPresence.PRESENT
        if self.scan_state is not EvidenceState.AVAILABLE or any(
            item.status is ConnectorStatus.UNKNOWN for item in external
        ):
            return ExternalPresence.UNKNOWN
        return ExternalPresence.ABSENT


class ReadOnlyTree(Protocol):
    """Minimal injected interface for bounded, read-only sysfs access."""

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        """Return matching directory basenames."""
        ...

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        """Read at most ``limit`` bytes or raise typed evidence failure."""
        ...


@final
class RootedSysfsReader:
    """Filesystem implementation confined to an explicitly injected root."""

    def __init__(self, root: Path) -> None:
        """Confine all subsequent reads to ``root``."""
        self._root = root

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        """List matching directories without returning absolute paths."""
        try:
            self._root.stat()
            if not self._root.is_dir():
                msg = "injected sysfs root is not a directory"
                raise EvidenceReadError(msg)
            return tuple(
                sorted(path.name for path in self._root.glob(pattern) if path.is_dir())
            )
        except OSError as error:
            if isinstance(error, EvidenceReadError):
                raise
            raise EvidenceReadError(str(error)) from error

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        """Perform one bounded read below the configured root."""
        relative = PurePath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            msg = "sysfs reads must remain below the injected root"
            raise EvidenceReadError(msg)
        path = self._root.joinpath(*relative.parts)
        try:
            with path.open("rb") as stream:
                value = stream.read(limit + 1)
        except OSError as error:
            raise EvidenceReadError(str(error)) from error
        if len(value) > limit:
            msg = f"sysfs field exceeds the {limit}-byte read limit"
            raise EvidenceReadError(msg)
        return value


def parse_edid(raw: bytes) -> ParsedEdid:  # noqa: C901, PLR0912
    """Parse binary EDID into separate exact-identity and readiness evidence."""
    actual_length = len(raw)
    raw_hash = sha256(raw).hexdigest()
    if actual_length > MAX_EDID_BYTES:
        return ParsedEdid(
            integrity=EdidIntegrity.BASE_INVALID,
            actual_length=actual_length,
            advertised_extension_count=None,
            expected_length=None,
            base_hash=None,
            raw_hash=raw_hash,
            blocks=(),
            issues=(EdidIssue.TOO_LARGE,),
        )
    if actual_length < EDID_BLOCK_BYTES:
        state = (
            EdidBlockState.MISSING if actual_length == 0 else EdidBlockState.INCOMPLETE
        )
        return ParsedEdid(
            integrity=EdidIntegrity.BASE_INVALID,
            actual_length=actual_length,
            advertised_extension_count=None,
            expected_length=None,
            base_hash=None,
            raw_hash=raw_hash,
            blocks=(EdidBlockValidation(0, state, None),),
            issues=(EdidIssue.TOO_SHORT,),
        )

    extension_count = raw[126]
    expected_blocks = extension_count + 1
    expected_length = expected_blocks * EDID_BLOCK_BYTES
    blocks = tuple(_validate_block(raw, index) for index in range(expected_blocks))
    issues: list[EdidIssue] = []
    base = raw[:EDID_BLOCK_BYTES]
    header_valid = base.startswith(EDID_HEADER)
    base_checksum_valid = blocks[0].checksum_valid is True
    if not header_valid:
        issues.append(EdidIssue.HEADER_INVALID)
    if not base_checksum_valid:
        issues.append(EdidIssue.BASE_CHECKSUM_INVALID)

    for block in blocks[1:]:
        if block.state is EdidBlockState.MISSING:
            _append_once(issues, EdidIssue.ADVERTISED_BLOCK_MISSING)
        elif block.state is EdidBlockState.INCOMPLETE:
            _append_once(issues, EdidIssue.ADVERTISED_BLOCK_INCOMPLETE)
        elif block.checksum_valid is False:
            _append_once(issues, EdidIssue.EXTENSION_CHECKSUM_INVALID)
    if actual_length > expected_length:
        issues.append(EdidIssue.TRAILING_DATA)

    base_hash = (
        sha256(base).hexdigest() if header_valid and base_checksum_valid else None
    )
    if base_hash is None:
        integrity = EdidIntegrity.BASE_INVALID
    elif (
        EdidIssue.EXTENSION_CHECKSUM_INVALID in issues
        or EdidIssue.TRAILING_DATA in issues
    ):
        integrity = EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID
    elif actual_length < expected_length:
        integrity = EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE
    else:
        integrity = EdidIntegrity.COMPLETE

    return ParsedEdid(
        integrity=integrity,
        actual_length=actual_length,
        advertised_extension_count=extension_count,
        expected_length=expected_length,
        base_hash=base_hash,
        raw_hash=raw_hash,
        blocks=blocks,
        issues=tuple(issues),
    )


def parse_edid_hex(value: str) -> ParsedEdid:
    """Decode strict hexadecimal fixture/capture input and parse it fail-closed."""
    hexadecimal = "0123456789abcdefABCDEF"
    whitespace = " \t\r\n\v\f"
    if any(character not in hexadecimal + whitespace for character in value):
        msg = "EDID text must contain only hexadecimal digits and ASCII whitespace"
        raise EdidHexError(msg)
    compact = "".join(character for character in value if character not in whitespace)
    if not compact or len(compact) % 2:
        msg = "EDID text must contain a non-empty, even number of hex digits"
        raise EdidHexError(msg)
    return parse_edid(bytes.fromhex(compact))


def classify_connector(kernel_name: str) -> tuple[str, ConnectorKind]:
    """Derive the X-style output name and connector family from sysfs."""
    prefix, separator, output = kernel_name.partition("-")
    if not separator or not prefix.startswith("card") or not prefix[4:].isdigit():
        msg = f"invalid DRM connector directory name: {kernel_name}"
        raise ValueError(msg)
    family = output.split("-", maxsplit=1)[0].casefold()
    if family in {"edp", "lvds", "dsi"}:
        kind = ConnectorKind.INTERNAL
    elif family == "writeback":
        kind = ConnectorKind.VIRTUAL
    else:
        kind = ConnectorKind.EXTERNAL
    return output, kind


def sample_drm(tree: ReadOnlyTree) -> DrmSnapshot:
    """Sample an injected DRM tree without mutating it or consulting X."""
    try:
        names = tree.list_directories("card*-*")
    except EvidenceReadError:
        return DrmSnapshot(EvidenceState.UNREADABLE, ())

    connectors: list[DrmConnector] = []
    malformed_name = False
    for name in names:
        try:
            output_name, kind = classify_connector(name)
        except ValueError:
            malformed_name = True
            continue
        status_state, status = _read_status(tree, name)
        connector_id = _read_connector_id(tree, name)
        edid = _read_edid(tree, name)
        connectors.append(
            DrmConnector(
                kernel_name=name,
                output_name=output_name,
                kind=kind,
                status_state=status_state,
                status=status,
                connector_id=connector_id,
                edid=edid,
            )
        )
    connectors.sort(key=lambda item: (item.output_name, item.kernel_name))
    scan_state = EvidenceState.MALFORMED if malformed_name else EvidenceState.AVAILABLE
    return DrmSnapshot(scan_state, tuple(connectors))


def _validate_block(raw: bytes, index: int) -> EdidBlockValidation:
    start = index * EDID_BLOCK_BYTES
    end = start + EDID_BLOCK_BYTES
    if start >= len(raw):
        return EdidBlockValidation(index, EdidBlockState.MISSING, None)
    if end > len(raw):
        return EdidBlockValidation(index, EdidBlockState.INCOMPLETE, None)
    return EdidBlockValidation(
        index,
        EdidBlockState.COMPLETE,
        sum(raw[start:end]) % 256 == 0,
    )


def _append_once(values: list[EdidIssue], value: EdidIssue) -> None:
    if value not in values:
        values.append(value)


def _read_status(
    tree: ReadOnlyTree, connector: str
) -> tuple[EvidenceState, ConnectorStatus]:
    state, raw = _read_optional(tree, f"{connector}/status", MAX_TEXT_BYTES)
    if state is not EvidenceState.AVAILABLE or raw is None:
        return state, ConnectorStatus.UNKNOWN
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return EvidenceState.MALFORMED, ConnectorStatus.UNKNOWN
    if value == ConnectorStatus.CONNECTED.value:
        return EvidenceState.AVAILABLE, ConnectorStatus.CONNECTED
    if value == ConnectorStatus.DISCONNECTED.value:
        return EvidenceState.AVAILABLE, ConnectorStatus.DISCONNECTED
    if value == ConnectorStatus.UNKNOWN.value:
        return EvidenceState.AVAILABLE, ConnectorStatus.UNKNOWN
    return EvidenceState.MALFORMED, ConnectorStatus.UNKNOWN


def _read_connector_id(tree: ReadOnlyTree, connector: str) -> ConnectorId:
    state, raw = _read_optional(tree, f"{connector}/connector_id", MAX_TEXT_BYTES)
    if state is not EvidenceState.AVAILABLE or raw is None:
        return ConnectorId(state)
    try:
        text = raw.decode("ascii").strip()
        value = int(text, 10)
    except (UnicodeDecodeError, ValueError):
        return ConnectorId(EvidenceState.MALFORMED)
    if not text.isascii() or not text.isdecimal() or not 0 <= value <= MAX_CONNECTOR_ID:
        return ConnectorId(EvidenceState.MALFORMED)
    return ConnectorId(EvidenceState.AVAILABLE, value)


def _read_edid(tree: ReadOnlyTree, connector: str) -> ConnectorEdid:
    state, raw = _read_optional(tree, f"{connector}/edid", MAX_EDID_BYTES)
    if state is not EvidenceState.AVAILABLE or raw is None:
        return ConnectorEdid(state)
    if not raw:
        return ConnectorEdid(EvidenceState.EMPTY)
    return ConnectorEdid(EvidenceState.AVAILABLE, raw, parse_edid(raw))


def _read_optional(
    tree: ReadOnlyTree, relative_path: str, limit: int
) -> tuple[EvidenceState, bytes | None]:
    try:
        return EvidenceState.AVAILABLE, tree.read_bytes(relative_path, limit)
    except EvidenceReadError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return EvidenceState.MISSING, None
        return EvidenceState.UNREADABLE, None
