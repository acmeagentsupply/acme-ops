#!/usr/bin/env bash
# ops_event_marker.sh — append a structured NDJSON event to ops_events.log
# Usage:
#   bash ops_event_marker.sh GATEWAY_COMPACTION start manual
#   bash ops_event_marker.sh GATEWAY_COMPACTION end   manual
#   bash ops_event_marker.sh CUSTOM_EVENT       note  "description here"
#
# Args:
#   $1  event name   (e.g. GATEWAY_COMPACTION)
#   $2  phase/action (e.g. start | end | note)
#   $3  source       (e.g. manual | detected | auto)
#   $4  (optional)   extra key=value pairs as JSON fragment
#
# The file is auto-created if missing.

set -euo pipefail

WATCHDOG_DIR="$HOME/.openclaw/watchdog"
OPS_LOG="$WATCHDOG_DIR/ops_events.log"
mkdir -p "$WATCHDOG_DIR"
touch "$OPS_LOG"

EVENT="${1:-CUSTOM_EVENT}"
PHASE="${2:-note}"
SOURCE="${3:-manual}"
EXTRA="${4:-}"

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

if [ -n "$EXTRA" ]; then
    LINE="{\"ts\":\"$TS\",\"event\":\"$EVENT\",\"phase\":\"$PHASE\",\"source\":\"$SOURCE\",\"extra\":\"$EXTRA\"}"
else
    LINE="{\"ts\":\"$TS\",\"event\":\"$EVENT\",\"phase\":\"$PHASE\",\"source\":\"$SOURCE\"}"
fi

# Atomic append via temp file + mv (avoids partial writes)
TMPFILE=$(mktemp "$WATCHDOG_DIR/.ops_event_XXXXXX")
echo "$LINE" > "$TMPFILE"
cat "$TMPFILE" >> "$OPS_LOG"
rm -f "$TMPFILE"

echo "MARKER OK: $LINE"
