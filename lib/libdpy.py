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
CACHE_DIR = os.path.expanduser("~/tmp")
XRANDR_CACHE_FILE = os.path.join(CACHE_DIR, ".xrandr.json")
INXI_RAW_CACHE_FILE = os.path.join(CACHE_DIR, ".inxi-Gxx.out")
INXI_JSON_CACHE_FILE = os.path.join(CACHE_DIR, ".inxi-Gxx.json")


def xrandr_status():
    # import traceback
    # traceback.print_stack()
    return subprocess.check_output('xrandr').decode()


def get_screen_geometries(use_cache=False):
    screens = get_xrandr_screen_geometries(use_cache)
    dpy = extract_xdpyinfo_geometry()
    return (dpy, screens)


def extract_xdpyinfo_geometry():
    dpy = subprocess.check_output('xdpyinfo').decode()
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


def display_xrandr_display_geometry(dpy):
    for k, v in dpy.items():
        print("%s=%d" % (k, v))


def extract_xrandr_screen_geometries(xrandr=None):
    if not xrandr:
        xrandr = xrandr_status()

    iterator = re.finditer(
        r'^(?P<name>\S+) connected ((?P<primary>primary) )?(?P<width>\d+)x(?P<height>\d+)\+(?P<x_offset>\d+)\+(?P<y_offset>\d+) \(.+\) (?P<x_mm>\d+)mm x (?P<y_mm>\d+)mm',
        xrandr,
        re.MULTILINE
    )
    screens = [m.groupdict() for m in iterator]
    if not screens:
        sys.stderr.write("Couldn't extract screens info from xrandr\n")
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

    # Cache the results
    os.makedirs(CACHE_DIR, exist_ok=True)

    try:
        cache_data = {
            "timestamp": time.time(),
            "screens": screens
        }
        with open(XRANDR_CACHE_FILE, 'w') as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to write XRandr cache: {str(e)}\n")

    return screens


def get_xrandr_screen_geometries(use_cache=False):
    """
    Get xrandr screen geometries, optionally using cached results if available.

    Args:
        use_cache: If True, use cached results if available

    Returns:
        List of screen geometries
    """
    if use_cache:
        try:
            if os.path.exists(XRANDR_CACHE_FILE):
                with open(XRANDR_CACHE_FILE, 'r') as f:
                    cache_data = json.load(f)
                    return cache_data["screens"]
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to read xrandr cache: {str(e)}\n")

    # No cache or cache loading failed, extract fresh data
    return extract_xrandr_screen_geometries()


def display_xrandr_screen_geometries(screens):
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
def get_screen(x, use_cache=False):
    screens = get_xrandr_screen_geometries(use_cache)
    for i, screen in enumerate(screens):
        if (x >= screen['x_offset'] and
            x <= screen['right']):
            return screen

    raise RuntimeError("Failed to find screen for x=%d" % x)


def get_current_screen_info(use_cache=False):
    info = get_mouse_location_info()
    return get_screen(info['X'], use_cache)


def extract_inxi_monitors():
    """
    Calls inxi -Gxx --output json --output-file print, parses the JSON from stdout,
    and extracts all sections for monitors.
    """
    command = "inxi -Gxx --output json --output-file print"
    inxi_output = subprocess.check_output(command, shell=True).decode('utf-8')

    inxi_data = json.loads(inxi_output)

    # Extract monitor sections
    monitors = []
    monitors = []
    if not (inxi_data and isinstance(inxi_data, list) and len(inxi_data) > 0):
        return monitors

    graphics_list = None
    for key, value in inxi_data[0].items():
        if key.endswith("#Graphics") and isinstance(value, list):
            graphics_list = value
            break

    if not graphics_list:
        return monitors

    for item in graphics_list:
        if not isinstance(item, dict):
            continue

        is_monitor = False
        cleaned_item = {}
        for key, value in item.items():
            if key.endswith("#Monitor"):
                is_monitor = True
            # Remove prefix (number#number#number#) from key
            cleaned_key = re.sub(r'^\d+#\d+#\d+#', '', key)
            cleaned_item[cleaned_key] = value

        if is_monitor:
            monitors.append(cleaned_item)

    return monitors


def get_inxi_monitors(use_cache=False):
    """
    Get inxi monitor data, optionally using cached results if available.

    Args:
        use_cache: If True, use cached results if available

    Returns:
        List of monitor dictionaries
    """
    if use_cache:
        if os.path.exists(INXI_JSON_CACHE_FILE):
            with open(INXI_JSON_CACHE_FILE, 'r') as f:
                cache_data = json.load(f)
                # Assuming the cache file directly stores the list of monitors
                return cache_data

    # No cache or cache loading failed, extract fresh data
    monitors = extract_inxi_monitors()

    # Cache the results
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(INXI_JSON_CACHE_FILE, 'w') as f:
        # Directly dump the list of monitors
        json.dump(monitors, f, indent=2)

    return monitors


def find_monitor_by_attribute(monitors, attribute, value):
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
    for monitor in monitors:
        if attribute in monitor and isinstance(monitor[attribute], str) and search_value in monitor[attribute].lower():
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
        inxi_monitors = get_inxi_monitors(use_cache=args.use_cache)
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
            found_monitor = find_monitor_by_attribute(inxi_monitors, search_attribute, search_value)

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

    print(f'XRANDR_CACHE={XRANDR_CACHE_FILE}')
    print(f'INXI_RAW_CACHE={INXI_RAW_CACHE_FILE}')
    print(f'INXI_JSON_CACHE={INXI_JSON_CACHE_FILE}')
    display_xrandr_display_geometry(dpy)
    display_xrandr_screen_geometries(screens)

    # Example usage of the new function
    # inxi_monitors = extract_inxi_monitors()
    # if inxi_monitors:
    #     print("\nInxi Monitor Information:")
    #     print(json.dumps(inxi_monitors, indent=2))


if __name__ == "__main__":
    main()
