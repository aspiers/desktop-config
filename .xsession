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

echo "### Fonts ..."
for dir in ~/.X_fonts ~/.truetype; do
  if [ -d $dir ]; then
    if xset fp+ $dir; then
      echo "Added font dir $dir"
    else
      echo "Failed to add font dir $dir !"
    fi
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

if executable_p startkde; then
  exec startkde
else
  echo "startkde not executable :-("
fi

if executable_p sawfish; then
  echo "### sawfish ..."
  sawfish &
else
  gnome-session &
fi

echo "### ~/.xsession-progs ..."
~/.xsession-progs &

# oh fuck
# exec fvwm
# exec twm


# this script must not terminate, otherwise its callers assume
# the X session has finished
echo "### end of .xsession, waiting for children to exit ..."
wait
