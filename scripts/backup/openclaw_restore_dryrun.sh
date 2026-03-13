#!/usr/bin/env bash
# openclaw_restore_dryrun.sh — Backup Hardening v1 Addendum
# Dry-run restore from latest GDrive snapshot into staging dir.
# NEVER overwrites live ~/.openclaw/
#
# Exit codes:
#   0  success — all invariants passed
#  40  integrity fail
#  50  snapshot missing/corrupt

set -uo pipefail

GDRIVE_BASE="$HOME/Library/CloudStorage/GoogleDrive-hendrik.homarus@gmail.com/My Drive/OpenClawBackups/AGENTMacBook"
STAGING_ROOT="$HOME/.openclaw/restore_staging"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
STAGING_DIR="$STAGING_ROOT/$TIMESTAMP"
LOG="$HOME/.openclaw/watchdog/backup.log"
FAIL=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S EST')] RESTORE_DRYRUN $*" | tee -a "$LOG"; }

log "=== START ts=$TIMESTAMP ==="

# ── Find latest snapshot ───────────────────────────────────────────────────────
LATEST=$(ls -dt "$GDRIVE_BASE/openclaw-"* 2>/dev/null | head -1)

if [ -z "$LATEST" ] || [ ! -d "$LATEST" ]; then
    log "FAIL: No snapshot found at $GDRIVE_BASE"
    exit 50
fi
log "SOURCE: $LATEST"
log "STAGING: $STAGING_DIR"

# ── Verify source is readable ──────────────────────────────────────────────────
if [ ! -r "$LATEST" ]; then
    log "FAIL: Source snapshot not readable"
    exit 50
fi

# ── Copy to staging (rsync, read-only from source, no --delete) ───────────────
mkdir -p "$STAGING_DIR"
rsync -a --no-perms \
    --exclude=node_modules/ \
    --exclude=.DS_Store \
    --exclude=*.sock \
    --exclude=*.pid \
    "$LATEST/" "$STAGING_DIR/" 2>/dev/null
RSYNC_EXIT=$?
if [ $RSYNC_EXIT -ne 0 ] && [ $RSYNC_EXIT -ne 24 ]; then
    log "FAIL: rsync exited $RSYNC_EXIT"
    exit 50
fi
log "RSYNC_DONE exit=$RSYNC_EXIT"

# ── Integrity invariants ───────────────────────────────────────────────────────
check_file() {
    local rel_path="$1"
    local full="$STAGING_DIR/$rel_path"
    if [ -f "$full" ]; then
        log "PASS: $rel_path exists"
    else
        log "FAIL: $rel_path MISSING"
        FAIL=$((FAIL+1))
    fi
}

check_json() {
    local rel_path="$1"
    local full="$STAGING_DIR/$rel_path"
    if [ -f "$full" ]; then
        python3 -m json.tool "$full" > /dev/null 2>&1 \
            && log "PASS: $rel_path is valid JSON" \
            || { log "FAIL: $rel_path invalid JSON"; FAIL=$((FAIL+1)); }
    else
        log "WARN: $rel_path not found (skipping JSON check)"
    fi
}

log "--- INVARIANT CHECKS ---"

# openclaw.json
check_file "openclaw.json"
check_json "openclaw.json"

# Watchdog scripts
check_file "watchdog/hendrik_watchdog.sh"
check_file "watchdog/silence_sentinel.py"
check_file "watchdog/model_router.py"

# LaunchAgents plist (from repo copy)
PLIST_FOUND=0
for plist in "$STAGING_DIR/workspace/openclaw-ops/scripts/backup/"*.plist; do
    [ -f "$plist" ] && { log "PASS: repo plist $(basename $plist)"; PLIST_FOUND=1; break; }
done
for plist in "$HOME/Library/LaunchAgents/ai.openclaw"*.plist; do
    [ -f "$plist" ] && { log "PASS: runtime plist $(basename $plist) (live, not staging)"; PLIST_FOUND=1; break; }
done
[ $PLIST_FOUND -eq 0 ] && { log "WARN: no launchd plist found in staging or live LaunchAgents"; }

log "--- INVARIANT RESULTS: fail_count=$FAIL ---"

# ── Show staging tree (truncated) ─────────────────────────────────────────────
log "STAGING_TREE (depth 2):"
find "$STAGING_DIR" -maxdepth 2 -type d 2>/dev/null | head -20 | while read -r d; do
    log "  $d"
done

if [ $FAIL -gt 0 ]; then
    log "RESULT: INTEGRITY_FAIL (${FAIL} failed)"
    log "STAGING: $STAGING_DIR (preserved for inspection)"
    exit 40
fi

log "RESULT: ALL_INVARIANTS_PASSED exit=0"
log "STAGING: $STAGING_DIR"
log "NOTE: safe to remove with: rm -rf $STAGING_DIR"
exit 0
