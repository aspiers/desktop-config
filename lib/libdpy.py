#!/usr/bin/python3
#
# Screen numbers are numbered counting from 0 and going
# left to right by X coordinate.
#
# Note that this code has NO awareness of layouts, including
# assignments, such as which screen will actually be used as the
# primary screen.  That needs to be handled in liblayout.

import re
import subprocess
import sys
import os
import json
import time
import argparse

# Global constants
GLOBAL_CACHE_DIR = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser("~/.cache")
CACHE_DIR = os.path.join(GLOBAL_CACHE_DIR, "libdpy")

os.makedirs(CACHE_DIR, exist_ok=True)


class DisplayDataCache:
    CACHE_FILES = {}
    cache_file = None  # Should be set in subclass

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if getattr(cls, 'cache_file', None) is not None:
            DisplayDataCache.CACHE_FILES[cls.__name__] = cls.cache_file

    @classmethod
    def clear_cache(cls):
        for cache_file in cls.CACHE_FILES.values():
            if os.path.exists(cache_file):
                os.remove(cache_file)
                # print(f"Removed {cache_file}", file=sys.stderr)

    def __init__(self):
        if self.cache_file is None:
            raise ValueError(f"cache_file must be set in {self.__class__.__name__}")

    def builder(self):
        """
        Override in subclass to provide data to cache (as bytes or str).
        """
        raise NotImplementedError

    def cache_reader(self):
        """
        Override in subclass to provide logic for reading cached data.
        """
        with open(self.cache_file, 'rb') as f:
            data = f.read()
            return data.decode()

    def get(self, use_cache=True):
        if not use_cache or not os.path.exists(self.cache_file):
            data_to_cache = self.builder()
            if isinstance(data_to_cache, str):
                data_to_cache = data_to_cache.encode('utf-8')
            with open(self.cache_file, 'wb') as f:
                f.write(data_to_cache)
        return self.cache_reader()


class XrandrCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xrandr.out")

    def builder(self):
        return subprocess.check_output('xrandr')


class XrandrJsonCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xrandr.json")

    def builder(self):
        xrandr = XrandrCache().get()
        iterator = re.finditer(
            r'^(?P<name>\S+) connected ((?P<primary>primary) )?(?P<width>\d+)x(?P<height>\d+)\+(?P<x_offset>\d+)\+(?P<y_offset>\d+) \(.+\) (?P<x_mm>\d+)mm x (?P<y_mm>\d+)mm',
            xrandr,
            re.MULTILINE
        )
        screens = [m.groupdict() for m in iterator]
        if not screens:
            sys.stderr.write("Couldn't extract screens info from xrandr\n")
            sys.stderr.write(xrandr)
            sys.exit(1)

        screens.sort(key = lambda x: int(x['x_offset']))
        for i, screen in enumerate(screens):
            for k, v in screen.items():
                if v:
                    if re.match(r'\d+', v):
                        screen[k] = int(v)
                else:
                    # primary can be None
                    screen[k] = ''

            screen['num'] = i
            screen['right'] = screen['x_offset'] + screen['width']

        # Note: There's a difference between "primary" as listed by xrandr
        # and a primary assignment in liblayout.  If the wrong (or no)
        # screen is configured as primary at the xrandr level, then it
        # could be auto-detected by comparing these two.

        cache_data = {
            "timestamp": time.time(),
            "screens": screens
        }
        return json.dumps(cache_data, indent=2)

    def cache_reader(self):
        with open(self.cache_file, 'r') as f:
            cache_data = json.load(f)
            return cache_data["screens"]


class XdpyinfoCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "xdpyinfo.out")

    def builder(self):
        return subprocess.check_output('xdpyinfo')


class InxiJsonCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "inxi-Gxx.json")

    def builder(self):
        return subprocess.check_output(
            'inxi -c 0 -Gxx --output json --output-file print',
            shell=True
        )

    def cache_reader(self):
        with open(self.cache_file, 'r') as f:
            return json.load(f)


class InxiMonitorsCache(DisplayDataCache):
    cache_file = os.path.join(CACHE_DIR, "inxi-Gxx.monitors.json")

    def builder(self):
        inxi_data = InxiJsonCache().get()
        monitors = []
        graphics_list = None

        for key, value in inxi_data[0].items():
            if key.endswith("#Graphics") and isinstance(value, list):
                graphics_list = value
                break

        if not graphics_list:
            return json.dumps(monitors, indent=2)

        for item in graphics_list:
            if not isinstance(item, dict):
                continue
            is_monitor = False
            cleaned_item = {}
            for key, value in item.items():
                if key.endswith("#Monitor"):
                    is_monitor = True
                cleaned_key = re.sub(r'^\d+#\d+#\d+#', '', key)
                cleaned_item[cleaned_key] = value
            if is_monitor:
                monitors.append(cleaned_item)

        return json.dumps(monitors, indent=2)

    def cache_reader(self):
        with open(self.cache_file, 'r') as f:
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


def monitors_connected(use_cache=True):
    xrandr = xrandr_output(use_cache)
    return len(re.findall(r'\bconnected\b', xrandr))


def large_monitor_connected(use_cache=True):
    if monitors_connected(use_cache) < 2:
        return False

    inxi = inxi_json_output(use_cache)
    return '"res": "3840x2160"' in inxi


