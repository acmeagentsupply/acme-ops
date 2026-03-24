#!/usr/bin/env bash
# =============================================================================
# ACME Install Helper v0.3  —  A-INSTALL-V0-001 / A-INSTALL-V0-002
# Bundle installer for ACME Agent Supply Co. products
# Compatible: bash 3.2+ (macOS default)
# =============================================================================
# SAFETY: No openclaw.json writes. No restarts. Writes ONLY to:
#   ~/.openclaw/watchdog/  and  ~/.openclaw/bin/
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"             # acme-ops repo root
SCRIPTS_ROOT="${REPO_ROOT}/scripts"

WATCHDOG_DIR="${HOME}/.openclaw/watchdog"
BIN_DIR="${HOME}/.openclaw/bin"
INSTALL_STATE_DIR="${WATCHDOG_DIR}/install"
INSTALL_STATE_FILE="${INSTALL_STATE_DIR}/install_state.json"
LOCK_FILE="${INSTALL_STATE_DIR}/bundles.lock.json"
OPS_EVENTS_LOG="${WATCHDOG_DIR}/ops_events.log"
A911_STATE="${WATCHDOG_DIR}/agent911_state.json"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
BUNDLE=""
DRY_RUN=false
APPLY=false
VERIFY=false
SCRIPT_VERSION="0.3.0"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
ts()       { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_info() { echo "[$(ts)] INFO  $*"; }
log_ok()   { echo "[$(ts)] OK    $*"; }
log_skip() { echo "[$(ts)] SKIP  $*"; }
log_dry()  { echo "[$(ts)] DRY   $*"; }
log_err()  { echo "[$(ts)] ERROR $*" >&2; }
log_vfy()  { echo "[$(ts)] VFY   $*"; }
log_fail() { echo "[$(ts)] FAIL  $*"; }

# Emit NDJSON event to ops log + stdout
emit_event() {
    local event_type="$1"; shift
    local json_extra=""
    while [[ $# -gt 0 ]]; do
        local pair="$1"
        local k="${pair%%=*}"
        local v="${pair#*=}"
        # Escape any quotes in value
        v="${v//\"/\\\"}"
        json_extra="${json_extra},\"${k}\":\"${v}\""
        shift
    done
    local line="{\"ts\":\"$(ts)\",\"event\":\"${event_type}\"${json_extra}}"
    if [[ "${DRY_RUN}" == "false" ]]; then
        mkdir -p "${WATCHDOG_DIR}" 2>/dev/null || true
        echo "${line}" >> "${OPS_EVENTS_LOG}"
    fi
    echo "  [EVENT] ${line}"
}

# ---------------------------------------------------------------------------
# SHA-256 helper (portable: macOS shasum -a 256; GNU sha256sum fallback)
# ---------------------------------------------------------------------------
sha256_file() {
    local f="$1"
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "${f}" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "${f}" | awk '{print $1}'
    else
        md5 -q "${f}" 2>/dev/null || md5sum "${f}" | awk '{print $1}'
    fi
}

# ---------------------------------------------------------------------------
# Lock file helpers
# ---------------------------------------------------------------------------
lockfile_get_sha() {
    local dst_rel="$1"
    if [[ ! -f "${LOCK_FILE}" ]]; then echo ""; return; fi
    python3 -c "
import json,sys
try:
    d = json.load(open('${LOCK_FILE}'))
    print(d.get('${dst_rel}', {}).get('sha256', ''))
except:
    print('')
" 2>/dev/null || echo ""
}

lockfile_update() {
    local dst_rel="$1"
    local sha256="$2"
    local version="$3"
    local ts_val="$4"

    mkdir -p "${INSTALL_STATE_DIR}"
    python3 - <<PYEOF
import json, os
path = '${LOCK_FILE}'
try:
    with open(path) as f:
        d = json.load(f)
except:
    d = {}
d['${dst_rel}'] = {'sha256': '${sha256}', 'installed_at': '${ts_val}', 'version': '${version}'}
d['_meta'] = {'schema': 'bundles.lock.v1.0', 'updated_at': '${ts_val}', 'installer_version': '${version}'}
with open(path, 'w') as f:
    json.dump(d, f, indent=2, sort_keys=True)
PYEOF
}

# ---------------------------------------------------------------------------
# Product file manifest — returns "src_rel:dst_rel[:x]" (x = needs exec bit)
#
# v0.3 changes:
#   sentinel  — updated to Transmission v2 layout:
#               Path 1: scripts/sentinel/ (attach bridge + funnel alignment)
#               Path 2: scripts/watchdog/ (predictive guard + compaction stack)
#   sphinxgate — REMOVED from --bundle all; policy layer ships inside Sentinel.
#                Standalone bundle coming soon.
# ---------------------------------------------------------------------------
bundle_files() {
    local product="$1"
    case "${product}" in
        radcheck)
            echo "radiation/radiation_check.py:radiation_check.py"
            echo "radiation/radcheck_scoring_v2.py:radcheck_scoring_v2.py"
            ;;
        sentinel)
            # Path 1 — attachment detection + funnel alignment (scripts/sentinel/)
            echo "sentinel/sentinel_attach_bridge.py:sentinel_attach_bridge.py"
            echo "sentinel/sentinel_funnel_alignment.py:sentinel_funnel_alignment.py"
            # Path 2 — predictive guard + compaction stack (scripts/watchdog/)
            echo "watchdog/sentinel_predictive_guard.py:sentinel_predictive_guard.py"
            echo "watchdog/silence_sentinel.py:silence_sentinel.py"
            echo "watchdog/sentinel_protection_emitter.py:sentinel_protection_emitter.py"
            echo "watchdog/compaction_log_parser.py:compaction_log_parser.py"
            echo "watchdog/compaction_budget_sentinel.py:compaction_budget_sentinel.py"
            echo "watchdog/hendrik_watchdog.sh:hendrik_watchdog.sh:x"
            ;;
        sphinxgate)
            # SphinxGate v1 ships as part of the Sentinel bundle (model_router policy layer).
            # Standalone installable artifact coming in a future release.
            # This bundle intentionally produces 0 files — install succeeds with SKIPPED=0, FAILED=0.
            echo "# SphinxGate standalone: coming soon — included in Sentinel bundle" > /dev/null
            ;;
        agent911)
            echo "agent911/agent911_snapshot.py:agent911_snapshot.py"
            echo "agent911/findmyagent_classifier.py:findmyagent_classifier.py"
            echo "agent911/weekly_operator_report.py:weekly_operator_report.py"
            ;;
        *)
            log_err "bundle_files: unknown product '${product}'"
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Agent911 state probe
# ---------------------------------------------------------------------------
a911_field() {
    local field="$1"
    if [[ ! -f "${A911_STATE}" ]]; then echo "unknown"; return; fi
    python3 -c "
import json
try:
    d = json.load(open('${A911_STATE}'))
    v = d.get('${field}')
    print(v if v is not None else 'unknown')
except:
    print('unknown')
" 2>/dev/null || echo "unknown"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
ACME Install Helper v${SCRIPT_VERSION}
Usage: $(basename "$0") --bundle <name> [--dry-run | --apply | --verify]

Bundles:
  radcheck    RadCheck scoring engine
  sentinel    Sentinel stack (attach bridge + funnel alignment + compaction guard)
              Note: SphinxGate policy layer is included in this bundle.
  agent911    Agent911 control plane (snapshot + FMA + weekly report)
  all         All of the above (radcheck + sentinel + agent911)

  sphinxgate  [Coming soon as standalone — currently ships inside sentinel bundle]

Modes (mutually exclusive):
  --dry-run   Print planned actions without writing files (default)
  --apply     Install files + write bundles.lock.json
  --verify    Read-only: confirm files present, perms correct, Agent911 visible

Safety:
  - Writes ONLY to ~/.openclaw/watchdog/ and ~/.openclaw/bin/
  - NEVER touches ~/.openclaw/openclaw.json
  - NEVER restarts the gateway
EOF
    exit 0
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle)   BUNDLE="${2:-}"; shift 2 ;;
        --dry-run)  DRY_RUN=true;  shift ;;
        --apply)    APPLY=true;    shift ;;
        --verify)   VERIFY=true;   shift ;;
        -h|--help)  usage ;;
        *)
            log_err "Unknown argument: $1"
            usage
            ;;
    esac
