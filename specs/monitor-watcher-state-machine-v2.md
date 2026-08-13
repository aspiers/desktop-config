# Monitor Watcher State Machine v2

## Status

**SPIKE — not deployed.** This design does not change either running monitor
watcher or its systemd unit. The executable reducer and synthetic trace tests
live under `specs/spikes/` so GNU Stow will not install them.

Tracked by `dc-a5y` — **Replace monitor watcher loop with a persistent state
machine**.

## Why replace the current loop

`bin/monitor-watcher-ng` has accumulated individually reasonable recovery
rules around a synchronous retry loop. The loop nevertheless treats a DRM
event as both the start of work and, after its 30-second budget, the only way
to resume work. That assumption is false: a dock can expose a connector, EDID,
and usable mode in stages without emitting a final event at the point the EDID
becomes useful.

The controller also conflates facts which must have independent lifetimes:

- physical connector topology;
- X-connected and X-active outputs;
- live monitor identity/EDID readiness;
- selected autorandr target;
- the last target applied to X;
- the profile proven continuously stable;
- the profile for which desktop work last completed.

This produces two user-visible failures:

1. An unresolved external link is abandoned after the fast retry budget.
2. A transient laptop-only match can be applied while that link trains,
   needlessly reprogramming the eDP CRTC and blanking the laptop panel.

A deferred autorandr postswitch marker creates a third error: it proves only
that autorandr applied *some* X state, not that the final desktop layout
changed.

## Design boundary

The replacement is a single event loop around four layers:

1. **Observation adapter** — samples sysfs, XRandR, and autorandr into one
   immutable canonical observation.
2. **Pure reducer** — consumes `(controller state, event, observation, time)`
   and emits a new state plus symbolic actions. It performs no I/O.
3. **Action adapter** — explicitly loads one selected autorandr profile or
   starts/stops one desktop-finalization transaction.
4. **Scheduler and persistence** — multiplexes DRM events with the nearest
   monotonic deadline and atomically stores a versioned state record.

DRM events are latency hints. Timers are an equally valid source of progress.

## Canonical observation

One probe produces these sorted fields:

```text
observed_at_ms
kernel_connected_outputs
kernel_external_outputs
x_connected_outputs
x_active_outputs
live_fingerprints
eligible_profiles
current_profiles
exact_profile
observation_key
valid
```

`eligible_profiles` comes from autorandr profile identity matching. The
watcher separately computes whether one eligible profile has an exact,
unambiguous mapping onto the complete connected and active topology.
`exact_profile` is set only for that proof.

`observation_key` hashes all fields except time. Repeated identical evidence
therefore has the same key. An observation is invalid if sysfs/X sampling is
internally inconsistent in a way that could authorize an action; invalid
samples may cause another probe but never an application or finalization.

Before every action, the runtime adapter drains queued DRM input. If it drains
anything, it re-observes instead of acting. After a blocking action it drains
and re-observes again.

## Profile-to-output mapping

Autorandr selects profile identity; topology validation remains the watcher's
responsibility. For each profile `setup` entry, match its documented EDID
fingerprint (including a single `*` wildcard) against `autorandr
--fingerprint`. Require a unique bijection and reject ambiguous matches. This
uses a documented machine-readable command rather than parsing the
human-readable `renaming display …` diagnostic.

A proven mapping belongs to one `physical_epoch`. Temporary EDID absence may
retain it within that epoch; a usable contradictory EDID revokes it
immediately. A physical topology change increments the epoch and invalidates
all prior mapping proof.

## Persisted controller state

The production implementation should atomically write a whitelist-parsed TSV
record; it must never `source` state as shell code.

```text
schema_version
boot_id
display
phase
physical_epoch
physical_token
reconcile_epoch
candidate_profile
candidate_mapping
candidate_proof_epoch
candidate_observation_key
aggressive_deadline_ms
next_timer_ms
backoff_index
verify_since_ms
last_drm_at_ms
attempted_application_keys
desktop_finalized_profile
finalization_id
finalization_profile
finalization_status
unknown_key
unknown_since_ms
unplug_since_ms
```

A schema mismatch or corrupt record is safely discarded. A boot-ID mismatch
keeps only durable desktop-finalization facts whose transaction status can be
verified independently.

## States

| State | Meaning | Normal wake-up |
| --- | --- | --- |
| `RECOVERING` | Validate persisted state against a fresh observation. | immediate |
| `QUIESCENT` | One known profile is exact and stable; no unresolved work. | DRM or 60s health tick |
| `DISCOVER_FAST` | Topology/identity is changing within the aggressive budget. | 0, 250ms, 500ms, 1s, then 2s |
| `APPLYING` | One explicit profile application is in progress. | process completion |
| `VERIFYING` | A candidate is exact/current/active and accumulating proof. | 1s |
| `WAIT_SLOW` | Fast budget expired, but an external display remains unresolved. | 5s, 10s, 20s, then 30s capped |
| `UNSUPPORTED` | A complete stable external identity matches no saved profile. | DRM or 60s health tick |
| `FINALIZING` | One durable desktop transition is running. | DRM or 1s status tick |
| `FINALIZE_FAILED` | The transition failed; automatic duplicate execution is barred. | DRM or 60s health tick |

