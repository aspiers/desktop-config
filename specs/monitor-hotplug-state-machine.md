# Plan: Monitor Hotplug State Machine

## Status: PROPOSED

## Problem Statement
`bin/monitor-watcher` currently reacts to a DRM event, tries to make the
display state valid, and then runs `bin/setup-monitor` synchronously.

This has three problems:

1. XRandR can lag behind the DRM event, so the watcher can accept a
   stale layout too early.
2. Some topologies are known partial states. On `celtic`, a home-dock
   transition can expose BenQ before Dell. In that state we should wait,
   not run `setup-monitor`.
3. `setup-monitor` is long-running. While it is running, the watcher is
   blind to newer monitor changes.

## Goals
- Make layout detection report whether the current state is ready,
  partial, or erroneous.
- Do not start `setup-monitor` until the topology is both ready and
  stable.
- Let `monitor-watcher` supervise a background `setup-monitor` run.
- Prefer cooperative stale termination at explicit checkpoints instead
  of aggressive killing.
- Keep a hard kill path as a last resort.

## Proposal

### 1. `get-layout --state`
Extend `bin/get-layout` with a machine-readable state mode.

Output format:

```text
<status>\t<value>
```

Statuses:
- `ready`: `<value>` is the layout file path
- `wait`: `<value>` is a human-readable reason
- `error`: `<value>` is a human-readable reason

For `celtic`, initial rules should include:
- laptop only: `ready`
- single generic external monitor: `ready`
- BenQ present and total monitor count is 2: `wait`
- BenQ present and total monitor count is 3: `ready` with
  `celtic+BenQ+Dell`

### 2. Settled Ready State In `monitor-watcher`
After a DRM event, `monitor-watcher` should poll until it sees a settled
ready state.

Each probe should:
- load the X display environment
- run `xrandr --auto`
- clear and repopulate display cache
- read monitor MD5
- call `get-layout --state`
- validate ready layouts with `liblayout.py --check-screen-counts`

The watcher should only treat a state as actionable when the same ready
layout and monitor MD5 have remained unchanged for a short settle
window.

### 3. Supervised Background `setup-monitor`
`monitor-watcher` should start `setup-monitor` in the background, keep
watching monitor state while it runs, and remember:
- expected layout
- expected MD5
- child PID

If a different settled ready state appears while a child is running:
- request termination of the stale child
- wait briefly for it to exit cooperatively
- kill as a last resort if it does not exit
- launch a fresh child for the newest settled state

### 4. Cooperative Stale Checks In `setup-monitor`
`setup-monitor` should accept:

```text
--layout <layout>
--expected-layout <layout>
--expected-md5 <md5>
```

It should validate expected state at safe checkpoints, including:
- after initial layout selection
- after `setup_xrandr`
- after cache refresh
- before Fluxbox restart
- before panel/tray restarts

If the expected state no longer matches live state, it should exit with
a dedicated stale exit code.

### 5. Termination Policy
- Normal stale handling: cooperative exit at checkpoints
- Escalation path: `monitor-watcher` sends `TERM` to the child
- Final fallback: `monitor-watcher` kills the stale child process group

The hard-kill path exists for stuck runs, not as the normal mechanism.

## Why This Approach
- It keeps the existing host-specific logic in the existing codebase.
- It makes partial topologies explicit instead of inferring them from
  failures.
- It removes the watcher blind spot during long-running setup.
- It minimizes the risk of interrupting timing-sensitive desktop changes
  in the middle of a phase.

## Files To Modify
- `bin/get-layout`
- `bin/monitor-watcher`
- `bin/setup-monitor`

## Validation
- laptop-only event should not trigger unnecessary reconfiguration
- single external monitor should settle and configure normally
- BenQ-then-Dell dock sequence should wait for the 3-monitor layout
- changing topology during `setup-monitor` should cancel stale work and
  restart for the latest settled state
