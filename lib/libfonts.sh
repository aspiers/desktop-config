#!/bin/bash

# Sets the following variables:
#
#   tiny_font: for watchlogs-window and similar
#   small_font: for top-term and similar
#   medium_font: for terminals (and maybe emacs)
#   large_font: for modal dialogs like chrome-session-fzf

. $ZDOTDIR/lib/libdpy.sh

# Unreadably small:
#   nexus artsie outcast
#   Most of the stuff from xlsfonts Gv -- -

if [[ $primary_width -gt 2000 ]]; then
    tiny_font='10x20'
    #tiny_font='12x24'
    tiny_font='xft:Hack:pixelsize=18'
    #small_font='-misc-hack-medium-r-normal--0-0-0-0-m-0-iso8859-15'
    small_font='xft:Hack:pixelsize=24'
    medium_font='xft:Hack:pixelsize=30'
    large_font='xft:Hack:pixelsize=36'
else
    tiny_font='smoothansi'
    small_font='10x20'
    medium_font='xft:Hack:pixelsize=24'
    large_font='xft:Hack:pixelsize=30'
fi
