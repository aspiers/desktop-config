# ruff: noqa: EM101, EM102, TRY003
"""Strict deterministic desktop-plan schema, codec, hash, and private store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, Protocol
from uuid import uuid4

from monitor_controller.codec import (
    StateCodecError,
    decode_schema_value,
    encode_schema_value,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    ConfigurationContentHash,
    EventGeneration,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    PlanningInputKey,
    TransitionId,
)
from monitor_controller.safeio import (
    DIRECTORY_OPEN_FLAGS as _DIRECTORY_OPEN_FLAGS,
)
from monitor_controller.safeio import SHA256_VALUE
from monitor_controller.safeio import (
    open_absolute_directory as _shared_open_absolute_directory,
)
from monitor_controller.safeio import (
    read_verified_file_at as _shared_read_verified_file_at,
)
from monitor_controller.safeio import (
    relative_regular_files_at as _shared_relative_regular_files_at,
)
from monitor_controller.safeio import (
    rename_noreplace_at as _shared_rename_noreplace_at,
)
from monitor_controller.safeio import (
    stable_file_details as _stable_file_details,
)
from monitor_controller.safeio import (
    validate_leaf_name as _shared_validate_leaf_name,
)
from monitor_controller.strictjson import strict_loads

from .layout import DisplayScreenSnapshot, ResolvedLayout  # noqa: TC001

PLAN_SCHEMA_VERSION: Final = 2
MAX_PLAN_BYTES: Final = 1024 * 1024
MAX_PLAN_ARTIFACT_BYTES: Final = 512 * 1024
MAX_PLAN_ARTIFACT_TOTAL_BYTES: Final = 2 * 1024 * 1024
MAX_PLAN_ARTIFACTS: Final = 64
MAX_PLAN_STRING_CHARS: Final = 65_536
_MIN_ARTIFACT_PATH_PARTS: Final = 2
_MAX_PATH_COMPONENT_CHARS: Final = 255
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_MIN_DIRECTORY_LINK_COUNT: Final = 2
_RENAME_NOREPLACE: Final = 1
_ARTIFACT_HASH_DOMAIN: Final = b"monitor-controller-plan-artifact-v1\x00"
_GENERATED_KEYS_PATH: Final = ".fluxbox/keys"
_EMACS_FONT_EXPRESSION: Final = "monitor-controller-apply-font-height"
_PANEL_POSITION: Final = "p=8;x=0;y=0"
_TERMINAL_FONT_NAME: Final = "SauceCodePro Nerd Font"
_SHA256 = SHA256_VALUE


class PlanCodecError(ValueError):
    """A staged desktop plan is malformed, inconsistent, or unsafe."""


class ImmutablePlanError(PlanCodecError):
    """A caller tried to replace already-published planning evidence."""


class OverlaySelection(StrEnum):
    """Reason one exact Fluxbox overlay content was selected."""

    LAYOUT = "layout"
    HOST = "host"
    DYNAMIC = "dynamic"


class DpiSource(StrEnum):
    """Deterministic precedence source for the planned XFCE DPI."""

    LAYOUT = "layout"
    MODEL_OVERRIDE = "model_override"
    PHYSICAL_SIZE = "physical_size"
    RESOLUTION = "resolution"
    LAPTOP_FALLBACK = "laptop_fallback"
    UNCHANGED = "unchanged"


class KeyboardDisposition(StrEnum):
    """Deferred external-keyboard connection intent."""

    CONNECT_ADVANTAGE_360 = "connect_advantage_360"
    DISCONNECT_ADVANTAGE_360 = "disconnect_advantage_360"
    UNCHANGED = "unchanged"


class PlannedActionKind(StrEnum):
    """Closed worker operation vocabulary; strings cannot add authority."""

    INSTALL_FLUXBOX_OVERLAY = "install_fluxbox_overlay"
    SET_PANEL_PROPERTIES = "set_panel_properties"
    SET_XFCE_DPI = "set_xfce_dpi"
    CONFIGURE_TERMINALS = "configure_terminal_fonts_and_theme"
    RELOAD_EMACS_FONTS = "reload_emacs_fonts"
    GENERATE_FLUXBOX_CONFIGURATION = "generate_fluxbox_configuration"
    APPLY_FLUXBOX_CONFIGURATION = "apply_fluxbox_configuration"
    APPLY_KEYBOARD_INTENT = "apply_keyboard_intent"
    APPLY_WINDOW_LAYOUT = "apply_window_layout"
    RESTART_FLUXBOX = "restart_fluxbox"
    RESTART_XFCE_PANEL = "restart_xfce_panel"
    RESTART_NM_APPLET = "restart_nm_applet_after_stable_tray"
    CAPTURE_TRAY_DIAGNOSTICS = "capture_tray_diagnostics"


_PREPARE_ACTION_KINDS: Final = (
    PlannedActionKind.INSTALL_FLUXBOX_OVERLAY,
    PlannedActionKind.SET_PANEL_PROPERTIES,
    PlannedActionKind.SET_XFCE_DPI,
    PlannedActionKind.CONFIGURE_TERMINALS,
    PlannedActionKind.RELOAD_EMACS_FONTS,
    PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION,
)
_FINALIZE_ACTION_KINDS: Final = (
    PlannedActionKind.APPLY_FLUXBOX_CONFIGURATION,
    PlannedActionKind.APPLY_KEYBOARD_INTENT,
    PlannedActionKind.RESTART_FLUXBOX,
    PlannedActionKind.APPLY_WINDOW_LAYOUT,
    PlannedActionKind.RESTART_XFCE_PANEL,
    PlannedActionKind.RESTART_NM_APPLET,
    PlannedActionKind.CAPTURE_TRAY_DIAGNOSTICS,
)


@dataclass(frozen=True, slots=True)
class PlannedTopology:
    """Exact immutable topology guarded by preparation and finalization."""

    kernel_connected_outputs: tuple[str, ...]
    kernel_external_outputs: tuple[str, ...]
    x_connected_outputs: tuple[str, ...]
    x_active_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field, values in (
            ("kernel connected", self.kernel_connected_outputs),
            ("kernel external", self.kernel_external_outputs),
            ("X connected", self.x_connected_outputs),
            ("X active", self.x_active_outputs),
        ):
            _sorted_unique_strings(values, f"plan {field} outputs")
        if not set(self.kernel_external_outputs) <= set(self.kernel_connected_outputs):
            raise PlanCodecError("plan external kernel outputs must be connected")
        if not set(self.x_active_outputs) <= set(self.x_connected_outputs):
            raise PlanCodecError("plan active X outputs must be connected")


@dataclass(frozen=True, slots=True)
class TransitionGuards:
    """All identity and topology evidence a staged plan is allowed to serve."""

    action_id: ActionId
    transition_id: TransitionId
    input_key: PlanningInputKey
    physical_token: PhysicalToken
    admitted_event_generation: EventGeneration
    observation_key: ObservationKey
    profile: str
    layout: str
    output_mapping: tuple[OutputMapping, ...]
    topology: PlannedTopology
    display_screens: tuple[DisplayScreenSnapshot, ...]

    def __post_init__(self) -> None:  # noqa: C901
        if self.action_id.kind is not ActionKind.PLAN:
            raise PlanCodecError("desktop plan guard action must be a planning action")
        if self.action_id.controller_instance != self.transition_id.controller_instance:
            raise PlanCodecError("plan action and transition instances differ")
        if self.input_key.physical_epoch < 0:
            raise PlanCodecError("plan physical epoch cannot be negative")
        if (
            self.profile != self.input_key.profile
            or self.layout != self.input_key.layout
        ):
            raise PlanCodecError("plan profile/layout differs from its input key")
        if self.observation_key != self.input_key.observation_key:
            raise PlanCodecError("plan observation differs from its input key")
        if self.output_mapping != self.input_key.mapping:
            raise PlanCodecError("plan mapping differs from its input key")
        if self.topology.x_active_outputs != self.input_key.active_outputs:
            raise PlanCodecError("plan active topology differs from its input key")
        screen_outputs = tuple(item.output for item in self.display_screens)
        if len(set(screen_outputs)) != len(screen_outputs):
            raise PlanCodecError("plan display snapshot repeats an output")
        if set(screen_outputs) != set(self.topology.x_active_outputs):
            raise PlanCodecError(
                "plan display geometry must equal exact active topology"
            )
        if {item.live_output for item in self.output_mapping} != set(
            self.topology.x_connected_outputs
        ):
            raise PlanCodecError("plan mapping must cover exact connected topology")


@dataclass(frozen=True, slots=True)
class AutorandrProfileIntent:
    """Exact captured profile bytes and parsed active-output semantics."""

    config_artifact: str
    config_sha256: str
    setup_artifact: str
    setup_sha256: str
    layout_artifact: str | None
    layout_sha256: str | None
    active_outputs: tuple[str, ...]
    primary_output: str
    input_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        for path, digest, field in (
            (self.config_artifact, self.config_sha256, "autorandr config"),
            (self.setup_artifact, self.setup_sha256, "autorandr setup"),
        ):
            _artifact_path(path)
            _sha256(digest, f"{field} hash")
        if (self.layout_artifact is None) is not (self.layout_sha256 is None):
            raise PlanCodecError("autorandr layout artifact/hash presence differs")
        if self.layout_artifact is not None and self.layout_sha256 is not None:
            _artifact_path(self.layout_artifact)
            _sha256(self.layout_sha256, "autorandr layout hash")
        _sorted_unique_strings(self.active_outputs, "autorandr active outputs")
        _text(self.primary_output, "autorandr primary output")
        if self.primary_output not in self.active_outputs:
            raise PlanCodecError("autorandr primary output must be active")
        _configuration_hashes(self.input_hashes, "autorandr profile input hashes")


@dataclass(frozen=True, slots=True)
class OverlayIntent:
    """Selected Fluxbox overlay and all candidate policy identities."""

    selection: OverlaySelection
    source_path: str | None
    artifact_path: str
    content_sha256: str
    candidate_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        _artifact_path(self.artifact_path)
        _sha256(self.content_sha256, "overlay content hash")
        _configuration_hashes(self.candidate_hashes, "overlay candidate hashes")
        if self.selection is OverlaySelection.DYNAMIC and self.source_path is not None:
            raise PlanCodecError("dynamic overlay cannot claim a source file")
        if self.selection is not OverlaySelection.DYNAMIC and self.source_path is None:
            raise PlanCodecError("selected overlay file requires a source path")


@dataclass(frozen=True, slots=True)
class PanelIntent:
    """One repeatable XFCE panel property set."""

    panel: int
    output: str
    position: str
    length: int
    size: int | None
    policy_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        if self.panel <= 0 or not self.output or self.output.isspace():
            raise PlanCodecError("panel intent has an invalid panel or output")
        if self.length <= 0 or (self.size is not None and self.size <= 0):
            raise PlanCodecError("panel length and optional size must be positive")
        if self.position != _PANEL_POSITION:
            raise PlanCodecError("panel position is outside the closed vocabulary")
        _configuration_hashes(self.policy_hashes, "panel policy hashes")


@dataclass(frozen=True, slots=True)
class DpiIntent:
    """Planned XFCE Xft DPI value, or an explicit unchanged decision."""

    value: int | None
    source: DpiSource
    policy_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        if self.value is not None and self.value <= 0:
            raise PlanCodecError("planned DPI must be positive")
        if (self.source is DpiSource.UNCHANGED) is not (self.value is None):
            raise PlanCodecError("unchanged DPI is the only intent without a value")
        _configuration_hashes(self.policy_hashes, "DPI policy hashes")


@dataclass(frozen=True, slots=True)
class TerminalThemeIntent:
    """Repeatable terminal font and colour intent from captured policies."""

    theme: str
    gnome_profile: str
    xfce_theme: str
    medium_font_name: str
    medium_font_size: int
    kitty_theme_artifact: str
    kitty_theme_sha256: str
    policy_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        for value, field in (
            (self.theme, "terminal theme"),
            (self.gnome_profile, "GNOME terminal profile"),
            (self.xfce_theme, "XFCE terminal theme"),
            (self.medium_font_name, "terminal font name"),
        ):
            _text(value, field)
        expected_profile = {"dark": "Dark", "light": "Bright"}.get(self.theme)
        if (
            expected_profile is None
            or self.gnome_profile != expected_profile
            or self.xfce_theme != self.theme
            or self.medium_font_name != _TERMINAL_FONT_NAME
        ):
            raise PlanCodecError("terminal intent is outside the closed vocabulary")
        if self.medium_font_size <= 0:
            raise PlanCodecError("terminal font size must be positive")
        _artifact_path(self.kitty_theme_artifact)
        _sha256(self.kitty_theme_sha256, "kitty theme hash")
        _configuration_hashes(self.policy_hashes, "terminal policy hashes")


@dataclass(frozen=True, slots=True)
class EmacsFontIntent:
    """Repeatable Emacs helper reload bound to an exact planned font height."""

    expression: str
    font_height: int
    policy_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        if self.expression != _EMACS_FONT_EXPRESSION:
            raise PlanCodecError("Emacs font function is outside the allowlist")
        if self.font_height <= 0:
            raise PlanCodecError("Emacs font height must be positive")
        _configuration_hashes(self.policy_hashes, "Emacs policy hashes")


@dataclass(frozen=True, slots=True)
class ResolvedVariable:
    """One exact renderer variable captured before planning."""

    name: str
    value: str

    def __post_init__(self) -> None:
        _text(self.name, "Fluxbox resolved variable name")
        _text(self.value, "Fluxbox resolved variable value")


@dataclass(frozen=True, slots=True)
class FluxboxGenerationIntent:
    """Complete deterministic renderer inputs with no live command discovery."""

    template_artifact: str
    template_sha256: str
    generator_artifact: str
    generator_sha256: str
    sublayouts_artifact: str
    sublayouts_sha256: str
    resolved_sublayouts_artifact: str
    resolved_sublayouts_sha256: str
    rendered_keys_artifact: str
    rendered_keys_sha256: str
    generated_keys_path: str
    monitor_count: int
    host_name: str
    resolved_variables: tuple[ResolvedVariable, ...]
    input_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        for path, digest, field in (
            (self.template_artifact, self.template_sha256, "Fluxbox template"),
            (self.generator_artifact, self.generator_sha256, "Fluxbox generator"),
            (self.sublayouts_artifact, self.sublayouts_sha256, "Fluxbox sublayouts"),
            (
                self.resolved_sublayouts_artifact,
                self.resolved_sublayouts_sha256,
                "resolved Fluxbox sublayouts",
            ),
            (
                self.rendered_keys_artifact,
                self.rendered_keys_sha256,
                "rendered Fluxbox keys",
            ),
        ):
            _artifact_path(path)
            _sha256(digest, f"{field} hash")
        if self.generated_keys_path != _GENERATED_KEYS_PATH:
            raise PlanCodecError("generated Fluxbox path is outside the allowlist")
        _text(self.host_name, "Fluxbox host name")
        if self.monitor_count <= 0:
            raise PlanCodecError("Fluxbox monitor count must be positive")
        names = tuple(item.name for item in self.resolved_variables)
        if names != tuple(sorted(set(names))):
            raise PlanCodecError("Fluxbox resolved variables must be sorted and unique")
        _configuration_hashes(self.input_hashes, "Fluxbox input hashes")


@dataclass(frozen=True, slots=True)
class KeyboardIntent:
    """Keyboard action deferred until disruptive finalization."""

    disposition: KeyboardDisposition
    reason: str
    policy_hashes: tuple[ConfigurationContentHash, ...]

    def __post_init__(self) -> None:
        _text(self.reason, "keyboard intent reason")
        _configuration_hashes(self.policy_hashes, "keyboard policy hashes")


@dataclass(frozen=True, slots=True)
class WindowLayoutIntent:
    """Exact parsed source and fully expanded Fluxbox map-command payloads."""

    source_artifact: str
    source_sha256: str
    actions_artifact: str
    actions_sha256: str
    action_count: int

    def __post_init__(self) -> None:
        _artifact_path(self.source_artifact)
        _sha256(self.source_sha256, "expanded layout source hash")
        _artifact_path(self.actions_artifact)
        _sha256(self.actions_sha256, "resolved window actions hash")
        if self.action_count <= 0:
            raise PlanCodecError("window layout intent requires exact actions")


@dataclass(frozen=True, slots=True)
class PlannedAction:
    """One closed ordered worker operation with exact artifact references."""

    sequence: int
    kind: PlannedActionKind
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise PlanCodecError("planned action sequence must be positive")
        if self.artifact_refs != tuple(sorted(set(self.artifact_refs))):
            raise PlanCodecError(
                "planned action artifact refs must be sorted and unique"
            )
        for value in self.artifact_refs:
            _artifact_path(value)

    @property
    def name(self) -> str:
        """Retain the diagnostic spelling used by trace/reporting callers."""
        return self.kind.value


@dataclass(frozen=True, slots=True)
class PlanArtifactManifestEntry:
    """One exact auxiliary artifact covered by the overall plan hash."""

    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        _artifact_path(self.relative_path)
        _sha256(self.sha256, "plan artifact hash")


@dataclass(frozen=True, slots=True)
class DesktopPlan:
    """Complete immutable desktop transition plan."""

    guards: TransitionGuards
    resolved_layout: ResolvedLayout
    autorandr: AutorandrProfileIntent
    overlay: OverlayIntent
    panels: tuple[PanelIntent, ...]
    dpi: DpiIntent
    terminal: TerminalThemeIntent
    emacs: EmacsFontIntent
    fluxbox: FluxboxGenerationIntent
    keyboard: KeyboardIntent
    windows: WindowLayoutIntent
    prepare_actions: tuple[PlannedAction, ...]
    finalize_actions: tuple[PlannedAction, ...]
    artifacts: tuple[PlanArtifactManifestEntry, ...]
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:  # noqa: C901, PLR0912 - cross-field schema
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise PlanCodecError("unsupported desktop plan schema version")
        if self.resolved_layout.layout != self.guards.layout:
            raise PlanCodecError("resolved layout differs from transition guard")
        if not self.panels:
            raise PlanCodecError("desktop plan must contain panel intents")
        _ordered_actions(self.prepare_actions, "prepare", _PREPARE_ACTION_KINDS)
        _ordered_actions(self.finalize_actions, "finalize", _FINALIZE_ACTION_KINDS)
        paths = tuple(item.relative_path for item in self.artifacts)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise PlanCodecError("plan artifact manifest must be sorted and unique")
        if self.windows.action_count != len(self.resolved_layout.window_actions):
            raise PlanCodecError("window artifact count differs from resolved layout")
        typed_paths = {
            self.autorandr.config_artifact: "artifacts/autorandr/config",
            self.autorandr.setup_artifact: "artifacts/autorandr/setup",
            self.overlay.artifact_path: "artifacts/fluxbox/overlay",
            self.terminal.kitty_theme_artifact: ("artifacts/terminal/kitty-theme.conf"),
            self.fluxbox.template_artifact: "artifacts/fluxbox/keys.erb",
            self.fluxbox.generator_artifact: "artifacts/fluxbox/generator-policy",
            self.fluxbox.sublayouts_artifact: "artifacts/fluxbox/sublayouts.yaml",
            self.fluxbox.resolved_sublayouts_artifact: (
                "artifacts/fluxbox/resolved-sublayouts.json"
            ),
            self.fluxbox.rendered_keys_artifact: (
                "artifacts/fluxbox/keys"  # gitleaks:allow
            ),
            self.windows.source_artifact: "artifacts/layout/expanded.yaml",
            self.windows.actions_artifact: "artifacts/layout/window-actions.json",
        }
        if self.autorandr.layout_artifact is not None:
            typed_paths[self.autorandr.layout_artifact] = "artifacts/autorandr/layout"
        if any(actual != expected for actual, expected in typed_paths.items()):
            raise PlanCodecError("typed intent artifact path is outside its allowlist")
        manifest = {item.relative_path: item.sha256 for item in self.artifacts}
        referenced_pairs = (
            (self.autorandr.config_artifact, self.autorandr.config_sha256),
            (self.autorandr.setup_artifact, self.autorandr.setup_sha256),
            (self.overlay.artifact_path, self.overlay.content_sha256),
            (self.terminal.kitty_theme_artifact, self.terminal.kitty_theme_sha256),
            (self.fluxbox.template_artifact, self.fluxbox.template_sha256),
            (self.fluxbox.generator_artifact, self.fluxbox.generator_sha256),
            (self.fluxbox.sublayouts_artifact, self.fluxbox.sublayouts_sha256),
            (
                self.fluxbox.resolved_sublayouts_artifact,
                self.fluxbox.resolved_sublayouts_sha256,
            ),
            (
                self.fluxbox.rendered_keys_artifact,
                self.fluxbox.rendered_keys_sha256,
            ),
            (self.windows.source_artifact, self.windows.source_sha256),
            (self.windows.actions_artifact, self.windows.actions_sha256),
            *(
                (
                    (
                        self.autorandr.layout_artifact,
                        self.autorandr.layout_sha256,
                    ),
                )
                if self.autorandr.layout_artifact is not None
                and self.autorandr.layout_sha256 is not None
                else ()
            ),
        )
        if len({path for path, _digest in referenced_pairs}) != len(referenced_pairs):
            raise PlanCodecError("typed plan intents must use distinct artifacts")
        referenced = {
            self.autorandr.config_artifact: self.autorandr.config_sha256,
            self.autorandr.setup_artifact: self.autorandr.setup_sha256,
            self.overlay.artifact_path: self.overlay.content_sha256,
            self.terminal.kitty_theme_artifact: self.terminal.kitty_theme_sha256,
            self.fluxbox.template_artifact: self.fluxbox.template_sha256,
            self.fluxbox.generator_artifact: self.fluxbox.generator_sha256,
            self.fluxbox.sublayouts_artifact: self.fluxbox.sublayouts_sha256,
            self.fluxbox.resolved_sublayouts_artifact: (
                self.fluxbox.resolved_sublayouts_sha256
            ),
            self.fluxbox.rendered_keys_artifact: self.fluxbox.rendered_keys_sha256,
            self.windows.source_artifact: self.windows.source_sha256,
            self.windows.actions_artifact: self.windows.actions_sha256,
        }
        if (
            self.autorandr.layout_artifact is not None
            and self.autorandr.layout_sha256 is not None
        ):
            referenced[self.autorandr.layout_artifact] = self.autorandr.layout_sha256
        if manifest != referenced:
            raise PlanCodecError(
                "every plan artifact must be referenced once with its exact hash"
            )
        action_refs = {
            item.kind: item.artifact_refs
            for item in (*self.prepare_actions, *self.finalize_actions)
        }
        expected_refs = {
            PlannedActionKind.INSTALL_FLUXBOX_OVERLAY: (self.overlay.artifact_path,),
            PlannedActionKind.CONFIGURE_TERMINALS: (
                self.terminal.kitty_theme_artifact,
            ),
            PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION: (
                self.fluxbox.rendered_keys_artifact,
            ),
            PlannedActionKind.APPLY_FLUXBOX_CONFIGURATION: (
                self.fluxbox.rendered_keys_artifact,
            ),
            PlannedActionKind.APPLY_WINDOW_LAYOUT: (self.windows.actions_artifact,),
        }
        if any(
            action_refs[kind] != expected_refs.get(kind, ()) for kind in action_refs
        ):
            raise PlanCodecError("planned action artifact allowlist is inconsistent")
        if self.autorandr.active_outputs != self.guards.topology.x_active_outputs:
            raise PlanCodecError(
                "autorandr active outputs differ from guarded topology"
            )
        primary_outputs = tuple(
            item.output for item in self.guards.display_screens if item.primary
        )
        if primary_outputs != (self.autorandr.primary_output,):
            raise PlanCodecError("autorandr primary differs from guarded display")
        if tuple(item.panel for item in self.panels) != tuple(
            range(1, len(self.panels) + 1)
        ):
            raise PlanCodecError("panel intents must be contiguous and ordered")


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    """One private file staged beside the canonical plan JSON."""

    relative_path: str
    content: bytes

    def __post_init__(self) -> None:
        _artifact_path(self.relative_path)
        if not self.content or len(self.content) > MAX_PLAN_ARTIFACT_BYTES:
            raise PlanCodecError("plan artifact is empty or exceeds its size limit")

    @property
    def sha256(self) -> str:
        """Return the canonical content digest for the manifest."""
        return _content_hash(self.content)


@dataclass(frozen=True, slots=True)
class DesktopPlanBundle:
    """Canonical plan plus all exact auxiliary artifact bytes."""

    plan: DesktopPlan
    artifacts: tuple[PlanArtifact, ...]

    def __post_init__(self) -> None:
        if len(self.artifacts) > MAX_PLAN_ARTIFACTS:
            raise PlanCodecError("plan has too many artifacts")
        paths = tuple(item.relative_path for item in self.artifacts)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise PlanCodecError("plan artifacts must be sorted and unique")
        if (
            sum(len(item.content) for item in self.artifacts)
            > MAX_PLAN_ARTIFACT_TOTAL_BYTES
        ):
            raise PlanCodecError("plan artifacts exceed the aggregate size limit")
        expected = tuple(
            PlanArtifactManifestEntry(item.relative_path, item.sha256)
            for item in self.artifacts
        )
        if self.plan.artifacts != expected:
            raise PlanCodecError("plan artifact bytes differ from their manifest")


class _HashWriter(Protocol):
    def update(self, value: bytes, /) -> object:
        """Add bytes to an incremental digest."""
        ...


class AtomicPlanStore:
    """FD-rooted no-replace plan publication with durable keyed revocation."""

    def __init__(self, root: Path, *, installation_fault: object | None = None) -> None:
        """Bind one canonical absolute root; opening is delayed until first use."""
        if not root.is_absolute():
            raise ValueError("plan staging root must be absolute")
        self._root = root
        self._root_fd = -1
        self._installation_fault = installation_fault

    @property
    def root(self) -> Path:
        """Return the diagnostic path; authority is always the retained root FD."""
        return self._root

    def close(self) -> None:
        """Release the retained root descriptor."""
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            self.close()

    def action_directory(self, action_id: ActionId) -> Path:
        """Return the diagnostic final path for one planning identity."""
        _plan_action(action_id)
        return self._root / action_id.value

    def revoke(self, action_id: ActionId) -> None:
        """Durably revoke publication authority before task cancellation."""
        _plan_action(action_id)
        root_fd = self._root_descriptor(create=True)
        marker = _revocation_name(action_id)
        payload = f"{action_id.value}\n".encode("ascii")
        try:
            _write_private_file_at(root_fd, marker, payload)
        except FileExistsError:
            if _read_private_file_at(root_fd, marker, 512) != payload:
                raise ImmutablePlanError("planning revocation marker changed") from None
        _sync_descriptor(root_fd)

    def is_revoked(self, action_id: ActionId) -> bool:
        """Return whether the retained namespace contains a durable revocation."""
        _plan_action(action_id)
        root_fd = self._root_descriptor(create=False)
        if root_fd < 0:
            return False
        try:
            _read_private_file_at(root_fd, _revocation_name(action_id), 512)
        except FileNotFoundError:
            return False
        return True

    def stage(  # noqa: C901, PLR0912, PLR0915 - atomic publication protocol
        self,
        action_id: ActionId,
        bundle: DesktopPlanBundle,
    ) -> PlanHash:
        """Publish a fully synced bundle with renameat2(RENAME_NOREPLACE)."""
        _plan_action(action_id)
        if bundle.plan.guards.action_id != action_id:
            raise PlanCodecError("plan bundle action differs from staging directory")
        if self.is_revoked(action_id):
            raise ImmutablePlanError("planning identity was revoked before publication")
        plan_hash = hash_plan_bundle(bundle)
        root_fd = self._root_descriptor(create=True)
        try:
            existing_fd = os.open(
                action_id.value, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd
            )
        except FileNotFoundError:
            existing_fd = -1
        except OSError as error:
            raise PlanCodecError("cannot safely inspect published plan") from error
        if existing_fd >= 0:
            os.close(existing_fd)
            existing = self.read(action_id)
            if existing != bundle or hash_plan_bundle(existing) != plan_hash:
                raise ImmutablePlanError("published planning identity is immutable")
            return plan_hash

        temporary_name = f".{action_id.value}.{uuid4().hex}.prepare"
        try:
            os.mkdir(temporary_name, _PRIVATE_DIRECTORY_MODE, dir_fd=root_fd)
            temporary_fd = os.open(
                temporary_name, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd
            )
        except OSError as error:
            raise PlanCodecError(
                "cannot create private temporary plan directory"
            ) from error
        published = False
        try:
            os.fchmod(temporary_fd, _PRIVATE_DIRECTORY_MODE)
            self._inject_fault("temporary_directory_created")
            _write_private_file_at(temporary_fd, "plan.json", encode_plan(bundle.plan))
            _write_private_file_at(
                temporary_fd, "plan.sha256", f"{plan_hash.value}\n".encode("ascii")
            )
            for artifact in bundle.artifacts:
                _write_relative_file_at(
                    temporary_fd, artifact.relative_path, artifact.content
                )
            self._inject_fault("bundle_written")
            _fsync_tree_at(temporary_fd)
            self._inject_fault("bundle_synced")
            if self.is_revoked(action_id):
                raise ImmutablePlanError(
                    "planning identity was revoked before publication"
                )
            if not _rename_noreplace_at(root_fd, temporary_name, action_id.value):
                _remove_directory_tree_at(root_fd, temporary_name, temporary_fd)
                temporary_fd = -1
                existing = self.read(action_id)
                if existing != bundle or hash_plan_bundle(existing) != plan_hash:
                    raise ImmutablePlanError("concurrent plan publication differs")
                return plan_hash
            published = True
            self._inject_fault("bundle_published")
            _sync_descriptor(root_fd)
            self._inject_fault("parent_synced")
            if self.is_revoked(action_id):
                self._discard_published(action_id, plan_hash)
                raise ImmutablePlanError(
                    "late plan publication rejected after revocation"
                )
        finally:
            if temporary_fd >= 0:
                if published:
                    os.close(temporary_fd)
                else:
                    _remove_directory_tree_at(root_fd, temporary_name, temporary_fd)
        return plan_hash

    def read(self, action_id: ActionId) -> DesktopPlanBundle:
        """Strictly read through retained parent FDs and reject revoked plans."""
        if self.is_revoked(action_id):
            raise ImmutablePlanError("planning identity is revoked")
        return self._read_published(action_id)

    def discard(self, action_id: ActionId, expected_hash: PlanHash | None) -> None:
        """Remove a revoked, validated plan via a detached FD-relative tombstone."""
        _plan_action(action_id)
        if not self.is_revoked(action_id):
            raise ImmutablePlanError("plan must be durably revoked before discard")
        self._discard_published(action_id, expected_hash)

    def _discard_published(
        self, action_id: ActionId, expected_hash: PlanHash | None
    ) -> None:
        root_fd = self._root_descriptor(create=False)
        if root_fd < 0:
            return
        try:
            bundle, validated_details = self._read_published_with_identity(action_id)
        except FileNotFoundError:
            return
        actual_hash = hash_plan_bundle(bundle)
        if expected_hash is not None and actual_hash != expected_hash:
            raise ImmutablePlanError("refusing to discard a different staged plan")
        source_fd = os.open(action_id.value, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd)
        source_details = _stable_file_details(os.fstat(source_fd))
        if source_details != validated_details:
            os.close(source_fd)
            raise ImmutablePlanError("published plan changed before discard")
        detached = f".{action_id.value}.{uuid4().hex}.discard"
        try:
            if not _rename_noreplace_at(root_fd, action_id.value, detached):
                raise ImmutablePlanError("discard tombstone unexpectedly exists")
            _sync_descriptor(root_fd)
            detached_details = os.stat(detached, dir_fd=root_fd, follow_symlinks=False)
            if _inode_details(os.fstat(source_fd)) != _inode_details(detached_details):
                raise ImmutablePlanError(
                    "detached plan inode differs from validated plan"
                )
            _remove_directory_tree_at(root_fd, detached, source_fd)
            source_fd = -1
        finally:
            if source_fd >= 0:
                os.close(source_fd)

    def _read_published(self, action_id: ActionId) -> DesktopPlanBundle:
        bundle, _details = self._read_published_with_identity(action_id)
        return bundle

    def _read_published_with_identity(
        self, action_id: ActionId
    ) -> tuple[DesktopPlanBundle, tuple[int, ...]]:
        _plan_action(action_id)
        root_fd = self._root_descriptor(create=False)
        if root_fd < 0:
            raise FileNotFoundError(action_id.value)
        try:
            directory_fd = os.open(
                action_id.value, _DIRECTORY_OPEN_FLAGS, dir_fd=root_fd
            )
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise
            raise PlanCodecError("cannot safely open plan action directory") from error
        try:
            _validate_private_directory_descriptor(
                directory_fd, "plan action directory"
            )
            before = _stable_file_details(os.fstat(directory_fd))
            plan = decode_plan(
                _read_private_file_at(directory_fd, "plan.json", MAX_PLAN_BYTES)
            )
            if plan.guards.action_id != action_id:
                raise PlanCodecError("staged plan path and action identity differ")
            artifacts: list[PlanArtifact] = []
            expected_files = {"plan.json", "plan.sha256"}
            for entry in plan.artifacts:
                content = _read_relative_file_at(
                    directory_fd, entry.relative_path, MAX_PLAN_ARTIFACT_BYTES
                )
                if _content_hash(content) != entry.sha256:
                    raise ImmutablePlanError("staged plan artifact content changed")
                artifacts.append(PlanArtifact(entry.relative_path, content))
                expected_files.add(entry.relative_path)
            if _relative_regular_files_at(directory_fd) != tuple(
                sorted(expected_files)
            ):
                raise PlanCodecError("staged plan directory has an unexpected file set")
            bundle = DesktopPlanBundle(plan, tuple(artifacts))
            stored_hash = (
                _read_private_file_at(directory_fd, "plan.sha256", 128)
                .decode("ascii")
                .strip()
            )
            _sha256(stored_hash, "stored plan hash")
            if hash_plan_bundle(bundle).value != stored_hash:
                raise ImmutablePlanError("staged plan content hash changed")
            _validate_private_directory_descriptor(
                directory_fd, "plan action directory"
            )
            after = _stable_file_details(os.fstat(directory_fd))
            if before != after:
                raise ImmutablePlanError("staged plan directory changed during read")
            return bundle, before
        finally:
            os.close(directory_fd)

    def _root_descriptor(self, *, create: bool) -> int:
        if self._root_fd >= 0:
            _validate_private_directory_descriptor(self._root_fd, "plan staging root")
            return self._root_fd
        descriptor = _open_absolute_directory(self._root, create=create)
        if descriptor < 0:
            return descriptor
        _validate_private_directory_descriptor(descriptor, "plan staging root")
        self._root_fd = descriptor
        return descriptor

    def _inject_fault(self, boundary: str) -> None:
        if self._installation_fault is None:
            return
        callback = self._installation_fault
        if not callable(callback):
            raise TypeError("plan installation fault must be callable")
        callback(boundary)


def with_artifacts(
    plan: DesktopPlan,
    artifacts: tuple[PlanArtifact, ...],
) -> DesktopPlanBundle:
    """Attach a sorted exact manifest to a plan and return its validated bundle."""
    ordered = tuple(sorted(artifacts, key=lambda item: item.relative_path))
    manifest = tuple(
        PlanArtifactManifestEntry(item.relative_path, item.sha256) for item in ordered
    )
    return DesktopPlanBundle(replace(plan, artifacts=manifest), ordered)


def encode_plan(plan: DesktopPlan) -> bytes:
    """Encode deterministic canonical JSON with no floats or implicit values."""
    try:
        document = encode_schema_value(plan)
        encoded = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (StateCodecError, TypeError, ValueError) as error:
        raise PlanCodecError(f"cannot encode desktop plan: {error}") from error
    if len(encoded) > MAX_PLAN_BYTES:
        raise PlanCodecError("desktop plan exceeds its encoded size limit")
    return encoded


def decode_plan(payload: bytes) -> DesktopPlan:
    """Strictly decode bounded JSON, rejecting duplicate and unknown fields."""
    if not payload or len(payload) > MAX_PLAN_BYTES:
        raise PlanCodecError("desktop plan is empty or exceeds its size limit")
    try:
        text = payload.decode("utf-8", errors="strict")
        document = strict_loads(text, PlanCodecError, reject_floats=True)
        decoded = decode_schema_value(document, DesktopPlan)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        StateCodecError,
        TypeError,
        ValueError,
    ) as error:
        if isinstance(error, PlanCodecError):
            raise
        raise PlanCodecError(f"cannot decode desktop plan: {error}") from error
    if not isinstance(decoded, DesktopPlan):
        raise PlanCodecError("decoded desktop plan has the wrong type")
    return decoded


def hash_plan_bundle(bundle: DesktopPlanBundle) -> PlanHash:
    """Hash canonical plan bytes and every path/content pair with length framing."""
    digest = hashlib.sha256()
    digest.update(b"monitor-controller-desktop-plan-v1\x00")
    _hash_component(digest, b"plan.json", encode_plan(bundle.plan))
    for artifact in bundle.artifacts:
        _hash_component(
            digest,
            artifact.relative_path.encode("utf-8"),
            artifact.content,
        )
    return PlanHash(f"sha256:{digest.hexdigest()}")


def _hash_component(digest: _HashWriter, path: bytes, content: bytes) -> None:
    digest.update(len(path).to_bytes(8, "big"))
    digest.update(path)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _content_hash(content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(_ARTIFACT_HASH_DOMAIN)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _text(value: str, field: str) -> None:
    if (
        not value
        or value.isspace()
        or len(value) > MAX_PLAN_STRING_CHARS
        or "\x00" in value
    ):
        raise PlanCodecError(f"{field} must be bounded non-empty text")


def _sha256(value: str, field: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise PlanCodecError(f"{field} must be a canonical SHA-256 digest")


def _configuration_hashes(
    values: tuple[ConfigurationContentHash, ...], field: str
) -> None:
    if not values:
        raise PlanCodecError(f"{field} must not be empty")
    keys = tuple(f"{item.path}\0{item.sha256}" for item in values)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise PlanCodecError(f"{field} must be sorted and unique")
    for item in values:
        _sha256(item.sha256, field)


def _ordered_actions(
    actions: tuple[PlannedAction, ...],
    phase: str,
    allowed: tuple[PlannedActionKind, ...],
) -> None:
    if tuple(item.sequence for item in actions) != tuple(range(1, len(actions) + 1)):
        raise PlanCodecError(f"{phase} action sequence is not contiguous")
    if tuple(item.kind for item in actions) != allowed:
        raise PlanCodecError(f"{phase} actions differ from the exact phase allowlist")


def _sorted_unique_strings(values: tuple[str, ...], field: str) -> None:
    if values != tuple(sorted(set(values))) or any(
        not item or item.isspace() for item in values
    ):
        raise PlanCodecError(f"{field} must be sorted, unique, and non-empty")


def _artifact_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.as_posix() != value
        or path.is_absolute()
        or len(path.parts) < _MIN_ARTIFACT_PATH_PARTS
        or path.parts[0] != "artifacts"
        or ".." in path.parts
        or any(not part or len(part) > _MAX_PATH_COMPONENT_CHARS for part in path.parts)
    ):
        raise PlanCodecError("plan artifact path is not canonical beneath artifacts/")


def _plan_action(action_id: ActionId) -> None:
    if action_id.kind is not ActionKind.PLAN:
        raise PlanCodecError("plan store accepts only planning action IDs")


def _revocation_name(action_id: ActionId) -> str:
    return f".{action_id.value}.revoked"


def _write_relative_file_at(root_fd: int, path: str, content: bytes) -> None:
    _artifact_path(path)
    parts = PurePosixPath(path).parts
    descriptor = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            _validate_leaf_name(component)
            try:
                child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                created = False
                try:
                    os.mkdir(component, _PRIVATE_DIRECTORY_MODE, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    created = False
                child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                if created:
                    os.fchmod(child, _PRIVATE_DIRECTORY_MODE)
                    _sync_descriptor(descriptor)
            _validate_private_directory_descriptor(child, "plan artifact parent")
            os.close(descriptor)
            descriptor = child
        _write_private_file_at(descriptor, parts[-1], content)
    finally:
        os.close(descriptor)


def _write_private_file_at(directory_fd: int, name: str, content: bytes) -> None:
    _validate_leaf_name(name)
    _validate_private_directory_descriptor(directory_fd, "plan parent directory")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, _PRIVATE_FILE_MODE, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while staging desktop plan")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_descriptor(directory_fd)


def _read_relative_file_at(directory_fd: int, path: str, maximum: int) -> bytes:
    _artifact_path(path)
    parts = PurePosixPath(path).parts
    descriptor = os.dup(directory_fd)
    try:
        for component in parts[:-1]:
            child = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            _validate_private_directory_descriptor(child, "plan artifact parent")
            os.close(descriptor)
            descriptor = child
        return _read_private_file_at(descriptor, parts[-1], maximum)
    finally:
        os.close(descriptor)


def _read_private_file_at(directory_fd: int, name: str, maximum: int) -> bytes:
    return _shared_read_verified_file_at(
        directory_fd,
        name,
        validate_file=lambda details: _validate_private_file_details(
            details, name, maximum
        ),
        validate_parent=lambda fd: _validate_private_directory_descriptor(
            fd, "plan parent directory"
        ),
        reference="staged plan file",
        error=PlanCodecError,
        changed_error=ImmutablePlanError,
    )


def _validate_private_file_details(
    details: os.stat_result, name: str, maximum: int
) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != _PRIVATE_FILE_MODE
        or details.st_nlink != 1
        or details.st_size <= 0
        or details.st_size > maximum
    ):
        raise PlanCodecError(f"staged plan file {name!r} has unsafe metadata")


def _validate_private_directory_descriptor(descriptor: int, field: str) -> None:
    try:
        details = os.fstat(descriptor)
    except OSError as error:
        raise PlanCodecError(f"{field} is unavailable") from error
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != _PRIVATE_DIRECTORY_MODE
        or details.st_nlink < _MIN_DIRECTORY_LINK_COUNT
    ):
        raise PlanCodecError(f"{field} is not a private retained directory")


def _inode_details(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
        details.st_nlink,
    )


def _relative_regular_files_at(root_fd: int) -> tuple[str, ...]:
    return tuple(
        sorted(
            _shared_relative_regular_files_at(
                root_fd,
                validate_directory=lambda fd: _validate_private_directory_descriptor(
                    fd, "plan tree directory"
                ),
                reference="staged plan tree",
                error=PlanCodecError,
            )
        )
    )


def _fsync_tree_at(root_fd: int) -> None:
    def walk(descriptor: int) -> None:
        _validate_private_directory_descriptor(descriptor, "plan tree directory")
        try:
            names = tuple(sorted(os.listdir(descriptor)))
        except OSError as error:
            raise PlanCodecError(
                "staged plan directory cannot be enumerated"
            ) from error
        for name in names:
            try:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise PlanCodecError("staged plan metadata cannot be read") from error
            if stat.S_ISDIR(details.st_mode):
                try:
                    child = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                except OSError as error:
                    raise PlanCodecError(
                        "staged plan directory cannot be opened"
                    ) from error
                try:
                    walk(child)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(details.st_mode):
                raise PlanCodecError("staged plan tree contains an unsafe entry")
        _sync_descriptor(descriptor)

    walk(root_fd)


def _remove_directory_tree_at(parent_fd: int, name: str, descriptor: int) -> None:
    _validate_leaf_name(name)
    _validate_private_directory_descriptor(descriptor, "plan cleanup directory")
    try:
        names = tuple(sorted(os.listdir(descriptor)))
    except OSError as error:
        raise PlanCodecError("plan cleanup directory cannot be enumerated") from error
    for child_name in names:
        _validate_leaf_name(child_name)
        try:
            details = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                child = os.open(child_name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                _remove_directory_tree_at(descriptor, child_name, child)
            elif stat.S_ISREG(details.st_mode):
                os.unlink(child_name, dir_fd=descriptor)
            else:
                raise PlanCodecError("plan cleanup encountered an unsafe entry")
        except OSError as error:
            raise PlanCodecError("plan cleanup entry cannot be removed") from error
    _sync_descriptor(descriptor)
    os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as error:
        raise PlanCodecError("plan cleanup directory cannot be removed") from error
    _sync_descriptor(parent_fd)


def _rename_noreplace_at(directory_fd: int, source: str, target: str) -> bool:
    _validate_leaf_name(source)
    _validate_leaf_name(target)
    _validate_private_directory_descriptor(directory_fd, "plan staging root")
    return _shared_rename_noreplace_at(
        directory_fd,
        source,
        target,
        "plan staging",
        PlanCodecError,
    )


def _open_absolute_directory(path: Path, *, create: bool) -> int:
    return _shared_open_absolute_directory(
        path,
        create=create,
        mode=_PRIVATE_DIRECTORY_MODE,
        reference="plan root",
        error=PlanCodecError,
    )


def _validate_leaf_name(name: str) -> None:
    _shared_validate_leaf_name(name, "plan path component", PlanCodecError)


def _sync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)
