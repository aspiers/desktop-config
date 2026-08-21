"""Contract tests for bounded DRM sysfs and EDID observation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from monitor_controller.observer.drm import (
    EDID_BLOCK_BYTES,
    MAX_EDID_BYTES,
    ConnectorKind,
    ConnectorStatus,
    EdidBlockState,
    EdidHexError,
    EdidIntegrity,
    EdidIssue,
    EvidenceReadError,
    EvidenceState,
    ExternalPresence,
    RootedSysfsReader,
    classify_connector,
    parse_edid,
    parse_edid_hex,
    sample_drm,
)

FIXTURES = Path(__file__).parent / "fixtures"
EDID_FIXTURES = FIXTURES / "edid"
SYSFS_FIXTURES = FIXTURES / "sysfs"


def read_edid(name: str) -> bytes:
    return bytes.fromhex((EDID_FIXTURES / name).read_text(encoding="ascii"))


@pytest.mark.parametrize(
    ("name", "integrity", "base_identity", "ready", "issues"),
    [
        ("valid-base.hex", EdidIntegrity.COMPLETE, True, True, ()),
        (
            "incomplete.hex",
            EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE,
            True,
            False,
            (
                EdidIssue.ADVERTISED_BLOCK_INCOMPLETE,
                EdidIssue.ADVERTISED_BLOCK_MISSING,
            ),
        ),
        (
            "extension-checksum-invalid.hex",
            EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
            True,
            False,
            (EdidIssue.EXTENSION_CHECKSUM_INVALID,),
        ),
        (
            "base-checksum-invalid.hex",
            EdidIntegrity.BASE_INVALID,
            False,
            False,
            (EdidIssue.BASE_CHECKSUM_INVALID,),
        ),
        (
            "samsung-broken-captured.hex",
            EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
            True,
            False,
            (
                EdidIssue.EXTENSION_CHECKSUM_INVALID,
                EdidIssue.ADVERTISED_BLOCK_INCOMPLETE,
            ),
        ),
        (
            "samsung-settled-synthetic.hex",
            EdidIntegrity.COMPLETE,
            True,
            True,
            (),
        ),
    ],
)
def test_edid_fixture_classification(
    name: str,
    integrity: EdidIntegrity,
    base_identity: bool,
    ready: bool,
    issues: tuple[EdidIssue, ...],
) -> None:
    parsed = parse_edid(read_edid(name))

    assert parsed.integrity is integrity
    assert parsed.base_identity_available is base_identity
    assert parsed.fully_ready is ready
    assert set(parsed.issues) == set(issues)


def test_captured_broken_and_synthetic_settled_share_exact_base_identity() -> None:
    broken = parse_edid(read_edid("samsung-broken-captured.hex"))
    settled = parse_edid(read_edid("samsung-settled-synthetic.hex"))

    assert broken.actual_length == 400
    assert settled.actual_length == 512
    assert broken.advertised_extension_count == 3
    assert broken.expected_length == 512
    assert broken.base_hash == settled.base_hash
    assert [(block.state, block.checksum_valid) for block in broken.blocks] == [
        (EdidBlockState.COMPLETE, True),
        (EdidBlockState.COMPLETE, True),
        (EdidBlockState.COMPLETE, False),
        (EdidBlockState.INCOMPLETE, None),
    ]
    assert all(
        block.state is EdidBlockState.COMPLETE and block.checksum_valid
        for block in settled.blocks
    )


def test_every_advertised_block_gets_a_separate_length_result() -> None:
    raw = bytearray(read_edid("valid-base.hex"))
    raw[126] = 2
    raw[127] = (-sum(raw[:127])) % 256
    raw.extend(bytes(EDID_BLOCK_BYTES))

    parsed = parse_edid(bytes(raw))

    assert parsed.integrity is EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE
    assert len(parsed.blocks) == 3
    assert parsed.blocks[0].checksum_valid is True
    assert parsed.blocks[1].checksum_valid is True
    assert parsed.blocks[2].state is EdidBlockState.MISSING


@pytest.mark.parametrize("raw", [b"", b"short", bytes(EDID_BLOCK_BYTES - 1)])
def test_short_binary_edid_fails_closed(raw: bytes) -> None:
    parsed = parse_edid(raw)

    assert parsed.integrity is EdidIntegrity.BASE_INVALID
    assert parsed.base_hash is None
    assert parsed.issues == (EdidIssue.TOO_SHORT,)


def test_oversized_binary_edid_fails_closed_without_parsing_blocks() -> None:
    parsed = parse_edid(bytes(MAX_EDID_BYTES + 1))

    assert parsed.integrity is EdidIntegrity.BASE_INVALID
    assert parsed.blocks == ()
    assert parsed.issues == (EdidIssue.TOO_LARGE,)


def test_bad_header_fails_even_with_a_valid_base_checksum() -> None:
    raw = bytearray(read_edid("valid-base.hex"))
    raw[0] = 1
    raw[127] = (-sum(raw[:127])) % 256

    parsed = parse_edid(bytes(raw))

    assert parsed.integrity is EdidIntegrity.BASE_INVALID
    assert parsed.base_hash is None
    assert parsed.issues == (EdidIssue.HEADER_INVALID,)


def test_bytes_beyond_advertised_length_fail_closed() -> None:
    parsed = parse_edid(read_edid("valid-base.hex") + b"\x00")

    assert parsed.integrity is EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID
    assert parsed.issues == (EdidIssue.TRAILING_DATA,)


@pytest.mark.parametrize(
    "value", ["", "0", "00zz", "00*11", "00:11", "00\N{NO-BREAK SPACE}11"]
)
def test_malformed_hex_fails_closed(value: str) -> None:
    with pytest.raises(EdidHexError):
        parse_edid_hex(value)


def test_hex_parser_accepts_ascii_whitespace_only_between_bytes() -> None:
    raw = read_edid("valid-base.hex")
    value = "\n".join(raw.hex()[index : index + 32] for index in range(0, 256, 32))

    assert parse_edid_hex(value).raw_hash == parse_edid(raw).raw_hash


@pytest.mark.parametrize(
    ("kernel_name", "output", "kind"),
    [
        ("card0-eDP-1", "eDP-1", ConnectorKind.INTERNAL),
        ("card1-LVDS-2", "LVDS-2", ConnectorKind.INTERNAL),
        ("card2-DSI-1", "DSI-1", ConnectorKind.INTERNAL),
        ("card0-Writeback-1", "Writeback-1", ConnectorKind.VIRTUAL),
        ("card0-DP-3", "DP-3", ConnectorKind.EXTERNAL),
        ("card12-HDMI-A-1", "HDMI-A-1", ConnectorKind.EXTERNAL),
        ("card0-Unknown-1", "Unknown-1", ConnectorKind.EXTERNAL),
    ],
)
def test_connector_family_classification(
    kernel_name: str, output: str, kind: ConnectorKind
) -> None:
    assert classify_connector(kernel_name) == (output, kind)


@pytest.mark.parametrize("kernel_name", ["card0", "DP-1", "cardx-DP-1"])
def test_malformed_connector_directory_is_rejected(kernel_name: str) -> None:
    with pytest.raises(ValueError, match="invalid DRM connector"):
        classify_connector(kernel_name)


@pytest.mark.parametrize(
    ("tree_name", "presence", "connected", "unresolved", "integrity"),
    [
        (
            "connected",
            ExternalPresence.PRESENT,
            ("DP-3", "eDP-1"),
            (),
            EdidIntegrity.COMPLETE,
        ),
        (
            "disconnected",
            ExternalPresence.ABSENT,
            ("eDP-1",),
            (),
            EdidIntegrity.ABSENT,
        ),
        (
            "missing",
            ExternalPresence.PRESENT,
            ("DP-3",),
            ("DP-3",),
            EdidIntegrity.ABSENT,
        ),
        (
            "incomplete",
            ExternalPresence.PRESENT,
            ("DP-3",),
            ("DP-3",),
            EdidIntegrity.BASE_VALID_EXTENSIONS_INCOMPLETE,
        ),
        (
            "checksum-invalid",
            ExternalPresence.PRESENT,
            ("DP-3",),
            ("DP-3",),
            EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
        ),
        (
            "samsung-broken",
            ExternalPresence.PRESENT,
            ("DP-3",),
            ("DP-3",),
            EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID,
        ),
        (
            "samsung-settled",
            ExternalPresence.PRESENT,
            ("DP-3",),
            (),
            EdidIntegrity.COMPLETE,
        ),
    ],
)
def test_temporary_sysfs_fixture_trees(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    tree_name: str,
    presence: ExternalPresence,
    connected: tuple[str, ...],
    unresolved: tuple[str, ...],
    integrity: EdidIntegrity,
) -> None:
    root = tmp_path / tree_name
    shutil.copytree(SYSFS_FIXTURES / tree_name, root)

    snapshot = sample_drm(RootedSysfsReader(root))
    external = next(
        item for item in snapshot.connectors if item.kind is ConnectorKind.EXTERNAL
    )

    assert snapshot.scan_state is EvidenceState.AVAILABLE
    assert snapshot.external_presence is presence
    assert snapshot.connected_outputs == connected
    assert snapshot.unresolved_connected_outputs == unresolved
    assert external.edid_evidence().integrity is integrity


def test_binary_edid_and_connector_identity_are_preserved_from_temporary_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "settled"
    shutil.copytree(SYSFS_FIXTURES / "samsung-settled", root)
    snapshot = sample_drm(RootedSysfsReader(root))
    connector = snapshot.connectors[0]

    assert connector.edid.raw == read_edid("samsung-settled-synthetic.hex")
    assert connector.connector_id.state is EvidenceState.AVAILABLE
    assert connector.connector_id.value == 73
    identity = connector.identity_evidence(91)
    assert identity is not None
    assert identity.kernel_connector_id == 73
    assert identity.x_connector_id == 91


class DenyingReader:
    """Inject unreadable fields while delegating all other temporary-tree reads."""

    def __init__(self, delegate: RootedSysfsReader, denied: frozenset[str]) -> None:
        """Configure the rooted delegate and relative paths to reject."""
        self._delegate = delegate
        self._denied = denied

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        """Delegate connector discovery."""
        return self._delegate.list_directories(pattern)

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        """Reject configured paths and delegate every other bounded read."""
        if relative_path in self._denied:
            msg = "injected unreadable field"
            raise EvidenceReadError(msg) from PermissionError(relative_path)
        return self._delegate.read_bytes(relative_path, limit)


class FailedScanReader:
    """Inject failure before any connector directory can be discovered."""

    def list_directories(self, pattern: str) -> tuple[str, ...]:
        """Fail the injected connector scan."""
        del pattern
        msg = "injected scan failure"
        raise EvidenceReadError(msg)

    def read_bytes(self, relative_path: str, limit: int) -> bytes:
        """Prove field reads are unreachable after a failed scan."""
        raise AssertionError((relative_path, limit))


def test_unreadable_edid_on_connected_external_is_not_unplug(
    tmp_path: Path,
) -> None:
    root = tmp_path / "settled"
    shutil.copytree(SYSFS_FIXTURES / "samsung-settled", root)
    reader = DenyingReader(RootedSysfsReader(root), frozenset({"card0-DP-3/edid"}))

    snapshot = sample_drm(reader)
    connector = snapshot.connectors[0]

    assert connector.status is ConnectorStatus.CONNECTED
    assert connector.edid.state is EvidenceState.UNREADABLE
    assert connector.edid_evidence().integrity is EdidIntegrity.ABSENT
    assert snapshot.external_presence is ExternalPresence.PRESENT
    assert snapshot.unresolved_connected_outputs == ("DP-3",)


def test_unreadable_status_prevents_external_absence_proof(tmp_path: Path) -> None:
    root = tmp_path / "disconnected"
    shutil.copytree(SYSFS_FIXTURES / "disconnected", root)
    reader = DenyingReader(RootedSysfsReader(root), frozenset({"card0-DP-3/status"}))

    snapshot = sample_drm(reader)

    assert snapshot.external_presence is ExternalPresence.UNKNOWN


def test_failed_connector_scan_is_unknown_not_empty_absence() -> None:
    snapshot = sample_drm(FailedScanReader())

    assert snapshot.scan_state is EvidenceState.UNREADABLE
    assert snapshot.connectors == ()
    assert snapshot.external_presence is ExternalPresence.UNKNOWN


def test_missing_injected_root_is_unknown_not_empty_absence(tmp_path: Path) -> None:
    snapshot = sample_drm(RootedSysfsReader(tmp_path / "does-not-exist"))

    assert snapshot.scan_state is EvidenceState.UNREADABLE
    assert snapshot.external_presence is ExternalPresence.UNKNOWN


def test_malformed_connector_name_prevents_absence_proof(tmp_path: Path) -> None:
    (tmp_path / "cardx-DP-1").mkdir()

    snapshot = sample_drm(RootedSysfsReader(tmp_path))

    assert snapshot.scan_state is EvidenceState.MALFORMED
    assert snapshot.external_presence is ExternalPresence.UNKNOWN


def test_kernel_unknown_status_is_available_uncertainty(tmp_path: Path) -> None:
    connector = tmp_path / "card0-DP-1"
    connector.mkdir()
    (connector / "status").write_text("unknown\n", encoding="ascii")

    sampled = sample_drm(RootedSysfsReader(tmp_path)).connectors[0]

    assert sampled.status_state is EvidenceState.AVAILABLE
    assert sampled.status is ConnectorStatus.UNKNOWN


def test_malformed_status_and_connector_id_remain_typed_uncertainty(
    tmp_path: Path,
) -> None:
    connector = tmp_path / "card0-DP-1"
    connector.mkdir()
    (connector / "status").write_bytes(b"connecting\n")
    (connector / "connector_id").write_bytes(b"7x\n")
    (connector / "edid").write_bytes(read_edid("valid-base.hex"))

    sampled = sample_drm(RootedSysfsReader(tmp_path)).connectors[0]

    assert sampled.status_state is EvidenceState.MALFORMED
    assert sampled.status is ConnectorStatus.UNKNOWN
    assert sampled.connector_id.state is EvidenceState.MALFORMED
    assert sampled.connector_id.value is None
    assert sampled.identity_evidence() is None


@pytest.mark.parametrize("value", ["-1", "4294967296", "9" * 64])
def test_connector_id_outside_unsigned_32_bit_range_is_malformed(
    tmp_path: Path, value: str
) -> None:
    connector = tmp_path / "card0-DP-1"
    connector.mkdir()
    (connector / "status").write_text("connected\n", encoding="ascii")
    (connector / "connector_id").write_text(value, encoding="ascii")

    sampled = sample_drm(RootedSysfsReader(tmp_path)).connectors[0]

    assert sampled.connector_id.state is EvidenceState.MALFORMED
    assert sampled.connector_id.value is None


def test_empty_edid_is_distinct_from_missing_and_unreadable(tmp_path: Path) -> None:
    root = tmp_path / "disconnected"
    shutil.copytree(SYSFS_FIXTURES / "disconnected", root)

    snapshot = sample_drm(RootedSysfsReader(root))
    external = next(
        item for item in snapshot.connectors if item.kind is ConnectorKind.EXTERNAL
    )

    assert external.edid.state is EvidenceState.EMPTY
    assert external.edid.raw is None


def test_rooted_reader_rejects_paths_outside_injected_tree(tmp_path: Path) -> None:
    reader = RootedSysfsReader(tmp_path)

    with pytest.raises(EvidenceReadError):
        reader.read_bytes("../sys/class/drm/card0-DP-1/status", 64)
