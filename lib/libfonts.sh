#!/bin/bash

# Sets the following variables:
#
#   tiny_font: for watchlogs-window and similar
#   small_font: for top-term and similar
#   medium_font: for terminals (and maybe emacs)
#   large_font: for modal dialogs like chrome-session-fzf

. $ZDOTDIR/lib/libhost.sh
# eval $( $ZDOTDIR/lib/libdpy.py )

# Unreadably small:
#   nexus artsie outcast
#   Most of the stuff from xlsfonts Gv -- -

read_localhost_nickname

case "$localhost_nickname" in
    ionian)
        # 2560x1440 (92dpi) + 1920x1080 (93dpi)
        tiny_font='smoothansi'
        small_font='xft:Monospace:size=8'
        medium_font='xft:Monospace:size=10'
        medium_font_tk='Roboto 10'
        medium_font_tk_mono='{Source Code Pro} 10'
        large_font='xft:Hack:size=16'
        ;;
    celtic)
        # 2256x1504 (193x167 dpi)
        if l39-monitor-connected; then
            tiny_font='smoothansi'
            #tiny_font='xft:Monospace:size=8'
            small_font='xft:Hack:size=12'
            #small_font='10x20'
            medium_font='xft:Monospace:size=14'
            medium_font_tk='Roboto 9'
            medium_font_tk_mono='{Source Code Pro} 9'
            large_font='xft:Hack:size=16'
        else
            tiny_font='smoothansi'
            #tiny_font='xft:Monospace:size=8'
            small_font='xft:Hack:size=11'
            #small_font='10x20'
            medium_font='xft:Monospace:size=12'
            medium_font_tk='Roboto 8'
            medium_font_tk_mono='{Source Code Pro} 8'
            large_font='xft:Hack:size=18'
        fi
        ;;
    aegean)
        # 3840x2160 (383dpi)
        tiny_font='xft:Monospace:size=8'
        #small_font='-misc-hack-medium-r-normal--0-0-0-0-m-0-iso8859-15'
        #small_font='10x20'
        small_font='xft:Hack:size=10'
        medium_font='xft:Monospace:size=12'
        medium_font_tk='Roboto 12'
        medium_font_tk_mono='{Source Code Pro} 12'
        large_font='xft:Hack:size=16'
        ;;
    *)
        echo >&2 "libfonts: unsupported host $localhost_nickname"
        return 1
        # if [[ $primary_width -gt 2000 ]]; then
        # else
        #     tiny_font='smoothansi'
        #     small_font='10x20'
        #     medium_font='xft:Hack:pixelsize=24'
        #     large_font='xft:Hack:pixelsize=30'
        # fi
        ;;
esac

small_font_gnome="${small_font#xft:}"
small_font_gnome="${small_font_gnome/:size=/ }"
medium_font_gnome="${medium_font#xft:}"
medium_font_gnome="${medium_font_gnome/:size=/ }"
large_font_gnome="${large_font#xft:}"
large_font_gnome="${large_font_gnome/:size=/ }"
