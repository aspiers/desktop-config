# Monitor Controller Python Architecture

## Status

**Accepted architecture. Substantially implemented; not yet authoritative.**
The authoritative service remains `monitor-watcher-ng.service`, executing
`bin/monitor-watcher-ng`.

What exists today:

- The observer, reducer, planner, persistence, recovery, and all four action
  workers, under `.local/lib/monitor-controller/`.
- `monitor-controller-shadow.service`, deployed and running. It observes and
  records decisions but hard-wires `NullDispatcher`, so it cannot act on the
  display.
- `monitor-controller.service` and `monitor_controller.active`, the
  exclusive-authority composition root. Stowed but **deliberately not
  enabled**, so a deployed-but-unverified unit cannot take authority at the
  next login.
- Cutover preflight and rollback (`monitor_controller.cutover`), exposed as
  the `preflight`, `cutover-commands`, and `rollback-commands` subcommands.

What gates the cutover: shadow traces for the seven scenarios in `dc-a5y.11`
must be captured and reconciled, and the live switch needs explicit
maintainer approval. Update this section to "authoritative" only once
`monitor-controller.service` is actually enabled and running.

This document selects the production technology and process boundaries for the
state model in `monitor-watcher-state-machine-v2.md`. The Bash reducer under
`specs/spikes/` remains a non-authoritative executable specification; it is not
the intended production implementation.

Tracked by `dc-a5y` — **Replace monitor watcher loop with a persistent state
machine**.

## Decision

Implement the controller in **Python 3.13** as:

- one persistent, single-authority `asyncio` controller;
- one pure, typed, deterministic reducer;
- modular observation, persistence, scheduling, and dispatch adapters;
- short-lived, keyed systemd user workers for side effects;
- a null dispatcher for shadow mode; and
- language-neutral scenarios plus JSONL capture/replay.

Use a project-local `uv` environment and lock file. Runtime code should use
frozen dataclasses, enums, explicit unions, `match`, and static checking. Use
`pytest`, Hypothesis, `pyright`, and `ruff` for development verification.

Do not use a third-party finite-state-machine framework. Admission,
dispatch acknowledgement, cancellation, recovery, and durable tombstones are
domain rules and should remain visible in the reducer.

## Why Python

Python is the best fit for this repository and problem because:

- the existing display and layout libraries are Python (`libdpy.py` and
  `liblayout.py`);
- Python 3.13, `uv`, `pytest`, and `pyudev` are already available on the target
  host;
- hardware actions are subprocess- and systemd-oriented rather than
  CPU-intensive;
- immutable event/reducer code is straightforward to simulate with a fake
  clock;
- raw XRandR, autorandr, sysfs, and EDID fixtures are easy to capture and
  replay; and
- Hypothesis can generate and shrink adversarial event sequences.

Go remains a reasonable fallback if static single-binary deployment becomes a
higher priority than reuse and test ergonomics. Rust's stronger type system
does not currently justify its greater implementation and maintenance cost for
this single-user, subprocess-heavy service.

## Process topology

```text
             udev DRM hints     persisted timers     worker status
                    \                 |                 /
                     +----------------+----------------+
                                      |
                                      v
                  +---------------------------------------+
                  | monitor-controller.service            |
                  |                                       |
                  | observer -> reducer -> state -> effects|
                  |                 |                     |
                  |                 +---- JSONL audit log  |
                  +-----------------+---------------------+
                                    |
                      immutable keyed action request
                                    |
          +----------------+--------+---------+----------------+
          |                |                  |                |
          v                v                  v                v
 monitor-probe@    monitor-apply@    monitor-prepare@  monitor-finalize@
```

Every worker is a sibling launched only by the controller. A preparation
worker never launches a finalizer or otherwise advances controller state.

Only the controller is a persistent decision-making authority. Workers are
short-lived and have no authority to select another profile or transition.
Systemd supervises them so they remain discoverable and cancellable if the
controller restarts.

Do not split observation, scheduling, reduction, and persistence into separate
long-running services. Those operations need one serialized event order and
one atomic state transition. Separate daemons would add IPC ordering and torn
snapshot failure modes without providing useful isolation.

## Source layout

The tracked project should live under
`.local/lib/monitor-controller/`, which Stow installs as
`~/.local/lib/monitor-controller/`. Its package should have boundaries
equivalent to:

