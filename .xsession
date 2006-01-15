exec 1>~/.xsession-errors 2>&1

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

xset m 2 4
xset r rate 180 50


if executable_p sawfish; then
  export KDEWM=sawfish
fi

[ -e ~/.xsession.local       ] && . ~/.xsession.local
[ -e ~/.xsession.${HOST%%.*} ] && . ~/.xsession.${HOST%%.*}

if executable_p startkde; then
  exec startkde
fi

sawfish &

~/.xsession-progs &

# oh fuck
# exec fvwm
# exec twm

# this script must not terminate, otherwise its callers assume
# the X session has finished
wait
