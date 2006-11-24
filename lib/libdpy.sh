#!/bin/bash

typeset -a dpy_geometry
#dpy_geometry=$( xwininfo -root | awk '/-geometry/ {print $2}' )
dpy_geometry=($( xprop -root -notype 32i ' $0 $1\n' _NET_DESKTOP_GEOMETRY ))

dpy_width=${dpy_geometry[1]}
dpy_height=${dpy_geometry[2]}
