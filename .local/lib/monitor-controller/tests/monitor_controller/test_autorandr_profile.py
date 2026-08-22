"""Deterministic transaction-local autorandr profile materialization contracts."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ApplicationAttemptKey,
    ApplyProfile,
    ConfigurationContentHash,
    ControllerInstanceId,
    EventGeneration,
    MappingProof,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    RawEvidenceSource,
)
from monitor_controller.observer.autorandr import (
    SavedAutorandrProfile,
    parse_saved_profile,
)
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.snapshot import StaticSavedProfiles
from monitor_controller.runtime.dispatcher import WorkerRequestContext
from monitor_controller.runtime.systemd import SystemdDispatcher, SystemdSupervisor
from monitor_controller.runtime.transactions import (
    ExpectedTopology,
    ImmutableTransactionError,
    TransactionProtocolError,
    TransactionRequest,
    TransactionStore,
)
from monitor_controller.workers.autorandr_profile import (
    POSTSWITCH_CONTENT,
    AutorandrProfileMaterializationError,
    materialize_autorandr_profile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "autorandr" / "profiles"
_INSTANCE = ControllerInstanceId(UUID("12345678-1234-5678-1234-567812345678"))
_ACTION = ActionId(_INSTANCE, ActionKind.APPLICATION, 31)
_ACTION_PROFILE = _ACTION.value


def _evidence(reference: str, text: str) -> TextCommandEvidence:
    return TextCommandEvidence(
        RawEvidenceSource.AUTORANDR_PROFILES,
        reference,
        text,
    )


def _profile(name: str) -> SavedAutorandrProfile:
    root = FIXTURES / name
    layout = root / "layout"
    parsed = parse_saved_profile(
        name,
        _evidence(f"profiles/{name}/config", (root / "config").read_text()),
        _evidence(f"profiles/{name}/setup", (root / "setup").read_text()),
        (
            _evidence(f"profiles/{name}/layout", layout.read_text())
            if layout.exists()
            else None
        ),
    )
    assert parsed.valid
    assert parsed.profile is not None
    return parsed.profile


def _contents(
    profile: SavedAutorandrProfile,
    mapping: tuple[OutputMapping, ...],
) -> dict[str, bytes]:
    materialized = materialize_autorandr_profile(
        profile,
        mapping,
        _ACTION_PROFILE,
    )
    return {
        item.relative_path.rsplit("/", maxsplit=1)[-1]: item.content
        for item in materialized.artifacts
    }


def test_real_aoc_profile_rewrites_config_and_setup_without_name_collision() -> None:
    profile = _profile("celtic+AOC-U28G2G6B")
    mapping = (
        OutputMapping("DisplayPort-2", "DisplayPort-7"),
        OutputMapping("eDP", "eDP"),
    )

    materialized = materialize_autorandr_profile(profile, mapping, _ACTION_PROFILE)
    contents = _contents(profile, mapping)
    config = contents["config"].decode()
    setup = contents["setup"].decode()

    # The real saved profile already has an unrelated off block for DisplayPort-7.
    # The action profile contains only the exact mapped setup/config bijection, so
    # remapping DisplayPort-2 cannot create two contradictory blocks.
    assert config.count("output DisplayPort-7\n") == 1
    assert config.count("output eDP\n") == 1
    assert "output DisplayPort-2\n" not in config
    assert {line.split()[0] for line in setup.splitlines()} == {
        "DisplayPort-7",
        "eDP",
    }
    assert materialized.active_outputs == ("DisplayPort-7", "eDP")
    assert contents["layout"] == b"celtic+external\n"
    assert contents["postswitch"] == POSTSWITCH_CONTENT
    assert next(
        item
        for item in materialized.artifacts
        if item.relative_path.endswith("/postswitch")
    ).executable


def test_real_samsung_wildcard_and_all_options_are_rendered_deterministically() -> None:
    profile = _profile("celtic+Samsung-Odyssey-G75F")
    mapping = (
        OutputMapping("DisplayPort-1", "DisplayPort-9"),
        OutputMapping("eDP", "eDP"),
    )

    first = materialize_autorandr_profile(profile, mapping, _ACTION_PROFILE)
    second = materialize_autorandr_profile(profile, mapping, _ACTION_PROFILE)
    contents = {
        item.relative_path.rsplit("/", maxsplit=1)[-1]: item.content
        for item in first.artifacts
    }

    assert first == second
    assert b"DisplayPort-9 " in contents["setup"]
    assert b"*" in contents["setup"]
    assert b"mode 5120x2160\n" in contents["config"]
    assert b"x-prop-broadcast_rgb Automatic\n" in contents["config"]
    payload = dict(first.payload)
    for name in ("config", "layout", "postswitch", "setup"):
        assert payload[f"{name}_sha256"] == (
            "sha256:" + hashlib.sha256(contents[name]).hexdigest()
        )


@pytest.mark.parametrize(
    "option",
    ["above", "below", "left-of", "right-of", "same-as"],
)
def test_connector_reference_options_follow_bijection_when_both_outputs_rename(
    option: str,
) -> None:
    profile = _profile("celtic+AOC-U28G2G6B")
    changed = replace(
        profile,
        config=tuple(
            replace(
                item,
                options=tuple(sorted((*item.options, (option, "eDP")))),
            )
            if item.output == "DisplayPort-2"
            else item
            for item in profile.config
        ),
    )
    mapping = (
        OutputMapping("DisplayPort-2", "DisplayPort-21"),
        OutputMapping("eDP", "eDP-9"),
    )

    config = _contents(changed, mapping)["config"].decode()

    assert "output DisplayPort-21\n" in config
    assert "output eDP-9\n" in config
    assert f"{option} eDP-9\n" in config
    assert f"{option} eDP\n" not in config


@pytest.mark.parametrize(
    ("reference", "message"),
    [("missing", "unmapped"), ("DisplayPort-2", "self-referential")],
)
def test_unknown_and_self_connector_references_are_rejected(
    reference: str,
    message: str,
) -> None:
    profile = _profile("celtic+AOC-U28G2G6B")
    changed = replace(
        profile,
        config=tuple(
            replace(
                item,
                options=tuple(sorted((*item.options, ("right-of", reference)))),
            )
            if item.output == "DisplayPort-2"
            else item
            for item in profile.config
        ),
    )
    mapping = (
        OutputMapping("DisplayPort-2", "DisplayPort-21"),
        OutputMapping("eDP", "eDP-9"),
    )

    with pytest.raises(AutorandrProfileMaterializationError, match=message):
        materialize_autorandr_profile(changed, mapping, _ACTION_PROFILE)


def test_optional_layout_absence_is_preserved() -> None:
    profile = _profile("celtic")
    materialized = materialize_autorandr_profile(
        profile,
        (OutputMapping("eDP", "eDP"),),
        _ACTION_PROFILE,
    )

    assert all(
        not item.relative_path.endswith("/layout") for item in materialized.artifacts
    )
    assert dict(materialized.payload)["layout_sha256"] is None
    assert materialized.layout is None


@pytest.mark.parametrize(
    "mapping",
    [
        (OutputMapping("eDP", "eDP"),),
        (
            OutputMapping("DisplayPort-2", "DisplayPort-7"),
            OutputMapping("eDP", "DisplayPort-7"),
        ),
        (
            OutputMapping("DisplayPort-2", "DisplayPort-7"),
            OutputMapping("missing", "eDP"),
        ),
    ],
)
def test_incomplete_colliding_and_wrong_saved_mappings_are_rejected(
    mapping: tuple[OutputMapping, ...],
) -> None:
    with pytest.raises(AutorandrProfileMaterializationError, match="bijection"):
        materialize_autorandr_profile(
            _profile("celtic+AOC-U28G2G6B"),
            mapping,
            _ACTION_PROFILE,
        )


def test_unsorted_mapping_and_malformed_option_are_rejected() -> None:
    profile = _profile("celtic+AOC-U28G2G6B")
    unsorted = (
        OutputMapping("eDP", "eDP"),
        OutputMapping("DisplayPort-2", "DisplayPort-7"),
    )
    with pytest.raises(AutorandrProfileMaterializationError, match="sorted"):
        materialize_autorandr_profile(profile, unsorted, _ACTION_PROFILE)

    target = next(item for item in profile.config if item.output == "DisplayPort-2")
    malformed = replace(target, options=(*target.options, ("unsafe", "x\ny")))
    changed = replace(
        profile,
        config=tuple(
            malformed if item.output == target.output else item
            for item in profile.config
        ),
    )
    mapping = (
        OutputMapping("DisplayPort-2", "DisplayPort-7"),
        OutputMapping("eDP", "eDP"),
    )
    with pytest.raises(AutorandrProfileMaterializationError, match="value"):
        materialize_autorandr_profile(changed, mapping, _ACTION_PROFILE)


def test_production_dispatcher_materializes_the_admitted_profile_before_submission(
    tmp_path: Path,
) -> None:
    profile = _profile("celtic")
    mapping = (OutputMapping("eDP", "eDP"),)
    observation_key = ObservationKey("dispatcher-materialized")
    proof = MappingProof(profile.name, 3, observation_key, mapping)
    effect = ApplyProfile(
        action_id=_ACTION,
        key=ApplicationAttemptKey(3, profile.name, observation_key),
        profile=profile.name,
        mapping=proof,
        admitted_event_generation=EventGeneration(8),
        observation_key=observation_key,
    )
    topology = ExpectedTopology(("eDP",), (), ("eDP",), ("eDP",))
    context = WorkerRequestContext(
        physical_epoch=3,
        physical_token=PhysicalToken("physical"),
        output_mapping=mapping,
        expected_topology=topology,
        profile_configuration_hashes=profile.configuration_hashes,
    )
    store = TransactionStore(tmp_path / "transactions")
    supervisor = SystemdSupervisor(systemctl=Path("/usr/bin/systemctl"))
    missing_dependency = SystemdDispatcher(store, supervisor)
    with pytest.raises(TransactionProtocolError, match="profile source"):
        asyncio.run(missing_dependency.write_request(effect, context))
    assert not store.action_directory(_ACTION).exists()

    dispatcher = SystemdDispatcher(
        store,
        supervisor,
        autorandr_profiles=StaticSavedProfiles((profile,)),
    )

    prepared = asyncio.run(dispatcher.write_request(effect, context))
    request = store.read_request(_ACTION)

    assert prepared.request_sha256 == request.request_sha256
    assert set(dict(request.payload)) == {
        "action_profile",
        "config_sha256",
        "layout_sha256",
        "postswitch_sha256",
        "setup_sha256",
    }
    materialized = materialize_autorandr_profile(profile, mapping, _ACTION.value)
    store.validate_artifacts(_ACTION, materialized.artifacts)

    changed_hashes = (
        ConfigurationContentHash(
            profile.configuration_hashes[0].path,
            "sha256:changed",
        ),
        *profile.configuration_hashes[1:],
    )
    other_action = ActionId(_INSTANCE, ActionKind.APPLICATION, 32)
    changed_effect = replace(
        effect,
        action_id=other_action,
        key=ApplicationAttemptKey(3, profile.name, observation_key),
    )
    with pytest.raises(TransactionProtocolError, match="differs from admission"):
        asyncio.run(
            dispatcher.write_request(
                changed_effect,
                replace(context, profile_configuration_hashes=changed_hashes),
            )
        )


def test_artifacts_publish_atomically_and_hash_manifest_is_request_bound(
    tmp_path: Path,
) -> None:
    profile = _profile("celtic")
    materialized = materialize_autorandr_profile(
        profile,
        (OutputMapping("eDP", "eDP"),),
        _ACTION_PROFILE,
    )
    topology = ExpectedTopology(("eDP",), (), ("eDP",), ("eDP",))
    request = TransactionRequest(
        action_id=_ACTION,
        action_kind=ActionKind.APPLICATION,
        unit_name=f"monitor-apply@{_ACTION.value}.service",
        physical_epoch=3,
        physical_token=PhysicalToken("physical"),
        admitted_event_generation=EventGeneration(8),
        observation_key=ObservationKey("materialized"),
        output_mapping=(OutputMapping("eDP", "eDP"),),
        expected_topology=topology,
        profile=profile.name,
        payload=materialized.payload,
    )
    store = TransactionStore(tmp_path / "transactions")

    written = store.create_request(request, materialized.artifacts)

    assert written.request_sha256
    store.validate_artifacts(_ACTION, materialized.artifacts)
    assert (
        store.read_artifact(
            _ACTION,
            next(
                item.relative_path
                for item in materialized.artifacts
                if item.relative_path.endswith("/postswitch")
            ),
            executable=True,
        )
        == POSTSWITCH_CONTENT
    )

    config = next(
        item
        for item in materialized.artifacts
        if item.relative_path.endswith("/config")
    )
    changed = replace(config, content=config.content + b"# changed\n")
    changed_artifacts = tuple(
        changed if item.relative_path == config.relative_path else item
        for item in materialized.artifacts
    )
    with pytest.raises(ImmutableTransactionError, match="changed"):
        store.create_request(request, changed_artifacts)
