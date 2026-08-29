# ruff: noqa: EM101, EM102, TRY003
"""Pure desktop plan construction and injected bounded staging adapter."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol

from monitor_controller.model import (
    CompletedPlan,
    ConfigurationContentHash,
    DiscardPlan,
    EventGeneration,
    ObservationKey,
    PhysicalToken,
    RawEvidenceSource,
    RequestPlan,
)
from monitor_controller.observer.autorandr import parse_saved_profile
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.safeio import (
    DIRECTORY_OPEN_FLAGS as _DIRECTORY_OPEN_FLAGS,
)
from monitor_controller.safeio import (
    FILE_READ_FLAGS as _FILE_READ_FLAGS,
)
from monitor_controller.safeio import SHA256_VALUE
from monitor_controller.safeio import (
    open_absolute_directory as _shared_open_absolute_directory,
)
from monitor_controller.safeio import (
    stable_file_details as _stable_file_details,
)

if TYPE_CHECKING:
    from monitor_controller.observer.autorandr import SavedAutorandrProfile

from .fluxbox_renderer import FluxboxRenderError, render_fluxbox_keys
from .layout import (
    DisplayScreenSnapshot,
    LayoutPlanningError,
    ResolvedLayout,
    configuration_include_paths,
    layout_path,
    parse_layout,
    parse_sublayouts,
    resolve_layout,
    resolve_sublayouts,
)
from .plan_codec import (
    AtomicPlanStore,
    AutorandrProfileIntent,
    DesktopPlan,
    DesktopPlanBundle,
    DpiIntent,
    DpiSource,
    EmacsFontIntent,
    FluxboxGenerationIntent,
    KeyboardDisposition,
    KeyboardIntent,
    OverlayIntent,
    OverlaySelection,
    PanelIntent,
    PlanArtifact,
    PlanArtifactManifestEntry,
    PlannedAction,
    PlannedActionKind,
    PlannedTopology,
    ResolvedVariable,
    TerminalThemeIntent,
    TransitionGuards,
    WindowLayoutIntent,
    with_artifacts,
)

MAX_CONFIGURATION_INPUT_BYTES: Final = 512 * 1024
MAX_CONFIGURATION_TOTAL_BYTES: Final = 4 * 1024 * 1024
MAX_CONFIGURATION_INPUTS: Final = 256
_CONTEXT_PATH: Final = "desktop/context.json"
_CONFIGURATION_HASH_DOMAIN: Final = b"monitor-controller-configuration-input-v2\x00"
_OUTPUT_NAME = re.compile(r"^[^\s\x00-\x1f\x7f]+$")
_PREPARE_ACTIONS: Final = (
    PlannedActionKind.INSTALL_FLUXBOX_OVERLAY,
    PlannedActionKind.SET_PANEL_PROPERTIES,
    PlannedActionKind.SET_XFCE_DPI,
    PlannedActionKind.CONFIGURE_TERMINALS,
    PlannedActionKind.RELOAD_EMACS_FONTS,
    PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION,
)
_FINALIZE_ACTIONS: Final = (
    PlannedActionKind.APPLY_FLUXBOX_CONFIGURATION,
    PlannedActionKind.APPLY_KEYBOARD_INTENT,
    PlannedActionKind.APPLY_WINDOW_LAYOUT,
    PlannedActionKind.RESTART_FLUXBOX,
    PlannedActionKind.RESTART_XFCE_PANEL,
    PlannedActionKind.RESTART_NM_APPLET,
    PlannedActionKind.CAPTURE_TRAY_DIAGNOSTICS,
)
_MIN_DIRECTORY_LINK_COUNT: Final = 2
_EDID_BASE_BYTES: Final = 128
_EDID_BASE_HEX_CHARS: Final = _EDID_BASE_BYTES * 2
_EDID_HEADER: Final = bytes.fromhex("00ffffffffffff00")
_EDID_DESCRIPTOR_START: Final = 54
_EDID_DESCRIPTOR_BYTES: Final = 18
_EDID_DESCRIPTOR_COUNT: Final = 4
_EDID_DISPLAY_NAME_TAG: Final = 0xFC
_EDID_VENDOR_LETTERS: Final = 26
_ASCII_PRINTABLE_MIN: Final = 32
_ASCII_PRINTABLE_MAX: Final = 126
_EDID_IDENTITY_HASH_DOMAIN: Final = b"monitor-controller-edid-model-v1\x00"
_SHA256_VALUE = SHA256_VALUE
_EDID_MODEL_KEY = re.compile(r"^[A-Z]{3}:[0-9a-f]{4}$")
_EDID_VENDOR_NAMES: Final = {
    "AOC": ("AOC", None),
    "BNQ": ("BenQ", None),
    "BOE": ("BOE", None),
    "GSM": ("LG (GoldStar)", "LG "),
    "LGE": ("LG (GoldStar)", "LG "),
    "SAM": ("Samsung", None),
}


class DesktopPlanningError(ValueError):
    """Immutable inputs cannot produce one exact safe desktop plan."""


class InputRole(StrEnum):
    """Closed semantic role for every configuration byte consumed by planning."""

    CONTEXT = "context"
    AUTORANDR_CONFIG = "autorandr_config"
    AUTORANDR_SETUP = "autorandr_setup"
    AUTORANDR_LAYOUT = "autorandr_layout"
    MAIN_LAYOUT = "main_layout"
    LAYOUT_INCLUDE = "layout_include"
    SUBLAYOUTS = "sublayouts"
    LAYOUT_OVERLAY = "layout_overlay"
    HOST_OVERLAY = "host_overlay"
    PANEL_POLICY = "panel_policy"
    DPI_POLICY = "dpi_policy"
    FONT_POLICY = "font_policy"
    TERMINAL_POLICY = "terminal_policy"
    KITTY_THEME = "kitty_theme"
    FLUXBOX_TEMPLATE = "fluxbox_template"
    FLUXBOX_GENERATOR = "fluxbox_generator"
    KEYBOARD_POLICY = "keyboard_policy"
    EMACS_POLICY = "emacs_policy"


@dataclass(frozen=True, slots=True)
class MonitorModelIdentity:
    """One model proven from a fixed saved EDID base block."""

    output: str
    model: str
    evidence_hash: str
    # PNP vendor code plus little-endian product id, e.g. "AOC:2802". Unlike
    # `model`, which imitates hwinfo's free-text rendering, this is read
    # directly from EDID bytes and is the only key DPI overrides may use.
    edid_model: str

    def __post_init__(self) -> None:
        if not _OUTPUT_NAME.fullmatch(self.output):
            raise DesktopPlanningError("monitor identity output is malformed")
        if not self.model or self.model.isspace() or "\x00" in self.model:
            raise DesktopPlanningError("monitor identity model must not be empty")
        if _SHA256_VALUE.fullmatch(self.evidence_hash) is None:
            raise DesktopPlanningError("monitor identity hash is malformed")
        if _EDID_MODEL_KEY.fullmatch(self.edid_model) is None:
            raise DesktopPlanningError("monitor EDID model key is malformed")


@dataclass(frozen=True, slots=True)
class ProfileMonitorIdentity:
    """Saved profile models with one config-selected primary identity."""

    primary: MonitorModelIdentity
    monitors: tuple[MonitorModelIdentity, ...]

    def __post_init__(self) -> None:
        outputs = tuple(item.output for item in self.monitors)
        if outputs != tuple(sorted(outputs)) or len(outputs) != len(set(outputs)):
            raise DesktopPlanningError("profile monitor identities are not canonical")
        if self.primary not in self.monitors:
            raise DesktopPlanningError(
                "primary monitor identity is absent from profile"
            )


@dataclass(frozen=True, slots=True)
class DesktopContext:
    """Hashed host policy plus saved-EDID primary model evidence."""

    host_name: str
    is_laptop: bool
    theme: str
    reference_dpi: int
    primary_monitor_output: str
    primary_monitor_model: str
    primary_monitor_identity_hash: str
    primary_monitor_edid_model: str
    benq_connected: bool
    emacs_font_height: int = 130

    def __post_init__(self) -> None:
        for value, field in (
            (self.host_name, "desktop host name"),
            (self.theme, "desktop theme"),
            (self.primary_monitor_output, "primary monitor output"),
            (self.primary_monitor_model, "primary monitor model"),
        ):
            if not value or value.isspace() or "\x00" in value:
                raise DesktopPlanningError(f"{field} must not be empty")
        if not _OUTPUT_NAME.fullmatch(self.primary_monitor_output):
            raise DesktopPlanningError("primary monitor output is malformed")
        if _SHA256_VALUE.fullmatch(self.primary_monitor_identity_hash) is None:
            raise DesktopPlanningError("primary monitor identity hash is malformed")
        if _EDID_MODEL_KEY.fullmatch(self.primary_monitor_edid_model) is None:
            raise DesktopPlanningError("primary monitor EDID model key is malformed")
        if self.theme not in {"dark", "light"}:
            raise DesktopPlanningError("desktop theme must be dark or light")
        if self.reference_dpi <= 0 or self.emacs_font_height <= 0:
            raise DesktopPlanningError("desktop DPI/font height must be positive")


def derive_profile_monitor_identity(
    profile: SavedAutorandrProfile,
) -> ProfileMonitorIdentity:
    """Derive every model solely from each saved fingerprint's fixed EDID base."""
    primary_outputs = tuple(
        item.output for item in profile.config if item.active and item.primary
    )
    if len(primary_outputs) != 1:
        raise DesktopPlanningError(
            "saved profile requires exactly one active primary model identity"
        )
    monitors = tuple(
        sorted(
            (_monitor_identity(item.output, item.value) for item in profile.setup),
            key=lambda item: item.output,
        )
    )
    primary = tuple(item for item in monitors if item.output == primary_outputs[0])
    if len(primary) != 1:
        raise DesktopPlanningError(
            "saved primary output lacks one fixed EDID model identity"
        )
    return ProfileMonitorIdentity(primary[0], monitors)


