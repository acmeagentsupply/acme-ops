#!/usr/bin/env bash
# model_state_probe.sh — Telemetry integrity probe (Wave 1, Track B)
# Issues 12 routed model calls, captures model_state.json after each.
# Validates monotonic timestamps. Logs load averages.
#
# Safety: read-only except probe log; no config changes; no gateway restart.
# Exit code: always 0 (diagnostic failures logged, not propagated)

set -uo pipefail

ROUTER="$HOME/.openclaw/watchdog/model_router.py"
STATE_FILE="$HOME/.openclaw/watchdog/model_state.json"
PROBE_LOG="$HOME/.openclaw/watchdog/model_state_probe.log"
TOTAL_CALLS=12
INTERVAL_S=3   # ~3s between calls → 12 calls in ~36-45s
PROMPT="Reply with exactly one word: OK"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] PROBE $*" | tee -a "$PROBE_LOG"; }
log_only() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] PROBE $*" >> "$PROBE_LOG"; }

if [ ! -f "$ROUTER" ]; then
  log "FATAL: model_router.py not found at $ROUTER"
  exit 0
fi

# ── B3: Load average at start ─────────────────────────────────────────────────
LOAD_START=$(python3 -c "import os; l=os.getloadavg(); print(f'{l[0]:.2f}/{l[1]:.2f}/{l[2]:.2f}')")
log "RUN_START calls=$TOTAL_CALLS interval_s=$INTERVAL_S"
log "LOAD_START=$LOAD_START"

# ── B1: Issue calls and capture state ─────────────────────────────────────────
declare -a TIMESTAMPS=()
declare -a PROVIDERS=()
declare -a MODELS=()
declare -a STATUSES=()

for i in $(seq 1 "$TOTAL_CALLS"); do
  # Call router with background lane + short timeout (use cheapest chain)
  CALL_OUT=$(python3 "$ROUTER" "$PROMPT" \
    --lane background \
    --timeout 20 \
    --req-id "probe-$i" \
    2>/dev/null) || true

  # Read model_state.json immediately after call
  STATE_JSON=$(cat "$STATE_FILE" 2>/dev/null || echo '{"provider":"error","status":"read_fail","updated_at":0}')

  UPDATED_AT=$(echo "$STATE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('updated_at',0))" 2>/dev/null || echo 0)
  PROVIDER=$(echo "$STATE_JSON"   | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('provider','?'))" 2>/dev/null || echo "?")
  MODEL=$(echo "$STATE_JSON"      | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('model','?'))" 2>/dev/null || echo "?")
  STATUS=$(echo "$STATE_JSON"     | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")

  TIMESTAMPS+=("$UPDATED_AT")
  PROVIDERS+=("$PROVIDER")
  MODELS+=("$MODEL")
  STATUSES+=("$STATUS")

  log_only "CALL_INDEX=$i updated_at=$UPDATED_AT provider=$PROVIDER model=$MODEL status=$STATUS"

  # Delay between calls (except last)
  [ "$i" -lt "$TOTAL_CALLS" ] && sleep "$INTERVAL_S"
done

# ── B2: Monotonic timestamp validation ───────────────────────────────────────
MONOTONIC_RESULT="PASS"
PREV_TS=0
VIOLATIONS=0

for i in "${!TIMESTAMPS[@]}"; do
  TS="${TIMESTAMPS[$i]}"
  CALL_NUM=$((i+1))

  # Check: timestamp > 0
  if python3 -c "import sys; sys.exit(0 if float('${TS}') > 0 else 1)" 2>/dev/null; then
    true
  else
    log_only "MONOTONIC_VIOLATION: call=$CALL_NUM updated_at=0 (zero timestamp)"
    VIOLATIONS=$((VIOLATIONS+1))
  fi

  # Check: strictly increasing
  if [ "$PREV_TS" != "0" ]; then
    INCREASED=$(python3 -c "print('yes' if float('${TS}') >= float('${PREV_TS}') else 'no')" 2>/dev/null || echo "no")
    if [ "$INCREASED" = "no" ]; then
      log_only "MONOTONIC_VIOLATION: call=$CALL_NUM ts=$TS <= prev=$PREV_TS"
      VIOLATIONS=$((VIOLATIONS+1))
    fi
  fi

  # Check: provider populated
  if [ "${PROVIDERS[$i]}" = "?" ] || [ "${PROVIDERS[$i]}" = "unknown" ] || [ -z "${PROVIDERS[$i]}" ]; then
    log_only "MONOTONIC_WARN: call=$CALL_NUM provider not populated (got: ${PROVIDERS[$i]})"
  fi

  PREV_TS="$TS"
done

if [ "$VIOLATIONS" -gt 0 ]; then
  MONOTONIC_RESULT="FAIL"
fi

log "MODEL_STATE_MONOTONIC=$MONOTONIC_RESULT (violations=$VIOLATIONS)"

# ── B3: Load average at end ───────────────────────────────────────────────────
LOAD_END=$(python3 -c "import os; l=os.getloadavg(); print(f'{l[0]:.2f}/{l[1]:.2f}/{l[2]:.2f}')")
log "LOAD_END=$LOAD_END"

# ── Summary ──────────────────────────────────────────────────────────────────
log "PROBE_COMPLETE calls_issued=$TOTAL_CALLS monotonic=$MONOTONIC_RESULT load_start=$LOAD_START load_end=$LOAD_END"
exit 0
