#!/usr/bin/env bash
# Acceptance test for stall_detector.py
# Creates a synthetic session JSONL with forced 95s gap, verifies WARN + HARD fire.

set -euo pipefail

STATE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DETECTOR="$STATE_DIR/stall_detector.py"
TEST_DIR="$(mktemp -d)"
TEST_STALL_LOG="$TEST_DIR/stall.log"
TEST_SEEN="$TEST_DIR/stall_seen.txt"
SESSION_FILE="$TEST_DIR/test-deadbeef-0000-0000-0000-000000000001.jsonl"

cleanup() { rm -rf "$TEST_DIR"; }
trap cleanup EXIT

TS_NOW="$(date '+%Y-%m-%d %H:%M:%S %Z')"
NOW_EPOCH="$(date +%s)"

# Build timestamps: user_ts = 95s ago, asst_ts = now  (95000ms gap → HARD > WARN)
USER_EPOCH=$(( NOW_EPOCH - 95 ))
# Format: 2026-02-24T16:30:00.000Z
fmt_iso() {
  python3 -c "import datetime; print(datetime.datetime.utcfromtimestamp($1).strftime('%Y-%m-%dT%H:%M:%S.000Z'))"
}
USER_ISO="$(fmt_iso $USER_EPOCH)"
ASST_ISO="$(fmt_iso $NOW_EPOCH)"

cat > "$SESSION_FILE" << JSONL
{"type":"session","version":3,"id":"test-deadbeef-0000-0000-0000-000000000001","timestamp":"${USER_ISO}","cwd":"/tmp"}
{"type":"model_change","id":"aaa00001","parentId":null,"timestamp":"${USER_ISO}","provider":"anthropic","modelId":"claude-sonnet-4-6"}
{"type":"message","id":"msg00001","parentId":"aaa00001","timestamp":"${USER_ISO}","message":{"role":"user","content":[{"type":"text","text":"test prompt"}],"timestamp":$(( USER_EPOCH * 1000 ))}}
{"type":"message","id":"msg00002","parentId":"msg00001","timestamp":"${ASST_ISO}","message":{"role":"assistant","content":[{"type":"text","text":"test response"}],"provider":"anthropic","model":"claude-sonnet-4-6","api":"anthropic-messages","usage":{"input":10,"output":5},"stopReason":"stop","timestamp":$(( NOW_EPOCH * 1000 ))}}
JSONL

echo "[test] Synthetic session written: $SESSION_FILE"
echo "[test] User ts: $USER_ISO  Asst ts: $ASST_ISO  Gap: 95s"
echo ""

# Run detector
RESULT="$(python3 "$DETECTOR" "$TEST_DIR" "$TEST_STALL_LOG" "$TEST_SEEN" "45000" "90000" "$TS_NOW")"
echo "[test] stall_detector stdout: $RESULT"
echo ""

# Verify stall log
echo "[test] stall.log contents:"
cat "$TEST_STALL_LOG"
echo ""

# Assertions
count_lines() { grep "$1" "$2" 2>/dev/null | wc -l | tr -d ' '; }

WARN_COUNT="$(count_lines MODEL_STALL_WARN "$TEST_STALL_LOG")"
HARD_COUNT="$(count_lines MODEL_STALL_HARD "$TEST_STALL_LOG")"

echo "[test] WARN count: $WARN_COUNT (expected: 1)"
echo "[test] HARD count: $HARD_COUNT (expected: 1)"

if [ "$WARN_COUNT" -eq 1 ] && [ "$HARD_COUNT" -eq 1 ]; then
  echo "[test] PASS: both WARN and HARD emitted"
else
  echo "[test] FAIL: expected 1 WARN + 1 HARD"
  exit 1
fi

# Verify dedup: run again — should produce no new lines
python3 "$DETECTOR" "$TEST_DIR" "$TEST_STALL_LOG" "$TEST_SEEN" "45000" "90000" "$TS_NOW" >/dev/null
WARN_COUNT2="$(count_lines MODEL_STALL_WARN "$TEST_STALL_LOG")"
HARD_COUNT2="$(count_lines MODEL_STALL_HARD "$TEST_STALL_LOG")"

if [ "$WARN_COUNT2" -eq 1 ] && [ "$HARD_COUNT2" -eq 1 ]; then
  echo "[test] PASS: dedup verified (second run produced no new lines)"
else
  echo "[test] FAIL: dedup broken (counts: WARN=$WARN_COUNT2 HARD=$HARD_COUNT2)"
  exit 1
fi

echo ""
echo "[test] ALL TESTS PASSED"
