#!/usr/bin/python3
#
# Screen numbers are numbered counting from 0 and going
# left to right by X coordinate.
#
# Note that this code has NO awareness of layouts, including
# assignments, such as which screen will actually be used as the
# primary screen.  That needs to be handled in liblayout.

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

# Global constants
GLOBAL_CACHE_DIR = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
CACHE_DIR = os.path.join(GLOBAL_CACHE_DIR, "libdpy")
DEBUG = False

os.makedirs(CACHE_DIR, exist_ok=True)


def debug(msg):
    if DEBUG:
        print("[DEBUG] %s" % msg, file=sys.stderr)


class DisplayDataCache:
    CACHE_FILES = {}
    cache_file = None  # Should be set in subclass

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if getattr(cls, "cache_file", None) is not None:
            DisplayDataCache.CACHE_FILES[cls.__name__] = cls.cache_file

    @classmethod
    def clear_cache(cls):
        for cache_file in cls.CACHE_FILES.values():
            if os.path.exists(cache_file):
                os.remove(cache_file)
                # print(f"Removed {cache_file}", file=sys.stderr)

            # Remove corresponding .md5 file
            md5_file = cache_file + ".md5"
            if os.path.exists(md5_file):
                os.remove(md5_file)
                # print(f"Removed {md5_file}", file=sys.stderr)

    def __init__(self):
        if self.cache_file is None:
            raise ValueError(f"cache_file must be set in {self.__class__.__name__}")

    @property
    def hash_cache_file(self):
        return self.cache_file + ".md5"

    def builder(self, use_cache=True):
        """
        Override in subclass to provide data to cache (as bytes or str).

        The use_cache option is for when caches are layered on caches;
        then there's an option of a request to bypass the top-level
        cache also causing a bypass of any caches it depends on.
        """
        raise NotImplementedError

    def cache_reader(self):
        """
        Override in subclass to provide logic for reading cached data.
        """
        with open(self.cache_file, "rb") as f:
            data = f.read()
            return data.decode()

    def get(self, use_cache=True):
        # print(f"{self.__class__}.get(use_cache={use_cache})")
        if not use_cache or not os.path.exists(self.cache_file):
            data_to_cache = self.builder(use_cache)
            if isinstance(data_to_cache, str):
                data_to_cache = data_to_cache.encode("utf-8")
            with open(self.cache_file, "wb") as f:
                f.write(data_to_cache)
            md5_hash = hashlib.md5(data_to_cache).hexdigest()
            with open(self.hash_cache_file, "w") as f:
                f.write(md5_hash)

        return self.cache_reader()


class XrandrCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xrandr.out")

    def builder(self, _use_cache):
        return subprocess.check_output("xrandr")


class XrandrJsonCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xrandr.json")

    def builder(self, use_cache=True):
        xrandr = XrandrCache().get(use_cache)
        iterator = re.finditer(
            r"^(?P<name>\S+) connected ((?P<primary>primary) )?"
            r"(?P<width>\d+)x(?P<height>\d+)\+(?P<x_offset>\d+)\+(?P<y_offset>\d+) "
            r"\(.+\) (?P<x_mm>\d+)mm x (?P<y_mm>\d+)mm",
            xrandr,
            re.MULTILINE,
        )
        screens = [m.groupdict() for m in iterator]
        if not screens:
            sys.stderr.write("Couldn't extract screens info from xrandr\n")
            sys.stderr.write(xrandr)
            sys.exit(1)

        screens.sort(key=lambda x: int(x["x_offset"]))
        for i, screen in enumerate(screens):
            for k, v in screen.items():
                if v:
                    if re.match(r"\d+", v):
                        screen[k] = int(v)
                else:
                    # primary can be None
                    screen[k] = ""

            screen["num"] = i
            screen["right"] = screen["x_offset"] + screen["width"]

        # Note: There's a difference between "primary" as listed by xrandr
        # and a primary assignment in liblayout.  If the wrong (or no)
        # screen is configured as primary at the xrandr level, then it
        # could be auto-detected by comparing these two.

        cache_data = {"timestamp": time.time(), "screens": screens}
        return json.dumps(cache_data, indent=2, sort_keys=True)

    def cache_reader(self):
        with open(self.cache_file, "r") as f:
            cache_data = json.load(f)
            return cache_data["screens"]


class XdpyinfoCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xdpyinfo.out")

    def builder(self, _use_cache):
        return subprocess.check_output("xdpyinfo")


class InxiJsonCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "inxi-Gxx.json")

    def builder(self, _use_cache):
        output = subprocess.check_output(
            "inxi -c 0 --tty -Gxx --output json --output-file print", shell=True
        )
        # Parse the JSON and re-serialize with sorted keys for stable hashing
        data = json.loads(output)
        return json.dumps(data, indent=2, sort_keys=True)

    def cache_reader(self):
        with open(self.cache_file, "r") as f:
            return json.load(f)


class InxiMonitorsCache(DisplayDataCache):
    # We discard the "note" and "pos" keys, and possibly others
    ALLOWED_KEYS = ("Monitor", "diag", "dpi", "mapped", "model", "res")

    cache_file = os.path.join(CACHE_DIR, "inxi-Gxx.monitors.json")

    def builder(self, use_cache=True):
        inxi_data = InxiJsonCache().get(use_cache)
        monitors = []
        graphics_list = None

        for key, value in inxi_data[0].items():
            if key.endswith("#Graphics") and isinstance(value, list):
                graphics_list = value
                break

        if not graphics_list:
            return json.dumps(monitors, indent=2, sort_keys=True)

        for item in graphics_list:
            if not isinstance(item, dict):
                continue
            is_monitor = False
            cleaned_item = {}
            for key, value in item.items():
                if key.endswith("#Monitor"):
                    is_monitor = True
                cleaned_key = re.sub(r"^\d+#\d+#\d+#", "", key)
                if cleaned_key not in self.ALLOWED_KEYS:
                    continue
                cleaned_item[cleaned_key] = value
            if is_monitor:
                monitors.append(cleaned_item)

        return json.dumps(monitors, indent=2, sort_keys=True)

    def cache_reader(self):
        with open(self.cache_file, "r") as f:
            return json.load(f)


class HwinfoMonitorJsonCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "hwinfo-monitor.json")

    def builder(self, _use_cache):
        output = subprocess.check_output(["hwinfo", "--monitor"], text=True)
        monitors = []
        current_monitor = None

        for line in output.split("\n"):
            line = line.rstrip()

            # Start of new monitor block
            if re.match(r"^\d+: ", line):
                if current_monitor:
                    monitors.append(current_monitor)
                current_monitor = {}
                continue

            if current_monitor is None:
                continue

            # Parse key-value pairs with indentation
            match = re.match(r"^  (\w[\w\s]*?):\s*(.+)$", line)
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip()

                # Remove quotes from string values
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]

                # Handle special keys
                if key == "Model":
                    current_monitor["model"] = value
                elif key == "Vendor":
                    # Parse vendor format: BNQ or DEL "DELL"
                    vendor_match = re.match(r'^(\S+)(?:\s+"(.+)")?$', value)
                    if vendor_match:
                        current_monitor["vendor_code"] = vendor_match.group(1)
                        if vendor_match.group(2):
                            current_monitor["vendor"] = vendor_match.group(2)
                elif key == "Serial ID":
                    current_monitor["serial"] = value
                elif key == "Size":
                    # Parse "527x296 mm"
                    size_match = re.match(r"(\d+)x(\d+) mm", value)
                    if size_match:
                        current_monitor["size_mm"] = {
                            "width": int(size_match.group(1)),
                            "height": int(size_match.group(2)),
                        }
                elif key == "Year of Manufacture":
                    current_monitor["year"] = int(value)
                elif key == "Week of Manufacture":
                    current_monitor["week"] = int(value)
                elif key == "Resolution":
                    # Collect all resolutions
                    if "resolutions" not in current_monitor:
                        current_monitor["resolutions"] = []
                    current_monitor["resolutions"].append(value)
                elif key == "Hardware Class":
                    current_monitor["hardware_class"] = value
                elif key == "Unique ID":
                    current_monitor["unique_id"] = value

        # Add the last monitor
        if current_monitor:
            monitors.append(current_monitor)

        return json.dumps(monitors, indent=2, sort_keys=True)

    def cache_reader(self):
        with open(self.cache_file, "r") as f:
            return json.load(f)