```text
monitor_controller/
    model.py
    reducer.py
    invariants.py
    codec.py
    observer/
        drm.py
        xrandr.py
        autorandr.py
        snapshot.py
    runtime/
        controller.py
        scheduler.py
        persistence.py
        dispatcher.py
        systemd.py
    workers/
        probe.py
        apply.py
        prepare.py
        finalize.py
    simulation/
        scenario.py
        replay.py
    cli.py
```

Tests should mirror these boundaries under `tests/monitor_controller/`.
The project-local `pyproject.toml` and `uv.lock` live beside the package. A
tracked install helper creates a fixed virtual environment under
`$XDG_DATA_HOME/monitor-controller/venv` and installs the locked project there.
Systemd executes that environment's entry point directly; service startup must
never resolve or download dependencies through `uv run`.

## Pure domain core

The core must not read the clock, environment, filesystem, X server, sysfs, or
subprocesses. Its conceptual interface is:

```python
@dataclass(frozen=True)
class Decision:
    state: State
    effects: tuple[Effect, ...]


def reduce(state: State, event: Event) -> Decision:
    ...
```

`Event` is a closed union containing values such as:

- `ObservationCompleted`;
- `PlanRequested`, `PlanCompleted`, and `PlanFailed`;
- `TimerFired`;
- `ProbeDispatched` and `ProbeFinished`;
- `ApplicationDispatched` and `ApplicationFinished`;
- `PreparationDispatched` and `PreparationFinished`;
- `FinalizationDispatched` and `FinalizationFinished`;
- `DispatchRejected`, `WorkerStatusUnknown`, and `WorkerTimedOut`;
- `ControllerStarted`; and
- `BootChanged`.

Time, boot ID, action ID, and observation validity are explicit event fields.
Effects are data, not callbacks, for example:

```python
ApplyProfile(profile="celtic", action_id="apply-42")
Schedule(deadline_ms=123456)
StopAction(action_id="apply-41")
FinalizeDesktop(profile="celtic", transition_id="finalize-19")
```

Every call runs central invariants before returning. Invalid transitions fail
closed and are logged; they never silently dispatch a side effect.

## Runtime event loop

The controller uses one `asyncio.Queue[Event]` and one consumer. Producers may
enqueue hints concurrently, but only the consumer can:

1. request a canonical observation;
2. call the reducer;
3. persist the resulting state;
4. fence and dispatch an admitted effect; and
5. persist dispatch acknowledgement or rejection.

The udev producer increments an in-memory event generation before enqueueing
each hint. Every observation records that generation. Immediately before a
worker launch, the controller yields to producers, drains queued hints, and
compares the current generation with the admitted generation. Any change emits
a dirty-admission event and forces re-observation instead of launch. The worker
then repeats the immutable topology guard from its request immediately before
mutation. This does not pretend that hardware can be frozen, but it closes the
known observation-to-dispatch queue race at both process boundaries.

DRM events are coalesced wake-up hints. A persisted nearest deadline always
causes another observation, even if no DRM event follows. Worker completion is
also a hint: success is not accepted until a fresh observation confirms the
same transition. Request-write failure, unit-start rejection, supervisor-query
failure, and timeout are explicit reducer inputs; no pending action can wait
forever because an adapter failed silently.

Observation commands have bounded timeouts. Timeout, parse failure, and
mutually inconsistent samples produce an explicit invalid observation rather
than guessed absence.

## Canonical observer

`observer.snapshot` coordinates one immutable observation from:

- DRM connector status, connector IDs, and raw EDID under `/sys/class/drm`;
- X connected/active topology and connector IDs;
- autorandr fingerprints, detected/current profiles, and documented monitor
  mappings; and
- current boot ID and monotonic time.

Command execution uses argument arrays, never `shell=True`. Parsing modules
return typed results and preserve raw evidence references for diagnostics.

The observer owns consistency policy. It samples generation-sensitive inputs
at the beginning and end and marks the result invalid when topology changes
across the sample. The reducer never tries to repair a torn observation.

Initial adapters may invoke documented command interfaces such as `xrandr` and
`autorandr`. Direct XCB or D-Bus bindings are optional later optimizations, not
a prerequisite for a structured controller. Do not import the existing
`libdpy.py` or `liblayout.py` directly into the canonical observer: they have
home-bound defaults, shared-cache mutation, process exits, and shell execution.
Extract pure parsing/calculation where useful, or wrap the existing commands
behind injected adapter interfaces until they are refactored.

