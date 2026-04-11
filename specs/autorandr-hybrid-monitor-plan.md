# Plan: Autorandr Hybrid Monitor Management

## Status: PROPOSED

## Problem Statement
The current custom monitor hotplug flow has three weak spots:

1. A DRM hotplug event can arrive before XRandR fully reflects the new
   monitor topology.
2. Some topologies surface in stages, especially through USB hubs or
   docks.
3. `bin/setup-monitor` is long-running, so a monitor change can make an
   in-flight run stale.

The display-profile detection and raw `xrandr` application layer is the
most likely place to simplify the system.

## Proposal
Use `autorandr` for profile matching and raw `xrandr` application, while
keeping local scripts for the rest of the desktop reconfiguration.

This is a hybrid design, not a full migration.

## What Autorandr Would Replace
- Detecting connected monitor hardware.
- Matching the current hardware to a saved display profile.
- Applying the corresponding `xrandr` configuration.
- Ignoring unmatched transient states by not loading any profile.

This would largely replace the display-profile-detection role currently
spread across `bin/monitor-watcher`, `bin/get-layout`, and the
`setup_xrandr()` part of `bin/setup-monitor`.

## What Stays Custom
- Host-specific semantic layout naming such as `celtic+BenQ+Dell`.
- Fluxbox layout YAML validation.
- Overlay, DPI, panel, keyboard, terminal, Fluxbox, and XFCE panel
  reconfiguration.
- Any policy which says that a known hardware state should still wait.
- Detection of stale long-running reconfiguration.

## Why This Could Help
- Autorandr is built around matching saved stable topologies.
- It can avoid changing anything when a transient partial topology does
  not match a saved profile.
- It has documented hook support for `predetect`, `preswitch`,
  `postswitch`, and `block`.
- It explicitly documents use of `predetect` for cases where xrandr runs
  too early after a monitor hotplug.

## Limitations
- It does not replace the rest of `setup-monitor`.
- It does not by itself solve stale in-flight reconfiguration.
- If the same physical state is sometimes valid and sometimes only an
  intermediate state, extra policy is still needed.

## High-Level Plan
1. Save autorandr profiles for the stable monitor topologies that should
   be auto-applied.
2. Do not define autorandr profiles for known transient topologies that
   should never be auto-applied.
3. Add a small `predetect` delay to reduce immediate hotplug races.
4. Move non-xrandr follow-up work into an autorandr-compatible wrapper,
   probably driven from `postswitch`.
5. Add stale-state checks to that wrapper so it can abort if the monitor
   state changes while it is still running.

## Suggested Validation
- laptop only
- single external monitor hotplug
- BenQ then Dell surfacing at different times via hub/dock
- unplug while post-switch work is still running

## Recommendation
This is worth reconsidering, but only as a partial replacement.

Autorandr looks like a good fit for the profile-detection and raw
`xrandr` layer. The rest of the desktop reconfiguration logic still
needs custom code, and stale-run protection remains necessary either
way.
