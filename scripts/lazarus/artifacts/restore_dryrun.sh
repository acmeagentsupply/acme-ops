#!/usr/bin/env bash
# restore_dryrun.sh — Lazarus Protocol v1: Dry-Run Restore Validation
# Extracts archive to staging_restore/, runs integrity checks.
# NEVER touches live directories.
# Exit codes: 0=success 40=integrity_fail 50=archive_missing_corrupt

set -uo pipefail

STAGING_DIR="$HOME/.openclaw/watchdog/lazarus/staging_restore"
BACKUP_LOG="$HOME/.openclaw/watchdog/backup.log"
ARCHIVE="${1:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] RESTORE_DRYRUN $*" | tee -a "$BACKUP_LOG"; }

log "RUN_START staging=$STAGING_DIR"

# ── Find archive if not provided ──────────────────────────────────────────────
if [ -z "$ARCHIVE" ]; then
  LAZARUS_BASE="$HOME/.openclaw/watchdog/backups/lazarus"
  ARCHIVE=$(find "$LAZARUS_BASE" -name "openclaw-*.tar.gz" 2>/dev/null | sort | tail -1)
fi

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  log "ARCHIVE_MISSING: $ARCHIVE"
  exit 50
fi

# ── Verify archive integrity ──────────────────────────────────────────────────
tar -tzf "$ARCHIVE" > /dev/null 2>&1 || { log "ARCHIVE_CORRUPT: $ARCHIVE"; exit 50; }
log "ARCHIVE_OK: $ARCHIVE"

# ── Clean and prepare staging ─────────────────────────────────────────────────
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

# ── Extract ──────────────────────────────────────────────────────────────────
tar -xzf "$ARCHIVE" -C "$STAGING_DIR" 2>/dev/null
log "EXTRACTED to $STAGING_DIR"

# ── Integrity checks ─────────────────────────────────────────────────────────
FAIL=0

check_file() {
  local path="$STAGING_DIR/$1"
  if [ -f "$path" ]; then
    log "CHECK_PASS: $1 exists"
  else
    log "CHECK_FAIL: $1 MISSING"
    FAIL=$((FAIL+1))
  fi
}

check_json() {
  local path="$STAGING_DIR/$1"
  if [ -f "$path" ]; then
    python3 -c "import json; json.load(open('$path'))" 2>/dev/null \
      && log "CHECK_PASS: $1 valid JSON" \
      || { log "CHECK_FAIL: $1 invalid JSON"; FAIL=$((FAIL+1)); }
  else
    log "CHECK_WARN: $1 not found (may be expected)"
  fi
}

# Critical files
check_file ".openclaw/openclaw.json"
check_file ".openclaw/watchdog/hendrik_watchdog.sh"
check_file ".openclaw/watchdog/silence_sentinel.py"
check_file ".openclaw/watchdog/model_router.py"

# JSON validity
check_json ".openclaw/openclaw.json"
check_json ".openclaw/cron/jobs.json"

# Script presence
for script in hendrik_watchdog.sh silence_sentinel.py model_router.py; do
  check_file ".openclaw/watchdog/$script"
done

# Credentials directory present
if [ -d "$STAGING_DIR/.openclaw/credentials" ]; then
  log "CHECK_PASS: credentials/ directory present"
else
  log "CHECK_WARN: credentials/ not found"
fi

log "INTEGRITY_CHECKS_DONE: fail_count=$FAIL"

if [ $FAIL -gt 0 ]; then
  log "RESULT: INTEGRITY_FAIL (${FAIL} check(s) failed)"
  exit 40
fi

log "RESULT: ALL_CHECKS_PASSED"
log "STAGING_DIR: $STAGING_DIR"
log "SAFE_TO_REMOVE: rm -rf $STAGING_DIR"
exit 0
