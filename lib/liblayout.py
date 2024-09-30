#!/usr/bin/python3

import os
import yaml

import libdpy


def get_layout_file(layout_name_or_path, dir=None):
    if os.path.isabs(layout_name_or_path):
        return layout_name_or_path
    return os.path.join(dir, layout_name_or_path) + '.yaml'


def get_sublayout_file():
    return os.path.expanduser('~/.fluxbox/sublayouts.yaml')


def percent(x, y):
    return round(x / y * 100)


def get_layout_params(layout_file):
    with open(layout_file) as f:
        layout = yaml.safe_load(f)

    screens = libdpy.extract_xrandr_screen_geometries().copy()
    if len(screens) != len(layout['screens']):
        sys.stderr.write(
            "xrandr got %d screens but %s had %d screens" %
            (len(screens), len(layout['screens']))
        )
        sys.exit(1)

    for i, s in enumerate(screens):
        # Note: this makes the unsafe assumption that the ordering of
        # screens in the YAML layout file match the ordering from
        # xrandr, but xrandr doesn't guarantee stable ordering.
        screen_layout = layout['screens'][i]
        s.update(screen_layout)

        s.setdefault('left_margin', 0)
        s.setdefault('right_margin', 0)
        s.setdefault('top_margin', 0)
        s.setdefault('bottom_margin', 0)

        s.setdefault('cols_1_2_margin', 0)
        s.setdefault('rows_1_2_margin', 0)

        s['logs_height'] = int(s['logs_height_pc'] * s['height'] / 100)

        s['active_width'] = s['width'] - s['left_margin'] - s['right_margin']
        s['active_height'] = s['height'] - s['top_margin'] - s['panel_height'] - s['logs_height']
        s['active_width_pc'] = percent(s['active_width'], s['width'])
        s['active_height_pc'] = percent(s['active_height'], s['height'])
        s['active_middle_x'] = s['left_margin'] + int(s['active_width'] / 2)
        s['active_middle_y'] = s['top_margin'] + int(s['active_height'] / 2)

        s['full_width'] = s['width']
        s['full_height'] = s['height'] - s['panel_height']

        s.setdefault('single_width_pc_of_active', 100)
        s.setdefault('single_height_pc_of_active', 100)
        s['single_width'] = int(s['single_width_pc_of_active'] * s['active_width'] / 100)
        s['single_height'] = int(s['single_height_pc_of_active'] * s['active_height'] / 100)
        s['single_left'] = s['left_margin'] + int((s['active_width'] - s['single_width']) / 2)
        s['single_top'] = s['top_margin'] + int((s['active_height'] - s['single_height']) / 2)
        s['single_middle_x'] = s['active_middle_x']
        s['single_middle_y'] = s['active_middle_y']

        s['cols_1_2_margin_pc_of_active'] = percent(s['cols_1_2_margin'], s['active_width'])
        s['rows_1_2_margin_pc_of_active'] = percent(s['rows_1_2_margin'], s['active_height'])

        s.setdefault('col1_width_pc_of_active', 50)
        s.setdefault('row1_height_pc_of_active', 50)
        s.setdefault('col2_width_pc_of_active', 100 - s['col1_width_pc_of_active'] - s['cols_1_2_margin_pc_of_active'])
        s.setdefault('row2_height_pc_of_active', 100 - s['row1_height_pc_of_active'] - s['rows_1_2_margin_pc_of_active'])

        s['col1_width'] = int(s['col1_width_pc_of_active'] * s['active_width'] / 100)
        s['col2_width'] = int(s['col2_width_pc_of_active'] * s['active_width'] / 100)
        s['col1_left'] = s['left_margin']
        s['col1_right'] = s['col1_left'] + s['col1_width']
        s['col2_left'] = s['col1_right'] + s['cols_1_2_margin']
        s['col2_right'] = s['col2_left'] + s['col2_width']
        s['col1_middle'] = s['col1_left'] + int(s['col1_width'] / 2)
        s['col2_middle'] = s['col2_left'] + int(s['col2_width'] / 2)

        s['row1_height'] = int(s['row1_height_pc_of_active'] * s['active_height'] / 100)
        s['row2_height'] = int(s['row1_height_pc_of_active'] * s['active_height'] / 100)
        s['row1_top'] = s['top_margin']
        s['row1_bottom'] = s['row1_top'] + s['row1_height']
        s['row2_top'] = s['row1_bottom'] + s['rows_1_2_margin']
        s['row2_bottom'] = s['row2_top'] + s['row2_height']
        s['row1_middle'] = s['row1_top'] + int(s['row1_height'] / 2)
        s['row2_middle'] = s['row2_top'] + int(s['row2_height'] / 2)

        s['SetHead'] = 'SetHead %d' % s['head']

    return screens, layout
