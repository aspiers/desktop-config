ADAMS_WM=sawfish

. ~/.shared_env

echo -n "X session started: "
date

echo $DISPLAY > ~/.Xdisplay

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

[ -x ~/bin/xwrits ] && ~/bin/xwrits &
which xscreensaver 2>/dev/null && xscreensaver -no-splash &

#which xmms 2>/dev/null >/dev/null && xmms &

if [ -e /etc/X11/XF86Config ] &&
   ls -l /etc/X11/XF86Config | grep -q 'TrackPoint';
then
  xset m 4 5
else
  xset m 2 4
fi

xset r rate 250 50
xset r rate 200 90 # this one might get ignored

run_wm () {
  "$@" && kill $$ &
}

if [ "$ADAMS_WM" = 'windowmaker' ]; then
  which wmaker 2>/dev/null >/dev/null && run_wm wmaker
elif [ "$ADAMS_WM" = 'enlightenment' ]; then
# wmapm &
# wmppp &

  which enlightenment 2>/dev/null >/dev/null && run_wm enlightenment
elif [ "$ADAMS_WM" = 'sawfish' ]; then
  if which Esetroot 2>/dev/null >/dev/null; then
    which retrieve_background 2>/dev/null >/dev/null && retrieve_background
#   which randomise_background 2>/dev/null >/dev/null && randomise_background
  fi

  if [ -z "$VNCDESKTOP" ]; then
    which gkrellm 2>/dev/null >/dev/null && gkrellm -geometry -0-0 &
  fi

  # something like this goes in local
  # which Eterm 2>/dev/null >/dev/null && Eterm -t watchlogs-system -g 100x12+770-0 &

  if which sawfish 2>/dev/null >/dev/null; then
    run_wm sawfish
  elif which sawmill 2>/dev/null >/dev/null; then
    run_wm sawmill
  else
    exit 1
  fi
fi

sleep 1

#which linpopup 2>/dev/null >/dev/null && linpopup -min &

[ -e ~/.xsession.local       ] && . ~/.xsession.local
[ -e ~/.xsession.${HOST%%.*} ] && . ~/.xsession.${HOST%%.*}

# this script must not terminate, otherwise its callers assume
# the X session has finished
while /bin/true; do
  sleep 999d
done