The aggressive deadline is a true deadline for *aggressive work*. It changes
retry policy from `DISCOVER_FAST` to `WAIT_SLOW`; it does not pretend that an
unresolved connected output has become resolved.

## Central invariants

One `assert_controller_invariants` function must enforce these after every
transition:

1. **No laptop fallback with external hardware present.** If sysfs or X reports
   a connected external output, an internal-only target is ineligible even if
   autorandr detects it.
2. **Explicit applications only.** Select a target first, then use `autorandr
   --load TARGET`; never use unrestricted `--change` during discovery.
3. **No duplicate application for unchanged evidence.** Apply each
   `(physical_epoch, target, observation_key)` at most once.
4. **EDID absence means uncertainty, not unplug.** It cannot select or finalize
   the laptop-only profile.
5. **Continuous final proof.** Exact connected and active topology, current
   profile agreement, no contradiction, event quietness, and the stability
   duration must all hold continuously.
6. **Independent desktop state.** `desktop_finalized_profile` changes only
   after successful desktop work, never after autorandr application alone.
7. **Profile-transition finalization.** Resume, connector rename, repeated
   apply, and EDID churn do not finalize if the verified profile equals
   `desktop_finalized_profile`.
8. **Explicit unplug proof.** Internal-only becomes eligible only after both
   sysfs and X report no external output for two observations spanning at
   least one second with no queued event.
9. **No implicit abandonment.** Every unresolved connected external topology
   has a scheduled timer.

## Transition table

Global rule: a changed physical topology increments `physical_epoch`, clears
candidate proof and application history, cancels stale verification, and
enters `DISCOVER_FAST`. A queued DRM event makes an observation dirty and
prevents action until re-probed.

| State | Event / guard | Action | Next state |
| --- | --- | --- | --- |
| `RECOVERING` | Finalizer transaction still running | Reattach; do not launch again | `FINALIZING` |
| `RECOVERING` | Persisted candidate is exact/current | Reset stability start | `VERIFYING` |
| `RECOVERING` | External topology unresolved | Restore deadline/backoff | `DISCOVER_FAST` or `WAIT_SLOW` |
| `RECOVERING` | Exact current profile, no pending transaction | Adopt as restart baseline without desktop work | `VERIFYING` |
| `QUIESCENT` | Same exact stable profile | No action | `QUIESCENT` |
| `QUIESCENT` | Candidate EDID disappears but mapped output remains | Preserve intent, do not load internal profile | `DISCOVER_FAST` |
| `QUIESCENT` | Another exact/current eligible profile appears | Start stability proof | `VERIFYING` |
| `QUIESCENT` | Other meaningful observation change | Start epoch/deadline | `DISCOVER_FAST` |
| `DISCOVER_FAST` | Invalid/mixed observation | Schedule fast probe | `DISCOVER_FAST` |
| `DISCOVER_FAST` | External connected, identity incomplete | Preserve external intent | `DISCOVER_FAST`, then `WAIT_SLOW` at deadline |
| `DISCOVER_FAST` | Complete unknown identity stable for 10s | Record reason | `UNSUPPORTED` |
| `DISCOVER_FAST` | Candidate already exact/current/active | Start stability proof | `VERIFYING` |
| `DISCOVER_FAST` | Eligible target not exact/current and application key is new | Persist key; explicitly load target | `APPLYING` |
| `DISCOVER_FAST` | Application key already attempted | Wait for changed evidence | `DISCOVER_FAST` or `WAIT_SLOW` |
| `DISCOVER_FAST` | Aggressive deadline reached unresolved | Preserve intent; begin slow backoff | `WAIT_SLOW` |
| `WAIT_SLOW` | Same unresolved observation | Increase capped backoff | `WAIT_SLOW` |
| `WAIT_SLOW` | Evidence changes | Reset fast backoff, retain epoch/candidate as valid | `DISCOVER_FAST` |
| `WAIT_SLOW` | Candidate becomes eligible/exact | Apply or verify | `APPLYING` or `VERIFYING` |
| `APPLYING` | Command completes | Drain events and re-observe | classify into discovery/verification |
| `APPLYING` | Same evidence remains unresolved | Keep attempted key; do not repeat | `DISCOVER_FAST` or `WAIT_SLOW` |
| `VERIFYING` | Event, contradiction, inactive/extra output, or no longer current | Reset proof; revoke only on contradiction/topology change | `DISCOVER_FAST` |
| `VERIFYING` | Exact proof younger than 10s | Continue proof | `VERIFYING` |
| `VERIFYING` | Proof complete; target equals finalized desktop profile | Record stable X profile only | `QUIESCENT` |
| `VERIFYING` | Proof complete during startup baseline adoption | Adopt desktop baseline without relayout | `QUIESCENT` |
| `VERIFYING` | Proof complete; target differs from finalized desktop profile | Create durable transaction | `FINALIZING` |
| `FINALIZING` | Same unit still running and topology valid | No duplicate launch | `FINALIZING` |
| `FINALIZING` | Topology changes | Stop stale unit; keep prior finalized profile | `DISCOVER_FAST` |
| `FINALIZING` | Unit succeeds | Persist finalized profile and acknowledge transaction | `QUIESCENT` |
| `FINALIZING` | Unit fails without cancellation | Record failure; do not retry same ID | `FINALIZE_FAILED` |
| `UNSUPPORTED` | Same complete unknown topology | No action | `UNSUPPORTED` |
| `UNSUPPORTED` | Identity becomes incomplete | Resume polling | `WAIT_SLOW` |
| `UNSUPPORTED` | Profile becomes eligible or genuine unplug completes | Start new epoch | `DISCOVER_FAST` |
| `FINALIZE_FAILED` | Same transition | Report only | `FINALIZE_FAILED` |
| `FINALIZE_FAILED` | Genuine new topology/profile transition | Clear failed transaction context | `DISCOVER_FAST` |

