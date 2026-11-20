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

PIDFILE="${LOG_FILE%.log}.pid"

launch_once() {
    local cmd="$1"
    # Remove any stale pidfile from a previous launch BEFORE starting the
    # new session. Without this, the "wait for pidfile to appear" loop
    # below sees the OLD run's leftover pidfile as already non-empty and
    # returns instantly with the PREVIOUS (dead) PID instead of the new
    # process's real one — the watchdog then tracks a PID that's already
    # dead, "discovers" it died on the very next poll, and launches a
    # SECOND replacement while the first relaunch (whose real PID was
    # never captured) keeps running unmonitored and unkilled. Reproduced
    # live on 2026-08-26 during kill_tree validation: this raced into a
    # duplicate process tree, the exact failure category this whole
    # script exists to prevent.
    rm -f "$PIDFILE"
    # Do NOT trust `echo $!` here — verified unreliable in production: a
    # real incident (2026-08-26) had `$!`-tracked PIDs the watchdog later
    # killed with `kill -9` that were still alive minutes later, resulting
    # in 5 concurrent duplicate job runs burning real API calls against
    # the same tasks simultaneously before anyone noticed. Root cause was
    # never pinned to one exact mechanism (setsid's fork-vs-exec behavior
    # depending on whether the calling shell is already a session leader
    # is the leading theory, but "the PID variable is corrupted, we don't
    # know exactly how" is disqualifying either way for something this
    # consequential).
    #
    # Instead: have the launched session write ITS OWN pid, from inside
    # itself, via $$ — read immediately before `exec` replaces the shell
    # with the real command (exec preserves the pid), so the pidfile is
    # guaranteed to name whatever process actually keeps running. No
    # capture, no ambiguity, no dependency on setsid's internal fork
    # decision.
    setsid bash -c "echo \$\$ > '$PIDFILE'; exec $cmd" < /dev/null >> "$LOG_FILE" 2>&1 &
    disown
    # Block until the pidfile actually appears (should be near-instant —
    # writing it is the literal first thing the new session does) so the
    # caller never reads a stale/prior pidfile.
    local waited=0
    while [ ! -s "$PIDFILE" ] && [ "$waited" -lt 50 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    cat "$PIDFILE" 2>/dev/null
}

wlog "=== wells_resumable_launch starting ==="
wlog "cmd: $CMD"
wlog "heartbeat: $HEARTBEAT  log: $LOG_FILE"
wlog "max_restarts=$MAX_RESTARTS stale_after=${STALE_AFTER}s poll_every=${POLL_EVERY}s"

kill_tree() {
    # Recursively SIGKILL a PID and every descendant, walked via
    # `pgrep -P` (parent-child), NOT process-group membership.
    #
    # Proven necessary (not just theoretical) by a live re-test on
    # 2026-08-26: `kill -9 -- "-$PID"` (process-group kill) left orphans
    # alive after killing the tracked setsid leader, because `uv run`
    # spawns its own child (the actual python harness process) in a NEW
    # process group of its own — a common supervisor pattern for signal
    # isolation. Process-group kill only reaches processes that stayed in
    # the leader's group; it does not reach a grandchild that left it.
    # Walking the actual parent-child tree reaches every descendant
    # regardless of what process group it's in.
    local root="$1"
    local pids
    pids=$(_collect_descendants "$root")
    # Kill children before the parent: once the parent (e.g. `uv run`) is
    # dead, an orphaned child gets reparented to init and `pgrep -P` can
    # no longer find it via this walk — so enumerate the whole tree
    # first, then kill bottom-up (reverse of the top-down walk order).
    local pid
    for pid in $(echo "$pids" | tac); do
        kill -9 "$pid" 2>/dev/null
    done
    kill -9 "$root" 2>/dev/null
}

_collect_descendants() {
    # BFS via pgrep -P, printing each descendant pid (root excluded) in
    # discovery order, one per line.
    local frontier="$1"
    local next child
    while [ -n "$frontier" ]; do
        next=""
        for pid in $frontier; do
            for child in $(pgrep -P "$pid" 2>/dev/null); do
                echo "$child"
                next="$next $child"
            done
        done
        frontier="$next"
    done
}

validate_pid() {
    # Defense in depth against whatever corrupted PID tracking in the
    # 2026-08-26 incident: refuse to proceed with a value that isn't
    # cleanly a positive integer, rather than silently `kill`ing garbage
    # (which fails silently under 2>/dev/null) and leaving the old
    # process running while a new one is also launched.
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) [ "$1" -gt 0 ] ;;
    esac
}

RESTARTS=0
JOB_PID=$(launch_once "$CMD")
if ! validate_pid "$JOB_PID"; then
    wlog "FATAL: launch_once did not produce a valid pid (got '$JOB_PID') — aborting rather than risk a duplicate."
    exit 1
fi
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
        # Kill the ENTIRE process tree rooted at $JOB_PID (see kill_tree
        # above) — not just the group. A plain `kill -9 $JOB_PID`, or
        # even a process-group kill, can leave uv run's own children (the
        # actual run_bench/gate_mutation Python process) as orphans that
        # keep running, still writing to the same checkpoint/heartbeat
        # files a newly-launched replacement would also be writing to.
        TREE_PIDS_BEFORE="$JOB_PID $(_collect_descendants "$JOB_PID")"
        kill_tree "$JOB_PID"
        # Confirm the kill actually landed — for EVERY pid in the tree,
        # not just the root — before launching a replacement. A duplicate
        # run is worse than a slightly-delayed restart. The 2026-08-26
        # incident's defining trait was exactly this: the watchdog
        # *believed* it had killed the old process and moved on without
        # ever checking the descendants.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            STILL_ALIVE=false
            for pid in $TREE_PIDS_BEFORE; do
                kill -0 "$pid" 2>/dev/null && STILL_ALIVE=true
            done
            $STILL_ALIVE || break
            sleep 1
        done
        if $STILL_ALIVE; then
            wlog "FATAL: one or more PIDs in tree ($TREE_PIDS_BEFORE) still alive 10s after kill_tree — refusing to launch a duplicate. Manual intervention needed."
            exit 1
        fi
        wlog "confirmed entire process tree ($TREE_PIDS_BEFORE) is dead."
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
    if ! validate_pid "$JOB_PID"; then
        wlog "FATAL: relaunch did not produce a valid pid (got '$JOB_PID') — aborting."
        exit 1
    fi
    wlog "relaunched, PID=$JOB_PID"
done
