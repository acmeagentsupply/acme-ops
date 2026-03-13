#!/bin/bash
# openclaw-watchdog-hygiene v1.1
# Keeps ~/.openclaw from ballooning. Safe, read-only for core state.
# Managed zones: watchdog backups, watchdog logs, optional gateway logs.
# Hard do-not-touch: openclaw.json, identity/**, credentials/**, agents/**/sessions/**, workspace/**

set -eo pipefail

WATCHDOG_HYGIENE_VERSION="1.1"

# ── Config (overridable via env) ────────────────────────────────────────────
HYGIENE_MAX_WATCHDOG_BYTES="${HYGIENE_MAX_WATCHDOG_BYTES:-1500000000}"   # 1.5GB
HYGIENE_MAX_OPENCLAW_BYTES="${HYGIENE_MAX_OPENCLAW_BYTES:-6000000000}"   # 6GB
HYGIENE_KEEP_DAYS="${HYGIENE_KEEP_DAYS:-7}"
HYGIENE_KEEP_LAST_N="${HYGIENE_KEEP_LAST_N:-10}"
HYGIENE_LOG_MAX_BYTES="${HYGIENE_LOG_MAX_BYTES:-1048576}"                 # 1MB
HYGIENE_TRIM_GATEWAY_LOGS="${HYGIENE_TRIM_GATEWAY_LOGS:-0}"
HYGIENE_DRY_RUN="${HYGIENE_DRY_RUN:-0}"
HYGIENE_RESTORE_STAGING_WARN_MB="${HYGIENE_RESTORE_STAGING_WARN_MB:-100}"
HYGIENE_RESTORE_STAGING_MB="${HYGIENE_RESTORE_STAGING_MB:-250}"
HYGIENE_LOG_WARN_BYTES="${HYGIENE_LOG_WARN_BYTES:-2097152}"             # warn at 2MB
HYGIENE_LOG_TRUNCATE_BYTES="${HYGIENE_LOG_TRUNCATE_BYTES:-10485760}"    # truncate at 10MB

OC_HOME="${HOME}/.openclaw"
WATCHDOG_DIR="${OC_HOME}/watchdog"
STATE_FILE="${WATCHDOG_DIR}/hygiene_state.json"
RESTORE_PRUNE_STATE_FILE="${WATCHDOG_DIR}/restore_staging_prune_state.json"
HYGIENE_LOG="${WATCHDOG_DIR}/hygiene.log"
RESTORE_PRUNE_COOLDOWN_SECS=1800

actions_taken=0
bytes_freed=0

# ── Helpers ─────────────────────────────────────────────────────────────────
log() {
  local ts; ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "[${ts}] $*" >> "${HYGIENE_LOG}"
}

run_or_dry() {
  local desc="$1"; shift
  if [[ "${HYGIENE_DRY_RUN}" == "1" ]]; then
    log "[DRY-RUN] would: ${desc}"
  else
    "$@"
    log "${desc}"
    (( actions_taken++ )) || true
  fi
}

bytes_of() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    du -sk "${path}" 2>/dev/null | awk '{print $1 * 1024}' || echo 0
  else
    echo 0
  fi
}

restore_lock_path() {
  local restore_root="$1"
  if [[ -f "${restore_root}/.restore_lock" ]]; then
    echo "${restore_root}/.restore_lock"
    return 0
  fi
  if [[ -f "${restore_root}/.active" ]]; then
    echo "${restore_root}/.active"
    return 0
  fi
  return 0
}

restore_last_prune_epoch() {
  local state_file="$1"
  [[ -f "${state_file}" ]] || return 0
  python3 - "${state_file}" <<'PY' 2>/dev/null || true
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except Exception:
    print("")
    raise SystemExit(0)

value = data.get("last_prune_epoch")
print("" if value is None else str(value))
PY
}

write_restore_prune_state() {
  local state_file="$1"
  local ts="$2"
  local epoch="$3"
  cat > "${state_file}" <<JSON
{
  "last_prune_ts": "${ts}",
  "last_prune_epoch": ${epoch}
}
JSON
}

# ── A: Disk census ───────────────────────────────────────────────────────────
log "=== openclaw-watchdog-hygiene START version=${WATCHDOG_HYGIENE_VERSION} ==="

before_watchdog=$(bytes_of "${WATCHDOG_DIR}")
before_openclaw=$(bytes_of "${OC_HOME}")

