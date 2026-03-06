# Plan: Debug and Fix INXI EDID Parsing

## Status: RESOLVED (won't fix inxi; bypassed via hwinfo hybrid detection)

## Problem Statement
INXI is failing to detect monitor models for external displays (Dell U2414H
and BenQ BL3200), showing them as "not-matched" with error
"parse_edid: unknown tag 112" (0x70).

## Root Cause Analysis

Investigation revealed **two separate issues**, only one of which
causes the "not-matched" problem:

### 1. "unknown tag 112" warnings (cosmetic, from laptop display)
Tag 0x70 is a valid **DisplayID extension block** type. The laptop's
BOE panel has two of these extension blocks in its 384-byte EDID. Inxi
doesn't handle DisplayID extensions, so it logs warnings. These
warnings do NOT affect the laptop display detection (monitor name is
in the base EDID block which parses fine). The `bin/inxi-debug` copy
already silences these.

### 2. `map_monitor_ids` matching failure (the actual bug)
Inxi's EDID parsing extracts monitor names correctly for all three
monitors. The real failure is in `map_monitor_ids()` (line ~19744)
which matches kernel connector names to X11 connector names. It
assumes port numbers are equal or off-by-one:

```perl
if ($d_1 eq $s_1 && ($d_m == $s_m || $d_m == ($s_m - 1)))
```

With AMDGPU, the numbers are completely unrelated:

| Kernel (sysfs) | X11 (xrandr)   | Match? |
| -------------- | -------------- | ------ |
| DP-10          | DisplayPort-12 | No     |
| DP-13          | DisplayPort-9  | No     |
| eDP-1          | eDP            | No (pattern mismatch) |

Since no sys↔display match is found, monitor EDID data (including
model names) is never associated with the X11 outputs.

## Resolution

Rather than patching inxi's fragile port-number matching, implemented
the hwinfo hybrid detection approach. See
`specs/hwinfo-fallback-monitor-detection.md` for the implemented
solution.

## Upstream Bug Report

The inxi `map_monitor_ids` bug could still be reported upstream. The
fix would be to add EDID-based matching (e.g. by serial number) as a
fallback when port number heuristics fail. However, since we now
bypass inxi for monitor model detection entirely, this is low priority.
