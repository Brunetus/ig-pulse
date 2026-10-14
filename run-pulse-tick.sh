#!/bin/zsh
# run-pulse-tick.sh — LaunchAgent entry for the ig-pulse monitor.
# com.marcus.ig-pulse fires this every 30 min while the Mac is awake.
# The monitor itself is pure API polling (zero model cost); it spawns
# run-pulse-architect.sh only when a spike fires. Kill switch: touch
# ~/Claude Code/ig-pulse/PAUSED.

export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$HOME/.local/bin:$HOME/Library/Python/3.9/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PULSE="$HOME/Claude Code/ig-pulse"
LOCK="$PULSE/state/tick.lock"

cd "$PULSE" || exit 1
mkdir -p state logs

# never overlap ticks; a stale lock (>2h) from a crashed tick is cleared
if [ -d "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null
  else
    exit 0
  fi
fi
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# monitor.py's log() already writes each line to logs/<date>.log, so stdout
# would duplicate every line — keep stderr only, so tracebacks still land here
PULSE_LOCK_HELD=1 python3 monitor.py >/dev/null 2>> "logs/$(date +%F).log"

# prune logs + consumed jobs older than 30 days, clips older than 14
find logs -name "*.log" -mtime +30 -delete 2>/dev/null
find jobs/done -name "*.json" -mtime +30 -delete 2>/dev/null
find clips -name "*.mp4" -mtime +14 -delete 2>/dev/null
exit 0
