# Framework 13 AMD Ryzen AI 9 HX 370 - Power Optimization Work in Progress

## Current Status Summary

### What We've Discovered

1. **Webcam Power Issue - ROOT CAUSE IDENTIFIED**
   - Initial symptoms: powertop showed webcam at 75.5% usage
   - Disabling WirePlumber monitoring (both `monitor.v4l2` and `monitor.libcamera`) successfully removed the webcam from WirePlumber's device list
   - However, this did NOT resolve the power drain
   - **True culprit:** The `uvcvideo` kernel driver itself keeps the webcam active even when nothing is using it
   - **Proof:** Unloading the driver with `sudo modprobe -r uvcvideo` caused:
     - Webcam immediately suspended (verified via `/sys/bus/usb/devices/3-3/power/runtime_status`)
     - Powertop showed webcam usage dropped to 0.0%
   - This is a kernel driver bug/limitation, not userspace polling

### Fixes Applied So Far

1. **WirePlumber Camera Monitor Disabled** ✅
   - File: `~/.config/wireplumber/wireplumber.conf.d/99-disable-camera-monitor.conf`
   - Content:
     ```
     wireplumber.profiles = {
       main = {
         "monitor.v4l2" = disabled
         "monitor.libcamera" = disabled
       }
     }
     ```
   - Result: Prevents WirePlumber from proactively monitoring the webcam (confirmed via `wpctl status`)
   - Impact: Necessary but not sufficient - driver still keeps device active

2. **System-level WirePlumber Config Removed** ✅
   - Removed redundant `/etc/wireplumber/wireplumber.conf.d/99-disable-camera-monitor.conf`
   - User-level config in `~/.config/` is sufficient and takes precedence

### Current Power Consumption Status

**UPDATED 2025-10-28 - Latest measurements:**
- ✅ Fixed EPP from `performance` to `power` mode
- ✅ Dimmed display from 80% to 23% brightness
- ✅ Tested Bluetooth impact (~0.5W)
- ✅ Tested "balanced" vs "power-saver" profile (power-saver is better)
- ✅ Closed atuin-desktop (~0.5W)
- **Current discharge: ~10.9W** (active workload: Chrome, Cursor, Telegram)
- **Total progress: ~14W saved** (from initial 25.5W)
- **Wakeups: ~7600/s** (active) - needs testing at true idle
- Still need to test: True idle power/wakeups with all apps closed

**Previous status (before EPP fix):**
- Powertop typically shows 3,000-5,000 wakeups/second when on battery
- Discharge rate frequently exceeds 10W, often reaching 15W or higher
- This is still 2-3x higher than the expected 4-7W idle range

**Recent powertop snapshot (with uvcvideo loaded):**
```
Summary: 4634.4 wakeups/second
Usage       Events/s    Category       Description
143.8 pkts/s            Device         Network interface: wlp192s0 (mt7925e)
100.0%                  Device         Radio device: btusb
75.5%                   Device         USB device: Laptop Webcam Module (2nd Gen) (Framework)
100.0%                  Device         Display backlight
```

**After unloading uvcvideo driver:**
- Webcam usage: 0.0%
- Webcam autosuspended

## Technical Analysis

### Why Disabling WirePlumber Monitoring Wasn't Enough

The Gemini deep research document suggested that WirePlumber/libcamera was "aggressively polling" the webcam. This was misleading. The actual mechanism is:

1. The `uvcvideo` kernel driver loads at boot for any UVC-compatible webcam
2. Even with no userspace processes accessing `/dev/video*`, the driver keeps the USB device active
3. This prevents USB autosuspend from working
4. The device stays powered, burning watts and generating interrupts

This is NOT continuous polling - it's the driver's failure to allow the device to enter USB suspend state. The fix mentioned in Framework community forums was a **libcamera patch** for keeping `/dev/video*` open, but we've confirmed nothing is holding the device files open on this system.

### Outstanding Issues from powertop

Based on Gemini deep research recommendations, we still need to address:

1. **Bluetooth (btusb)** - 100.0% usage
   - Not autosuspending properly
   - Solution: Enable `btusb` autosuspend via modprobe.d

