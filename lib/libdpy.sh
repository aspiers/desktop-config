#!/bin/bash

typeset -a dpy_geometry
dpy_geometry=( $(
  xwininfo -root | \
  awk '/-geometry/ { sub(/x/, " ", $2); sub(/\+.*/, "", $2); print $2 }'
) )
# this doesn't work under nomachine's NX client
#dpy_geometry=($( xprop -root -notype 32i ' $0 $1\n' _NET_DESKTOP_GEOMETRY ))

# bash counts from 0, zsh counts from 1 - ugh!
_array_index_test=(0 1 2)
if [[ "${_array_index_test[1]}" == 1 ]]; then
    echo "shell array indices start at 0"
    dpy_width=${dpy_geometry[0]}
    dpy_height=${dpy_geometry[1]}
elif [[ "${_array_index_test[1]}" == 0 ]]; then
    echo "shell array indices start at 1"
    dpy_width=${dpy_geometry[1]}
    dpy_height=${dpy_geometry[2]}
fi
unset _array_index_test

if echo "$dpy_width$dpy_height" | egrep -q '[^0-9]'; then
    echo "xprop failed to determine _NET_DESKTOP_GEOMETRY; assuming 1280x1024" >&2
    dpy_width=1280
    dpy_height=1024
fi
