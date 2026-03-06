

# **An In-Depth Analysis and Optimization Guide for Idle Power Consumption on the Framework 13 with AMD Ryzen AI 9 HX 370**

## **Executive Summary & Initial Assessment**

The Framework 13, equipped with the AMD Ryzen AI 9 HX 370 System-on-Chip (SoC), represents a cutting-edge platform designed for high performance and competitive power efficiency.1 However, the observed idle power consumption of 25.5 W on a system running openSUSE Tumbleweed is highly anomalous. This figure is approximately four to six times higher than the expected and achievable baseline for this hardware, which, when properly configured under Linux, can idle in the 4-7 W range.2 This significant discrepancy indicates a severe misconfiguration or software-level issue that is preventing the system's sophisticated power management capabilities from functioning correctly.

An initial diagnosis based on the provided powertop utility output reveals several active hardware components, most notably the integrated webcam, Bluetooth, and Wi-Fi modules. While powertop correctly reports the total system power draw, its per-device attribution can be misleading. The high "Usage" percentages shown for these devices do not represent their direct power consumption. Instead, they signify a state of continuous activity that acts as a veto against the entire platform entering its deepest, most power-efficient idle states.

The investigation points to a confluence of three primary causal factors, which must be addressed systematically to restore nominal power consumption:

1. **A known software issue within the modern Linux media stack** is the most significant contributor. The PipeWire media server and its session manager, WirePlumber, are aggressively polling the webcam via the libcamera library, keeping its kernel driver perpetually active and preventing the system from idling down.3  
2. **Sub-optimal power management defaults for the wireless subsystems** represent a secondary but important factor. The drivers for the Bluetooth (btusb) and the MediaTek Wi-Fi 7 (mt7925e) modules may not be configured for aggressive power saving out-of-the-box, leading to unnecessary activity and wakeups.4  
3. **Potential misconfiguration of the core CPU frequency scaling driver** is a foundational concern. The AMD Ryzen AI 300 series ("Strix Point") architecture relies heavily on the amd-pstate kernel driver for efficient operation. If the system is using the legacy acpi-cpufreq driver or if amd-pstate is not in its optimal mode, both idle efficiency and peak performance will be compromised.6

This report will provide a comprehensive, step-by-step guide to diagnose and resolve each of these contributing factors. The instructions are tailored specifically for the openSUSE Tumbleweed distribution, with the objective of systematically eliminating the sources of excessive power draw and achieving an optimized idle state consistent with the hardware's capabilities.

## **Deconstructing the powertop Diagnosis: Signal vs. Noise**

The powertop utility is an indispensable tool for power management analysis, but its output requires careful interpretation to avoid misdiagnosis.8 The provided screenshot contains the critical clues needed to solve the power consumption issue, but it is essential to distinguish between direct measurements and diagnostic indicators.

### **Core Metric Validation**

The single most reliable piece of information in the powertop output is the line: "The battery reports a discharge rate of 25.5 W". This value is read directly from the laptop's battery management system (BMS), a dedicated microcontroller that monitors the flow of current into and out of the battery. This figure represents the ground truth for the entire system's power consumption at that moment and confirms the existence of a severe power drain issue.

### **Interpreting the "Usage" Column**

The primary source of confusion stems from the "Usage" column. This metric has different meanings for devices versus processes and does not directly correlate to wattage.

* **For Devices:** The percentage listed next to a device, such as the 75.2% for the webcam or 100.0% for the Bluetooth radio, does not mean that device is consuming that percentage of the total 25.5 W. Instead, it indicates the percentage of time the device was in an active, non-suspended state during powertop's measurement intervals.8 A device with 100% usage is one that is never entering its low-power suspend state, effectively telling the rest of the system that it is busy. The unit ms/s (milliseconds per second) or us/s (microseconds per second) seen for some devices indicates the duration of activity within each second.10  
* **For Processes:** For software processes, the ms/s or µs/s value indicates how much time the process was actively running on and utilizing the CPU within each second.10

### **Analysis of the Screenshot**

