# Power Management Diagnostic Commands - Framework 13 AMD

This document provides a reference of useful commands for diagnosing and monitoring power consumption issues on Linux systems, particularly the Framework 13 with AMD Ryzen AI 9 HX 370.

## Battery Power Consumption

### Check Current Battery Discharge Rate
```bash
# Method 1: Calculate from current and voltage
current=$(cat /sys/class/power_supply/BAT1/current_now)
voltage=$(cat /sys/class/power_supply/BAT1/voltage_now)
awk -v c=$current -v v=$voltage 'BEGIN {printf "Battery discharge: %.2f W (%.2f mA at %.2f V)\n", c*v/1000000000000, c/1000, v/1000000}'

# Method 2: Using powertop (most reliable)
sudo powertop
# Look for "The battery reports a discharge rate of X.X W"
```

### Check Battery Status
```bash
# Is the system on battery or AC?
cat /sys/class/power_supply/BAT1/status
# Output: Charging, Discharging, or Full

# Check battery capacity
cat /sys/class/power_supply/BAT1/capacity  # Percentage
cat /sys/class/power_supply/BAT1/charge_now  # Current charge (µAh)
cat /sys/class/power_supply/BAT1/charge_full  # Full capacity (µAh)
```

## CPU Power Management

### Check CPU Frequency Scaling Driver
```bash
# Current driver (should be amd-pstate-epp for AMD Ryzen AI 300)
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver

# Current governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# Available governors
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors
```

### Check Energy Performance Preference (EPP)
```bash
# Current EPP setting (critical for battery life!)
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
# Should be 'power' on battery, not 'performance'

# Available EPP settings
cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_available_preferences

# Check EPP for all CPUs
find /sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference -exec sh -c 'echo "{}: $(cat {})"' \;

# Comprehensive AMD P-state diagnostics (best option if available)
~/.local/bin/amd-pstate triage
# Shows EPP, frequencies, boost state, prefcore, and CPPC MSR values for all cores
```

### Power Profiles
```bash
# Check available power profiles
powerprofilesctl list

# Check current profile
powerprofilesctl get

# Set profile
powerprofilesctl set power-saver  # For battery
powerprofilesctl set balanced
powerprofilesctl set performance   # For AC
```

### CPU Idle States (C-States)
```bash
# Check available C-states
cat /sys/devices/system/cpu/cpu0/cpuidle/state*/name

# Check C-state usage and residency time
for state in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
  echo "$(basename $state): $(cat $state/name) - Usage: $(cat $state/usage) - Time: $(cat $state/time)"
done

# Detailed idle state info
cpupower idle-info

# Check which CPUidle driver is active (should NOT be intel_idle on AMD)
cat /sys/devices/system/cpu/cpuidle/current_driver

# Check if intel_idle is loaded (bad on AMD systems!)
lsmod | grep intel_idle
cat /sys/module/intel_idle/parameters/max_cstate 2>/dev/null && echo "intel_idle loaded (BAD on AMD!)" || echo "intel_idle not loaded"
```

## USB Device Power Management

### Check USB Device Autosuspend Status
```bash
# Find all USB devices
lsusb

# Check specific device power management (example for device 3-5)
cat /sys/bus/usb/devices/3-5/product
cat /sys/bus/usb/devices/3-5/power/control        # Should be 'auto' for autosuspend
cat /sys/bus/usb/devices/3-5/power/runtime_status # active or suspended
cat /sys/bus/usb/devices/3-5/power/autosuspend_delay_ms

# Check all USB device power states
for dev in /sys/bus/usb/devices/*/product; do
  if [ -f "$dev" ]; then
    dir=$(dirname "$dev")
    echo "Device: $(cat $dev)"
    echo "Control: $(cat $dir/power/control)"
    echo "Status: $(cat $dir/power/runtime_status)"
    echo "---"
  fi
done
```

### Check What's Using USB Devices
```bash
# Check what processes are using video devices (webcam)
sudo fuser -v /dev/video*

# Using lsof (more detailed)
sudo lsof /dev/video*

# Check what's holding USB device files open
sudo lsof | grep /dev/bus/usb
```

