[ -d ~/local/bin ] && PATH=~/local/bin:"$PATH"

echo -n "X session started: "
date

ADAMS_WM=sawfish

[ -e ~/.xsession.local.pre ] && . ~/.xsession.local.pre

[ -x ~/bin/xwrits ] && ~/bin/xwrits &
xscreensaver -no-splash &

which xmms >&/dev/null && xmms &

if [ -e /etc/X11/XF86Config ] &&
   ls -l /etc/X11/XF86Config | grep -q 'TrackPoint';
then
  xset m 4 5
else
  xset m 2 4
fi

xset r rate 250 70

run_wm () {
  "$@" && kill $$ &
}

if [ "$ADAMS_WM" = 'windowmaker' ]; then
  which wmaker >&/dev/null && run_wm wmaker
elif [ "$ADAMS_WM" = 'enlightenment' ]; then
# wmapm &
# wmppp &

  which enlightenment >&/dev/null && run_wm enlightenment
elif [ "$ADAMS_WM" = 'sawfish' ]; then
  if which Esetroot >&/dev/null; then
    which retrieve_background >&/dev/null && retrieve_background
#   which randomise_background >&/dev/null && randomise_background
  fi

  which gkrellm >&/dev/null && gkrellm -geometry -0-0 &

  # something like this goes in local
  # which Eterm >&/dev/null && Eterm -t watchlogs-system -g 100x12+770-0 &

  export GGDIR=`cat ~/.ggdir`

  if which sawfish >&/dev/null; then
    run_wm sawfish
  elif which sawmill >&/dev/null; then
    run_wm sawmill
  else
    exit 1
  fi
fi

sleep 1

which linpopup >&/dev/null && linpopup -min &

[ -e ~/.xsession.local ] && . ~/.xsession.local

# this script must not terminate, otherwise its callers assume
# the X session has finished
while /bin/true; do
  sleep 999d
done