def _monitor_identity(output: str, pattern: str) -> MonitorModelIdentity:
    wildcard = pattern.find("*")
    if wildcard >= 0 and wildcard < _EDID_BASE_HEX_CHARS:
        raise DesktopPlanningError(
            "saved fingerprint wildcard obscures the EDID base model identity"
        )
    base_hex = pattern[:_EDID_BASE_HEX_CHARS]
    if (
        len(base_hex) != _EDID_BASE_HEX_CHARS
        or re.fullmatch(r"[0-9a-fA-F]+", base_hex) is None
    ):
        raise DesktopPlanningError(
            "saved fingerprint lacks a fixed complete EDID base model identity"
        )
    base = bytes.fromhex(base_hex)
    if base[: len(_EDID_HEADER)] != _EDID_HEADER or sum(base) % 256 != 0:
        raise DesktopPlanningError("saved fingerprint EDID base is malformed")
    vendor_code = _edid_vendor_code(base)
    display_names = tuple(
        _edid_display_name(base[offset : offset + _EDID_DESCRIPTOR_BYTES])
        for offset in range(
            _EDID_DESCRIPTOR_START,
            _EDID_DESCRIPTOR_START + _EDID_DESCRIPTOR_COUNT * _EDID_DESCRIPTOR_BYTES,
            _EDID_DESCRIPTOR_BYTES,
        )
        if base[offset : offset + 3] == b"\x00\x00\x00"
        and base[offset + 3] == _EDID_DISPLAY_NAME_TAG
    )
    if len(display_names) != 1:
        raise DesktopPlanningError(
            "saved fingerprint EDID model descriptor is missing or ambiguous"
        )
    vendor_name, descriptor_prefix = _EDID_VENDOR_NAMES.get(
        vendor_code, (vendor_code, None)
    )
    display_name = display_names[0]
    if descriptor_prefix is not None and display_name.startswith(descriptor_prefix):
        display_name = display_name.removeprefix(descriptor_prefix)
    model = f"{vendor_name} {display_name}"
    digest = hashlib.sha256()
    digest.update(_EDID_IDENTITY_HASH_DOMAIN)
    _hash_configuration_component(digest, b"output", output.encode("utf-8"))
    _hash_configuration_component(digest, b"base-edid", base)
    edid_model = f"{vendor_code}:{_edid_product_code(base):04x}"
    return MonitorModelIdentity(
        output, model, f"sha256:{digest.hexdigest()}", edid_model
    )


def _edid_vendor_code(base: bytes) -> str:
    encoded = int.from_bytes(base[8:10], "big")
    values = ((encoded >> 10) & 31, (encoded >> 5) & 31, encoded & 31)
    if any(value < 1 or value > _EDID_VENDOR_LETTERS for value in values):
        raise DesktopPlanningError("saved fingerprint EDID vendor is malformed")
    return "".join(chr(ord("A") + value - 1) for value in values)


def _edid_product_code(base: bytes) -> int:
    return int.from_bytes(base[10:12], "little")


def _edid_display_name(descriptor: bytes) -> str:
    raw = descriptor[5:]
    if b"\n" in raw:
        raw, padding = raw.split(b"\n", maxsplit=1)
        if any(value not in {0, 32} for value in padding):
            raise DesktopPlanningError(
                "saved fingerprint EDID model descriptor has malformed padding"
            )
    raw = raw.rstrip(b"\x00 ")
    if not raw or any(
        value < _ASCII_PRINTABLE_MIN or value > _ASCII_PRINTABLE_MAX for value in raw
    ):
        raise DesktopPlanningError(
            "saved fingerprint EDID model descriptor has malformed text"
        )
    return raw.decode("ascii")


