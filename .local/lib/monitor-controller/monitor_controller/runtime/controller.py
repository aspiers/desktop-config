"""Single-consumer asyncio authority for monitor-controller decisions."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast

from monitor_controller.model import (
    BROKEN_EXTENSION_EDID_INTEGRITIES,
    ActionId,
    ActionKind,
    ActionLifecycle,
    ActivateProbe,
    AdmissionDirtied,
    ApplicationDispatched,
    ApplicationFinished,
    ApplyProfile,
    BootChanged,
    BootId,
    CanonicalObservation,
    ControllerStarted,
    Decision,
    DiscardPlan,
    DispatchRejected,
    DrmHintReceived,
    Effect,
    Event,
    EventGeneration,
    EventMetadata,
    FinalizationDispatched,
    FinalizationFinished,
    FinalizeDesktop,
    ObservationCompleted,
    ObservationFailed,
    PlanCompleted,
    PlanFailed,
    PlanHash,
    PlanRequested,
    PreparationDispatched,
    PreparationFinished,
    PrepareDesktop,
    ProbeDispatched,
    ProbeFinished,
    RequestObservation,
    RequestPlan,
    Schedule,
    State,
    StopAction,
    WorkerCancellationAcknowledged,
    WorkerOutcome,
    WorkerStatusUnknown,
    WorkerTimedOut,
    WorkerUnit,
)
from monitor_controller.reducer import reduce
from monitor_controller.runtime.audit import (
    DecisionAuditTiming,
    RotatingAuditLog,
)
from monitor_controller.runtime.dispatcher import (
    ActionDispatcher,
    DispatchAdapterError,
    DispatchEffect,
    DispatchStartResult,
    NullDispatcher,
    PreparedDispatch,
    WorkerActivity,
    WorkerCompletion,
    WorkerRequestContext,
)
from monitor_controller.runtime.scheduler import DeadlineScheduler, SchedulerClock
from monitor_controller.runtime.transactions import ExpectedTopology

DEFAULT_PLANNING_TIMEOUT_SECONDS = 30.0
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 10.0
WORKER_STATUS_POLL_MS = 1_000


class RuntimeAuthorityError(RuntimeError):
    """Raised when more than one asyncio task tries to consume controller events."""


class StateWriter(Protocol):
    """Synchronous durable state boundary owned only by the queue consumer."""

    def save(self, state: State) -> None:
        """Persist the complete state before any newly admitted work."""
        ...


class ObservationAdapter(Protocol):
    """Injected bounded canonical-observation boundary."""

    async def observe(self) -> CanonicalObservation:
        """Return one immutable generation-fenced canonical observation."""
        ...


class PlanningAdapter(Protocol):
    """Injected transaction-local pure planning boundary."""

    async def create_plan(self, request: RequestPlan) -> PlanHash:
        """Compute and stage an immutable plan without changing live desktop state."""
        ...

    async def discard_plan(self, request: DiscardPlan) -> None:
        """Remove superseded transaction-local staged artifacts."""
        ...


class SerializedController:
    """Own state reduction, persistence, scheduling, and dispatch in one task."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        initial_state: State,
        store: StateWriter,
        observer: ObservationAdapter,
        planner: PlanningAdapter,
        dispatcher: ActionDispatcher | NullDispatcher,
        audit: RotatingAuditLog,
        clock: SchedulerClock,
        planning_timeout_seconds: float = DEFAULT_PLANNING_TIMEOUT_SECONDS,
        adapter_timeout_seconds: float = DEFAULT_ADAPTER_TIMEOUT_SECONDS,
    ) -> None:
        """Bind explicit adapters without reading the display or starting a task."""
        if planning_timeout_seconds <= 0:
            msg = "planning timeout must be positive"
            raise ValueError(msg)
        if adapter_timeout_seconds <= 0:
            msg = "adapter timeout must be positive"
            raise ValueError(msg)
        self._state = initial_state
        self._store = store
        self._observer = observer
        self._planner = planner
        self._dispatcher = dispatcher
        self._audit = audit
        self._clock = clock
        self._planning_timeout_seconds = planning_timeout_seconds
        self._adapter_timeout_seconds = adapter_timeout_seconds
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._scheduler = DeadlineScheduler(self._queue, clock)
        self._event_generation = initial_state.event_generation.value
        self._consumer_task: asyncio.Task[object] | None = None
        self._planning_tasks: dict[ActionId, asyncio.Task[None]] = {}
        self._worker_monitor_tasks: dict[ActionId, asyncio.Task[None]] = {}
        self._worker_started_ms: dict[ActionId, int] = {}
        self._event_worker_duration_ms: dict[int, int] = {}
        self._pending_boot_observation: tuple[CanonicalObservation, int] | None = None
        self._fence_depth = 0
        self._started = False

    @property
    def state(self) -> State:
        """Return the current immutable authoritative state."""
        return self._state

    @property
    def event_generation(self) -> EventGeneration:
        """Return the producer-side generation used by observer and launch fences."""
        return EventGeneration(self._event_generation)

    @property
    def pending_event_count(self) -> int:
        """Return the number of serialized inputs waiting for the consumer."""
        return self._queue.qsize()

    @property
    def scheduled_deadline_ms(self) -> int | None:
        """Return the deadline currently armed by authoritative state."""
        return self._scheduler.deadline_ms

    def current_generation(self) -> EventGeneration:
        """Implement the canonical observer's generation-source protocol."""
        return self.event_generation

    def notify_drm_hint(self, *, observed_at_ms: int | None = None) -> EventGeneration:
        """Increment generation before atomically enqueueing one coalescible hint."""
        self._event_generation += 1
        generation = EventGeneration(self._event_generation)
        now_ms = (
            self._clock.monotonic_ms() if observed_at_ms is None else observed_at_ms
        )
        self._queue.put_nowait(
            DrmHintReceived(
                EventMetadata(now_ms, self._state.boot_id),
                generation,
            )
        )
        return generation

    def notify_worker_event(
        self,
        event: (
            ProbeFinished
            | ApplicationFinished
            | PreparationFinished
            | FinalizationFinished
            | WorkerCancellationAcknowledged
            | WorkerStatusUnknown
            | WorkerTimedOut
        ),
    ) -> None:
        """Enqueue worker truth without allowing the producer to touch state."""
        started_ms = self._worker_started_ms.get(event.action_id)
        if started_ms is not None:
            self._event_worker_duration_ms[id(event)] = max(
                0, event.metadata.processed_at_ms - started_ms
            )
        self._queue.put_nowait(event)

    def notify_worker_query_failure(
        self,
        action_id: ActionId,
        reason: str,
        *,
        observed_at_ms: int | None = None,
    ) -> None:
        """Convert a supervisor query failure into an explicit reducer event."""
        now_ms = (
            self._clock.monotonic_ms() if observed_at_ms is None else observed_at_ms
        )
        self.notify_worker_event(
            WorkerStatusUnknown(
                EventMetadata(now_ms, self._state.boot_id),
                action_id,
                _bounded_reason("query", reason),
            )
        )

    def notify_worker_timeout(
        self,
        action_id: ActionId,
        deadline_ms: int,
        *,
        observed_at_ms: int | None = None,
    ) -> None:
        """Convert a worker deadline into an explicit reducer event."""
        now_ms = (
            self._clock.monotonic_ms() if observed_at_ms is None else observed_at_ms
        )
        self.notify_worker_event(
            WorkerTimedOut(
                EventMetadata(now_ms, self._state.boot_id),
                action_id,
                deadline_ms,
            )
        )

    async def start(self) -> None:
        """Arm a persisted deadline and enqueue the explicit startup event once."""
        if self._started:
            return
        self._started = True
        self._scheduler.arm(self._state.next_timer_ms, self._state.boot_id)
        self._queue.put_nowait(
            ControllerStarted(
                EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                self._state.controller_instance,
            )
        )
        self._reconcile_worker_monitor()

    async def run(self) -> None:
        """Consume indefinitely; cancellation is the service shutdown boundary."""
        await self.start()
        while True:
            await self.process_next()

    async def process_next(self) -> None:
        """Consume exactly one queued event as the sole state authority."""
        self._claim_consumer()
        event = await self._queue.get()
        try:
            await self._process_event(event)
        finally:
            self._queue.task_done()

    async def consume(self, event: Event) -> Decision:
        """Consume an injected event directly, primarily for deterministic tests."""
        self._claim_consumer()
        return await self._process_event(event)

    async def process_available(self) -> int:
        """Consume the current finite queue snapshot without waiting for new input."""
        self._claim_consumer()
        count = self._queue.qsize()
        for _unused in range(count):
            event = self._queue.get_nowait()
            try:
                await self._process_event(event)
            finally:
                self._queue.task_done()
        return count

    async def close(self) -> None:
        """Cancel local planning, monitor, and timer producers only."""
        tasks = (
            *self._planning_tasks.values(),
            *self._worker_monitor_tasks.values(),
        )
        self._planning_tasks.clear()
        self._worker_monitor_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._scheduler.close()

    def _claim_consumer(self) -> None:
        current = asyncio.current_task()
        if current is None:
            msg = "controller consumption requires an asyncio task"
            raise RuntimeAuthorityError(msg)
        if self._consumer_task is None:
            self._consumer_task = cast("asyncio.Task[object]", current)
        elif self._consumer_task is not current:
            msg = "controller already has a different queue consumer"
            raise RuntimeAuthorityError(msg)

    async def _process_event(
        self,
        event: Event,
        *,
        observation_duration_ms: int | None = None,
        command_duration_ms: int | None = None,
    ) -> Decision:
        prior_state = self._state
        started_ms = self._clock.monotonic_ms()
        decision = reduce(prior_state, event)
        reduced_ms = max(started_ms, self._clock.monotonic_ms())
        self._store.save(decision.state)
        persisted_ms = max(reduced_ms, self._clock.monotonic_ms())
        self._state = decision.state
        self._event_generation = (
            decision.state.event_generation.value
            if isinstance(event, BootChanged)
            else max(
                self._event_generation,
                decision.state.event_generation.value,
            )
        )
        self._audit.append_decision(
            prior_state,
            event,
            decision,
            DecisionAuditTiming(
                started_ms,
                reduced_ms,
                persisted_ms,
                observation_duration_ms=observation_duration_ms,
                command_duration_ms=command_duration_ms,
                worker_duration_ms=self._event_worker_duration_ms.pop(id(event), None),
            ),
        )
        if isinstance(
            event,
            ProbeFinished
            | ApplicationFinished
            | PreparationFinished
            | FinalizationFinished
            | WorkerCancellationAcknowledged,
        ):
            self._worker_started_ms.pop(event.action_id, None)
        # Persisted time and worker supervision must be live before an adapter blocks.
        self._scheduler.arm(self._state.next_timer_ms, self._state.boot_id)
        self._reconcile_worker_monitor()
        for effect in decision.effects:
            await self._handle_effect(effect)
        # A recursive effect may have replaced the deadline while this decision's
        # remaining inert Schedule effects were processed.
        self._scheduler.arm(self._state.next_timer_ms, self._state.boot_id)
        return decision

    async def _handle_effect(self, effect: Effect) -> None:
        if isinstance(effect, RequestObservation):
            await self._request_observation()
            return
        if isinstance(effect, Schedule):
            # The authoritative current deadline is reconciled after all effects.
            return
        if isinstance(effect, RequestPlan):
            await self._request_plan(effect)
            return
        if isinstance(
            effect,
            ActivateProbe | ApplyProfile | PrepareDesktop | FinalizeDesktop,
        ):
            if self._fence_depth == 0:
                await self._dispatch(effect)
            return
        if isinstance(effect, StopAction):
            await self._stop_action(effect)
            return
        await self._discard_plan(effect)

    async def _request_observation(self) -> None:
        pending = self._pending_boot_observation
        self._pending_boot_observation = None
        if pending is None:
            observation_started_ms = self._clock.monotonic_ms()
            try:
                observation = await asyncio.wait_for(
                    self._observer.observe(),
                    timeout=self._adapter_timeout_seconds,
                )
            except Exception as error:  # noqa: BLE001 - adapter trust boundary
                observation_finished_ms = max(
                    observation_started_ms, self._clock.monotonic_ms()
                )
                reason = _bounded_reason("observation", _exception_detail(error))
                self._audit.append_runtime_failure(
                    boundary="observation",
                    detail=reason,
                    recorded_at_ms=observation_finished_ms,
                )
                await self._process_event(
                    ObservationFailed(
                        EventMetadata(observation_finished_ms, self._state.boot_id),
                        reason,
                    ),
                    observation_duration_ms=(
                        observation_finished_ms - observation_started_ms
                    ),
                )
                return
            observation_finished_ms = max(
                observation_started_ms, self._clock.monotonic_ms()
            )
            observation_duration_ms = observation_finished_ms - observation_started_ms
        else:
            observation, observation_duration_ms = pending
            observation_finished_ms = self._clock.monotonic_ms()

        if observation.boot_id != self._state.boot_id:
            self._pending_boot_observation = (
                observation,
                observation_duration_ms,
            )
            await self._process_event(
                BootChanged(
                    EventMetadata(observation_finished_ms, observation.boot_id),
                    self._state.boot_id,
                )
            )
            return

        await self._process_event(
            ObservationCompleted(
                EventMetadata(observation_finished_ms, observation.boot_id),
                observation,
            ),
            observation_duration_ms=observation_duration_ms,
        )

    async def _request_plan(self, request: RequestPlan) -> None:
        if request.action_id in self._planning_tasks:
            return
        accepted = PlanRequested(
            EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
            request.action_id,
            request.input_key,
        )
        await self._process_event(accepted)
        action = self._state.planning
        if (
            action is None
            or action.action_id != request.action_id
            or action.lifecycle is not ActionLifecycle.DISPATCHED
        ):
            return
        task = asyncio.create_task(
            self._run_plan(request, self._state.boot_id),
            name=f"monitor-controller-plan-{request.action_id.value}",
        )
        self._planning_tasks[request.action_id] = task

    async def _run_plan(self, request: RequestPlan, boot_id: BootId) -> None:
        try:
            plan_hash = await asyncio.wait_for(
                self._planner.create_plan(request),
                timeout=self._planning_timeout_seconds,
            )
            event: Event = PlanCompleted(
                EventMetadata(self._clock.monotonic_ms(), boot_id),
                request.action_id,
                request.input_key,
                plan_hash,
            )
        except Exception as error:  # noqa: BLE001 - explicit adapter trust boundary
            event = PlanFailed(
                EventMetadata(self._clock.monotonic_ms(), boot_id),
                request.action_id,
                request.input_key,
                _exception_detail(error),
            )
        finally:
            self._planning_tasks.pop(request.action_id, None)
        self._queue.put_nowait(event)

    async def _dispatch(self, effect: DispatchEffect) -> None:
        if not await self._pre_dispatch_fence(effect):
            return
        if isinstance(self._dispatcher, NullDispatcher):
            await self._record_null_dispatch(effect)
            return
        dispatch_started_ms = self._clock.monotonic_ms()
        prepared = await self._write_dispatch_request(effect)
        if prepared is None:
            return
        if not await self._pre_dispatch_fence(effect):
            await self._discard_prepared(prepared)
            return
        await self._start_prepared(effect, prepared, dispatch_started_ms)

    async def _record_null_dispatch(self, effect: DispatchEffect) -> None:
        if not isinstance(self._dispatcher, NullDispatcher):
            return
        record = self._dispatcher.record(effect, self._clock.monotonic_ms())
        self._audit.append_would_dispatch(record)
        await self._process_event(
            DispatchRejected(
                EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                effect.action_id,
                "null dispatcher: worker launch intentionally unavailable",
            )
        )

    async def _write_dispatch_request(
        self, effect: DispatchEffect
    ) -> PreparedDispatch | None:
        if isinstance(self._dispatcher, NullDispatcher):
            return None
        try:
            context = self._worker_request_context(effect)
            prepared = await asyncio.wait_for(
                self._dispatcher.write_request(effect, context),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - explicit adapter trust boundary
            await self._reject_dispatch(effect, "request_write", error)
            return None
        if prepared.action_id == effect.action_id:
            return prepared
        await self._discard_prepared(prepared)
        await self._reject_dispatch(
            effect,
            "request_write",
            ValueError("prepared request has a different action ID"),
        )
        return None

    def _worker_request_context(self, effect: DispatchEffect) -> WorkerRequestContext:
        observation = self._state.latest_observation
        if (
            observation is None
            or not observation.valid
            or observation.observation_key != effect.observation_key
            or observation.event_generation != effect.admitted_event_generation
            or self._state.physical_token != observation.physical_token
        ):
            msg = "admitted effect lacks its exact persisted observation proof"
            raise ValueError(msg)
        layout: str | None = None
        probe_base_hash: str | None = None
        probe_edid_integrity = None
        profile_configuration_hashes = ()
        if isinstance(effect, ActivateProbe):
            if effect.key.physical_epoch != self._state.physical_epoch:
                msg = "probe effect physical epoch is no longer current"
                raise ValueError(msg)
            candidate = observation.probe_candidate
            identity_matches = tuple(
                item
                for item in observation.base_identity_profiles
                if item.profile == effect.key.profile and item.output == effect.output
            )
            edid = next(
                (
                    item
                    for item in observation.edid_integrity
                    if item.output == effect.output
                ),
                None,
            )
            if (
                candidate is None
                or candidate.output != effect.output
                or candidate.internal_output != effect.internal_output
                or candidate.preferred_mode != effect.preferred_mode
                or candidate.profile != effect.key.profile
                or len(identity_matches) != 1
                or edid is None
                or edid.base_hash is None
                or edid.integrity not in BROKEN_EXTENSION_EDID_INTEGRITIES
            ):
                msg = "probe effect lacks exact broken-extension identity proof"
                raise ValueError(msg)
            probe_base_hash = edid.base_hash
            probe_edid_integrity = edid.integrity
            mapping = ()
        elif isinstance(effect, ApplyProfile):
            if (
                effect.key.physical_epoch != self._state.physical_epoch
                or effect.mapping.physical_epoch != self._state.physical_epoch
                or effect.mapping.observation_key != effect.observation_key
            ):
                msg = "application effect proof is outside its admitted epoch"
                raise ValueError(msg)
            matching_profiles = tuple(
                item
                for item in observation.eligible_profiles
                if item.profile == effect.profile
                and item.mapping == effect.mapping.outputs
            )
            if len(matching_profiles) != 1:
                msg = "application effect lacks one exact saved-profile proof"
                raise ValueError(msg)
            mapping = effect.mapping.outputs
            profile_configuration_hashes = matching_profiles[0].configuration_hashes
        else:
            candidate = self._state.candidate
            planning = self._state.planning
            if (
                candidate is None
                or candidate.observation_key != effect.observation_key
                or candidate.mapping.physical_epoch != self._state.physical_epoch
                or planning is None
                or planning.transition_id != effect.transition_id
                or planning.plan_hash != effect.plan_hash
                or planning.profile != effect.profile
                or planning.input_key.physical_epoch != self._state.physical_epoch
                or planning.input_key.observation_key != effect.observation_key
                or planning.input_key.mapping != candidate.mapping.outputs
            ):
                msg = "desktop effect lacks its exact mapping and plan proof"
                raise ValueError(msg)
            mapping = candidate.mapping.outputs
            layout = planning.input_key.layout
        return WorkerRequestContext(
            physical_epoch=self._state.physical_epoch,
            physical_token=observation.physical_token,
            output_mapping=mapping,
            expected_topology=ExpectedTopology(
                kernel_connected_outputs=observation.kernel_connected_outputs,
                kernel_external_outputs=observation.kernel_external_outputs,
                x_connected_outputs=observation.x_connected_outputs,
                x_active_outputs=observation.x_active_outputs,
            ),
            layout=layout,
            probe_base_hash=probe_base_hash,
            probe_edid_integrity=probe_edid_integrity,
            profile_configuration_hashes=profile_configuration_hashes,
        )

    async def _start_prepared(
        self,
        effect: DispatchEffect,
        prepared: PreparedDispatch,
        dispatch_started_ms: int,
    ) -> None:
        if isinstance(self._dispatcher, NullDispatcher):
            return
        try:
            result = await asyncio.wait_for(
                self._dispatcher.start(
                    prepared,
                    lambda: self._final_dispatch_fence_passes(effect),
                ),
                timeout=self._adapter_timeout_seconds,
            )
        except DispatchAdapterError as error:
            if error.completion is None:
                # Without terminal evidence this contract guarantees no submission.
                await self._discard_prepared(prepared)
                await self._reject_dispatch(effect, "unit_start", error)
            else:
                await self._complete_definitely_rejected_start(
                    effect,
                    prepared,
                    error,
                    dispatch_started_ms,
                )
        except Exception as error:  # noqa: BLE001 - post-submission uncertainty
            await self._mark_dispatch_uncertain(
                effect,
                prepared,
                "unit_start",
                error,
                dispatch_started_ms,
            )
        else:
            await self._handle_start_result(
                effect,
                prepared,
                result,
                dispatch_started_ms,
            )

    async def _complete_definitely_rejected_start(
        self,
        effect: DispatchEffect,
        prepared: PreparedDispatch,
        error: DispatchAdapterError,
        dispatch_started_ms: int,
    ) -> None:
        """Persist the exact unit and immutable manager-rejection result."""
        completion = error.completion
        if (
            completion is None
            or completion.action_id != effect.action_id
            or completion.terminal_lifecycle is not ActionLifecycle.FAILED
        ):
            await self._mark_dispatch_uncertain(
                effect,
                prepared,
                "unit_start_terminal_result",
                error,
                dispatch_started_ms,
            )
            return
        await self._acknowledge_dispatch(effect, prepared, dispatch_started_ms)
        finished_ms = max(dispatch_started_ms, self._clock.monotonic_ms())
        await self._process_event(
            _finished_event(
                completion,
                EventMetadata(finished_ms, self._state.boot_id),
            )
        )
        self._audit.append_runtime_failure(
            boundary="unit_start",
            detail=str(error),
            recorded_at_ms=finished_ms,
            action_id=effect.action_id.value,
        )

    async def _handle_start_result(
        self,
        effect: DispatchEffect,
        prepared: PreparedDispatch,
        result: object,
        dispatch_started_ms: int,
    ) -> None:
        if result is DispatchStartResult.FENCE_REJECTED:
            await self._handle_final_fence_rejection(effect, prepared)
        elif result is DispatchStartResult.ACCEPTED:
            await self._acknowledge_dispatch(effect, prepared, dispatch_started_ms)
        else:
            await self._mark_dispatch_uncertain(
                effect,
                prepared,
                "unit_start_result",
                TypeError(f"unexpected start result: {result!r}"),
                dispatch_started_ms,
            )

    async def _handle_final_fence_rejection(
        self, effect: DispatchEffect, prepared: PreparedDispatch
    ) -> None:
        await self._discard_prepared(prepared)
        if self.event_generation != effect.admitted_event_generation:
            await self._record_dirty_admission(effect)
        elif self._effect_is_current(effect):
            await self._reject_dispatch(
                effect,
                "unit_start",
                RuntimeError("adapter rejected a passing final fence"),
            )

    async def _acknowledge_dispatch(
        self,
        effect: DispatchEffect,
        prepared: PreparedDispatch,
        dispatch_started_ms: int,
    ) -> None:
        dispatch_finished_ms = max(dispatch_started_ms, self._clock.monotonic_ms())
        self._worker_started_ms[effect.action_id] = dispatch_finished_ms
        await self._process_event(
            _dispatched_event(
                effect,
                prepared.unit,
                EventMetadata(dispatch_finished_ms, self._state.boot_id),
            ),
            command_duration_ms=dispatch_finished_ms - dispatch_started_ms,
        )

    def _final_dispatch_fence_passes(self, effect: DispatchEffect) -> bool:
        """Check admission immediately before the adapter's non-yielding submit."""
        return (
            self.event_generation == effect.admitted_event_generation
            and self._effect_is_current(effect)
        )

    async def _record_dirty_admission(self, effect: DispatchEffect) -> None:
        # Re-observation may re-emit the same admission, but the dirty boundary
        # itself must return to the queue before any replacement can dispatch.
        self._fence_depth += 1
        try:
            await self._process_event(
                AdmissionDirtied(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    self.event_generation,
                )
            )
        finally:
            self._fence_depth -= 1

    async def _mark_dispatch_uncertain(
        self,
        effect: DispatchEffect,
        prepared: PreparedDispatch,
        boundary: str,
        error: Exception,
        dispatch_started_ms: int,
    ) -> None:
        """Persist exclusion and begin stop/query recovery after possible submit."""
        reason = _bounded_reason(boundary, _exception_detail(error))
        finished_ms = max(dispatch_started_ms, self._clock.monotonic_ms())
        self._worker_started_ms[effect.action_id] = finished_ms
        # Submission may already have succeeded. Persist its worker identity first,
        # then persist the stopping exclusion before attempting diagnostic audit I/O.
        # An audit failure is fatal to this consume call but cannot restore ADMITTED
        # authority or permit a second mutation.
        await self._process_event(
            _dispatched_event(
                effect,
                prepared.unit,
                EventMetadata(finished_ms, self._state.boot_id),
            ),
            command_duration_ms=finished_ms - dispatch_started_ms,
        )
        await self._process_event(
            WorkerStatusUnknown(
                EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                effect.action_id,
                reason,
            )
        )
        self._audit.append_runtime_failure(
            boundary=boundary,
            detail=reason,
            recorded_at_ms=finished_ms,
            action_id=effect.action_id.value,
        )

    async def _pre_dispatch_fence(self, effect: DispatchEffect) -> bool:
        await asyncio.sleep(0)
        self._fence_depth += 1
        try:
            await self._drain_pre_dispatch_events()
        finally:
            self._fence_depth -= 1
        current_generation = self.event_generation
        if current_generation != effect.admitted_event_generation:
            await self._record_dirty_admission(effect)
            return False
        return self._effect_is_current(effect)

    async def _drain_pre_dispatch_events(self) -> None:
        # Consume the finite snapshot in queue order. Processing only DRM hints and
        # re-appending older deferred events would place them behind events arriving
        # while a hint-triggered observation awaited its adapter.
        count = self._queue.qsize()
        for _unused in range(count):
            event = self._queue.get_nowait()
            try:
                await self._process_event(event)
            finally:
                self._queue.task_done()

    def _effect_is_current(self, effect: DispatchEffect) -> bool:
        action = {
            ActionKind.PROBE: self._state.probe,
            ActionKind.APPLICATION: self._state.application,
            ActionKind.PREPARATION: self._state.preparation,
            ActionKind.FINALIZATION: self._state.finalization,
        }[effect.action_id.kind]
        return (
            action is not None
            and action.action_id == effect.action_id
            and action.lifecycle is ActionLifecycle.ADMITTED
            and action.admitted_event_generation == effect.admitted_event_generation
        )

    async def _reject_dispatch(
        self,
        effect: DispatchEffect,
        boundary: str,
        error: Exception,
    ) -> None:
        reason = (
            str(error)
            if isinstance(error, DispatchAdapterError)
            else _bounded_reason(boundary, _exception_detail(error))
        )
        self._audit.append_runtime_failure(
            boundary=boundary,
            detail=reason,
            recorded_at_ms=self._clock.monotonic_ms(),
            action_id=effect.action_id.value,
        )
        await self._process_event(
            DispatchRejected(
                EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                effect.action_id,
                reason,
            )
        )

    async def _discard_prepared(self, prepared: PreparedDispatch) -> None:
        if isinstance(self._dispatcher, NullDispatcher):
            return
        try:
            await asyncio.wait_for(
                self._dispatcher.discard_prepared(prepared),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - diagnostic cleanup boundary
            self._audit.append_runtime_failure(
                boundary="prepared_request_cleanup",
                detail=_exception_detail(error),
                recorded_at_ms=self._clock.monotonic_ms(),
                action_id=prepared.action_id.value,
            )

    async def _stop_action(self, effect: StopAction) -> None:  # noqa: PLR0911
        if isinstance(self._dispatcher, NullDispatcher):
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    "null dispatcher cannot prove worker inactivity or outcome",
                )
            )
            return
        action = {
            ActionKind.PROBE: self._state.probe,
            ActionKind.APPLICATION: self._state.application,
            ActionKind.PREPARATION: self._state.preparation,
            ActionKind.FINALIZATION: self._state.finalization,
        }[effect.action_id.kind]
        if (
            action is None
            or action.action_id != effect.action_id
            or action.lifecycle is not ActionLifecycle.STOPPING
            or action.unit is None
        ):
            return
        try:
            terminal_lifecycle = action.terminal_after_stop or ActionLifecycle.CANCELLED
            await asyncio.wait_for(
                self._dispatcher.stop(effect.action_id, terminal_lifecycle),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - explicit adapter trust boundary
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    _bounded_reason("stop", _exception_detail(error)),
                )
            )
            return
        try:
            activity = await asyncio.wait_for(
                self._dispatcher.worker_activity(action.unit),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - explicit adapter trust boundary
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    _bounded_reason("query_after_stop", _exception_detail(error)),
                )
            )
            return
        if activity is WorkerActivity.ACTIVE:
            return
        if activity is not WorkerActivity.INACTIVE:
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    _bounded_reason(
                        "query_after_stop",
                        f"unexpected worker activity: {activity!r}",
                    ),
                )
            )
            return
        try:
            completion = await asyncio.wait_for(
                self._dispatcher.worker_completion(action.unit),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - explicit adapter trust boundary
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    _bounded_reason(
                        "result_after_stop",
                        _exception_detail(error),
                    ),
                )
            )
            return
        if completion is None or completion.action_id != effect.action_id:
            await self._process_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                    effect.action_id,
                    "result_after_stop: inactive worker lacks its exact result",
                )
            )
            return
        await self._process_event(
            WorkerCancellationAcknowledged(
                EventMetadata(self._clock.monotonic_ms(), self._state.boot_id),
                effect.action_id,
                completion.terminal_lifecycle,
                completion.exit_status,
            )
        )

    async def _discard_plan(self, effect: DiscardPlan) -> None:
        try:
            await asyncio.wait_for(
                self._planner.discard_plan(effect),
                timeout=self._adapter_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - diagnostic cleanup boundary
            self._audit.append_runtime_failure(
                boundary="discard_plan",
                detail=_exception_detail(error),
                recorded_at_ms=self._clock.monotonic_ms(),
                action_id=effect.action_id.value,
            )

    def _reconcile_worker_monitor(self) -> None:
        expected = next(
            (
                (action.action_id, action.unit, action.worker_deadline_ms)
                for action in (
                    self._state.probe,
                    self._state.application,
                    self._state.preparation,
                    self._state.finalization,
                )
                if action is not None
                and action.lifecycle is ActionLifecycle.DISPATCHED
                and action.unit is not None
                and action.worker_deadline_ms is not None
            ),
            None,
        )
        expected_id = None if expected is None else expected[0]
        for action_id, task in tuple(self._worker_monitor_tasks.items()):
            if action_id != expected_id:
                task.cancel()
                self._worker_monitor_tasks.pop(action_id, None)
        if expected is None or expected_id in self._worker_monitor_tasks:
            return
        action_id, unit, deadline_ms = expected
        task = asyncio.create_task(
            self._monitor_worker(
                action_id,
                unit,
                deadline_ms,
                self._state.boot_id,
            ),
            name=f"monitor-controller-worker-monitor-{action_id.value}",
        )
        self._worker_monitor_tasks[action_id] = task

    async def _monitor_worker(
        self,
        action_id: ActionId,
        unit: WorkerUnit,
        deadline_ms: int,
        boot_id: BootId,
    ) -> None:
        """Poll one acknowledged worker until explicit result, loss, or timeout."""
        dispatcher = self._dispatcher
        if isinstance(dispatcher, NullDispatcher):
            self.notify_worker_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), boot_id),
                    action_id,
                    "supervisor: null dispatcher cannot own an acknowledged worker",
                )
            )
            return
        try:
            while True:
                now_ms = self._clock.monotonic_ms()
                if now_ms >= deadline_ms:
                    self.notify_worker_event(
                        WorkerTimedOut(
                            EventMetadata(now_ms, boot_id),
                            action_id,
                            deadline_ms,
                        )
                    )
                    return
                activity = await asyncio.wait_for(
                    dispatcher.worker_activity(unit),
                    timeout=self._adapter_timeout_seconds,
                )
                if activity is WorkerActivity.INACTIVE:
                    completion = await asyncio.wait_for(
                        dispatcher.worker_completion(unit),
                        timeout=self._adapter_timeout_seconds,
                    )
                    if completion is None:
                        self.notify_worker_event(
                            WorkerStatusUnknown(
                                EventMetadata(self._clock.monotonic_ms(), boot_id),
                                action_id,
                                "supervisor: worker inactive without an exact result",
                            )
                        )
                    elif completion.action_id != action_id:
                        self.notify_worker_event(
                            WorkerStatusUnknown(
                                EventMetadata(self._clock.monotonic_ms(), boot_id),
                                action_id,
                                "supervisor: terminal result action identity differs",
                            )
                        )
                    else:
                        self.notify_worker_event(
                            _completion_event(
                                completion,
                                EventMetadata(self._clock.monotonic_ms(), boot_id),
                                deadline_ms,
                            )
                        )
                    return
                if activity is not WorkerActivity.ACTIVE:
                    self.notify_worker_event(
                        WorkerStatusUnknown(
                            EventMetadata(self._clock.monotonic_ms(), boot_id),
                            action_id,
                            _bounded_reason(
                                "worker_query",
                                f"unexpected worker activity: {activity!r}",
                            ),
                        )
                    )
                    return
                next_poll_ms = min(
                    deadline_ms,
                    self._clock.monotonic_ms() + WORKER_STATUS_POLL_MS,
                )
                await self._clock.sleep_until(next_poll_ms)
        except Exception as error:  # noqa: BLE001 - supervisor trust boundary
            self.notify_worker_event(
                WorkerStatusUnknown(
                    EventMetadata(self._clock.monotonic_ms(), boot_id),
                    action_id,
                    _bounded_reason("worker_query", _exception_detail(error)),
                )
            )


