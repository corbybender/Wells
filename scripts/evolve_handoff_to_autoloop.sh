#!/bin/bash
# evolve_handoff_to_autoloop.sh — one-time bridge: wait for the currently
# running manually-launched `evolve gate` to finish, resolve it
# (promote/reject per its own recommendation), then hand off to the
# autonomous loop for the remaining budget. Run once, detached (setsid),
# so it survives the launching session ending — same reasoning as
# wells_resumable_launch.sh.
set -u

REPO=/home/corbybender/Projects/seacs
WATCHDOG_PID="$1"           # PID of the running wells_resumable_launch.sh for the manual gate
MUTATION_ID="$2"            # its mutation id
MAX_CYCLES="${3:-10}"
MAX_DAYS="${4:-3}"

LOG="$REPO/logs/evolve_handoff.log"
mkdir -p "$REPO/logs"
hlog() { echo "$(date '+%Y-%m-%dT%H:%M:%S') [handoff] $*" | tee -a "$LOG"; }

hlog "=== handoff starting: waiting for watchdog PID $WATCHDOG_PID (mutation $MUTATION_ID) ==="
while kill -0 "$WATCHDOG_PID" 2>/dev/null; do
    sleep 30
done
hlog "watchdog PID $WATCHDOG_PID exited — manual gate finished."

cd "$REPO" || { hlog "FATAL: cannot cd to $REPO"; exit 1; }

RESULT=$(uv run python3 -c "from wells.main import main; main()" evolve show "$MUTATION_ID" 2>&1)
REC=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('recommendation',''))" 2>/dev/null)
hlog "recommendation: ${REC:-unknown}"

if [ "$REC" = "promote" ]; then
    hlog "promoting $MUTATION_ID ..."
    uv run python3 -c "from wells.main import main; main()" evolve promote "$MUTATION_ID" 2>&1 | tee -a "$LOG"
    git add AGENT.md
    git commit -m "evolve: promote $MUTATION_ID (handoff from manual gate)" 2>&1 | tee -a "$LOG"
    git push origin HEAD 2>&1 | tee -a "$LOG"
elif [ "$REC" = "reject" ]; then
    hlog "rejecting $MUTATION_ID ..."
    uv run python3 -c "from wells.main import main; main()" evolve reject "$MUTATION_ID" 2>&1 | tee -a "$LOG"
else
    hlog "no clear recommendation (${REC:-empty}) — leaving mutation as-is, not blocking handoff."
fi

hlog "=== launching autoloop: max_cycles=$MAX_CYCLES max_days=$MAX_DAYS ==="
HB="$REPO/.wells/evolve/autoloop_heartbeat.json"
CMD="uv run python3 -c 'from wells.main import main; main()' evolve autoloop --max-cycles $MAX_CYCLES --max-days $MAX_DAYS --split val --profile zai --timeout 1200 --heartbeat $HB"
setsid nohup bash "$REPO/scripts/wells_resumable_launch.sh" \
    --cmd "$CMD" \
    --heartbeat "$HB" \
    --log "$REPO/logs/autoloop.log" \
    --max-restarts 40 \
    --stale-after 300 \
    --poll-every 60 \
    < /dev/null > "$REPO/logs/autoloop_wrapper.log" 2>&1 &
disown
sleep 2
AUTOLOOP_WD=$(pgrep -f "wells_resumable_launch.sh --cmd.*evolve autoloop" | head -1)
hlog "autoloop watchdog PID: ${AUTOLOOP_WD:-FAILED TO START}"
hlog "=== handoff complete ==="