## Timers

Use monotonic milliseconds from `/proc/uptime`, not wall-clock time or Bash
`SECONDS`.

```text
AGGRESSIVE_BUDGET_MS = 30000
FAST_DELAYS_MS        = 0, 250, 500, 1000, 2000
SLOW_DELAYS_MS        = 5000, 10000, 20000, 30000 (cap)
PROFILE_STABILITY_MS  = 10000
EVENT_QUIET_MS        = 5000
UNKNOWN_STABILITY_MS  = 10000
UNPLUG_STABILITY_MS   = 1000
HEALTH_POLL_MS        = 60000
```

Persist absolute deadlines. On service restart, an overdue timer fires
immediately; unresolved waiting is neither reset nor abandoned. DRM events
schedule an immediate observation but reset the aggressive deadline only when
they reveal a new physical epoch.

## Desktop finalization transaction

Autorandr postswitch must cease being authoritative. After stable proof, create
a durable transition ID only when `candidate_profile !=
desktop_finalized_profile`.

The production implementation should run `setup-monitor` synchronously in a
dedicated oneshot systemd unit keyed by that ID. The unit is outside the
watcher's cgroup and remains discoverable after watcher restart. Repeated starts
of the same active instance are no-ops. The watcher updates
`desktop_finalized_profile` only after successful completion.

Strict exactly-once completion across arbitrary power loss is impossible
because `setup-monitor` is not transactional. This protocol provides
exactly-once admission/launch across watcher restarts and prevents automatic
rerun of a failed transaction under the same ID.

## Spike interface

The non-deployed Bash spike is intentionally smaller than the future adapters.
It proves the reducer policy with synthetic observations. Every `OBSERVE` input
contains:

```text
key physical_token external_state eligible_profile exact_profile current_profile
```

where `external_state` is `none`, `unresolved`, `known`, or `unknown`.
The reducer emits symbolic actions such as:

```text
SCHEDULE delay_ms
APPLY profile
FINALIZE transition_id profile
STOP_FINALIZER transition_id
```

It never executes autorandr, xrandr, systemd, or `setup-monitor`.

## Required trace coverage

Table-driven traces must assert state, next timer, autorandr applications, and
desktop finalizations for:

- laptop-only startup already current;
- genuine laptop-to-external plug;
- readiness arriving after the aggressive deadline without a DRM event;
- EDID ready → missing → ready;
- transient laptop-only detection while external remains connected;
- genuine unplug;
- resume to the same external profile;
- automatic X transition requiring desktop-only finalization;
- renamed connector with unchanged profile identity;
- stable unknown external monitor;
- restart in every non-stable phase;
- unchanged observation/application deduplication;
- finalizer reattachment without duplicate launch.

The trace must fail if an internal-only application occurs while external
hardware is present, if identical evidence causes repeated application, or if
an unresolved external state lacks a future timer.

## Migration outline

1. Land and review the pure reducer spike.
2. Implement the canonical observation adapter and EDID bijection tests.
3. Add versioned persistence and event/timer scheduler in shadow/dry-run mode.
4. Compare shadow decisions with the existing watcher during real hub cycles.
5. Add durable desktop-finalizer units and remove postswitch-marker authority.
6. Switch the service entry point only after synthetic tests and repeated live
   Samsung reconnects pass; retain `monitor-system legacy` as the rollback.