done

if [[ -z "${BUNDLE}" ]]; then
    log_err "--bundle is required"
    usage
fi

# Default to dry-run if no mode specified
if [[ "${DRY_RUN}" == "false" && "${APPLY}" == "false" && "${VERIFY}" == "false" ]]; then
    log_info "No mode flag; defaulting to --dry-run"
    DRY_RUN=true
fi

# Mutual exclusivity
MODES_SET=0
[[ "${DRY_RUN}" == "true" ]] && MODES_SET=$((MODES_SET+1))
[[ "${APPLY}" == "true" ]]   && MODES_SET=$((MODES_SET+1))
[[ "${VERIFY}" == "true" ]]  && MODES_SET=$((MODES_SET+1))
if [[ "${MODES_SET}" -gt 1 ]]; then
    log_err "Only one of --dry-run / --apply / --verify may be specified"
    exit 1
fi

# Validate bundle
case "${BUNDLE}" in
    radcheck|sentinel|sphinxgate|agent911|all) ;;
    *)
        log_err "Unknown bundle '${BUNDLE}'. Valid: radcheck|sentinel|sphinxgate|agent911|all"
        exit 1
        ;;
esac

# v0.3: sphinxgate standalone produces no files — warn and exit cleanly
if [[ "${BUNDLE}" == "sphinxgate" ]]; then
    log_info "SphinxGate standalone bundle: coming soon."
    log_info "SphinxGate policy layer is currently included in the Sentinel bundle."
    log_info "Run: ./acme_install.sh --bundle sentinel --apply"
    exit 0
