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


def xrandr_status():
    return subprocess.check_output('xrandr').decode()


def extract_xrandr_geometries():
    xrandr = xrandr_status()
    dpy = extract_display_geometry(xrandr)
    screens = extract_xrandr_screen_geometries(xrandr)
    return (dpy, screens)


def extract_display_geometry(xrandr=None):
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

    return screens


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
def get_screen(x):
    screens = extract_xrandr_screen_geometries()
    for i, screen in enumerate(screens):
        if (x >= screen['x_offset'] and
            x <= screen['right']):
            return screen

    raise RuntimeError("Failed to find screen for x=%d" % x)


def get_current_screen_info():
    info = get_mouse_location_info()
    return get_screen(info['X'])


def main():
    (dpy, screens) = extract_xrandr_geometries()
    display_xrandr_display_geometry(dpy)
    display_xrandr_screen_geometries(screens)


if __name__ == "__main__":
    main()
