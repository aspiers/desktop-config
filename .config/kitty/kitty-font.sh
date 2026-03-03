#!/bin/bash
# Called via geninclude from kitty.conf to output font settings.
# Sources libfonts.sh to get per-host font name and size.

. "$ZDOTDIR/lib/libfonts.sh" 2>/dev/null

if [[ -n "$font_name" && -n "$medium_font_size" ]]; then
        echo "font_family      $font_name"
        echo "font_size        $medium_font_size"
else
        # Fallback
        echo "font_family      SauceCodePro Nerd Font"
        echo "font_size        12"
fi