# Wrappers for external use


def clear_cache():
    DisplayDataCache.clear_cache()


def xrandr_output(use_cache=True):
    return XrandrCache().get(use_cache=use_cache)


def xdpyinfo_output(use_cache=True):
    return XdpyinfoCache().get(use_cache=use_cache)


def inxi_json_output(use_cache=True):
    return InxiJsonCache().get(use_cache=use_cache)


def get_xrandr_screen_geometries(use_cache=True):
    return XrandrJsonCache().get(use_cache=use_cache)


def get_inxi_monitors(use_cache=True):
    return InxiMonitorsCache().get(use_cache=use_cache)


def get_hwinfo_monitors(use_cache=True):
    return HwinfoMonitorJsonCache().get(use_cache=use_cache)


def monitors_connected(use_cache=True):
    xrandr = xrandr_output(use_cache)
    return len(re.findall(r"\bconnected\b", xrandr))


def large_monitor_connected(use_cache=True):
    if monitors_connected(use_cache) < 2:
        return False

    monitors = get_inxi_monitors(use_cache)
    for monitor in monitors:
        if monitor.get("Monitor") == "eDP-1":
            continue
        if "res" not in monitor:
            continue

        res = monitor["res"]
        if "x" not in res:
            continue

        width = int(res.split("x")[0])
        if width > 3000:
            return monitor

    return False


def external_monitor_connected(use_cache=True):
    """
    Returns first external (non-laptop) monitor found, or False.
    Unlike large_monitor_connected(), this returns any external monitor
    regardless of size.
    Falls back to xrandr data if inxi doesn't detect the external monitor.
    """
    if monitors_connected(use_cache) < 2:
        return False

    # First try inxi data
    monitors = get_inxi_monitors(use_cache)
    for monitor in monitors:
        if monitor.get("Monitor") == "eDP-1":
            continue
        if "res" in monitor:
            # Ensure model key exists for callers
            if "model" not in monitor:
                monitor["model"] = monitor.get("Monitor", "Unknown")
            return monitor

    # Fallback to xrandr if inxi didn't find external monitor
    screens = get_xrandr_screen_geometries(use_cache)
    for screen in screens:
        # Skip laptop display (eDP)
        if screen.get("name", "").startswith("eDP"):
            continue
        # Return a minimal dict with resolution info
        if screen.get("width") and screen.get("height"):
            return {
                "Monitor": screen.get("name"),
                "res": f"{screen['width']}x{screen['height']}",
                "model": "Unknown",
                "mapped": screen.get("name"),
            }

    return False


def get_monitor_properties(monitor_info, use_cache=True):
    """
    Extract useful properties from monitor dict and calculate DPI.
    Returns dict with: width_px, height_px, width_mm, height_mm, dpi, model
    Falls back gracefully if physical dimensions not available.
    """
    if not monitor_info:
        return None

    props = {"model": monitor_info.get("model", "Unknown")}

    # Extract resolution in pixels
    res = monitor_info.get("res", "")
    if "x" in res:
        parts = res.split("x")
        props["width_px"] = int(parts[0])
        props["height_px"] = int(parts[1])
    else:
        props["width_px"] = 0
        props["height_px"] = 0

    # Try to get physical dimensions from xrandr
    # Need to match this monitor with xrandr data
    xrandr_screens = get_xrandr_screen_geometries(use_cache)
    monitor_name = monitor_info.get("mapped") or monitor_info.get("Monitor")

    props["width_mm"] = 0
    props["height_mm"] = 0
    props["dpi"] = 0

    if monitor_name:
        for screen in xrandr_screens:
            if screen.get("name") == monitor_name:
                props["width_mm"] = screen.get("x_mm", 0)
                props["height_mm"] = screen.get("y_mm", 0)

                # Calculate DPI from physical dimensions if available
                if props["width_mm"] > 0 and props["width_px"] > 0:
                    # DPI = pixels / (mm / 25.4)
                    props["dpi"] = int(props["width_px"] / (props["width_mm"] / 25.4))
                break

    return props


