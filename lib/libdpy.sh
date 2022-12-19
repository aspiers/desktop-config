#!/bin/bash

extract_xrandr_geometry () {
    xrandr | \
        perl -lne '
          if (/'"$1"'/ && / (\d+)x(\d+)\+(\d+)\+(\d+) \(.+\) (\d+)mm x (\d+)mm/) {
            print "width=$1";
            print "height=$2";
            print "xoffset=$3";
            print "yoffset=$4";
            print "x_mm=$5";
            print "y_mm=$6";
            printf "x_dpi=%.2f\n", ($1 / $5 * 25.4);
            printf "y_dpi=%.2f\n", ($2 / $6 * 25.4);
          }
        ' | \
        sed "s/^/$2_/"
}

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

    eval $( extract_xrandr_geometry ' connected primary ' primary )
    eval $( extract_xrandr_geometry ' connected \d' secondary )
    # this doesn't work under nomachine's NX client
    #dpy_geometry=($( xprop -root -notype 32i ' $0 $1\n' _NET_DESKTOP_GEOMETRY ))

    if [ -n "$ZSH_VERSION" ]; then
        # Make sure arrays start from 0 for this scope
        setopt local_options ksh_arrays
    fi

    dpy_width=${dpy_geometry[0]}
    dpy_height=${dpy_geometry[1]}

    if echo "$dpy_width$dpy_height" | egrep -q '[^0-9]'; then
        echo "failed to determine desktop geometry" >&2
    fi
}

get_dpy_geometry
