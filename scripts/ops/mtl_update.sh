#!/usr/bin/env bash
# mtl_update.sh — MTL + Dashboard updater wrapper
# Usage: bash mtl_update.sh [--dry-run]
# Always exits 0. Commits only if files changed.

set -uo pipefail
REPO="$HOME/.openclaw/workspace"
SCRIPT="openclaw-ops/scripts/ops/mtl_apply_updates.py"
OPS_DIR="openclaw-ops/ops"
LOG="$HOME/.openclaw/watchdog/backup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] MTL_UPDATE $*" | tee -a "$LOG"; }

log "START"

cd "$REPO" || { log "ERROR: cannot cd to $REPO"; exit 0; }

# Run updater
python3 "$SCRIPT" 2>&1 | while IFS= read -r line; do log "$line"; done

# Stage only ops files
git add "$OPS_DIR/MTL.md" \
        "$OPS_DIR/DASHBOARD.md" \
        "$OPS_DIR/MTL.snapshot.json" \
        "$OPS_DIR/mtl_updates.ndjson" 2>/dev/null || true

# Commit if anything changed
if git diff --cached --quiet; then
  log "NO_CHANGES: nothing to commit"
else
  git commit -m "ops: update MTL/DASHBOARD $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    2>&1 | while IFS= read -r line; do log "$line"; done
  log "COMMITTED"

  # Push
  git push 2>&1 | while IFS= read -r line; do log "$line"; done
  log "PUSHED"
fi

log "DONE"
exit 0