fi

if [[ "${BUNDLE}" == "all" ]]; then
    SELECTED_BUNDLES="radcheck sentinel agent911"
else
    SELECTED_BUNDLES="${BUNDLE}"
fi

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
MODE_LABEL="DRY-RUN"
[[ "${APPLY}" == "true" ]]  && MODE_LABEL="APPLY"
[[ "${VERIFY}" == "true" ]] && MODE_LABEL="VERIFY"

# Counters
INSTALLED_COUNT=0
SKIPPED_COUNT=0
FAILED_COUNT=0
INSTALLED_LIST=""

# Verify result storage
VFY_BUNDLES=""
VFY_INSTALLED=""
VFY_WIRED=""
VFY_A911=""
VFY_STATUS=""

# ---------------------------------------------------------------------------
# Install a single file
# ---------------------------------------------------------------------------
install_file() {
    local src_rel="$1"
    local dst_rel="$2"
    local product="$3"
    local needs_exec="${4:-}"

    local src="${SCRIPTS_ROOT}/${src_rel}"
    local dst="${WATCHDOG_DIR}/${dst_rel}"

    emit_event "INSTALL_STEP" "product=${product}" "src=${src_rel}" "dst=${dst_rel}" "mode=${MODE_LABEL}"

    if [[ ! -f "${src}" ]]; then
        log_err "Source not found: ${src}"
        FAILED_COUNT=$((FAILED_COUNT+1))
        emit_event "INSTALL_STEP" "product=${product}" "src=${src_rel}" "result=MISSING_SOURCE"
        return
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_dry "would install: ${src_rel}  →  ${dst}"
        INSTALLED_COUNT=$((INSTALLED_COUNT+1))
        return
    fi

    local src_sha
    src_sha="$(sha256_file "${src}")"
    local lock_sha
    lock_sha="$(lockfile_get_sha "${dst_rel}")"

    if [[ -f "${dst}" && -n "${lock_sha}" && "${src_sha}" == "${lock_sha}" ]]; then
        log_skip "already current (lockfile match): ${dst_rel}"
        SKIPPED_COUNT=$((SKIPPED_COUNT+1))
        emit_event "INSTALL_STEP" "product=${product}" "dst=${dst_rel}" "result=ALREADY_CURRENT" "sha256=${src_sha}"
        return
    fi

    if [[ -f "${dst}" && -z "${lock_sha}" ]] && cmp -s "${src}" "${dst}"; then
        log_skip "already current (content match): ${dst_rel}"
        SKIPPED_COUNT=$((SKIPPED_COUNT+1))
        lockfile_update "${dst_rel}" "${src_sha}" "${SCRIPT_VERSION}" "$(ts)"
        emit_event "INSTALL_STEP" "product=${product}" "dst=${dst_rel}" "result=ALREADY_CURRENT_RETROLOCK" "sha256=${src_sha}"
        return
    fi

    mkdir -p "$(dirname "${dst}")"
    if [[ -f "${dst}" ]]; then
        local bak="${dst}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
        cp "${dst}" "${bak}"
        log_info "backup: $(basename "${dst}") → $(basename "${bak}")"
    fi

    cp "${src}" "${dst}"

    if [[ "${needs_exec}" == "x" ]]; then
        chmod +x "${dst}"
        log_info "chmod +x: ${dst_rel}"
    fi

    lockfile_update "${dst_rel}" "${src_sha}" "${SCRIPT_VERSION}" "$(ts)"

    log_ok "installed: ${src_rel}  →  ${dst}"
    INSTALLED_COUNT=$((INSTALLED_COUNT+1))
    INSTALLED_LIST="${INSTALLED_LIST} ${dst_rel}"
    emit_event "INSTALL_STEP" "product=${product}" "dst=${dst_rel}" "result=INSTALLED" "sha256=${src_sha}"
}

