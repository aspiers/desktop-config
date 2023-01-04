#!/bin/bash

. $ZDOTDIR/lib/libdpy.sh

if [[ $dpy_width -gt 2000 ]]; then
    #small_font=10x20
    #small_font=12x24
    small_font='xft:Hack:pixelsize=24'
else
    small_font=smoothansi
fi
