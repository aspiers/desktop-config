ADAMS_WM=sawfish

[ -x ~/bin/xwrits ] && ~/bin/xwrits &
xscreensaver -no-splash &

#~/bin/xmms &

if ls -l /etc/X11/XF86Config | grep -q 'TrackPoint'; then
  xset m 4 5
else
  xset m 2 4
fi

if [ -d /share/X_fonts/misc ] && echo "$DISPLAY" | grep -q mediaconsult; then
  xset fp+ /share/X_fonts/misc
fi

if [ "$ADAMS_WM" = 'windowmaker' ]; then
  which wmaker >&/dev/null && exec wmaker
elif [ "$ADAMS_WM" = 'enlightenment' ]; then
#  wmapm &
#  wmppp &

  which enlightenment >&/dev/null && exec enlightenment
elif [ "$ADAMS_WM" = 'sawfish' ]; then
  randomise_background

  which gkrellm >&/dev/null && gkrellm -geometry -0-0 &
  which Eterm >&/dev/null && Eterm -t watchlogs-system -g 200x12+0-0 &

  export GGDIR=~/moo

  if which sawfish >&/dev/null; then
    sawfish
  elif which sawmill >&/dev/null; then
    sawmill
  else
    exit 1
  fi
fi

linpopup -min &
