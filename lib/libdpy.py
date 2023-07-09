#!/usr/bin/env python3

import re
import subprocess


def extract_xrandr_geometries():
    xrandr = subprocess.check_output('xrandr').decode('utf8')

    iterator = re.finditer(
        r'^(?P<name>\S+) connected (?P<primary>primary )?(?P<width>\d+)x(?P<height>\d+)\+(?P<xoffset>\d+)\+(?P<yoffset>\d+) \(.+\) (?P<x_mm>\d+)mm x (?P<y_mm>\d+)mm',
        xrandr,
        re.MULTILINE
    )
    screens = [m.groupdict() for m in iterator]

    if len(screens) == 1:
        screens[0]['label'] = 'primary'
    else:
        for i, screen in enumerate(screens):
            screen['label'] = screen.get('primary', 'secondary')

    return screens


def display_xrandr_geometries(screens):
    for i, screen in enumerate(screens):
        screen['x_dpi'] = int(screen['width']) / int(screen['x_mm']) * 25.4
        screen['y_dpi'] = int(screen['height']) / int(screen['y_mm']) * 25.4
        for k, v in screen.items():
            for label in (str(i), screen['label']):
                if k in ('primary', 'label'):
                    continue
                print("screen_%s_%s=%s" % (label, k, v))

    print("monitors_connected=%d" % len(screens))


if __name__ == "__main__":
    screens = extract_xrandr_geometries()
    display_xrandr_geometries(screens)
