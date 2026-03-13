#!/usr/bin/env bash
# openclaw_snapshot.sh — Safe additive snapshot of ~/.openclaw/ to Google Drive
# Includes: manifest+checksum (A1), retention guardrail (A2), restore drill signal (A3)
# Exit code: always 0 (failures logged, not propagated)

set -uo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
SOURCE="$HOME/.openclaw/"
GDRIVE_ROOT="$HOME/Library/CloudStorage/GoogleDrive-hendrik.homarus@gmail.com/My Drive"
BACKUP_BASE="$GDRIVE_ROOT/OpenClawBackups/AGENTMacBook"
DEST="$BACKUP_BASE/openclaw-$TIMESTAMP"
BACKUP_LOG="$HOME/.openclaw/watchdog/backup.log"
LATEST_LINK="$BACKUP_BASE/latest"
KEEP_LAST_N=14
RESTORE_STAGING="$HOME/.openclaw/restore_staging"

# ── Logging ───────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$BACKUP_LOG"; }
log_and_echo() { echo "$*"; log "$*"; }

# ── Safety guard ──────────────────────────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
  log "SAFETY_ABORT: running as root — refusing"
  exit 0
fi
if [ ! -d "$SOURCE" ]; then
  log "BACKUP_SOURCE_MISSING: $SOURCE — aborting"
  exit 0
fi

log_and_echo "BACKUP START ts=$TIMESTAMP src=$SOURCE dest=$DEST"

# ── Check / create destination ────────────────────────────────────────────────
if ! mkdir -p "$DEST" 2>/dev/null; then
  log "BACKUP_TARGET_ERROR: cannot create $DEST"
  [ ! -d "$GDRIVE_ROOT" ] && log "BACKUP_TARGET_MISSING: GDrive not accessible at $GDRIVE_ROOT"
  exit 0
fi
log "DEST_CREATED: $DEST"