2. **Wi-Fi (mt7925e)** - 143.8 pkts/s
   - High activity even when idle
   - MediaTek MT7925E is a new Wi-Fi 7 card with immature Linux driver
   - Solution: System updates + PCIe ASPM optimization

3. **CPU Frequency Scaling** - Not yet verified
   - Need to check if `amd-pstate` driver is active and in EPP mode
   - Solution: Add `amd_pstate=active` kernel parameter if needed

## Next Steps - Action Plan

### CRITICAL: Research Methodology

**For each issue, we must:**
1. **Check if problem is known upstream** - Search kernel mailing lists, bug trackers, GitHub issues
2. **Find latest solutions** - Check for patches, workarounds, kernel version requirements
3. **Report if unknown** - File bug reports with proper diagnostics if issue is not documented

### Immediate Actions (Highest Impact)

#### 1. Verify and Configure AMD P-State Driver ✅ COMPLETED - MAJOR ISSUE FOUND AND FIXED
**Priority:** HIGHEST - CPU power management is foundational

**Status:** RESOLVED (2025-10-28)

**Findings:**
- ✅ Driver: `amd-pstate-epp` is active (correct - EPP mode for "active" configuration)
- ✅ Governor: `powersave` (correct)
- ℹ️  No explicit `amd_pstate=` kernel parameter set (using default active mode)
- ❌ **CRITICAL BUG:** EPP was set to `performance` while on battery with `balanced` profile
- ✅ **FIX APPLIED:** Switched to `power-saver` profile using `powerprofilesctl set power-saver`