## Persistence

Persist the authoritative state as strict versioned JSON under separate
namespaces:

```text
$XDG_STATE_HOME/monitor-controller/active/state.json
$XDG_STATE_HOME/monitor-controller/shadow/state.json
```

Write to a sibling temporary file, flush and `fsync`, rename atomically, then
`fsync` the directory before dispatching newly admitted work. The codec rejects
unknown or duplicate fields, invalid enums, malformed IDs, out-of-range
numbers, inconsistent action relationships, and records exceeding a bounded
size before mutating in-memory state. Persist:

- schema version and boot ID;
- physical epoch and observation keys;
- current phase and deadlines;
- stable X and finalized desktop profiles;
- monotonically allocated action/transition sequence;
- admitted, dispatched, completed, failed, and cancelled action identities;
- pending probe/application/preparation/finalization payloads; and
- recovery-relevant worker unit names;
- output-mapping proof, unplug proof, backoff, and observation/event generation;
  and
- only recovery-relevant action tombstones plus sequence high-water marks.

Monotonic deadlines are valid only for the same boot. On a boot-ID change,
retain durable identity/tombstone facts but discard monotonic waits and require
fresh observation.

Corrupt authoritative state is not simply discarded on the same boot. Before
authority can resume, recovery scans the active transaction namespace and
matching systemd units, reconstructs all surviving worker exclusions and ID
high-water marks, and reconciles them with a fresh observation. If any worker
or sequence relationship remains ambiguous, the controller stays fail-closed
in `RECOVERING` and requires operator resolution. Action IDs also contain a
fresh controller-instance UUID so corruption cannot make a reused numeric
sequence collide with a surviving unit.

The JSONL audit stream is diagnostic and replayable, not the source of truth.
A truncated audit tail must not prevent state recovery. Rotate audit logs by
size and retain a bounded number; completed historical actions are summarized
by sequence high-water marks rather than accumulated forever in `state.json`.

## Worker protocol

For each admitted action, write an immutable request atomically under:

```text
$XDG_RUNTIME_DIR/monitor-controller/<namespace>/transactions/<action-id>/request.json
```

`<namespace>` is `active` or `shadow`. The two are disjoint by construction
and mutually inaccessible via the units' `InaccessiblePaths=`, so neither
controller can read or overwrite the other's in-flight transactions. (An
earlier draft of this document placed transactions under
`$XDG_RUNTIME_DIR/monitor-system/`, which predates the namespace split and
the removal of `bin/monitor-system` in `1f57823`.)

The request binds the action to its physical epoch/token, admitted event
generation and observation key, resolved profile/output mapping, expected
active topology, profile-to-layout mapping, and—where applicable—staged plan
hash. Then start a typed systemd user template unit. The worker writes
`result.json` atomically and exits. The controller derives process truth from
the systemd unit and treats the result file as action output.

Use distinct unit templates where timeout and cancellation policy differ:

- `monitor-probe@.service` — one bounded safe-mode activation;
- `monitor-apply@.service` — one explicit autorandr profile load;
- `monitor-prepare@.service` — cancellable repeatable desktop preparation; and
- `monitor-finalize@.service` — disruptive window/process commitment.

Workers validate both payload-to-unit identity and current topology before
mutation. Preparation/finalization workers repeat cooperative guards at every
safe mutating boundary, preserving the existing `setup-monitor`
`assert_expected_state` protection while moving its policy into Python. They
never select a fallback profile. Cancellation is keyed and idempotent. Late
completion from a superseded ID is recorded but cannot mutate current
controller state.

Static template units plus documented systemd properties are sufficient for
the first implementation. A direct systemd D-Bus client may replace
`systemctl` subprocesses later, but no custom IPC service is required. Add real
user-unit contract tests for instance escaping, rapid completion before
acknowledgement, timeout, cancellation, cgroup cleanup, and restart
reattachment; fake supervisors alone are insufficient.

## Autorandr application policy