With this understanding, the powertop output can be re-interpreted not as a list of the largest power consumers, but as a prioritized list of "idleness preventers."

* **"USB device: Laptop Webcam Module (2nd Gen) (Framework)" at 75.2% usage:** This is the most significant finding. The webcam is not consuming \~19 W (  
  $$25.5\\,\\text{W} \\times 0.752$$  
  ). Rather, its near-constant active state is the primary catalyst preventing the entire system from entering a deep sleep state.  
* **"Radio device: btusb" at 100.0% usage:** This indicates the USB interface for the Bluetooth controller is failing to autosuspend. This is a classic Linux power management issue where a device remains fully powered even when idle.5  
* **"Network Interface: wlp1s0 (mt7925e)" at 63.3% usage:** The MediaTek Wi-Fi card is also exhibiting excessive activity, likely due to immature driver power-saving features not being engaged correctly.  
* **Summary Line:** The "Summary: 4946.4 wakeups/second" is a direct symptom of this underlying device activity. A wakeup occurs when the CPU is pulled out of an idle state to handle an event or interrupt. An optimized, idle system should see wakeups in the low hundreds or even double digits. A value approaching 5000 indicates a constant barrage of interrupts from misbehaving hardware or drivers, burning power by repeatedly waking the CPU for no productive reason.

The true source of the 25.5 W power draw is not the peripherals themselves, but the high-power state in which the entire SoC is being held. Modern processors achieve ultra-low idle power by entering deep "package C-states" (e.g., C9, C10), where large functional units of the chip, including CPU cores, the memory controller, and I/O blocks, are power-gated (turned off).5 However, the system can only enter these deep sleep states if all components agree that the system is truly idle. The constant activity from the webcam, Bluetooth, and Wi-Fi modules acts as a persistent veto. The SoC's power management unit observes this continuous bus traffic and interrupts, concludes that the system is active, and therefore remains in a light idle state (like C1 or C2), where cores are clocked down but the majority of the SoC remains powered on. The 25.5 W is the energy cost of this light idle state. This reframes the problem entirely: the goal is not to reduce the power of the webcam, but to allow the webcam to sleep so that the *entire system* can sleep.

## **The Primary Culprit: Resolving the Webcam's Idle Power State**

The most significant contributor to the excessive power consumption is the Framework Laptop Webcam Module (2nd Gen) being held in an active state. This is not a hardware fault but a well-documented software interaction between the modern Linux media stack and the webcam's kernel driver.

### **Identifying the Root Cause**

The issue originates from the behavior of PipeWire, the default audio and video server on modern Linux distributions, and its session manager, WirePlumber. In an effort to provide a seamless user experience, WirePlumber uses the libcamera library to proactively monitor for the presence of video devices.3 This allows applications to immediately detect when a camera becomes available. However, a bug or an overly aggressive default configuration in this monitoring mechanism causes it to keep the underlying uvcvideo kernel module for the webcam constantly open and active. The kernel interprets this as the device being "in use," which prevents the USB controller from placing the webcam into its low-power suspend state. This constant activity is directly responsible for the high "Usage" percentage in powertop and is the primary blocker preventing the CPU from entering deep idle states.

### **The Solution: Disabling libcamera Monitoring in WirePlumber**

The community-validated and effective solution is to create a custom configuration file for WirePlumber that explicitly disables the libcamera monitor. This action instructs WirePlumber to stop polling the webcam, which in turn allows the uvcvideo kernel module to release its hold on the device. Once the module is inactive, the USB subsystem can successfully autosuspend the webcam, eliminating it as a source of wakeups and allowing the platform to idle down properly. This change does not affect the webcam's functionality; it will still activate normally when an application like Cheese or a web browser requests access to it.

### **Step-by-Step Instructions for openSUSE Tumbleweed**

To implement this fix, a system-wide configuration override for WirePlumber should be created.

1. **Create the Configuration Directory:** WirePlumber's configuration can be extended by adding files to /etc/wireplumber/wireplumber.conf.d/. This directory may not exist by default. Open a terminal and create it with the following command:  
   Bash  
   sudo mkdir \-p /etc/wireplumber/wireplumber.conf.d/

