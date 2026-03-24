#!/usr/bin/env bash
# hendrik_watchdog.sh — v2.1 (Probe Tuning Sprint 2026-03-12)
# Probe hierarchy: HTTP (primary) → port/process → WebSocket (capability signal only)
# States: HEALTHY / DEGRADED / HARD_DOWN
# HARD_DOWN: only from HTTP + port + process failures — WS never triggers restart
# SIGTERM requires 8+ hard failures + 2-of-3 signal confirmation + 900s cooldown.
# Reason codes: HTTP_OK HTTP_SLOW HTTP_DOWN PORT_DOWN PROCESS_MISSING WS_OK WS_SLOW WS_DOWN
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
WATCHDOG_TARGET="+19787606557"
STATE_DIR="$HOME/.openclaw/watchdog"
LOG_FILE="$STATE_DIR/watchdog.log"
GATEWAY_URL="http://127.0.0.1:${WATCHDOG_PORT}/"

# --- Probe Thresholds ---
# HTTP (primary — based on measured p95 ~150ms, hard ceiling 3s)
HTTP_SOFT_TIMEOUT_S=1      # >1s = DEGRADED signal
HTTP_HARD_TIMEOUT_S=3      # >3s or fail = hard failure signal
# WebSocket (secondary capability signal only — never triggers restart)
WS_TIMEOUT_S=15            # generous; WS is informational only
# Failure counters
SOFT_FAIL_THRESHOLD=5      # DEGRADED cycles before sending advisory
HARD_FAIL_THRESHOLD=8      # HARD_DOWN cycles before recovery action
# SOFT_FAIL_THRESHOLD and HARD_FAIL_THRESHOLD must remain present for RadCheck RC_CFG_007/RC_WD_004

# --- Cooldown ---
RESTART_COOLDOWN_SECONDS=900
COOLDOWN_FILE="$STATE_DIR/restart_cooldown.txt"

# --- State files ---
SOFT_FAIL_FILE="$STATE_DIR/soft_fail_count.txt"
HARD_FAIL_FILE="$STATE_DIR/hard_fail_count.txt"
OPS_EVENTS_LOG="$STATE_DIR/ops_events.log"

# --- Helper Functions ---
ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE" >/dev/null; }
have() { command -v "$1" >/dev/null 2>&1; }

ms_since() { echo $(( ($(date +%s) - $1) * 1000 )); }

OC_OPS_LOG="$HOME/.openclaw/ops/ops_events.log"
emit_oc_event() {
  local event_type="$1" severity="$2" message="$3"
  local ts_iso; ts_iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  mkdir -p "$(dirname "$OC_OPS_LOG")"
  printf '{"timestamp":"%s","component":"watchdog","event_type":"%s","severity":"%s","layer":2,"reliability_impact":"%s","message":"%s"}\n' \
    "$ts_iso" "$event_type" "$severity" \
    "$([ "$severity" = "warn" ] || [ "$severity" = "error" ] && echo "negative" || echo "neutral")" \
    "$message" >> "$OC_OPS_LOG" 2>/dev/null || true
}

send_msg() {
  local msg="$1"
  openclaw channels send --channel whatsapp --to "$WATCHDOG_TARGET" --text "$msg" >/dev/null 2>&1 && return 0 || true
  openclaw channel send --channel whatsapp --to "$WATCHDOG_TARGET" --text "$msg" >/dev/null 2>&1 && return 0 || true
  return 0
}

read_counter() {
  local f="$1" val=0
  [ -f "$f" ] && val=$(cat "$f" 2>/dev/null || echo "0")
  echo $(( val + 0 ))
}
write_counter() { echo "$2" > "$1"; }
reset_counter() { echo "0" > "$1"; }

