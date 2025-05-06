#!/usr/bin/python3

import os
import sys

sys.path.append(os.getenv('HOME') + '/lib')

import libdpy

def get_monitors_count(use_cache=True):
    """Returns the number of connected monitors based on xrandr.

    Args:
        use_cache: Whether to use cached xrandr data. Defaults to True.
    """
    screens = libdpy.get_xrandr_screen_geometries(use_cache=use_cache)
    return len(screens)

if __name__ == "__main__":
    # Default behavior for CLI: use cache, consistent with original script's typical usage
    count = get_monitors_count(use_cache=True)
    print(count)
