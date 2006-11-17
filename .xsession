#!/bin/bash

host=${HOST:-$HOSTNAME}
host=${host%%.*}
exec 1> ~/.xsession-my-errors.${host} 2>&1

. ~/.shared_env

echo -n "### .xsession started: "
date

. ~/lib/libproc.sh

echo $DISPLAY > ~/.Xdisplay.${HOST%%.*}

echo "### dbus ..."
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ] && executable_p dbus-launch; then
  eval `dbus-launch --sh-syntax --exit-with-session`
fi

echo "### Fonts ..."
# also change SESSION/.cfg-post.d/SESSION
for dir in ~/.X_fonts ~/.truetype; do
  if [ -d $dir ]; then
    if xset fp+ $dir; then
      echo "Added font dir $dir"
    else
      echo "Failed to add font dir $dir"
    fi
  else
    echo "Warning: $dir doesn't exist"
  fi
done
xset fp rehash

echo "### devilspie ..."
if executable_p devilspie; then
  devilspie >& ~/.devilspie-${HOST%%.*}.log &
else
  echo "Warning: devilspie not executable"
fi

if executable_p openbox; then
  export KDEWM=openbox
  kdetrayproxy &
fi

# Note that ~/.cfg/SESSION/.cfg-post.d/SESSION ensures
# that there is a symlink from within ~/.kde/Autostart
# to .xsession-progs so we can exec if starting kde,
# otherwise we need to start .xsession-progs here.
if executable_p startkde; then
  exec startkde
elif executable_p kde; then
  exec kde
fi

echo "startkde and kde both not executable :-("

if executable_p gnome-session; then
  gnome-session &
else
  # oh fuck
  ( fvwm || twm ) &
fi

echo "### ~/.xsession-progs ..."
. ~/.xsession-progs &

# this script must not terminate, otherwise its callers assume
# the X session has finished
echo "### end of .xsession, waiting for children to exit ..."
wait
