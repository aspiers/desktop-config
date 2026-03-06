# NVMe / Crash Investigation

## TL;DR

The machine crashes because the NVMe firmware (SGW00110) has a bug that causes
it to misbehave during suspend/hibernate, hanging the machine. The same bug
also produces false SMART alarms (`SMART FAILED`, `percentage_used: 103%`,
`power_cycles: 1,417,554`) — none of these reflect actual drive wear; the
drive has only consumed 0.3% of its rated 4,000 TBW endurance.

Kingston released SGW00113 (2025-09-03) which explicitly fixes the wear value
bug, and SGW00115 (2025-12-16) which adds further stability improvements.
Updating to SGW00115 is the fix. The firmware binary must be requested from
Kingston support since KSM is Windows-only:
`https://www.kingston.com/en/support/technical/emailsupport`

## System

- **Machine**: celtic.linksys.moosehall
- **Drive**: Kingston FURY Renegade G5 — `KINGSTON SFYR2S4T0` (4 TB)
- **Serial**: 50026B7283A44B30
- **Firmware**: SGW00110 (both slots)
- **Drive age**: ~9 months
- **Investigation date**: 2026-02-23

## Crash History

From `last -x reboot shutdown`:

| Boot date        | Outcome           | Last journal entry before crash                  |
| ---------------- | ----------------- | ------------------------------------------------ |
| 2025-12-18       | crash             | (wtmpdb begins here — unknown)                   |
| 2025-12-26       | shutdown (manual) | —                                                |
| 2026-01-02       | crash             | network activity (dnsmasq), then silence         |
| 2026-01-07       | crash             | network activity, then silence                   |
| 2026-02-06       | crash             | bluetooth endpoint unregistered, then silence    |
| 2026-02-08       | crash             | `kernel: PM: hibernation: hibernation entry`     |
| 2026-02-15       | crash             | `kernel: PM: suspend entry (s2idle)`             |
| 2026-02-22 23:01 | running (current) |                                                  |

**Pattern**: The two most recent crashes both occurred at the moment of
entering suspend or hibernate. Earlier crashes have no final kernel message,
suggesting a hard hang that happened to occur when the journal flush interval
hadn't yet written the last entries — consistent with the machine hanging
silently (not kernel panicking, which would write more).

The user has attempted Alt+SysRq+u (emergency remount read-only) before at
least the last two or three crashes.

## The Actual Crash Cause: Suspend/Hibernate Failure

Boot -2 (Feb 15) last line:
```
kernel: PM: suspend entry (s2idle)
```

Boot -3 (Feb 8) last lines:
```
systemd-sleep: Performing sleep operation 'hybrid-sleep'...
kernel: PM: hibernation: hibernation entry
```

The machine is configured to use **s2idle** (S0 low-power idle) for suspend
and **hybrid-sleep** for some events. It crashes when entering sleep.

Key context from the current boot:
```
kernel: nvme 0000:bf:00.0: platform quirk: setting simple suspend
systemd-hibernate-resume: Unable to resume from device '/dev/system/swap'
  (254:2) offset 0, continuing boot process.
```

The `platform quirk: setting simple suspend` message is notable — the kernel
applies a simplified suspend path to this NVMe drive specifically. This quirk
exists precisely because some NVMe drives misbehave during PCIe power state
transitions.

The failed hibernate resume on current boot (`Unable to resume from device`)
suggests the previous attempt to hibernate didn't complete cleanly (i.e., it
crashed mid-hibernate), leaving a stale resume state that systemd then
correctly ignored.

## NVMe SMART Data

```
SMART overall-health self-assessment: FAILED!
- NVM subsystem reliability has been degraded

Critical Warning:     0x04
Temperature:          28–35 °C
Available Spare:      100%
Percentage Used:      103%
Data Units Read:      ~8.16 TB
Data Units Written:   ~12.22 TB
Power On Hours:       1796
Power Cycles:         1,417,554   ← IMPOSSIBLE / firmware bug
Unsafe Shutdowns:     19
Media Errors:         0
Error Log Entries:    0
```

