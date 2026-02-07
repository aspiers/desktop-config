# Python Conversion Plan: set-layout-dpi and setup-panels

## Overview
Convert two shell scripts to Python for better maintainability, error handling, and integration with existing Python libraries (libdpy.py).

## Script 4: set-layout-dpi (136 lines)

### Current Functionality
- Extract and set DPI from layout configuration files
- Multiple fallback strategies for DPI detection:
  1. Direct DPI setting from layout file
  2. Model-based overrides (LG HDR 4K, AOC U28G2G6B → 128 DPI, BenQ BL3200 → 84 DPI)
  3. Auto-detected DPI from monitor physical size
  4. Calculated DPI from resolution (96 * width / 2560)
  5. Fallback to 128 DPI for laptops, or no change for desktops
- Integrates with libdpy.py for monitor detection

### Current Dependencies
- libdpy.py (Python library)
- jq (for JSON parsing)
- set-xfce4-dpi (wrapper for xfconf-query)
- get-layout (Python script)

### Python Conversion Approach

#### File Structure
```
bin/set-layout-dpi.py  (new)
lib/libdpi.py          (new - DPI detection utilities)
```

#### Key Functions to Implement

**lib/libdpi.py:**
```python
def set_xfce_dpi(dpi_value):
    """Set DPI via xfconf-query"""
    pass

def get_dpi_from_layout_file(layout_file):
    """Extract dpi: value from YAML layout file"""
    pass

def get_dpi_from_model_override(model):
    """Return model-specific DPI overrides"""
    pass

def calculate_dpi_from_resolution(width_px):
    """Calculate DPI: 96 * (width / 2560)"""
    pass

def get_primary_monitor_properties():
    """Get monitor properties from libdpy"""
    pass

def auto_detect_dpi():
    """Auto-detect DPI using multiple methods in priority order"""
    pass

def is_laptop_host():
    """Check if current host is a laptop"""
    # Reads ~/.localhost-nickname
    # Checks against known laptop hostnames
    pass
```

**bin/set-layout-dpi.py:**
```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('layout', nargs='?', help='Layout name')
    args = parser.parse_args()

    if args.layout:
        layout_file = get_layout_file(args.layout)
    else:
        layout_file = get_current_layout_file()

    if not layout_file:
        sys.exit(1)

    # Try to set DPI from layout file or auto-detect
    if not set_dpi_from_layout_file(layout_file):
        auto_detect_dpi()
```

#### Integration Points
- Reuse libdpy.py functions:
  - `get_monitor_properties()`
  - `get_inxi_primary_monitor()`
- Use PyYAML for layout file parsing
- Use subprocess for xfconf-query

### Testing Strategy
1. Test with various monitor configurations
2. Test layout file parsing
3. Test fallback chain
4. Test host detection

## Script 5: setup-panels (107 lines)

### Current Functionality
- Configure XFCE4 panels based on monitor setup
- Set panel 1 to primary monitor (or "Primary" magic value)
- Configure panels 2 and 3 for secondary monitors
- Calculate panel sizes based on DPI/scale factor
- Host-specific overrides for BenQ monitor

### Current Dependencies
- libdpy.py (Python library)
- xfconf-query (XFCE configuration tool)
- host-has-prop (shell function - needs implementation)
- BenQ-connected (shell script wrapper)

### Python Conversion Approach

#### File Structure
```
bin/setup-panels.py  (new)
lib/libxfce.py       (new - XFCE panel management utilities)
```

#### Key Functions to Implement

**lib/libxfce.py:**
```python
def set_panel_property(panel_num, prop_name, value, value_type='string'):
    """Set XFCE panel property via xfconf-query"""
    pass

def set_panel_output(panel_num, output_name):
    """Set panel to specific monitor output"""
    pass

def set_panel_size(panel_num, size_px):
    """Set panel size in pixels"""
    pass

def get_xrandr_primary_output():
    """Get primary monitor output name from libdpy"""
    pass

def get_secondary_outputs():
    """Get list of non-primary monitor outputs"""
    pass

def calculate_ui_scale_factor():
    """Calculate UI scale factor for panel sizing"""
    # From libdpy.calculate_ui_scale_factor()
    pass

def is_laptop_host():
    """Check if current host is a laptop"""
    pass

def is_benq_connected():
    """Check if BenQ BL3200 monitor is connected"""
    # From libdpy.large_monitor_connected() or connected-monitor-props
    pass

def setup_panels():
    """Main panel setup logic"""
    pass
```

