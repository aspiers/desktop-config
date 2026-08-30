"""Shared EDID and sysfs evidence builders for the worker test quartet.

The four worker test modules each carried near-identical copies of these
helpers (dc-6de). What genuinely differs between them — which EDID each
connector carries, which profile source is read, how a fingerprint wildcard
is filled — stays in the caller as explicit arguments, so each module's
scenario semantics remain visible at its call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def edid_fixture(directory: Path, name: str) -> bytes:
    """Read one hex EDID fixture as bytes."""
    return bytes.fromhex((directory / name).read_text(encoding="ascii"))


def saved_edid(setup_path: Path, output: str, *, wildcard_fill: str) -> bytes:
    """Extract one output's saved fingerprint EDID from an autorandr setup file.

    Saved Samsung setups intentionally wildcard one unstable extension region;
    *wildcard_fill* fills it deterministically for identity-guard tests.
    """
    value = next(
        line.split()[1]
        for line in setup_path.read_text(encoding="ascii").splitlines()
        if line.startswith(f"{output} ")
    )
    return bytes.fromhex(value.replace("*", wildcard_fill))


def write_sysfs_connectors(
    root: Path,
    connectors: Iterable[tuple[str, int, bytes]],
) -> Path:
    """Materialize a synthetic DRM connector tree for the rooted sysfs reader."""
    for name, connector_id, edid in connectors:
        connector = root / name
        connector.mkdir(parents=True)
        connector.joinpath("status").write_text("connected\n", encoding="ascii")
        connector.joinpath("connector_id").write_text(
            f"{connector_id}\n", encoding="ascii"
        )
        connector.joinpath("edid").write_bytes(edid)
    return root
