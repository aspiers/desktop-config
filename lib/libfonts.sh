#!/bin/bash

# Sets the following variables:
#
#   tiny_font: for watchlogs-window and similar
#   small_font: for top-term and similar
#   medium_font: for terminals (and maybe emacs)
#   large_font: for modal dialogs like chrome-session-fzf
#   xl_font: for minimal TUIs like bluetuith
#
# Also derives these variables from the above, so that
# gnome-terminal-config can use them:
#
#   small_font_gnome
#   medium_font_gnome
#   large_font_gnome
#
# And zoom factors for gnome-terminal (relative to medium_font for unified profiles):
#
#   small_font_gnome_zoom_from_medium
#   large_font_gnome_zoom_from_medium
#   xl_font_gnome_zoom_from_medium

. $ZDOTDIR/lib/libhost.sh
# eval $( $ZDOTDIR/lib/libdpy.py )

# Unreadably small:
#   nexus artsie outcast
#   Most of the stuff from xlsfonts Gv -- -

read_localhost_nickname

# font_name="Monospace"
font_name="Hack Nerd Font"
# font_name="Maple Mono NF"
font_name="SauceCodePro Nerd Font"

tk_font_name="Roboto"
tk_mono_font_name="Source Code Pro"

tiny_font_name="$font_name"
small_font_name="$font_name"
medium_font_name="$font_name"
large_font_name="$font_name"
emacs_font_height=130

case "$localhost_nickname" in
    ionian)
        # 2560x1440 (92dpi) + 1920x1080 (93dpi)
        tiny_font="smoothansi"
        small_font="xft:$small_font_name:size=11"
        medium_font="xft:$medium_font_name:size=12"
        medium_font_tk="$tk_font_name 12"
        medium_font_tk_mono="{$tk_mono_font_name} 12"
        large_font="xft:$large_font_name:size=16"
        xl_font="xft:$xl_font_name:size=20"
        ;;
    celtic)
        # 285mm x 190mm according grep mm /var/log/Xorg.0.log
        # new hi-res display 2880x1920 (256x256 dpi)
        # old matte display 2256x1504 (193x167 dpi)
        if large-monitor-connected; then
            tiny_font="smoothansi"
            #tiny_font="xft:$tiny_font_name:size=8"
            small_font="xft:$small_font_name:size=12"
            #small_font="10x20"
            medium_font="xft:$medium_font_name:size=14"
            medium_font_tk="$tk_font_name 9"
            medium_font_tk_mono="{$tk_mono_font_name} 9"
            large_font="xft:$large_font_name:size=16"
            xl_font="xft:$xl_font_name:size=20"
        else
            #tiny_font="smoothansi"
            tiny_font="xft:$tiny_font_name:size=12"
            small_font="xft:$small_font_name:size=12"
            #small_font="10x20"
            medium_font="xft:$medium_font_name:size=14"
            medium_font_tk="$tk_font_name 14"
            medium_font_tk_mono="{$tk_mono_font_name} 14"
            large_font="xft:$large_font_name:size=20"
            xl_font="xft:$xl_font_name:size=24"
        fi
        ;;
    aegean)
        # 3840x2160 (383dpi)
        tiny_font="xft:$tiny_font_name:size=5"
        #small_font="-misc-hack-medium-r-normal--0-0-0-0-m-0-iso8859-15"
        #small_font="10x20"
        small_font="xft:$small_font_name:size=10"
        medium_font="xft:$medium_font_name:size=12"
        medium_font_tk="$tk_font_name 9"
        medium_font_tk_mono="{$tk_mono_font_name} 9"
        large_font="xft:$large_font_name:size=16"
        xl_font="xft:$xl_font_name:size=20"
        ;;
    *)
        echo >&2 "libfonts: unsupported host $localhost_nickname"
        return 1
        # if [[ $primary_width -gt 2000 ]]; then
        # else
        #     tiny_font="smoothansi"
        #     small_font="10x20"
        #     medium_font="xft:$medium_font_name:pixelsize=24"
        #     large_font="xft:$large_font_name:pixelsize=30"
        # fi
        ;;
esac

small_font_gnome="${small_font#xft:}"
small_font_gnome="${small_font_gnome/:size=/ }"
medium_font_gnome="${medium_font#xft:}"
medium_font_gnome="${medium_font_gnome/:size=/ }"
large_font_gnome="${large_font#xft:}"
large_font_gnome="${large_font_gnome/:size=/ }"
xl_font_gnome="${xl_font#xft:}"
xl_font_gnome="${xl_font_gnome/:size=/ }"

# Calculate zoom factors for gnome-terminal (relative to medium_font for unified profiles)
medium_font_size=$(echo "$medium_font_gnome" | sed 's/.* \([0-9]\+\)$/\1/')
small_font_size=$(echo "$small_font_gnome" | sed 's/.* \([0-9]\+\)$/\1/')
large_font_size=$(echo "$large_font_gnome" | sed 's/.* \([0-9]\+\)$/\1/')
xl_font_size=$(echo "$xl_font_gnome" | sed 's/.* \([0-9]\+\)$/\1/')

small_font_gnome_zoom_from_medium=$(echo "scale=3; $small_font_size / $medium_font_size" | bc)
large_font_gnome_zoom_from_medium=$(echo "scale=3; $large_font_size / $medium_font_size" | bc)
xl_font_gnome_zoom_from_medium=$(echo "scale=3; $xl_font_size / $medium_font_size" | bc)