## Kernel Modules and Drivers

### Check Loaded Modules
```bash
# Check if specific modules are loaded
lsmod | grep uvcvideo   # Webcam driver
lsmod | grep btusb      # Bluetooth USB driver
lsmod | grep mt7925e    # Wi-Fi driver
lsmod | grep intel_idle # Should NOT be loaded on AMD

# Module parameters
cat /sys/module/btusb/parameters/enable_autosuspend
cat /sys/module/intel_idle/parameters/max_cstate 2>/dev/null

# Module information
modinfo btusb | grep -E "parm.*autosuspend"
modinfo uvcvideo
```

### Unload/Load Modules
```bash
# Unload a module (temporarily, until reboot)
sudo modprobe -r uvcvideo
sudo modprobe -r intel_idle

# Load a module
sudo modprobe uvcvideo
sudo modprobe intel_idle

# Check if module can be unloaded (usage count must be 0)
lsmod | grep uvcvideo  # Check the number in 3rd column
```

## PipeWire / WirePlumber

### Check Camera Monitoring Status
```bash
# Check if WirePlumber is monitoring cameras
wpctl status

# Look at Video section - should be empty if monitoring disabled
# Example output:
# Video
#  ├─ Devices:
#  ├─ Sinks:
#  ├─ Sources:

# Check WirePlumber service status
systemctl --user status wireplumber.service
systemctl --user status pipewire.service

# Check WirePlumber configuration
ls -la ~/.config/wireplumber/wireplumber.conf.d/
cat ~/.config/wireplumber/wireplumber.conf.d/99-disable-camera-monitor.conf
```

## Bluetooth

### Check Bluetooth Status
```bash
# Bluetooth controller info
bluetoothctl show

# List paired devices
bluetoothctl devices

# Check connected devices
hcitool con

# Check specific device connection
bluetoothctl info 80:99:E7:F0:B1:D3 | grep Connected

# Bluetooth power state
rfkill list bluetooth
```

## Interrupts and Wakeups

### Check Interrupt Sources
```bash
# View all interrupts
cat /proc/interrupts

# Find top interrupt sources (sorted by count)
cat /proc/interrupts | awk 'NR==1 || NR>1 {sum=0; for(i=2;i<=NF;i++) if($i ~ /^[0-9]+$/) sum+=$i; if(sum>0) print sum, $0}' | sort -rn | head -20

# Check specific device interrupts
cat /proc/interrupts | grep -i "mt7925e\|btusb\|xhci\|amdgpu"
```

### Powertop Analysis
```bash
# Interactive mode (most common)
sudo powertop

# Run for specific duration and save to CSV
sudo powertop --csv=/tmp/powertop-output.csv --time=30

# Auto-tune all power settings (BE CAREFUL - may affect functionality)
sudo powertop --auto-tune

# Check what powertop would suggest
sudo powertop --html=/tmp/powertop.html
```

## PCIe Power Management

### Check PCIe ASPM (Active State Power Management)
```bash
# Check current ASPM policy
cat /sys/module/pcie_aspm/parameters/policy
# Output: [default] performance powersave powersupersave
# Bracket shows active policy

# Check per-device ASPM capabilities
lspci -vv | grep -A 10 "LnkCap\|ASPM"

# List all PCIe devices
lspci
```

## Network Interface Power Management

### Check Wi-Fi Power Management
```bash
# Check Wi-Fi interface power management
iwconfig wlp192s0

# Check if power management is enabled (should show "off" for lowest latency)
iwconfig wlp192s0 | grep "Power Management"

# Network interface statistics
ip -s link show wlp192s0

# Monitor network traffic
sudo tcpdump -i wlp192s0 -c 100

# Check for broadcast/multicast storms
netstat -s | grep -i multicast
```

## System-Wide Diagnostics

### Check Kernel Boot Parameters
```bash
# Current boot parameters
cat /proc/cmdline

# Should include things like:
# - amd_pstate=active (for AMD CPU power management)
# - intel_idle.max_cstate=0 (to disable intel_idle on AMD)
# - pcie_aspm=force (for PCIe power saving)
```

