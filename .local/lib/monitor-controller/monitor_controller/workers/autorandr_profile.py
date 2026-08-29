"""Pure deterministic transaction-local autorandr profile materialization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from monitor_controller.runtime.transactions import TransactionArtifact
from monitor_controller.safeio import SHA256_VALUE

if TYPE_CHECKING:
    from monitor_controller.model import OutputMapping
    from monitor_controller.observer.autorandr import SavedAutorandrProfile

_ACTION_PROFILE = re.compile(r"^application-[0-9a-f]{32}-[1-9][0-9]*$")
_OUTPUT_NAME = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_OPTION_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_SHA256 = SHA256_VALUE
_CONTROL_CHARACTER_BOUNDARY: Final = 32
_MAX_OUTPUT_NAME_CHARS: Final = 128
_CONNECTOR_REFERENCE_OPTIONS: Final = frozenset(
    {"above", "below", "left-of", "right-of", "same-as"}
)

APPLICATION_PAYLOAD_FIELDS: Final = frozenset(
    {
        "action_profile",
        "config_sha256",
        "layout_sha256",
        "postswitch_sha256",
        "setup_sha256",
    }
)
POSTSWITCH_EVIDENCE_FILENAME: Final = "enabled-outputs"
POSTSWITCH_EVIDENCE_ENVIRONMENT: Final = (
    "MONITOR_CONTROLLER_AUTORANDR_POSTSWITCH_EVIDENCE"
)
POSTSWITCH_CONTENT: Final = b"""#!/bin/sh
set -eu

evidence=${MONITOR_CONTROLLER_AUTORANDR_POSTSWITCH_EVIDENCE:-}
[ -n "$evidence" ] || exit 0

