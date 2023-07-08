#!/bin/bash

# Sets the following variables:
#
#   tiny_font: for watchlogs-window and similar
#   small_font: for top-term and similar
#   medium_font: for terminals (and maybe emacs)
#   large_font: for modal dialogs like chrome-session-fzf

. $ZDOTDIR/lib/libhost.sh
# . $ZDOTDIR/lib/libdpy.sh

# Unreadably small:
#   nexus artsie outcast
#   Most of the stuff from xlsfonts Gv -- -

read_localhost_nickname

case "$localhost_nickname" in
    ionian)
        # 2560x1440 (92dpi) + 1920x1080 (93dpi)
        tiny_font='smoothansi'
        small_font='xft:Hack:pixelsize=9'
        medium_font='xft:Hack:pixelsize=10'
        large_font='xft:Hack:pixelsize=22'
        ;;
    aegean|celtic)
        # aegean: 3840x2160 (383dpi)
        # celtic: 2256x1504 (201dpi)
        tiny_font='10x20'
        #tiny_font='12x24'
        tiny_font='xft:Hack:pixelsize=18'
        #small_font='-misc-hack-medium-r-normal--0-0-0-0-m-0-iso8859-15'
        small_font='xft:Hack:pixelsize=24'
        medium_font='xft:Hack:pixelsize=30'
        large_font='xft:Hack:pixelsize=36'
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

medium_font_gnome="${medium_font#xft:}"
medium_font_gnome="${medium_font_gnome/:pixelsize=/ }"
