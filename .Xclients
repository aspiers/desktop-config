#!/bin/sh

host=${HOST:-$HOSTNAME}
host=${host%%.*}
exec ~/.xsession "$@" >~/.Xclients-errors.${host} 2>&1