The controller computes a unique saved-output-to-live-output bijection from
saved setup fingerprints and live `autorandr --fingerprint`, then materializes
an immutable transaction-local autorandr profile whose `config` and `setup`
output names are both rewritten consistently to those admitted live
connectors. The artifact includes a no-op postswitch hook, and all three files
are covered by the request hash. The application worker
validates that hash and current topology, then invokes the explicit remapped
profile without asking autorandr to make a second rename decision, equivalent
to:

```text
XDG_CONFIG_HOME=TRANSACTION_XDG autorandr --skip-options gamma --load ACTION_PROFILE
```

It must suppress the current postswitch side effect. During migration the hook
recognizes a controller-specific deferral environment and returns without
launching `setup-monitor`; worker completion and fresh observation replace the
pending marker. `AUTORANDR_MONITORS` records the enabled live output set after application and
may be used by the no-op notification hook as post-action evidence; it does not
encode saved-to-live connector renames. Reject ambiguous pre-action bijections
and never parse human-readable rename diagnostics as a protocol.

At active cutover, autorandr postswitch ceases to be a finalization authority.
A manual autorandr invocation may notify the controller that X changed, but
only the controller can admit desktop work after observation. If the controller
is unavailable, the hook logs a deferred-notification failure rather than
launching an unkeyed finalizer.

## Worker process ownership

Action units use bounded start/stop timeouts and cooperative `SIGTERM`, followed
by systemd cgroup cleanup. Finalization workers must not leave intended
persistent children in their own short-lived cgroup. Fluxbox, `xfce4-panel`,
`nm-applet`, and delayed diagnostics are started or restarted through their own
user units/scopes; the finalizer waits for documented readiness and then exits.

Unit files must define `KillMode`, `TimeoutStartSec`, `TimeoutStopSec`, stop
grace, and failure reporting explicitly. Privilege-hardening changes are made
only after contract tests prove compatibility with the X session and child
service ownership.

## Desktop preparation and commitment

Planning and preparation are orthogonal reducer lifecycles, not implicit calls
made by a finalizer. Planning uses states equivalent to `PLAN_IDLE`,
`PLAN_PENDING`, `PLANNING`, `PLAN_READY`, and `PLAN_FAILED`. Its input key is
derived from physical epoch, exact target/layout, resolved output mapping, and
configuration-file hashes. A keyed cancellable runtime task computes and
atomically stages the plan, then returns its plan hash in `PlanCompleted`; a
restart may safely recompute an unacknowledged plan.

Preparation uses states equivalent to `PREPARE_IDLE`, `PREPARE_PENDING`,
`PREPARING`, `PREPARED`, `PREPARE_STOPPING`, and `PREPARE_FAILED`, keyed by the
completed plan hash and transition key. Startup baseline adoption explicitly
forbids planning and preparation, even when no finalized profile has yet been
loaded from valid state.

Preserve the three-phase split from the state-machine design:

1. **Preflight** is the acknowledged planning lifecycle above. It computes and
   stages intended configuration in the transaction directory without writing
   shared display caches or live desktop state.
2. **Prepare** is eligible only after `PLAN_READY` and an initial two-second
   clean proof of the same
   exact/current/active target and only after probe/application workers have
   completed. It applies repeatable, supersedable properties while the full
   ten-second proof continues.
3. **Finalize** is eligible only when full proof holds, preparation is
   `PREPARED` for the same plan hash, and a fresh dirty-event fence passes. It
   moves windows and performs only process restarts still proven necessary.

Topology or identity contradiction requests keyed preparation cancellation and
blocks finalization until the worker acknowledges completion. Preparation
failure is explicit and cannot silently fall through to an unprepared
finalizer. Restart recovery reattaches a running unit or validates staged
artifacts before accepting `PREPARED`; a plan from another epoch or hash is
removed.

Python owns phase ordering, staging, transition guards, cancellation, and
result reporting. Existing shell may remain temporarily only as bounded leaf
operations with one documented effect and structured exit status. The
production finalizer must not invoke the monolithic `setup-monitor` workflow as
a second orchestration engine.

## Simulation and tests

### Language-neutral scenario format

Port the Bash spike's scenarios to JSON records containing timestamped input
events and expected state/effect sequences. Assertions must include effect
counts, IDs, and ordering, not only final state.

During migration, a compatibility runner can feed the same scenarios to both
reducers. Remove the Bash reducer after parity is established and the Python
suite includes all invariants.

