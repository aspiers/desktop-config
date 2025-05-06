#!/usr/bin/python3

import os
import sys
import re

# Simplified sys.path modification
sys.path.append(os.path.join(os.getenv('HOME'), 'lib'))
sys.path.append(os.path.join(os.getenv('HOME'), 'bin'))

import libdpy
import monitors_connected

INXI_RAW_CACHE = libdpy.INXI_RAW_CACHE_FILE

def check_large_monitor():
    # 1. Updates/populates inxi cache (both raw and JSON).
    libdpy.get_inxi_monitors(use_cache=False) # Force cache refresh

    # 2. Checks if a large monitor is connected.
    num_monitors = monitors_connected.get_monitors_count(use_cache=True)

    if num_monitors >= 2:
        if not os.path.exists(INXI_RAW_CACHE):
            sys.stderr.write(f"Error: INXI raw cache {INXI_RAW_CACHE} not found after get_inxi_monitors call.\n")
            return False
        with open(INXI_RAW_CACHE, 'r') as f_raw:
            if 'res: 3840x2160' in f_raw.read():
                return True
    return False

if __name__ == "__main__":
    if check_large_monitor():
        sys.exit(0)
    else:
        sys.exit(1)