@dataclass(frozen=True, slots=True)
class ConfigurationInput:
    """One bounded regular-file snapshot, including explicit absence evidence."""

    roles: tuple[InputRole, ...]
    path: str
    content: bytes | None

    def __post_init__(self) -> None:
        if self.roles != tuple(sorted(set(self.roles), key=lambda item: item.value)):
            raise DesktopPlanningError("configuration roles must be sorted and unique")
        if not self.roles:
            raise DesktopPlanningError("configuration input requires a semantic role")
        candidate = PurePosixPath(self.path)
        if (
            not self.path
            or candidate.as_posix() != self.path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or "\x00" in self.path
        ):
            raise DesktopPlanningError("configuration input path is not canonical")
        if self.content is not None and (
            not self.content or len(self.content) > MAX_CONFIGURATION_INPUT_BYTES
        ):
            raise DesktopPlanningError(
                "configuration input content is empty or exceeds its size limit"
            )

    @property
    def content_hash(self) -> ConfigurationContentHash:
        """Hash presence, path, sorted roles, length, and exact content."""
        digest = hashlib.sha256()
        digest.update(_CONFIGURATION_HASH_DOMAIN)
        _hash_configuration_component(digest, b"path", self.path.encode("utf-8"))
        for role in self.roles:
            _hash_configuration_component(digest, b"role", role.value.encode("ascii"))
        if self.content is None:
            _hash_configuration_component(digest, b"presence", b"absent")
            _hash_configuration_component(digest, b"content", b"")
        else:
            _hash_configuration_component(digest, b"presence", b"present")
            _hash_configuration_component(digest, b"content", self.content)
        return ConfigurationContentHash(self.path, f"sha256:{digest.hexdigest()}")


@dataclass(frozen=True, slots=True)
class DesktopConfigurationSnapshot:
    """Complete sorted configuration file set consumed by one plan."""

    inputs: tuple[ConfigurationInput, ...]

    def __post_init__(self) -> None:
        if not self.inputs or len(self.inputs) > MAX_CONFIGURATION_INPUTS:
            raise DesktopPlanningError(
                "configuration input count is outside accepted bounds"
            )
        paths = tuple(item.path for item in self.inputs)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise DesktopPlanningError(
                "configuration input paths must be sorted and unique"
            )
        total = sum(len(item.content or b"") for item in self.inputs)
        if total > MAX_CONFIGURATION_TOTAL_BYTES:
            raise DesktopPlanningError("configuration inputs exceed aggregate limit")
        roles = {role for item in self.inputs for role in item.roles}
        required = set(InputRole) - {InputRole.AUTORANDR_LAYOUT}
        if missing := required - roles:
            names = ", ".join(sorted(item.value for item in missing))
            raise DesktopPlanningError(f"configuration snapshot lacks roles: {names}")

    @property
    def hashes(self) -> tuple[ConfigurationContentHash, ...]:
        """Return the exact sorted planning-key hash manifest."""
        return tuple(item.content_hash for item in self.inputs)

    def one(self, role: InputRole) -> ConfigurationInput:
        """Return exactly one input for a singleton semantic role."""
        values = tuple(item for item in self.inputs if role in item.roles)
        if len(values) != 1:
            raise DesktopPlanningError(
                f"configuration role {role.value!r} requires exactly one input"
            )
        return values[0]

    def many(self, role: InputRole) -> tuple[ConfigurationInput, ...]:
        """Return every input for a repeatable role in path order."""
        return tuple(item for item in self.inputs if role in item.roles)

    def hashes_for(self, *roles: InputRole) -> tuple[ConfigurationContentHash, ...]:
        """Return sorted hashes for the requested semantic policy roles."""
        wanted = set(roles)
        return tuple(
            item.content_hash
            for item in self.inputs
            if not wanted.isdisjoint(item.roles)
        )


@dataclass(frozen=True, slots=True)
class DesktopDisplaySnapshot:
    """Exact immutable observation-derived display evidence for planning."""

    physical_epoch: int
    physical_token: PhysicalToken
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey
    topology: PlannedTopology
    screens: tuple[DisplayScreenSnapshot, ...]

    def __post_init__(self) -> None:
        if self.physical_epoch < 0:
            raise DesktopPlanningError("display physical epoch cannot be negative")
        outputs = tuple(item.output for item in self.screens)
        if len(set(outputs)) != len(outputs):
            raise DesktopPlanningError("display snapshot repeats output geometry")
        if set(outputs) != set(self.topology.x_active_outputs):
            raise DesktopPlanningError(
                "display snapshot geometry differs from exact active topology"
            )
        if sum(item.primary for item in self.screens) != 1:
            raise DesktopPlanningError("display snapshot requires one primary output")


@dataclass(frozen=True, slots=True)
class DesktopPlanningInputs:
    """All bytes and facts supplied to the pure planner in one immutable value."""

    request: RequestPlan
    display: DesktopDisplaySnapshot
    context: DesktopContext
    configuration: DesktopConfigurationSnapshot

    def __post_init__(self) -> None:
        key = self.request.input_key
        if self.request.profile != key.profile:
            raise DesktopPlanningError("plan request profile differs from input key")
        if self.display.physical_epoch != key.physical_epoch:
            raise DesktopPlanningError("display epoch differs from planning input key")
        if self.display.observation_key != key.observation_key:
            raise DesktopPlanningError(
                "display observation differs from planning input key"
            )
        if self.display.topology.x_active_outputs != key.active_outputs:
            raise DesktopPlanningError(
                "display active topology differs from planning input key"
            )
        if self.configuration.hashes != key.configuration_hashes:
            raise DesktopPlanningError(
                "captured configuration hashes differ from planning input key"
            )
        context = self.configuration.one(InputRole.CONTEXT)
        if context.content != encode_desktop_context(self.context):
            raise DesktopPlanningError(
                "desktop context bytes differ from the captured context input"
            )
        mapped = {item.live_output for item in key.mapping}
        if mapped != set(self.display.topology.x_connected_outputs):
            raise DesktopPlanningError(
                "planning mapping does not cover exact connected topology"
            )
        primary_mapping = tuple(
            item.live_output
            for item in key.mapping
            if item.saved_output == self.context.primary_monitor_output
        )
        observed_primary = tuple(
            item.output for item in self.display.screens if item.primary
        )
        if primary_mapping != observed_primary:
            raise DesktopPlanningError(
                "saved primary model identity does not map to observed primary output"
            )


class DesktopPlanningInputSource(Protocol):
    """Injected adapter which gathers bounded immutable bytes before planning."""

    def load(self, request: RequestPlan) -> DesktopPlanningInputs:
        """Return one exact snapshot and no live desktop capability."""
        ...


class DesktopDisplaySnapshotSource(Protocol):
    """Return the immutable observation capture admitted by one request."""

    def display_for(self, request: RequestPlan) -> DesktopDisplaySnapshot:
        """Return display facts bound to the request's exact observation key."""
        ...


class DesktopContextSource(Protocol):
    """Combine static host policy with captured saved-profile monitor identity."""

    def context_for(
        self,
        profile: str,
        layout: str,
        monitor_identity: ProfileMonitorIdentity,
    ) -> DesktopContext:
        """Return deterministic context without mutable display discovery."""
        ...