def calculate_ui_scale_factor(monitor_info=None, reference_dpi=None, use_cache=True):
    """
    Calculate UI scaling factor based on monitor DPI relative to system DPI.
    Returns float: 1.0 if system DPI matches physical DPI, >1.0 if physical DPI > system.

    If monitor_info is None, uses the primary monitor.
    If monitor_info is a dict from inxi, extracts DPI from it.
    If reference_dpi is None, reads current system DPI from xfconf-query (XFCE4),
    or falls back to 96.
    """
    # Try to read system DPI from XFCE4 config if not specified
    if reference_dpi is None:
        try:
            result = (
                subprocess.check_output(
                    ["xfconf-query", "-c", "xsettings", "-p", "/Xft/DPI"],
                    stderr=subprocess.DEVNULL,
                )
                .decode("utf8")
                .strip()
            )
            if result:
                reference_dpi = int(result)
                debug("Using system DPI from xfconf: %d" % reference_dpi)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            reference_dpi = 96
            debug("Could not read system DPI, using fallback: %d" % reference_dpi)

    debug(
        "calculate_ui_scale_factor called: monitor_info=%s, reference_dpi=%d, use_cache=%s"
        % (monitor_info, reference_dpi, use_cache)
    )

    if monitor_info is None:
        # Get primary monitor
        debug("Getting primary monitor from inxi...")
        monitor_info = get_inxi_primary_monitor(use_cache)
        if not monitor_info:
            debug("No primary monitor found, returning 1.0")
            return 1.0
        debug("Primary monitor: %s" % monitor_info.get("model", "unknown"))

    props = get_monitor_properties(monitor_info, use_cache)
    if not props:
        debug("Could not get monitor properties, returning 1.0")
        return 1.0

    debug(
        "Monitor properties: dpi=%s, width_px=%s, height_px=%s, width_mm=%s, height_mm=%s"
        % (
            props.get("dpi"),
            props.get("width_px"),
            props.get("height_px"),
            props.get("width_mm"),
            props.get("height_mm"),
        )
    )

    # Use calculated DPI if available
    if props["dpi"] > 0:
        scale = props["dpi"] / reference_dpi
        debug(
            "Using DPI-based calculation: %s / %d = %s"
            % (props["dpi"], reference_dpi, scale)
        )
        return scale

    # Fallback: estimate from resolution
    # 3840 → ~1.5, 2560 → 1.0, 1920 → ~0.75
    if props["width_px"] > 0:
        scale = props["width_px"] / 2560.0
        debug(
            "Using resolution-based fallback: %s / 2560.0 = %s"
            % (props["width_px"], scale)
        )
        return scale

    debug("No DPI or width available, returning 1.0")
    return 1.0


def get_screen_geometries(use_cache=True):
    dpy = extract_xdpyinfo_geometry(use_cache)
    screens = get_xrandr_screen_geometries(use_cache)
    return (dpy, screens)


def extract_xdpyinfo_geometry(use_cache=True):
    dpy = xdpyinfo_output(use_cache)
    m = re.search(
        r"^\s*dimensions:\s+(?P<dpy_width>\d+)x(?P<dpy_height>\d+) pixels\s+"
        + r"\((?P<dpy_x_mm>\d+)x(?P<dpy_y_mm>\d+) millimeters\)",
        dpy,
        re.MULTILINE,
    )
    if not m:
        sys.stderr.write("Couldn't extract display size from xrandr\n")
        sys.exit(1)

    d = {}
    for k, v in m.groupdict().items():
        d[k] = int(v)

    # Note that the physical dimensions in mm here are not guaranteed
    # accurate and actually depend on the software dpi setting.
    d["dpy_x_dpi"] = round(d["dpy_width"] / d["dpy_x_mm"] * 25.4)
    d["dpy_y_dpi"] = round(d["dpy_height"] / d["dpy_y_mm"] * 25.4)

    return d


