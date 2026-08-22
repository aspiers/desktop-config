"""Crash, serialization, and generation-race tests for the controller runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from monitor_controller.model import (
    ActionId,
    ActionKind,
    ActionLifecycle,
    BaseIdentityMatch,
    BootId,
    CanonicalObservation,
    ConfigurationContentHash,
    ConnectorIdentityEvidence,
    ControllerInstanceId,
    ControllerPhase,
    DisplayIdentity,
    EdidEvidence,
    EdidIntegrity,
    EventGeneration,
    EventMetadata,
    Fingerprint,
    ObservationCompleted,
    ObservationGeneration,
    ObservationKey,
    ObservationValidity,
    OutputMapping,
    PhysicalToken,
    PlanHash,
    ProfileMatch,
    ProfileScope,
    RawEvidenceReference,
    RawEvidenceSource,
    RequestPlan,
    State,
    TimerFired,
    WorkerUnit,
)
from monitor_controller.runtime.audit import RotatingAuditLog
from monitor_controller.runtime.controller import (
    RuntimeAuthorityError,
    SerializedController,
)
from monitor_controller.runtime.dispatcher import (
    DispatchEffect,
    DispatchStartResult,
    FinalDispatchFence,
    PreparedDispatch,
    WorkerActivity,
)

_BOOT = BootId(UUID(int=701))
_INSTANCE = ControllerInstanceId(UUID(int=702))
_CONFIG = (ConfigurationContentHash("layouts/dock.yaml", "sha256:dock"),)


class _Clock:
    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms
        self.waiters: list[tuple[int, asyncio.Event]] = []

    def monotonic_ms(self) -> int:
        return self.now_ms

    async def sleep_until(self, deadline_ms: int) -> None:
        if deadline_ms <= self.now_ms:
            await asyncio.sleep(0)
            return
        event = asyncio.Event()
        self.waiters.append((deadline_ms, event))
        await event.wait()

    def advance(self, now_ms: int) -> None:
        assert now_ms >= self.now_ms
        self.now_ms = now_ms
        for deadline, event in tuple(self.waiters):
            if deadline <= now_ms:
                event.set()
        self.waiters = [item for item in self.waiters if not item[1].is_set()]


class _Store:
    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.saved: list[State] = []

    def save(self, state: State) -> None:
        if self.crash:
            msg = "injected persistence crash"
            raise OSError(msg)
        self.saved.append(state)


class _FailingRuntimeAudit(RotatingAuditLog):
    def __init__(self, path: Path, initial_state: State, store: _Store) -> None:
        super().__init__(path, initial_state)
        self.store = store
        self.failure_states: list[State] = []

    def append_runtime_failure(
        self,
        *,
        boundary: str,
        detail: str,
        recorded_at_ms: int,
        action_id: str | None = None,
    ) -> None:
        del boundary, detail, recorded_at_ms, action_id
        assert self.store.saved
        self.failure_states.append(self.store.saved[-1])
        msg = "injected audit I/O failure"
        raise OSError(msg)


class _Observer:
    def __init__(self, *, exact: bool = False) -> None:
        self.calls = 0
        self.exact = exact
        self.generation_source: Callable[[], EventGeneration] = lambda: EventGeneration(
            0
        )

    async def observe(self) -> CanonicalObservation:
        self.calls += 1
        return _observation(
            exact=self.exact,
            observation_generation=self.calls + 1,
            event_generation=self.generation_source().value,
        )


class _Planner:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[RequestPlan] = []
        self.discards: list[object] = []
        self.hang_discard = False
        self.on_discard: Callable[[], None] | None = None

    async def create_plan(self, request: RequestPlan) -> PlanHash:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return PlanHash("plan-hash")

    async def discard_plan(self, request: object) -> None:
        self.discards.append(request)
        if self.on_discard is not None:
            self.on_discard()
        if self.hang_discard:
            await asyncio.Event().wait()


class _Dispatcher:
    def __init__(self) -> None:
        self.writes: list[DispatchEffect] = []
        self.starts: list[PreparedDispatch] = []
        self.submissions: list[PreparedDispatch] = []
        self.discards: list[PreparedDispatch] = []
        self.stops: list[ActionId] = []
        self.queries: list[WorkerUnit] = []
        self.write_failure: Exception | None = None
        self.start_failure: Exception | None = None
        self.after_write: Callable[[], None] | None = None
        self.before_final_fence: Callable[[], None] | None = None
        self.hang_after_submission = False
        self.start_result: object = DispatchStartResult.ACCEPTED
        self.activity = WorkerActivity.ACTIVE
        self.query_failure: Exception | None = None
        self.persisted_before_write: Callable[[], bool] = lambda: True

    async def write_request(self, effect: DispatchEffect) -> PreparedDispatch:
        assert self.persisted_before_write()
        self.writes.append(effect)
        if self.write_failure is not None:
            raise self.write_failure
        unit = WorkerUnit(effect.action_id, f"worker@{effect.action_id.value}.service")
        prepared = PreparedDispatch(
            effect.action_id,
            unit,
            f"request:{len(self.writes)}",
        )
        if self.after_write is not None:
            callback = self.after_write
            self.after_write = None
            callback()
        return prepared

    async def start(
        self,
        prepared: PreparedDispatch,
        final_fence: FinalDispatchFence,
    ) -> DispatchStartResult:
        self.starts.append(prepared)
        if self.before_final_fence is not None:
            callback = self.before_final_fence
            self.before_final_fence = None
            callback()
        if not final_fence():
            return DispatchStartResult.FENCE_REJECTED
        self.submissions.append(prepared)
        if self.start_failure is not None:
            raise self.start_failure
        if self.hang_after_submission:
            await asyncio.Event().wait()
        return self.start_result  # type: ignore[return-value]

    async def discard_prepared(self, prepared: PreparedDispatch) -> None:
        self.discards.append(prepared)

    async def stop(self, action_id: ActionId) -> None:
        self.stops.append(action_id)

    async def worker_activity(self, unit: WorkerUnit) -> WorkerActivity:
        self.queries.append(unit)
        if self.query_failure is not None:
            raise self.query_failure
        return self.activity


class _HangingDispatcher(_Dispatcher):
    async def write_request(self, effect: DispatchEffect) -> PreparedDispatch:
        self.writes.append(effect)
        await asyncio.Event().wait()
        message = "unreachable after adapter cancellation"
        raise AssertionError(message)


class _MismatchedStartDispatcher(_Dispatcher):
    async def start(
        self,
        prepared: PreparedDispatch,
        final_fence: FinalDispatchFence,
    ) -> DispatchStartResult:
        self.starts.append(prepared)
        assert final_fence()
        self.submissions.append(prepared)
        return WorkerUnit(prepared.action_id, "unexpected-worker.service")  # type: ignore[return-value]


class _HangingObserver(_Observer):
    async def observe(self) -> CanonicalObservation:
        self.calls += 1
        await asyncio.Event().wait()
        message = "unreachable after adapter cancellation"
        raise AssertionError(message)


class _BlockingObserver(_Observer):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def observe(self) -> CanonicalObservation:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return _observation(
            observation_generation=self.calls + 1,
            event_generation=self.generation_source().value,
        )


def _state(*, finalized: str | None = None) -> State:
    return State(
        boot_id=_BOOT,
        controller_instance=_INSTANCE,
        display_identity=DisplayIdentity(":runtime"),
        desktop_finalized_profile=finalized,
    )


def _observation(
    *,
    exact: bool = False,
    observation_generation: int = 1,
    event_generation: int = 0,
    at_ms: int = 0,
) -> CanonicalObservation:
    outputs = ("DP-1", "eDP-1")
    match = ProfileMatch(
        "dock",
        ProfileScope.MIXED,
        "layouts/dock.yaml",
        (OutputMapping("DP-SAVED", "DP-1"), OutputMapping("eDP-1", "eDP-1")),
        outputs,
        _CONFIG,
    )
    return CanonicalObservation(
        observed_at_ms=at_ms,
        observation_generation=ObservationGeneration(observation_generation),
        boot_id=_BOOT,
        physical_token=PhysicalToken("physical-dock"),
        begin_event_generation=EventGeneration(event_generation),
        end_event_generation=EventGeneration(event_generation),
        kernel_connected_outputs=outputs,
        kernel_external_outputs=("DP-1",),
        x_connected_outputs=outputs,
        x_active_outputs=outputs if exact else ("eDP-1",),
        x_external_outputs=("DP-1",),
        connector_identities=(ConnectorIdentityEvidence("DP-1", "card0-DP-1", 1, 1),),
        live_fingerprints=(
            Fingerprint("DP-1", "external"),
            Fingerprint("eDP-1", "internal"),
        ),
        base_identity_profiles=(BaseIdentityMatch("dock", "DP-1"),),
        edid_integrity=(EdidEvidence("DP-1", EdidIntegrity.COMPLETE, "base"),),
        probe_candidate=None,
        eligible_profiles=(match,),
        current_profiles=("dock",) if exact else (),
        exact_profile="dock" if exact else None,
        observation_key=ObservationKey("dock-key"),
        validity=ObservationValidity.VALID,
        invalidity_reason=None,
        raw_evidence=(
            RawEvidenceReference(
                RawEvidenceSource.DRM_CONNECTORS,
                "test:runtime",
                "sha256:runtime",
            ),
        ),
    )


def _event(observation: CanonicalObservation) -> ObservationCompleted:
    return ObservationCompleted(
        EventMetadata(observation.observed_at_ms, _BOOT), observation
    )


def _controller(  # noqa: PLR0913
    tmp_path: Path,
    *,
    state: State | None = None,
    store: _Store | None = None,
    observer: _Observer | None = None,
    planner: _Planner | None = None,
    dispatcher: _Dispatcher | None = None,
    audit: RotatingAuditLog | None = None,
) -> tuple[SerializedController, _Store, _Observer, _Planner, _Dispatcher, _Clock]:
    initial = state or _state()
    state_store = store or _Store()
    snapshot_observer = observer or _Observer()
    plan_adapter = planner or _Planner()
    action_dispatcher = dispatcher or _Dispatcher()
    clock = _Clock()
    controller = SerializedController(
        initial_state=initial,
        store=state_store,
        observer=snapshot_observer,
        planner=plan_adapter,
        dispatcher=action_dispatcher,
        audit=audit or RotatingAuditLog(tmp_path / "audit.jsonl", initial),
        clock=clock,
        adapter_timeout_seconds=0.05,
    )
    snapshot_observer.generation_source = controller.current_generation
    action_dispatcher.persisted_before_write = lambda: bool(
        state_store.saved
        and state_store.saved[-1].phase is ControllerPhase.APPLY_PENDING
    )
    return (
        controller,
        state_store,
        snapshot_observer,
        plan_adapter,
        action_dispatcher,
        clock,
    )


def test_unresolved_recovery_worker_cannot_authorize_runtime_dispatch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        action_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 1)
        unit = WorkerUnit(action_id, "worker@possibly-live.service")
        initial = replace(
            _state(),
            action_sequence_high_water=1,
            recovery_units=(unit,),
        )
        controller, store, _observer, _planner, dispatcher, _clock = _controller(
            tmp_path,
            state=initial,
        )

        decision = await controller.consume(_event(_observation()))

        assert decision.state == initial
        assert not decision.effects
        assert controller.state == initial
        assert store.saved == [initial]
        assert not dispatcher.writes
        assert not dispatcher.starts
        await controller.close()

    asyncio.run(exercise())


def test_persistence_crash_prevents_request_creation_and_state_replacement(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = _Store(crash=True)
        controller, _store, _observer, _planner, dispatcher, _clock = _controller(
            tmp_path, store=store
        )
        initial = controller.state

        with pytest.raises(OSError, match="persistence crash"):
            await controller.consume(_event(_observation()))

        assert controller.state == initial
        assert dispatcher.writes == []
        assert dispatcher.starts == []
        await controller.close()

    asyncio.run(exercise())


def test_udev_generation_is_incremented_before_enqueue_without_mutating_state(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path
        )
        initial = controller.state

        generation = controller.notify_drm_hint(observed_at_ms=4)

        assert generation == EventGeneration(1)
        assert controller.event_generation == EventGeneration(1)
        assert controller.state == initial
        assert controller.pending_event_count == 1
        await controller.close()

    asyncio.run(exercise())


def test_second_consumer_task_is_rejected(tmp_path: Path) -> None:
    async def exercise() -> None:
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path
        )
        timer = TimerFired(EventMetadata(0, _BOOT), 0)
        first = asyncio.create_task(controller.consume(timer))
        await first

        with pytest.raises(RuntimeAuthorityError, match="different queue consumer"):
            await controller.consume(timer)
        await controller.close()

    asyncio.run(exercise())


def test_queued_udev_hint_at_yield_forces_reobservation_and_zero_request(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller, _store, observer, _planner, dispatcher, _clock = _controller(
            tmp_path
        )

        async def enqueue_at_yield() -> None:
            controller.notify_drm_hint(observed_at_ms=1)

        producer = asyncio.create_task(enqueue_at_yield())
        await controller.consume(_event(_observation()))
        await producer

        assert observer.calls == 1
        assert dispatcher.writes == []
        assert dispatcher.starts == []
        assert controller.state.event_generation == EventGeneration(1)
        await controller.close()

    asyncio.run(exercise())


def test_pre_dispatch_drain_preserves_fifo_while_observation_awaits(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        observer = _BlockingObserver()
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path,
            observer=observer,
        )
        action_id = ActionId(_INSTANCE, ActionKind.APPLICATION, 1)

        async def produce_around_observation() -> None:
            controller.notify_worker_query_failure(
                action_id,
                "older deferred event",
                observed_at_ms=1,
            )
            controller.notify_drm_hint(observed_at_ms=2)
            await observer.started.wait()
            controller.notify_worker_query_failure(
                action_id,
                "newer event",
                observed_at_ms=3,
            )
            observer.release.set()

        producer = asyncio.create_task(produce_around_observation())
        await controller.consume(_event(_observation()))
        await producer
        await controller.process_available()

        records = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        ]
        worker_event_times = [
            record["event"]["metadata"]["processed_at_ms"]
            for record in records
            if record.get("event_type") == "WorkerStatusUnknown"
        ]
        assert worker_event_times == [1, 3]
        await controller.close()

    asyncio.run(exercise())


def test_udev_hint_after_request_creation_is_fenced_before_unit_start(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        dispatcher = _Dispatcher()
        controller, _store, observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, dispatcher=dispatcher
        )

        def enqueue_after_write() -> None:
            controller.notify_drm_hint(observed_at_ms=2)

        dispatcher.after_write = enqueue_after_write

        await controller.consume(_event(_observation()))

        assert observer.calls == 2
        assert len(dispatcher.writes) == 1
        assert dispatcher.starts == []
        assert len(dispatcher.discards) == 1
        assert controller.state.phase is ControllerPhase.APPLY_PENDING
        await controller.close()

    asyncio.run(exercise())


def test_udev_hint_at_final_submission_interval_guarantees_zero_launch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        dispatcher = _Dispatcher()
        controller, _store, observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, dispatcher=dispatcher
        )

        def enqueue_before_final_fence() -> None:
            controller.notify_drm_hint(observed_at_ms=3)

        dispatcher.before_final_fence = enqueue_before_final_fence

        await controller.consume(_event(_observation()))

        assert observer.calls == 2
        assert len(dispatcher.starts) == 1
        assert dispatcher.submissions == []
        assert len(dispatcher.discards) == 1
        assert controller.state.event_generation == EventGeneration(1)
        assert controller.state.phase is ControllerPhase.APPLY_PENDING
        await controller.close()

    asyncio.run(exercise())


def test_dispatch_adapter_timeout_becomes_an_explicit_reducer_failure(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        dispatcher = _HangingDispatcher()
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, dispatcher=dispatcher
        )

        await controller.consume(_event(_observation()))

        assert controller.state.phase is ControllerPhase.APPLY_FAILED
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.FAILED
        await controller.close()

    asyncio.run(exercise())


def test_request_write_crash_is_a_definite_dispatch_rejection(tmp_path: Path) -> None:
    async def exercise() -> None:
        dispatcher = _Dispatcher()
        dispatcher.write_failure = OSError("injected write crash")
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, dispatcher=dispatcher
        )

        await controller.consume(_event(_observation()))

        assert controller.state.phase is ControllerPhase.APPLY_FAILED
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.FAILED
        assert (
            controller.state.action_tombstones[-1].lifecycle is ActionLifecycle.FAILED
        )
        await controller.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "dispatcher_type",
    [
        pytest.param(_Dispatcher, id="start-timeout"),
        pytest.param(_MismatchedStartDispatcher, id="mismatched-unit-result"),
    ],
)
def test_post_submission_uncertainty_holds_mutation_exclusion(
    tmp_path: Path,
    dispatcher_type: type[_Dispatcher],
) -> None:
    async def exercise() -> None:
        dispatcher = dispatcher_type()
        dispatcher.activity = WorkerActivity.ACTIVE
        if dispatcher_type is _Dispatcher:
            dispatcher.hang_after_submission = True
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, dispatcher=dispatcher
        )

        await controller.consume(_event(_observation()))

        assert len(dispatcher.submissions) == 1
        assert dispatcher.stops
        assert dispatcher.queries
        assert controller.state.phase is ControllerPhase.APPLYING
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.STOPPING
        assert (
            controller.state.application.terminal_after_stop is ActionLifecycle.UNKNOWN
        )
        assert not controller.state.action_tombstones
        await controller.close()

    asyncio.run(exercise())


def test_uncertain_submission_persists_exclusion_before_diagnostic_audit(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        initial = _state()
        store = _Store()
        dispatcher = _Dispatcher()
        dispatcher.start_failure = OSError("possibly submitted")
        dispatcher.activity = WorkerActivity.ACTIVE
        audit = _FailingRuntimeAudit(tmp_path / "audit.jsonl", initial, store)
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path,
            state=initial,
            store=store,
            dispatcher=dispatcher,
            audit=audit,
        )

        with pytest.raises(OSError, match="audit I/O failure"):
            await controller.consume(_event(_observation()))

        persisted_lifecycles = tuple(
            saved.application.lifecycle
            for saved in store.saved
            if saved.application is not None
        )
        assert ActionLifecycle.DISPATCHED in persisted_lifecycles
        assert persisted_lifecycles[-1] is ActionLifecycle.STOPPING
        assert audit.failure_states[-1].application is not None
        assert (
            audit.failure_states[-1].application.lifecycle is ActionLifecycle.STOPPING
        )
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.STOPPING
        assert len(dispatcher.submissions) == 1
        await controller.close()

    asyncio.run(exercise())


def test_planner_crash_enqueues_plan_failed_and_cannot_wait_forever(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        planner = _Planner(OSError("injected planning crash"))
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path,
            state=_state(finalized="laptop"),
            planner=planner,
        )

        await controller.consume(_event(_observation(exact=True)))
        for _unused in range(5):
            await asyncio.sleep(0)
        await controller.process_available()

        assert planner.requests
        assert controller.state.planning_state.value == "plan_failed"
        assert controller.state.planning is not None
        assert controller.state.planning.lifecycle is ActionLifecycle.FAILED
        await controller.close()

    asyncio.run(exercise())


def test_observation_boot_change_is_reduced_before_reusing_monotonic_state(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        old_boot = BootId(UUID(int=700))
        initial = replace(_state(), boot_id=old_boot, next_timer_ms=0)
        controller, _store, observer, _planner, _dispatcher, _clock = _controller(
            tmp_path, state=initial
        )

        await controller.consume(TimerFired(EventMetadata(0, old_boot), 0))

        assert observer.calls == 2
        assert controller.state.boot_id == _BOOT
        assert controller.state.latest_observation is not None
        assert controller.event_generation == controller.state.event_generation
        await controller.close()

    asyncio.run(exercise())


def test_persisted_timer_progresses_without_another_drm_event(tmp_path: Path) -> None:
    async def exercise() -> None:
        controller, _store, observer, _planner, dispatcher, clock = _controller(
            tmp_path
        )
        unresolved = replace(
            _observation(),
            eligible_profiles=(),
            base_identity_profiles=(),
        )
        await controller.consume(_event(unresolved))
        deadline = controller.state.next_timer_ms
        assert deadline is not None
        assert observer.calls == 0

        await asyncio.sleep(0)
        clock.advance(deadline)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await controller.process_available()

        assert observer.calls == 2
        assert len(dispatcher.starts) == 1
        await controller.close()

    asyncio.run(exercise())


def test_worker_monitor_polls_and_times_out_at_persisted_deadline(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        dispatcher = _Dispatcher()
        controller, store, _observer, _planner, _dispatcher, clock = _controller(
            tmp_path,
            dispatcher=dispatcher,
        )

        await controller.consume(_event(_observation()))
        action = controller.state.application
        assert action is not None
        deadline_ms = action.worker_deadline_ms
        assert deadline_ms is not None
        persisted_action = store.saved[-1].application
        assert persisted_action is not None
        assert persisted_action.worker_deadline_ms == deadline_ms

        await asyncio.sleep(0)
        assert dispatcher.queries == [action.unit]
        clock.advance(deadline_ms)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await controller.process_available()

        timed_out = controller.state.application
        assert timed_out is not None
        assert timed_out.lifecycle is ActionLifecycle.STOPPING
        assert timed_out.terminal_after_stop is ActionLifecycle.TIMED_OUT
        assert dispatcher.stops == [action.action_id]
        await controller.close()

    asyncio.run(exercise())


def test_restart_monitor_marks_inactive_worker_with_lost_notification_unknown(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        first, _store, _observer, _planner, _dispatcher, _clock = _controller(tmp_path)
        await first.consume(_event(_observation()))
        persisted = first.state
        action = persisted.application
        assert action is not None
        assert action.worker_deadline_ms is not None
        await first.close()

        dispatcher = _Dispatcher()
        dispatcher.activity = WorkerActivity.INACTIVE
        restarted, _store2, _observer2, _planner2, _dispatcher2, _clock2 = _controller(
            tmp_path / "restart",
            state=persisted,
            dispatcher=dispatcher,
        )
        await restarted.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await restarted.process_available()

        terminal = restarted.state.application
        assert terminal is not None
        assert terminal.lifecycle is ActionLifecycle.UNKNOWN
        assert restarted.state.phase is ControllerPhase.APPLY_FAILED
        assert dispatcher.queries == [action.unit, action.unit]
        await restarted.close()

    asyncio.run(exercise())


def test_observation_timeout_is_explicit_and_rearms_authoritative_timer(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        observer = _HangingObserver()
        initial = replace(_state(), next_timer_ms=0)
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path,
            state=initial,
            observer=observer,
        )

        await controller.consume(TimerFired(EventMetadata(0, _BOOT), 0))

        assert observer.calls == 1
        assert controller.state.latest_observation is None
        assert controller.state.next_timer_ms == 1_000
        assert controller.scheduled_deadline_ms == 1_000
        assert '"record":"runtime_failure"' in (tmp_path / "audit.jsonl").read_text()
        await controller.close()

    asyncio.run(exercise())


def test_discard_plan_timeout_is_bounded_with_timer_already_armed(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        planner = _Planner()
        controller, _store, _observer, _planner, _dispatcher, _clock = _controller(
            tmp_path,
            state=_state(finalized="laptop"),
            observer=_Observer(exact=True),
            planner=planner,
        )
        await controller.consume(_event(_observation(exact=True)))
        for _unused in range(5):
            await asyncio.sleep(0)
        await controller.process_available()
        assert controller.state.planning_state.value == "plan_ready"

        armed_during_discard: list[int | None] = []
        planner.on_discard = lambda: armed_during_discard.append(
            controller.scheduled_deadline_ms
        )
        planner.hang_discard = True
        latest = controller.state.latest_observation
        assert latest is not None
        changed = replace(
            _observation(
                exact=True,
                observation_generation=controller.state.observation_generation.value
                + 1,
                at_ms=latest.observed_at_ms + 1,
            ),
            physical_token=PhysicalToken("changed-physical-dock"),
            observation_key=ObservationKey("changed-dock-key"),
        )

        await controller.consume(_event(changed))

        assert len(planner.discards) == 1
        assert armed_during_discard == [controller.state.next_timer_ms]
        text = (tmp_path / "audit.jsonl").read_text()
        assert '"boundary":"discard_plan"' in text
        await controller.close()

    asyncio.run(exercise())


def test_stop_acceptance_does_not_release_exclusion_before_inactive_evidence(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        dispatcher = _Dispatcher()
        dispatcher.activity = WorkerActivity.ACTIVE
        controller, _store, _observer, _planner, _dispatcher, clock = _controller(
            tmp_path, dispatcher=dispatcher
        )
        await controller.consume(_event(_observation()))
        action = controller.state.application
        assert action is not None
        deadline_ms = action.worker_deadline_ms
        assert deadline_ms is not None
        clock.advance(deadline_ms)

        controller.notify_worker_timeout(
            action.action_id,
            deadline_ms=deadline_ms,
            observed_at_ms=deadline_ms,
        )
        await controller.process_available()

        assert dispatcher.stops == [action.action_id]
        assert controller.state.phase is ControllerPhase.APPLYING
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.STOPPING
        assert (
            controller.state.application.terminal_after_stop
            is ActionLifecycle.TIMED_OUT
        )
        assert not controller.state.action_tombstones

        dispatcher.activity = WorkerActivity.INACTIVE
        await controller.consume(
            _event(
                _observation(
                    observation_generation=(
                        controller.state.observation_generation.value + 1
                    ),
                    event_generation=0,
                    at_ms=deadline_ms + 1,
                )
            )
        )

        assert dispatcher.stops == [action.action_id, action.action_id]
        assert controller.state.phase is ControllerPhase.APPLY_FAILED
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.TIMED_OUT
        await controller.close()

    asyncio.run(exercise())


def test_worker_timeout_is_an_explicit_event_and_holds_exclusion_until_stop_ack(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller, _store, _observer, _planner, dispatcher, clock = _controller(
            tmp_path
        )
        await controller.consume(_event(_observation()))
        action = controller.state.application
        assert action is not None
        deadline_ms = action.worker_deadline_ms
        assert deadline_ms is not None
        dispatcher.activity = WorkerActivity.INACTIVE
        clock.advance(deadline_ms)

        controller.notify_worker_timeout(
            action.action_id,
            deadline_ms=deadline_ms,
            observed_at_ms=deadline_ms,
        )
        await controller.process_available()

        assert dispatcher.stops == [action.action_id]
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.TIMED_OUT
        await controller.close()

    asyncio.run(exercise())


def test_query_failure_is_an_explicit_event_and_holds_exclusion_until_stop_ack(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        controller, _store, _observer, _planner, dispatcher, _clock = _controller(
            tmp_path
        )
        await controller.consume(_event(_observation()))
        action = controller.state.application
        assert action is not None
        assert controller.state.phase is ControllerPhase.APPLYING

        dispatcher.activity = WorkerActivity.INACTIVE
        controller.notify_worker_query_failure(
            action.action_id,
            "injected query crash",
            observed_at_ms=3,
        )
        await controller.process_available()

        assert dispatcher.stops == [action.action_id]
        assert controller.state.phase is ControllerPhase.APPLY_FAILED
        assert controller.state.application is not None
        assert controller.state.application.lifecycle is ActionLifecycle.UNKNOWN
        await controller.close()

    asyncio.run(exercise())
