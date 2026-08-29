"""Pure real-configuration desktop planning and private staging contracts."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from monitor_controller.desktop.fluxbox_renderer import (
    FluxboxRenderError,
    render_fluxbox_keys,
)
from monitor_controller.desktop.layout import (
    MAX_INCLUDE_DEPTH,
    MAX_LAYOUT_FILE_BYTES,
    MAX_LAYOUT_FILES,
    DisplayScreenSnapshot,
    LayoutPlanningError,
    ResolvedLayout,
    parse_layout,
    resolve_layout,
)
from monitor_controller.desktop.plan_codec import (
    PLAN_SCHEMA_VERSION,
    AtomicPlanStore,
    DesktopPlan,
    DpiSource,
    ImmutablePlanError,
    KeyboardDisposition,
    OverlaySelection,
    PlanArtifact,
    PlanCodecError,
    PlannedActionKind,
    PlannedTopology,
    decode_plan,
    encode_plan,
    hash_plan_bundle,
)
from monitor_controller.desktop.planner import (
    EDID_DPI_OVERRIDES,
    AtomicDesktopPlanningAdapter,
    ConfigurationInput,
    DesktopConfigurationSnapshot,
    DesktopDisplaySnapshot,
    DesktopPlanningError,
    FilesystemDesktopPlanningInputSource,
    InputRole,
    build_desktop_plan,
    derive_profile_monitor_identity,
    dynamic_overlay,
)
from monitor_controller.model import (
    ActionId,
    ActionKind,
    BootId,
    CanonicalObservation,
    ControllerInstanceId,
    DiscardPlan,
    DisplayIdentity,
    EventGeneration,
    EventMetadata,
    Fingerprint,
    ObservationCompleted,
    ObservationGeneration,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanningInputKey,
    ProfileMatch,
    ProfileScope,
    RequestPlan,
    State,
    TransitionId,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.runtime.controller import SerializedController
from monitor_controller.runtime.dispatcher import NullDispatcher
from monitor_controller.shadow import ShadowDesktopContextSource, load_saved_profiles

if TYPE_CHECKING:
    from monitor_controller.observer.autorandr import SavedAutorandrProfile

_REPO = next(
    parent for parent in Path(__file__).parents if (parent / ".fluxbox").is_dir()
)
_FIXTURES = Path(__file__).parent / "fixtures"
_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_LEGACY_GEOMETRY_FIELDS = (
    "active_height",
    "active_height_pc",
    "active_left",
    "active_middle_x",
    "active_middle_y",
    "active_top",
    "active_width",
    "active_width_pc",
    "bottom_margin",
    "col1_left",
    "col1_middle",
    "col1_right",
    "col1_width",
    "col2_left",
    "col2_middle",
    "col2_right",
    "col2_width",
    "col3_left",
    "col3_middle",
    "col3_right",
    "col3_width",
    "cols_1_2_margin",
    "cols_1_2_margin_pc_of_active",
    "cols_1_2_middle",
    "cols_1_2_width",
    "cols_2_3_margin",
    "cols_2_3_margin_pc_of_active",
    "full_height",
    "full_width",
    "head",
    "height",
    "left_margin",
    "logs_height",
    "panel_height",
    "right_margin",
    "row1_bottom",
    "row1_height",
    "row1_middle",
    "row1_top",
    "row2_bottom",
    "row2_height",
    "row2_middle",
    "row2_top",
    "rows_1_2_margin",
    "rows_1_2_margin_pc_of_active",
    "single_height",
    "single_left",
    "single_middle_x",
    "single_middle_y",
    "single_top",
    "single_width",
    "top_margin",
    "width",
    "x_offset",
    "y_offset",
)


def _joined_bytes(*parts: bytes) -> bytes:
    return b"".join(parts)


def _case(  # noqa: PLR0913
    *,
    sequence: int,
    profile: str,
    layout: str,
    external: str | None,
    external_size: tuple[int, int, int, int] | None,
    theme: str = "dark",
) -> tuple[FilesystemDesktopPlanningInputSource, RequestPlan]:
    observation_key = ObservationKey(f"real-{layout}-{theme}")
    if external is None:
        connected = ("eDP",)
        external_outputs: tuple[str, ...] = ()
        screens = (
            DisplayScreenSnapshot(
                output="eDP",
                width=2880,
                height=1920,
                x=0,
                y=0,
                width_mm=285,
                height_mm=190,
                primary=True,
            ),
        )
        mapping = (OutputMapping("eDP", "eDP"),)
    else:
        assert external_size is not None
        width, height, width_mm, height_mm = external_size
        connected = tuple(sorted((external, "eDP")))
        external_outputs = (external,)
        screens = (
            DisplayScreenSnapshot(
                output="eDP",
                width=2880,
                height=1920,
                x=0,
                y=21 if profile == "Level39" else 0,
                width_mm=285,
                height_mm=190,
                primary=False,
            ),
            DisplayScreenSnapshot(
                output=external,
                width=width,
                height=height,
                x=2880,
                y=0,
                width_mm=width_mm,
                height_mm=height_mm,
                primary=True,
            ),
        )
        mapping = tuple(
            sorted(
                (OutputMapping(external, external), OutputMapping("eDP", "eDP")),
                key=lambda item: (item.saved_output, item.live_output),
            )
        )
    display = DesktopDisplaySnapshot(
        physical_epoch=sequence,
        physical_token=PhysicalToken(f"physical-{layout}"),
        admitted_event_generation=EventGeneration(sequence),
        observation_key=observation_key,
        topology=PlannedTopology(
            kernel_connected_outputs=connected,
            kernel_external_outputs=external_outputs,
            x_connected_outputs=connected,
            x_active_outputs=connected,
        ),
        screens=screens,
    )
    source = FilesystemDesktopPlanningInputSource(
        root=_REPO,
        display=display,
        context=ShadowDesktopContextSource(host_name="celtic", theme=theme),
    )
    captured_profile = source.complete_profile(
        next(
            item
            for item in load_saved_profiles(_REPO / ".config" / "autorandr")
            if item.name == profile
        )
    )
    key = PlanningInputKey(
        physical_epoch=sequence,
        profile=profile,
        layout=layout,
        observation_key=observation_key,
        mapping=mapping,
        active_outputs=display.topology.x_active_outputs,
        configuration_hashes=captured_profile.configuration_hashes,
    )
    request = RequestPlan(
        ActionId(_INSTANCE, ActionKind.PLAN, sequence),
        TransitionId(_INSTANCE, sequence),
        key,
        profile,
    )
    return source, request


def _celtic() -> tuple[FilesystemDesktopPlanningInputSource, RequestPlan]:
    return _case(
        sequence=1,
        profile="celtic",
        layout="celtic",
        external=None,
        external_size=None,
    )


@pytest.mark.parametrize(
    (
        "profile",
        "layout",
        "external",
        "size",
        "model",
        "dpi",
        "dpi_source",
        "overlay",
        "col3",
    ),
    [
        (
            "celtic",
            "celtic",
            None,
            None,
            "BOE NE135A1M-NY1",
            128,
            DpiSource.LAYOUT,
            OverlaySelection.LAYOUT,
            0,
        ),
        (
            "celtic+AOC-U28G2G6B",
            "celtic+external",
            "DisplayPort-2",
            (3840, 2160, 600, 340),
            "AOC U28G2G6B",
            128,
            DpiSource.MODEL_OVERRIDE,
            OverlaySelection.LAYOUT,
            0,
        ),
        (
            "celtic+Samsung-Odyssey-G75F",
            "celtic+ultrawide",
            "DisplayPort-1",
            (5120, 2160, 930, 400),
            "Samsung Odyssey G75F",
            139,
            DpiSource.PHYSICAL_SIZE,
            # No .fluxbox/overlay.celtic+ultrawide exists, and the host overlay
            # is laptop-sized, so this layout must reach the DPI-aware
            # generator rather than inherit overlay.celtic.
            OverlaySelection.DYNAMIC,
            1976,
        ),
        (
            "Level39",
            "celtic+external",
            "DisplayPort-1",
            (3840, 2160, 600, 340),
            "LG (GoldStar) HDR 4K",
            128,
            DpiSource.MODEL_OVERRIDE,
            OverlaySelection.LAYOUT,
            0,
        ),
    ],
)
def test_real_saved_edids_drive_model_and_complete_plan(  # noqa: PLR0913, PLR0917
    profile: str,
    layout: str,
    external: str | None,
    size: tuple[int, int, int, int] | None,
    model: str,
    dpi: int,
    dpi_source: DpiSource,
    overlay: OverlaySelection,
    col3: int,
) -> None:
    source, request = _case(
        sequence=3,
        profile=profile,
        layout=layout,
        external=external,
        external_size=size,
    )

    captured = source.load(request)
    bundle = build_desktop_plan(captured)
    plan = bundle.plan

    assert plan.guards.input_key == captured.request.input_key
    assert plan.guards.input_key == request.input_key
    assert plan.guards.transition_id == request.transition_id
    assert plan.guards.physical_token.value == f"physical-{layout}"
    assert plan.resolved_layout.layout == layout
    assert plan.overlay.selection is overlay
    assert plan.resolved_layout.screens[-1].value("col3_width") == col3
    assert {item.panel for item in plan.panels} == {1, 2, 3}
    assert captured.context.primary_monitor_model == model
    assert plan.dpi.value == dpi
    assert plan.dpi.source is dpi_source
    assert captured.configuration.one(InputRole.CONTEXT).content_hash in (
        plan.dpi.policy_hashes
    )
    assert captured.configuration.one(InputRole.AUTORANDR_CONFIG).content_hash in (
        plan.dpi.policy_hashes
    )
    assert captured.configuration.one(InputRole.AUTORANDR_SETUP).content_hash in (
        plan.dpi.policy_hashes
    )
    assert plan.terminal.theme == "dark"
    assert plan.emacs.expression == "monitor-controller-apply-font-height"
    assert plan.fluxbox.monitor_count == len(plan.guards.display_screens)
    assert plan.keyboard.disposition is KeyboardDisposition.DISCONNECT_ADVANTAGE_360
    assert tuple(item.sequence for item in plan.prepare_actions) == tuple(
        range(1, len(plan.prepare_actions) + 1)
    )
    assert tuple(item.sequence for item in plan.finalize_actions) == tuple(
        range(1, len(plan.finalize_actions) + 1)
    )
    assert not {item.name for item in plan.prepare_actions} & {
        item.name for item in plan.finalize_actions
    }
    expected_artifacts = {
        "artifacts/autorandr/config",
        "artifacts/autorandr/setup",
        "artifacts/fluxbox/generator-policy",
        "artifacts/fluxbox/keys",
        "artifacts/fluxbox/keys.erb",
        "artifacts/fluxbox/overlay",
        "artifacts/fluxbox/resolved-sublayouts.json",
        "artifacts/fluxbox/sublayouts.yaml",
        "artifacts/layout/expanded.yaml",
        "artifacts/layout/window-actions.json",
        "artifacts/terminal/kitty-theme.conf",
    }
    if (_REPO / ".config" / "autorandr" / profile / "layout").exists():
        expected_artifacts.add("artifacts/autorandr/layout")
    assert {item.relative_path for item in bundle.artifacts} == expected_artifacts
    assert plan.resolved_layout.window_actions
    assert all(
        "<s_" not in item.map_command for item in plan.resolved_layout.window_actions
    )
    rendered_keys = next(
        item.content
        for item in bundle.artifacts
        if item.relative_path == "artifacts/fluxbox/keys"
    )
    assert b"<%" not in rendered_keys
    assert b"%x(" not in rendered_keys
    monitor_comment = (
        f"# Number of monitors connected: {len(plan.guards.display_screens)}".encode()
    )
    assert monitor_comment in rendered_keys


def test_host_overlay_serves_only_the_bare_host_layout() -> None:
    """A multi-monitor layout must not inherit the laptop's host overlay.

    ``.fluxbox/overlay.celtic`` is sized for the internal 2880x1920 panel.
    Because the host nickname is also a layout name, an unqualified host
    match would apply those HiDPI fonts to every layout lacking its own
    overlay file, which is how the 139 DPI ultrawide ended up drawing
    ``sans-16:bold`` window titles.
    """
    bare_source, bare_request = _celtic()
    bare_plan = build_desktop_plan(bare_source.load(bare_request)).plan
    # For the bare host layout the two roles name the same file, so the
    # layout branch claims it first; either way overlay.celtic is what the
    # laptop-only layout must still receive.
    assert bare_plan.overlay.selection is OverlaySelection.LAYOUT
    assert bare_plan.overlay.source_path == ".fluxbox/overlay.celtic"

    source, request = _case(
        sequence=4,
        profile="celtic+Samsung-Odyssey-G75F",
        layout="celtic+ultrawide",
        external="DisplayPort-1",
        external_size=(5120, 2160, 930, 400),
    )
    plan = build_desktop_plan(source.load(request)).plan
    assert plan.overlay.selection is OverlaySelection.DYNAMIC
    assert plan.overlay.source_path is None

    overlay = next(
        item.content
        for item in build_desktop_plan(source.load(request)).artifacts
        if item.relative_path == "artifacts/fluxbox/overlay"
    )
    assert b"sans-16:bold" not in overlay


def _profile_with_setup_value(
    profile: str, output: str, value: str
) -> SavedAutorandrProfile:
    saved = next(
        item
        for item in load_saved_profiles(_REPO / ".config" / "autorandr")
        if item.name == profile
    )
    setup = tuple(
        Fingerprint(item.output, value) if item.output == output else item
        for item in saved.setup
    )
    return replace(saved, setup=setup)


def test_model_identity_fails_closed_when_wildcard_obscures_edid_base() -> None:
    saved = next(
        item
        for item in load_saved_profiles(_REPO / ".config" / "autorandr")
        if item.name == "celtic"
    )
    fingerprint = saved.setup[0].value
    obscured = f"{fingerprint[:100]}*{fingerprint[102:]}"

    with pytest.raises(DesktopPlanningError, match="wildcard obscures"):
        derive_profile_monitor_identity(
            _profile_with_setup_value("celtic", "eDP", obscured)
        )


def test_model_identity_rejects_malformed_and_ambiguous_base_descriptors() -> None:
    with pytest.raises(DesktopPlanningError, match="EDID base is malformed"):
        derive_profile_monitor_identity(
            _profile_with_setup_value("celtic", "eDP", "00" * 128)
        )

    saved = next(
        item
        for item in load_saved_profiles(_REPO / ".config" / "autorandr")
        if item.name == "celtic"
    )
    fingerprint = saved.setup[0].value
    base = bytearray.fromhex(fingerprint[:256])
    duplicate_name = b"\x00\x00\x00\xfc\x00DUPLICATE\n   "
    assert len(duplicate_name) == 18
    base[54:72] = duplicate_name
    base[127] = (-sum(base[:127])) % 256
    ambiguous = f"{base.hex()}{fingerprint[256:]}"

    with pytest.raises(DesktopPlanningError, match="missing or ambiguous"):
        derive_profile_monitor_identity(
            _profile_with_setup_value("celtic", "eDP", ambiguous)
        )


def test_primary_model_identity_is_bound_to_saved_to_live_mapping() -> None:
    source, request = _case(
        sequence=4,
        profile="Level39",
        layout="celtic+external",
        external="DisplayPort-1",
        external_size=(3840, 2160, 600, 340),
    )
    swapped = tuple(
        sorted(
            (
                OutputMapping("DisplayPort-1", "eDP"),
                OutputMapping("eDP", "DisplayPort-1"),
            ),
            key=lambda item: (item.saved_output, item.live_output),
        )
    )
    bad_request = replace(
        request,
        input_key=replace(request.input_key, mapping=swapped),
    )

    with pytest.raises(DesktopPlanningError, match="does not map"):
        source.load(bad_request)


def _render_fluxbox_expression(template: bytes, expression: str) -> bytes:
    prelude_end = template.index(b"%>") + len(b"%>")
    source = template[:prelude_end] + f"\n<%= {expression} %>\n".encode()
    return render_fluxbox_keys(
        source,
        monitor_count=2,
        host_name="celtic",
        template_label=".fluxbox/keys.erb",
        generator_label="bin/fluxbox-gen-config",
    )


_ERB_WARNING_HEADER = (
    b"# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    b"# WARNING: This file is auto-generated. DO NOT EDIT IT MANUALLY.\n"
    b"# Edit the template instead:\n"
    b"#   .fluxbox/keys.erb\n"
    b"# Then regenerate by running:\n"
    b"#   bin/fluxbox-gen-config\n"
    b"# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
)


def _live_erb_ground_truth(monitor_count: int, tmp_path: Path) -> bytes | None:
    """Render the live template with real Ruby erb, or None without ruby.

    Mirrors fixtures/fluxbox/regenerate: fake monitors-connected on PATH and
    the celtic nickname, prefixed with the legacy warning header.
    """
    erb = shutil.which("erb")
    if erb is None:
        return None
    fake_bin = tmp_path / f"erb-bin-{monitor_count}"
    fake_bin.mkdir()
    stub = fake_bin / "monitors-connected"
    stub.write_text(f"#!/bin/sh\necho {monitor_count}\n", encoding="ascii")
    stub.chmod(0o755)
    completed = subprocess.run(  # noqa: S603 - fixed argv over tracked input
        [erb, str(_REPO / ".fluxbox" / "keys.erb")],
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "localhost_nickname": "celtic",
        },
        timeout=30,
    )
    return _ERB_WARNING_HEADER + completed.stdout


def test_closed_fluxbox_renderer_matches_legacy_erb_golden_bytes(
    tmp_path: Path,
) -> None:
    """The Python renderer must match Ruby erb over the live template.

    With ruby installed the ground truth is rendered fresh, so keys.erb edits
    cannot leave this red on fixture staleness alone; without ruby the
    checked-in goldens stand in and fixtures/fluxbox/regenerate refreshes
    them.
    """
    template = (_REPO / ".fluxbox" / "keys.erb").read_bytes()
    golden_root = _FIXTURES / "fluxbox"
    expected_hashes = {
        name: digest
        for digest, name in (
            line.split("  ", maxsplit=1)
            for line in golden_root.joinpath("SHA256SUMS")
            .read_text(encoding="ascii")
            .splitlines()
        )
    }

    for monitor_count in (1, 2, 3):
        name = f"keys-{monitor_count}.golden"
        ground_truth = _live_erb_ground_truth(monitor_count, tmp_path)
        source = "live erb output"
        if ground_truth is None:
            ground_truth = golden_root.joinpath(name).read_bytes()
            source = name
            digest = hashlib.sha256(ground_truth).hexdigest()
            assert digest == expected_hashes[name]
        rendered = render_fluxbox_keys(
            template,
            monitor_count=monitor_count,
            host_name="celtic",
            template_label=".fluxbox/keys.erb",
            generator_label="bin/fluxbox-gen-config",
        )
        assert rendered == ground_truth, (
            f"renderer disagrees with {source} for {monitor_count} monitor(s); "
            "if .fluxbox/keys.erb changed, refresh the fixtures with "
            "fixtures/fluxbox/regenerate"
        )


def test_closed_fluxbox_renderer_rejects_unknown_execution() -> None:
    template = (_REPO / ".fluxbox" / "keys.erb").read_bytes()
    with pytest.raises(FluxboxRenderError, match="unknown Ruby expression"):
        _render_fluxbox_expression(template, "system('touch /tmp/pwned')")


@pytest.mark.parametrize(
    "payload",
    [
        "$(touch /tmp/pwned)",
        "bad'quote",
        'bad"quote',
        r"bad\\escape",
        "bad\nline",
        "bad;command",
        "bad`command`",
        "bad&command",
        "bad{brace}",
    ],
)
@pytest.mark.parametrize(
    "expression",
    [
        "notify '{payload}'",
        "keymode '{payload}'",
        "keymode 'reorg', '{payload}'",
        "keymode_done '{payload}'",
        "notify_transient '{payload}', 'layer'",
        "notify_transient 'set to top layer', '{payload}'",
        "delay('{payload}', 500)",
        "delay(notify('{payload}'), 500)",
        'next_unhidden "{payload}"',
    ],
)
def test_closed_fluxbox_renderer_rejects_unsafe_helper_text_arguments(
    expression: str,
    payload: str,
) -> None:
    template = (_REPO / ".fluxbox" / "keys.erb").read_bytes()
    with pytest.raises(FluxboxRenderError):
        _render_fluxbox_expression(template, expression.replace("{payload}", payload))


@pytest.mark.parametrize(
    "expression",
    [
        "delay('Restart', 500; system('id'))",
        "delay('Restart', 6000000)",
        "delay(notify('Restarted fluxbox'), 500\n)",
        'next_unhidden "(Class=Emacs)", evil: true',
        'next_unhidden "(Class=Emacs)", focus: true; system("id")',
    ],
)
def test_closed_fluxbox_renderer_rejects_unsafe_numeric_and_keyword_arguments(
    expression: str,
) -> None:
    template = (_REPO / ".fluxbox" / "keys.erb").read_bytes()
    with pytest.raises(FluxboxRenderError):
        _render_fluxbox_expression(template, expression)


@pytest.mark.parametrize(
    "expression",
    [
        "delay('Restart', 501)",
        "delay(notify('Restarted fluxbox'), 501)",
        'next_unhidden "(Class=Emacs)", focus: false',
        'next_unhidden "(Class=Emacs)", prev: true',
        'next_unhidden "(Class=Emacs)", native: true',
    ],
)
def test_closed_fluxbox_renderer_accepts_new_wellformed_helper_arguments(
    expression: str,
) -> None:
    """Well-formed helper calls need no Python edit alongside a keys.erb edit.

    Exact argument values used to be pinned in per-helper frozensets, so
    adding one keybinding meant editing the template and the renderer's
    allowlists together (dc-mmk). The character-level grammar plus the
    live-erb parity test replace that double bookkeeping.
    """
    template = (_REPO / ".fluxbox" / "keys.erb").read_bytes()
    assert _render_fluxbox_expression(template, expression)


def test_identical_inputs_have_identical_canonical_bytes_and_hash() -> None:
    source, request = _celtic()

    first = build_desktop_plan(source.load(request))
    second = build_desktop_plan(source.load(request))

    assert first == second
    assert encode_plan(first.plan) == encode_plan(second.plan)
    assert hash_plan_bundle(first) == hash_plan_bundle(second)
    assert decode_plan(encode_plan(first.plan)) == first.plan


def _materialize_snapshot(root: Path, snapshot: DesktopConfigurationSnapshot) -> None:
    for item in snapshot.inputs:
        if item.content is None or InputRole.CONTEXT in item.roles:
            continue
        destination = root / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)


def _request_from_production_profile_capture(
    template: RequestPlan,
    source: FilesystemDesktopPlanningInputSource,
    root: Path,
) -> RequestPlan:
    profile = source.complete_profile(
        next(
            item
            for item in load_saved_profiles(root / ".config" / "autorandr")
            if item.name == template.profile
        )
    )
    admitted = PlanningInputKey(
        physical_epoch=template.input_key.physical_epoch,
        profile=profile.name,
        layout=profile.layout,
        observation_key=template.input_key.observation_key,
        mapping=template.input_key.mapping,
        active_outputs=template.input_key.active_outputs,
        configuration_hashes=profile.configuration_hashes,
    )
    return replace(template, input_key=admitted)


def _semantic_projection(desktop: DesktopPlan, roles: tuple[InputRole, ...]) -> object:
    values: list[object] = []
    role_set = set(roles)
    if role_set & {
        InputRole.AUTORANDR_CONFIG,
        InputRole.AUTORANDR_SETUP,
        InputRole.AUTORANDR_LAYOUT,
    }:
        values.append(desktop.autorandr)
    if role_set & {InputRole.MAIN_LAYOUT, InputRole.LAYOUT_INCLUDE}:
        values.append(desktop.resolved_layout)
    if role_set & {InputRole.LAYOUT_OVERLAY, InputRole.HOST_OVERLAY}:
        values.append(desktop.overlay)
    if InputRole.PANEL_POLICY in role_set:
        values.append(desktop.panels)
    if InputRole.DPI_POLICY in role_set:
        values.append(desktop.dpi)
    if role_set & {
        InputRole.FONT_POLICY,
        InputRole.TERMINAL_POLICY,
        InputRole.KITTY_THEME,
    }:
        values.append(desktop.terminal)
    if role_set & {
        InputRole.FLUXBOX_TEMPLATE,
        InputRole.FLUXBOX_GENERATOR,
        InputRole.SUBLAYOUTS,
    }:
        values.append(desktop.fluxbox)
    if InputRole.KEYBOARD_POLICY in role_set:
        values.append(desktop.keyboard)
    if InputRole.EMACS_POLICY in role_set:
        values.append(desktop.emacs)
    assert values, roles
    return tuple(values)


def test_every_consumed_real_configuration_changes_its_semantic_intent(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    original_inputs = source.load(request)
    original = build_desktop_plan(original_inputs)
    mutable = tuple(
        item
        for item in original_inputs.configuration.inputs
        if item.roles != (InputRole.CONTEXT,)
    )
    assert mutable

    for index, target in enumerate(mutable):
        root = tmp_path / str(index)
        _materialize_snapshot(root, original_inputs.configuration)
        destination = root / target.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            (
                b"celtic\n"
                if InputRole.AUTORANDR_LAYOUT in target.roles
                else b"# newly present\n"
            )
            if target.content is None
            else target.content + b"\n"
        )
        changed_source = FilesystemDesktopPlanningInputSource(
            root=root,
            display=original_inputs.display,
            context=original_inputs.context,
        )
        changed_request = _request_from_production_profile_capture(
            request, changed_source, root
        )
        changed_inputs = changed_source.load(changed_request)
        changed = build_desktop_plan(changed_inputs)

        assert changed_inputs.request.input_key != original_inputs.request.input_key
        assert _semantic_projection(changed.plan, target.roles) != _semantic_projection(
            original.plan, target.roles
        ), target.path
        assert hash_plan_bundle(changed) != hash_plan_bundle(original), target.path
        changed_source.close()


def test_theme_changes_are_full_manifest_keyed_and_bad_geometry_fails_closed() -> None:
    source, request = _celtic()
    baseline_inputs = source.load(request)
    baseline = build_desktop_plan(baseline_inputs)
    light_source, light_request = _case(
        sequence=1,
        profile="celtic",
        layout="celtic",
        external=None,
        external_size=None,
        theme="light",
    )
    light_inputs = light_source.load(light_request)
    light = build_desktop_plan(light_inputs)
    assert light_inputs.request.input_key != baseline_inputs.request.input_key
    assert light.plan.terminal.gnome_profile == "Bright"
    assert hash_plan_bundle(light) != hash_plan_bundle(baseline)

    display = replace(
        baseline_inputs.display,
        screens=(
            DisplayScreenSnapshot(
                output="eDP",
                width=3000,
                height=2000,
                x=0,
                y=0,
                width_mm=285,
                height_mm=190,
                primary=True,
            ),
        ),
    )
    moved_source = FilesystemDesktopPlanningInputSource(
        root=_REPO,
        display=display,
        context=baseline_inputs.context,
    )
    with pytest.raises(DesktopPlanningError, match="mode differs"):
        build_desktop_plan(moved_source.load(request))


def test_pure_builder_performs_no_filesystem_shell_home_or_x_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, request = _celtic()
    captured = source.load(request)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        message = "pure planner attempted I/O"
        raise AssertionError(message)

    class ForbiddenEnvironment:
        def get(self, *_args: object, **_kwargs: object) -> object:
            return forbidden()

        def __getitem__(self, _key: str) -> object:
            return forbidden()

    with monkeypatch.context() as patch:
        for owner, name in (
            (os, "open"),
            (os, "getenv"),
            (os, "stat"),
            (os, "listdir"),
            (builtins, "open"),
            (Path, "open"),
            (Path, "read_bytes"),
            (Path, "read_text"),
            (Path, "home"),
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
        ):
            patch.setattr(owner, name, forbidden)
        patch.setattr(os, "environ", ForbiddenEnvironment())
        bundle = build_desktop_plan(captured)

    assert bundle.plan.guards.profile == "celtic"


def test_plan_codec_rejects_unknown_duplicate_and_tampered_schema() -> None:
    source, request = _celtic()
    encoded = encode_plan(build_desktop_plan(source.load(request)).plan)
    raw = json.loads(encoded)
    raw["authority"] = True
    with pytest.raises(PlanCodecError):
        decode_plan(json.dumps(raw).encode())

    version_field = f'"schema_version":{PLAN_SCHEMA_VERSION}'.encode()
    duplicate = encoded.replace(
        version_field,
        version_field + b"," + version_field,
    )
    with pytest.raises(PlanCodecError, match="duplicate"):
        decode_plan(duplicate)

    raw = json.loads(encoded)
    raw["schema_version"] = PLAN_SCHEMA_VERSION - 1
    with pytest.raises(PlanCodecError, match="schema"):
        decode_plan(json.dumps(raw).encode())


def test_atomic_store_publishes_private_idempotent_exact_bundle(tmp_path: Path) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    store = AtomicPlanStore(tmp_path / "runtime" / "plans")

    first = store.stage(request.action_id, bundle)
    second = store.stage(request.action_id, bundle)

    assert first == second == hash_plan_bundle(bundle)
    assert store.read(request.action_id) == bundle
    directory = store.action_directory(request.action_id)
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) in {0o600, 0o700}
        for path in directory.rglob("*")
    )
    store.revoke(request.action_id)
    with pytest.raises(ImmutablePlanError, match="revoked"):
        store.read(request.action_id)
    store.discard(request.action_id, first)
    assert not directory.exists()


def test_injected_adapter_stages_and_discards_exact_request(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    store = AtomicPlanStore(tmp_path / "runtime" / "plans")
    adapter = AtomicDesktopPlanningAdapter(source, store)

    async def exercise() -> None:
        completed = await adapter.create_plan(request)
        assert completed.plan_hash == hash_plan_bundle(store.read(request.action_id))
        assert completed.input_key == request.input_key
        discard = DiscardPlan(request.action_id, completed.plan_hash)
        await adapter.revoke_plan(discard)
        await adapter.discard_plan(discard)
        assert not store.action_directory(request.action_id).exists()

    asyncio.run(exercise())


def test_all_tracked_fluxbox_layouts_and_recursive_includes_parse() -> None:
    root = _REPO / ".fluxbox" / "layouts"
    files = tuple(
        (path.relative_to(_REPO).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*.yaml"))
    )
    layouts = tuple(sorted(root.glob("*.yaml")))
    assert layouts

    for path in layouts:
        parsed = parse_layout(path.stem, files)
        primary = next(
            (
                index
                for index, screen in enumerate(parsed.screens)
                if screen.assignment == "primary"
            ),
            0,
        )
        screens = tuple(
            DisplayScreenSnapshot(
                output=f"OUT-{index}",
                width=2000 + index * 100,
                height=1200,
                x=index * 2200,
                y=0,
                width_mm=500,
                height_mm=300,
                primary=index == primary,
            )
            for index in range(len(parsed.screens))
        )
        resolved = resolve_layout(parsed, screens)
        assert len(resolved.screens) == len(parsed.screens)
        assert resolved.window_rule_count > 0
        assert path.relative_to(_REPO).as_posix() in resolved.consumed_paths


def test_layout_parser_rejects_missing_and_cyclic_includes() -> None:
    main = (
        b"screens:\n  -\n    name: one\n    head: 1\nwindows:\n  <INCLUDE common/a>\n"
    )
    with pytest.raises(LayoutPlanningError, match="missing injected"):
        parse_layout("test", ((".fluxbox/layouts/test.yaml", main),))

    cyclic = (
        (".fluxbox/layouts/test.yaml", main),
        (".fluxbox/layouts/common/a.yaml", b"<INCLUDE common/b>\n"),
        (".fluxbox/layouts/common/b.yaml", b"<INCLUDE common/a>\n"),
    )
    with pytest.raises(LayoutPlanningError, match="cycle"):
        parse_layout("test", cyclic)


@pytest.mark.parametrize(
    "payload",
    [
        _joined_bytes(
            b"unknown: 1\nscreens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: 1\n    mystery: value\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: *MISSING\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n -\n   name: one\n   head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n  - - *matcher\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n      - Raise\n",
        ),
        _joined_bytes(
            b"---\nscreens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: 'one'\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: {value: 1}\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: !integer 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        _joined_bytes(
            b"screens:\n  -\n    name: one\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise: now\n",
        ),
        _joined_bytes(
            b"screens:\r\n  -\r\n    name: one\r\n    head: 1\r\n",
            b"windows:\r\n  - - (Name=x)\r\n    - Raise\r\n",
        ),
        _joined_bytes(
            b"screens:\n\t-\n    name: one\n    head: 1\n",
            b"windows:\n  - - (Name=x)\n    - Raise\n",
        ),
        b"\xffscreens:\n",
    ],
)
def test_layout_parser_rejects_malformed_structural_corpus(payload: bytes) -> None:
    with pytest.raises(LayoutPlanningError):
        parse_layout("test", ((".fluxbox/layouts/test.yaml", payload),))


def test_layout_parser_enforces_file_depth_and_resolved_output_budgets() -> None:
    minimal = (
        b"screens:\n  -\n    name: one\n    head: 1\n"
        b"windows:\n  - - (Name=x)\n    - Raise\n"
    )
    too_many = tuple(
        (f".fluxbox/layouts/{index}.yaml", minimal)
        for index in range(MAX_LAYOUT_FILES + 1)
    )
    with pytest.raises(LayoutPlanningError, match="file count"):
        parse_layout("0", too_many)

    with pytest.raises(LayoutPlanningError, match="too large"):
        parse_layout(
            "test",
            (
                (
                    ".fluxbox/layouts/test.yaml",
                    b"x" * (MAX_LAYOUT_FILE_BYTES + 1),
                ),
            ),
        )

    deep_files = [
        (
            ".fluxbox/layouts/test.yaml",
            _joined_bytes(
                b"screens:\n  -\n    name: one\n    head: 1\n",
                b"windows:\n  <INCLUDE depth/0>\n",
            ),
        )
    ]
    deep_files.extend(
        (
            f".fluxbox/layouts/depth/{index}.yaml",
            f"<INCLUDE depth/{index + 1}>\n".encode(),
        )
        for index in range(MAX_INCLUDE_DEPTH + 1)
    )
    deep_files.append(
        (
            f".fluxbox/layouts/depth/{MAX_INCLUDE_DEPTH + 1}.yaml",
            b"- - (Name=x)\n  - Raise\n",
        )
    )
    with pytest.raises(LayoutPlanningError, match="depth"):
        parse_layout("test", tuple(deep_files))

    long_command = b"R" * 12_000
    window_group = b"".join(
        b"- - (Name=x)\n  - " + long_command + b"\n" for _unused in range(20)
    )
    main = b"screens:\n  -\n    name: one\n    head: 1\nwindows:\n" + b"".join(
        f"  <INCLUDE large/{index}>\n".encode() for index in range(5)
    )
    parsed = parse_layout(
        "test",
        (
            (".fluxbox/layouts/test.yaml", main),
            *(
                (f".fluxbox/layouts/large/{index}.yaml", window_group)
                for index in range(5)
            ),
        ),
    )
    with pytest.raises(LayoutPlanningError, match="resolved layout exceeds"):
        resolve_layout(
            parsed,
            (
                DisplayScreenSnapshot(
                    output="eDP",
                    width=1920,
                    height=1080,
                    x=0,
                    y=0,
                    width_mm=300,
                    height_mm=200,
                    primary=True,
                ),
            ),
        )


def test_layout_include_dag_is_charged_during_expansion() -> None:
    files: list[tuple[str, bytes]] = []
    root = (
        b"screens:\n  -\n    name: one\n    head: 1\nwindows:\n  <INCLUDE common/0>\n"
    )
    files.append((".fluxbox/layouts/test.yaml", root))
    files.extend(
        (
            f".fluxbox/layouts/common/{index}.yaml",
            (f"<INCLUDE common/{index + 1}>\n<INCLUDE common/{index + 1}>\n").encode(),
        )
        for index in range(15)
    )
    files.append(
        (
            ".fluxbox/layouts/common/15.yaml",
            b"- - (Name=x)\n  - Raise\n",
        )
    )
    with pytest.raises(LayoutPlanningError, match="line limit"):
        parse_layout("test", tuple(files))


def test_configuration_hash_domain_separates_path_role_presence_and_content() -> None:
    present = ConfigurationInput((InputRole.PANEL_POLICY,), "a/policy", b"absent")
    absent = ConfigurationInput((InputRole.PANEL_POLICY,), "a/policy", None)
    other_path = ConfigurationInput((InputRole.PANEL_POLICY,), "b/policy", b"absent")
    other_role = ConfigurationInput((InputRole.DPI_POLICY,), "a/policy", b"absent")
    other_content = ConfigurationInput(
        (InputRole.PANEL_POLICY,), "a/policy", b"present"
    )

    assert (
        len(
            {
                present.content_hash.sha256,
                absent.content_hash.sha256,
                other_path.content_hash.sha256,
                other_role.content_hash.sha256,
                other_content.content_hash.sha256,
            }
        )
        == 5
    )
    artifact = PlanArtifact("artifacts/test/value", present.content or b"missing")
    assert artifact.sha256 != present.content_hash.sha256


def test_plan_action_enum_phase_and_artifact_allowlists_are_closed() -> None:
    source, request = _celtic()
    encoded = encode_plan(build_desktop_plan(source.load(request)).plan)
    raw = json.loads(encoded)
    assert [item["kind"] for item in raw["prepare_actions"]] == [
        item.value
        for item in (
            PlannedActionKind.INSTALL_FLUXBOX_OVERLAY,
            PlannedActionKind.SET_PANEL_PROPERTIES,
            PlannedActionKind.SET_XFCE_DPI,
            PlannedActionKind.CONFIGURE_TERMINALS,
            PlannedActionKind.RELOAD_EMACS_FONTS,
            PlannedActionKind.GENERATE_FLUXBOX_CONFIGURATION,
        )
    ]
    raw["prepare_actions"][0]["kind"] = "run_current_live_command"
    with pytest.raises(PlanCodecError):
        decode_plan(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["finalize_actions"][2]["artifact_refs"] = ["artifacts/fluxbox/overlay"]
    with pytest.raises(PlanCodecError, match="artifact allowlist"):
        decode_plan(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["emacs"]["expression"] = '(shell-command "arbitrary")'
    with pytest.raises(PlanCodecError, match="Emacs font function"):
        decode_plan(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["fluxbox"]["generated_keys_path"] = ".fluxbox/keys.attacker"
    with pytest.raises(PlanCodecError, match="generated Fluxbox path"):
        decode_plan(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["autorandr"]["setup_artifact"] = raw["autorandr"]["config_artifact"]
    raw["autorandr"]["setup_sha256"] = raw["autorandr"]["config_sha256"]
    with pytest.raises(PlanCodecError, match="artifact"):
        decode_plan(json.dumps(raw).encode())


def test_stable_file_validation_ignores_atime_only(tmp_path: Path) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    store = AtomicPlanStore(tmp_path / "plans")
    store.stage(request.action_id, bundle)
    plan_path = store.action_directory(request.action_id) / "plan.json"
    before = plan_path.stat()
    os.utime(
        plan_path,
        ns=(before.st_atime_ns + 1_000_000_000, before.st_mtime_ns),
    )
    assert store.read(request.action_id) == bundle


def test_capture_and_plan_store_retain_authority_across_parent_swap(
    tmp_path: Path,
) -> None:
    real_source, request = _celtic()
    captured = real_source.load(request)
    source_root = tmp_path / "capture"
    _materialize_snapshot(source_root, captured.configuration)
    source = FilesystemDesktopPlanningInputSource(
        root=source_root,
        display=captured.display,
        context=captured.context,
    )
    expected = source.configuration_for(request.profile, request.input_key.layout)
    moved_source = tmp_path / "capture-retained"
    source_root.rename(moved_source)
    source_root.mkdir(mode=0o700)
    assert (
        source.configuration_for(request.profile, request.input_key.layout) == expected
    )

    bundle = build_desktop_plan(source.load(request))
    plan_parent = tmp_path / "authority"
    store = AtomicPlanStore(plan_parent / "plans")
    plan_hash = store.stage(request.action_id, bundle)
    moved_parent = tmp_path / "authority-retained"
    plan_parent.rename(moved_parent)
    (plan_parent / "plans").mkdir(mode=0o700, parents=True)
    assert store.read(request.action_id) == bundle
    store.revoke(request.action_id)
    store.discard(request.action_id, plan_hash)
    assert not (moved_parent / "plans" / request.action_id.value).exists()


@pytest.mark.parametrize(
    "boundary",
    [
        "temporary_directory_created",
        "bundle_written",
        "bundle_synced",
        "bundle_published",
        "parent_synced",
    ],
)
def test_plan_publication_is_retryable_at_fault_boundaries(
    tmp_path: Path, boundary: str
) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))

    def fail(reached: str) -> None:
        if reached == boundary:
            raise RuntimeError(boundary)

    store = AtomicPlanStore(tmp_path / boundary / "plans", installation_fault=fail)
    with pytest.raises(RuntimeError, match=boundary):
        store.stage(request.action_id, bundle)
    store.close()
    recovered = AtomicPlanStore(tmp_path / boundary / "plans")
    assert recovered.stage(request.action_id, bundle) == hash_plan_bundle(bundle)
    assert recovered.read(request.action_id) == bundle


def test_concurrent_plan_publishers_converge_without_replacement(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    barrier = threading.Barrier(2)
    results: list[object] = []

    def pause(boundary: str) -> None:
        if boundary == "bundle_synced":
            barrier.wait(timeout=5)

    def publish() -> None:
        try:
            store = AtomicPlanStore(tmp_path / "plans", installation_fault=pause)
            results.append(store.stage(request.action_id, bundle))
        except Exception as error:  # noqa: BLE001 - collected by test thread
            results.append(error)

    threads = (threading.Thread(target=publish), threading.Thread(target=publish))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert results == [hash_plan_bundle(bundle), hash_plan_bundle(bundle)]


def test_revocation_wins_after_bundle_sync_before_publication(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    reached = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def pause(boundary: str) -> None:
        if boundary == "bundle_synced":
            reached.set()
            if not release.wait(timeout=5):
                message = "revocation test did not release publisher"
                raise TimeoutError(message)

    store = AtomicPlanStore(tmp_path / "plans", installation_fault=pause)

    def publish() -> None:
        try:
            results.append(store.stage(request.action_id, bundle))
        except (ImmutablePlanError, PlanCodecError, OSError) as error:
            results.append(error)

    thread = threading.Thread(target=publish)
    thread.start()
    assert reached.wait(timeout=5)
    store.revoke(request.action_id)
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1
    assert isinstance(results[0], ImmutablePlanError)
    assert not store.action_directory(request.action_id).exists()


def test_plan_store_rejects_unsafe_existing_root_without_chmod(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    root = tmp_path / "public-plans"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = AtomicPlanStore(root)

    with pytest.raises(PlanCodecError, match="private retained directory"):
        store.stage(request.action_id, bundle)

    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_revocation_rejects_cancellation_suppressing_late_publisher(
    tmp_path: Path,
) -> None:
    source, request = _celtic()
    bundle = build_desktop_plan(source.load(request))
    store = AtomicPlanStore(tmp_path / "plans")

    async def exercise() -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def publish_late() -> object:
            started.set()
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
            return await asyncio.to_thread(store.stage, request.action_id, bundle)

        task = asyncio.create_task(publish_late())
        await started.wait()
        store.revoke(request.action_id)
        task.cancel()
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], ImmutablePlanError)
        assert not store.action_directory(request.action_id).exists()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("profile", "layout", "external", "size", "model", "expected"),
    [
        (
            "celtic",
            "celtic",
            None,
            None,
            "BOE NE135A1M-NY1",
            (2880, 1885, 1584, 1296, 0, 1584, 2880, 942),
        ),
        (
            "celtic+AOC-U28G2G6B",
            "celtic+external",
            "DisplayPort-2",
            (3840, 2160, 600, 340),
            "AOC U28G2G6B",
            (3840, 2017, 2150, 1651, 0, 2169, 3072, 1008),
        ),
        (
            "celtic+Samsung-Odyssey-G75F",
            "celtic+ultrawide",
            "DisplayPort-1",
            (5120, 2160, 930, 400),
            "Samsung Odyssey G75F",
            (5120, 2017, 1587, 1536, 1976, 1597, 3072, 1008),
        ),
    ],
)
def test_real_geometry_matches_independent_legacy_truncation_golden(  # noqa: PLR0913, PLR0917
    profile: str,
    layout: str,
    external: str | None,
    size: tuple[int, int, int, int] | None,
    model: str,
    expected: tuple[int, ...],
) -> None:
    source, request = _case(
        sequence=9,
        profile=profile,
        layout=layout,
        external=external,
        external_size=size,
    )
    inputs = source.load(request)
    assert inputs.context.primary_monitor_model == model
    screen = build_desktop_plan(inputs).plan.resolved_layout.screens[-1]
    assert (
        screen.value("active_width"),
        screen.value("active_height"),
        screen.value("col1_width"),
        screen.value("col2_width"),
        screen.value("col3_width"),
        screen.value("col2_left"),
        screen.value("single_width"),
        screen.value("row1_height"),
    ) == expected


_LEGACY_LIBLAYOUT_SCRIPT = """
import json
import sys
from pathlib import Path

repository = Path(sys.argv[1])
sys.path.insert(0, str(repository / "lib"))
import liblayout

layout_root = repository / ".fluxbox" / "layouts"
liblayout.get_layout_file = lambda name, dir=None: str(
    layout_root / (name if str(name).endswith(".yaml") else f"{name}.yaml")
)
screens = json.loads(sys.stdin.read())
liblayout.libdpy.get_xrandr_screen_geometries = (
    lambda use_cache=False: [dict(item) for item in screens]
)
resolved, _layout = liblayout.get_layout_params(
    str(layout_root / f"{sys.argv[2]}.yaml"), use_cache=False
)
print(json.dumps(resolved, sort_keys=True))
"""


def _assert_legacy_geometry_parity(
    planned: ResolvedLayout,
    layout: str,
    screens: tuple[DisplayScreenSnapshot, ...],
    tmp_path: Path,
) -> None:
    """Run lib/liblayout.py over the same screens and compare every field."""
    screen_payload = [
        {
            "height": item.height,
            "height_mm": item.height_mm,
            "primary": item.primary,
            "width": item.width,
            "width_mm": item.width_mm,
            "x_offset": item.x,
            "y_offset": item.y,
        }
        for item in sorted(screens, key=lambda item: (item.x, item.y, item.output))
    ]
    completed = subprocess.run(  # noqa: S603
        ("/usr/bin/python3", "-I", "-c", _LEGACY_LIBLAYOUT_SCRIPT, str(_REPO), layout),
        check=False,
        capture_output=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        input=json.dumps(screen_payload),
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    legacy = json.loads(completed.stdout)
    assert len(legacy) == len(planned.screens)
    for planned_screen, legacy_screen in zip(planned.screens, legacy, strict=True):
        planned_fields = {item.name for item in planned_screen.geometry}
        assert set(_LEGACY_GEOMETRY_FIELDS) <= planned_fields
        assert set(_LEGACY_GEOMETRY_FIELDS) <= set(legacy_screen)
        assert {
            name: planned_screen.value(name) for name in _LEGACY_GEOMETRY_FIELDS
        } == {
            name: legacy_screen[name] for name in _LEGACY_GEOMETRY_FIELDS
        }, f"{layout}: geometry drift against lib/liblayout.py"


@pytest.mark.parametrize(
    ("profile", "layout", "external", "size", "model"),
    [
        ("celtic", "celtic", None, None, "BOE NE135A1M-NY1"),
        (
            "celtic+AOC-U28G2G6B",
            "celtic+external",
            "DisplayPort-2",
            (3840, 2160, 600, 340),
            "AOC U28G2G6B",
        ),
        (
            "celtic+Samsung-Odyssey-G75F",
            "celtic+ultrawide",
            "DisplayPort-1",
            (5120, 2160, 930, 400),
            "Samsung Odyssey G75F",
        ),
    ],
)
def test_offline_real_layouts_match_legacy_liblayout(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    profile: str,
    layout: str,
    external: str | None,
    size: tuple[int, int, int, int] | None,
    model: str,
) -> None:
    source, request = _case(
        sequence=10,
        profile=profile,
        layout=layout,
        external=external,
        external_size=size,
    )
    inputs = source.load(request)
    assert inputs.context.primary_monitor_model == model
    planned = build_desktop_plan(inputs).plan.resolved_layout
    _assert_legacy_geometry_parity(
        planned, layout, inputs.display.screens, tmp_path
    )


def test_real_celtic_reducer_controller_capture_stage_completion_and_baseline(
    tmp_path: Path,
) -> None:
    source, expected_request = _celtic()
    profile = ProfileMatch(
        profile=expected_request.profile,
        scope=ProfileScope.INTERNAL_ONLY,
        layout=expected_request.input_key.layout,
        mapping=expected_request.input_key.mapping,
        active_outputs=("eDP",),
        configuration_hashes=expected_request.input_key.configuration_hashes,
    )
    boot_id = BootId(UUID(int=91))
    observation = CanonicalObservation(
        observed_at_ms=0,
        observation_generation=ObservationGeneration(1),
        boot_id=boot_id,
        physical_token=PhysicalToken("physical-celtic"),
        begin_event_generation=EventGeneration(1),
        end_event_generation=EventGeneration(1),
        kernel_connected_outputs=("eDP",),
        kernel_external_outputs=(),
        x_connected_outputs=("eDP",),
        x_active_outputs=("eDP",),
        x_external_outputs=(),
        connector_identities=(),
        live_fingerprints=(),
        base_identity_profiles=(),
        edid_integrity=(),
        probe_candidate=None,
        eligible_profiles=(profile,),
        current_profiles=("celtic",),
        exact_profile="celtic",
        observation_key=expected_request.input_key.observation_key,
        validity=ObservationValidity.VALID,
        invalidity_reason=None,
        raw_evidence=(),
    )

    class Store:
        def __init__(self) -> None:
            self.states: list[State] = []

        def save(self, state: State) -> None:
            self.states.append(state)

    class Observer:
        async def observe(self) -> CanonicalObservation:
            message = "injected observation should be consumed directly"
            raise AssertionError(message)

    class Clock:
        def monotonic_ms(self) -> int:
            return 0

        async def sleep_until(self, deadline_ms: int) -> None:
            del deadline_ms
            await asyncio.Event().wait()

    async def exercise() -> None:
        baseline_store = AtomicPlanStore(tmp_path / "baseline" / "plans")
        baseline_planner = AtomicDesktopPlanningAdapter(source, baseline_store)
        baseline_state = State(
            boot_id=boot_id,
            controller_instance=_INSTANCE,
            display_identity=DisplayIdentity(":offline-celtic"),
        )
        baseline = SerializedController(
            initial_state=baseline_state,
            store=Store(),
            observer=Observer(),
            planner=baseline_planner,
            dispatcher=NullDispatcher(),
            audit=RotatingAuditLog(tmp_path / "baseline.audit", baseline_state),
            clock=Clock(),
        )
        await baseline.consume(
            ObservationCompleted(EventMetadata(0, boot_id), observation)
        )
        assert baseline.state.baseline_adoption
        assert baseline.state.planning is None
        assert not baseline_store.root.exists()
        await baseline.close()

        plan_store = AtomicPlanStore(tmp_path / "transition" / "plans")
        planner = AtomicDesktopPlanningAdapter(source, plan_store)
        initial = State(
            boot_id=boot_id,
            controller_instance=_INSTANCE,
            display_identity=DisplayIdentity(":offline-celtic"),
            desktop_finalized_profile="previous-layout",
        )
        controller = SerializedController(
            initial_state=initial,
            store=Store(),
            observer=Observer(),
            planner=planner,
            dispatcher=NullDispatcher(),
            audit=RotatingAuditLog(tmp_path / "transition.audit", initial),
            clock=Clock(),
        )
        await controller.consume(
            ObservationCompleted(EventMetadata(0, boot_id), observation)
        )
        await controller.process_next()

        completed = controller.state.planning
        assert completed is not None
        assert completed.input_key == expected_request.input_key
        assert completed.plan_hash is not None
        bundle = plan_store.read(completed.action_id)
        assert bundle.plan.guards.input_key == completed.input_key
        assert hash_plan_bundle(bundle) == completed.plan_hash
        await controller.close()
        planner.close()
        baseline_planner.close()

    asyncio.run(exercise())


def test_dynamic_overlay_constants_match_setup_monitor() -> None:
    """The Python overlay generator must track setup_overlay() byte-for-byte.

    bin/setup-monitor remains authoritative until cutover, so the same layout
    must not draw different fonts depending on which pipeline relaid it out.
    Commit 6bc8fc9 changed only the shell and left the planner on the old
    constants for two days (dc-txr); this pins the two together.
    """
    shell = (_REPO / "bin" / "setup-monitor").read_text(encoding="utf-8")

    base_match = re.search(r"^\s*local base_font=(\d+)$", shell, re.MULTILINE)
    assert base_match, "cannot find base_font in setup_overlay()"
    base_font = int(base_match.group(1))

    title_match = re.search(
        r"window\.title\.height:\s*\$\(\( (\d+) \+ \(title_font - (\d+)\) \* 2 \)\)",
        shell,
    )
    menu_match = re.search(
        r"menu\.titleHeight:\s*\$\(\( (\d+) \+ \(title_font - (\d+)\) \* 2 \)\)",
        shell,
    )
    assert title_match, "cannot find window.title.height arithmetic"
    assert menu_match, "cannot find menu.titleHeight arithmetic"

    for scale in (Decimal(1), Decimal("0.85"), Decimal("1.45")):
        title_font = round(base_font * scale)
        menu_font = round((base_font + 1) * scale)
        title_base, title_ref = (int(g) for g in title_match.groups())
        menu_base, menu_ref = (int(g) for g in menu_match.groups())
        expected = (
            "window.borderWidth:               1\n"
            "window.handleWidth:               8\n"
            f"window.font:                      sans-{title_font}:bold\n"
            f"window.title.height:              "
            f"{title_base + (title_font - title_ref) * 2}\n"
            f"menu.title.font:                  sans-{title_font}:bold\n"
            f"menu.frame.font:                  sans-{menu_font}\n"
            f"menu.titleHeight:                 "
            f"{menu_base + (title_font - menu_ref) * 2}\n"
            "menu.itemHeight:                  10\n"
        ).encode()
        assert dynamic_overlay(scale) == expected


def test_dpi_overrides_agree_with_set_layout_dpi_shell_table() -> None:
    """Planner and shell must resolve the same DPI for every saved monitor.

    The shell table in bin/set-layout-dpi is keyed on hwinfo's free-text
    model names while the planner keys on EDID vendor and product bytes; a
    silent mismatch falls through to physical-size DPI (dc-b2u). This pins
    the two tables together through the saved profiles' own EDIDs.
    """
    shell = (_REPO / "bin" / "set-layout-dpi").read_text(encoding="utf-8")
    case_body = shell.split("try_dpi_from_model_override", 1)[1].split("esac", 1)[0]
    shell_table: dict[str, int] = {}
    for arm in case_body.split(";;"):
        dpi_match = re.search(r"set-xfce4-dpi (\d+)", arm)
        if dpi_match is None:
            continue
        header = re.search(r'^\s*((?:"[^"]*"\|)*"[^"]*")\)\s*$', arm, re.MULTILINE)
        assert header, f"cannot parse case pattern in: {arm!r}"
        for name in re.findall(r'"([^"]+)"', header.group(1)):
            shell_table[name] = int(dpi_match.group(1))
    assert shell_table, "cannot parse try_dpi_from_model_override()"

    seen_keys: set[str] = set()
    for saved in load_saved_profiles(_REPO / ".config" / "autorandr"):
        primary = derive_profile_monitor_identity(saved).primary
        seen_keys.add(primary.edid_model)
        assert EDID_DPI_OVERRIDES.get(primary.edid_model) == shell_table.get(
            primary.model
        ), f"{saved.name}: planner and shell disagree for {primary.model}"

    # Every planner override must be reachable through a saved profile;
    # an unreachable key is dead policy that can drift unnoticed.
    assert set(EDID_DPI_OVERRIDES) <= seen_keys


def _synthetic_parity_screens(
    count: int, primary_index: int
) -> tuple[DisplayScreenSnapshot, ...]:
    shapes = ((2880, 1920, 285, 190), (3840, 2160, 600, 340), (5120, 2160, 930, 400))
    screens: list[DisplayScreenSnapshot] = []
    x = 0
    for index in range(count):
        width, height, width_mm, height_mm = shapes[index % len(shapes)]
        screens.append(
            DisplayScreenSnapshot(
                output=f"OUT-{index}",
                width=width,
                height=height,
                x=x,
                y=0,
                width_mm=width_mm,
                height_mm=height_mm,
                primary=index == primary_index,
            )
        )
        x += width
    return tuple(screens)


def test_every_tracked_layout_geometry_matches_legacy_liblayout(
    tmp_path: Path,
) -> None:
    """Both layout implementations must agree over every tracked layout.

    lib/liblayout.py stays authoritative for the live pipeline until cutover
    while desktop/layout.py is a full reimplementation; nothing else detects
    the two drifting apart (dc-kbu). The saved-profile parity cases above
    cover only the layouts with profiles; this sweeps the rest with synthetic
    screens of matching count.
    """
    root = _REPO / ".fluxbox" / "layouts"
    files = tuple(
        (path.relative_to(_REPO).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*.yaml"))
    )
    layouts = tuple(sorted(root.glob("*.yaml")))
    assert layouts
    for path in layouts:
        parsed = parse_layout(path.stem, files)
        primary_index = next(
            (
                index
                for index, screen in enumerate(parsed.screens)
                if screen.assignment == "primary"
            ),
            0,
        )
        screens = _synthetic_parity_screens(len(parsed.screens), primary_index)
        planned = resolve_layout(parsed, screens)
        _assert_legacy_geometry_parity(planned, path.stem, screens, tmp_path)
