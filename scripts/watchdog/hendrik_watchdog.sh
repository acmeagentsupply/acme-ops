#!/usr/bin/env bash
set -euo pipefail
LOOP_START_S=$(date +%s)

# --- Lockfile: prevent concurrent runs ---
_LOCK_DIR="$HOME/.openclaw/watchdog"
_LOCK_FILE="$_LOCK_DIR/watchdog.lock"
_LOCK_PID="$_LOCK_DIR/watchdog.pid"
mkdir -p "$_LOCK_DIR"
if [ -f "$_LOCK_FILE" ]; then
  _OLD_PID=$(cat "$_LOCK_PID" 2>/dev/null || echo "")
  if [ -n "$_OLD_PID" ] && kill -0 "$_OLD_PID" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] LOCK: instance already running (PID $_OLD_PID), exiting" >> "$_LOCK_DIR/watchdog.log"
    exit 0
  fi
  rm -f "$_LOCK_FILE" "$_LOCK_PID"
fi
echo $$ > "$_LOCK_PID"
touch "$_LOCK_FILE"
trap 'rm -f "$_LOCK_FILE" "$_LOCK_PID"' EXIT INT TERM

echo "HB $(date '+%Y-%m-%d %H:%M:%S %Z') watchdog run user=$(whoami) uid=$UID host=$(hostname)" >> /Users/AGENT/.openclaw/watchdog/heartbeat.log

# --- Configuration ---
WATCHDOG_LABEL="ai.openclaw.gateway"
WATCHDOG_PORT="18789"
WATCHDOG_AGENT="main"
WATCHDOG_TARGET="+19787606557"
STATE_DIR="$HOME/.openclaw/watchdog"
LOG_FILE="$STATE_DIR/watchdog.log"
WAIT_SECS="15"

# --- Helper Functions ---
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE" >/dev/null; }
have() { command -v "$1" >/dev/null 2>&1; }

send_msg() {
  local msg="$1"
  if openclaw channels 2>/dev/null | grep -qi "send"; then
    openclaw channels send --channel whatsapp --to "$WATCHDOG_TARGET" --text "$msg" >/dev/null 2>&1 && return 0
  fi
  openclaw channel send --channel whatsapp --to "$WATCHDOG_TARGET" --text "$msg" >/dev/null 2>&1 && return 0 || true
  openclaw send --channel whatsapp --to "$WATCHDOG_TARGET" --text "$msg" >/dev/null 2>&1 && return 0 || true
  return 0  # never fatal — send_msg must not trip set -e
}

# --- Main Logic ---

mkdir -p "$STATE_DIR"
touch "$LOG_FILE"
log "WATCHDOG start (user=$(whoami) uid=$UID host=$(hostname))"

CONFIG="$HOME/.openclaw/openclaw.json"
if [ ! -f "$CONFIG" ]; then
  log "ERROR: missing config: $CONFIG"
  send_msg "[openclaw][watchdog] CONFIG MISSING: ~/.openclaw/openclaw.json" || true
  exit 2
fi

if have jq; then
  if jq -e . "$CONFIG" >/dev/null 2>&1; then
    log "CONFIG OK (jq parse)"
  else
    log "ERROR: CONFIG BAD (jq parse failed)"
    send_msg "[openclaw][watchdog] CONFIG BAD (jq parse failed). Gateway may be down." || true
    exit 3
  fi
else
  log "WARN: jq not found; skipping strict config parse"
fi

LISTENING="no"
if have lsof; then
  lsof -nP -iTCP:"$WATCHDOG_PORT" -sTCP:LISTEN >/dev/null 2>&1 && LISTENING="yes" || true
else
  if have nc; then
    nc -z 127.0.0.1 "$WATCHDOG_PORT" >/dev/null 2>&1 && LISTENING="yes" || true
  fi
fi
log "PORT $WATCHDOG_PORT listening=$LISTENING"

PROBE_OK="unknown"
if openclaw gateway probe >/dev/null 2>&1; then
  PROBE_OK="yes"
  log "GATEWAY PROBE ok"
  # Reset debounce counter on success
  echo "0" > "$STATE_DIR/probe_fail_count.txt"
else
  PROBE_OK="no"
  log "GATEWAY PROBE failed"
fi

