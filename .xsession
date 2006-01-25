#!/bin/bash

exec 1>~/.xsession-my-errors 2>&1

. ~/.shared_env

echo -n "### X session started: "
date

executable_p () {
    which "$1" >/dev/null 2>&1
}

run_if_executable () {
    executable_p "$1" && "$@"
}

echo $DISPLAY > ~/.Xdisplay.${HOST%%.*}

echo "### Xmodmap ..."
if [ -z "$VNCDESKTOP" ]; then
  if [ -e ~/.Xmodmap.${HOST%%.*} ]; then
    . ~/.Xmodmap.${HOST%%.*}
  elif [ -e ~/.Xmodmap.local ]; then
    . ~/.Xmodmap.local
  fi
fi

echo "### Xresources ..."
[ -e ~/.Xresources/all.rdb ] && xrdb -merge ~/.Xresources/all.rdb

echo "### Fonts ..."
for dir in ~/.X_fonts ~/.truetype; do
  if [ -d $dir ]; then
    xset fp+ $dir
  fi
done
xset fp rehash

echo "### Mouse ..."
xset m 2 4

echo "### Keyboard ..."
xset r rate 180 50

echo "### .xsession.local.pre ..."
[ -e ~/.xsession.local.pre       ] && . ~/.xsession.local.pre
[ -e ~/.xsession.${HOST%%.*}.pre ] && . ~/.xsession.${HOST%%.*}.pre

if executable_p sawfish; then
  export KDEWM=sawfish
fi

echo "### .xsession.local ..."
[ -e ~/.xsession.local       ] && . ~/.xsession.local
[ -e ~/.xsession.${HOST%%.*} ] && . ~/.xsession.${HOST%%.*}

if executable_p startkde; then
  exec startkde
fi

echo "### sawfish ..."
sawfish &

echo "### ~/.xsession-progs ..."
~/.xsession-progs &

# oh fuck
# exec fvwm
# exec twm


# this script must not terminate, otherwise its callers assume
# the X session has finished
echo "### end of .xsession, waiting for children to exit ..."
wait