cooldown_active() {
  [ -f "$COOLDOWN_FILE" ] || return 1
  local last_restart; last_restart=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo "0")
  local elapsed=$(( $(date +%s) - last_restart ))
  [ "$elapsed" -lt "$RESTART_COOLDOWN_SECONDS" ] && return 0 || return 1
}
stamp_cooldown() { date +%s > "$COOLDOWN_FILE"; }

# --- Main Logic ---
mkdir -p "$STATE_DIR"
touch "$LOG_FILE" "$OPS_EVENTS_LOG"
log "WATCHDOG start (user=$(whoami) uid=$UID host=$(hostname))"

CONFIG="$HOME/.openclaw/openclaw.json"
if [ ! -f "$CONFIG" ]; then
  log "ERROR: missing config: $CONFIG"
  send_msg "[openclaw][watchdog] CONFIG MISSING: ~/.openclaw/openclaw.json" || true
  exit 2
fi
if have jq; then
  jq -e . "$CONFIG" >/dev/null 2>&1 && log "CONFIG OK (jq parse)" || { log "ERROR: CONFIG BAD"; exit 3; }
fi

# ─── PRIMARY SIGNAL 1: HTTP probe ─────────────────────────────────────────────
# Fast (~100ms measured), no Node.js startup. Primary health gate.
HTTP_REASON="HTTP_UNKNOWN"
HTTP_MS=0
HTTP_OK="no"
_HTTP_T0=$(date +%s)
_HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  --max-time "$HTTP_HARD_TIMEOUT_S" \
  --connect-timeout 2 \
  "$GATEWAY_URL" 2>/dev/null || echo "000")
HTTP_MS=$(ms_since $_HTTP_T0)

if [ "$_HTTP_CODE" = "200" ] || [ "$_HTTP_CODE" = "401" ] || [ "$_HTTP_CODE" = "403" ]; then
  HTTP_OK="yes"
  if [ "$HTTP_MS" -le $(( HTTP_SOFT_TIMEOUT_S * 1000 )) ]; then
    HTTP_REASON="HTTP_OK"
  else
    HTTP_REASON="HTTP_SLOW"
  fi
else
  HTTP_REASON="HTTP_DOWN"
fi
log "HTTP probe: code=$_HTTP_CODE ms=$HTTP_MS reason=$HTTP_REASON"

# ─── PRIMARY SIGNAL 2: Port check ─────────────────────────────────────────────
PORT_UP="no"
if have lsof; then
  lsof -nP -iTCP:"$WATCHDOG_PORT" -sTCP:LISTEN >/dev/null 2>&1 && PORT_UP="yes" || true
elif have nc; then
  nc -z 127.0.0.1 "$WATCHDOG_PORT" >/dev/null 2>&1 && PORT_UP="yes" || true
fi
PORT_REASON=$( [ "$PORT_UP" = "yes" ] && echo "PORT_OK" || echo "PORT_DOWN" )
log "PORT $WATCHDOG_PORT listening=$PORT_UP reason=$PORT_REASON"

# ─── PRIMARY SIGNAL 3: Process check ──────────────────────────────────────────
PROCESS_UP="no"
GW_PID=$(launchctl print "gui/$UID/$WATCHDOG_LABEL" 2>/dev/null | grep '^\s*pid' | awk '{print $3}' | head -1 || echo "")
if [ -n "$GW_PID" ] && kill -0 "$GW_PID" 2>/dev/null; then
  PROCESS_UP="yes"
fi
PROCESS_REASON=$( [ "$PROCESS_UP" = "yes" ] && echo "PROCESS_OK" || echo "PROCESS_MISSING" )
log "PROCESS pid=${GW_PID:-none} alive=$PROCESS_UP reason=$PROCESS_REASON"

# ─── SECONDARY SIGNAL: WebSocket capability probe (informational only) ─────────
# Runs async in background — result logged but NEVER triggers restart or HARD_DOWN.
WS_REASON="WS_SKIP"
WS_MS=0
_WS_T0=$(date +%s)
if timeout "$WS_TIMEOUT_S" openclaw gateway probe >/dev/null 2>&1; then
  WS_MS=$(ms_since $_WS_T0)
  WS_REASON="WS_OK"
  [ "$WS_MS" -gt 3000 ] && WS_REASON="WS_SLOW"
