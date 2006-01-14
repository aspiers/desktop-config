. ~/.shared_env

echo -n "X session started: "
date

executable_p () {
    which "$1" >/dev/null 2>&1
}

run_if_executable () {
    executable_p "$1" && "$@"
}

echo $DISPLAY > ~/.Xdisplay.${HOST%%.*}

if [ -z "$VNCDESKTOP" ]; then
  if [ -e ~/.Xmodmap.${HOST%%.*} ]; then
    . ~/.Xmodmap.${HOST%%.*}
  elif [ -e ~/.Xmodmap.local ]; then
    . ~/.Xmodmap.local
  fi
fi

[ -e ~/.Xresources/all.rdb ] && xrdb -merge ~/.Xresources/all.rdb
for dir in ~/.X_fonts ~/.truetype; do
  if [ -d $dir ]; then
    xset fp+ $dir
  fi
done
xset fp rehash

[ -e ~/.xsession.local.pre       ] && . ~/.xsession.local.pre
[ -e ~/.xsession.${HOST%%.*}.pre ] && . ~/.xsession.${HOST%%.*}.pre

run_if_exists ~/bin/xwrits &
run_if_exists workrave &
run_if_exists xscreensaver -no-splash &
#run_if_exists xmms &

if [ -e /etc/X11/xorg.conf ] &&
   ls -l /etc/X11/xorg.conf | grep -q 'TrackPoint';
then
  xset m 4 5
else
  xset m 2 4
fi

xset r rate 180 50

if executable_p Esetroot; then
  run_if_exists retrieve_background
#  run_if_exists randomise_background
fi

if [ -z "$VNCDESKTOP" ]; then
  run_if_exists gkrellm -geometry -0-0 &
fi

# assume bash
typeset -a dpy_geometry
#dpy_geometry=$( xwininfo -root | awk '/-geometry/ {print $2}' )
dpy_geometry=($( xprop -root -notype 32i ' $0 $1\n' _NET_DESKTOP_GEOMETRY ))
dpy_width=${dpy_geometry[1]}
dpy_height=${dpy_geometry[2]}
echo $dpy_width x $dpy_height
# Each char in smoothansi is 13 pixels high by 6 pixels wide.
# Take 10% of height:
watchlogs_height=$(( (dpy_height / 5) / 13 ))
# Allow some space on the right for gkrellm:
watchlogs_width=$(( (dpy_width - 90) / 6 ))
run_if_exists \
  Eterm -t watchlogs-system -g ${watchlogs_width}x${watchlogs_height}+0-0 &

if executable_p sawfish; then
  export KDEWM=sawfish
fi

[ -e ~/.xsession.local       ] && . ~/.xsession.local
[ -e ~/.xsession.${HOST%%.*} ] && . ~/.xsession.${HOST%%.*}

if executable_p startkde; then
  exec startkde
fi

exec sawfish

# oh fuck
exec fvwm
exec twm

# this script must not terminate, otherwise its callers assume
# the X session has finished
wait