2. **Create the Configuration File:** Using a text editor, create a new configuration file within this directory. The name should be prefixed with a number like 99- to ensure it is loaded after the default configurations.  
   Bash  
   sudo nano /etc/wireplumber/wireplumber.conf.d/99-disable-camera-monitor.conf

3. **Add the Configuration Snippet:** Add the following content to the newly created file. This syntax, derived from community solutions, instructs WirePlumber to disable the monitor.libcamera feature within its main profile.3  
   wireplumber.profiles \= {  
     main \= {  
       "monitor.libcamera" \= disabled  
     }  
   }

   Save the file and exit the editor (in nano, press Ctrl+X, then Y, then Enter).  
4. **Restart Media Services:** To apply the new configuration, the PipeWire and WirePlumber services for the current user must be restarted.  
   Bash  
   systemctl \--user restart wireplumber.service pipewire.service

5. **Verification:** After applying the change, run powertop again. The entry for "USB device: Laptop Webcam Module" should now show a "Usage" of 0.0% or a value very close to zero. This single change is expected to cause a dramatic and immediate reduction in the total system power draw.

This particular issue serves as a powerful case study in the complexities of modern hardware enablement on Linux. The transition to unified media frameworks like PipeWire and libcamera is a significant architectural improvement over the fragmented legacy systems. However, this incident demonstrates how a feature designed to improve user experience—proactive device monitoring—can have severe, unintended consequences for power management if not implemented with a holistic view of the system's power states. For users of rolling-release distributions like openSUSE Tumbleweed, access to the latest software necessary for new hardware also means being on the front lines of discovering and resolving such intricate integration challenges.

## **Investigating Secondary Contributors: Wireless Subsystem Tuning**

With the primary cause of power drain addressed, the next step is to optimize the wireless components—Bluetooth and Wi-Fi—which powertop also flagged as excessively active. These issues are typically due to driver defaults that prioritize performance or compatibility over power savings.

### **A. Bluetooth (btusb) Power Management**

The powertop output showing "Radio device: btusb" at 100.0% usage is a clear indication that USB autosuspend is disabled or not functioning for the Bluetooth controller. The btusb kernel module is responsible for managing the USB interface of the combined Wi-Fi/Bluetooth card. By default, it may not aggressively power down the device.

The solution is to create a kernel module configuration file that explicitly instructs the btusb driver to enable its autosuspend feature.

#### **Step-by-Step Instructions for openSUSE Tumbleweed**

1. **Create a modprobe.d Configuration File:** Kernel module options are configured via files in the /etc/modprobe.d/ directory.12 Create a new file for this purpose:  
   Bash  
   sudo nano /etc/modprobe.d/bluetooth-powersave.conf

2. **Add the Autosuspend Option:** Insert the following line into the file. This tells the kernel to load the btusb module with the enable\_autosuspend parameter set to 1 (on).14  
   options btusb enable\_autosuspend=1

   Save and close the file.  
3. **Rebuild the Initial Ramdisk (initrd):** On openSUSE, kernel modules that are required early in the boot process are loaded from an initrd. For changes in /etc/modprobe.d/ to affect these modules, the initrd must be rebuilt.15 This can be forced with the dracut command:  
   Bash  
   sudo dracut \--force

   This process will take a minute or two to complete.  
4. **Reboot and Verify:** After rebooting the system, run powertop and navigate to the "Tunables" tab using the Tab key. Find the entry corresponding to the Bluetooth device (it may be identified by its name or as an "Atheros" or similar device). The status for its autosuspend setting should now be "Good". On the "Overview" page, the "Usage" for "Radio device: btusb" should be at or near 0.0% when Bluetooth is idle.

### **B. Wi-Fi (MediaTek MT7925E) Power Management**

The "Network Interface: wlp1s0 (mt7925e)" showing 63.3% usage points to another power management inefficiency. The MediaTek MT7925E is a new Wi-Fi 7 card, and its Linux driver (mt7925e) is still undergoing active development and maturation.16 High idle usage is often a symptom of the driver not fully utilizing the hardware's power-saving capabilities, such as PCIe Active State Power Management (ASPM).

