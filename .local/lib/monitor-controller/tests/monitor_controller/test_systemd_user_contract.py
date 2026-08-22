"""Real harmless user-unit tests for the keyed worker protocol."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    ApplicationAction,
    ApplicationAttemptKey,
    ApplyProfile,
    BootId,
    ConfigurationContentHash,
    ControllerInstanceId,
    ControllerPhase,
    ControllerStarted,
    DisplayIdentity,
    EdidIntegrity,
    EventGeneration,
    EventMetadata,
    Fingerprint,
    MappingProof,
    ObservationKey,
    OutputMapping,
    PhysicalToken,
    ProfileScope,
    RawEvidenceSource,
    State,
    WorkerUnit,
)
from monitor_controller.observer.autorandr import (
    AutorandrConfigOutput,
    SavedAutorandrProfile,
)
from monitor_controller.observer.drm import RootedSysfsReader, sample_drm
from monitor_controller.observer.evidence import TextCommandEvidence
from monitor_controller.observer.snapshot import StaticSavedProfiles
from monitor_controller.observer.topology import derive_canonical_topology
from monitor_controller.observer.xrandr import sample_xrandr
from monitor_controller.reducer import reduce
from monitor_controller.runtime.dispatcher import (
    DispatchAdapterError,
    DispatchStartResult,
    WorkerActivity,
    WorkerRequestContext,
)
from monitor_controller.runtime.persistence import StateNamespace
from monitor_controller.runtime.recovery import recover_state
from monitor_controller.runtime.systemd import (
    BoundedSystemctlRunner,
    SystemctlCommandResult,
    SystemctlCommandRunner,
    SystemdDispatcher,
    SystemdRecoveryScanner,
    SystemdSupervisor,
    SystemdSupervisorError,
    SystemdUnitState,
)
from monitor_controller.runtime.transactions import (
    BoundTransactionRecord,
    ExpectedTopology,
    TransactionRequest,
    TransactionStore,
)

_REPOSITORY = Path(__file__).parents[5]
_STATIC_UNIT_DIRECTORY = _REPOSITORY / ".config" / "systemd" / "user"
_FIXTURES = Path(__file__).parent / "fixtures"
_XRANDR_FIXTURES = _FIXTURES / "xrandr"
_EDID_FIXTURES = _FIXTURES / "edid"
_PROFILE_SETUP = (
    _FIXTURES / "autorandr" / "profiles" / "celtic+Samsung-Odyssey-G75F" / "setup"
)
_TOPOLOGY = ExpectedTopology(("TEST-1",), ("TEST-1",), ("TEST-1",), ("TEST-1",))
_MAPPING = (OutputMapping("TEST-SAVED", "TEST-1"),)
_PROBE_ARGV = (
    "--output",
    "DisplayPort-9",
    "--mode",
    "5120x2160",
    "--right-of",
    "eDP",
)
_PROFILE_HASHES = (
    ConfigurationContentHash("profiles/harmless-contract/config", "sha256:config"),
    ConfigurationContentHash("profiles/harmless-contract/setup", "sha256:setup"),
)


def _saved_profile() -> SavedAutorandrProfile:
    return SavedAutorandrProfile(
        name="harmless-contract",
        setup=(Fingerprint("TEST-SAVED", "saved-fingerprint"),),
        config=(AutorandrConfigOutput("TEST-SAVED", (("mode", "1920x1080"),)),),
        layout="harmless-contract",
        scope=ProfileScope.EXTERNAL_ONLY,
        configuration_hashes=_PROFILE_HASHES,
    )


def _application_dispatcher(
    store: TransactionStore,
    supervisor: SystemdSupervisor,
) -> SystemdDispatcher:
    return SystemdDispatcher(
        store,
        supervisor,
        autorandr_profiles=StaticSavedProfiles((_saved_profile(),)),
    )


@dataclass(frozen=True, slots=True)
class _StaticProbeEvidence:
    query_text: str
    properties_text: str

    def query(self) -> TextCommandEvidence:
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_QUERY,
            "harmless-systemd:query",
            self.query_text,
        )

    def properties(self) -> TextCommandEvidence:
        return TextCommandEvidence(
            RawEvidenceSource.XRANDR_PROPERTIES,
            "harmless-systemd:properties",
            self.properties_text,
        )


@dataclass(slots=True)
class _RealContract:
    runtime_dir: Path
    root: Path
    unit_paths: tuple[Path, ...]
    unit_templates: Mapping[ActionKind, str]
    rejection_template: str
    no_result_template: str
    probe_sysfs_root: Path
    probe_log_path: Path
    store: TransactionStore
    supervisor: SystemdSupervisor
    instance: ControllerInstanceId
    units: list[WorkerUnit]
    sequence: int = 0

    def request(
        self,
        behavior: str,
        *,
        delay_ms: int = 0,
        spawn_child: bool = False,
        ready_path: Path | None = None,
        release_path: Path | None = None,
    ) -> TransactionRequest:
        self.sequence += 1
        action_id = ActionId(self.instance, ActionKind.APPLICATION, self.sequence)
        unit = self.supervisor.unit_for_action(action_id)
        self.units.append(unit)
        payload_items: list[tuple[str, str | int | bool]] = [
            ("delay_ms", delay_ms),
            ("spawn_child", spawn_child),
            ("test_behavior", behavior),
        ]
        if ready_path is not None and release_path is not None:
            payload_items.extend(
                (
                    ("ready_path", str(ready_path)),
                    ("release_path", str(release_path)),
                )
            )
        elif ready_path is not None or release_path is not None:
            msg = "both completed-result barrier paths are required"
            raise ValueError(msg)
        payload = tuple(sorted(payload_items))
        return self.store.create_request(
            TransactionRequest(
                action_id=action_id,
                action_kind=ActionKind.APPLICATION,
                unit_name=unit.unit_name,
                physical_epoch=1,
                physical_token=PhysicalToken("harmless-contract"),
                admitted_event_generation=EventGeneration(0),
                observation_key=ObservationKey("harmless-contract-observation"),
                output_mapping=_MAPPING,
                expected_topology=_TOPOLOGY,
                profile="harmless-contract",
                payload=payload,
            )
        )

    def probe_request(self) -> TransactionRequest:
        """Create one exact harmless request for the production probe entry point."""
        self.sequence += 1
        action_id = ActionId(self.instance, ActionKind.PROBE, self.sequence)
        unit = self.supervisor.unit_for_action(action_id)
        self.units.append(unit)
        query_text = (_XRANDR_FIXTURES / "inactive.query").read_text(encoding="utf-8")
        properties_text = (_XRANDR_FIXTURES / "inactive.props").read_text(
            encoding="utf-8"
        )
        drm = sample_drm(RootedSysfsReader(self.probe_sysfs_root))
        xrandr = sample_xrandr(_StaticProbeEvidence(query_text, properties_text))
        topology = derive_canonical_topology(drm, xrandr)
        target = next(
            item
            for item in drm.connectors
            if item.output_name == "DP-3" and item.edid.parsed is not None
        )
        assert target.edid.parsed is not None
        assert target.edid.parsed.base_hash is not None
        return self.store.create_request(
            TransactionRequest(
                action_id=action_id,
                action_kind=ActionKind.PROBE,
                unit_name=unit.unit_name,
                physical_epoch=1,
                physical_token=topology.physical_token,
                admitted_event_generation=EventGeneration(0),
                observation_key=ObservationKey("harmless-production-probe"),
                output_mapping=(),
                expected_topology=ExpectedTopology(
                    topology.kernel_connected_outputs,
                    topology.kernel_external_outputs,
                    topology.x_connected_outputs,
                    topology.x_active_outputs,
                ),
                profile="celtic+Samsung-Odyssey-G75F",
                payload=(
                    ("base_identity_hash", target.edid.parsed.base_hash),
                    (
                        "edid_integrity",
                        EdidIntegrity.BASE_VALID_EXTENSIONS_INVALID.value,
                    ),
                    ("internal_output", "eDP"),
                    ("preferred_mode", "5120x2160"),
                    ("probe_output", "DisplayPort-9"),
                ),
            )
        )

    def effect_and_context(
        self,
        *,
        supervisor: SystemdSupervisor | None = None,
    ) -> tuple[ApplyProfile, WorkerRequestContext]:
        """Allocate a harmless production-dispatch-shaped application."""
        self.sequence += 1
        action_id = ActionId(self.instance, ActionKind.APPLICATION, self.sequence)
        observation_key = ObservationKey(f"harmless-final-fence-{self.sequence}")
        mapping = MappingProof(
            "harmless-contract",
            1,
            observation_key,
            _MAPPING,
        )
        selected_supervisor = self.supervisor if supervisor is None else supervisor
        unit = selected_supervisor.unit_for_action(action_id)
        self.units.append(unit)
        return (
            ApplyProfile(
                action_id=action_id,
                key=ApplicationAttemptKey(
                    1,
                    "harmless-contract",
                    observation_key,
                ),
                profile="harmless-contract",
                mapping=mapping,
                admitted_event_generation=EventGeneration(0),
                observation_key=observation_key,
            ),
            WorkerRequestContext(
                physical_epoch=1,
                physical_token=PhysicalToken("harmless-contract"),
                output_mapping=_MAPPING,
                expected_topology=_TOPOLOGY,
                profile_configuration_hashes=_PROFILE_HASHES,
            ),
        )

    def unit(self, request: TransactionRequest) -> WorkerUnit:
        return WorkerUnit(request.action_id, request.unit_name)


@pytest.fixture(scope="module")
def real_contract() -> Iterator[_RealContract]:
    systemctl = shutil.which("systemctl")
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    if systemctl is None or runtime_value is None:
        pytest.skip("systemctl and XDG_RUNTIME_DIR are required")
    manager = subprocess.run(  # noqa: S603
        (systemctl, "--user", "show-environment"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if manager.returncode != 0:
        pytest.skip(f"systemd user manager is unavailable: {manager.stderr.strip()}")

    runtime_dir = Path(runtime_value)
    suffix = uuid4().hex
    template_base = f"monitor-contract-{suffix}"
    templates = {
        ActionKind.PROBE: f"{template_base}-probe@.service",
        ActionKind.APPLICATION: f"{template_base}-apply@.service",
        ActionKind.PREPARATION: f"{template_base}-prepare@.service",
        ActionKind.FINALIZATION: f"{template_base}-finalize@.service",
    }
    unit_directory = runtime_dir / "systemd" / "user"
    unit_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    application_unit_path = unit_directory / templates[ActionKind.APPLICATION]
    probe_unit_path = unit_directory / templates[ActionKind.PROBE]
    no_result_template = f"{template_base}-no-result@.service"
    no_result_unit_path = unit_directory / no_result_template
    rejection_template = f"{template_base}-reject@.service"
    rejection_unit_path = unit_directory / rejection_template
    unit_paths = (
        application_unit_path,
        probe_unit_path,
        no_result_unit_path,
        rejection_unit_path,
    )
    root = runtime_dir / f"monitor-system-contract-{suffix}" / "transactions"
    work_root = root.parent
    work_root.mkdir(mode=0o700, parents=True)
    probe_sysfs_root = _probe_sysfs_tree(work_root / "probe-sysfs")
    fake_bin = work_root / "bin"
    fake_bin.mkdir(mode=0o700)
    probe_log_path = work_root / "xrandr-arguments.log"
    python = Path(sys.executable).absolute()
    _write_fake_xrandr(
        fake_bin / "xrandr",
        python=python,
        log_path=probe_log_path,
    )
    application_unit_path.write_text(
        _contract_unit(python=python, transaction_root=root),
        encoding="utf-8",
    )
    probe_unit_path.write_text(
        _production_probe_unit(
            python=python,
            transaction_root=root,
            sysfs_root=probe_sysfs_root,
            fake_bin=fake_bin,
        ),
        encoding="utf-8",
    )
    no_result_unit_path.write_text(_no_result_unit(), encoding="utf-8")
    rejection_unit_path.write_text(_rejected_start_unit(), encoding="utf-8")
    _systemctl(systemctl, "daemon-reload")
    store = TransactionStore(root)
    supervisor = SystemdSupervisor(
        systemctl=Path(systemctl),
        unit_templates=templates,
    )
    contract = _RealContract(
        runtime_dir=runtime_dir,
        root=root,
        unit_paths=unit_paths,
        unit_templates=templates,
        rejection_template=rejection_template,
        no_result_template=no_result_template,
        probe_sysfs_root=probe_sysfs_root,
        probe_log_path=probe_log_path,
        store=store,
        supervisor=supervisor,
        instance=ControllerInstanceId(UUID(hex=suffix)),
        units=[],
    )
    try:
        yield contract
    finally:
        for unit in reversed(contract.units):
            _systemctl(systemctl, "stop", unit.unit_name, check=False)
            _systemctl(systemctl, "reset-failed", unit.unit_name, check=False)
        for unit_path in unit_paths:
            unit_path.unlink(missing_ok=True)
        _systemctl(systemctl, "daemon-reload", check=False)
        # A failed oneshot can be re-materialized while teardown removes its
        # template. Clear only these harmless exact instances once more afterward.
        for unit in reversed(contract.units):
            _systemctl(systemctl, "reset-failed", unit.unit_name, check=False)
        contract.store.close()
        shutil.rmtree(root.parent, ignore_errors=True)


def test_static_production_templates_are_explicit_and_fail_closed() -> None:
    expected_timeouts = {
        "monitor-probe@.service": "30s",
        "monitor-apply@.service": "120s",
        "monitor-prepare@.service": "300s",
        "monitor-finalize@.service": "300s",
    }
    for name, timeout in expected_timeouts.items():
        text = (_STATIC_UNIT_DIRECTORY / name).read_text(encoding="utf-8")
        directives = set(text.splitlines())
        assert "Type=oneshot" in directives
        assert "CollectMode=inactive-or-failed" in directives
        assert f"TimeoutStartSec={timeout}" in directives
        assert "TimeoutStartFailureMode=terminate" in directives
        if name == "monitor-finalize@.service":
            assert "TimeoutStopSec=130s" in directives
            assert "KillMode=mixed" in directives
        else:
            assert "TimeoutStopSec=5s" in directives
            assert "KillMode=control-group" in directives
        assert "KillSignal=SIGTERM" in directives
        assert "FinalKillSignal=SIGKILL" in directives
        assert "SendSIGKILL=yes" in directives
        assert "Restart=no" in directives
        if name == "monitor-probe@.service":
            assert "monitor_controller.cli internal probe" in text
            assert "reject-unimplemented" not in text
        elif name == "monitor-apply@.service":
            assert "Environment=HOME=%h" in directives
            assert "PassEnvironment=DISPLAY XAUTHORITY" in directives
            assert "monitor_controller.cli internal apply" in text
            assert "--sysfs-root /sys/class/drm" in text
            assert "reject-unimplemented" not in text
        elif name == "monitor-prepare@.service":
            assert "monitor_controller.cli internal prepare" in text
            assert "--plan-root %t/monitor-controller/active/plans" in text
            assert "--sysfs-root /sys/class/drm" in text
            assert "--home-root %h --leaf-root %h/bin" in text
            assert "reject-unimplemented" not in text
        else:
            assert "monitor_controller.cli internal finalize" in text
            assert "--plan-root %t/monitor-controller/active/plans" in text
            assert "--event-generation-file" in text
            assert "--home-root %h --leaf-root %h/bin" in text
            assert "reject-unimplemented" not in text
        assert "record-systemd-result" in text
        assert "SuccessExitStatus=" not in text


def test_harmless_unit_reuses_production_safety_contract(
    real_contract: _RealContract,
) -> None:
    production = (_STATIC_UNIT_DIRECTORY / "monitor-apply@.service").read_text(
        encoding="utf-8"
    )
    harmless = real_contract.unit_paths[0].read_text(encoding="utf-8")
    shared = {
        "Type=oneshot",
        "CollectMode=inactive-or-failed",
        "Environment=HOME=%h",
        "Environment=PATH=/usr/bin:/bin",
        "Environment=PYTHONUNBUFFERED=1",
        "PassEnvironment=DISPLAY XAUTHORITY",
        (
            "UnsetEnvironment=BASH_ENV ENV LD_LIBRARY_PATH LD_PRELOAD "
            "PYTHONHOME PYTHONINSPECT PYTHONPATH PYTHONSTARTUP PYTHONUSERBASE"
        ),
        "TimeoutStartFailureMode=terminate",
        "KillMode=control-group",
        "KillSignal=SIGTERM",
        "FinalKillSignal=SIGKILL",
        "SendSIGKILL=yes",
        "Restart=no",
        "RemainAfterExit=no",
        "StandardOutput=journal",
        "StandardError=journal",
        "UMask=0077",
    }

    assert shared <= set(production.splitlines())
    assert shared <= set(harmless.splitlines())
    assert "Environment=DISPLAY=" in harmless
    assert "monitor_controller.workers.test_worker" in harmless
    assert "record-systemd-result" in harmless


def test_dispatcher_final_fence_submits_real_manager_job_without_display(
    real_contract: _RealContract,
) -> None:
    async def exercise() -> None:
        effect, context = real_contract.effect_and_context()
        dispatcher = _application_dispatcher(
            real_contract.store,
            real_contract.supervisor,
        )
        prepared = await dispatcher.write_request(effect, context)
        fence_calls: list[ActionId] = []

        result = await dispatcher.start(
            prepared,
            lambda: not fence_calls.append(effect.action_id),
        )

        assert result is DispatchStartResult.ACCEPTED
        assert fence_calls == [effect.action_id]
        _wait_state(
            real_contract.supervisor,
            prepared.unit,
            WorkerActivity.INACTIVE,
            timeout_seconds=3,
        )
        completion = await dispatcher.worker_completion(prepared.unit)
        assert completion is not None
        assert completion.terminal_lifecycle is ActionLifecycle.COMPLETED

    asyncio.run(exercise())


def test_real_systemd_runs_production_probe_entry_with_harmless_adapters(
    real_contract: _RealContract,
) -> None:
    real_contract.probe_log_path.unlink(missing_ok=True)
    request = real_contract.probe_request()
    unit = real_contract.unit(request)

    assert (
        real_contract.supervisor.start(
            unit,
            lambda: True,
            lambda: real_contract.store.claim_submission(request.action_id),
        )
        is DispatchStartResult.ACCEPTED
    )
    _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.INACTIVE,
        timeout_seconds=5,
    )

    result = real_contract.store.read_result(request.action_id)
    arguments = real_contract.probe_log_path.read_text(encoding="utf-8").splitlines()
    assert arguments == ["--query", "--props", " ".join(_PROBE_ARGV)]
    assert result.outcome is ActionLifecycle.COMPLETED
    assert result.exit_status == 0
    assert result.request_sha256 == request.request_sha256
    assert real_contract.store.execution_claim_if_present(request.action_id) is not None


def test_real_systemd_escape_matches_systemd_escape(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("rapid")
    systemd_escape = shutil.which("systemd-escape")
    if systemd_escape is None:
        pytest.skip("systemd-escape is unavailable")
    completed = subprocess.run(  # noqa: S603
        (
            systemd_escape,
            f"--template={real_contract.unit_templates[ActionKind.APPLICATION]}",
            request.action_id.value,
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == request.unit_name


def test_rapid_completion_before_acknowledgement_is_unambiguous(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("rapid")
    unit = real_contract.unit(request)
    delayed = SystemdSupervisor(
        systemctl=Path(shutil.which("systemctl") or "/usr/bin/systemctl"),
        runner=_result_delaying_runner(real_contract.store, request),
        unit_templates=real_contract.unit_templates,
    )
    delayed.prepare_start(unit)

    def submission_guard() -> BoundTransactionRecord:
        return real_contract.store.claim_submission(request.action_id)

    assert delayed.start(unit, lambda: True, submission_guard) is (
        DispatchStartResult.ACCEPTED
    )
    result = real_contract.store.read_result(request.action_id)
    # The result existed before ``start`` returned; the unit may still be running
    # ExecStopPost, so retain exclusion until manager process truth is terminal.
    state = _wait_state(
        delayed,
        unit,
        WorkerActivity.INACTIVE,
        timeout_seconds=3,
    )
    assert state.activity is WorkerActivity.INACTIVE
    assert result.outcome is ActionLifecycle.COMPLETED
    dispatcher = SystemdDispatcher(real_contract.store, delayed)
    completion = asyncio.run(dispatcher.worker_completion(unit))
    assert completion is not None
    assert completion.terminal_lifecycle is ActionLifecycle.COMPLETED
    with pytest.raises(SystemdSupervisorError, match="submission guard"):
        delayed.start(unit, lambda: True, submission_guard)


def test_real_restart_reconciles_result_written_before_completion_state_ack(
    real_contract: _RealContract,
) -> None:
    effect, context = real_contract.effect_and_context()
    dispatcher = _application_dispatcher(
        real_contract.store,
        real_contract.supervisor,
    )
    prepared = asyncio.run(dispatcher.write_request(effect, context))
    boot_id = BootId(uuid4())
    display_identity = DisplayIdentity(":harmless-result-before-ack")
    persisted = State(
        boot_id=boot_id,
        controller_instance=real_contract.instance,
        display_identity=display_identity,
        phase=ControllerPhase.APPLYING,
        physical_epoch=1,
        attempted_application_keys=frozenset({effect.key}),
        application=ApplicationAction(
            action_id=effect.action_id,
            key=effect.key,
            admitted_event_generation=effect.admitted_event_generation,
            profile=effect.profile,
            scope=ProfileScope.MIXED,
            mapping=effect.mapping,
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=prepared.unit,
            worker_deadline_ms=300_000,
        ),
        action_sequence_high_water=effect.action_id.sequence,
    )
    delayed = SystemdSupervisor(
        systemctl=Path(shutil.which("systemctl") or "/usr/bin/systemctl"),
        runner=_result_delaying_runner(
            real_contract.store,
            real_contract.store.read_request(effect.action_id),
        ),
        unit_templates=real_contract.unit_templates,
    )

    assert (
        asyncio.run(
            SystemdDispatcher(real_contract.store, delayed).start(
                prepared,
                lambda: True,
            )
        )
        is DispatchStartResult.ACCEPTED
    )
    _wait_state(delayed, prepared.unit, WorkerActivity.INACTIVE, timeout_seconds=3)
    snapshot = SystemdRecoveryScanner(real_contract.store, delayed).scan(
        StateNamespace.ACTIVE
    )
    recovered = recover_state(
        persisted,
        current_boot_id=boot_id,
        controller_instance=ControllerInstanceId(uuid4()),
        display_identity=display_identity,
        namespace=StateNamespace.ACTIVE,
        scanner=SystemdRecoveryScanner(real_contract.store, delayed),
    )

    assert any(item.action_id == effect.action_id for item in snapshot.verified_results)
    assert recovered.authority_allowed
    assert recovered.requires_fresh_observation
    assert recovered.state.application is None
    assert any(
        item.action_id == effect.action_id
        and item.lifecycle is ActionLifecycle.COMPLETED
        for item in recovered.state.action_tombstones
    )
    assert not recovered.reasons


def test_actual_manager_start_rejection_writes_recoverable_terminal_result(
    real_contract: _RealContract,
    tmp_path: Path,
) -> None:
    templates = dict(real_contract.unit_templates)
    templates[ActionKind.APPLICATION] = real_contract.rejection_template
    supervisor = SystemdSupervisor(
        systemctl=Path(shutil.which("systemctl") or "/usr/bin/systemctl"),
        unit_templates=templates,
    )
    effect, context = real_contract.effect_and_context(supervisor=supervisor)
    store = TransactionStore(tmp_path / "rejected-transactions")
    dispatcher = _application_dispatcher(store, supervisor)
    prepared = asyncio.run(dispatcher.write_request(effect, context))

    with pytest.raises(DispatchAdapterError, match="systemctl start exited"):
        asyncio.run(dispatcher.start(prepared, lambda: True))

    result = store.read_result(effect.action_id)
    snapshot = SystemdRecoveryScanner(store, supervisor).scan(StateNamespace.ACTIVE)
    assert store.submission_claim_if_present(effect.action_id) is not None
    assert store.execution_claim_if_present(effect.action_id) is None
    assert result.outcome is ActionLifecycle.FAILED
    assert result.exit_status != 0
    assert snapshot.ambiguities == ()
    assert snapshot.verified_results[0].action_id == effect.action_id

    boot_id = BootId(uuid4())
    display_identity = DisplayIdentity(":harmless-start-rejection")
    persisted = State(
        boot_id=boot_id,
        controller_instance=real_contract.instance,
        display_identity=display_identity,
        phase=ControllerPhase.APPLYING,
        physical_epoch=1,
        attempted_application_keys=frozenset({effect.key}),
        application=ApplicationAction(
            action_id=effect.action_id,
            key=effect.key,
            admitted_event_generation=effect.admitted_event_generation,
            profile=effect.profile,
            scope=ProfileScope.MIXED,
            mapping=effect.mapping,
            lifecycle=ActionLifecycle.DISPATCHED,
            unit=prepared.unit,
            worker_deadline_ms=300_000,
        ),
        action_sequence_high_water=effect.action_id.sequence,
    )
    recovered = recover_state(
        persisted,
        current_boot_id=boot_id,
        controller_instance=ControllerInstanceId(uuid4()),
        display_identity=display_identity,
        namespace=StateNamespace.ACTIVE,
        scanner=SystemdRecoveryScanner(store, supervisor),
    )
    terminal = recovered.state.application
    assert recovered.authority_allowed
    assert recovered.requires_fresh_observation
    assert terminal is not None
    assert terminal.lifecycle is ActionLifecycle.FAILED
    assert terminal.exit_status == result.exit_status
    assert recovered.reasons == ()
    store.close()


def test_rejected_start_is_definite_and_never_runs_a_worker(
    real_contract: _RealContract,
) -> None:
    action_id = ActionId(
        real_contract.instance,
        ActionKind.PREPARATION,
        real_contract.sequence + 10_000,
    )
    unit = real_contract.supervisor.unit_for_action(action_id)
    real_contract.units.append(unit)

    with pytest.raises(SystemdSupervisorError):
        _ = real_contract.supervisor.start(unit, lambda: True, lambda: None)
    assert not real_contract.store.action_directory(action_id).exists()


def test_systemd_timeout_is_timed_out_and_cleans_forced_cgroup(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("ignore_term", delay_ms=10_000, spawn_child=True)
    unit = real_contract.unit(request)
    real_contract.supervisor.prepare_start(unit)
    assert (
        real_contract.supervisor.start(
            unit,
            lambda: True,
            lambda: real_contract.store.claim_submission(request.action_id),
        )
        is DispatchStartResult.ACCEPTED
    )
    active = _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    cgroup_path = _cgroup_path(active.control_group)
    _wait_until(lambda: _cgroup_process_count(cgroup_path) >= 2, timeout_seconds=3)
    terminal = _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.INACTIVE,
        timeout_seconds=6,
    )
    result = real_contract.store.read_result(request.action_id)

    # CollectMode may already have removed and freshly materialized the inactive
    # instance, so terminal semantics intentionally come from the bound result.
    assert terminal.main_pid == 0
    assert result.outcome is ActionLifecycle.TIMED_OUT
    assert result.exit_status == 124
    assert _cgroup_process_count(cgroup_path) == 0


def test_no_intent_sigterm_defers_to_real_systemd_timeout_result(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("cooperative", delay_ms=10_000)
    unit = real_contract.unit(request)
    assert (
        real_contract.supervisor.start(
            unit,
            lambda: True,
            lambda: real_contract.store.claim_submission(request.action_id),
        )
        is DispatchStartResult.ACCEPTED
    )
    _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.INACTIVE,
        timeout_seconds=5,
    )

    result = real_contract.store.read_result(request.action_id)
    assert real_contract.store.stop_intent_if_present(request.action_id) is None
    assert result.outcome is ActionLifecycle.TIMED_OUT
    assert result.exit_status == 124
    assert "SERVICE_RESULT=timeout" in result.detail


def test_stop_after_completed_result_before_process_exit_keeps_completion(
    real_contract: _RealContract,
) -> None:
    marker = uuid4().hex
    ready_path = real_contract.root.parent / f"completed-ready-{marker}"
    release_path = real_contract.root.parent / f"completed-release-{marker}"
    request = real_contract.request(
        "completed_result_barrier",
        ready_path=ready_path,
        release_path=release_path,
    )
    unit = real_contract.unit(request)
    dispatcher = SystemdDispatcher(real_contract.store, real_contract.supervisor)
    try:
        assert (
            real_contract.supervisor.start(
                unit,
                lambda: True,
                lambda: real_contract.store.claim_submission(request.action_id),
            )
            is DispatchStartResult.ACCEPTED
        )
        _wait_until(
            lambda: (
                ready_path.exists()
                and real_contract.store.result_if_present(request.action_id) is not None
            ),
            timeout_seconds=3,
        )
        assert real_contract.supervisor.reattach(unit).activity is WorkerActivity.ACTIVE

        asyncio.run(dispatcher.stop(request.action_id, ActionLifecycle.CANCELLED))
        completion = asyncio.run(dispatcher.worker_completion(unit))
        immutable_result = real_contract.store.read_result(request.action_id)

        assert completion is not None
        assert completion.terminal_lifecycle is ActionLifecycle.COMPLETED
        assert immutable_result.outcome is ActionLifecycle.COMPLETED
        assert immutable_result.exit_status == 0
        assert (
            real_contract.store.read_stop_intent(request.action_id).terminal_lifecycle
            is ActionLifecycle.CANCELLED
        )
        assert real_contract.supervisor.reattach(unit).activity is (
            WorkerActivity.INACTIVE
        )
    finally:
        ready_path.unlink(missing_ok=True)
        release_path.unlink(missing_ok=True)


def test_collect_mode_removes_failed_instance_but_result_remains_recoverable(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("fail")
    unit = real_contract.unit(request)
    assert (
        real_contract.supervisor.start(
            unit,
            lambda: True,
            lambda: real_contract.store.claim_submission(request.action_id),
        )
        is DispatchStartResult.ACCEPTED
    )
    _wait_until(
        lambda: real_contract.store.result_if_present(request.action_id) is not None,
        timeout_seconds=3,
    )
    executable = shutil.which("systemctl") or "/usr/bin/systemctl"
    _wait_until(
        lambda: not _unit_is_listed(executable, unit.unit_name),
        timeout_seconds=3,
    )

    snapshot = SystemdRecoveryScanner(
        real_contract.store,
        real_contract.supervisor,
    ).scan(StateNamespace.ACTIVE)
    recovered = next(
        item
        for item in snapshot.verified_results
        if item.action_id == request.action_id
    )
    assert recovered.terminal_lifecycle is ActionLifecycle.FAILED
    assert real_contract.store.read_result(request.action_id).outcome is (
        ActionLifecycle.FAILED
    )
    assert not snapshot.ambiguities


@pytest.mark.parametrize(
    "terminal_lifecycle",
    [ActionLifecycle.TIMED_OUT, ActionLifecycle.UNKNOWN],
)
def test_controller_stop_intent_overrides_non_timeout_manager_result(
    real_contract: _RealContract,
    terminal_lifecycle: ActionLifecycle,
) -> None:
    request = real_contract.request("sleep", delay_ms=10_000)
    unit = real_contract.unit(request)
    real_contract.supervisor.start(
        unit,
        lambda: True,
        lambda: real_contract.store.claim_submission(request.action_id),
    )
    _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    real_contract.store.create_stop_intent(
        request.action_id,
        terminal_lifecycle,
    )

    real_contract.supervisor.stop(unit)

    terminal = real_contract.supervisor.reattach(unit)
    result = real_contract.store.read_result(request.action_id)
    assert terminal.result != "timeout"
    assert result.outcome is terminal_lifecycle


def test_cooperative_cancellation_and_forced_timeout_wait_for_cgroup_cleanup(
    real_contract: _RealContract,
) -> None:
    cooperative = real_contract.request("cooperative", delay_ms=10_000)
    cooperative_unit = real_contract.unit(cooperative)
    real_contract.supervisor.prepare_start(cooperative_unit)
    real_contract.supervisor.start(
        cooperative_unit,
        lambda: True,
        lambda: real_contract.store.claim_submission(cooperative.action_id),
    )
    _wait_state(
        real_contract.supervisor,
        cooperative_unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    # Let the harmless worker install its cooperative handler after exec.
    time.sleep(0.1)
    real_contract.store.create_stop_intent(
        cooperative.action_id,
        ActionLifecycle.CANCELLED,
    )
    _ = real_contract.supervisor.stop(cooperative_unit)
    cooperative_result = real_contract.store.read_result(cooperative.action_id)
    assert cooperative_result.outcome is ActionLifecycle.CANCELLED
    assert real_contract.supervisor.reattach(cooperative_unit).activity is (
        WorkerActivity.INACTIVE
    )

    forced = real_contract.request("ignore_term", delay_ms=10_000, spawn_child=True)
    forced_unit = real_contract.unit(forced)
    real_contract.supervisor.prepare_start(forced_unit)
    real_contract.supervisor.start(
        forced_unit,
        lambda: True,
        lambda: real_contract.store.claim_submission(forced.action_id),
    )
    active = _wait_state(
        real_contract.supervisor,
        forced_unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    cgroup_path = _cgroup_path(active.control_group)
    _wait_until(lambda: _cgroup_process_count(cgroup_path) >= 2, timeout_seconds=3)
    dispatcher = SystemdDispatcher(real_contract.store, real_contract.supervisor)
    asyncio.run(dispatcher.stop(forced.action_id, ActionLifecycle.CANCELLED))
    forced_result = real_contract.store.read_result(forced.action_id)
    forced_completion = asyncio.run(dispatcher.worker_completion(forced_unit))

    assert forced_result.outcome is ActionLifecycle.TIMED_OUT
    assert forced_completion is not None
    assert forced_completion.terminal_lifecycle is ActionLifecycle.TIMED_OUT
    assert real_contract.supervisor.reattach(forced_unit).activity is (
        WorkerActivity.INACTIVE
    )
    assert _cgroup_process_count(cgroup_path) == 0


def test_real_recovery_and_reducer_keep_forced_systemd_timeout(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("ignore_term", delay_ms=10_000, spawn_child=True)
    unit = real_contract.unit(request)
    real_contract.supervisor.start(
        unit,
        lambda: True,
        lambda: real_contract.store.claim_submission(request.action_id),
    )
    active = _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    cgroup_path = _cgroup_path(active.control_group)
    _wait_until(lambda: _cgroup_process_count(cgroup_path) >= 2, timeout_seconds=3)
    dispatcher = SystemdDispatcher(real_contract.store, real_contract.supervisor)
    asyncio.run(dispatcher.stop(request.action_id, ActionLifecycle.CANCELLED))

    scanner = SystemdRecoveryScanner(real_contract.store, real_contract.supervisor)
    snapshot = scanner.scan(StateNamespace.ACTIVE)
    forced = next(
        item
        for item in snapshot.verified_tombstones
        if item.action_id == request.action_id
    )
    boot_id = BootId(uuid4())
    display_identity = DisplayIdentity(":harmless-contract")
    persisted = State(
        boot_id=boot_id,
        controller_instance=real_contract.instance,
        display_identity=display_identity,
    )

    recovered = recover_state(
        persisted,
        current_boot_id=boot_id,
        controller_instance=ControllerInstanceId(uuid4()),
        display_identity=display_identity,
        namespace=StateNamespace.ACTIVE,
        scanner=scanner,
    )

    assert snapshot.ambiguities == ()
    assert forced.lifecycle is ActionLifecycle.TIMED_OUT
    assert _cgroup_process_count(cgroup_path) == 0
    assert recovered.authority_allowed
    assert forced in recovered.state.action_tombstones
    decision = reduce(
        recovered.state,
        ControllerStarted(
            EventMetadata(0, boot_id),
            ControllerInstanceId(uuid4()),
        ),
    )
    assert forced in decision.state.action_tombstones


def test_controller_restart_can_reattach_surviving_worker(
    real_contract: _RealContract,
) -> None:
    request = real_contract.request("sleep", delay_ms=10_000)
    unit = real_contract.unit(request)
    real_contract.supervisor.prepare_start(unit)
    real_contract.supervisor.start(
        unit,
        lambda: True,
        lambda: real_contract.store.claim_submission(request.action_id),
    )
    _wait_state(
        real_contract.supervisor,
        unit,
        WorkerActivity.ACTIVE,
        timeout_seconds=3,
    )
    _wait_until(
        lambda: (
            real_contract.store.execution_claim_if_present(request.action_id)
            is not None
        ),
        timeout_seconds=3,
    )

    restarted = SystemdSupervisor(
        systemctl=Path(shutil.which("systemctl") or "/usr/bin/systemctl"),
        unit_templates=real_contract.unit_templates,
    )
    assert restarted.reattach(unit).activity is WorkerActivity.ACTIVE
    snapshot = SystemdRecoveryScanner(real_contract.store, restarted).scan(
        StateNamespace.ACTIVE
    )
    assert unit in snapshot.units
    assert snapshot.ambiguities == ()
    real_contract.store.create_stop_intent(
        request.action_id,
        ActionLifecycle.CANCELLED,
    )
    restarted.stop(unit)
    assert restarted.reattach(unit).activity is WorkerActivity.INACTIVE


def test_real_previously_invoked_unit_without_result_cannot_restart(
    real_contract: _RealContract,
) -> None:
    action_id = ActionId(
        real_contract.instance,
        ActionKind.PROBE,
        real_contract.sequence + 20_000,
    )
    templates = dict(real_contract.unit_templates)
    templates[ActionKind.PROBE] = real_contract.no_result_template
    supervisor = SystemdSupervisor(
        systemctl=Path(shutil.which("systemctl") or "/usr/bin/systemctl"),
        unit_templates=templates,
    )
    unit = supervisor.unit_for_action(action_id)
    real_contract.units.append(unit)
    request = real_contract.store.create_request(
        TransactionRequest(
            action_id=action_id,
            action_kind=ActionKind.PROBE,
            unit_name=unit.unit_name,
            physical_epoch=1,
            physical_token=PhysicalToken("harmless-no-result"),
            admitted_event_generation=EventGeneration(0),
            observation_key=ObservationKey("harmless-no-result"),
            output_mapping=(),
            expected_topology=_TOPOLOGY,
            profile="harmless-probe-profile",
            payload=(
                ("base_identity_hash", "0" * 64),
                ("edid_integrity", "base_valid_extensions_invalid"),
                ("internal_output", "eDP-TEST"),
                ("preferred_mode", "1920x1080"),
                ("probe_output", "TEST-1"),
            ),
        )
    )

    def submission_guard() -> BoundTransactionRecord:
        return real_contract.store.claim_submission(request.action_id)

    assert supervisor.start(unit, lambda: True, submission_guard) is (
        DispatchStartResult.ACCEPTED
    )
    terminal = _wait_state(
        supervisor,
        unit,
        WorkerActivity.INACTIVE,
        timeout_seconds=3,
    )
    assert terminal.activity is WorkerActivity.INACTIVE
    assert real_contract.store.submission_claim_if_present(action_id) is not None
    assert real_contract.store.execution_claim_if_present(action_id) is None
    assert real_contract.store.result_if_present(action_id) is None
    with pytest.raises(SystemdSupervisorError, match="submission guard"):
        supervisor.start(unit, lambda: True, submission_guard)


def _probe_sysfs_tree(root: Path) -> Path:
    internal_value = next(
        line.split()[1]
        for line in _PROFILE_SETUP.read_text(encoding="ascii").splitlines()
        if line.startswith("eDP ")
    )
    values = (
        ("card0-eDP-1", 73, bytes.fromhex(internal_value.replace("*", "0"))),
        (
            "card0-DP-3",
            91,
            bytes.fromhex(
                (_EDID_FIXTURES / "samsung-broken-captured.hex").read_text(
                    encoding="ascii"
                )
            ),
        ),
    )
    for name, connector_id, edid in values:
        connector = root / name
        connector.mkdir(mode=0o700, parents=True)
        connector.joinpath("status").write_text("connected\n", encoding="ascii")
        connector.joinpath("connector_id").write_text(
            f"{connector_id}\n",
            encoding="ascii",
        )
        connector.joinpath("edid").write_bytes(edid)
    return root


def _write_fake_xrandr(path: Path, *, python: Path, log_path: Path) -> None:
    query = (_XRANDR_FIXTURES / "inactive.query").read_text(encoding="utf-8")
    properties = (_XRANDR_FIXTURES / "inactive.props").read_text(encoding="utf-8")
    source = (
        f"#!{python}\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"QUERY = {query!r}\n"
        f"PROPERTIES = {properties!r}\n"
        f"LOG = Path({str(log_path)!r})\n"
        "arguments = tuple(sys.argv[1:])\n"
        "with LOG.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(arguments) + '\\n')\n"
        "if arguments == ('--query',):\n"
        "    sys.stdout.write(QUERY)\n"
        "elif arguments == ('--props',):\n"
        "    sys.stdout.write(PROPERTIES)\n"
        f"elif arguments != {_PROBE_ARGV!r}:\n"
        "    raise SystemExit(64)\n"
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _production_probe_unit(
    *,
    python: Path,
    transaction_root: Path,
    sysfs_root: Path,
    fake_bin: Path,
) -> str:
    """Inject harmless dependencies while retaining the production entry point."""
    common = (
        f"{python} -I -m monitor_controller.workers.common "
        "record-systemd-result "
        f"--transaction-root {transaction_root} "
        "--action-id %I --unit %n --action-kind probe "
        "--service-result ${SERVICE_RESULT} --exit-code ${EXIT_CODE} "
        "--exit-status ${EXIT_STATUS}"
    )
    worker = (
        f"{python} -I -m monitor_controller.cli internal probe "
        f"--transaction-root {transaction_root} "
        f"--action-id %I --unit %n --sysfs-root {sysfs_root}"
    )
    production = (_STATIC_UNIT_DIRECTORY / "monitor-probe@.service").read_text(
        encoding="utf-8"
    )
    lines: list[str] = []
    for source_line in production.splitlines():
        rendered = source_line
        if source_line.startswith("Description="):
            rendered = "Description=Harmless production probe entry point (%I)"
        elif source_line.startswith("Environment=PATH="):
            rendered = f"Environment=PATH={fake_bin}:/usr/bin:/bin"
        elif source_line.startswith("ExecStart="):
            lines.extend(
                (
                    "Environment=DISPLAY=",
                    "Environment=XAUTHORITY=",
                    "Environment=WAYLAND_DISPLAY=",
                )
            )
            rendered = f"ExecStart={worker}"
        elif source_line.startswith("ExecStopPost="):
            rendered = f"ExecStopPost={common}"
        elif source_line.startswith("TimeoutStartSec="):
            rendered = "TimeoutStartSec=3s"
        elif source_line.startswith("TimeoutStopSec="):
            rendered = "TimeoutStopSec=500ms"
        lines.append(rendered)
    return "\n".join((*lines, ""))


def _contract_unit(*, python: Path, transaction_root: Path) -> str:
    """Substitute harmless commands into the tracked production apply contract."""
    common = (
        f"{python} -I -m monitor_controller.workers.common "
        "record-systemd-result "
        f"--transaction-root {transaction_root} "
        "--action-id %I --unit %n --action-kind application "
        "--service-result ${SERVICE_RESULT} --exit-code ${EXIT_CODE} "
        "--exit-status ${EXIT_STATUS}"
    )
    worker = (
        f"{python} -I -m monitor_controller.workers.test_worker "
        f"--transaction-root {transaction_root} "
        "--action-id %I --unit %n --action-kind application"
    )
    production = (_STATIC_UNIT_DIRECTORY / "monitor-apply@.service").read_text(
        encoding="utf-8"
    )
    lines: list[str] = []
    for source_line in production.splitlines():
        rendered = source_line
        if source_line.startswith("Description="):
            rendered = "Description=Harmless keyed production contract (%I)"
        elif source_line.startswith("ExecStart="):
            lines.extend(
                (
                    "Environment=DISPLAY=",
                    "Environment=XAUTHORITY=",
                    "Environment=WAYLAND_DISPLAY=",
                )
            )
            rendered = f"ExecStart={worker}"
        elif source_line.startswith("ExecStopPost="):
            rendered = f"ExecStopPost={common}"
        elif source_line.startswith("TimeoutStartSec="):
            rendered = "TimeoutStartSec=1s"
        elif source_line.startswith("TimeoutStopSec="):
            rendered = "TimeoutStopSec=500ms"
        lines.append(rendered)
    return "\n".join((*lines, ""))


def _rejected_start_unit() -> str:
    """Return a loaded harmless template which rejects manual starts."""
    return (
        "[Unit]\n"
        "Description=Harmless manager start rejection (%I)\n"
        "RefuseManualStart=yes\n"
        "CollectMode=inactive-or-failed\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/true\n"
        "RemainAfterExit=no\n"
    )


def _no_result_unit() -> str:
    """Return a harmless real unit which intentionally leaves no transaction."""
    return (
        "[Unit]\n"
        "Description=Harmless previously-invoked no-result contract (%I)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=DISPLAY=\n"
        "Environment=XAUTHORITY=\n"
        "Environment=WAYLAND_DISPLAY=\n"
        "ExecStart=/usr/bin/true\n"
        "TimeoutStartSec=5s\n"
        "TimeoutStartFailureMode=terminate\n"
        "TimeoutStopSec=500ms\n"
        "KillMode=control-group\n"
        "KillSignal=SIGTERM\n"
        "FinalKillSignal=SIGKILL\n"
        "SendSIGKILL=yes\n"
        "Restart=no\n"
        "RemainAfterExit=no\n"
        "UMask=0077\n"
    )


class _ResultDelayingRunner:
    def __init__(
        self,
        delegate: SystemctlCommandRunner,
        store: TransactionStore,
        request: TransactionRequest,
    ) -> None:
        self._delegate = delegate
        self._store = store
        self._request = request

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> SystemctlCommandResult:
        result = self._delegate.run(arguments, timeout_seconds=timeout_seconds)
        if "start" in arguments and result.returncode == 0:
            _wait_until(
                lambda: (
                    self._store.result_if_present(self._request.action_id) is not None
                ),
                timeout_seconds=5,
            )
        return result


def _result_delaying_runner(
    store: TransactionStore,
    request: TransactionRequest,
) -> SystemctlCommandRunner:
    return _ResultDelayingRunner(BoundedSystemctlRunner(), store, request)


def _wait_state(
    supervisor: SystemdSupervisor,
    unit: WorkerUnit,
    activity: WorkerActivity,
    *,
    timeout_seconds: float,
) -> SystemdUnitState:
    state = None

    def reached() -> bool:
        nonlocal state
        state = supervisor.reattach(unit)
        return state.activity is activity

    _wait_until(reached, timeout_seconds=timeout_seconds)
    assert state is not None
    return state


def _wait_until(predicate: Callable[[], bool], *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("timed out waiting for harmless systemd contract state")
        time.sleep(0.02)


def _unit_is_listed(executable: str, unit_name: str) -> bool:
    result = _systemctl(
        executable,
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        unit_name,
    )
    return any(line.split()[0] == unit_name for line in result.stdout.splitlines())


def _cgroup_path(control_group: str) -> Path:
    assert control_group.startswith("/")
    return Path("/sys/fs/cgroup") / control_group.removeprefix("/")


def _cgroup_process_count(path: Path) -> int:
    try:
        text = (path / "cgroup.procs").read_text(encoding="ascii")
    except FileNotFoundError:
        return 0
    return len(text.splitlines())


def _systemctl(
    executable: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        (executable, "--user", *arguments),
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )
