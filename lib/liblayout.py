#!/usr/bin/python3
#
# Note: screens section of layout files should match the
# order given by X coordinates in xrandr output.
#
# Screen numbers from libdpy are numbered counting from 0
# and going left to right by X coordinate.
#
# head numbers are what fluxbox SetHead and Head matcher
# use.  They start at 1, which is the primary display.

import os
import sys
import json
import re
import yaml

import libdpy


def warn(msg):
    sys.stderr.write(msg + "\n")


def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def get_layout_file(layout_name_or_path, dir=os.path.expanduser('~/.fluxbox/layouts')):
    if os.path.isabs(layout_name_or_path):
        return layout_name_or_path
    return os.path.join(dir, layout_name_or_path) + '.yaml'


def read_layout_file(layout_name_or_path, dir=os.path.expanduser('~/.fluxbox/layouts')):
    with open(get_layout_file(layout_name_or_path, dir)) as f:
        return f.read()


def count_layout_screens(layout_name_or_path, dir=os.path.expanduser('~/.fluxbox/layouts')):
    """
    Count the number of screens defined in a layout file.

    Args:
        layout_name_or_path: Layout name (without .yaml) or full path
        dir: Directory containing layout files (default: ~/.fluxbox/layouts)

    Returns:
        Integer count of screens defined in the layout's screens section

    Raises:
        FileNotFoundError: If layout file doesn't exist
        yaml.YAMLError: If YAML is malformed
        KeyError: If layout file doesn't have a 'screens' section
    """
    layout_file = get_layout_file(layout_name_or_path, dir)
    with open(layout_file) as f:
        content = f.read()
        content = process_includes(content)
        try:
            layout = yaml.safe_load(content)
        except yaml.YAMLError as e:
            die(f"YAML parsing error in {layout_file}: {e}")

    if 'screens' not in layout:
        die(f"Layout file {layout_file} has no 'screens' section")

    return len(layout['screens'])


def get_sublayout_file():
    return os.path.expanduser('~/.fluxbox/sublayouts.yaml')


def percent(x, y):
    return round(x / y * 100)


def parse_value(value, reference):
    """
    Parse a value that can be either pixels (int) or percentage (string with %).
    Returns the pixel value.

    Args:
        value: Either an int (pixels) or string ending with '%'
        reference: The reference value for percentage calculations (e.g., screen width)

    Returns:
        Integer pixel value
    """
    if isinstance(value, str) and value.endswith('%'):
        percentage = float(value[:-1])
        return int(percentage * reference / 100)
    return int(value)


def process_includes(content, indent=''):
    return re.sub(
        r'^([ \t]*)<INCLUDE\s+(.+?)>',
        lambda m: get_include_content(m.group(2), m.group(1)),
        content,
        flags=re.MULTILINE
    )

def get_include_content(include_file, parent_indent=''):
    to_include = read_layout_file(include_file)
    # Process includes recursively
    processed = process_includes(to_include)
    # Add parent's indentation to each line
    return parent_indent + processed.replace('\n', '\n' + parent_indent)

