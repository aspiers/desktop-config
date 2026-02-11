#!/bin/bash
#
# Shared rofi theming library.
#
# Provides Catppuccin-based colour variables and a rofi theme string
# that adapts to the current dark/light setting in ~/.config/theme.
#
# Usage in a rofi script:
#
#   source "$(dirname "$0")/../lib/librofi.sh"
#
#   # Use ROFI_THEME_STR in your rofi invocation:
#   rofi -show foo -modes "foo:$0" \
#       -theme-str "$ROFI_THEME_STR" \
#       -theme-str 'window {width: 800px;}' \
#       ...
#
#   # Use ROFI_COLOUR_* variables in Pango markup:
#   echo "<span foreground=\"$ROFI_COLOUR_GREEN\">ok</span>"
#
# Exported variables:
#
#   ROFI_THEME          "light" or "dark"
#   ROFI_THEME_STR      Complete -theme-str value for rofi
#
#   Background/foreground:
#     ROFI_COLOUR_BG      Main background
#     ROFI_COLOUR_BG_ALT  Alternate background (inputbar, message)
#     ROFI_COLOUR_FG      Main foreground text
#     ROFI_COLOUR_FG_ALT  Subdued foreground text
#
#   Accents:
#     ROFI_COLOUR_ACCENT  Primary accent (prompt, links)
#     ROFI_COLOUR_BORDER  Border/separator colour
#     ROFI_COLOUR_SEL_BG  Selected element background
#     ROFI_COLOUR_SEL_FG  Selected element foreground
#     ROFI_COLOUR_URGENT  Urgent/error colour
#
#   Semantic signal colours (good contrast against ROFI_COLOUR_BG):
#     ROFI_COLOUR_GREEN
#     ROFI_COLOUR_YELLOW
#     ROFI_COLOUR_MAUVE
#     ROFI_COLOUR_RED

ROFI_THEME_FILE=~/.config/theme
ROFI_THEME=$(cat "$ROFI_THEME_FILE" 2>/dev/null || echo "dark")

# Colour values from https://github.com/catppuccin/rofi
if [[ "$ROFI_THEME" == "light" ]]; then
        # Catppuccin Latte
        ROFI_COLOUR_BG="#eff1f5"     # Base
        ROFI_COLOUR_BG_ALT="#e6e9ef" # Mantle
        ROFI_COLOUR_FG="#4c4f69"     # Text
        ROFI_COLOUR_FG_ALT="#6c6f85" # Subtext0
        ROFI_COLOUR_ACCENT="#1e66f5" # Blue
        ROFI_COLOUR_BORDER="#7287fd" # Lavender
        ROFI_COLOUR_SEL_BG="#1e66f5" # Blue
        ROFI_COLOUR_SEL_FG="#eff1f5" # Base (light text on blue)
        ROFI_COLOUR_URGENT="#d20f39" # Red
        ROFI_COLOUR_MSG_FG="#5c5f77" # Subtext1

        # Semantic signal colours (dark on light bg)
        ROFI_COLOUR_GREEN="#40a02b"  # Green
        ROFI_COLOUR_YELLOW="#df8e1d" # Yellow
        ROFI_COLOUR_MAUVE="#8839ef"  # Mauve
        ROFI_COLOUR_RED="#d20f39"    # Red
else
        # Catppuccin Mocha
        ROFI_COLOUR_BG="#1e1e2e"     # Base
        ROFI_COLOUR_BG_ALT="#181825" # Mantle
        ROFI_COLOUR_FG="#cdd6f4"     # Text
        ROFI_COLOUR_FG_ALT="#a6adc8" # Subtext0
        ROFI_COLOUR_ACCENT="#89b4fa" # Blue
        ROFI_COLOUR_BORDER="#b4befe" # Lavender
        ROFI_COLOUR_SEL_BG="#89b4fa" # Blue
        ROFI_COLOUR_SEL_FG="#1e1e2e" # Base (dark text on blue)
        ROFI_COLOUR_URGENT="#f38ba8" # Red
        ROFI_COLOUR_MSG_FG="#bac2de" # Subtext1

        # Semantic signal colours (bright on dark bg)
        ROFI_COLOUR_GREEN="#a6e3a1"  # Green
        ROFI_COLOUR_YELLOW="#f9e2af" # Yellow
        ROFI_COLOUR_MAUVE="#cba6f7"  # Mauve
        ROFI_COLOUR_RED="#f38ba8"    # Red
fi

# Build a reusable rofi theme string.
# Scripts can append extra -theme-str arguments to override
# specific properties (e.g. window width, listview lines).
read -r -d '' ROFI_THEME_STR <<-RASI || true
* {
    bg:        ${ROFI_COLOUR_BG};
    bg-alt:    ${ROFI_COLOUR_BG_ALT};
    fg:        ${ROFI_COLOUR_FG};
    fg-alt:    ${ROFI_COLOUR_FG_ALT};
    accent:    ${ROFI_COLOUR_ACCENT};
    urgent:    ${ROFI_COLOUR_URGENT};
    sel-bg:    ${ROFI_COLOUR_SEL_BG};
    sel-fg:    ${ROFI_COLOUR_SEL_FG};
    border-cl: ${ROFI_COLOUR_BORDER};

    /* Override default theme variables so inherited rules also use
       Catppuccin colours (e.g. textbox { text-color: var(foreground) }) */
    foreground:       @fg;
    background:       @bg;
    lightfg:          @fg;
    lightbg:          @bg-alt;
    separatorcolor:   @border-cl;
    blue:             @accent;
    red:              @urgent;
}
window {
    background-color: @bg;
    border:           2px;
    border-color:     @border-cl;
    border-radius:    8px;
}
mainbox {
    background-color: @bg;
    children:         [ inputbar, message, listview ];
}
inputbar {
    background-color: @bg-alt;
    text-color:       @fg;
    padding:          8px 12px;
    border:           0 0 1px 0;
    border-color:     @border-cl;
    children:         [ prompt, entry ];
}
prompt {
    background-color: inherit;
    text-color:       @accent;
    padding:          0 8px 0 0;
}
entry {
    background-color: inherit;
    text-color:       @fg;
    placeholder-color: @fg-alt;
}
message {
    background-color: @bg-alt;
    border:           0 0 1px 0;
    border-color:     @border-cl;
    padding:          4px 12px;
}
message textbox {
    background-color: inherit;
    text-color:       ${ROFI_COLOUR_MSG_FG};
}
listview {
    background-color: @bg;
    padding:          4px 0;
    scrollbar:        false;
}
element {
    background-color: @bg;
    text-color:       @fg;
    padding:          4px 12px;
}
element normal.normal {
    background-color: @bg;
    text-color:       @fg;
}
element alternate.normal {
    background-color: @bg-alt;
    text-color:       @fg;
}
element selected.normal {
    background-color: @sel-bg;
    text-color:       @sel-fg;
    border-radius:    4px;
}
element normal.urgent {
    background-color: @bg;
    text-color:       @urgent;
}
element alternate.urgent {
    background-color: @bg-alt;
    text-color:       @urgent;
}
element selected.urgent {
    background-color: @urgent;
    text-color:       @sel-fg;
    border-radius:    4px;
}
element normal.active {
    background-color: @bg;
    text-color:       @accent;
}
element alternate.active {
    background-color: @bg-alt;
    text-color:       @accent;
}
element selected.active {
    background-color: @accent;
    text-color:       @sel-fg;
    border-radius:    4px;
}
element-icon {
    background-color: inherit;
    size:             24px;
    padding:          0 8px 0 0;
}
element-text {
    background-color: inherit;
    text-color:       inherit;
}
RASI
