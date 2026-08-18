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
3. Some Samsung plug/resume sequences deadlock with a valid, exact base EDID
   identity but invalid extension blocks while the external output remains
   disabled. Passive polling does not repair this state; a safe activation
   probe causes the monitor to republish valid extension data.

A deferred autorandr postswitch marker creates another error: it proves only
that autorandr applied *some* X state, not that the final desktop layout
changed.

## Design boundary

The replacement is a single event loop around four layers:

1. **Observation adapter** — samples sysfs, XRandR, and autorandr into one
   immutable canonical observation.
2. **Pure reducer** — consumes `(controller state, event, observation, time)`
   and emits a new state plus symbolic actions. It performs no I/O.
3. **Action adapter** — activates one safe probe mode, explicitly loads one
   selected autorandr profile, or starts/stops one desktop-finalization
   transaction.
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
base_identity_profiles
edid_integrity
probe_candidate
probe_output
probe_mode
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

`base_identity_profiles` contains profiles whose complete 128-byte EDID base
block is checksum-valid and exactly equal to the live base block. Manufacturer,
product, binary serial, text serial, and base checksum must therefore all
agree; model-name heuristics are insufficient. `edid_integrity` separately
records whether the advertised extension count, length, and per-block checksums
are complete. A monitor can have a proven base identity while its extension
blocks are not ready.

`probe_candidate` is set only when exactly one saved profile entry resolves to
the inactive external output and has that base identity, exactly one external
output is connected but inactive, exactly
one internal output is active, the connected topology exactly matches the
profile, and no full autorandr profile matches. Its mode is the output's
advertised preferred mode; the probe does not select a desktop profile.

`observation_key` hashes all fields except time. Repeated identical evidence
therefore has the same key. `valid` describes whether the canonical sample is
internally coherent. Broken extension checksums are represented explicitly in
`edid_integrity`; they do not make an otherwise coherent base-identity sample
invalid. A mixed/torn sample whose facts cannot safely be classified has
`valid=0` and may schedule another observation but can never authorize a probe,
application, or finalization.

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
candidate_scope
candidate_mapping
candidate_proof_epoch
candidate_observation_key
aggressive_deadline_ms
next_timer_ms
backoff_index
verify_since_ms
last_drm_at_ms
attempted_probe_keys
pending_probe_key
pending_probe_profile
pending_probe_output
pending_probe_internal_output
pending_probe_mode
probe_status
probe_exit_status
attempted_application_keys
pending_application_key
pending_application_profile
pending_application_scope
application_status
application_exit_status
desktop_finalized_profile
finalization_sequence
finalization_id
finalization_profile
finalization_transition_key
finalization_status
finalization_exit_status
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
| `PROBE_PENDING` | A safe output activation is admitted from one exact base identity but not yet acknowledged as dispatched. | same clean observation or dispatch acknowledgement |
| `PROBING` | One acknowledged safe-mode activation probe is in progress. | process completion |
| `PROBE_FAILED` | A probe failed or became unknowable after restart; unchanged evidence cannot repeat it. | changed evidence or 60s health tick |
| `APPLY_PENDING` | An explicit profile load is admitted but not yet acknowledged as dispatched. | clean observation or dispatch acknowledgement |
| `APPLYING` | One acknowledged explicit profile application is in progress. | process completion |
| `APPLY_FAILED` | An application failed or its outcome became unknowable after restart; unchanged evidence cannot repeat it. | changed evidence or 60s health tick |
| `VERIFYING` | A candidate is exact/current/active and accumulating proof. | 1s |
| `WAIT_SLOW` | Fast budget expired, but an external display remains unresolved. | 5s, 10s, 20s, then 30s capped |
| `UNSUPPORTED` | A complete stable external identity matches no saved profile. | DRM or 60s health tick |
| `FINALIZE_PENDING` | A durable desktop transition is admitted but not yet acknowledged as dispatched. | clean observation or dispatch acknowledgement |
| `FINALIZING` | One acknowledged durable desktop transition is running. | DRM or 1s status tick |
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
2. **A probe is not profile authorization.** A safe activation probe requires
   one checksum-valid base block exactly matching one saved profile, an exact
   connected topology, one inactive external output, one active internal
   output, and no complete autorandr match. It may enable only the output's
   advertised preferred mode. It never authorizes autorandr, desktop work, or
   persistence of a selected profile; those require a later full match.
3. **Explicit applications only.** Select a target first, then use `autorandr
   --load TARGET`; never use unrestricted `--change` during discovery. Action
   admission and dispatch acknowledgement are distinct so a queued event cannot
   erase an action before it executes or mark an unexecuted action complete.