def show_xrandr_display_geometry(dpy):
    for k, v in dpy.items():
        print("%s=%d" % (k, v))


def show_xrandr_screen_geometries(screens):
    for i, screen in enumerate(screens):
        screen["x_dpi"] = round(int(screen["width"]) / int(screen["x_mm"]) * 25.4, 2)
        screen["y_dpi"] = round(int(screen["height"]) / int(screen["y_mm"]) * 25.4, 2)
        for k, v in screen.items():
            if k in ("primary", "assignment"):
                continue
            print("screen_%d_%s=%s" % (i, k, v))

    print("monitors_connected=%d" % len(screens))


def get_mouse_location_info():
    location_info = (
        subprocess.check_output(["xdotool", "getmouselocation", "--shell"])
        .decode("utf8")
        .split("\n")
    )

    info = {}
    for line in location_info:
        if not line:
            continue
        k, v = line.split("=")
        info[k] = int(v)

    return info


# FIXME: check y coord too
def get_screen(x, use_cache=True):
    screens = get_xrandr_screen_geometries(use_cache)
    for i, screen in enumerate(screens):
        if x >= screen["x_offset"] and x <= screen["right"]:
            return screen

    raise RuntimeError("Failed to find xrandr screen for x=%d" % x)


def get_current_screen_info(use_cache=True):
    info = get_mouse_location_info()
    return get_screen(info["X"], use_cache)


def find_monitor_by_attribute(attribute, value, use_cache=True):
    """
    Finds a monitor in the list whose specified attribute contains the given value (case-insensitive).

    Args:
        monitors: List of monitor dictionaries.
        attribute: The attribute key to search within (e.g., 'model', 'resolution').
        value: The substring to search for within the attribute's value.

    Returns:
        The matching monitor dictionary, or None if not found.
    """
    search_value = value.lower()
    for monitor in get_inxi_monitors(use_cache=use_cache):
        if (
            attribute in monitor
            and isinstance(monitor[attribute], str)
            and search_value in monitor[attribute].lower()
        ):
            return monitor
    return None


def get_xrandr_primary_monitor(use_cache=True):
    """
    Returns the primary monitor from xrandr (using cache if available), or None if not found.
    """
    screens = get_xrandr_screen_geometries(use_cache)
    for screen in screens:
        if screen.get("primary") == "primary":
            return screen
    return None


def get_inxi_primary_monitor(use_cache=True):
    """
    Returns the primary monitor from inxi data, or None if not found.
    Primary is indicated by 'primary' in the 'pos' field in inxi monitor data.
    Falls back to xrandr primary if inxi doesn't have it.
    """
    monitors = get_inxi_monitors(use_cache=use_cache)
    for monitor in monitors:
        pos = monitor.get("pos", "")
        if "primary" in pos:
            return monitor
    return None


def _print_json_or_die(data, error_msg):
    """Print data as JSON and exit 0, or print error to stderr and exit 1."""
    if data:
        print(json.dumps(data, indent=2))
        sys.exit(0)
    else:
        sys.stderr.write(error_msg + "\n")
        sys.exit(1)


def _cmd_find_primary(source, use_cache):
    """Handle --find-xrandr-primary and --find-inxi-primary."""
    if source == "xrandr":
        primary = get_xrandr_primary_monitor(use_cache=use_cache)
        label = "xrandr"
    else:
        primary = get_inxi_primary_monitor(use_cache=use_cache)
        label = "inxi data"

    _print_json_or_die(primary, f"No primary monitor found in {label}")