class FilesystemDesktopPlanningInputSource:
    """Read a real configuration tree beneath one explicit immutable root.

    The source never discovers ``HOME``, reads X, refreshes a shared cache, or
    invokes a command.  Display and context facts are injected from the already
    completed observation/configuration capture.
    """

    def __init__(
        self,
        *,
        root: Path,
        display: DesktopDisplaySnapshot | DesktopDisplaySnapshotSource,
        context: DesktopContext | DesktopContextSource,
        emacs_policy_paths: tuple[str, ...] = (
            "bin/monitor-controller-emacs-fonts.el",
        ),
    ) -> None:
        """Bind explicit immutable facts and a configuration-tree root."""
        if not root.is_absolute():
            raise ValueError("desktop configuration root must be absolute")
        if not emacs_policy_paths:
            raise ValueError("at least one explicit Emacs policy path is required")
        self._root = root
        self._root_fd = _open_configuration_root(root)
        self._display = display
        self._context = context
        self._emacs_policy_paths = tuple(sorted(emacs_policy_paths))

    def close(self) -> None:
        """Release the retained configuration-root descriptor."""
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            self.close()

    def configuration_for(
        self, profile: str, layout: str
    ) -> DesktopConfigurationSnapshot:
        """Capture all real files which the requested plan can consume."""
        profile_root = PurePosixPath(".config/autorandr") / profile
        profile_inputs = (
            self._required(InputRole.AUTORANDR_CONFIG, profile_root / "config"),
            self._required(InputRole.AUTORANDR_SETUP, profile_root / "setup"),
            self._optional(InputRole.AUTORANDR_LAYOUT, profile_root / "layout"),
        )
        captured_profile = _parse_captured_profile(profile, *profile_inputs)
        if captured_profile.layout != layout:
            raise DesktopPlanningError(
                "captured autorandr profile layout differs from requested layout"
            )
        monitor_identity = derive_profile_monitor_identity(captured_profile)
        context = self._context_for(profile, layout, monitor_identity)
        values: list[ConfigurationInput] = [
            ConfigurationInput(
                (InputRole.CONTEXT,),
                _CONTEXT_PATH,
                encode_desktop_context(context),
            ),
            *profile_inputs,
        ]
        main = layout_path(layout)
        layout_inputs = self._layout_inputs(main)
        values.extend(layout_inputs)
        values.extend(
            (
                self._required(InputRole.SUBLAYOUTS, ".fluxbox/sublayouts.yaml"),
                self._optional(InputRole.LAYOUT_OVERLAY, f".fluxbox/overlay.{layout}"),
                self._optional(
                    InputRole.HOST_OVERLAY,
                    f".fluxbox/overlay.{context.host_name}",
                ),
                self._required(InputRole.PANEL_POLICY, "bin/setup-panels"),
                self._required(InputRole.DPI_POLICY, "bin/set-layout-dpi"),
                self._required(InputRole.DPI_POLICY, "bin/set-xfce4-dpi"),
                self._required(InputRole.FONT_POLICY, "lib/libfonts.sh"),
                self._required(InputRole.TERMINAL_POLICY, "bin/setup-terminals"),
                self._required(InputRole.TERMINAL_POLICY, "bin/gnome-terminal-config"),
                self._required(InputRole.TERMINAL_POLICY, "bin/gnome-terminal-profile"),
                self._required(InputRole.TERMINAL_POLICY, "bin/xfce4-terminal-config"),
                self._required(InputRole.TERMINAL_POLICY, "bin/kitty-theme-config"),
                self._required(
                    InputRole.KITTY_THEME,
                    f".config/kitty/{context.theme}-theme.conf",
                ),
                self._required(InputRole.FLUXBOX_TEMPLATE, ".fluxbox/keys.erb"),
                self._required(InputRole.FLUXBOX_GENERATOR, "bin/fluxbox-gen-config"),
                self._required(
                    InputRole.FLUXBOX_GENERATOR,
                    ".local/lib/monitor-controller/monitor_controller/desktop/fluxbox_renderer.py",
                ),
                self._required(InputRole.KEYBOARD_POLICY, "bin/setup-keyboard"),
            )
        )
        values.extend(
            self._required(InputRole.EMACS_POLICY, path)
            for path in self._emacs_policy_paths
        )
        by_path: dict[str, ConfigurationInput] = {}
        for item in values:
            previous = by_path.get(item.path)
            if previous is not None:
                if previous.content != item.content:
                    raise DesktopPlanningError(
                        f"configuration path {item.path!r} has conflicting content"
                    )
                roles = tuple(
                    sorted({*previous.roles, *item.roles}, key=lambda role: role.value)
                )
                by_path[item.path] = ConfigurationInput(roles, item.path, item.content)
            else:
                by_path[item.path] = item
        return DesktopConfigurationSnapshot(
            tuple(sorted(by_path.values(), key=lambda item: item.path))
        )

    def complete_profile(self, profile: SavedAutorandrProfile) -> SavedAutorandrProfile:
        """Attach the complete desktop manifest used by reducer planning keys."""
        configuration = self.configuration_for(profile.name, profile.layout)
        return replace(profile, configuration_hashes=configuration.hashes)

    def load(self, request: RequestPlan) -> DesktopPlanningInputs:
        """Capture the exact already-admitted full manifest and display snapshot."""
        profile = request.input_key.profile
        layout = request.input_key.layout
        configuration = self.configuration_for(profile, layout)
        if request.input_key.configuration_hashes != configuration.hashes:
            raise DesktopPlanningError(
                "admitted planning key differs from the captured full manifest"
            )
        captured_profile = _parse_captured_profile(
            profile,
            configuration.one(InputRole.AUTORANDR_CONFIG),
            configuration.one(InputRole.AUTORANDR_SETUP),
            configuration.one(InputRole.AUTORANDR_LAYOUT),
        )
        context = self._context_for(
            profile,
            layout,
            derive_profile_monitor_identity(captured_profile),
        )
        return DesktopPlanningInputs(
            request=request,
            display=self._display_for(request),
            context=context,
            configuration=configuration,
        )

    def _display_for(self, request: RequestPlan) -> DesktopDisplaySnapshot:
        if isinstance(self._display, DesktopDisplaySnapshot):
            return self._display
        return self._display.display_for(request)

    def _context_for(
        self,
        profile: str,
        layout: str,
        monitor_identity: ProfileMonitorIdentity,
    ) -> DesktopContext:
        if isinstance(self._context, DesktopContext):
            primary = monitor_identity.primary
            if (
                self._context.primary_monitor_output != primary.output
                or self._context.primary_monitor_model != primary.model
                or self._context.primary_monitor_identity_hash != primary.evidence_hash
            ):
                raise DesktopPlanningError(
                    "injected desktop context differs from saved monitor identity"
                )
            return self._context
        return self._context.context_for(profile, layout, monitor_identity)

    def _layout_inputs(self, main: str) -> tuple[ConfigurationInput, ...]:
        pending = [main]
        captured: dict[str, bytes] = {}
        while pending:
            path = pending.pop()
            if path in captured:
                continue
            content = self._read(path, required=True)
            if content is None:
                raise DesktopPlanningError(f"required layout {path!r} is absent")
            captured[path] = content
            pending.extend(
                dependency
                for dependency in configuration_include_paths(path, content)
                if dependency not in captured
            )
            if len(captured) > MAX_CONFIGURATION_INPUTS:
                raise DesktopPlanningError("layout include graph exceeds its limit")
        return tuple(
            ConfigurationInput(
                (InputRole.MAIN_LAYOUT if path == main else InputRole.LAYOUT_INCLUDE,),
                path,
                content,
            )
            for path, content in sorted(captured.items())
        )

    def _required(
        self, role: InputRole, path: str | PurePosixPath
    ) -> ConfigurationInput:
        logical = PurePosixPath(path).as_posix()
        content = self._read(logical, required=True)
        if content is None:
            raise DesktopPlanningError(f"required configuration {logical!r} is absent")
        return ConfigurationInput((role,), logical, content)

    def _optional(
        self, role: InputRole, path: str | PurePosixPath
    ) -> ConfigurationInput:
        logical = PurePosixPath(path).as_posix()
        return ConfigurationInput((role,), logical, self._read(logical, required=False))

    def _read(  # noqa: C901, PLR0912, PLR0915 - retained-FD capture protocol
        self,
        logical: str,
        *,
        required: bool,
    ) -> bytes | None:
        path = PurePosixPath(logical)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != logical:
            raise DesktopPlanningError("configuration read path is not canonical")
        if self._root_fd < 0:
            raise DesktopPlanningError("configuration source is closed")
        directory_fd = os.dup(self._root_fd)
        try:
            _validate_configuration_directory(directory_fd)
            for component in path.parts[:-1]:
                try:
                    child = os.open(
                        component, _DIRECTORY_OPEN_FLAGS, dir_fd=directory_fd
                    )
                except FileNotFoundError:
                    if not required:
                        return None
                    raise DesktopPlanningError(
                        f"required configuration {logical!r} does not exist"
                    ) from None
                except OSError as error:
                    raise DesktopPlanningError(
                        f"cannot safely open configuration parent for {logical!r}"
                    ) from error
                _validate_configuration_directory(child)
                os.close(directory_fd)
                directory_fd = child
            try:
                descriptor = os.open(
                    path.parts[-1], _FILE_READ_FLAGS, dir_fd=directory_fd
                )
            except FileNotFoundError:
                if required:
                    raise DesktopPlanningError(
                        f"required configuration {logical!r} does not exist"
                    ) from None
                return None
            except OSError as error:
                raise DesktopPlanningError(
                    f"cannot safely open configuration {logical!r}"
                ) from error
            try:
                before = os.fstat(descriptor)
                _validate_configuration_file(before, logical)
                chunks: list[bytes] = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 64 * 1024))
                    if not chunk:
                        raise DesktopPlanningError(
                            f"configuration {logical!r} was truncated during read"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise DesktopPlanningError(
                        f"configuration {logical!r} grew during capture"
                    )
                after = os.fstat(descriptor)
                _validate_configuration_file(after, logical)
                if _stable_file_details(before) != _stable_file_details(after):
                    raise DesktopPlanningError(
                        f"configuration {logical!r} changed during capture"
                    )
                _validate_configuration_directory(directory_fd)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_fd)


