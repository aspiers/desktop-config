#!/bin/sh

# Useful process handling functions.
#
# Use via:
#
#   . $ZDOTDIR/lib/libproc.sh

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

# Checks for a named process running as me
process_running_my_uid () {
    proc="$1"
    pgrep -u "`id -un`" "$proc" >/dev/null
}

# Runs a named process unless it's already running as me
run_unless_running () {
    prog="$1"; shift
    if ! executable_p "$prog"; then
        echo "$prog not executable" >&2
    fi
    process_running_my_uid "$prog" || "$prog" "$@"
}

# obtain_lock LOCKFILE COMMAND
# 
# Tries once to obtains a lock via ln -s LOCKINFO LOCKFILE, to protect
# against COMMAND running concurrently.  Returns success (zero) iff
# lock was obtained.  Does not block, although may prompt for user
# guidance if running interactively - cross-checks lock with process
# table and does the sensible thing in each case.
#
# Assumes that /proc/$pid/cmdline of the resulting command will
# contain the first word of COMMAND.  This is so it can check whether
# the lock has become stale and another process has assumed $pid.  If
# this assumption is broken, and a second attempt is made to obtain
# the lock, it will incorrectly treat the valid lock as a stale one,
# and attempt to remove it.
#
# N.B. Any code calling this function is responsible for clearing the
# lock when COMMAND stops!
# 
# Sample usage follows:
# 
#   clean_up () {
#     [ -L "$lock" ] && rm "$lock"
#   }
#   
#   obtain_lock "$lock" "$cmd" || exit 1
#   # Signal must be trapped *after* obtaining lock, otherwise failure
#   # to obtain the lock would remove an active lock.  This leaves a
#   # tiny window within which unexpected failure of the script could
#   # leave a stale lock.
#   trap clean_up EXIT
#
obtain_lock () {
    if [ $# != 2 ] || [ -z "$2" ]; then
        echo "ERROR: Usage: obtain_lock LOCKFILE COMMAND" >&2
        return 1
    fi

    local lock="$1"
    local required_lockinfo="$USER@$HOSTNAME.$$:`date +%s`"
    local cmd="$2"

    if ln -s "$required_lockinfo" "$lock" 2>/dev/null; then
        #echo "got lock $lock" >&2
        return 0
    fi

    #echo "obtain_lock: $lock already exists!"

    local lockinfo=$( file -b "$lock" )
    case "$lockinfo" in
      *symbolic\ link\ to*$USER@$HOSTNAME.*[0-9]*:*[0-9]*)
        : ;;
      *)
        echo "obtain_lock: Couldn't parse lock info [$lockinfo] from $lock" >&2
        echo "*symbolic\ link\ to*$USER@$HOSTNAME.*[0-9])"
        return 1 ;;
    esac

    # or: sed 's/^symbolic link to `\(.\+\)'\''$/\1/' )
    #' # emacs font-lock hack
    local pid="${lockinfo%:*}"
    pid="${pid#*\`$USER@$HOSTNAME.}"
    # strip domain name from hostname, if any
    pid="${pid##[a-z]*.}"
    case "$pid" in
      [0-9]*[0-9])
        : ;;
      *)
        echo "obtain_lock: Couldn't extract PID from [$lockinfo]; stripped to [$pid]" >&2
        return 1 ;;
    esac

    local interactive=y
    # Require both STDIN and STDOUT to be ttys to run interactively.
    if ! [ -t 0 ] || ! [ -t 1 ]; then
        interactive=
    fi

    if [ -d "/proc/$pid" ]; then
        running="$(</proc/$pid/cmdline)"
        cmd0="${cmd%% *}"
        case "$running" in
          *$cmd0*)
            echo "obtain_lock: $cmd0 already running" >&2
            ps -fp "$pid" >&2
            return 1
            ;;
        esac
    fi

    # /proc/$pid missing - if we got here, we found a stale lock.

    if [ -z "$interactive" ]; then
        # Should be robust enough now to automatically remove stale locks.
        echo "obtain_lock: removing stale lock $lock for pid $pid"
        if ! rm "$lock"; then
            echo "obtain_lock: rm of stale $lock failed" >&2
            return 1
        fi
        if ln -s "$required_lockinfo" "$lock" 2>/dev/null; then
            #echo "got lock $lock" >&2
            return 0
        else
            echo "obtain_lock: second attempt to claim lock $lock failed" >&2
            return 1
        fi
    fi

    # We're interactive.
    echo -n "obtain_lock: lock $lock for pid $pid seems stale, remove it now? (y/n) "
    local confirm
    read confirm
    case "$confirm" in
      y*|Y*)
        if rm "$lock"; then
            echo "obtain_lock: Removed stale lock $lock"
            if ln -s "$required_lockinfo" "$lock" 2>/dev/null; then
                #echo "got lock $lock" >&2
                return 0
            fi
        else
            echo "obtain_lock: rm of stale $lock failed" >&2
            return 1
        fi
        ;;
      *)
        echo "obtain_lock: Not removing lock."
    esac
    return 1
}

# Calculate the time a process was started.  Broken; use
# ttrack-age which is a cleaner, more portable, userspace-based
# solution anyway.
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

# If we are running with -e flag, we can watch out for commands failing
# via:
#
# trap check_success_on_exit EXIT
#
# [ do stuff ]
#
# success=hooray # keep exit handler happy

check_success_on_exit () {
    if [ "$success" != 'hooray' ]; then
        cat <<EOF

WARNING: script terminated prematurely!  The last command failed;
please carefully examine the output immediately above this message for
clues as to what went wrong.

EOF
    fi
}