The solution involves a combination of ensuring the system is fully up-to-date to receive the latest driver and firmware fixes, and verifying that critical PCIe power-saving features are enabled.

#### **Step-by-Step Instructions**

1. **Ensure System is Fully Updated:** The most critical step for new hardware is to run the latest software. On a rolling release like openSUSE Tumbleweed, this is achieved by regularly running a full distribution upgrade. This will provide the latest kernel (containing the mt7925e driver) and the linux-firmware package (containing the necessary firmware blobs for the card).  
   Bash  
   sudo zypper dup

   Ongoing improvements to the mt7925e driver in newer kernels are known to fix performance and stability issues, which are often linked to power management.16  
2. **Verify PCIe ASPM Status:** ASPM allows PCIe links to enter low-power states (L0s, L1) when idle, which is critical for laptop battery life. Check the currently active ASPM policy:  
   Bash  
   cat /sys/module/pcie\_aspm/parameters/policy

   The output will show the available policies, with the active one enclosed in brackets, for example, \[default\] powersupersave powersave performance. For optimal battery life, the powersave or powersupersave policy is desired.  
3. **Force Enable ASPM (If Necessary):** In some cases, the system BIOS may conservatively disable ASPM. It can be forcibly enabled via a kernel boot parameter. This should be done using the YaST Boot Loader module, as detailed in the following section. The parameter to add is pcie\_aspm=force.4 This can resolve a variety of power and stability issues with PCIe devices.

The challenges with the wireless subsystems highlight a critical "dependency triad" in hardware support: the kernel driver, the device firmware, and the hardware itself. A bug or limitation in any one of these components can lead to sub-optimal behavior. The high "Usage" percentage in powertop is likely a symptom of an immature driver/firmware combination that is not aggressively transitioning the PCIe link into its low-power L1 state when idle. This underscores a key advantage of using a rolling-release distribution for new hardware: it provides the rapid cadence of updates to the kernel and firmware that are essential for stabilizing the platform and realizing its full efficiency potential.

## **Foundational Optimization: Configuring the AMD P-State Driver**

The final and most foundational layer of power management tuning involves ensuring the CPU itself is being managed by the correct driver in its most efficient mode. For the AMD Ryzen AI 9 HX 370 "Strix Point" processor, this is non-negotiable for achieving both optimal performance and low idle power.

### **Background: acpi-cpufreq vs. amd-pstate**

Historically, Linux has used the acpi-cpufreq driver to manage CPU frequencies on AMD platforms. This driver relies on performance states (P-states) exposed through the system's ACPI tables. However, this is a legacy mechanism that typically only offers a few coarse-grained frequency steps, which is inadequate for the dynamic and rapid frequency scaling required by modern CPUs.6

The modern solution is the amd-pstate driver. It interfaces directly with AMD's on-chip Collaborative Processor Performance Control (CPPC) firmware.6 This allows the kernel and the processor's own firmware to work together, enabling much finer-grained, more responsive, and ultimately more efficient control over frequency, voltage, and power.

### **Understanding amd-pstate Operating Modes**

The amd-pstate driver can operate in several modes, configured via a kernel boot parameter. For the newest hardware, the active mode is the most advanced and recommended.

* **passive:** In this mode, the kernel's generic scaling governors (like schedutil) dictate specific performance levels to the CPPC firmware. This is an older operational mode for amd-pstate.7  
* **guided:** Here, the kernel provides a minimum and maximum performance range, and the CPPC firmware autonomously selects the optimal frequency within that range based on its internal telemetry. This is a good modern option.7  
* **active (EPP):** This is the most sophisticated mode. The operating system provides a high-level "Energy Performance Preference" (EPP) hint—such as power, balance\_power, balance\_performance, or performance—to the CPPC firmware. The firmware then uses this hint, along with its own complex, real-time telemetry on workload, temperature, and power limits, to make all frequency and voltage decisions.7 This allows the hardware to manage itself, which is typically the most efficient approach.