else
  WS_MS=$(ms_since $_WS_T0)
  WS_REASON="WS_DOWN"
fi
log "WS probe (capability signal only): ms=$WS_MS reason=$WS_REASON"

# ─── GATEWAY STATE CLASSIFICATION ─────────────────────────────────────────────
# Only HTTP + port + process count toward HARD_DOWN.
# WS is capability signal — contributes to DEGRADED at most.
FAILURE_SIGNALS=0
[ "$HTTP_OK" = "no" ]      && FAILURE_SIGNALS=$(( FAILURE_SIGNALS + 1 ))
[ "$PORT_UP" = "no" ]      && FAILURE_SIGNALS=$(( FAILURE_SIGNALS + 1 ))
[ "$PROCESS_UP" = "no" ]   && FAILURE_SIGNALS=$(( FAILURE_SIGNALS + 1 ))

GATEWAY_STATE="HEALTHY"
GATEWAY_REASON="$HTTP_REASON"

if [ "$FAILURE_SIGNALS" -ge 2 ]; then
  GATEWAY_STATE="HARD_DOWN"
  GATEWAY_REASON="${HTTP_REASON}+${PORT_REASON}+${PROCESS_REASON}"
elif [ "$HTTP_OK" = "no" ] || [ "$PORT_UP" = "no" ] || [ "$PROCESS_UP" = "no" ]; then
  # Single primary signal failure — degraded, not hard down
  GATEWAY_STATE="DEGRADED"
  GATEWAY_REASON="${HTTP_REASON}+${PORT_REASON}+${PROCESS_REASON}"
elif [ "$HTTP_REASON" = "HTTP_SLOW" ]; then
  GATEWAY_STATE="DEGRADED"
  GATEWAY_REASON="HTTP_SLOW"
elif [ "$WS_REASON" = "WS_SLOW" ] || [ "$WS_REASON" = "WS_DOWN" ]; then
  # WS issue only — gateway is DEGRADED capability-wise, still operationally healthy
  GATEWAY_STATE="DEGRADED"
  GATEWAY_REASON="$WS_REASON"
fi

log "GATEWAY_STATE=$GATEWAY_STATE reason=$GATEWAY_REASON signals=$FAILURE_SIGNALS"

# ─── STATE-BASED COUNTER MANAGEMENT ──────────────────────────────────────────
SOFT_COUNT=$(read_counter "$SOFT_FAIL_FILE")
HARD_COUNT=$(read_counter "$HARD_FAIL_FILE")
_LOAD=$(python3 -c "import os; a=os.getloadavg(); print(f'{a[0]:.2f},{a[1]:.2f},{a[2]:.2f}')" 2>/dev/null || echo "unknown")

if [ "$GATEWAY_STATE" = "HEALTHY" ]; then
  reset_counter "$SOFT_FAIL_FILE"
  reset_counter "$HARD_FAIL_FILE"
  log "HEALTHY — all primary signals OK (http=${HTTP_MS}ms ws=${WS_MS}ms)"