**bin/setup-panels.py:**
```python
def main():
    # Get primary output
    primary = get_xrandr_primary_output()

    # Setup panel 1 on primary
    set_panel_output(1, "Primary")

    # Setup panels 2 and 3 on secondaries
    secondaries = get_secondary_outputs()
    for i, output in enumerate(secondaries[:2]):
        if output:
            set_panel_output(i + 2, output)

    # Set panel sizes
    if is_laptop_host():
        if is_benq_connected():
            # Hard-coded sizes for BenQ setup
            set_panel_size(1, 30)
            set_panel_size(2, 45)
            set_panel_size(3, 28)
        else:
            # Calculate sizes based on DPI
            scale = calculate_ui_scale_factor()
            panel_size = clamp(24 * scale, 24, 40)
            set_panel_size(1, panel_size)
            if secondaries:
                set_panel_size(2, 36)
```

#### Integration Points
- Reuse libdpy.py functions:
  - `get_xrandr_screen_geometries()`
  - `calculate_ui_scale_factor()`
  - `connected-monitor-props` (Python version)
- Use subprocess for xfconf-query

### Testing Strategy
1. Test panel configuration on single monitor
2. Test on multi-monitor setups
3. Test panel size calculations
4. Test host-specific overrides

## Shared Components

### Missing Function Implementation

**host-has-prop**: Not currently found in shell-env. Options:
1. Implement as simple hostname pattern matching against known laptop hostnames
2. Create a ~/.hostprops file for property storage
3. Implement as wrapper around existing checks (e.g., laptop monitor detection via libdpy)

**Recommendation**: Implement as hostname pattern matching:
```python
LAPTOP_HOSTNAMES = ['celtic', 'arabian', 'aegean', 'ionian', 'atlantic', 'indian', 'adriatic']

def host_has_prop(prop):
    if prop == 'laptop':
        return get_localhost_nickname() in LAPTOP_HOSTNAMES
    return False
```

### File Organization

**New files to create:**
1. `lib/libdpi.py` - DPI detection and XFCE DPI setting
2. `lib/libxfce.py` - XFCE panel management
3. `bin/set-layout-dpi.py` - Python replacement for set-layout-dpi
4. `bin/setup-panels.py` - Python replacement for setup-panels

**Files to modify:**
1. `bin/setup-monitor` - Update to use Python versions
2. Any scripts that source these scripts

### Dependencies

**Required Python packages:**
- PyYAML (already used in liblayout.py)
- No additional packages needed (uses standard library subprocess, json)

**External commands:**
- xfconf-query (XFCE configuration tool)
- jq (can be replaced with Python json module)
- xrandr (via libdpy.py)
- inxi (via libdpy.py)

## Implementation Order

1. Create `lib/libdpi.py` with DPI detection utilities
2. Create `bin/set-layout-dpi.py` using libdpi
3. Test set-layout-dpi.py extensively
4. Create `lib/libxfce.py` with panel management utilities
5. Create `bin/setup-panels.py` using libxfce
6. Test setup-panels.py extensively
7. Update bin/setup-monitor to use new Python versions
8. Update documentation

## Benefits of Python Conversion

1. **Better error handling**: Try/except instead of shell error codes
2. **No dependency on jq**: Use Python's json module
3. **Easier testing**: Python unittest vs shell script testing
4. **Type hints**: Add type annotations for better code clarity
5. **Code reuse**: Share functions between scripts more easily
6. **Better logging**: Use Python logging module
7. **No shell quoting issues**: No need to worry about escaping
8. **Integration**: Already integrates with libdpy.py