log "CENSUS before: openclaw=$(du -sh "${OC_HOME}" 2>/dev/null | cut -f1), watchdog=$(du -sh "${WATCHDOG_DIR}" 2>/dev/null | cut -f1)"
log "Top dirs under ~/.openclaw:"
du -sh "${OC_HOME}"/*/  2>/dev/null | sort -rh | head -8 | while read -r line; do log "  ${line}"; done || true

# ── B: Guard self-referential backup growth ─────────────────────────────────
log "B: Self-referential backup guard"
if [[ -d "${WATCHDOG_DIR}/backups" ]]; then
  while IFS= read -r -d '' nested; do
    local_size=$(stat -f%z "${nested}" 2>/dev/null || echo 0)
    run_or_dry "removed nested archive ${nested} (${local_size}B)" rm -f "${nested}"
    (( bytes_freed += local_size )) || true
  done < <(find "${WATCHDOG_DIR}/backups" -mindepth 2 -name "*.tar.gz" -print0 2>/dev/null || true)
fi

# ── C: Prune old archives safely ────────────────────────────────────────────
log "C: Pruning old archives (keep_days=${HYGIENE_KEEP_DAYS}, keep_last_n=${HYGIENE_KEEP_LAST_N})"
for backup_dir in \
    "${WATCHDOG_DIR}/backups" \
    "${WATCHDOG_DIR}/lazarus/backups" \
    "${WATCHDOG_DIR}"; do
  [[ -d "${backup_dir}" ]] || continue

  tarballs=()
  while IFS= read -r tf; do
    tarballs+=("${tf}")
  done < <(find "${backup_dir}" -maxdepth 1 -name "*.tar.gz" 2>/dev/null | \
    xargs -I{} stat -f "%m %N" {} 2>/dev/null | sort -rn | awk '{print $2}' || true)
  total=${#tarballs[@]}
  log "  ${backup_dir}: found ${total} tarballs"

  keep_n=$(( HYGIENE_KEEP_LAST_N ))
  idx=0
  for tarball in "${tarballs[@]}"; do
    (( idx++ )) || true
    if (( idx <= keep_n )); then
      log "  KEEP (last-N) ${tarball}"
      continue
    fi
    file_mtime=$(stat -f%m "${tarball}" 2>/dev/null || echo 0)
    now=$(date +%s)
    age_days=$(( (now - file_mtime) / 86400 ))
    if (( age_days >= HYGIENE_KEEP_DAYS )); then
      fsize=$(stat -f%z "${tarball}" 2>/dev/null || echo 0)
      run_or_dry "deleted archive ${tarball} (age=${age_days}d, ${fsize}B)" rm -f "${tarball}"
      (( bytes_freed += fsize )) || true
    fi
  done
done

# ── D: Log hygiene ──────────────────────────────────────────────────────────
log "D: Log hygiene (cap=${HYGIENE_LOG_MAX_BYTES}B, warn=${HYGIENE_LOG_WARN_BYTES}B, truncate=${HYGIENE_LOG_TRUNCATE_BYTES}B)"

truncate_log() {
  local logfile="$1" limit="$2" lines="$3"
  local fsize; fsize=$(stat -f%z "${logfile}" 2>/dev/null || echo 0)
  if (( fsize > limit )); then
    if [[ "${HYGIENE_DRY_RUN}" == "1" ]]; then
      log "[DRY-RUN] would truncate ${logfile} (${fsize}B -> last ${lines} lines)"
    else
      local tmp; tmp=$(mktemp)
      echo "# TRUNCATED by openclaw-watchdog-hygiene at $(date -u +"%Y-%m-%dT%H:%M:%SZ") (was ${fsize}B)" > "${tmp}"
      tail -n "${lines}" "${logfile}" >> "${tmp}"
      mv "${tmp}" "${logfile}"
      local new_size; new_size=$(stat -f%z "${logfile}" 2>/dev/null || echo 0)
      local saved=$(( fsize - new_size ))
      (( bytes_freed += saved )) || true
      (( actions_taken++ )) || true
      log "  truncated ${logfile}: ${fsize}B -> ${new_size}B (saved ${saved}B)"
    fi
  else
    log "  ok (${fsize}B): ${logfile}"
  fi
}

for logfile in \
    "${WATCHDOG_DIR}/watchdog.log" \
    "${WATCHDOG_DIR}/stall.log" \
    "${WATCHDOG_DIR}/ops_events.log" \
    "${WATCHDOG_DIR}/heartbeat.log" \
    "${WATCHDOG_DIR}/backup.log"; do
  [[ -f "${logfile}" ]] || continue
  truncate_log "${logfile}" "${HYGIENE_LOG_MAX_BYTES}" 2000
done

for logfile in \
    "${WATCHDOG_DIR}/status.log" \
    "${WATCHDOG_DIR}/radiation_findings.log"; do
  [[ -f "${logfile}" ]] || continue
  fsize=$(stat -f%z "${logfile}" 2>/dev/null || echo 0)
  if (( fsize > HYGIENE_LOG_WARN_BYTES && fsize <= HYGIENE_LOG_TRUNCATE_BYTES )); then
    log "  WARN (${fsize}B approaching limit): ${logfile}"
  fi
  truncate_log "${logfile}" "${HYGIENE_LOG_TRUNCATE_BYTES}" 5000
done

# ── E.5: restore_staging governance ────────────────────────────────────────
RESTORE_STAGING="${OC_HOME}/restore_staging"
RESTORE_COMPONENT_TAG="component=restore_staging_guard"
log "E.5: ${RESTORE_COMPONENT_TAG} restore_staging check"
if [[ ! -d "${RESTORE_STAGING}" ]]; then
  log "  ${RESTORE_COMPONENT_TAG} restore_staging: not present, skipping"
else
  lock_path=$(restore_lock_path "${RESTORE_STAGING}")
  rs_mb=$(du -sm "${RESTORE_STAGING}" 2>/dev/null | awk '{print $1}')
  rs_before_bytes=$(bytes_of "${RESTORE_STAGING}")
  now_epoch=$(date +%s)
  last_prune_epoch=$(restore_last_prune_epoch "${RESTORE_PRUNE_STATE_FILE}")
  log "  ${RESTORE_COMPONENT_TAG} restore_staging_pressure=${rs_mb}MB"

  if [[ -n "${lock_path}" ]]; then
    log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_mb}MB action=skipped reason=active_lock (${lock_path})"
  else
    if (( rs_mb >= HYGIENE_RESTORE_STAGING_WARN_MB )); then
      log "  ${RESTORE_COMPONENT_TAG} WARN size=${rs_mb}MB warn_threshold=${HYGIENE_RESTORE_STAGING_WARN_MB}MB prune_threshold=${HYGIENE_RESTORE_STAGING_MB}MB"
    fi

    if (( rs_mb < HYGIENE_RESTORE_STAGING_WARN_MB )); then
      log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_mb}MB action=skipped reason=under_warn_limit (${rs_mb}MB < ${HYGIENE_RESTORE_STAGING_WARN_MB}MB)"
    elif (( rs_mb <= HYGIENE_RESTORE_STAGING_MB )); then
      log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_mb}MB action=skipped reason=under_prune_limit (${rs_mb}MB <= ${HYGIENE_RESTORE_STAGING_MB}MB)"
    elif [[ -n "${last_prune_epoch}" ]] && (( now_epoch - last_prune_epoch < RESTORE_PRUNE_COOLDOWN_SECS )); then
      cooldown_age=$(( now_epoch - last_prune_epoch ))
      log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_mb}MB action=skipped reason=prune_cooldown_active (${cooldown_age}s < ${RESTORE_PRUNE_COOLDOWN_SECS}s)"
    else
      log "  ${RESTORE_COMPONENT_TAG} ${rs_mb}MB exceeds ${HYGIENE_RESTORE_STAGING_MB}MB - pruning files older than 24h"
      if [[ "${HYGIENE_DRY_RUN}" != "1" ]]; then
        find "${RESTORE_STAGING}" -type f -mmin +1440 -delete 2>/dev/null || true
        find "${RESTORE_STAGING}" -type d -empty -not -path "${RESTORE_STAGING}" -delete 2>/dev/null || true
      fi
      rs_after_mb=$(du -sm "${RESTORE_STAGING}" 2>/dev/null | awk '{print $1}')
      rs_after_bytes=$(bytes_of "${RESTORE_STAGING}")
      saved=$(( rs_before_bytes - rs_after_bytes ))
      (( bytes_freed += saved )) || true

      if (( rs_after_mb > HYGIENE_RESTORE_STAGING_MB )); then
        log "  ${RESTORE_COMPONENT_TAG} still ${rs_after_mb}MB after partial prune - full prune"
        if [[ "${HYGIENE_DRY_RUN}" != "1" ]]; then
          if [[ -f "${RESTORE_STAGING}/.keep" ]]; then
            tmp_keep=$(mktemp)
            cp "${RESTORE_STAGING}/.keep" "${tmp_keep}"
            rm -rf "${RESTORE_STAGING:?}"/*
            mv "${tmp_keep}" "${RESTORE_STAGING}/.keep"
          else
            rm -rf "${RESTORE_STAGING:?}"/*
          fi
        fi
        rs_final_mb=$(du -sm "${RESTORE_STAGING}" 2>/dev/null | awk '{print $1}')
        rs_final_bytes=$(bytes_of "${RESTORE_STAGING}")
        extra_saved=$(( rs_after_bytes - rs_final_bytes ))
        (( bytes_freed += extra_saved )) || true
        (( actions_taken++ )) || true
        write_restore_prune_state "${RESTORE_PRUNE_STATE_FILE}" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "${now_epoch}"
        log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_final_mb}MB action=full_prune reason=still_over_limit_after_partial"
      else
        (( actions_taken++ )) || true
        write_restore_prune_state "${RESTORE_PRUNE_STATE_FILE}" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "${now_epoch}"
        log "  ${RESTORE_COMPONENT_TAG} size_before=${rs_mb}MB size_after=${rs_after_mb}MB action=partial_prune reason=deleted_files_older_than_24h"
      fi
    fi
  fi
fi

# ── E: Optional gateway log trimming ───────────────────────────────────────
if [[ "${HYGIENE_TRIM_GATEWAY_LOGS}" == "1" ]]; then
  log "E: Gateway log trimming (enabled)"
  GW_ERR="${OC_HOME}/logs/gateway.err.log"
  GW_LOG="${OC_HOME}/logs/gateway.log"
  GW_ERR_LIMIT=$(( 50 * 1024 * 1024 ))
  GW_LOG_LIMIT=$(( 100 * 1024 * 1024 ))

  for gw_file in "${GW_ERR}:${GW_ERR_LIMIT}" "${GW_LOG}:${GW_LOG_LIMIT}"; do
    gw_path="${gw_file%%:*}"
    gw_limit="${gw_file##*:}"
    [[ -f "${gw_path}" ]] || continue
    fsize=$(stat -f%z "${gw_path}" 2>/dev/null || echo 0)
    if (( fsize > gw_limit )); then
      if [[ "${HYGIENE_DRY_RUN}" == "1" ]]; then
        log "[DRY-RUN] would trim ${gw_path} (${fsize}B -> last 5000 lines)"
      else
        tmp=$(mktemp)
        echo "# TRIMMED by openclaw-watchdog-hygiene at $(date -u +"%Y-%m-%dT%H:%M:%SZ") (was ${fsize}B)" > "${tmp}"
        tail -n 5000 "${gw_path}" >> "${tmp}"
        mv "${tmp}" "${gw_path}"
        new_size=$(stat -f%z "${gw_path}" 2>/dev/null || echo 0)
        saved=$(( fsize - new_size ))
        (( bytes_freed += saved )) || true
        (( actions_taken++ )) || true
        log "  trimmed ${gw_path}: ${fsize}B -> ${new_size}B (saved ${saved}B)"
      fi
    else
      log "  ok (${fsize}B): ${gw_path}"
    fi
  done
else
  log "E: Gateway log trimming disabled (HYGIENE_TRIM_GATEWAY_LOGS=0)"
fi

# ── F: Write hygiene_state.json ─────────────────────────────────────────────
after_watchdog=$(bytes_of "${WATCHDOG_DIR}")
after_openclaw=$(bytes_of "${OC_HOME}")

ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "${STATE_FILE}" <<JSON
{
  "ts": "${ts}",
  "version": "${WATCHDOG_HYGIENE_VERSION}",
  "dry_run": ${HYGIENE_DRY_RUN},
  "before_bytes_watchdog": ${before_watchdog},
  "after_bytes_watchdog": ${after_watchdog},
  "before_bytes_openclaw": ${before_openclaw},
  "after_bytes_openclaw": ${after_openclaw},
  "bytes_freed_estimate": ${bytes_freed},
  "actions_taken": ${actions_taken}
}
JSON

log "SUMMARY: version=${WATCHDOG_HYGIENE_VERSION}, actions=${actions_taken}, freed=${bytes_freed}B, watchdog=$(du -sh "${WATCHDOG_DIR}" 2>/dev/null | cut -f1), openclaw=$(du -sh "${OC_HOME}" 2>/dev/null | cut -f1)"
log "=== openclaw-watchdog-hygiene DONE ==="