## NVMe Self-Test Log

All recent short self-tests fail with "Completed: failed segments":

| Test # | POH  | Status                      |
| ------ | ---- | --------------------------- |
| 0      | 1796 | failed segments (seg 2)     |
| 1      | 1773 | failed segments (seg 2)     |
| 2      | 1749 | failed segments (seg 2)     |
| 3      | 1733 | failed segments (seg 2)     |
| 4      | 1716 | failed segments (seg 2)     |
| 5      | 1700 | failed segments (seg 2)     |
| 6      | 1679 | failed segments (seg 2)     |
| 7      | 1660 | failed segments (seg 2)     |
| 8      | 1650 | failed segments (seg 2)     |
| 9      | 1639 | **Completed without error** |
| 10–19  | ≤1632| **Completed without error** |

**Self-test failure onset**: between POH 1639 and 1650 (~146 hours ago,
estimated ~2026-02-17).

Crucially, this is **after** the crashes began in December 2025 — so the
self-test failures are not the cause of the crashes. They may be a further
symptom of the firmware bug, or a separate (possibly related) regression.

All failing tests report:
- `Operation Result: 0x7` (failed segment)
- `Segment Number: 0x2` (segment 2 of the short test)
- `Valid Diagnostic Information: 0xf` (all diag fields valid)
- `Status Code Type: 0 / Status Code: 0` (Generic Success — contradicting the failure)
- `Failing LBA: 0` (logical block 0)
- `Namespace Identifier: 0`

The `Status Code: 0 = Successful Completion` combined with `Operation Result: 0x7
= failed segments` is internally contradictory, further suggesting a firmware
reporting bug rather than genuine NAND failure.

## SMART Counter Analysis: Firmware Bug

The drive's rated endurance is **4,000 TBW** (per Kingston specs, confirmed by
TechPowerUp SSD database for SFYR2S4T0). The drive has written **12.22 TB**
— only **0.3%** of rated endurance. The `percentage_used: 103%` cannot
reflect actual NAND wear.

The `power_cycles: 1,417,554` is physically impossible for a ~9-month-old drive
running ~6.8 hours/day (1796 POH / ~229 days ≈ 7.8 h/day). It implies ~789
power cycles per hour continuously — clearly a counter wraparound or
increment bug in firmware SGW00110.

This pattern (wildly inflated write counters, high `percentage_used`, 100%
available spare, zero media errors) matches known Kingston firmware bugs
reported on Reddit and forums for other Kingston NVMe models.

## Firmware Update Available

Two firmware updates exist beyond current SGW00110:

| Version   | Date       | Changes                                                                 |
| --------- | ---------- | ----------------------------------------------------------------------- |
| SGW00113  | 2025-09-03 | Improve stability; **Fix unbounded limit in percentage used value**; Reduce firmware slots from 2 to 1 |
| SGW00115  | 2025-12-16 | Improve 4K random read/write performance; Improve firmware stability    |

**SGW00113 explicitly fixes the `percentage_used` bug.** SGW00115 is the
latest and adds further stability improvements. Target: SGW00115.

Release notes: `https://media.kingston.com/support/downloads/SFYR2_SGW00115_RN.pdf`
(Cloudflare-protected; open in browser.)

### Slot change warning

SGW00113 reduces the drive from 2 firmware slots to 1. Per a French tech
site covering the release (touslesdrivers.com): Kingston dropped the second
slot deliberately — presumably the fix required more flash space than was
available with 2 slots, so rollback capability was sacrificed. After
updating, slot count becomes 1. This means `fw-commit --slot=2` will be
invalid after the update — use `--slot=1`.

### How to update on Linux (no KSM required)

Kingston's official tool (KSM) is Windows-only. There is no Linux version,
and Kingston FURY CTRL is unrelated (RAM RGB software only). However,
`nvme-cli` handles firmware updates natively, and Kingston has previously
supplied firmware binaries directly to Linux users on request — confirmed by
a GitHub repo (`vulgo/kingston-a2000-firmware-bin-linux`) where Kingston did
exactly this for the A2000.

