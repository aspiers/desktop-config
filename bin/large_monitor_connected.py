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

def large_monitor_connected():
    num_monitors = monitors_connected.get_monitors_count()

    if num_monitors >= 2:
        if not os.path.exists(INXI_RAW_CACHE):
            sys.stderr.write(f"Error: INXI raw cache {INXI_RAW_CACHE} not found after get_inxi_monitors call.\n")
            return False
        with open(INXI_RAW_CACHE, 'r') as f_raw:
            if 'res: 3840x2160' in f_raw.read():
                return True
    return False

if __name__ == "__main__":
    sys.exit(0 if large_monitor_connected() else 1)