# --- Task C: GATEWAY_STALL detector ---
# port up + probe fail = frozen agent loop (not a crash)
OPS_EVENTS_LOG="$STATE_DIR/ops_events.log"
touch "$OPS_EVENTS_LOG"
if [ "$LISTENING" = "yes" ] && [ "$PROBE_OK" = "no" ]; then
  _STALL_LOAD=$(python3 -c "import os; a=os.getloadavg(); print(f'{a[0]:.2f},{a[1]:.2f},{a[2]:.2f}')" 2>/dev/null || echo "unknown")
  _STALL_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "{\"ts\":\"$_STALL_TS\",\"event\":\"GATEWAY_STALL\",\"port\":\"up\",\"probe\":\"timeout\",\"load\":\"$_STALL_LOAD\",\"action\":\"pending_debounce\",\"req\":\"watchdog\"}" >> "$OPS_EVENTS_LOG"
  log "GATEWAY_STALL detected: port=up probe=fail load=$_STALL_LOAD"
fi

# --- Task B: Probe debounce (N=3 consecutive failures before kickstart) ---
PROBE_DEBOUNCE_FILE="$STATE_DIR/probe_fail_count.txt"
PROBE_DEBOUNCE_N=3
_CURRENT_FAIL_COUNT=0
if [ -f "$PROBE_DEBOUNCE_FILE" ]; then
  _CURRENT_FAIL_COUNT=$(cat "$PROBE_DEBOUNCE_FILE" 2>/dev/null || echo "0")
  _CURRENT_FAIL_COUNT=$(( _CURRENT_FAIL_COUNT + 0 ))  # coerce to int
fi

if [ "$LISTENING" != "yes" ] || [ "$PROBE_OK" = "no" ]; then
  _NEW_FAIL_COUNT=$(( _CURRENT_FAIL_COUNT + 1 ))
  echo "$_NEW_FAIL_COUNT" > "$PROBE_DEBOUNCE_FILE"

  if [ "$_NEW_FAIL_COUNT" -lt "$PROBE_DEBOUNCE_N" ]; then
    log "PROBE_DEBOUNCE count=$_NEW_FAIL_COUNT action=NONE (threshold=$PROBE_DEBOUNCE_N)"
  else
    log "PROBE_DEBOUNCE count=$_NEW_FAIL_COUNT action=KICKSTART (threshold=$PROBE_DEBOUNCE_N reached)"
    # Update GATEWAY_STALL event action now that kickstart is happening
    if [ "$LISTENING" = "yes" ] && [ "$PROBE_OK" = "no" ]; then
      _STALL_TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
      echo "{\"ts\":\"$_STALL_TS\",\"event\":\"GATEWAY_STALL\",\"port\":\"up\",\"probe\":\"timeout\",\"load\":\"$_STALL_LOAD\",\"action\":\"kickstart\",\"req\":\"watchdog\"}" >> "$OPS_EVENTS_LOG"
    fi
    echo "0" > "$PROBE_DEBOUNCE_FILE"

    log "RECOVERY: kickstart launchd service: $WATCHDOG_LABEL"
    launchctl kickstart -k "gui/$UID/$WATCHDOG_LABEL" >/dev/null 2>&1 || true

    end=$(( $(date +%s) + WAIT_SECS ))
    while [ "$(date +%s)" -lt "$end" ]; do
      if have lsof && lsof -nP -iTCP:"$WATCHDOG_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        LISTENING="yes"
        break
      fi
      sleep 1
    done

    if openclaw gateway probe >/dev/null 2>&1; then
      log "RECOVERY RESULT: probe ok after kickstart"
      send_msg "[openclaw][watchdog] RECOVERED: gateway probe OK after kickstart." || true
    else
      log "RECOVERY RESULT: probe still failing after kickstart"
      ERRLOG="$HOME/.openclaw/logs/gateway.err.log"
      if [ -f "$ERRLOG" ]; then
        tail -n 25 "$ERRLOG" | sed 's/^/[watchdog][tail] /' | tee -a "$LOG_FILE" >/dev/null
      else
        log "WARN: no gateway.err.log found at $ERRLOG"
      fi
    send_msg "[openclaw][watchdog] ALERT: gateway still failing after kickstart. Check gateway.err.log." || true
    fi  # end: probe ok after kickstart check
  fi    # end: debounce threshold check
fi      # end: listening/probe fail check

# --- Stall Detector ---
STALL_LOG="$STATE_DIR/stall.log"
STALL_SEEN="$STATE_DIR/stall_seen.txt"
SESSIONS_DIR="$HOME/.openclaw/agents/main/sessions"
touch "$STALL_LOG" "$STALL_SEEN"

STALL_RESULT="unknown|-1|unknown"
if command -v python3 >/dev/null 2>&1 && [ -d "$SESSIONS_DIR" ]; then
  STALL_RESULT="$(python3 "$STATE_DIR/stall_detector.py" \
    "$SESSIONS_DIR" "$STALL_LOG" "$STALL_SEEN" \
    "45000" "90000" "$(ts)" 2>/dev/null)" || STALL_RESULT="unknown|-1|unknown"