4. **No duplicate probe or application for unchanged evidence.** Probe each
   `(physical_epoch, base_identity_profile, observation_key)` and apply each
   `(physical_epoch, target, observation_key)` at most once.
5. **EDID absence means uncertainty, not unplug.** It cannot select or finalize
   the laptop-only profile.
6. **Continuous final proof.** Exact connected and active topology, current
   profile agreement, no contradiction, event quietness, and the stability
   duration must all hold continuously.
7. **Durable action dispatch.** Activation probes, autorandr loads, and desktop finalization
   run as keyed, discoverable workers. Persist admission before launch and
   acknowledge dispatch only after the service manager accepts the keyed unit;
   recovery re-observes pending admissions and reattaches acknowledged workers.
   An indeterminate or failed probe/application becomes explicit
   `PROBE_FAILED`/`APPLY_FAILED` rather than being silently deduplicated into
   waiting.
8. **Independent desktop state.** `desktop_finalized_profile` changes only
   after successful desktop work, never after autorandr application alone.
9. **Profile-transition finalization.** Resume, connector rename, repeated
   apply, and EDID churn do not finalize if the verified profile equals
   `desktop_finalized_profile`.
10. **Explicit unplug proof.** Internal-only becomes eligible only after both
   sysfs and X report no external output for two observations spanning at
   least one second with no queued event.
11. **No implicit abandonment.** Every unresolved connected external topology
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
| `DISCOVER_FAST` | Invalid/mixed observation | Schedule another observation; no action | `DISCOVER_FAST` |
| `DISCOVER_FAST` | One exact base identity, broken extensions, exact connected/inactive topology, no full match, new probe key | Persist probe admission | `PROBE_PENDING` |
| `PROBE_PENDING` | Queued event dirties observation before dispatch | Re-observe; leave admission re-emittable only under the same base identity | `PROBE_PENDING` or discovery state |
| `PROBE_PENDING` | Probe worker is accepted | Persist attempted key and worker identity | `PROBING` |
| `PROBING` | Safe-mode activation completes | Drain events and re-observe; do not infer profile readiness | `DISCOVER_FAST` |
| `PROBING` | Physical topology changes while worker may still mutate X | Request idempotent stop; preserve worker admission until completion callback | `PROBING` |
| `PROBING` | Command fails or restart makes outcome unknowable | Preserve terminal attempt evidence | `PROBE_FAILED` |
| `PROBE_FAILED` | Same base identity and observation | Report only; never repeat automatically | `PROBE_FAILED` |
| `PROBE_FAILED` | Genuinely changed evidence | Clear terminal context and classify anew | `DISCOVER_FAST` |
| `DISCOVER_FAST` | External connected, identity incomplete | Preserve external intent | `DISCOVER_FAST`, then `WAIT_SLOW` at deadline |
| `DISCOVER_FAST` | Complete unknown identity stable for 10s | Record reason | `UNSUPPORTED` |
| `DISCOVER_FAST` | Candidate already exact/current/active | Start stability proof | `VERIFYING` |
| `DISCOVER_FAST` | Eligible target not exact/current and application key is new | Persist admission; emit explicit load request | `APPLY_PENDING` |
| `APPLY_PENDING` | Queued event dirties observation before dispatch | Re-observe; leave admission re-emittable | `APPLY_PENDING` or discovery state |
| `APPLY_PENDING` | Keyed worker is accepted by the service manager | Persist attempted key and worker identity | `APPLYING` |
| `DISCOVER_FAST` | Application key already attempted | Wait for changed evidence | `DISCOVER_FAST` or `WAIT_SLOW` |
| `DISCOVER_FAST` | Aggressive deadline reached unresolved | Preserve intent; begin slow backoff | `WAIT_SLOW` |
| `WAIT_SLOW` | Same unresolved observation | Increase capped backoff | `WAIT_SLOW` |
| `WAIT_SLOW` | Evidence changes | Reset fast backoff, retain epoch/candidate as valid | `DISCOVER_FAST` |
| `WAIT_SLOW` | A unique probe candidate appears | Admit safe activation | `PROBE_PENDING` |
| `WAIT_SLOW` | Candidate becomes eligible/exact | Apply or verify | `APPLYING` or `VERIFYING` |
| `APPLYING` | Same worker still running and observation changes | Preserve admission; do not classify or dispatch another display action | `APPLYING` |
| `APPLYING` | Physical topology changes while worker may still mutate X | Request idempotent stop; preserve worker admission until completion callback | `APPLYING` |
| `APPLYING` | Command completes | Drain events and re-observe | classify into discovery/verification |
| `APPLYING` | Command succeeds | Re-observe without repeating the attempted key | discovery or `VERIFYING` |
| `APPLYING` | Command fails or restart makes outcome unknowable | Preserve terminal attempt evidence; do not silently deduplicate it into waiting | `APPLY_FAILED` |
| `APPLY_FAILED` | Same evidence | Report only; never repeat side effects automatically | `APPLY_FAILED` |
| `APPLY_FAILED` | Genuinely changed target/evidence | Clear terminal context and classify anew | `DISCOVER_FAST` |
| `VERIFYING` | Event, contradiction, inactive/extra output, or no longer current | Reset proof; revoke only on contradiction/topology change | `DISCOVER_FAST` |
| `VERIFYING` | Exact proof younger than 10s | Continue proof | `VERIFYING` |
| `VERIFYING` | Proof complete; target equals finalized desktop profile | Record stable X profile only | `QUIESCENT` |
| `VERIFYING` | Proof complete during startup baseline adoption | Adopt desktop baseline without relayout | `QUIESCENT` |
| `VERIFYING` | Proof complete; target differs from finalized desktop profile | Admit durable transaction | `FINALIZE_PENDING` |
| `FINALIZE_PENDING` | Queued event dirties observation before dispatch | Re-observe; leave transaction re-emittable | `FINALIZE_PENDING` or discovery state |
| `FINALIZE_PENDING` | Adapter acknowledges dispatch | Persist running status | `FINALIZING` |
| `FINALIZING` | Same unit still running and topology valid | No duplicate launch | `FINALIZING` |
| `FINALIZING` | Topology changes | Stop stale unit; keep prior finalized profile | `DISCOVER_FAST` |
| `FINALIZING` | Unit succeeds | Preserve a result-pending tombstone and re-observe | `FINALIZING` |
| `FINALIZING` | Fresh valid observation confirms the completed transaction | Persist finalized profile and acknowledge transaction | `QUIESCENT` |
| `FINALIZING` | Observation is temporarily invalid after completion | Keep the tombstone and continue probing; never rerun desktop work | `FINALIZING` |
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