**AMD P-State Modes (from https://github.com/Hekel1989/amdpstate-configuration):**
- **Passive** (kernel 6.1+): Processor targets performance levels relative to max capacity
- **Active** (kernel 6.3+): Firmware receives performance/efficiency hints via EPP driver ← **Current mode**
- **Guided** (kernel 6.4+): Platform auto-selects performance based on workload
  - Potential optimization: Try `amd_pstate=guided` kernel parameter

**Power Impact:**
- Before: ~13-15W discharge on battery
- After 1 minute: 9.10W discharge
- **Improvement: ~4-6W saved** (~30-40% reduction!)

**Root Cause Analysis:**
All 24 CPU cores were stuck on `performance` EPP despite power-profiles-daemon reporting `balanced` profile. This caused CPUs to run at high frequencies and voltages unnecessarily on battery power.

**Verification completed:**
```bash
# Before fix
$ powerprofilesctl get
balanced
$ cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
performance  # <-- WRONG for battery!

# After fix
$ powerprofilesctl set power-saver
$ cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
power  # <-- CORRECT

# Comprehensive verification with amd-pstate tool
$ ~/.local/bin/amd-pstate triage
# Shows:
# - Status: active ✅
# - Prefcore: enabled ✅
# - All 24 CPUs: EPP = power ✅
# - Boost: disabled (0) ✅
# - Scaling frequencies properly constrained for power saving ✅
```

**Outstanding Questions:**
1. Why was `balanced` profile mapping to `performance` EPP instead of `balance_performance` or `balance_power`?
   - This needs upstream investigation
   - [ ] Search power-profiles-daemon issue tracker for similar reports
   - [ ] Check if this is specific to Ryzen AI 300 series
   - [ ] Verify if power-profiles-daemon needs updating

2. **CRITICAL: Does EPP reset to "performance" after suspend/resume?**
   - **Known kernel 6.15 regression on Ryzen 7040**: EPP always resets to "performance" after resume from suspend
   - Framework community thread: https://community.frame.work/t/increased-power-usage-after-resuming-from-suspend-on-ryzen-7040-kernel-6-15-regression/74531
   - **Patch merged to mainline** by Mario Limonciello (thank you!)
   - Current kernel: 6.15.8 - **check if patch is included!**
   - **Workaround**: Re-run `powerprofilesctl set power-saver` after every resume
   - May explain why EPP was at "performance" - could be from previous suspend/resume cycle

**Recommendation:**
- Continue using `power-saver` profile when on battery
- **CRITICAL**: After each suspend/resume, verify EPP hasn't reset to "performance"
- Check command: `cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference`
- Consider creating a systemd resume hook to automatically reset EPP if needed

Reference: Gemini-deep-research.md:136-187

#### 2. Enable Bluetooth Autosuspend ✅ INVESTIGATED - NOT AN ISSUE
**Priority:** HIGH - Currently showing 100% usage
**Status:** NOT A BUG (2025-10-28)

**Findings:**
- ✅ `btusb` module has `enable_autosuspend=Y` (already enabled)
- ✅ USB device power control set to `auto` (correct)
- ✅ MediaTek wireless device at `/sys/bus/usb/devices/3-5`
- ℹ️  Device is `active` because Bluetooth mouse (MX Master 3S) is actively connected
- ℹ️  Active BT connection: `D8:35:48:AE:1C:69` (MX Master 3S)

**Conclusion:**
The "100% usage" in powertop is **expected and correct** - the Bluetooth radio must stay active to maintain connection with the mouse. This is not a power drain issue that can be fixed. The device will autosuspend when no Bluetooth devices are connected.

**No action required.**

#### 3. CPU Idle States (C-States) Investigation ✅ VERIFIED CORRECT
**Priority:** INFORMATIONAL
**Status:** RESOLVED - NO ISSUE (2025-10-28)

**CORRECTION OF EARLIER MISUNDERSTANDING:**
- ✅ CPUidle driver: `acpi_idle` (correct for AMD)
- ✅ C-states available: POLL, C1, C2, C3 (normal for AMD Ryzen)
- ℹ️  `intel_idle` module exists in kernel but is NOT active
- ℹ️  **C9/C10 states do NOT exist on AMD** - those are Intel-specific states

**Current C-state usage (CPU0):**
```
state0: POLL - Usage: 3149029 - Time: 69588857
state1: C1   - Usage: 23388465 - Time: 2280233207
state2: C2   - Usage: 47553864 - Time: 18951254899
state3: C3   - Usage: 44688799 - Time: 83143376979  (most time spent here)
```

**Research Findings:**
After thorough investigation and web research:

1. **AMD does not have C9/C10 states** - These are Intel-specific. AMD uses C1, C2, C3, C6, and newer models support ACPI C4
2. **Framework 13 AMD users report 1.6-4.5W idle on Linux** - This proves the hardware is capable
3. **The C-states shown are CORRECT for AMD** - POLL, C1, C2, C3 are the expected ACPI states
4. **intel_idle is NOT preventing anything** - It's built into kernel but not active; `acpi_idle` is correctly in use

**Conclusion:**
The C-states are working correctly. The power consumption issue is NOT due to missing C-states, but due to:
1. ✅ **FIXED**: CPU EPP was set to `performance` (now fixed → saved 4-6W)
2. Active devices (Bluetooth mouse, Wi-Fi, keyboard) preventing deep sleep
3. Possible other configuration issues still to be identified

**No action required for C-states.**

**Check current status:**
```bash
# Find Bluetooth USB device
lsusb | grep -i bluetooth
# Check autosuspend status
for dev in /sys/bus/usb/devices/*/product; do
  if grep -qi bluetooth "$dev"; then
    dir=$(dirname "$dev")
    echo "Device: $(cat $dev)"
    echo "Control: $(cat $dir/power/control)"
    echo "Status: $(cat $dir/power/runtime_status)"
  fi
done
```

**Implementation:**
```bash
# Create modprobe config
sudo tee /etc/modprobe.d/bluetooth-powersave.conf << 'EOF'
options btusb enable_autosuspend=1
EOF

# Rebuild initrd
sudo dracut --force

# Reboot
```

**Verification:**
```bash
# Check powertop "Tunables" tab for Bluetooth autosuspend = "Good"
# Re-run the status check above - should show "auto" and "suspended" when idle
```

Reference: Gemini-deep-research.md:91-113

#### 3. Investigate Wi-Fi High Activity
**Priority:** MEDIUM - High packet rate and driver maturity concerns

**Research phase:**
- [ ] Check current mt7925e driver version and compare with latest upstream
- [ ] Search for mt7925e idle power issues in kernel mailing list
- [ ] Check MediaTek driver GitHub/GitLab for known issues
- [ ] Identify what's causing 143.8 pkts/s when system should be idle

**Diagnostic commands:**
```bash
# Check driver version
modinfo mt7925e | grep -E "version|vermagic"
# Monitor network activity
sudo tcpdump -i wlp192s0 -c 100
# Check for broadcast/multicast storms
netstat -s | grep -i multicast
```

#### 4. Update System for Latest Wi-Fi Driver/Firmware
```bash
sudo zypper dup
```

**Rationale:** MT7925E driver improvements ongoing in newer kernels (6.14.3+)

Reference: Gemini-deep-research.md:114-135

#### 5. Verify/Enable PCIe ASPM
**Research phase:**
- [ ] Check if ASPM is causing any device instability issues on this platform
- [ ] Search for Framework 13 AMD ASPM issues/recommendations

**Check current policy:**
```bash
cat /sys/module/pcie_aspm/parameters/policy
# Check per-device ASPM status
lspci -vv | grep -A 10 "LnkCap\|ASPM"
```

**If not `powersave` or `powersupersave`:**
- Add kernel parameter: `pcie_aspm=force`
- Via YaST Boot Loader

Reference: Gemini-deep-research.md:127-133

#### 6. Address Webcam uvcvideo Driver Issue
**Priority:** LOW - Already confirmed not a major power contributor

**Status:** Proven that unloading uvcvideo drops webcam to 0% usage, but overall system power consumption remains high (10-15W). This suggests webcam is a minor contributor.

**Research phase:**
- [ ] Check upstream kernel bugzilla for uvcvideo autosuspend issues
- [ ] Search for Framework webcam specific uvcvideo bugs
- [ ] Check if newer kernels have fixes

**Deferred action:** Blacklisting uvcvideo until we confirm it's needed for overall power goals

### Measurement and Validation

After each change:
1. Reboot
2. Let system idle for 5+ minutes
3. Run `sudo powertop` and observe for 2-3 minutes
4. Record:
   - Total wakeups/second
   - Battery discharge rate (W)
   - Per-device usage percentages
   - Any devices still showing high usage

**Target metrics:**
- Idle power: 4-7W
- Wakeups: <500/second
- All devices: 0.0% usage or autosuspended

### Additional Considerations

From Gemini-deep-research.md:213-217:

1. **Expansion Cards:** Framework USB-A, HDMI, DisplayPort cards can draw ~1W idle
   - Consider using only USB-C cards for minimum power draw

2. **Browser Hardware Acceleration:** Not idle power, but affects media playback
   - Enable VA-API in Chrome/Firefox flags

3. **Display Backlight:** Currently showing 100% usage in powertop
   - May need separate investigation

#### 4. Wakeup Sources Analysis
**Status:** ANALYZED (2025-10-28)

**Top wakeup sources from powertop:**
- ℹ️  Radio device: btusb (100%) - Expected (Bluetooth mouse connected)
- ℹ️  Radio device: mt7925e (100%) - Wi-Fi card active
- ℹ️  USB xHCI controllers (100%) - Active due to connected devices
- ℹ️  USB Kinesis Keyboard (100%) - Active peripheral
- ℹ️  PCI AMD Device 151f (100%) - Integrated controller

**Process wakeups** (ms/s):
- powertop: 147.9 ms/s (measurement tool itself)
- claude: 101.3 ms/s (AI assistant)
- Chrome renderers: ~0.6 ms/s each
- Various background apps: <1 ms/s

**Analysis:**
Most wakeups are from legitimately active devices (Bluetooth mouse, keyboard, Wi-Fi). The high number of devices showing "100%" is expected when they're in use. The key issue preventing deep idle is NOT the wakeups themselves, but the **intel_idle driver preventing deep C-states**.

**Recommendation:** Fix intel_idle issue first, then re-evaluate if wakeups are still excessive.

## Summary: Priority Actions

### ✅ COMPLETED
1. **AMD P-State EPP Fix** - Switched from `performance` to `power` mode → **~4-6W saved**
2. **Bluetooth Investigation** - Confirmed working correctly (active for mouse)
3. **Webcam WirePlumber** - Disabled camera monitoring

### 🔍 REMAINING INVESTIGATION NEEDED
**Current Status: ~9-12W idle (target: 4-7W)**

The EPP fix gave us a major improvement (4-6W saved), but we're still ~5W above target.

**Remaining areas to investigate:**

1. **System not truly idle** - Active devices keeping things awake:
   - Bluetooth mouse connected (necessary, but costs power)
   - Wi-Fi active (143.8 pkts/s - investigate what's causing traffic)
   - Multiple Chrome tabs/processes
   - Background services

2. **Display Configuration** - Multiple power considerations:
   - **Screen brightness**: Framework users recommend 35-40/255 for optimal balance
   - **Fractional scaling in GNOME**: Known to be power inefficient
     - Workaround: Use integer scaling or 1680x1050 resolution instead
   - Current brightness level should be checked and potentially lowered

3. **NVMe SSD Power Management** - Framework-specific recommendation:
   - Set APST (Autonomous Power State Transition) to more aggressive mode:
     ```bash
     # Check current setting
     sudo nvme get-feature /dev/nvme0 --feature-id=2

     # Set to more aggressive power saving (value=2)
     sudo nvme set-feature /dev/nvme0 --feature-id=2 --value=2
     ```
   - This was reported to help in Framework community thread #44439

4. **Hardware Video Acceleration** - Critical for media playback:
   - **Chrome**: Verify hardware acceleration flags enabled
   - **Firefox**: Enable VA-API for hardware decoding
   - Use Mesa drivers (not proprietary)
   - Test with: `mpv --hwdec=auto video.mp4` or VLC Flatpak
   - Without this: Video playback can drain 50% battery in 2 hours

5. **Wi-Fi Card** - Hardware consideration:
   - MediaTek MT7925E is very new (Wi-Fi 7)
   - Framework community reports AMD RZ616 as potential power drain
   - Intel AX210 reported as alternative with better power efficiency
   - Check if firmware/driver updates available: `sudo zypper dup`

6. **Power-Profiles-Daemon Version** - Software consideration:
   - Verify PPD version is recent (0.30 installed, check for updates)
   - Framework users reported improvements with updated PPD from Mario Limonciello's repos
   - **Important**: Use "balanced" mode, not "power-saver" with newer PPD
   - Current setting: "power-saver" - may need to test "balanced" instead!

7. **Expansion Cards** - Framework-specific issue:
   - Non-USB-C cards (USB-A, HDMI, DP) can draw ~1W each even idle
   - Module placement can matter (USB-A in port 2, DP in port 4 recommended)
   - Consider using only USB-C cards for battery use
   - Check current configuration: What cards are installed and where?

**URGENT: Verify Kernel 6.15 Regression Fix:**

**Current System Status (2025-10-28):**
- Kernel version: `6.15.8-1-default`
- Current EPP: `power` ✅
- Current profile: `power-saver` ✅
- Battery status: Discharging

**The kernel 6.15 regression patch has been merged to mainline by Mario Limonciello, but it's unclear if 6.15.8 includes the fix.**

**Important:** After EVERY suspend/resume cycle, verify EPP hasn't reset:
```bash
# Check if EPP reset to "performance"
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference

# If it shows "performance" instead of "power", re-apply:
powerprofilesctl set power-saver
```

**Workaround: Create systemd resume hook to auto-fix EPP**
```bash
# Create resume hook script
sudo tee /usr/lib/systemd/system-sleep/fix-epp-after-resume << 'EOF'
#!/bin/bash
# Fix kernel 6.15 regression where EPP resets to "performance" after resume

if [ "$1" = "post" ]; then
    # Wait for system to stabilize
    sleep 2

    # Check if on battery
    on_battery=false
    for ps in /sys/class/power_supply/BAT*/status; do
        if [ -f "$ps" ] && grep -q "Discharging" "$ps"; then
            on_battery=true
            break
        fi
    done

    # If on battery, force power-saver profile
    if [ "$on_battery" = true ]; then
        /usr/bin/powerprofilesctl set power-saver
        logger "Fixed EPP after resume: set to power-saver"
    fi
fi
EOF

# Make executable
sudo chmod +x /usr/lib/systemd/system-sleep/fix-epp-after-resume

# Test after next suspend/resume
```

**Quick Win Actions (from Framework community #44439):**
1. ✅ **Test "balanced" instead of "power-saver" profile** (BUT see kernel 6.15 regression above!):
   ```bash
   powerprofilesctl set balanced
   # Wait 1 minute, then check power consumption
   ```
   Framework users reported better results with "balanced" + newer PPD
   **Note**: This may not help if kernel regression resets EPP after suspend

2. **Enable NVMe APST** (Autonomous Power State Transition):
   ```bash
   sudo nvme set-feature /dev/nvme0 --feature-id=2 --value=2
   ```
   Note: This is not persistent across reboots (needs udev rule or systemd service)

3. **Lower screen brightness** to 35-40 (out of 255):
   ```bash
   # Check current brightness
   cat /sys/class/backlight/*/brightness
   cat /sys/class/backlight/*/max_brightness
   # Adjust via desktop environment or:
   echo 40 | sudo tee /sys/class/backlight/*/brightness
   ```

4. **Verify hardware video acceleration**:
   ```bash
   # Check VA-API support
   vainfo
   # Test with mpv
   mpv --hwdec=auto test-video.mp4
   # Check Chrome flags: chrome://gpu
   ```

5. **Check expansion card configuration**:
   - What cards are currently installed?
   - Are any non-USB-C cards installed? (USB-A, HDMI, DP add ~1W each)

**Longer-term Actions:**
1. Let system truly idle (close browser tabs, disconnect Bluetooth mouse)
2. Re-measure with powertop after 5+ minutes of no activity
3. Compare to Framework community reports of 1.6-4.5W idle
4. Consider updating to latest BIOS if not already on 3.05+
5. Monitor for kernel/firmware updates that improve mt7925e driver

## Wakeup Analysis

**Current Status (2025-10-28):**
- **Active workload**: ~7600 wakeups/s with Chrome, Cursor, Telegram running
- **Target for idle**: 100-200 wakeups/s (per Linux community best practices)
- **Good idle**: 3-100 wakeups/s with full GNOME desktop
- **Problematic**: >1000 wakeups/s is considered "very bad"

**Research findings:**
- Without certain processes, notebooks can have 100-200 wakeups/s
- A full GNOME desktop can achieve 3 wakeups/s when properly tuned
- 200 wakeups/s is "not very good"
- 1000 wakeups/s from one process is "very bad"
- 9000+ wakeups/s indicates serious problems

**Top wakeup contributors (current active system):**
1. Cursor GPU process: 222.4 events/s
2. Cursor renderers: 117.5 events/s
3. Chrome processes: ~200+ events/s combined
4. pipewire: 36.4 events/s

**C-state residency:** ✅ **C3 at 94.2%** - Excellent, CPUs spending most time in deepest idle state

**Action items:**
1. Test true idle wakeups with all applications closed
2. Consider disabling Cursor GPU acceleration (`--disable-gpu` flag)
3. Investigate pipewire wakeups (36.4 events/s baseline)

## Open Questions

1. ~~Should we blacklist uvcvideo permanently?~~ → Deferred (minor contributor ~0.5W)
2. ~~Will intel_idle prevent AMD C-states?~~ → NO - C-states are correct for AMD
3. **Why was `balanced` profile using `performance` EPP?** → **LIKELY kernel 6.15 regression!** EPP resets to "performance" after suspend/resume
4. **Should we use "balanced" instead of "power-saver" profile?** → NO - tested, jumped to 19.5W vs 12.4W with power-saver
5. **Is kernel 6.15 EPP regression patched in 6.15.8?** → Patch merged to mainline, needs verification after suspend/resume
6. **Should we try `amd_pstate=guided` kernel parameter?** → Potential optimization to test
7. ~~What's causing the high Wi-Fi packet rate?~~ → Normal for active system with browser/apps
8. What expansion cards are installed? (non-USB-C can add ~1W each)
9. ~~Is NVMe APST configured?~~ → ✅ YES - already enabled, transitions to power state 4 after 100ms
10. Is hardware video acceleration enabled? → Mesa-libva installed, needs vainfo verification
11. ~~What is current screen brightness?~~ → Tested: dimming from 80% to 23% saved ~1.8W
12. ~~Bluetooth power consumption?~~ → Tested: ~0.5W when enabled with no devices connected
13. **What are true idle wakeups/power with all apps closed?** → Needs testing

## Useful Tools

### AMD P-state Diagnostics
The `amd-pstate` tool (installed in `~/.local/bin/amd-pstate`) provides comprehensive AMD P-state diagnostics:

```bash
# Run full diagnostics
~/.local/bin/amd-pstate triage

# Shows:
# - AMD P-state driver status (should be "active")
# - Prefcore status (should be "enabled")
# - Per-CPU information:
#   - Energy Performance Preference (EPP) for all 24 cores
#   - Frequency scaling min/max limits
#   - Boost status
#   - Prefcore rankings (higher = preferred for performance)
# - CPPC MSR values (low-level CPU performance control)
```

**Key observations from current system:**
- CPU cores 0-3: Higher prefcore scores (196-208) = performance cores
- CPU cores 4-11, 16-23: Lower scores (125) = efficiency cores
- With power-saver profile:
  - CPUs 0-11: Max freq capped at 0.6 GHz (very aggressive)
  - CPUs 12-23: Max freq at 2.0 GHz (nominal)
  - Boost disabled (0) on all cores

This tool is invaluable for verifying EPP settings after suspend/resume cycles.

## References

- Main research doc: `specs/Framework-13-power/Gemini-deep-research.md`
- Diagnostic commands: `specs/Framework-13-power/HOWTO.md`
- WirePlumber config: `~/.config/wireplumber/wireplumber.conf.d/99-disable-camera-monitor.conf`
- AMD P-state tool: `~/.local/bin/amd-pstate`
- **AMD P-State Configuration Guide**: https://github.com/Hekel1989/amdpstate-configuration
  - Explains passive/active/guided modes
  - EPP hints and optimization strategies
  - Kernel parameter configuration
- Framework Community Threads:
  - Webcam power consumption: https://community.frame.work/t/high-2nd-gen-webcam-power-consumption/56363
  - **High idle power draw (with solutions)**: https://community.frame.work/t/responded-bad-battery-life-high-power-draw-at-idle-and-light-use-framework-13-ryzen/44439
  - **CRITICAL: Kernel 6.15 EPP regression after suspend/resume**: https://community.frame.work/t/increased-power-usage-after-resuming-from-suspend-on-ryzen-7040-kernel-6-15-regression/74531
  - PPD changes ineffective after suspend: https://community.frame.work/t/tracking-ppd-changes-have-no-effect-after-suspend-on-amd-until-reboot/40695
  - Power management guide: https://community.frame.work/t/guide-fw13-ryzen-power-management/42988

## Revision History

- 2025-10-28: Initial documentation after EPP performance mode discovery
  - Major fix: Changed EPP from `performance` to `power` → **4-6W saved**
  - Investigated: Webcam (minor), Bluetooth (working correctly), C-states (correct for AMD)
  - Corrected misconception: intel_idle is NOT the issue (AMD uses acpi_idle correctly)
  - **CRITICAL discovery**: Kernel 6.15 regression resets EPP to "performance" after suspend/resume
    - This likely explains why EPP was at "performance" initially
    - Patch merged to mainline by Mario Limonciello
    - Workaround: systemd resume hook provided
  - Added Framework community findings:
    - NVMe APST power saving
    - Hardware video acceleration critical for media
    - "balanced" mode may work better than "power-saver" with updated PPD
    - Screen brightness recommendations (35-40/255)
    - Fractional scaling power inefficiency in GNOME
    - Expansion card power considerations
