#!/bin/bash

exec 1> ~/.xsession-my-errors 2>&1

. ~/.shared_env

echo -n "### .xsession started: "
date

executable_p () {
    which "$1" >/dev/null 2>&1
}

run_if_executable () {
    executable_p "$1" && "$@"
}

process_running_my_uid () {
    proc="$1"
    pgrep -u "`id -un`" "$proc" >/dev/null
}

run_unless_running () {
    prog="$1"; shift
    if ! executable_p "$prog"; then
        echo "$prog not executable" >&2
    fi
    process_running_my_uid "$prog" || "$prog" "$@"
}

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

echo "### .xsession.local.pre ..."
[ -e ~/.xsession.local.pre       ] && . ~/.xsession.local.pre
[ -e ~/.xsession.${HOST%%.*}.pre ] && . ~/.xsession.${HOST%%.*}.pre

if executable_p sawfish; then
  export KDEWM=sawfish
else
  echo "sawfish not executable :-("
fi

echo "### .xsession.local ..."
[ -e ~/.xsession.local       ] && . ~/.xsession.local
[ -e ~/.xsession.${HOST%%.*} ] && . ~/.xsession.${HOST%%.*}

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

if executable_p sawfish; then
  echo "### sawfish ..."
  sawfish &
elif executable_p gnome-session; then
  gnome-session &
else
  # oh fuck
  ( fvwm || twm ) &
fi

echo "### ~/.xsession-progs ..."
~/.xsession-progs &

# this script must not terminate, otherwise its callers assume
# the X session has finished
echo "### end of .xsession, waiting for children to exit ..."
wait
