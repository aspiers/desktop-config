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

# Check if layout file specifies a manual ui_scale to override dynamic calculation
get_layout_ui_scale() {
    local layout_file
    layout_file=$(get-layout)
    if [[ -f "$layout_file" ]]; then
        local scale
        scale=$(grep -E '^[[:space:]]*ui_scale:' "$layout_file" | sed 's/.*ui_scale:[[:space:]]*//')
        if [[ -n "$scale" ]]; then
            echo "$scale"
            return 0
        fi
    fi
    return 1
}

scale_factor=1
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
xl_font_name="$font_name"
emacs_font_height=130

case "$localhost_nickname" in
    ionian)
        # 2560x1440 (92dpi) + 1920x1080 (93dpi)
        tiny_font="smoothansi"
        small_font="xft:${small_font_name}:size=11"
        medium_font="xft:${medium_font_name}:size=12"
        medium_font_tk="$tk_font_name 12"
        medium_font_tk_mono="$tk_mono_font_name 12"
        large_font="xft:${large_font_name}:size=16"
        xl_font="xft:${xl_font_name}:size=20"
        ;;
    celtic)
        # 285mm x 190mm according grep mm /var/log/Xorg.0.log
        # new hi-res display 2880x1920 (256x256 dpi)
        # old matte display 2256x1504 (193x167 dpi)

        # Check for manual ui_scale in layout file first
        if ui_scale=$(get_layout_ui_scale); then
            scale_factor="$ui_scale"
        else
            # Calculate font sizes based on DPI scale factor
            scale_factor=$($ZDOTDIR/lib/libdpy.py --calculate-ui-scale)
        fi
        #scale_factor=1

        # Base sizes at 1.0 scale.  These must work on celtic
        # with no monitors connected.
        base_tiny=8
        base_small=10
        base_medium_tk=12
        base_medium=14
        base_large=16
        base_xl=22

        # LG HDR 4k in Level 39 is 3840x2160 600x340mm (162x161 dpi)
        # original target sizes for large-monitor-connected:
        # tiny: 8
        # small: 12
        # medium: 14
        # medium_font_tk="$tk_font_name 9"
        # large: 16
        # xl: 20

        # original target sizes for celtic only (layout DPI 128):
        # tiny: 12
        # small: 12
        # medium: 14
        # medium_font_tk="$tk_font_name 14"
        # large: 20
        # xl: 24

        # Scale and round to nearest integer.  One awk invocation replaces
        # seven bc subshells: rounding for six font sizes plus the
        # tiny-font threshold check, ~80ms saved on every rofi launch.
        eval "$(awk -v s="$scale_factor" \
            -v bt="$base_tiny" -v bs="$base_small" -v bm="$base_medium" \
            -v bmt="$base_medium_tk" -v bl="$base_large" -v bx="$base_xl" \
            'BEGIN {
                printf "tiny_size=%d small_size=%d medium_size=%d ",
                    int(bt*s+0.5), int(bs*s+0.5), int(bm*s+0.5)
                printf "medium_tk_size=%d large_size=%d xl_size=%d ",
                    int(bmt*s+0.5), int(bl*s+0.5), int(bx*s+0.5)
                printf "tiny_use_smoothansi=%d\n", (s > 2.0) ? 1 : 0
            }')"

        if [ "$tiny_use_smoothansi" -eq 1 ]; then
            tiny_font="smoothansi"
        else
            tiny_font="xft:${tiny_font_name}:size=${tiny_size}"
        fi

        small_font="xft:${small_font_name}:size=${small_size}"
        medium_font="xft:${medium_font_name}:size=${medium_size}"
        medium_font_tk="$tk_font_name $medium_tk_size"
        medium_font_tk_mono="{$tk_mono_font_name} $medium_tk_size"
        large_font="xft:${large_font_name}:size=${large_size}"
        xl_font="xft:${xl_font_name}:size=${xl_size}"
        ;;
    aegean)
        # 3840x2160 (383dpi)
        tiny_font="xft:${tiny_font_name}:size=5"
        #small_font="-misc-hack-medium-r-normal--0-0-0-0-m-0-iso8859-15"
        #small_font="10x20"
        small_font="xft:${small_font_name}:size=10"
        medium_font="xft:${medium_font_name}:size=12"
        medium_font_tk="$tk_font_name 9"
        medium_font_tk_mono="{$tk_mono_font_name} 9"
        large_font="xft:${large_font_name}:size=16"
        xl_font="xft:${xl_font_name}:size=20"
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

tiny_font_gnome="${tiny_font#xft:}"
tiny_font_gnome="${tiny_font_gnome/:size=/ }"
small_font_gnome="${small_font#xft:}"
small_font_gnome="${small_font_gnome/:size=/ }"
medium_font_gnome="${medium_font#xft:}"
medium_font_gnome="${medium_font_gnome/:size=/ }"
large_font_gnome="${large_font#xft:}"
large_font_gnome="${large_font_gnome/:size=/ }"
xl_font_gnome="${xl_font#xft:}"
xl_font_gnome="${xl_font_gnome/:size=/ }"

# Calculate zoom factors for gnome-terminal (relative to medium_font for
# unified profiles).  Pure-bash size extraction (parameter expansion strips
# everything up to and including the last space) avoids 5 sed subshells.
tiny_font_size="${tiny_font_gnome##* }"
medium_font_size="${medium_font_gnome##* }"
small_font_size="${small_font_gnome##* }"
large_font_size="${large_font_gnome##* }"
xl_font_size="${xl_font_gnome##* }"

# One awk invocation replaces 3 bc subshells.
eval "$(awk -v s="$small_font_size" -v m="$medium_font_size" \
    -v l="$large_font_size" -v x="$xl_font_size" \
    'BEGIN {
        printf "small_font_gnome_zoom_from_medium=%.3f ", s/m
        printf "large_font_gnome_zoom_from_medium=%.3f ", l/m
        printf "xl_font_gnome_zoom_from_medium=%.3f\n",   x/m
    }')"

# Output all variables if script is executed directly (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    cat <<EOF
scale_factor=$scale_factor
tiny_font=$tiny_font
small_font=$small_font
medium_font=$medium_font
medium_font_tk=$medium_font_tk
medium_font_tk_mono=$medium_font_tk_mono
large_font=$large_font
xl_font=$xl_font

small_font_gnome=$small_font_gnome
medium_font_gnome=$medium_font_gnome
large_font_gnome=$large_font_gnome
xl_font_gnome=$xl_font_gnome

small_font_gnome_zoom_from_medium=$small_font_gnome_zoom_from_medium
large_font_gnome_zoom_from_medium=$large_font_gnome_zoom_from_medium
xl_font_gnome_zoom_from_medium=$xl_font_gnome_zoom_from_medium
EOF
fi
