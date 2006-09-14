#!/bin/sh

host=${HOST:-$HOSTNAME}
host=${host%%.*}
exec 1> ~/.xsession-my-errors.${host} 2>&1

echo -n "### .xinitrc started: "
date

exec ~/.xsession "$@"
