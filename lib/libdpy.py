#!/usr/bin/python3

import re
import subprocess


def extract_xrandr_geometries():
    xrandr = subprocess.check_output('xrandr').decode('utf8')

    iterator = re.finditer(
        r'^(?P<name>\S+) connected ((?P<primary>primary) )?(?P<width>\d+)x(?P<height>\d+)\+(?P<x_offset>\d+)\+(?P<y_offset>\d+) \(.+\) (?P<x_mm>\d+)mm x (?P<y_mm>\d+)mm',
        xrandr,
        re.MULTILINE
    )
    screens = [m.groupdict() for m in iterator]

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

    if len(screens) == 1:
        screens[0]['label'] = 'primary'
    else:
        for i, screen in enumerate(screens):
            screen['label'] = screen.get('primary') or 'secondary'

    return screens


def display_xrandr_geometries(screens):
    for i, screen in enumerate(screens):
        screen['x_dpi'] = int(screen['width']) / int(screen['x_mm']) * 25.4
        screen['y_dpi'] = int(screen['height']) / int(screen['y_mm']) * 25.4
        for label in (str(i), screen['label']):
            for k, v in screen.items():
                if k in ('primary', 'label'):
                    continue
                print("screen_%s_%s=%s" % (label, k, v))

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


def get_screen(x):
    screens = extract_xrandr_geometries()
    for i, screen in enumerate(screens):
        if (x >= screen['x_offset'] and
            x <= screen['right']):
            return screen

    raise RuntimeError("Failed to find screen for x=%d" % x)


def get_current_screen():
    info = get_mouse_location_info()
    return get_screen(info['X'])


def main():
    screens = extract_xrandr_geometries()
    display_xrandr_geometries(screens)


if __name__ == "__main__":
    main()