### Reducer tests

Use parametrized `pytest` scenarios with a fake monotonic clock. Require
explicit transition coverage, invalid-event fail-closed cases, every admission
race window, recovery phase, and failure tombstone; do not chase meaningless
Cartesian state/event pairs.

### Property-based tests

Use Hypothesis rule-based stateful testing to generate and shrink arbitrary
sequences of observations, timers, worker results, and restarts. Assert at
least:

- at most one display mutation is acknowledged in flight;
- unresolved external intent never authorizes an internal-only profile;
- invalid observations never advance identity or authorize actions;
- action and transition IDs are never reused;
- finalization requires stable exact topology;
- stale worker completion cannot affect a newer transition; and
- replay produces identical states and effects.

### Adapter contract tests

Keep raw fixtures for:

- `xrandr --query` and `xrandr --props`;
- autorandr fingerprint, detected, current, and monitor mapping output;
- synthetic sysfs connector trees;
- valid, incomplete, and checksum-invalid EDIDs; and
- the captured Samsung failure/settled samples.

Adapter tests run entirely against temporary directories and fake command
results. Separate systemd protocol tests use real temporary user units to
validate the worker boundary without touching the display.

### Runtime crash tests

Use fake persistence and worker supervisors to inject failure:

- after admission persistence but before unit start;
- after unit start but before dispatch acknowledgement;
- during every worker phase;
- on a DRM event during observation, after state persistence, after request
  creation, and immediately before unit start;
- after worker success but before fresh observation;
- during atomic state replacement; and
- across boot-ID change.

### Capture and replay

Append one JSONL decision record per reducer input, including:

- wake reason and monotonic time;
- canonical observation or worker result;
- prior and resulting state keys;
- emitted effects; and
- observation, command, and worker timing.

Provide CLI commands equivalent to:

```text
monitor-controller simulate scenario.json
monitor-controller replay trace.jsonl
monitor-controller status
```

Replay calls the same reducer and fails on nondeterminism or changed effects.
Sensitive or bulky raw EDID data should be stored by hash in ordinary logs and
included only in explicit diagnostic bundles.

## Authority and service integration

The active controller acquires an exclusive `flock` lock under
`$XDG_RUNTIME_DIR/monitor-controller/active/authority.lock` before loading
state. Lock loss or contention is fatal; it never degrades into a second
authority. The shadow controller uses a separate lock, state directory,
transaction namespace, and audit stream and has no worker unit permission.

`monitor-controller.service` declares `Conflicts=` against both legacy watcher
units and the shadow unit, with ordering and lifecycle integration under the
existing graphical/Fluxbox session target. Cutover explicitly stops shadow,
starts the active controller, verifies its lock and baseline adoption, and
keeps a command to stop it and re-enable `monitor-watcher-ng.service`.

Unit permissions and request namespaces enforce the null-dispatch boundary;
`--shadow` is not merely a Boolean checked at individual call sites.

## Shadow deployment

The first real deployment is a separate
`monitor-controller-shadow.service` using the real observer, scheduler,
persistence, and audit log but a null dispatcher. It may emit
`WOULD_PROBE`, `WOULD_APPLY`, `WOULD_PREPARE`, and `WOULD_FINALIZE`; it cannot
start action workers.

Keep `monitor-watcher-ng.service` authoritative during shadow collection.
Compare both controllers across repeated Samsung plug, unplug, slow readiness,
and resume sequences. Convert discrepant traces into deterministic fixtures.

Only after review and live acceptance should the new controller receive action
authority. Stop shadow before active cutover and never run two dispatchers
concurrently.

## Incremental implementation order

1. Establish `pyproject.toml`, locked development tools, package skeleton, and
   typed model.
2. Port reducer semantics and all synthetic scenarios.
3. Add persistence/recovery tests and property-based invariants.
4. Implement fixture-driven observation adapters.
5. Implement the fake-clock runtime and null dispatcher.
6. Deploy the shadow service and collect real traces.
7. Implement keyed probe and autorandr workers.
8. Split desktop preflight/prepare/finalize workers and test cancellation.
9. Compare shadow decisions through repeated physical cycles.
10. Cut over the systemd authority with an explicit rollback path.

No step should point the live unit at the pure reducer or grant a shadow
instance side-effect authority.