The optimal configuration is to enable the active (EPP) mode at the kernel level. The standard power-profiles-daemon (PPD) service, which integrates with desktop environments like GNOME and KDE, can then be used to seamlessly switch the EPP hint based on whether the laptop is on battery (power) or AC power (balance\_performance or performance).

### **Step-by-Step Instructions for openSUSE Tumbleweed**

1. **Check the Current Driver:** First, determine which CPU frequency scaling driver is currently in use.  
   Bash  
   cat /sys/devices/system/cpu/cpu0/cpufreq/scaling\_driver

   If the output is acpi-cpufreq, then amd-pstate is not active, and the following steps are critical. If the output is amd-pstate or amd-pstate-epp, this process will ensure it is set to the optimal active mode.  
2. **Add Kernel Parameter using YaST:** The most robust way to modify kernel boot parameters on openSUSE is through the YaST configuration tool.22  
   * Launch YaST (it can be found in the application menu or by running sudo yast2 in a terminal).  
   * Navigate to System \> Boot Loader.  
   * Select the Kernel Parameters tab.  
   * In the text field labeled "Optional Kernel Command Line Parameter", add amd\_pstate=active. If other parameters are already present, ensure they are separated by spaces.  
   * Click OK. YaST will automatically apply the changes to the bootloader configuration.  
3. **Reboot and Verify:** After rebooting, verify that the new driver is active.  
   Bash  
   cat /sys/devices/system/cpu/cpu0/cpufreq/scaling\_driver

   The output should now be amd-pstate-epp, which is the name of the driver when operating in active mode.19  
4. **Verify EPP Control:** Confirm that the system exposes the EPP hints and that they can be controlled by the power profile daemon.  
   * Check available preferences: cat /sys/devices/system/cpu/cpu0/cpufreq/energy\_performance\_available\_preferences  
   * Check the current preference: cat /sys/devices/system/cpu/cpu0/cpufreq/energy\_performance\_preference  
   * Use the power settings in the GNOME or KDE desktop environment to switch between "Power Saver", "Balanced", and "Performance". Re-run the second command after each change to observe the EPP hint changing accordingly. This confirms that the entire stack, from the desktop UI down to the CPU firmware, is working correctly.

The following table provides a clear reference for the amd-pstate operating modes, justifying the recommendation of active mode for this hardware platform.

| Mode | Kernel Parameter | Control Mechanism | Key Characteristic | Recommended Use Case |
| :---- | :---- | :---- | :---- | :---- |
| **Active (EPP)** | amd\_pstate=active | Firmware-driven with OS hints | OS sets an Energy Performance Preference (e.g., power, balance\_power). Firmware makes all real-time decisions. Most responsive and efficient. | **Strongly Recommended for Ryzen 7040/AI 300 series and newer.** 7 |
| Guided | amd\_pstate=guided | Autonomous (Firmware) | OS sets min/max performance bounds; firmware autonomously selects a performance level within that range. | A modern alternative to Active, but offers less direct control via PPD. 7 |
| Passive | amd\_pstate=passive | Kernel-driven | The kernel's scaling governor (e.g., schedutil) requests specific performance levels (P-states). | Legacy/fallback mode for amd-pstate. Less efficient than Active or Guided. 7 |
| Disabled | amd\_pstate=disable or none | ACPI (Legacy) | Falls back to the acpi-cpufreq driver. Very coarse-grained control. | Not recommended; results in poor performance and high power consumption. 6 |

## **Synthesis and Strategic Action Plan**

The analysis confirms that the reported 25.5 W idle power consumption is not due to a single hardware fault but is a platform-level issue caused by a cascade of software misconfigurations. The root of the problem lies in peripherals being kept unnecessarily active, which in turn prevents the AMD Ryzen AI 9 HX 370 SoC from entering its deep, power-saving idle states. The primary catalyst is a known issue in the libcamera monitoring component of the WirePlumber media session manager, compounded by sub-optimal power-saving configurations for the Bluetooth and Wi-Fi modules and the potential use of a legacy CPU scaling driver.

By systematically addressing each of these issues, it is possible to restore the system's idle power consumption to the expected 4-7 W range.