# ── A3: Restore drill cadence signal ─────────────────────────────────────────
if [ -d "$RESTORE_STAGING" ]; then
  LATEST_DRILL=$(ls -1dt "$RESTORE_STAGING"/[0-9]* 2>/dev/null | head -1)
  if [ -n "$LATEST_DRILL" ]; then
    DRILL_TS=$(basename "$LATEST_DRILL")
    # Parse timestamp YYYYMMDD-HHMMSS → epoch
    DRILL_EPOCH=$(date -j -f "%Y%m%d-%H%M%S" "$DRILL_TS" "+%s" 2>/dev/null \
                  || python3 -c "
import time, sys
s = sys.argv[1]
t = time.mktime(time.strptime(s, '%Y%m%d-%H%M%S'))
print(int(t))" "$DRILL_TS" 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    if [ "${DRILL_EPOCH:-0}" -gt 0 ]; then
      AGE_HOURS=$(( (NOW_EPOCH - DRILL_EPOCH) / 3600 ))
      log "RESTORE_DRILL_AGE_HOURS=$AGE_HOURS (last drill: $DRILL_TS)"
    else
      log "RESTORE_DRILL_MISSING (could not parse staging timestamp)"
    fi
  else
    log "RESTORE_DRILL_MISSING (no staging dirs found)"
  fi
else
  log "RESTORE_DRILL_MISSING (staging dir absent: $RESTORE_STAGING)"
fi

# ── Repo divergence check (push discipline) ───────────────────────────────────
WORKSPACE="$HOME/.openclaw/workspace"
if [ -d "$WORKSPACE/.git" ]; then
  GIT_BRANCH=$(cd "$WORKSPACE" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
  GIT_SB=$(cd "$WORKSPACE" && git status -sb 2>/dev/null | head -3)
  GIT_LOG=$(cd "$WORKSPACE" && git log --oneline --decorate -n 3 2>/dev/null | tr '\n' '|')
  AHEAD=$(cd "$WORKSPACE" && git rev-list origin/main..HEAD --count 2>/dev/null || echo 0)
  log "REPO_BRANCH: $GIT_BRANCH"
  log "REPO_STATUS: $GIT_SB"
  log "REPO_LOG: $GIT_LOG"
  if [ "${AHEAD:-0}" -gt 0 ]; then
    log "REPO_AHEAD_COMMITS=$AHEAD — WARNING: unpushed commits. Push before assuming repo is backed up."
  else
    log "REPO_AHEAD_COMMITS=0 — repo in sync with origin"
  fi
fi

# ── Rsync (additive — no --delete) ───────────────────────────────────────────
RSYNC_CMD=(
  rsync -a --no-perms
  --exclude="node_modules/"
  --exclude=".DS_Store"
  --exclude="*.sock"
  --exclude="*.pid"
  --exclude=".write_test"
  --exclude=".access_test"
  --stats
)

log "RSYNC_CMD: ${RSYNC_CMD[*]} $SOURCE $DEST/"
RSYNC_OUTPUT=$("${RSYNC_CMD[@]}" "$SOURCE" "$DEST/" 2>&1)
RSYNC_EXIT=$?

if [ "$RSYNC_EXIT" -eq 0 ] || [ "$RSYNC_EXIT" -eq 24 ]; then
  FILES_XFERRED=$(echo "$RSYNC_OUTPUT" | grep "Number of regular files transferred:" | awk '{print $NF}' || echo "?")
  TOTAL_SIZE=$(echo "$RSYNC_OUTPUT" | grep "Total file size:" | awk '{print $4}' || echo "?")
  log "BACKUP_SUCCESS: files_transferred=$FILES_XFERRED total_size=$TOTAL_SIZE dest=$DEST"
  log_and_echo "BACKUP OK: $TIMESTAMP — files=$FILES_XFERRED size=$TOTAL_SIZE"
else
  log "BACKUP_WARN: rsync exited $RSYNC_EXIT — partial backup"
  log "RSYNC_OUTPUT: ${RSYNC_OUTPUT:0:300}"
  log_and_echo "BACKUP PARTIAL: rsync exit=$RSYNC_EXIT (logged, not fatal)"
fi

# ── A1: Snapshot manifest + checksum ─────────────────────────────────────────
MANIFEST="$DEST/manifest.txt"
{
  MANIFEST_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # File count and total bytes (du -sk = kbytes; use find for count)
  FILE_COUNT=$(find "$DEST" -not -name "manifest.txt" -type f 2>/dev/null | wc -l | tr -d ' ')
  TOTAL_BYTES=$(du -sb "$DEST" 2>/dev/null | awk '{print $1}' \
                || du -sk "$DEST" 2>/dev/null | awk '{print $1*1024}' || echo 0)
  # Deterministic file-list hash (sorted paths, no manifest itself)
  FILE_LIST_HASH=$(find "$DEST" -not -name "manifest.txt" -type f \
                   | sort | shasum -a 256 2>/dev/null | awk '{print $1}' \
                   || find "$DEST" -not -name "manifest.txt" -type f \
                      | sort | sha256sum 2>/dev/null | awk '{print $1}' \
                   || echo "unavailable")
  echo "snapshot_timestamp: $TIMESTAMP"
  echo "manifest_created:   $MANIFEST_TS"
  echo "source_path:        $SOURCE"
  echo "dest_path:          $DEST"
  echo "file_count:         $FILE_COUNT"
  echo "total_bytes:        $TOTAL_BYTES"
  echo "filelist_sha256:    $FILE_LIST_HASH"
  echo "rsync_exit_status:  $RSYNC_EXIT"
} > "$MANIFEST" 2>/dev/null
log "MANIFEST_WRITTEN: $MANIFEST (files=$FILE_COUNT bytes=$TOTAL_BYTES sha256=$FILE_LIST_HASH)"

# ── Update latest symlink ─────────────────────────────────────────────────────
ln -sfn "$DEST" "$LATEST_LINK" 2>/dev/null \
  && log "LATEST_LINK updated → $DEST" \
  || log "WARN: could not update latest symlink"

# ── A2: Retention guardrail (keep last KEEP_LAST_N, log decision) ────────────
SNAPSHOT_LIST=$(ls -1dt "$BACKUP_BASE"/openclaw-* 2>/dev/null)
SNAPSHOT_COUNT=$(echo "$SNAPSHOT_LIST" | grep -c "openclaw-" || echo 0)

if [ "${SNAPSHOT_COUNT:-0}" -gt "$KEEP_LAST_N" ]; then
  TO_DELETE=$(echo "$SNAPSHOT_LIST" | tail -n +$((KEEP_LAST_N + 1)))
  REMOVED=0
  while IFS= read -r old; do
    [ -z "$old" ] && continue
    # Safety: never delete current run or source
    if [ "$old" = "$DEST" ] || [ "$old" = "$SOURCE" ]; then
      log "RETENTION_PRUNE_WARN: skipping current dest/source: $old"
      continue
    fi
    if rm -rf "$old" 2>/dev/null; then
      log "RETENTION_PRUNE_REMOVED: $old"
      REMOVED=$((REMOVED+1))
    else
      log "RETENTION_PRUNE_WARN: could not remove $old"
    fi
  done <<< "$TO_DELETE"
  log "RETENTION_PRUNE: removed=$REMOVED (keeping $KEEP_LAST_N of $SNAPSHOT_COUNT)"
else
  log "RETENTION_PRUNE: none_needed (count=$SNAPSHOT_COUNT keep=$KEEP_LAST_N)"
fi

log_and_echo "BACKUP DONE ts=$TIMESTAMP"
exit 0