fi

LAST_MODEL_PROVIDER="$(echo "$STALL_RESULT" | cut -d'|' -f1)"
LAST_MODEL_AGE_S="$(echo "$STALL_RESULT" | cut -d'|' -f2)"
LAST_MODEL_STATUS="$(echo "$STALL_RESULT" | cut -d'|' -f3)"

LOOP_MS=$(( ($(date +%s) - LOOP_START_S) * 1000 ))

HB="[openclaw][watchdog] HB $(ts): gateway OK (port ${WATCHDOG_PORT} listening, probe OK) loop_ms=${LOOP_MS} last_model_age_s=${LAST_MODEL_AGE_S} last_model_provider=${LAST_MODEL_PROVIDER} last_model_status=${LAST_MODEL_STATUS}"
log "HEARTBEAT: $HB"
send_msg "$HB" >/dev/null 2>&1 || true
STATUS_LOG="/Users/AGENT/.openclaw/watchdog/status.log"
TS="$(date '+%Y-%m-%d %H:%M:%S %Z')"
UID_NOW="$(id -u)"

LISTENING="no"
if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:18789 -sTCP:LISTEN >/dev/null 2>&1; then LISTENING="yes"; fi
fi

PROBE="fail"
if openclaw gateway probe >/dev/null 2>&1; then PROBE="ok"; else PROBE="fail"; fi

PROBE2="$PROBE"
if [ "$PROBE" != "ok" ]; then
  launchctl kickstart -k "gui/$UID_NOW/ai.openclaw.gateway" >/dev/null 2>&1 || true
  sleep 2
  if openclaw gateway probe >/dev/null 2>&1; then PROBE2="ok"; else PROBE2="fail"; fi
fi

MODEL_PRIMARY="unknown"
if command -v jq >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
  MODEL_PRIMARY="$(jq -r '.agents.defaults.model.primary // "unknown"' "$CONFIG" 2>/dev/null || echo "unknown")"
fi

# --- Token summary (SphinxGate) — read-only, non-blocking, <150ms ---
TOKEN_SUMMARY="tokens_total=unknown tokens_in=unknown tokens_out=unknown"
if command -v python3 >/dev/null 2>&1; then
  TOKEN_SUMMARY="$(python3 - <<'PYEOF' 2>/dev/null || echo "tokens_total=unknown tokens_in=unknown tokens_out=unknown"
import os
from datetime import datetime, timedelta
TOKENS_LOG = os.path.expanduser("~/.openclaw/metrics/tokens.log")
result = "tokens_total=unknown tokens_in=unknown tokens_out=unknown"
try:
    cutoff = datetime.now() - timedelta(hours=1)
    in_sum = out_sum = total_sum = 0
    if os.path.exists(TOKENS_LOG):
        with open(TOKENS_LOG) as f:
            tail = f.readlines()[-200:]
        for line in tail:
            p = line.strip().split(",")
            if len(p) < 8:
                continue
            try:
                row_ts = datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if row_ts < cutoff:
                continue
            try:
                in_sum    += max(0, int(p[5]))
                out_sum   += max(0, int(p[6]))
                total_sum += max(0, int(p[7]))
            except Exception:
                continue
        result = f"tokens_total={total_sum} tokens_in={in_sum} tokens_out={out_sum}"
except Exception:
    pass
print(result)
PYEOF
  )" || TOKEN_SUMMARY="tokens_total=unknown tokens_in=unknown tokens_out=unknown"
fi

# --- Silence Sentinel ---
SILENCE_SUMMARY=$(python3 "$STATE_DIR/silence_sentinel.py" 2>/dev/null || echo "silence_warn=0 silence_age_s=unknown")

# --- Compaction Budget Sentinel (A-RC-P3-001) ---
COMP_SUMMARY=$(python3 "$STATE_DIR/compaction_budget_sentinel.py" 2>/dev/null || echo "comp_storm=err comp_active=err comp_events_2h=err comp_alert=ERROR")

# --- Sentinel Protection Emitter (A-SEN-P1-001) ---
python3 "$STATE_DIR/sentinel_protection_emitter.py" >/dev/null 2>&1 || true

echo "$TS status: port18789=$LISTENING probe=$PROBE2 model_primary=$MODEL_PRIMARY $TOKEN_SUMMARY $SILENCE_SUMMARY $COMP_SUMMARY" >> "$STATUS_LOG"


log "WATCHDOG done"
exit 0
