#!/bin/bash

host=${HOST:-$HOSTNAME}
host=${host%%.*}
exec 1> ~/.xsession-my-errors.${host} 2>&1

. ~/.shared_env

echo -n "### .xsession started: "
date

echo "### WINDOWMANAGER=$WINDOWMANAGER"

. ~/lib/libproc.sh

echo $DISPLAY > ~/.Xdisplay.${HOST%%.*}

echo "### dbus ..."
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && executable_p dbus-launch; then
  eval `dbus-launch --sh-syntax --exit-with-session`
fi

echo "### .xsession-prewm.d ..."
. $ZDOT_RUN_HOOKS .xsession-prewm.d

xprop -root > /tmp/xprop.out
xprop -root -remove _NET_NUMBER_OF_DESKTOPS -remove _NET_DESKTOP_NAMES -remove _NET_CURRENT_DESKTOP

for ob in openbox-kde-session openbox-session openbox; do
    if executable_p $ob; then
        echo "### $ob ..."
        export KDEWM=openbox
        run_if_executable kdetrayproxy &
        break
    fi
done

# Note that ~/.cfg/SESSION/.cfg-post.d/SESSION ensures
# that there is a symlink from within ~/.kde/Autostart
# to .xsession-progs so we can exec if starting kde,
# otherwise we need to start .xsession-progs here.
if executable_p startkde; then
  echo "### startkde ..."
  exec startkde
elif executable_p kde; then
  echo "### kde ..."
  exec kde
fi

echo "!!! startkde and kde both not executable :-("

if executable_p gnome-session; then
  echo "### gnome ..."
  gnome-session &
else
  echo "### oh fuck... fvwm or worse"
  ( fvwm || twm ) &
fi

echo "### ~/.xsession-progs ..."
. ~/.xsession-progs &

# this script must not terminate, otherwise its callers assume
# the X session has finished
echo "### end of .xsession, waiting for children to exit ..."
wait