### **Prioritized Checklist**

The following is a prioritized, step-by-step action plan to resolve the power consumption issues. The steps are ordered by their expected impact.

1. **\[Highest Impact\] Fix the Webcam Idle State:**  
   * Implement the WirePlumber configuration change to disable libcamera monitoring as detailed in Section III.  
   * **Expected Result:** An immediate and substantial drop in total power consumption, likely by several watts, as the primary idleness-prevention mechanism is removed.  
2. \*\* Enable amd-pstate Active Mode:\*\*  
   * Use the YaST Boot Loader module to add the amd\_pstate=active kernel parameter as detailed in Section V.  
   * **Expected Result:** Enables the most efficient and responsive CPU scaling mechanism, leading to lower power consumption during both idle and light-use scenarios.  
3. \*\* Optimize Wireless Modules:\*\*  
   * Create the /etc/modprobe.d/bluetooth-powersave.conf file to enforce btusb autosuspend, and rebuild the initrd as detailed in Section IV.  
   * Ensure the system is fully updated via sudo zypper dup to obtain the latest kernel driver and firmware for the MediaTek MT7925E Wi-Fi card.  
   * Verify PCIe ASPM is active, and if necessary, add the pcie\_aspm=force kernel parameter using YaST.  
   * **Expected Result:** Reduces wakeups from wireless devices, contributing to a more stable and lower-power idle state.  
4. **\[Verification\] Re-evaluate with powertop:**  
   * After implementing all changes and rebooting, run powertop for several minutes while the system is idle.  
   * **Expected Result:** The total discharge rate reported by the battery should now be in the 4-7 W range. In the device list, the "Usage" for the webcam and Bluetooth modules should be at or near 0.0%, and the Wi-Fi module's usage should be significantly reduced. The total wakeups/second should also be drastically lower.

### **Further Considerations**

* **Browser Hardware Acceleration:** While not a factor in idle power, ensuring hardware-accelerated video decoding is properly enabled in web browsers like Chrome and Firefox can significantly reduce CPU load and power consumption during media playback. This typically involves setting specific browser flags to enable VA-API support on Linux.26  
* **Expansion Cards:** It is a known characteristic of the Framework Laptop platform that certain Expansion Cards, particularly USB-A, HDMI, and DisplayPort, can draw a small but measurable amount of power (up to \~1 W) even when idle.28 For achieving the absolute minimum idle power draw, using only USB-C Expansion Cards is recommended.  
* **BIOS Reset:** In the unlikely event that power management issues persist after these software corrections, performing a full BIOS reset to factory defaults can sometimes resolve unusual hardware states that may have been triggered by previous configurations.29

#### **Works cited**

