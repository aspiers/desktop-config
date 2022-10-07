#!/bin/bash

get_dpy_geometry () {
    typeset -a dpy_geometry primary_geometry
    dpi=(
        $(
            xdpyinfo | \
                awk '/resolution: .* dots per inch/ { sub(/x/, " ", $2); print $2}'
        )
    )

    dpy_geometry=(
        $(
            xwininfo -root | \
                awk '/-geometry/ { sub(/x/, " ", $2); sub(/\+.*/, "", $2); print $2 }'
        )
    )

    primary_geometry=(
        $(
            xrandr | awk '/primary/ { sub(/x/, " ", $4); sub(/\+.*/, "", $4); print $4 }'
        )
    )
    # this doesn't work under nomachine's NX client
    #dpy_geometry=($( xprop -root -notype 32i ' $0 $1\n' _NET_DESKTOP_GEOMETRY ))

    if [ -n "$ZSH_VERSION" ]; then
        # Make sure arrays start from 0 for this scope
        setopt local_options ksh_arrays
    fi

    dpy_width=${dpy_geometry[0]}
    dpy_height=${dpy_geometry[1]}

    primary_width=${primary_geometry[0]}
    primary_height=${primary_geometry[1]}

    if echo "$dpy_width$dpy_height" | egrep -q '[^0-9]'; then
        echo "failed to determine desktop geometry" >&2
    fi
}

get_dpy_geometry