## Safe monitor activation probe

The activation probe resolves a specific hardware deadlock before autorandr
application. It is neither optimistic desktop preparation nor profile
application. The adapter runs only the admitted command equivalent to:

```text
xrandr --output PROBE_OUTPUT --mode ADMITTED_PREFERRED_MODE --right-of ACTIVE_INTERNAL_OUTPUT
```

`ADMITTED_PREFERRED_MODE` is copied from the same clean observation which
admitted the action, so a later mode-list change fails the command instead of
silently selecting a different mode. The adapter does not set primary output,
load an autorandr profile, move windows, or run a
postswitch hook. Completion merely requests a fresh observation. Full EDID
integrity and ordinary autorandr/profile-topology matching must subsequently
select the target; probe success cannot manufacture that authorization.

The production action is keyed and persisted like profile application. It is
attempted once per `(physical_epoch, base_identity_profile, observation_key)`.
A restart after acknowledged dispatch records an unknown/failed outcome rather
than repeating the mode set. A changed physical topology cancels the admission.

## Optimistic desktop preparation and disruptive commit

Waiting for full stability before starting any desktop work imposes the entire
verification interval in front of the already long `setup-monitor` pipeline.
Instead, begin repeatable preparation once a target has a short clean proof of
an exact active X topology, continue observing concurrently, and reserve the
most disruptive work for a separately authorized commit.

### Phase 1: preflight and planning

Start immediately for a plausible exact target. Determine the layout, bind the
work to its transition and physical epoch, load display data, validate screen
counts, refresh libdpy caches, and calculate the intended overlay, panel
properties, DPI, and other configuration. Prefer staging calculated values
under the transition ID so cancellation before mutation only removes that
transaction directory.

### Phase 2: optimistic soft application

Apply repeatable configuration which a newer transaction can safely supersede:

- install the selected Fluxbox overlay;
- update panel properties;
- apply layout DPI;
- configure terminal fonts and themes;
- reload Emacs fonts; and
- generate Fluxbox configuration.

These changes are not necessarily invisible, but they avoid window movement
and process restarts. Continue probing throughout this phase. Contradictory
physical topology, active X topology, or usable identity evidence cancels the
worker; temporary EDID absence alone does not. Split the current
`fluxbox-reconfigure` helper so configuration generation can happen here while
`fluxbox-remote Reconfigure` remains part of commit. Move `setup_keyboard` out
of this phase because a speculative target must not change external keyboard
connection state.

### Phase 3: disruptive commit

Authorize commit only after a fresh observation still has the same physical
epoch, target layout, exact active X topology, no contradictory identity, no
queued DRM event, and a short clean interval. Then:

