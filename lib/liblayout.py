#!/usr/bin/python3

import os

import libdpy


def percent(x, y):
    return round(x / y * 100)


def get_layout_params(layout_file, layout):
    screens = libdpy.extract_xrandr_screen_geometries().copy()
    if len(screens) != len(layout['screens']):
        sys.stderr.write(
            "xrandr got %d screens but %s had %d screens" %
            (len(screens), len(layout['screens']))
        )
        sys.exit(1)

    for i, s in enumerate(screens):
        screen_layout = layout['screens'][i]
        s.update(screen_layout)

        s.setdefault('left_margin', 0)
        s.setdefault('cols_1_2_margin', 0)
        s.setdefault('right_margin', 0)
        s.setdefault('top_margin', 0)
        s.setdefault('bottom_margin', 0)
        s['active_width'] = s['width'] - s['left_margin'] - s['right_margin']
        s['active_width_pc'] = percent(s['active_width'], s['width'])
        s['col1_width'] = int(s['col1_width_pc_of_active'] * s['active_width'] / 100)
        s['col1_left'] = s['left_margin']
        s['col1_right'] = s['col1_left'] + s['col1_width']
        s['col2_left'] = s['col1_right'] + s['cols_1_2_margin']
        s['col2_width'] = int(s['col2_width_pc_of_active'] * s['active_width'] / 100)
        s['col2_right'] = s['col2_left'] + s['col2_width']

        s['logs_height'] = int(s['logs_height_pc'] * s['height'] / 100)

        s['active_height'] = s['height'] - s['top_margin'] - s['panel_height'] - s['logs_height']
        s['active_height_pc'] = percent(s['active_height'], s['height'])
        s['2row_height'] = int(s['active_height'] / 2)
        s['SetHead'] = 'SetHead %d' % s['head']

    return screens