class AtomicDesktopPlanningAdapter:
    """Key-check pure plans, then atomically stage them under the planning ID."""

    def __init__(
        self,
        source: DesktopPlanningInputSource,
        store: AtomicPlanStore,
    ) -> None:
        """Bind an input source and private plan store without performing I/O."""
        self._source = source
        self._store = store

    async def create_plan(self, request: RequestPlan) -> CompletedPlan:
        """Capture, bridge to the full key, build, and publish one exact bundle."""
        inputs = await asyncio.to_thread(self._source.load, request)
        await asyncio.sleep(0)
        bundle = build_desktop_plan(inputs)
        await asyncio.sleep(0)
        plan_hash = await asyncio.to_thread(
            self._store.stage, request.action_id, bundle
        )
        staged = await asyncio.to_thread(self._store.read, request.action_id)
        if staged != bundle:
            raise DesktopPlanningError("staged plan differs from its captured bundle")
        return CompletedPlan(plan_hash, staged.plan.guards.input_key)

    async def revoke_plan(self, request: DiscardPlan) -> None:
        """Durably revoke a keyed publisher before runtime cancellation."""
        await asyncio.to_thread(self._store.revoke, request.action_id)

    async def discard_plan(self, request: DiscardPlan) -> None:
        """Discard only a revoked matching plan in this private namespace."""
        await asyncio.to_thread(
            self._store.discard, request.action_id, request.plan_hash
        )

    def close(self) -> None:
        """Release retained source and store descriptors after runtime shutdown."""
        close_source = getattr(self._source, "close", None)
        if close_source is not None:
            close_source()
        self._store.close()