elif [ "$GATEWAY_STATE" = "DEGRADED" ]; then
  SOFT_COUNT=$(( SOFT_COUNT + 1 ))
  write_counter "$SOFT_FAIL_FILE" "$SOFT_COUNT"
  reset_counter "$HARD_FAIL_FILE"
  log "DEGRADED: reason=$GATEWAY_REASON soft_count=$SOFT_COUNT/$SOFT_FAIL_THRESHOLD load=$_LOAD"
  _TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "{\"ts\":\"$_TS\",\"event\":\"GATEWAY_DEGRADED\",\"reason\":\"$GATEWAY_REASON\",\"soft_count\":$SOFT_COUNT,\"http_ms\":$HTTP_MS,\"ws_ms\":$WS_MS,\"load\":\"$_LOAD\",\"action\":\"warn_only\"}" >> "$OPS_EVENTS_LOG"
  emit_oc_event "gateway_degraded" "warn" "reason=$GATEWAY_REASON soft_count=$SOFT_COUNT http_ms=${HTTP_MS} ws_ms=${WS_MS}"
  if [ "$SOFT_COUNT" -ge "$SOFT_FAIL_THRESHOLD" ]; then
    log "DEGRADED threshold reached ($SOFT_COUNT) — advisory only, NO restart"
    send_msg "[openclaw][watchdog] ADVISORY: gateway DEGRADED ($SOFT_COUNT cycles, reason=$GATEWAY_REASON). Port+process healthy. Monitoring." || true
  fi

elif [ "$GATEWAY_STATE" = "HARD_DOWN" ]; then
  HARD_COUNT=$(( HARD_COUNT + 1 ))
  write_counter "$HARD_FAIL_FILE" "$HARD_COUNT"
  log "HARD_DOWN: reason=$GATEWAY_REASON hard_count=$HARD_COUNT/$HARD_FAIL_THRESHOLD signals=$FAILURE_SIGNALS load=$_LOAD"
  _TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "{\"ts\":\"$_TS\",\"event\":\"GATEWAY_HARD_DOWN\",\"reason\":\"$GATEWAY_REASON\",\"signals\":$FAILURE_SIGNALS,\"hard_count\":$HARD_COUNT,\"load\":\"$_LOAD\"}" >> "$OPS_EVENTS_LOG"
  emit_oc_event "gateway_hard_down" "error" "reason=$GATEWAY_REASON signals=$FAILURE_SIGNALS hard_count=$HARD_COUNT"

  if [ "$HARD_COUNT" -ge "$HARD_FAIL_THRESHOLD" ]; then
    log "HARD_DOWN threshold reached ($HARD_COUNT/$HARD_FAIL_THRESHOLD) — evaluating recovery"

    if cooldown_active; then
      _LAST=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo "0")
      _ELAPSED=$(( $(date +%s) - _LAST ))
      log "COOLDOWN active: ${_ELAPSED}s/${RESTART_COOLDOWN_SECONDS}s — skipping destructive action"
      send_msg "[openclaw][watchdog] ALERT: HARD_DOWN but cooldown active (${_ELAPSED}s/${RESTART_COOLDOWN_SECONDS}s, reason=$GATEWAY_REASON). Manual check needed." || true
    else
      send_msg "[openclaw][watchdog] ALERT: HARD_DOWN confirmed (${FAILURE_SIGNALS}/3 signals, ${HARD_COUNT} failures, reason=$GATEWAY_REASON). Attempting SIGUSR1." || true
      log "RECOVERY stage 2: sending SIGUSR1 (hot reload)"
      if [ -n "$GW_PID" ] && kill -0 "$GW_PID" 2>/dev/null; then
        kill -USR1 "$GW_PID" >/dev/null 2>&1 || true
        log "SIGUSR1 sent to PID $GW_PID"
      else
        log "RECOVERY stage 2: PID unavailable, using gentle kickstart"
        launchctl kickstart "gui/$UID/$WATCHDOG_LABEL" >/dev/null 2>&1 || true
      fi
      sleep 5

      # Recheck with HTTP (fast, reliable) not WS
      _RC=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 --connect-timeout 2 "$GATEWAY_URL" 2>/dev/null || echo "000")
      if [ "$_RC" = "200" ] || [ "$_RC" = "401" ] || [ "$_RC" = "403" ]; then
        log "RECOVERY stage 2 SUCCESS: HTTP $RC ok after SIGUSR1"
        send_msg "[openclaw][watchdog] RECOVERED: HTTP probe OK after SIGUSR1 (code=$_RC)." || true
        reset_counter "$HARD_FAIL_FILE"
      else
        log "RECOVERY stage 2 FAILED: HTTP still failing (code=$_RC) after SIGUSR1"
        log "RECOVERY stage 3: SIGTERM last resort — cooldown will apply"
        send_msg "[openclaw][watchdog] ALERT: SIGUSR1 failed (HTTP code=$_RC). Issuing SIGTERM. Cooldown ${RESTART_COOLDOWN_SECONDS}s applies." || true
        stamp_cooldown
        launchctl kickstart -k "gui/$UID/$WATCHDOG_LABEL" >/dev/null 2>&1 || true
        reset_counter "$HARD_FAIL_FILE"
        log "RECOVERY stage 3: SIGTERM issued, cooldown stamped"
      fi
    fi
  else
    log "HARD_DOWN count $HARD_COUNT below threshold $HARD_FAIL_THRESHOLD — monitoring, no action"
  fi