def _cmd_monitor_properties(use_cache):
    """Handle --get-monitor-properties."""
    primary = get_inxi_primary_monitor(use_cache=use_cache)
    if not primary:
        sys.stderr.write("No primary monitor found\n")
        sys.exit(1)

    props = get_monitor_properties(primary, use_cache=use_cache)
    _print_json_or_die(props, "Could not extract monitor properties")


def _cmd_calculate_ui_scale(scale_arg, use_cache):
    """Handle --calculate-ui-scale."""
    ref_dpi = scale_arg if scale_arg != -1 else None
    scale = calculate_ui_scale_factor(
        monitor_info=None,
        reference_dpi=ref_dpi,
        use_cache=use_cache,
    )
    print(f"{scale:.4f}")
    sys.exit(0)


def _cmd_find_by_attribute(args):
    """Handle --find-by-model and --find-by-res."""
    if args.find_by_model:
        search_attribute = "model"
        search_value = args.find_by_model
    else:
        search_attribute = "res"
        search_value = args.find_by_res

    found_monitor = find_monitor_by_attribute(search_attribute, search_value)
    _print_json_or_die(
        found_monitor,
        f"Error: No monitor found with {search_attribute} containing '{search_value}'",
    )


def _cmd_json_dump(getter, use_cache):
    """Handle --inxi-json and --hwinfo-json."""
    data = getter(use_cache=use_cache)
    if data:
        print(json.dumps(data, indent=2))
    sys.exit(0)


def _cmd_default(use_cache):
    """Default action: show xrandr display and screen geometries."""
    (dpy, screens) = get_screen_geometries(use_cache=use_cache)
    show_xrandr_display_geometry(dpy)
    show_xrandr_screen_geometries(screens)


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Display information about XRandR screen geometries"
    )
    parser.add_argument(
        "--use-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use cached XRandR/inxi data if available",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output",
    )
    parser.add_argument(
        "--inxi-json",
        action="store_true",
        help="Output inxi monitor JSON instead of normal output",
    )
    parser.add_argument(
        "--hwinfo-json",
        action="store_true",
        help="Output hwinfo monitor JSON instead of normal output",
    )
    parser.add_argument(
        "--find-by-model",
        metavar="MODEL",
        help="Search for a monitor model containing MODEL and output its JSON data",
    )
    parser.add_argument(
        "--find-by-res",
        metavar="RESOLUTION",
        help="Search for monitor resolution containing RESOLUTION and output JSON data",
    )
    parser.add_argument(
        "--find-xrandr-primary",
        action="store_true",
        help="Output the primary monitor according to xrandr (not inxi) as JSON",
    )
    parser.add_argument(
        "--find-inxi-primary",
        action="store_true",
        help="Output the primary monitor according to inxi as JSON",
    )
    parser.add_argument(
        "--get-monitor-properties",
        action="store_true",
        help="Get properties (resolution, DPI, etc.) of primary monitor as JSON",
    )
    parser.add_argument(
        "--calculate-ui-scale",
        metavar="REFERENCE_DPI",
        type=int,
        nargs="?",
        const=-1,
        default=None,
        help="Calculate UI scale factor for primary monitor (auto-detects reference DPI from XFCE4)",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    global DEBUG
    DEBUG = args.debug

    if args.find_xrandr_primary:
        _cmd_find_primary("xrandr", args.use_cache)
    elif args.find_inxi_primary:
        _cmd_find_primary("inxi", args.use_cache)
    elif args.get_monitor_properties:
        _cmd_monitor_properties(args.use_cache)
    elif args.calculate_ui_scale is not None:
        _cmd_calculate_ui_scale(args.calculate_ui_scale, args.use_cache)
    elif args.find_by_model or args.find_by_res:
        _cmd_find_by_attribute(args)
    elif args.inxi_json:
        _cmd_json_dump(get_inxi_monitors, args.use_cache)
    elif args.hwinfo_json:
        _cmd_json_dump(get_hwinfo_monitors, args.use_cache)
    else:
        _cmd_default(args.use_cache)


if __name__ == "__main__":
    main()
