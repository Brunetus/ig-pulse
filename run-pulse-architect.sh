#!/bin/zsh
# run-pulse-architect.sh <job.json> — spawn one pulse-architect session for a
# fired spike job. Standalone launcher in the run-viral-scan.sh mold: this is
# deliberately NOT an account wrapper — do not source run-daily-core.sh.
# Serialization: one architect at a time (lock dir), and we yield to any
# running daily/scan/worker session before starting (Photos-wedge + ledger
# safety). If still contended after the wait, the job is re-queued for the
# next monitor tick instead of colliding.

export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$HOME/.local/bin:$HOME/Library/Python/3.9/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PULSE="$HOME/Claude Code/ig-pulse"
JOB="$1"
NOTIF_TITLE="ig-pulse"
LOCK="$PULSE/state/architect.lock"

cd "$PULSE" || exit 1
[ -f "$JOB" ] || { echo "$(date): job file missing: $JOB"; exit 1; }
JOB_ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$JOB" 2>/dev/null)
[ -n "$JOB_ID" ] || { echo "$(date): unreadable job: $JOB"; exit 1; }
PERMALINK=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('permalink') or '')" "$JOB" 2>/dev/null)

# notify <message> [sound] — clickable via terminal-notifier (click opens the
# spiking reel); falls back to osascript with the link in the text (Marcus,
# 2026-07-28: notifications must carry the reel link so he can test it)
notify() {
  local msg="$1" sound="$2" short="${PERMALINK#https://www.}"
  if command -v terminal-notifier >/dev/null 2>&1 && [ -n "$PERMALINK" ]; then
    terminal-notifier -title "$NOTIF_TITLE" -message "$msg — click to open reel" \
      -open "$PERMALINK" ${sound:+-sound "$sound"} 2>/dev/null && return
  fi
  osascript -e "display notification \"$msg — ${short:-no link}\" with title \"$NOTIF_TITLE\"${sound:+ sound name \"$sound\"}" 2>/dev/null
}

requeue() {
  python3 - "$JOB" <<'EOF'
import json, sys
p = sys.argv[1]
j = json.load(open(p))
j["status"] = "queued"
j.pop("spawned_at", None)
json.dump(j, open(p, "w"), indent=1)
EOF
  echo "$(date): $1 — job $JOB_ID re-queued for next tick"
  exit 0
}

# ---- kill switch: honor PAUSED at every stage, not just in the monitor ----
[ -f "$PULSE/PAUSED" ] && requeue "PAUSED"

# ---- yield to dailies / bank scan / another pulse architect (max 45 min) ----
for i in {1..45}; do
  [ -f "$PULSE/PAUSED" ] && requeue "PAUSED mid-yield"
  BUSY=$(pgrep -f "run-daily.sh|run-daily-core.sh|BANK.md|run-viral-scan.sh" | head -1)
  [ -z "$BUSY" ] && [ ! -d "$LOCK" ] && break
  [ $i -eq 45 ] && requeue "still contended after 45 min"
  sleep 60
done
mkdir "$LOCK" 2>/dev/null || requeue "lost lock race"
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
[ -f "$PULSE/PAUSED" ] && requeue "PAUSED before session start"

# ---- Anthropic API pre-flight (short: a spike is time-sensitive; the job
# re-queues rather than waiting hours) ----
curl -s --max-time 10 -o /dev/null https://api.anthropic.com/ || {
  sleep 120
  curl -s --max-time 10 -o /dev/null https://api.anthropic.com/ || requeue "Anthropic API unreachable"
}

echo "===== pulse architect start job=$JOB_ID $(date) ====="
DONE="$PULSE/jobs/done/$JOB_ID.json"
rm -f "$DONE"

# ---- Photos export-health probe (mirrors run-daily-core.sh probe_photos_export;
# probes the newest ARCHIVE row — type-12 assets are already filtered there).
# Fresh per run: the dailies' 9 AM health file is stale by the afternoon. ----
IMAB="$HOME/Claude Code/imabgold-ig"
HEALTH="$PULSE/state/photos-export-health.txt"
UUID=$(head -1 "$IMAB/routine/photos-archive.tsv" 2>/dev/null | cut -f1)
if [ -z "$UUID" ]; then
  echo "UNKNOWN $(date +%H:%M:%S) no catalogued video to probe" > "$HEALTH"
elif osascript -e 'on run argv' \
     -e 'with timeout of 25 seconds' \
     -e 'tell application "Photos" to get filename of media item id (item 1 of argv)' \
     -e 'end timeout' -e 'end run' "$UUID/L0/001" > /dev/null 2>&1; then
  echo "OK $(date +%H:%M:%S) media-item queries answering" > "$HEALTH"
else
  echo "STALLED $(date +%H:%M:%S) media-item query timed out; no export lane this run - use on-disk footage and file pulse-export-requests" > "$HEALTH"
fi
echo "photos-export-health: $(cat "$HEALTH")"

# Model pinned to plain opus (NOT the drifting default, NOT opus[1m] — the 1M
# window bills a premium this never needs). Posting tools are not granted;
# delivery is outbox-only and this launcher does the iCloud move.
claude -p "You are the PULSE ARCHITECT. Read the file '$PULSE/PULSE.md' and execute it exactly for the job file '$JOB'. Your cwd is '$PULSE'. Today's date: $(date +%F)." \
  --permission-mode acceptEdits \
  --allowedTools "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Task,Agent,Skill,ToolSearch" \
  --model opus
STATUS=$?
echo "===== session exit $STATUS $(date) ====="

# ---- verdict is the done-file, not the exit code ----
if [ ! -f "$DONE" ]; then
  notify "job $JOB_ID died without a verdict - check ig-pulse/logs" "Basso"
  python3 - "$JOB" <<'EOF' 2>/dev/null
import json, sys
p = sys.argv[1]
j = json.load(open(p))
j["status"] = "failed"
json.dump(j, open(p, "w"), indent=1)
EOF
  exit 1
fi

VERDICT=$(python3 -c "import json,sys; j=json.load(open(sys.argv[1])); print(j.get('verdict',''), '|', j.get('concept') or j.get('reason',''))" "$DONE")

# ---- iCloud hand-off (launcher-side, mirrors run-daily-core.sh move_outbox) ----
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/ig-imabgold"
STAGED=0
if [ -d "$PULSE/outbox" ]; then
  mkdir -p "$ICLOUD"
  for f in "$PULSE/outbox"/*(N); do
    if mv "$f" "$ICLOUD/"; then
      echo "icloud-staged: ${f:t}"
      STAGED=$((STAGED+1))
    else
      echo "icloud-stage FAILED: ${f:t}"
      osascript -e "display notification \"iCloud stage FAILED for ${f:t}\" with title \"$NOTIF_TITLE\" sound name \"Basso\"" 2>/dev/null
    fi
  done
fi

case "$VERDICT" in
  SHIP*)
    notify "Fired reel STAGED ($STAGED files to iCloud): ${VERDICT#SHIP | }" "Glass" ;;
  DECLINE*)
    notify "Spike declined: ${VERDICT#DECLINE | }" ;;
esac
echo "===== done job=$JOB_ID verdict=$VERDICT $(date) ====="
exit 0