fi

# ─── Stall Detector ───────────────────────────────────────────────────────────
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

if [ "$LAST_MODEL_STATUS" = "stall" ] || { [ "$LAST_MODEL_AGE_S" != "-1" ] && [ "$LAST_MODEL_AGE_S" -gt 90000 ] 2>/dev/null; }; then
  emit_oc_event "agent_stall_detected" "warn" "agent stall age_s=$LAST_MODEL_AGE_S provider=$LAST_MODEL_PROVIDER"
fi

# ─── Token Summary ────────────────────────────────────────────────────────────
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
            if len(p) < 8: continue
            try:
                row_ts = datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S")
            except Exception: continue
            if row_ts < cutoff: continue
            try:
                in_sum    += max(0, int(p[5]))
                out_sum   += max(0, int(p[6]))
                total_sum += max(0, int(p[7]))
            except Exception: continue
        result = f"tokens_total={total_sum} tokens_in={in_sum} tokens_out={out_sum}"
except Exception:
    pass
print(result)
PYEOF
  )" || TOKEN_SUMMARY="tokens_total=unknown tokens_in=unknown tokens_out=unknown"
fi

# ─── Silence Sentinel + Compaction ───────────────────────────────────────────
SILENCE_SUMMARY=$(python3 "$STATE_DIR/silence_sentinel.py" 2>/dev/null || echo "silence_warn=0 silence_age_s=unknown")
COMP_SUMMARY=$(python3 "$STATE_DIR/compaction_budget_sentinel.py" 2>/dev/null || echo "comp_storm=err comp_active=err comp_events_2h=err comp_alert=ERROR")
python3 "$STATE_DIR/sentinel_protection_emitter.py" >/dev/null 2>&1 || true

# ─── Status log + Heartbeat ──────────────────────────────────────────────────
LOOP_MS=$(( ($(date +%s) - LOOP_START_S) * 1000 ))
STATUS_LOG="$STATE_DIR/status.log"
MODEL_PRIMARY="unknown"
if command -v jq >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
  MODEL_PRIMARY="$(jq -r '.agents.defaults.model.primary // "unknown"' "$CONFIG" 2>/dev/null || echo "unknown")"
fi

echo "$(ts) status: port=$PORT_UP http=$HTTP_REASON ws=$WS_REASON state=$GATEWAY_STATE model_primary=$MODEL_PRIMARY $TOKEN_SUMMARY $SILENCE_SUMMARY $COMP_SUMMARY" >> "$STATUS_LOG"

log "HEARTBEAT: [openclaw][watchdog] HB $(ts): state=$GATEWAY_STATE reason=$GATEWAY_REASON http_ms=${HTTP_MS} ws_ms=${WS_MS} port=$PORT_UP process=$PROCESS_UP loop_ms=${LOOP_MS} last_model_age_s=${LAST_MODEL_AGE_S} last_model_provider=${LAST_MODEL_PROVIDER}"

log "WATCHDOG done"
exit 0