1. Reviews on the new Framework Laptop 13 are live\!, accessed on October 26, 2025, [https://frame.work/blog/reviews-on-the-new-framework-laptop-13-are-live](https://frame.work/blog/reviews-on-the-new-framework-laptop-13-are-live)  
2. FW13 AMD HX 370 power consumption test results \- no change? : r ..., accessed on October 26, 2025, [https://www.reddit.com/r/framework/comments/1kb4o2q/fw13\_amd\_hx\_370\_power\_consumption\_test\_results\_no/](https://www.reddit.com/r/framework/comments/1kb4o2q/fw13_amd_hx_370_power_consumption_test_results_no/)  
3. High 2nd gen webcam power consumption \- Linux \- Framework Community, accessed on October 26, 2025, [https://community.frame.work/t/high-2nd-gen-webcam-power-consumption/56363](https://community.frame.work/t/high-2nd-gen-webcam-power-consumption/56363)  
4. mt7925e-Linux-issues \- LENOVO COMMUNITY, accessed on October 26, 2025, [https://forums.lenovo.com/t5/ThinkPad-P-and-W-Series-Mobile-Workstations/mt7925e-Linux-issues/m-p/5396075](https://forums.lenovo.com/t5/ThinkPad-P-and-W-Series-Mobile-Workstations/mt7925e-Linux-issues/m-p/5396075)  
5. \[TRACKING\] Linux battery life tuning \- Page 2 \- Framework Community, accessed on October 26, 2025, [https://community.frame.work/t/tracking-linux-battery-life-tuning/6665?page=2](https://community.frame.work/t/tracking-linux-battery-life-tuning/6665?page=2)  
6. Documentation/admin-guide/pm/amd-pstate.rst \- The Linux Kernel Archives, accessed on October 26, 2025, [https://www.kernel.org/doc/Documentation/admin-guide/pm/amd-pstate.rst](https://www.kernel.org/doc/Documentation/admin-guide/pm/amd-pstate.rst)  
7. amd-pstate CPU Performance Scaling Driver \- The Linux Kernel documentation, accessed on October 26, 2025, [https://docs.kernel.org/admin-guide/pm/amd-pstate.html](https://docs.kernel.org/admin-guide/pm/amd-pstate.html)  
8. Powertop \- ArchWiki, accessed on October 26, 2025, [https://wiki.archlinux.org/title/Powertop](https://wiki.archlinux.org/title/Powertop)  
9. 2.2. PowerTOP, accessed on October 26, 2025, [https://jfearn.fedorapeople.org/fdocs/en-US/Fedora/20/html/Power\_Management\_Guide/sect-PowerTOP.html](https://jfearn.fedorapeople.org/fdocs/en-US/Fedora/20/html/Power_Management_Guide/sect-PowerTOP.html)  
10. Usage unit in Powertop \- command line \- Ask Ubuntu, accessed on October 26, 2025, [https://askubuntu.com/questions/416777/usage-unit-in-powertop](https://askubuntu.com/questions/416777/usage-unit-in-powertop)  
11. How to properly disable wireplumber suspend hook? : r/archlinux \- Reddit, accessed on October 26, 2025, [https://www.reddit.com/r/archlinux/comments/1ifc4wf/how\_to\_properly\_disable\_wireplumber\_suspend\_hook/](https://www.reddit.com/r/archlinux/comments/1ifc4wf/how_to_properly_disable_wireplumber_suspend_hook/)  
12. modprobe.d(5) — kmod, accessed on October 26, 2025, [https://manpages.opensuse.org/Tumbleweed/kmod/modprobe.d.5.en.html](https://manpages.opensuse.org/Tumbleweed/kmod/modprobe.d.5.en.html)  
13. Managing kernel modules | Administration Guide | SLES 15 SP7 \- SUSE Documentation, accessed on October 26, 2025, [https://documentation.suse.com/sles/15-SP7/html/SLES-all/cha-mod.html](https://documentation.suse.com/sles/15-SP7/html/SLES-all/cha-mod.html)  
14. How to disable Bluetooth power saving? \- Ask Ubuntu, accessed on October 26, 2025, [https://askubuntu.com/questions/1264725/how-to-disable-bluetooth-power-saving](https://askubuntu.com/questions/1264725/how-to-disable-bluetooth-power-saving)  
15. Why isn't a new file in /etc/modprobe.d used? \- Hardware \- openSUSE Forums, accessed on October 26, 2025, [https://forums.opensuse.org/t/why-isnt-a-new-file-in-etc-modprobe-d-used/97367](https://forums.opensuse.org/t/why-isnt-a-new-file-in-etc-modprobe-d-used/97367)  
16. WiFi driver status for MediaTek 7925? : r/linuxquestions \- Reddit, accessed on October 26, 2025, [https://www.reddit.com/r/linuxquestions/comments/1jt3yzf/wifi\_driver\_status\_for\_mediatek\_7925/](https://www.reddit.com/r/linuxquestions/comments/1jt3yzf/wifi_driver_status_for_mediatek_7925/)  
17. mediatek — Linux Wireless documentation, accessed on October 26, 2025, [https://wireless.docs.kernel.org/en/latest/en/users/drivers/mediatek.html](https://wireless.docs.kernel.org/en/latest/en/users/drivers/mediatek.html)  
18. MT7925 WiFi Performance Fixed with 6.14.3 : r/linux \- Reddit, accessed on October 26, 2025, [https://www.reddit.com/r/linux/comments/1k6plmq/mt7925\_wifi\_performance\_fixed\_with\_6143/](https://www.reddit.com/r/linux/comments/1k6plmq/mt7925_wifi_performance_fixed_with_6143/)  
19. AMD P-State and AMD P-State EPP Scaling Driver Configuration Guide \- Reddit, accessed on October 26, 2025, [https://www.reddit.com/r/linux/comments/15p4bfs/amd\_pstate\_and\_amd\_pstate\_epp\_scaling\_driver/](https://www.reddit.com/r/linux/comments/15p4bfs/amd_pstate_and_amd_pstate_epp_scaling_driver/)  
20. \[SOLVED\] How to actually use the AMD pstate driver / Kernel & Hardware / Arch Linux Forums, accessed on October 26, 2025, [https://bbs.archlinux.org/viewtopic.php?id=292940](https://bbs.archlinux.org/viewtopic.php?id=292940)  
21. Power efficiency problems with AMD CPUs on Linux 6.5.5 \- Sheogorath's Blog, accessed on October 26, 2025, [https://shivering-isles.com/2023/10/power-efficiency-problems-amd-cpus-linux-6-5-5](https://shivering-isles.com/2023/10/power-efficiency-problems-amd-cpus-linux-6-5-5)  
22. Modifying Kernel Boot Parameters \- SUSE Documentation, accessed on October 26, 2025, [https://documentation.suse.com/smart/systems-management/html/kernel-boot-parameters-modify/index.html](https://documentation.suse.com/smart/systems-management/html/kernel-boot-parameters-modify/index.html)  
23. Boot parameters | Start-Up | openSUSE Leap 15.6, accessed on October 26, 2025, [https://doc.opensuse.org/documentation/leap/startup/html/book-startup/cha-boot-parameters.html](https://doc.opensuse.org/documentation/leap/startup/html/book-startup/cha-boot-parameters.html)  
24. \[amd\_pstate\_epp\] AMD Power Management \- Drivers & Power Profiles \- openSUSE Forums, accessed on October 26, 2025, [https://forums.opensuse.org/t/amd-pstate-epp-amd-power-management-drivers-power-profiles/172365](https://forums.opensuse.org/t/amd-pstate-epp-amd-power-management-drivers-power-profiles/172365)  
25. How to use AMD P-State in Linux \- Page 16 \- Kernel, boot, graphics & hardware, accessed on October 26, 2025, [https://forum.endeavouros.com/t/how-to-use-amd-p-state-in-linux/25247?page=16](https://forum.endeavouros.com/t/how-to-use-amd-p-state-in-linux/25247?page=16)  
26. How GPU hardware acceleration works with Linux | PCWorld, accessed on October 26, 2025, [https://www.pcworld.com/article/2550326/how-gpu-hardware-acceleration-works-with-linux.html](https://www.pcworld.com/article/2550326/how-gpu-hardware-acceleration-works-with-linux.html)  
27. Make Chrome go brrrrrr.... \- DaTosh Blog, accessed on October 26, 2025, [https://datosh.github.io/post/hardware\_accelerate\_chrome/](https://datosh.github.io/post/hardware_accelerate_chrome/)  
28. \[SOLVED\] Really disappointed in the battery \- 1360p \- Linux \- Framework Community, accessed on October 26, 2025, [https://community.frame.work/t/solved-really-disappointed-in-the-battery-1360p/32295](https://community.frame.work/t/solved-really-disappointed-in-the-battery-1360p/32295)  
29. FW13 AMD Battery drained completely 2 times \- Linux \- Framework Community, accessed on October 26, 2025, [https://community.frame.work/t/fw13-amd-battery-drained-completely-2-times/60852](https://community.frame.work/t/fw13-amd-battery-drained-completely-2-times/60852)  
30. Excessive battery drain on Framework 13 with Ryzen 5 7040, accessed on October 26, 2025, [https://community.frame.work/t/excessive-battery-drain-on-framework-13-with-ryzen-5-7040/60690](https://community.frame.work/t/excessive-battery-drain-on-framework-13-with-ryzen-5-7040/60690)