def _completion_event(
    completion: WorkerCompletion,
    metadata: EventMetadata,
    deadline_ms: int,
) -> (
    ProbeFinished
    | ApplicationFinished
    | PreparationFinished
    | FinalizationFinished
    | WorkerStatusUnknown
    | WorkerTimedOut
):
    """Translate exact persisted terminal semantics into reducer events."""
    if completion.terminal_lifecycle is ActionLifecycle.UNKNOWN:
        return WorkerStatusUnknown(
            metadata,
            completion.action_id,
            "supervisor: exact terminal transaction outcome is unknown",
        )
    if completion.terminal_lifecycle is ActionLifecycle.TIMED_OUT:
        return WorkerTimedOut(
            metadata,
            completion.action_id,
            deadline_ms,
            manager_confirmed=True,
        )
    return _finished_event(completion, metadata)


def _finished_event(
    completion: WorkerCompletion,
    metadata: EventMetadata,
) -> ProbeFinished | ApplicationFinished | PreparationFinished | FinalizationFinished:
    action_id = completion.action_id
    outcome = {
        ActionLifecycle.COMPLETED: WorkerOutcome.SUCCEEDED,
        ActionLifecycle.FAILED: WorkerOutcome.FAILED,
        ActionLifecycle.CANCELLED: WorkerOutcome.CANCELLED,
    }.get(completion.terminal_lifecycle)
    if outcome is None:
        msg = "unknown/timed-out completion requires its dedicated reducer event"
        raise ValueError(msg)
    if action_id.kind is ActionKind.PROBE:
        return ProbeFinished(
            metadata,
            action_id,
            outcome,
            completion.exit_status,
        )
    if action_id.kind is ActionKind.APPLICATION:
        return ApplicationFinished(
            metadata,
            action_id,
            outcome,
            completion.exit_status,
        )
    if action_id.kind is ActionKind.PREPARATION:
        if completion.plan_hash is None:
            msg = "preparation result lacks its bound plan hash"
            raise ValueError(msg)
        return PreparationFinished(
            metadata,
            action_id,
            outcome,
            completion.exit_status,
            completion.plan_hash,
        )
    if action_id.kind is ActionKind.FINALIZATION:
        return FinalizationFinished(
            metadata,
            action_id,
            outcome,
            completion.exit_status,
        )
    msg = "planning actions cannot produce systemd worker completion"
    raise ValueError(msg)


def _dispatched_event(
    effect: DispatchEffect,
    unit: WorkerUnit,
    metadata: EventMetadata,
) -> (
    ProbeDispatched
    | ApplicationDispatched
    | PreparationDispatched
    | FinalizationDispatched
):
    if isinstance(effect, ActivateProbe):
        return ProbeDispatched(metadata, effect.action_id, unit)
    if isinstance(effect, ApplyProfile):
        return ApplicationDispatched(metadata, effect.action_id, unit)
    if isinstance(effect, PrepareDesktop):
        return PreparationDispatched(metadata, effect.action_id, unit)
    return FinalizationDispatched(metadata, effect.action_id, unit)


def _exception_detail(error: Exception) -> str:
    return _bounded_reason(type(error).__name__, str(error))


def _bounded_reason(boundary: str, detail: str) -> str:
    clean_boundary = " ".join(boundary.split())[:64] or "adapter"
    clean_detail = " ".join(detail.split())[:448] or "unspecified failure"
    return f"{clean_boundary}: {clean_detail}"
