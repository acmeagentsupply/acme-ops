#!/usr/bin/env bash
# backup_local.sh — Lazarus Protocol v1: Local Tarball Snapshot
# Creates a dated archive of critical OpenClaw state.
# NEVER deletes source files. Always exits 0 (errors logged, not propagated).
# Exit codes: 0=success 10=partial 20=policy_block 30=runtime_error

set -uo pipefail

TIMESTAMP=$(date +%Y-%m-%d)
SNAPSHOT_ID=$(date +%Y%m%d-%H%M%S)
ARCHIVE_DIR="$HOME/.openclaw/watchdog/backups/lazarus/$TIMESTAMP"
ARCHIVE_FILE="$ARCHIVE_DIR/openclaw-$SNAPSHOT_ID.tar.gz"
MANIFEST_FILE="$ARCHIVE_DIR/manifest-$SNAPSHOT_ID.json"
BACKUP_LOG="$HOME/.openclaw/watchdog/backup.log"
EXIT_CODE=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] LAZARUS $*" | tee -a "$BACKUP_LOG"; }

log "RUN_START snapshot_id=$SNAPSHOT_ID"
mkdir -p "$ARCHIVE_DIR" || { log "RUNTIME_ERROR: cannot create $ARCHIVE_DIR"; exit 30; }

# ── Build file list ────────────────────────────────────────────────────────────
TMPLIST=$(mktemp)
EXCLUDE_FILE=$(mktemp)

cat > "$EXCLUDE_FILE" <<'EXCL'
node_modules
.DS_Store
*.sock
*.pid
staging_restore
restore_staging
.openclaw/watchdog/backups
EXCL

# Capture ~/.openclaw/
tar -czf "$ARCHIVE_FILE" \
  --exclude-from="$EXCLUDE_FILE" \
  -C "$HOME" ".openclaw" \
  --warning=no-file-changed \
  2>/dev/null
TAR_EXIT=$?
rm -f "$TMPLIST" "$EXCLUDE_FILE"

if [ $TAR_EXIT -ne 0 ] && [ $TAR_EXIT -ne 1 ]; then
  log "RUNTIME_ERROR: tar exited $TAR_EXIT"
  EXIT_CODE=30
fi

# ── Add LaunchAgents (append to archive) ──────────────────────────────────────
LA_COUNT=0
for plist in "$HOME/Library/LaunchAgents/ai.openclaw"*.plist; do
  [ -f "$plist" ] || continue
  PLDIR=$(mktemp -d)
  cp "$plist" "$PLDIR/" && \
    tar -rzf "$ARCHIVE_FILE" -C "$PLDIR" "$(basename "$plist")" 2>/dev/null && \
    LA_COUNT=$((LA_COUNT+1))
  rm -rf "$PLDIR"
done
log "LAUNCHAGENTS_CAPTURED: $LA_COUNT plist(s)"

# ── Manifest ──────────────────────────────────────────────────────────────────
FILE_COUNT=$(tar -tzf "$ARCHIVE_FILE" 2>/dev/null | wc -l | tr -d ' ')
ARCHIVE_SIZE=$(stat -f%z "$ARCHIVE_FILE" 2>/dev/null || stat -c%s "$ARCHIVE_FILE" 2>/dev/null || echo 0)
SHA256=$(shasum -a 256 "$ARCHIVE_FILE" 2>/dev/null | awk '{print $1}' || openssl dgst -sha256 "$ARCHIVE_FILE" 2>/dev/null | awk '{print $NF}' || echo "unavailable")

cat > "$MANIFEST_FILE" <<MANIFEST
{
  "snapshot_id": "$SNAPSHOT_ID",
  "archive": "$ARCHIVE_FILE",
  "file_count": $FILE_COUNT,
  "archive_size_bytes": $ARCHIVE_SIZE,
  "sha256": "$SHA256",
  "launchagents_captured": $LA_COUNT,
  "exit_code": $EXIT_CODE,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
MANIFEST

log "MANIFEST_WRITTEN: $MANIFEST_FILE"
log "ARCHIVE: $ARCHIVE_FILE size=${ARCHIVE_SIZE}B files=$FILE_COUNT sha256=$SHA256"
log "RUN_END exit_code=$EXIT_CODE"
exit $EXIT_CODE