# Returns (embellished_xrandr_screens, layout) where:
#
# - embellished_xrandr_screens is xrandr per-screen data with extra
#   config from the "screens" section of the layout file merged
#   in. Note: this assumes that the ordering of screens in the YAML
#   layout file match the ordering based on X offsets given by xrandr.
#
# - layout is the parsed YAML layout file, which is a top-level dict
#   with two keys:
#
#   - screens (used to calculate embellished_xrandr_screens above)
#
#   - windows, which is an array of [window_matcher, *layout_cmds]
#     arrays
def get_layout_params(layout_file, use_cache=False):
    with open(layout_file) as f:
        content = f.read()
        content = process_includes(content)
        # sys.stderr.write(content)
        try:
            layout = yaml.safe_load(content)
        except yaml.YAMLError as e:
            die("YAML parsing error:", e)

    screens = libdpy.get_xrandr_screen_geometries(use_cache=use_cache).copy()
    if len(screens) != len(layout['screens']):
        die(
            "xrandr got %d screens but %s had %d screens\n" %
            (len(screens), layout_file, len(layout['screens']))
        )

    if len(screens) == 1:
        screen_layout = layout['screens'][0]
        assignment = screen_layout.get('assignment')
        if assignment and assignment != 'primary':
            warn(f'WARNING: sole screen had assignment {assignment} not primary')
        screen_layout['assignment'] = 'primary'

    for i, s in enumerate(screens):
        # Note: this assumes that the ordering of screens in the YAML
        # layout file match the ordering based on X offsets given by
        # xrandr.
        screen_layout = layout['screens'][i]
        if not screen_layout.get('name'):
            die(f'screen {i} is missing name in layout')

        assignment = screen_layout.get('assignment')

        if assignment == "primary" and not s["primary"]:
            die(f'Screen {screen_layout["name"]} was assigned as primary '
                'by layout but not by xrandr')

        if not assignment and s["primary"]:
            die(f'Screen {screen_layout["name"]} was assigned as primary '
                'by xrandr but not by layout')

        s.update(screen_layout)

        # Parse margins - can be pixels (int) or percentage (string with %)
        for margin in ['left_margin', 'right_margin']:
            s[margin] = parse_value(s.get(margin, 0), s['width'])

        for margin in ['top_margin', 'bottom_margin']:
            s[margin] = parse_value(s.get(margin, 0), s['height'])

        s['cols_1_2_margin'] = parse_value(s.get('cols_1_2_margin', 0), s['width'])
        s['rows_1_2_margin'] = parse_value(s.get('rows_1_2_margin', 0), s['height'])

        s['logs_height'] = int(s['logs_height_pc'] * s['height'] / 100)

        s['active_left'] = s['left_margin']
        s['active_top'] = s['top_margin']
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

        s['head'] = s['head']
        s['SetHead'] = 'SetHead %d' % s['head']

    return screens, layout


def get_adjacent_screen(direction, layout_name_or_path=None, use_cache=False):
    """
    Get the screen to the left or right of the current screen.

    Args:
        direction (str): Either 'left' or 'right' to specify which adjacent screen to find
        layout_name_or_path (str, optional): Layout name or path to use. If None, will need to be determined.
        use_cache (bool, optional): Whether to use cached XRandr data if available. Default is False.

    Returns:
        dict: The adjacent screen information or None if not found
    """
    if direction not in ('left', 'right'):
        raise ValueError("Direction must be either 'left' or 'right'")

    # Get current screen where mouse is located
    current_screen = libdpy.get_current_screen_info(use_cache=use_cache)

    # We need to have a layout file to look up the mapping
    if not layout_name_or_path:
        # In a real implementation, you would need to determine the active layout
        # This is a placeholder - you might want to add code to detect the active layout
        raise ValueError("layout_name_or_path must be provided")

    layout_file = get_layout_file(layout_name_or_path)
    screens, layout = get_layout_params(layout_file, use_cache=use_cache)

    # Find the current screen in the layout using the num attribute
    # This assumes the screens array in both libdpy and the layout file
    # are ordered the same way (by x_offset)
    current_screen_num = current_screen['num']

    if current_screen_num < 0 or current_screen_num >= len(screens):
        return None

    current_layout_screen = screens[current_screen_num]

    # Check if the current screen has the requested adjacent screen
    if direction in current_layout_screen:
        adjacent_name = current_layout_screen[direction]

        # Find the screen with this name
        for screen in screens:
            if 'name' in screen and screen['name'] == adjacent_name:
                return screen
            # Some screens might use 'assignment' instead of 'name'
            elif 'assignment' in screen and screen['assignment'] == adjacent_name:
                return screen

    return None


def main():
    if len(sys.argv) < 2:
        # No layout specified, use bin/get-layout to determine it
        import subprocess
        bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'bin')
        get_layout_script = os.path.join(bin_dir, 'get-layout')
        try:
            result = subprocess.run([get_layout_script], capture_output=True, text=True, check=True)
            layout_file = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            die(f"Failed to get layout from {get_layout_script}: {e.stderr}")
    else:
        exe, layout_name, *_rest = sys.argv
        layout_file = get_layout_file(layout_name)
    screens, layout = get_layout_params(layout_file)
    print(json.dumps({'screens': screens, 'layout': layout}, indent=2))


if __name__ == "__main__":
    main()