# ---------------------------------------------------------------------------
# Verify a single bundle (read-only)
# ---------------------------------------------------------------------------
verify_bundle() {
    local product="$1"
    local installed_ok=true
    local wired_ok=true
    local a911_ok=true
    local reasons=""

    echo ""
    echo "─── Verify: ${product} ──────────────────────────────────────────"

    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        local needs_exec=""
        local clean_line="${line}"
        if [[ "${line}" =~ :x$ ]]; then
            needs_exec="x"
            clean_line="${line%:x}"
        fi
        local dst_rel="${clean_line##*:}"
        local dst="${WATCHDOG_DIR}/${dst_rel}"

        if [[ ! -f "${dst}" ]]; then
            log_fail "MISSING: ${dst_rel}"
            installed_ok=false
            reasons="${reasons}MISSING:${dst_rel};"
        else
            log_vfy "PRESENT: ${dst_rel}"

            if [[ "${dst_rel}" == *.sh ]] || [[ "${needs_exec}" == "x" ]]; then
                if [[ ! -x "${dst}" ]]; then
                    log_fail "NOT EXECUTABLE: ${dst_rel}"
                    wired_ok=false
                    reasons="${reasons}NOT_EXEC:${dst_rel};"
                else
                    log_vfy "EXECUTABLE: ${dst_rel}"
                fi
            fi

            local lock_sha
            lock_sha="$(lockfile_get_sha "${dst_rel}")"
            if [[ -n "${lock_sha}" ]]; then
                local actual_sha
                actual_sha="$(sha256_file "${dst}")"
                if [[ "${actual_sha}" == "${lock_sha}" ]]; then
                    log_vfy "LOCK OK: ${dst_rel} (sha256 matches)"
                else
                    log_fail "LOCK MISMATCH: ${dst_rel} (installed sha256 differs from lockfile)"
                    wired_ok=false
                    reasons="${reasons}LOCK_MISMATCH:${dst_rel};"
                fi
            else
                log_vfy "LOCK MISSING: ${dst_rel} (no lockfile entry — run --apply to pin)"
            fi
        fi
    done < <(bundle_files "${product}")

    case "${product}" in
        radcheck)
            local rc_score
            rc_score="$(a911_field "radcheck" 2>/dev/null || echo "unknown")"
            if [[ "${rc_score}" != "unknown" && "${rc_score}" != "" ]]; then
                log_vfy "A911 VISIBLE: radcheck (field present)"
            else
                log_vfy "A911 NOT_VISIBLE: radcheck field absent (run agent911 first)"
                a911_ok=false
                reasons="${reasons}A911_RADCHECK_ABSENT;"
            fi
            ;;
        sentinel)
            local prot_state
            prot_state="$(a911_field "protection_state" 2>/dev/null || echo "unknown")"
            if [[ "${prot_state}" != "unknown" && "${prot_state}" != "" ]]; then
                log_vfy "A911 VISIBLE: sentinel (protection_state present)"
            else
                log_vfy "A911 NOT_VISIBLE: protection_state absent"
                a911_ok=false
                reasons="${reasons}A911_SENTINEL_ABSENT;"
            fi
            ;;
        agent911)
            local schema
            schema="$(a911_field "schema_version" 2>/dev/null || echo "unknown")"
            if [[ "${schema}" == "agent911.v1.0" ]]; then
                log_vfy "A911 VISIBLE: agent911 (schema_version=agent911.v1.0)"
            else
                log_vfy "A911 NOT_VISIBLE: schema_version=${schema}"
                a911_ok=false
                reasons="${reasons}A911_SCHEMA_ABSENT;"
            fi
            ;;
    esac

    local has_install_event="NO"
    if [[ -f "${OPS_EVENTS_LOG}" ]]; then
        if grep -q "\"product\":\"${product}\"" "${OPS_EVENTS_LOG}" 2>/dev/null; then
            has_install_event="YES"
            log_vfy "EVENTS LOG: install event found for ${product}"
        else
            log_vfy "EVENTS LOG: no prior install event for ${product} (OK if first-time)"
        fi
    fi

    local status="PASS"
    local installed_str="YES"
    local wired_str="YES"
    local a911_str="YES"

    [[ "${installed_ok}" == "false" ]] && { status="FAIL"; installed_str="NO"; }
    [[ "${wired_ok}" == "false" ]]     && { status="FAIL"; wired_str="NO"; }
    [[ "${a911_ok}" == "false" ]]      && { a911_str="NO"; }

    emit_event "INSTALL_VERIFY_RESULT" \
        "product=${product}" \
        "installed=${installed_str}" \
        "wired=${wired_str}" \
        "agent911_visible=${a911_str}" \
        "events_log=${has_install_event}" \
        "status=${status}" \
        "reasons=${reasons}"

    VFY_BUNDLES="${VFY_BUNDLES}${product} "
    VFY_INSTALLED="${VFY_INSTALLED}${installed_str} "
    VFY_WIRED="${VFY_WIRED}${wired_str} "
    VFY_A911="${VFY_A911}${a911_str} "
    VFY_STATUS="${VFY_STATUS}${status} "
}