def encode_desktop_context(context: DesktopContext) -> bytes:
    """Return canonical no-float bytes which make context key-sensitive."""
    return json.dumps(
        {
            "benq_connected": context.benq_connected,
            "emacs_font_height": context.emacs_font_height,
            "host_name": context.host_name,
            "is_laptop": context.is_laptop,
            "primary_monitor_edid_model": context.primary_monitor_edid_model,
            "primary_monitor_identity_hash": context.primary_monitor_identity_hash,
            "primary_monitor_model": context.primary_monitor_model,
            "primary_monitor_output": context.primary_monitor_output,
            "reference_dpi": context.reference_dpi,
            "theme": context.theme,
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_desktop_plan(  # noqa: PLR0915
    inputs: DesktopPlanningInputs,
) -> DesktopPlanBundle:
    """Construct deterministic leaf payloads without filesystem, HOME, X, or shell."""
    request = inputs.request
    configuration = inputs.configuration
    parsed_profile = _validate_autorandr_profile(inputs)
    layout_files = tuple(
        (item.path, _required_content(item))
        for item in configuration.inputs
        if any(
            role in item.roles
            for role in {InputRole.MAIN_LAYOUT, InputRole.LAYOUT_INCLUDE}
        )
    )
    try:
        parsed = parse_layout(request.input_key.layout, layout_files)
        resolved = resolve_layout(parsed, inputs.display.screens)
        sublayouts_content = _required_content(configuration.one(InputRole.SUBLAYOUTS))
        sublayouts = resolve_sublayouts(
            parse_sublayouts(sublayouts_content), resolved.screens
        )
    except LayoutPlanningError as error:
        raise DesktopPlanningError(str(error)) from error

    profile_config = configuration.one(InputRole.AUTORANDR_CONFIG)
    profile_setup = configuration.one(InputRole.AUTORANDR_SETUP)
    profile_layout = configuration.one(InputRole.AUTORANDR_LAYOUT)
    profile_config_artifact = PlanArtifact(
        "artifacts/autorandr/config", _required_content(profile_config)
    )
    profile_setup_artifact = PlanArtifact(
        "artifacts/autorandr/setup", _required_content(profile_setup)
    )
    profile_layout_artifact = (
        None
        if profile_layout.content is None
        else PlanArtifact("artifacts/autorandr/layout", profile_layout.content)
    )
    mapped = {item.saved_output: item.live_output for item in request.input_key.mapping}
    primary_saved = next(
        item.output for item in parsed_profile.config if item.active and item.primary
    )
    autorandr = AutorandrProfileIntent(
        config_artifact=profile_config_artifact.relative_path,
        config_sha256=profile_config_artifact.sha256,
        setup_artifact=profile_setup_artifact.relative_path,
        setup_sha256=profile_setup_artifact.sha256,
        layout_artifact=(
            None
            if profile_layout_artifact is None
            else profile_layout_artifact.relative_path
        ),
        layout_sha256=(
            None if profile_layout_artifact is None else profile_layout_artifact.sha256
        ),
        active_outputs=tuple(
            sorted(
                mapped[item.output]
                for item in parsed_profile.config
                if item.active and item.output in mapped
            )
        ),
        primary_output=mapped[primary_saved],
        input_hashes=configuration.hashes_for(
            InputRole.AUTORANDR_CONFIG,
            InputRole.AUTORANDR_SETUP,
            InputRole.AUTORANDR_LAYOUT,
        ),
    )
    overlay, overlay_artifact = _overlay_intent(inputs, resolved)
    scale = _ui_scale(resolved, inputs)
    panels = _panel_intents(inputs, scale)
    dpi = _dpi_intent(inputs, resolved)
    terminal, kitty_artifact = _terminal_intent(inputs, scale)
    emacs = EmacsFontIntent(
        expression="monitor-controller-apply-font-height",
        font_height=inputs.context.emacs_font_height,
        policy_hashes=configuration.hashes_for(InputRole.EMACS_POLICY),
    )

    expanded_layout_artifact = PlanArtifact(
        "artifacts/layout/expanded.yaml", resolved.expanded_yaml.encode("utf-8")
    )
    window_actions_artifact = PlanArtifact(
        "artifacts/layout/window-actions.json",
        _encode_window_actions(resolved),
    )
    windows = WindowLayoutIntent(
        source_artifact=expanded_layout_artifact.relative_path,
        source_sha256=expanded_layout_artifact.sha256,
        actions_artifact=window_actions_artifact.relative_path,
        actions_sha256=window_actions_artifact.sha256,
        action_count=len(resolved.window_actions),
    )

    template_content = _required_content(configuration.one(InputRole.FLUXBOX_TEMPLATE))
    template_artifact = PlanArtifact("artifacts/fluxbox/keys.erb", template_content)
    renderer_input = next(
        item
        for item in configuration.many(InputRole.FLUXBOX_GENERATOR)
        if item.path.endswith("/fluxbox_renderer.py")
    )
    generator_artifact = PlanArtifact(
        "artifacts/fluxbox/generator-policy",
        _required_content(renderer_input),
    )
    sublayouts_artifact = PlanArtifact(
        "artifacts/fluxbox/sublayouts.yaml", sublayouts_content
    )
    resolved_sublayouts_artifact = PlanArtifact(
        "artifacts/fluxbox/resolved-sublayouts.json",
        _canonical_json(
            [
                {
                    "name": name,
                    "per_current_screen": [list(commands) for commands in per_screen],
                }
                for name, per_screen in sublayouts
            ]
        ),
    )
    try:
        rendered_keys = render_fluxbox_keys(
            template_content,
            monitor_count=len(inputs.display.screens),
            host_name=inputs.context.host_name,
            template_label=".fluxbox/keys.erb",
            generator_label="bin/fluxbox-gen-config",
        )
    except FluxboxRenderError as error:
        raise DesktopPlanningError(str(error)) from error
    rendered_keys_artifact = PlanArtifact("artifacts/fluxbox/keys", rendered_keys)
    fluxbox = FluxboxGenerationIntent(
        template_artifact=template_artifact.relative_path,
        template_sha256=template_artifact.sha256,
        generator_artifact=generator_artifact.relative_path,
        generator_sha256=generator_artifact.sha256,
        sublayouts_artifact=sublayouts_artifact.relative_path,
        sublayouts_sha256=sublayouts_artifact.sha256,
        resolved_sublayouts_artifact=resolved_sublayouts_artifact.relative_path,
        resolved_sublayouts_sha256=resolved_sublayouts_artifact.sha256,
        rendered_keys_artifact=rendered_keys_artifact.relative_path,
        rendered_keys_sha256=rendered_keys_artifact.sha256,
        generated_keys_path=".fluxbox/keys",
        monitor_count=len(inputs.display.screens),
        host_name=inputs.context.host_name,
        resolved_variables=(
            ResolvedVariable("localhost_nickname", inputs.context.host_name),
            ResolvedVariable("monitors_connected", str(len(inputs.display.screens))),
        ),
        input_hashes=configuration.hashes_for(
            InputRole.FLUXBOX_TEMPLATE,
            InputRole.FLUXBOX_GENERATOR,
            InputRole.SUBLAYOUTS,
        ),
    )
    keyboard = _keyboard_intent(inputs)
    guards = TransitionGuards(
        action_id=request.action_id,
        transition_id=request.transition_id,
        input_key=request.input_key,
        physical_token=inputs.display.physical_token,
        admitted_event_generation=inputs.display.admitted_event_generation,
        observation_key=inputs.display.observation_key,
        profile=request.profile,
        layout=request.input_key.layout,
        output_mapping=request.input_key.mapping,
        topology=inputs.display.topology,
        display_screens=inputs.display.screens,
    )
    fluxbox_refs = (rendered_keys_artifact.relative_path,)
    prepare_refs = {
        PlannedActionKind.INSTALL_FLUXBOX_OVERLAY: (overlay_artifact.relative_path,),
        PlannedActionKind.CONFIGURE_TERMINALS: (kitty_artifact.relative_path,),
        PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION: fluxbox_refs,
    }
    finalize_refs = {
        PlannedActionKind.APPLY_FLUXBOX_CONFIGURATION: fluxbox_refs,
        PlannedActionKind.APPLY_WINDOW_LAYOUT: (window_actions_artifact.relative_path,),
    }
    prepare = tuple(
        PlannedAction(sequence, kind, prepare_refs.get(kind, ()))
        for sequence, kind in enumerate(_PREPARE_ACTIONS, start=1)
    )
    finalize = tuple(
        PlannedAction(sequence, kind, finalize_refs.get(kind, ()))
        for sequence, kind in enumerate(_FINALIZE_ACTIONS, start=1)
    )
    artifacts = (
        profile_config_artifact,
        profile_setup_artifact,
        *((profile_layout_artifact,) if profile_layout_artifact is not None else ()),
        expanded_layout_artifact,
        window_actions_artifact,
        overlay_artifact,
        template_artifact,
        generator_artifact,
        sublayouts_artifact,
        resolved_sublayouts_artifact,
        rendered_keys_artifact,
        kitty_artifact,
    )
    manifest = tuple(
        PlanArtifactManifestEntry(item.relative_path, item.sha256)
        for item in sorted(artifacts, key=lambda item: item.relative_path)
    )
    plan = DesktopPlan(
        guards=guards,
        resolved_layout=resolved,
        autorandr=autorandr,
        overlay=overlay,
        panels=panels,
        dpi=dpi,
        terminal=terminal,
        emacs=emacs,
        fluxbox=fluxbox,
        keyboard=keyboard,
        windows=windows,
        prepare_actions=prepare,
        finalize_actions=finalize,
        artifacts=manifest,
    )
    return with_artifacts(plan, artifacts)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _encode_window_actions(resolved: ResolvedLayout) -> bytes:
    return _canonical_json(
        [
            {
                "commands": list(action.commands),
                "map_command": action.map_command,
                "matcher": action.matcher,
            }
            for action in resolved.window_actions
        ]
    )


def _profile_evidence(item: ConfigurationInput) -> TextCommandEvidence:
    content = _required_content(item)
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DesktopPlanningError(
            f"autorandr profile input {item.path!r} is not UTF-8"
        ) from error
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        item.path,
        text,
    )


def _parse_captured_profile(
    name: str,
    config: ConfigurationInput,
    setup: ConfigurationInput,
    layout: ConfigurationInput,
) -> SavedAutorandrProfile:
    result = parse_saved_profile(
        name,
        _profile_evidence(config),
        _profile_evidence(setup),
        None if layout.content is None else _profile_evidence(layout),
    )
    if not result.valid or result.profile is None:
        reasons = ",".join(item.code.value for item in result.issues)
        raise DesktopPlanningError(f"captured autorandr profile is invalid: {reasons}")
    return result.profile


def _validate_autorandr_profile(
    inputs: DesktopPlanningInputs,
) -> SavedAutorandrProfile:
    configuration = inputs.configuration
    profile = _parse_captured_profile(
        inputs.request.profile,
        configuration.one(InputRole.AUTORANDR_CONFIG),
        configuration.one(InputRole.AUTORANDR_SETUP),
        configuration.one(InputRole.AUTORANDR_LAYOUT),
    )
    key = inputs.request.input_key
    if profile.layout != key.layout:
        raise DesktopPlanningError("autorandr layout differs from planning layout")
    saved_to_live = {item.saved_output: item.live_output for item in key.mapping}
    if set(saved_to_live) != {item.output for item in profile.setup}:
        raise DesktopPlanningError("autorandr setup differs from admitted mapping")
    active = {
        saved_to_live[item.output]
        for item in profile.config
        if item.output in saved_to_live and item.active
    }
    if active != set(inputs.display.topology.x_active_outputs):
        raise DesktopPlanningError(
            "autorandr active outputs differ from observed topology"
        )
    screens = {item.output: item for item in inputs.display.screens}
    for item in profile.config:
        live_output = saved_to_live.get(item.output)
        if live_output is None or not item.active:
            continue
        screen = screens[live_output]
        if item.mode is not None and item.mode != f"{screen.width}x{screen.height}":
            raise DesktopPlanningError("autorandr mode differs from observed geometry")
        position = next((value for name, value in item.options if name == "pos"), None)
        if position is not None and position != f"{screen.x}x{screen.y}":
            raise DesktopPlanningError(
                "autorandr position differs from observed geometry"
            )
        if item.primary != screen.primary:
            raise DesktopPlanningError(
                "autorandr primary differs from observed geometry"
            )
    return profile


def _overlay_intent(
    inputs: DesktopPlanningInputs,
    resolved: ResolvedLayout,
) -> tuple[OverlayIntent, PlanArtifact]:
    configuration = inputs.configuration
    layout_overlay = configuration.one(InputRole.LAYOUT_OVERLAY)
    host_overlay = configuration.one(InputRole.HOST_OVERLAY)
    # The host overlay is sized for this host's internal panel alone, so it
    # may only serve the bare host layout.  On a host whose nickname is also a
    # layout name (celtic), matching it for any layout captures every
    # multi-monitor layout that has no overlay file of its own and applies
    # laptop HiDPI fonts to an external monitor -- which is how the 139 DPI
    # ultrawide came to draw sans-16:bold window titles.
    #
    # For that one eligible layout both roles name the same file, so the
    # layout branch below already serves it and OverlaySelection.HOST is
    # never planned.  The role is still declared, and its hash still guards
    # the plan, so that editing overlay.<host> reliably invalidates it.
    host_overlay_is_eligible = resolved.layout == inputs.context.host_name
    if layout_overlay.content is not None:
        selection = OverlaySelection.LAYOUT
        source = layout_overlay.path
        content = layout_overlay.content
    elif host_overlay.content is not None and host_overlay_is_eligible:
        selection = OverlaySelection.HOST
        source = host_overlay.path
        content = host_overlay.content
    else:
        selection = OverlaySelection.DYNAMIC
        source = None
        content = dynamic_overlay(_ui_scale(resolved, inputs))
    artifact = PlanArtifact("artifacts/fluxbox/overlay", content)
    return (
        OverlayIntent(
            selection=selection,
            source_path=source,
            artifact_path=artifact.relative_path,
            content_sha256=artifact.sha256,
            candidate_hashes=configuration.hashes_for(
                InputRole.LAYOUT_OVERLAY, InputRole.HOST_OVERLAY
            ),
        ),
        artifact,
    )


# Matches the hand-written external-monitor overlays, which all settled on
# sans-12:bold; fluxbox sizes fonts in points against the monitor's real
# Xft.dpi, so a fixed base keeps the same physical size as monitors change.
_OVERLAY_BASE_FONT = Decimal(12)


def dynamic_overlay(scale: Decimal) -> bytes:
    """Generate the DPI-scaled fluxbox overlay for layouts with no file.

    The constants mirror setup_overlay() in bin/setup-monitor, which stays
    authoritative until cutover; the parity test
    test_dynamic_overlay_constants_match_setup_monitor fails when either
    side changes alone (dc-txr).
    """
    title_font = _rounded(_OVERLAY_BASE_FONT * scale)
    menu_font = _rounded((_OVERLAY_BASE_FONT + 1) * scale)
    title_height = 32 + (title_font - 12) * 2
    menu_height = 26 + (title_font - 12) * 2
    return (
        "window.borderWidth:               1\n"
        "window.handleWidth:               8\n"
        f"window.font:                      sans-{title_font}:bold\n"
        f"window.title.height:              {title_height}\n"
        f"menu.title.font:                  sans-{title_font}:bold\n"
        f"menu.frame.font:                  sans-{menu_font}\n"
        f"menu.titleHeight:                 {menu_height}\n"
        "menu.itemHeight:                  10\n"
    ).encode()


def _ui_scale(resolved: ResolvedLayout, inputs: DesktopPlanningInputs) -> Decimal:
    if resolved.ui_scale is not None:
        return Decimal(resolved.ui_scale)
    primary = _primary_screen(inputs.display.screens)
    if primary.width_mm > 0:
        physical_dpi = Decimal(primary.width) * Decimal("25.4") / primary.width_mm
        return physical_dpi / inputs.context.reference_dpi
    return Decimal(primary.width) / Decimal(2560)


def _panel_intents(
    inputs: DesktopPlanningInputs, scale: Decimal
) -> tuple[PanelIntent, ...]:
    ordered = tuple(
        sorted(inputs.display.screens, key=lambda item: (item.x, item.y, item.output))
    )
    primary = _primary_screen(ordered)
    other = tuple(item.output for item in ordered if item.output != primary.output)
    sizes: dict[int, int] = {}
    if inputs.context.is_laptop:
        if inputs.context.benq_connected:
            sizes = {1: 30, 2: 45, 3: 28}
        else:
            sizes[1] = max(24, min(40, _rounded(Decimal(36) * scale)))
            if other:
                sizes[2] = 36
    outputs = (
        "Primary",
        *(other[index] if index < len(other) else "none" for index in range(2)),
    )
    return tuple(
        PanelIntent(
            panel=index,
            output=output,
            position="p=8;x=0;y=0",
            length=100,
            size=sizes.get(index),
            policy_hashes=inputs.configuration.hashes_for(InputRole.PANEL_POLICY),
        )
        for index, output in enumerate(outputs, start=1)
    )


# Keyed by EDID vendor and product bytes, never by free-text model names:
# hwinfo and this planner render the same monitor differently ("SAMSUNG
# Odyssey G75F" vs "Samsung Odyssey G75F"), and a silently unmatched name
# falls through to physical-size DPI (dc-b2u). bin/set-layout-dpi keeps the
# name-keyed shell table until cutover; the parity test compares both per
# saved profile. BenQ BL3200 (84) is shell-only: no saved profile carries
# its EDID, so it cannot be keyed here until one is captured again.
EDID_DPI_OVERRIDES: Final = {
    "GSM:7707": 128,  # LG (GoldStar) HDR 4K, saved as Level39
    "AOC:2802": 128,  # AOC U28G2G6B
}


def _dpi_intent(inputs: DesktopPlanningInputs, resolved: ResolvedLayout) -> DpiIntent:
    policy_hashes = inputs.configuration.hashes_for(
        InputRole.CONTEXT,
        InputRole.AUTORANDR_CONFIG,
        InputRole.AUTORANDR_SETUP,
        InputRole.DPI_POLICY,
    )
    if resolved.dpi is not None:
        return DpiIntent(resolved.dpi, DpiSource.LAYOUT, policy_hashes)
    edid_model = inputs.context.primary_monitor_edid_model
    if edid_model in EDID_DPI_OVERRIDES:
        return DpiIntent(
            EDID_DPI_OVERRIDES[edid_model], DpiSource.MODEL_OVERRIDE, policy_hashes
        )
    primary = _primary_screen(inputs.display.screens)
    if primary.width_mm > 0:
        value = _decimal_floor(
            Decimal(primary.width) * Decimal("25.4") / primary.width_mm
        )
        if value > 0:
            return DpiIntent(value, DpiSource.PHYSICAL_SIZE, policy_hashes)
    if primary.width > 0:
        return DpiIntent(
            96 * primary.width // 2560, DpiSource.RESOLUTION, policy_hashes
        )
    if inputs.context.is_laptop:
        return DpiIntent(128, DpiSource.LAPTOP_FALLBACK, policy_hashes)
    return DpiIntent(None, DpiSource.UNCHANGED, policy_hashes)


def _terminal_intent(
    inputs: DesktopPlanningInputs, scale: Decimal
) -> tuple[TerminalThemeIntent, PlanArtifact]:
    host = inputs.context.host_name
    if host == "celtic":
        medium_size = _rounded(Decimal(14) * scale)
    elif host in {"ionian", "aegean"}:
        medium_size = 12
    else:
        raise DesktopPlanningError(
            f"terminal font policy does not support host {host!r}"
        )
    kitty = inputs.configuration.one(InputRole.KITTY_THEME)
    kitty_content = _required_content(kitty)
    artifact = PlanArtifact("artifacts/terminal/kitty-theme.conf", kitty_content)
    theme = inputs.context.theme
    return (
        TerminalThemeIntent(
            theme=theme,
            gnome_profile="Bright" if theme == "light" else "Dark",
            xfce_theme=theme,
            medium_font_name="SauceCodePro Nerd Font",
            medium_font_size=medium_size,
            kitty_theme_artifact=artifact.relative_path,
            kitty_theme_sha256=artifact.sha256,
            policy_hashes=inputs.configuration.hashes_for(
                InputRole.TERMINAL_POLICY,
                InputRole.FONT_POLICY,
                InputRole.KITTY_THEME,
            ),
        ),
        artifact,
    )


def _keyboard_intent(inputs: DesktopPlanningInputs) -> KeyboardIntent:
    if not inputs.context.is_laptop:
        disposition = KeyboardDisposition.UNCHANGED
        reason = "non-laptop host has no monitor-driven Advantage 360 intent"
    elif inputs.context.benq_connected:
        disposition = KeyboardDisposition.CONNECT_ADVANTAGE_360
        reason = "BenQ-connected laptop topology requests Advantage 360 connection"
    else:
        disposition = KeyboardDisposition.DISCONNECT_ADVANTAGE_360
        reason = "laptop topology without BenQ requests Advantage 360 disconnection"
    return KeyboardIntent(
        disposition=disposition,
        reason=reason,
        policy_hashes=inputs.configuration.hashes_for(InputRole.KEYBOARD_POLICY),
    )


def _required_content(item: ConfigurationInput) -> bytes:
    if item.content is None:
        raise DesktopPlanningError(
            "required configuration roles "
            f"{tuple(role.value for role in item.roles)!r} are absent"
        )
    return item.content


def _primary_screen(
    screens: tuple[DisplayScreenSnapshot, ...],
) -> DisplayScreenSnapshot:
    values = tuple(item for item in screens if item.primary)
    if len(values) != 1:
        raise DesktopPlanningError(
            "display snapshot requires exactly one primary screen"
        )
    return values[0]


def _rounded(value: Decimal) -> int:
    try:
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (ValueError, OverflowError) as error:
        raise DesktopPlanningError(
            "calculated desktop size is outside integer range"
        ) from error


def _decimal_floor(value: Decimal) -> int:
    try:
        return int(value)
    except (ValueError, OverflowError) as error:
        raise DesktopPlanningError(
            "calculated desktop value is outside integer range"
        ) from error


class _Digest(Protocol):
    def update(self, value: bytes, /) -> object:
        """Add one framed component to the digest."""
        ...


def _hash_configuration_component(digest: _Digest, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _open_configuration_root(path: Path) -> int:
    descriptor = _shared_open_absolute_directory(
        path,
        create=False,
        mode=0o700,
        reference="configuration root",
        error=DesktopPlanningError,
        validate=_validate_configuration_directory,
    )
    if descriptor < 0:
        raise DesktopPlanningError(
            "configuration root component cannot be safely opened"
        )
    return descriptor


def _validate_configuration_directory(descriptor: int) -> None:
    try:
        details = os.fstat(descriptor)
    except OSError as error:
        raise DesktopPlanningError("configuration directory is unavailable") from error
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_nlink < _MIN_DIRECTORY_LINK_COUNT
    ):
        raise DesktopPlanningError("configuration path contains a non-directory")


def _validate_configuration_file(details: os.stat_result, logical: str) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink < 1
        or details.st_size <= 0
        or details.st_size > MAX_CONFIGURATION_INPUT_BYTES
    ):
        raise DesktopPlanningError(
            f"configuration {logical!r} is not a bounded regular file"
        )

