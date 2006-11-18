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
# Obtains a lock via mkdir LOCKFILE, to protect against COMMAND
# running concurrently.  Returns success (zero) iff lock was obtained.
# Cross-checks lock with process table and does the sensible thing in
# each case.
#
# N.B. Any code calling this function is responsible for clearing
# the lock when COMMAND stops!
# 
# Sample usage follows:
# 
#   clean_up () {
#     [ -d "$lock" ] && rmdir "$lock"
#   }  
#   
#   obtain_lock "$lock" "$cmd" || exit 1
#   # Signal must be trapped *after* obtaining lock, otherwise
#   # failure to obtain the lock would remove an active lock.
#   trap clean_up EXIT
#
obtain_lock () {
  if [ $# != 2 ] || [ -z "$2" ]; then
    echo "ERROR: Usage: obtain_lock LOCKFILE COMMAND" >&2
    return 1
  fi

  lock="$1"
  cmd="$2"

  if mkdir "$lock" 2>/dev/null; then
    echo "got lock $lock" >&2
    return 0
  fi

  echo "$lock already exists!"
  echo "Looking for running processes matching '$cmd' ..."
  # -f is needed since $cmd could be a shell script in which case
  # $0 would be the interpreter not $cmd itself.
  for pid in $( pgrep -f "$cmd" ); do
    [ "$pid" != $$ ] && pids="$pid $pids"
  done
  if [ -z "$pids" ]; then
    if ! [ -t 0 ]; then
      echo -n "None found; rmdir $lock manually."
      return 1
    fi
    echo -n "None found; if lock is stale, remove it now? (y/n) "
    read confirm
    case "$confirm" in
      y*|Y*)
        if rmdir "$lock"; then
          echo "Removed $lock, now re-run."
        else
          # rmdir will output an error
          return 1
        fi
        ;;
      *)
        echo "Not removing lock."
    esac
    return 1
  else
    pids="${pids% }"
    pids="${pids// /,}"
    ps -fp "$pids"
  fi
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