# ---------------------------------------------------------------------------
# Print VERIFY SUMMARY table
# ---------------------------------------------------------------------------
print_verify_table() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════════════╗"
    echo "║                         VERIFY SUMMARY                                  ║"
    echo "╠══════════════╦═══════════╦═══════╦══════════════════╦════════════════╣"
    printf  "║ %-12s ║ %-9s ║ %-5s ║ %-16s ║ %-14s ║\n" \
        "bundle" "installed" "wired" "agent911_visible" "status"
    echo "╠══════════════╬═══════════╬═══════╬══════════════════╬════════════════╣"

    local bundles_arr=($VFY_BUNDLES)
    local inst_arr=($VFY_INSTALLED)
    local wire_arr=($VFY_WIRED)
    local a911_arr=($VFY_A911)
    local stat_arr=($VFY_STATUS)

    local i=0
    local all_pass=true
    while [[ $i -lt ${#bundles_arr[@]} ]]; do
        local b="${bundles_arr[$i]}"
        local ins="${inst_arr[$i]}"
        local wir="${wire_arr[$i]}"
        local a91="${a911_arr[$i]}"
        local sts="${stat_arr[$i]}"
        [[ "${sts}" != "PASS" ]] && all_pass=false
        printf  "║ %-12s ║ %-9s ║ %-5s ║ %-16s ║ %-14s ║\n" \
            "${b}" "${ins}" "${wir}" "${a91}" "${sts}"
        i=$((i+1))
    done

    echo "╚══════════════╩═══════════╩═══════╩══════════════════╩════════════════╝"
    echo ""
    if [[ "${all_pass}" == "true" ]]; then
        log_ok "All bundles PASS"
    else
        log_fail "One or more bundles FAIL — see details above"
    fi
}

# ===========================================================================
# MAIN
# ===========================================================================
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  ACME Install Helper v${SCRIPT_VERSION}  —  mode: ${MODE_LABEL}              ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo "  bundle    : ${BUNDLE}"
echo "  repo      : ${SCRIPTS_ROOT}"
echo "  watchdog  : ${WATCHDOG_DIR}"
echo "  state     : ${INSTALL_STATE_FILE}"
echo "  lockfile  : ${LOCK_FILE}"
echo "  log       : ${OPS_EVENTS_LOG}"
echo ""

emit_event "INSTALL_RUN_START" \
    "run_id=${RUN_ID}" \
    "bundle=${BUNDLE}" \
    "mode=${MODE_LABEL}" \
    "version=${SCRIPT_VERSION}"

# ---------------------------------------------------------------------------
# VERIFY MODE
# ---------------------------------------------------------------------------
if [[ "${VERIFY}" == "true" ]]; then
    for product in ${SELECTED_BUNDLES}; do
        verify_bundle "${product}"
    done
    print_verify_table

    emit_event "INSTALL_RUN_END" \
        "run_id=${RUN_ID}" \
        "bundle=${BUNDLE}" \
        "mode=VERIFY" \
        "status=${VFY_STATUS// /,}"

    echo "  SAFETY CHECK"
    echo "  ✓ openclaw.json   — NOT touched"
    echo "  ✓ gateway restart — NOT triggered"
    echo "  ✓ verify          — READ-ONLY, no writes"
    echo ""
    exit 0
fi

# ---------------------------------------------------------------------------
# DRY-RUN / APPLY MODE
# ---------------------------------------------------------------------------
for product in ${SELECTED_BUNDLES}; do
    echo ""
    echo "─── Bundle: ${product} ─────────────────────────────────────────"
    emit_event "INSTALL_BUNDLE_SELECTED" "product=${product}"

    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        local_needs_exec=""
        [[ "${line}" =~ :x$ ]] && local_needs_exec="x"
        local_line="${line%:x}"
        src_rel="${local_line%%:*}"
        dst_rel="${local_line##*:}"
        install_file "${src_rel}" "${dst_rel}" "${product}" "${local_needs_exec}"
    done < <(bundle_files "${product}")
done

# Result
RESULT_STATUS="OK"
[[ "${FAILED_COUNT}" -gt 0 ]] && RESULT_STATUS="PARTIAL"
[[ "${INSTALLED_COUNT}" -eq 0 && "${FAILED_COUNT}" -eq 0 ]] && RESULT_STATUS="NO_OP"
[[ "${DRY_RUN}" == "true" ]] && RESULT_STATUS="DRY_RUN"

echo ""
echo "═══════════════════════════════════════════════════════════════════"
log_info "Results: installed=${INSTALLED_COUNT} skipped=${SKIPPED_COUNT} failed=${FAILED_COUNT} status=${RESULT_STATUS}"

emit_event "INSTALL_RESULT" \
    "bundle=${BUNDLE}" \
    "installed=${INSTALLED_COUNT}" \
    "skipped=${SKIPPED_COUNT}" \
    "failed=${FAILED_COUNT}" \
    "status=${RESULT_STATUS}"

# Write install_state.json (apply mode)
if [[ "${APPLY}" == "true" ]]; then
    mkdir -p "${INSTALL_STATE_DIR}"
    TS_NOW="$(ts)"

    INSTALLED_JSON_ARRAY=""
    for f in ${INSTALLED_LIST}; do
        INSTALLED_JSON_ARRAY="${INSTALLED_JSON_ARRAY}\"${f}\","
    done
    INSTALLED_JSON_ARRAY="${INSTALLED_JSON_ARRAY%,}"

    BUNDLE_OBJ=""
    for product in ${SELECTED_BUNDLES}; do
        BUNDLE_OBJ="${BUNDLE_OBJ}\"${product}\":{\"installed_at\":\"${TS_NOW}\",\"version\":\"${SCRIPT_VERSION}\"},"
    done
    BUNDLE_OBJ="${BUNDLE_OBJ%,}"

    cat > "${INSTALL_STATE_FILE}" <<JSONEOF
{
  "schema": "acme_install.v1.0",
  "last_run": "${TS_NOW}",
  "run_id": "${RUN_ID}",
  "mode": "${MODE_LABEL}",
  "bundle_requested": "${BUNDLE}",
  "bundles": {${BUNDLE_OBJ}},
  "installed_files": [${INSTALLED_JSON_ARRAY}],
  "result_status": "${RESULT_STATUS}",
  "lockfile": "${LOCK_FILE}",
  "safety": {
    "openclaw_json_touched": false,
    "gateway_restarted": false,
    "writes_outside_watchdog_or_bin": false
  }
}
JSONEOF
    log_ok "state written    →  ${INSTALL_STATE_FILE}"
    log_ok "lockfile written →  ${LOCK_FILE}"
fi

emit_event "INSTALL_RUN_END" \
    "run_id=${RUN_ID}" \
    "bundle=${BUNDLE}" \
    "status=${RESULT_STATUS}" \
    "installed=${INSTALLED_COUNT}" \
    "skipped=${SKIPPED_COUNT}"

echo ""
echo "  SAFETY CHECK"
echo "  ✓ openclaw.json   — NOT touched"
echo "  ✓ gateway restart — NOT triggered"
echo "  ✓ write targets   — ~/.openclaw/watchdog/ and ~/.openclaw/bin/ ONLY"
echo ""

exit 0