tmp=$evidence.$$
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
umask 077
printf '%s\n' "${AUTORANDR_MONITORS:-}" > "$tmp"
mv -f -- "$tmp" "$evidence"
trap - EXIT HUP INT TERM
exit 0
"""


class AutorandrProfileMaterializationError(ValueError):
    """A saved profile and admitted mapping cannot form one safe artifact."""


@dataclass(frozen=True, slots=True)
class MaterializedAutorandrProfile:
    """Canonical action profile content and its immutable transaction manifest."""

    source_profile: str
    action_profile: str
    artifacts: tuple[TransactionArtifact, ...]
    active_outputs: tuple[str, ...]
    layout: str | None

    def __post_init__(self) -> None:
        if not _ACTION_PROFILE.fullmatch(self.action_profile):
            msg = "materialized autorandr action profile name is malformed"
            raise AutorandrProfileMaterializationError(msg)
        paths = tuple(item.relative_path for item in self.artifacts)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            msg = "materialized autorandr artifact paths are not sorted and unique"
            raise AutorandrProfileMaterializationError(msg)

    @property
    def payload(self) -> tuple[tuple[str, str | None], ...]:
        """Return the closed hash manifest embedded in the immutable request."""
        by_name = {
            artifact.relative_path.rsplit("/", maxsplit=1)[-1]: _content_hash(
                artifact.content
            )
            for artifact in self.artifacts
        }
        return tuple(
            sorted(
                (
                    ("action_profile", self.action_profile),
                    ("config_sha256", by_name["config"]),
                    ("layout_sha256", by_name.get("layout")),
                    ("postswitch_sha256", by_name["postswitch"]),
                    ("setup_sha256", by_name["setup"]),
                )
            )
        )

    @property
    def profile_relative_directory(self) -> str:
        """Return the fixed profile directory below the action artifact root."""
        return f"artifacts/xdg-config/autorandr/{self.action_profile}"


def materialize_autorandr_profile(
    profile: SavedAutorandrProfile,
    mapping: tuple[OutputMapping, ...],
    action_profile: str,
) -> MaterializedAutorandrProfile:
    """Render one exact saved-to-live bijection without filesystem access."""
    if not _ACTION_PROFILE.fullmatch(action_profile):
        msg = "autorandr action profile must equal a canonical application action ID"
        raise AutorandrProfileMaterializationError(msg)
    mapping_by_saved = _validate_complete_mapping(profile, mapping)
    config_by_saved = {item.output: item for item in profile.config}
    if len(config_by_saved) != len(profile.config):
        msg = "saved autorandr profile has duplicate config outputs"
        raise AutorandrProfileMaterializationError(msg)
    for block in profile.config:
        for name, value in block.options:
            _validate_option(name, value)

    config_blocks: list[str] = []
    for saved_output in sorted(mapping_by_saved):
        block = config_by_saved.get(saved_output)
        if block is None:
            msg = f"saved setup output {saved_output!r} has no config block"
            raise AutorandrProfileMaterializationError(msg)
        live_output = mapping_by_saved[saved_output]
        lines = [f"output {live_output}"]
        for name, value in block.options:
            rendered_value = _mapped_option_value(
                name,
                value,
                live_output=live_output,
                mapping_by_saved=mapping_by_saved,
            )
            lines.append(name if rendered_value is None else f"{name} {rendered_value}")
        config_blocks.append("\n".join(lines))
    config_content = ("\n".join(config_blocks) + "\n").encode("utf-8")

    setup_by_saved = {item.output: item.value for item in profile.setup}
    setup_content = (
        "".join(
            f"{mapping_by_saved[saved]} {setup_by_saved[saved]}\n"
            for saved in sorted(mapping_by_saved)
        )
    ).encode("ascii")

    profile_root = f"artifacts/xdg-config/autorandr/{action_profile}"
    artifacts: list[TransactionArtifact] = [
        TransactionArtifact(f"{profile_root}/config", config_content),
    ]
    layout = profile.layout if _profile_has_layout_file(profile) else None
    if layout is not None:
        _validate_layout(layout)
        artifacts.append(
            TransactionArtifact(
                f"{profile_root}/layout",
                f"{layout}\n".encode(),
            )
        )
    artifacts.extend(
        (
            TransactionArtifact(
                f"{profile_root}/postswitch",
                POSTSWITCH_CONTENT,
                executable=True,
            ),
            TransactionArtifact(f"{profile_root}/setup", setup_content),
        )
    )
    active_outputs = tuple(
        sorted(
            mapping_by_saved[item.output]
            for item in profile.config
            if item.output in mapping_by_saved and item.active
        )
    )
    return MaterializedAutorandrProfile(
        source_profile=profile.name,
        action_profile=action_profile,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.relative_path)),
        active_outputs=active_outputs,
        layout=layout,
    )


def artifact_hash_matches(content: bytes, expected: str) -> bool:
    """Compare bytes with one canonical request-bound SHA-256 digest."""
    return bool(_SHA256.fullmatch(expected)) and _content_hash(content) == expected


def profile_artifact_path(action_profile: str, name: str) -> str:
    """Return one closed artifact path after validating both fixed components."""
    if not _ACTION_PROFILE.fullmatch(action_profile):
        msg = "request action profile name is malformed"
        raise AutorandrProfileMaterializationError(msg)
    if name not in {"config", "layout", "postswitch", "setup"}:
        msg = "request autorandr artifact name is outside the closed manifest"
        raise AutorandrProfileMaterializationError(msg)
    return f"artifacts/xdg-config/autorandr/{action_profile}/{name}"


def _validate_complete_mapping(
    profile: SavedAutorandrProfile,
    mapping: tuple[OutputMapping, ...],
) -> dict[str, str]:
    keys = tuple(f"{item.saved_output}\0{item.live_output}" for item in mapping)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        msg = "admitted output mapping must be sorted and unique"
        raise AutorandrProfileMaterializationError(msg)
    saved = tuple(item.saved_output for item in mapping)
    live = tuple(item.live_output for item in mapping)
    setup_outputs = {item.output for item in profile.setup}
    if (
        len(setup_outputs) != len(profile.setup)
        or set(saved) != setup_outputs
        or len(saved) != len(set(saved))
        or len(live) != len(set(live))
        or any(not _valid_output_name(item) for item in (*saved, *live))
    ):
        msg = "admitted mapping is not an exact saved-setup-to-live bijection"
        raise AutorandrProfileMaterializationError(msg)
    return {item.saved_output: item.live_output for item in mapping}


def _validate_option(name: str, value: str | None) -> None:
    if not _OPTION_NAME.fullmatch(name):
        msg = f"saved autorandr option name is malformed: {name!r}"
        raise AutorandrProfileMaterializationError(msg)
    if value is not None and (
        not value
        or any(character in "\x00\r\n" for character in value)
        or any(ord(character) < _CONTROL_CHARACTER_BOUNDARY for character in value)
    ):
        msg = f"saved autorandr option value is malformed: {name!r}"
        raise AutorandrProfileMaterializationError(msg)


def _mapped_option_value(
    name: str,
    value: str | None,
    *,
    live_output: str,
    mapping_by_saved: dict[str, str],
) -> str | None:
    """Rewrite every installed XRandR connector-reference option fail-closed."""
    if name not in _CONNECTOR_REFERENCE_OPTIONS:
        return value
    if value is None or not _valid_output_name(value):
        msg = f"saved autorandr connector reference is malformed: {name!r}"
        raise AutorandrProfileMaterializationError(msg)
    mapped = mapping_by_saved.get(value)
    if mapped is None:
        msg = f"saved autorandr connector reference is unmapped: {value!r}"
        raise AutorandrProfileMaterializationError(msg)
    if mapped == live_output:
        msg = f"saved autorandr connector reference is self-referential: {value!r}"
        raise AutorandrProfileMaterializationError(msg)
    return mapped


def _validate_layout(layout: str) -> None:
    if (
        not layout
        or layout.isspace()
        or any(character in "\x00\r\n" for character in layout)
    ):
        msg = "saved autorandr layout metadata is malformed"
        raise AutorandrProfileMaterializationError(msg)


def _profile_has_layout_file(profile: SavedAutorandrProfile) -> bool:
    return any(
        item.path.rsplit("/", maxsplit=1)[-1] == "layout"
        for item in profile.configuration_hashes
    )


def _valid_output_name(value: str) -> bool:
    return (
        len(value) <= _MAX_OUTPUT_NAME_CHARS
        and _OUTPUT_NAME.fullmatch(value) is not None
    )


def _content_hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