1. apply the staged Fluxbox configuration and keyboard intent;
2. run `ly` to place windows;
3. restart Fluxbox;
4. restart `xfce4-panel`; and
5. restart `nm-applet`, wait for the tray, and capture diagnostics.

Check the transition at safe boundaries. Do not hard-kill an atomic restart
step merely because newer evidence arrived; finish that step, then cancel or
run a corrective transaction. A successful process exit is still not durable
completion: re-observe and commit `desktop_finalized_profile` only if the same
transition remains authorized.

The controller therefore has orthogonal display-convergence and desktop-worker
lifecycles. Full ten-second proof is not a prerequisite for all work; beginning
useful repeatable work and declaring the desktop transition complete use
separate confidence thresholds.

## Desktop finalization transaction

Autorandr postswitch must cease being authoritative. After stable proof, create
a durable transition ID only when `candidate_profile !=
desktop_finalized_profile`.

The production implementation should run `setup-monitor` synchronously in a
dedicated oneshot systemd unit keyed by that ID. The unit is outside the
watcher's cgroup and remains discoverable after watcher restart. Repeated starts
of the same active instance are no-ops. Admission, dispatch acknowledgement,
and completion are separate persisted steps. Even after a successful unit
exit, the watcher updates `desktop_finalized_profile` only after a fresh valid
observation proves that the transaction still describes the current topology.
Pending admissions require that same fresh observation before dispatch after a
watcher restart; acknowledged running units are reattached instead. Each new
admission receives a monotonically increasing transaction sequence, so failed
or cancelled IDs are never reused after an away-and-back transition.

Strict exactly-once completion across arbitrary power loss is impossible
because `setup-monitor` is not transactional. This protocol provides
exactly-once admission/launch across watcher restarts and prevents automatic
rerun of a failed transaction under the same ID.

## Spike interface

The non-deployed Bash spike is intentionally smaller than the future adapters.
It proves the reducer policy with synthetic observations. Every `OBSERVE` input
contains:

```text
key physical_token external_state eligible_profile eligible_scope exact_profile current_profile valid
```

where `external_state` is `none`, `unresolved`, `probeable`, `known`, or
`unknown`. Optional trailing `identity_profile`, `probe_output`,
`probe_internal_output`, and `probe_mode` values fully describe the admitted
command only when `external_state=probeable`. The reducer emits symbolic
actions such as:

```text
SCHEDULE delay_ms
PROBE profile output internal_output mode probe_key
APPLY profile application_key
FINALIZE transition_id profile
STOP_PROBE probe_key
STOP_APPLICATION application_key
STOP_FINALIZER transition_id
```

It never executes autorandr, xrandr, systemd, or `setup-monitor`. The repository's
`.stow-local-ignore` anchors `^specs$`, and a Stow dry run must confirm that the
spike is not linked into `$HOME`.

## Required trace coverage

Table-driven traces must assert state, next timer, autorandr applications, and
desktop finalizations for:

- laptop-only startup already current;
- genuine laptop-to-external plug;
- readiness arriving after the aggressive deadline without a DRM event;
- EDID ready → missing → ready;
- exact known base identity with broken extensions → one safe activation probe
  → later full match → application;
- invalid/mixed observations never admitting a probe;
- probe admission interrupted before dispatch, failed probe, physical change
  during an acknowledged probe, and restart without duplicate execution;
- transient laptop-only detection while external remains connected;
- genuine unplug;
- resume to the same external profile;
- automatic X transition requiring desktop-only finalization;
- renamed connector with unchanged profile identity;
- stable unknown external monitor;
- restart in every non-stable phase;
- unchanged observation/application deduplication;
- same-token and changed-token events during acknowledged probe/application
  workers without concurrent actions or lost completion tracking;
- finalizer reattachment without duplicate launch.

The trace must fail if an internal-only application occurs while external
hardware is present, if invalid evidence admits a probe, if identical evidence
causes repeated probe/application, or if
an unresolved external state lacks a future timer. It must also distinguish
admission from dispatch, preserve completed-action tombstones across uncertain
samples, and reject truncated, duplicate, semantically invalid, or
arithmetic-bearing persisted records without partial mutation.

## Migration outline

1. Land and review the pure reducer spike.
2. Implement the canonical observation adapter, base-identity/integrity
   classification, safe activation-probe adapter, and EDID bijection tests.
3. Add versioned persistence and event/timer scheduler in shadow/dry-run mode.
4. Compare shadow decisions with the existing watcher during real hub cycles.
5. Add durable desktop-finalizer units and remove postswitch-marker authority.
6. Switch the service entry point only after synthetic tests and repeated live
   Samsung reconnects pass; retain `monitor-system legacy` as the rollback.
