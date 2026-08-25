#!/bin/bash
# wells_resumable_launch.sh — Run a long `wells bench run` / `wells evolve
# gate` command fully detached (survives the launching shell/session
# ending) with an auto-restarting watchdog that resumes from checkpoint
# on death instead of losing progress.
#
# Requires the command to be checkpoint/resume-capable: it must accept
# --bench-id (or a mutation_id, for evolve gate) and --resume, and must
# write a heartbeat file via --heartbeat PATH. `wells bench run` and
# `wells evolve gate` both support this (see src/wells/evolve/runner.py's
# run_bench docstring — "Fault tolerance").
#
# Usage:
#   bash scripts/wells_resumable_launch.sh \
#     --cmd "cd /path/to/repo && uv run python3 -c 'from wells.main import main; main()' evolve gate <id> --split val --profile zai --timeout 1200" \
#     --heartbeat /path/to/repo/.wells/evolve/mutations/<id>/heartbeat.json \
#     --log /path/to/repo/logs/evolve_gate_<id>.log \
#     [--max-restarts 20] [--stale-after 900] [--poll-every 60]
#
# What this actually does, and doesn't do:
#   - Launches $CMD (with " --resume" appended on restarts, never on the
#     first launch) via setsid+nohup — a genuinely new session, not just
#     a backgrounded job in the calling shell's process group, so it
#     survives that shell (or the whole Claude Code session) exiting.
#   - Polls the heartbeat file's `timestamp` field; if it goes stale
#     (no update in --stale-after seconds) or the tracked PID is no
#     longer alive, relaunches $CMD with --resume — up to --max-restarts
#     times, then gives up and leaves the log for a human to read.
#   - Exits cleanly (no more restarts) once the heartbeat reports
#     status=="complete", or $CMD's own process exits 0.
#   - Does NOT itself survive a full machine reboot — for that, register
#     it with systemd or check back after a reboot with --resume by hand.
#     Does NOT retry indefinitely on a command that's simply wrong (bad
#     args, missing corpus) — --max-restarts bounds the damage from a
#     persistently-broken command, same reasoning as any watchdog retry
#     cap.

set -u

CMD="" HEARTBEAT="" LOG_FILE="" MAX_RESTARTS=20 STALE_AFTER=900 POLL_EVERY=60

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cmd)           CMD="$2";           shift 2 ;;
        --heartbeat)     HEARTBEAT="$2";     shift 2 ;;
        --log)           LOG_FILE="$2";      shift 2 ;;
        --max-restarts)  MAX_RESTARTS="$2";  shift 2 ;;
        --stale-after)   STALE_AFTER="$2";   shift 2 ;;
        --poll-every)    POLL_EVERY="$2";    shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

[ -z "$CMD" ]       && { echo "ERROR: --cmd required" >&2; exit 2; }
[ -z "$HEARTBEAT" ] && { echo "ERROR: --heartbeat required" >&2; exit 2; }
[ -z "$LOG_FILE" ]  && { echo "ERROR: --log required" >&2; exit 2; }

mkdir -p "$(dirname "$LOG_FILE")"
WATCHDOG_LOG="${LOG_FILE%.log}_watchdog.log"

wlog() { echo "$(date '+%Y-%m-%dT%H:%M:%S') [watchdog] $*" | tee -a "$WATCHDOG_LOG"; }

hb_timestamp() {
    python3 -c "import json,sys
try:
    print(int(json.load(open('$HEARTBEAT')).get('timestamp', 0)))
except Exception:
    print(0)" 2>/dev/null || echo 0
}

hb_status() {
    python3 -c "import json,sys
try:
    print(json.load(open('$HEARTBEAT')).get('status', ''))
except Exception:
    print('')" 2>/dev/null || echo ""
}

launch_once() {
    local cmd="$1"
    # setsid: new session, detached from this watchdog's own controlling
    # terminal/process group — survives the watchdog's parent shell
    # exiting the same way the watchdog itself survives it (see below).
    setsid nohup bash -c "$cmd" < /dev/null >> "$LOG_FILE" 2>&1 &
    disown
    echo $!
}

wlog "=== wells_resumable_launch starting ==="
wlog "cmd: $CMD"
wlog "heartbeat: $HEARTBEAT  log: $LOG_FILE"
wlog "max_restarts=$MAX_RESTARTS stale_after=${STALE_AFTER}s poll_every=${POLL_EVERY}s"

RESTARTS=0
JOB_PID=$(launch_once "$CMD")
wlog "launched (attempt 1/$((MAX_RESTARTS + 1))), PID=$JOB_PID"

while true; do
    sleep "$POLL_EVERY"

    STATUS=$(hb_status)
    if [ "$STATUS" = "complete" ]; then
        wlog "heartbeat reports complete — done, no more restarts."
        exit 0
    fi

    ALIVE=false
    kill -0 "$JOB_PID" 2>/dev/null && ALIVE=true

    TS=$(hb_timestamp)
    NOW=$(date +%s)
    AGE=$((NOW - TS))

    if $ALIVE && [ "$TS" -gt 0 ] && [ "$AGE" -lt "$STALE_AFTER" ]; then
        wlog "alive, heartbeat ${AGE}s old, status=${STATUS:-unknown} — OK"
        continue
    fi

    if $ALIVE && [ "$AGE" -ge "$STALE_AFTER" ]; then
        wlog "PID $JOB_PID alive but heartbeat stale (${AGE}s > ${STALE_AFTER}s) — killing before restart"
        kill -9 "$JOB_PID" 2>/dev/null
        sleep 2
    elif ! $ALIVE; then
        wlog "PID $JOB_PID no longer alive (heartbeat age ${AGE}s, status=${STATUS:-unknown})"
    fi

    if [ "$RESTARTS" -ge "$MAX_RESTARTS" ]; then
        wlog "max restarts ($MAX_RESTARTS) reached — giving up. Check $LOG_FILE and $HEARTBEAT by hand."
        exit 1
    fi

    RESTARTS=$((RESTARTS + 1))
    wlog "restarting with --resume (attempt $((RESTARTS + 1))/$((MAX_RESTARTS + 1)))"
    JOB_PID=$(launch_once "${CMD} --resume")
    wlog "relaunched, PID=$JOB_PID"
done