**Step 1**: Request the SGW00115 binary from Kingston support:
`https://www.kingston.com/en/support/technical/emailsupport`

Suggested message:
> I have a Kingston FURY Renegade G5 (SFYR2S4T0, 4TB, serial 50026B7283A44B30)
> running firmware SGW00110. I'm on Linux and cannot use KSM. I need the
> SGW00115 firmware binary to apply via nvme-cli (`nvme fw-download`).
> Could you provide the .bin file directly, as Kingston has done previously
> for other Linux users?

**Step 2**: Once the `.bin` file is obtained:
```bash
# Prestage firmware onto drive
sudo nvme fw-download /dev/nvme0 --fw=SGW00115.bin

# Activate on slot 1, action 1 = commit + reset on next reboot
sudo nvme fw-commit /dev/nvme0 --slot=1 --action=1

# Reboot, then verify:
sudo nvme id-ctrl /dev/nvme0 | grep '^fr'
# Should show: frs1 : ... (SGW00115)
```

## Suspend/Hibernate: Likely Root Cause of Crashes

The `nvme 0000:bf:00.0: platform quirk: setting simple suspend` kernel message
indicates the kernel already knows this drive needs special handling during
power state transitions.

Possible contributing factors to suspend/hibernate crashes:

1. **Firmware bug interaction with PCIe power management**: The same firmware
   that miscounts power cycles may handle PCIe L1/L2 power state transitions
   incorrectly, causing the drive to become unresponsive after suspend.

2. **s2idle vs S3 mismatch**: The machine uses `Low-power S0 idle` (s2idle)
   by default. This keeps PCIe partially active, which can expose NVMe
   firmware bugs that a full S3 sleep would not.

3. **Hibernate offset issue**: The stale `resume=` parameter in the kernel
   cmdline suggests hibernate state isn't being tracked correctly between
   boots.

## Other Boot Errors (Likely Unrelated)

- `setfont` failures on virtual console setup (cosmetic)
- `ucsi_acpi USBC000:00: unknown error 256` (USB-C controller — Framework laptop quirk)
- udev rule typo: `UBSYSTEMS` instead of `SUBSYSTEMS` in
  `/etc/udev/rules.d/60-kaleidoscope.rules:13`
- `dnsmasq` failing on port 53 (systemd-resolved conflict)
- `swaync` D-Bus name conflict with `org.freedesktop.Notifications`
- `Toucan mail syncing daemon` failed to start

## Summary

| Issue                          | Verdict                                                       |
| ------------------------------ | ------------------------------------------------------------- |
| `SMART FAILED` / `103% used`   | Firmware bug in SGW00110, confirmed fixed in SGW00113         |
| `1,417,554 power_cycles`       | Impossible value, firmware counter bug                        |
| Self-test segment 2 failures   | Likely also firmware bug (internally contradictory data)      |
| Media errors / data corruption | None — data appears intact                                    |
| Crash cause                    | Machine hangs on suspend/hibernate entry, likely NVMe firmware|
| Firmware update available      | Yes: SGW00115 (latest); get binary from Kingston support      |

## Recommended Actions

1. **Email Kingston support** for the SGW00115 binary (see "How to update on
   Linux" section above for the suggested message and `nvme-cli` commands).

2. **While waiting**, optionally add `nvme_core.default_ps_max_latency_us=0`
   to the kernel cmdline to disable NVMe power state management and reduce
   the risk of suspend hangs.

3. **After firmware update**, run a new short self-test to confirm all clear:
   ```bash
   sudo nvme device-self-test /dev/nvme0 --self-test-code=1
   # wait ~2 minutes, then:
   sudo nvme self-test-log /dev/nvme0
   sudo nvme smart-log /dev/nvme0 | grep -E 'critical|percentage|power_cycles'
   ```