### Check System Logs
```bash
# Check for power-related kernel messages
dmesg | grep -i "idle\|c-state\|power\|suspend"

# Check recent power-profiles-daemon logs
journalctl -u power-profiles-daemon --since "10 minutes ago"

# Check WirePlumber logs
journalctl --user -u wireplumber.service --since "10 minutes ago"
```

### System Information
```bash
# CPU model
cat /proc/cpuinfo | grep "model name" | head -1

# Kernel version
uname -r

# Distribution
cat /etc/os-release
```

## Quick Power Audit Checklist

Run these commands to get a comprehensive power status snapshot:

```bash
#!/bin/bash
echo "=== Battery Status ==="
cat /sys/class/power_supply/BAT1/status
current=$(cat /sys/class/power_supply/BAT1/current_now)
voltage=$(cat /sys/class/power_supply/BAT1/voltage_now)
awk -v c=$current -v v=$voltage 'BEGIN {printf "Discharge: %.2f W\n", c*v/1000000000000}'

echo -e "\n=== CPU Power Management ==="
echo "Scaling driver: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver)"
echo "EPP setting: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference)"
echo "Power profile: $(powerprofilesctl get)"
echo "CPUidle driver: $(cat /sys/devices/system/cpu/cpuidle/current_driver)"

echo -e "\n=== Problematic Modules ==="
lsmod | grep intel_idle && echo "WARNING: intel_idle loaded on AMD system!" || echo "intel_idle: Not loaded (good)"
lsmod | grep uvcvideo && echo "uvcvideo loaded (webcam active)" || echo "uvcvideo: Not loaded"

echo -e "\n=== USB Device Status ==="
echo "Webcam: $(cat /sys/bus/usb/devices/3-3/power/runtime_status 2>/dev/null || echo 'N/A')"
echo "MediaTek wireless: $(cat /sys/bus/usb/devices/3-5/power/runtime_status 2>/dev/null || echo 'N/A')"

echo -e "\n=== C-State Usage (CPU0) ==="
for state in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
  echo "$(cat $state/name): $(cat $state/time) µs"
done | column -t

echo -e "\n=== Run 'sudo powertop' for detailed analysis ==="
```

Save this as a script and run for quick diagnostics!

## Common Issues and Checks

### Issue: High Idle Power (>10W)
```bash
# Check these in order:
1. powerprofilesctl get  # Should be 'power-saver' on battery
2. cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference  # Should be 'power'
3. lsmod | grep intel_idle  # Should be empty on AMD
4. cat /sys/devices/system/cpu/cpuidle/current_driver  # Should be acpi_idle or amd-specific
5. sudo powertop  # Check top power consumers
```

### Issue: Device Won't Autosuspend
```bash
# For USB device 3-5 (example):
cat /sys/bus/usb/devices/3-5/power/control  # Should be 'auto'
cat /sys/bus/usb/devices/3-5/power/runtime_status  # Check if 'suspended'
cat /sys/bus/usb/devices/3-5/power/runtime_active_time  # Increasing means something is using it
cat /sys/bus/usb/devices/3-5/power/runtime_suspended_time  # Should be increasing when idle

# Check what's preventing suspend
sudo lsof | grep "3-5"
cat /sys/bus/usb/devices/3-5/power/runtime_usage  # Should be 0 when nothing using it
```

### Issue: Webcam Preventing Sleep
```bash
# Check if WirePlumber is monitoring
wpctl status | grep -A 5 "Video"

# Check if anything has webcam open
sudo fuser /dev/video*

# Check driver status
lsmod | grep uvcvideo
```

## References

- Linux Kernel CPUidle Documentation: https://www.kernel.org/doc/html/latest/admin-guide/pm/cpuidle.html
- AMD P-State Driver: https://docs.kernel.org/admin-guide/pm/amd-pstate.html
- PowerTOP: https://wiki.archlinux.org/title/Powertop
- Framework Community: https://community.frame.work/