def get_screen_geometries(use_cache=True):
    screens = get_xrandr_screen_geometries(use_cache)
    dpy = extract_xdpyinfo_geometry(use_cache)
    return (dpy, screens)


def extract_xdpyinfo_geometry(use_cache=True):
    dpy = xdpyinfo_output(use_cache)
    m = re.search(
        r'^\s*dimensions:\s+(?P<dpy_width>\d+)x(?P<dpy_height>\d+) pixels\s+' +
        r'\((?P<dpy_x_mm>\d+)x(?P<dpy_y_mm>\d+) millimeters\)',
        dpy,
        re.MULTILINE
    )
    if not m:
        sys.stderr.write("Couldn't extract display size from xrandr\n")
        sys.exit(1)

    d = {}
    for k, v in m.groupdict().items():
        d[k] = int(v)

    # Note that the physical dimensions in mm here are not guaranteed
    # accurate and actually depend on the software dpi setting.
    d['dpy_x_dpi'] = round(d['dpy_width'] / d['dpy_x_mm'] * 25.4)
    d['dpy_y_dpi'] = round(d['dpy_height'] / d['dpy_y_mm'] * 25.4)

    return d


def show_xrandr_display_geometry(dpy):
    for k, v in dpy.items():
        print("%s=%d" % (k, v))


def show_xrandr_screen_geometries(screens):
    for i, screen in enumerate(screens):
        screen['x_dpi'] = round(int(screen['width']) / int(screen['x_mm']) * 25.4, 2)
        screen['y_dpi'] = round(int(screen['height']) / int(screen['y_mm']) * 25.4, 2)
        for k, v in screen.items():
            if k in ('primary', 'assignment'):
                continue
            print("screen_%d_%s=%s" % (i, k, v))

    print("monitors_connected=%d" % len(screens))


def get_mouse_location_info():
    location_info = subprocess.check_output(
        ['xdotool', 'getmouselocation', '--shell']
    ).decode('utf8').split('\n')

    info = {}
    for line in location_info:
        if not line:
            continue
        k, v = line.split('=')
        info[k] = int(v)

    return info


# FIXME: check y coord too
def get_screen(x, use_cache=True):
    screens = get_xrandr_screen_geometries(use_cache)
    for i, screen in enumerate(screens):
        if (x >= screen['x_offset'] and
            x <= screen['right']):
            return screen

    raise RuntimeError("Failed to find screen for x=%d" % x)


def get_current_screen_info(use_cache=True):
    info = get_mouse_location_info()
    return get_screen(info['X'], use_cache)


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
        if (attribute in monitor and
            isinstance(monitor[attribute], str) and
            search_value in monitor[attribute].lower()):
            return monitor
    return None


def get_xrandr_primary_monitor(use_cache=False):
    """
    Returns the primary monitor from xrandr (using cache if available), or None if not found.
    """
    screens = get_xrandr_screen_geometries(use_cache)
    for screen in screens:
        if screen.get('primary') == 'primary':
            return screen
    return None


def main():
    parser = argparse.ArgumentParser(
        description='Display information about XRandR screen geometries')
    parser.add_argument('--use-cache',
                        action=argparse.BooleanOptionalAction,
                        default=True,
                        help='Use cached XRandR/inxi data if available')
    parser.add_argument('--inxi-json',
                        action='store_true',
                        help='Output inxi monitor JSON instead of normal output')
    parser.add_argument('--find-by-model',
                        metavar='MODEL',
                        help='Search for a monitor model containing MODEL and output its JSON data')
    parser.add_argument('--find-by-res',
                        metavar='RESOLUTION',
                        help='Search for a monitor resolution containing RESOLUTION and output its JSON data')
    parser.add_argument('--find-xrandr-primary',
                        action='store_true',
                        help='Output the primary monitor according to xrandr (not inxi) as JSON')
    args = parser.parse_args()

    if args.find_xrandr_primary:
        primary = get_xrandr_primary_monitor(use_cache=args.use_cache)
        if primary:
            print(json.dumps(primary, indent=2))
            sys.exit(0)
        else:
            sys.stderr.write("No primary monitor found in xrandr\n")
            sys.exit(1)

    if args.find_by_model or args.find_by_res:
        found_monitor = None
        search_attribute = None
        search_value = None

        if args.find_by_model:
            search_attribute = 'model'
            search_value = args.find_by_model
        elif args.find_by_res:
            search_attribute = 'res'
            search_value = args.find_by_res

        if search_attribute and search_value:
            found_monitor = find_monitor_by_attribute(search_attribute, search_value)

        if found_monitor:
            print(json.dumps(found_monitor, indent=2))
            sys.exit(0)
        else:
            sys.stderr.write(f"Error: No monitor found with {search_attribute} containing '{search_value}'\n")
            sys.exit(1)

    if args.inxi_json:
        inxi_monitors = get_inxi_monitors(use_cache=args.use_cache)
        if inxi_monitors:
            print(json.dumps(inxi_monitors, indent=2))
        sys.exit(0)

    (dpy, screens) = get_screen_geometries(use_cache=args.use_cache)

    show_xrandr_display_geometry(dpy)
    show_xrandr_screen_geometries(screens)

    # Example usage of the new function
    # inxi_monitors = extract_inxi_monitors()
    # if inxi_monitors:
    #     print("\nInxi Monitor Information:")
    #     print(json.dumps(inxi_monitors, indent=2))


if __name__ == "__main__":
    main()
