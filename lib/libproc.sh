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

process_starttime () {
    echo "process_starttime broken!" >&2
    return 1
    pid="$1"
    boot_time=$( awk '/btime/ {print $2}' /proc/stat )
    jiffies_since_boot=$( awk '{print $22}' /proc/$pid/stat )
    # this probably doesn't work
    hertz=$(
        echo -e '#include <asm/param.h>\ngiveittome HZ' | \
            gcc -I/lib/modules/`uname -r`/build/include -D__KERNEL__ -E - | \
            awk '/giveittome/ {print $2}'
    )
    hertz=100
    starttime=$(( boot_time + jiffies_since_boot/hertz ))
    #echo "btime $boot_time jif $jiffies_since_boot hz $hertz"
    return $starttime
}
