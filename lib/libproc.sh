#!/bin/sh

# useful process handling functions

executable_p () {
    which "$1" >/dev/null 2>&1
}

run_if_executable () {
    if executable_p "$1"; then
        "$@"
    else
        echo "$1 not executable; skipping $*"
    fi
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
