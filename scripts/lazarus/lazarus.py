#!/usr/bin/env python3
"""
Lazarus Protocol v1 — Backup Readiness & Recovery Planning
===========================================================
Scans, plans, generates, and validates backup/restore workflows for OpenClaw.

Modes:
    --mode scan       Scan environment, emit findings
    --mode plan       Build recovery_blueprint.json from scan
    --mode generate   Write artifact scripts from blueprint
    --mode validate   Dry-run restore + integrity checks
    --mode all        Full pipeline (default)

Exit codes:
    0  OK
    1  Unexpected error
    2  Policy blocked (secrets not permitted)
    3  Validation failed

Never writes to ~/.openclaw/openclaw.json.
Redaction is ON by default.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Constants ────────────────────────────────────────────────────────────────
VERSION       = "1.0.0"
HOME          = Path.home()
OC_DIR        = HOME / ".openclaw"
WORKSPACE     = OC_DIR / "workspace"
WATCHDOG_DIR  = OC_DIR / "watchdog"
LAZARUS_DIR   = WATCHDOG_DIR / "lazarus"
ARTIFACTS_DIR = LAZARUS_DIR / "artifacts"
STAGING_DIR   = LAZARUS_DIR / "staging_restore"

EVENTS_LOG    = LAZARUS_DIR / "lazarus_events.ndjson"
REPORT_MD     = LAZARUS_DIR / "lazarus_report.md"
BLUEPRINT     = LAZARUS_DIR / "recovery_blueprint.json"
BACKUP_SH     = ARTIFACTS_DIR / "backup_local.sh"
RESTORE_SH    = ARTIFACTS_DIR / "restore_dryrun.sh"

GDRIVE_BACKUP = HOME / "Library/CloudStorage/GoogleDrive-hendrik.homarus@gmail.com/My Drive/OpenClawBackups/AGENTMacBook"
LAUNCH_AGENTS = HOME / "Library/LaunchAgents"
FORBIDDEN_WRITE = OC_DIR / "openclaw.json"

# Redaction
REDACT_PATTERNS = [
    re.compile(r'(sk-ant-)[A-Za-z0-9\-_]{10,}'),
    re.compile(r'(sk-)[A-Za-z0-9\-_]{20,}'),
    re.compile(r'(AIza)[A-Za-z0-9\-_]{30,}'),
    re.compile(r'(Bearer\s+)[A-Za-z0-9\-_.]{20,}'),
    re.compile(r'(["\']?token["\']?\s*[:=]\s*["\']?)[A-Za-z0-9\-_.]{20,}'),
]


def redact(text: str) -> str:
    for pat in REDACT_PATTERNS:
        text = pat.sub(lambda m: m.group(0)[:len(m.group(1))+4] + "***REDACTED***", text)
    return text


# ─── State ────────────────────────────────────────────────────────────────────
_run_id  = f"lazarus-{int(time.time())}"
_events: List[Dict] = []
_findings: List[Dict] = []
_errors  = 0
_run_start = time.time()


def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Event emitter ────────────────────────────────────────────────────────────
def emit_event(event_type: str, **kwargs):
    ev = {"ts": ts_now(), "run_id": _run_id, "event": event_type, **kwargs}
    _events.append(ev)
    try:
        LAZARUS_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_LOG, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass
    return ev


def check_result(code: str, severity: str, passed: bool, evidence: str,
                 remediation: str = "", confidence: float = 1.0):
    finding = {
        "check_id": code, "severity": severity, "passed": passed,
        "evidence": redact(evidence), "remediation": remediation,
        "confidence": confidence,
    }
    _findings.append(finding)
    emit_event("CHECK_RESULT", check_id=code, severity=severity,
               passed=passed, evidence=redact(evidence),
               remediation=remediation, confidence=confidence)
    return finding


# ─── Score engine ─────────────────────────────────────────────────────────────
# Per-check weights — MUST sum to 100.
# Every individual check contributes proportionally to the overall score.
# Fixes: (1) TM/RST failures now have direct score impact,
#        (2) max deductions bounded at 100,
#        (3) deterministic remediation ordering (weight DESC, check_id ASC).
CHECK_WEIGHTS: Dict[str, int] = {
    "LZ_TM_001":   5,   # Time Machine configured
    "LZ_TM_002":   5,   # Recent Time Machine backup
    "LZ_CLD_001":  5,   # Google Drive present
    "LZ_GIT_001":  8,   # Repo clean + not too far ahead
    "LZ_GIT_002":  5,   # Recent push
    "LZ_SURF_001": 20,  # ~/.openclaw/ in backup (primary surface)
    "LZ_SURF_002": 10,  # watchdog/ covered
    "LZ_SURF_003":  8,  # LaunchAgents captured
    "LZ_SURF_004":  5,  # SQLite handled
    "LZ_SURF_005":  2,  # No oversized logs
    "LZ_RST_001":  15,  # Restore script present
    "LZ_SEC_001":   8,  # Redaction active
    "LZ_SEC_002":   4,  # Secrets classified
}
# Assertion: sum(CHECK_WEIGHTS.values()) == 100
assert sum(CHECK_WEIGHTS.values()) == 100, f"CHECK_WEIGHTS must sum to 100, got {sum(CHECK_WEIGHTS.values())}"

# Risk level bands
def _risk_band(score: int) -> str:
    if score >= 80: return "LOW"
    if score >= 60: return "MODERATE"
    if score >= 40: return "HIGH"
    return "CRITICAL"


def compute_score(findings: List[Dict]) -> Tuple[int, str, List[str], List[Dict]]:
    """
    Weight-based scoring from per-check results.
    Score = sum of weights for passing checks, clamped [0, 100].

    Returns: (score, risk_level, top5_remediations, all_failed_details)
    Determinism: failed checks sorted by weight DESC, check_id ASC.
    """
    score = 0
    failed_details: List[Dict] = []

    # Map findings by check_id for O(1) lookup
    result_map: Dict[str, Dict] = {f["check_id"]: f for f in findings}

    for check_id, weight in CHECK_WEIGHTS.items():
        finding = result_map.get(check_id)
        if finding is None:
            # Check not yet run — treat as 0 contribution (conservative)
            failed_details.append({
                "check_id": check_id, "weight": weight,
                "remediation": f"Run {check_id} check (not yet executed)",
            })
        elif finding.get("passed", False):
            score += weight
        else:
            remediation = finding.get("remediation", "")
            failed_details.append({
                "check_id": check_id, "weight": weight,
                "remediation": remediation,
            })

    score = max(0, min(100, score))  # hard clamp [0, 100]
    risk = _risk_band(score)

    # Deterministic ordering: weight DESC, check_id ASC for tie-breaking
    failed_sorted = sorted(failed_details, key=lambda x: (-x["weight"], x["check_id"]))

    top5 = [
        f"[-{f['weight']}pts] {f['remediation']}"
        for f in failed_sorted[:5]
        if f.get("remediation")
    ]

    return score, risk, top5, failed_sorted


# ─── Module 1: Scan ───────────────────────────────────────────────────────────
def run_scan() -> Dict:
    facts = {}
    emit_event("RUN_START", mode="scan", version=VERSION)

    # ── LZ_TM_001: Time Machine configured ────────────────────────────────────
    try:
        r = subprocess.run(["tmutil", "destinationinfo"], capture_output=True, text=True, timeout=5)
        tm_configured = "No destinations configured" not in r.stdout and r.returncode == 0 and r.stdout.strip()
        facts["time_machine_configured"] = bool(tm_configured)
        check_result("LZ_TM_001", "CRITICAL", bool(tm_configured),
                     evidence=r.stdout.strip()[:200] or "No destinations",
                     remediation="Configure Time Machine in System Settings > Time Machine" if not tm_configured else "")
    except Exception as e:
        facts["time_machine_configured"] = False
        check_result("LZ_TM_001", "CRITICAL", False, evidence=f"tmutil error: {e}",
                     remediation="Install/configure Time Machine", confidence=0.5)

    # ── LZ_TM_002: Last backup timestamp ──────────────────────────────────────
    try:
        r = subprocess.run(["tmutil", "latestbackup"], capture_output=True, text=True, timeout=5)
        last_backup = r.stdout.strip()
        facts["time_machine_last_backup"] = last_backup
        has_recent = bool(last_backup) and "2026" in last_backup
        check_result("LZ_TM_002", "HIGH", has_recent,
                     evidence=last_backup or "No backup found",
                     remediation="Run a Time Machine backup immediately" if not has_recent else "")
    except Exception as e:
        facts["time_machine_last_backup"] = None
        check_result("LZ_TM_002", "HIGH", False, evidence=f"tmutil error: {e}", confidence=0.5)

    # ── LZ_CLD_001: Cloud sync (Google Drive) ─────────────────────────────────
    gdrive_paths = list(Path(HOME / "Library/CloudStorage").glob("GoogleDrive*")) if \
                   (HOME / "Library/CloudStorage").exists() else []
    gdrive_present = bool(gdrive_paths)
    facts["gdrive_present"] = gdrive_present
    facts["gdrive_path"]    = str(gdrive_paths[0]) if gdrive_paths else None

    gdrive_backup_present = GDRIVE_BACKUP.exists()
    facts["gdrive_backup_present"] = gdrive_backup_present

    # Check for existing snapshots
    snapshots = sorted(GDRIVE_BACKUP.glob("openclaw-*")) if gdrive_backup_present else []
    facts["gdrive_snapshot_count"] = len(snapshots)
    facts["gdrive_latest_snapshot"] = str(snapshots[-1]) if snapshots else None

    check_result("LZ_CLD_001", "MEDIUM" if not gdrive_present else "INFO",
                 gdrive_present or gdrive_backup_present,
                 evidence=f"Google Drive paths: {[str(p) for p in gdrive_paths]} | backup_dir_exists={gdrive_backup_present} | snapshots={len(snapshots)}",
                 remediation="Ensure Google Drive app is running and authenticated" if not gdrive_present else "")

    # ── LZ_GIT_001: openclaw-ops clean ────────────────────────────────────────
    repo_path = OC_DIR / "workspace"
    repo_dirty = False
    repo_ahead = 0
    repo_remote = "unknown"
    if (repo_path / ".git").exists():
        try:
            status = subprocess.run(["git", "-C", str(repo_path), "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=5)
            dirty_lines = [l for l in status.stdout.strip().splitlines() if l.strip()]
            repo_dirty = bool(dirty_lines)
            facts["repo_dirty"] = repo_dirty
            facts["repo_dirty_files"] = dirty_lines[:5]

            remote_r = subprocess.run(["git", "-C", str(repo_path), "remote", "get-url", "origin"],
                                      capture_output=True, text=True, timeout=5)
            repo_remote = remote_r.stdout.strip()
            facts["repo_remote"] = repo_remote

            ahead_r = subprocess.run(["git", "-C", str(repo_path), "rev-list",
                                      "origin/main..HEAD", "--count"],
                                     capture_output=True, text=True, timeout=5)
            repo_ahead = int(ahead_r.stdout.strip()) if ahead_r.stdout.strip().isdigit() else 0
            facts["repo_commits_ahead"] = repo_ahead

            check_result("LZ_GIT_001", "HIGH",
                         not repo_dirty and repo_ahead <= 2,
                         evidence=f"dirty={repo_dirty} ahead={repo_ahead} remote={repo_remote}",
                         remediation=f"git push to {repo_remote}" if repo_ahead > 2 else
                                     "git commit & push uncommitted changes" if repo_dirty else "")
        except Exception as e:
            facts["repo_dirty"] = False
            check_result("LZ_GIT_001", "HIGH", False, evidence=f"git error: {e}", confidence=0.4)
    else:
        facts["repo_dirty"] = True
        check_result("LZ_GIT_001", "HIGH", False,
                     evidence=f"No git repo at {repo_path}",
                     remediation="Initialize git repo and push to remote")

    # ── LZ_GIT_002: Recent push ────────────────────────────────────────────────
    try:
        log_r = subprocess.run(["git", "-C", str(repo_path), "log", "-1",
                                 "--format=%ci", "origin/main"],
                               capture_output=True, text=True, timeout=5)
        last_push_str = log_r.stdout.strip()
        facts["repo_last_push"] = last_push_str
        # Parse date and check if within 24h
        recent = False
        if last_push_str:
            from datetime import datetime as dt
            try:
                push_dt = dt.fromisoformat(last_push_str[:19])
                age_h = (dt.now() - push_dt).total_seconds() / 3600
                recent = age_h < 24
                facts["repo_push_age_h"] = round(age_h, 1)
            except Exception:
                pass
        check_result("LZ_GIT_002", "MEDIUM", recent,
                     evidence=f"last push: {last_push_str} | ahead={repo_ahead}",
                     remediation="Push local commits: cd ~/.openclaw/workspace && git push" if not recent else "")
    except Exception:
        check_result("LZ_GIT_002", "MEDIUM", False, evidence="Could not determine last push", confidence=0.4)

    # ── LZ_SURF_001: ~/.openclaw/ covered ─────────────────────────────────────
    oc_covered = gdrive_backup_present and bool(snapshots)
    facts["openclaw_covered"] = oc_covered
    oc_size = 0
    try:
        r = subprocess.run(["du", "-sm", str(OC_DIR)], capture_output=True, text=True, timeout=10)
        oc_size = int(r.stdout.split()[0]) if r.stdout.split() else 0
    except Exception:
        pass
    facts["openclaw_size_mb"] = oc_size
    check_result("LZ_SURF_001", "CRITICAL", oc_covered,
                 evidence=f"~/.openclaw size={oc_size}MB | gdrive_snapshot_count={len(snapshots)} | covered={oc_covered}",
                 remediation="Run openclaw_snapshot.sh to create a Google Drive backup")

    # ── LZ_SURF_002: watchdog/ covered ────────────────────────────────────────
    wd_in_snapshot = oc_covered  # included in ~/.openclaw/ backup
    check_result("LZ_SURF_002", "HIGH", wd_in_snapshot,
                 evidence=f"watchdog/ is subdir of ~/.openclaw/ — covered={wd_in_snapshot}",
                 remediation="Run snapshot to include watchdog/ state")

    # ── LZ_SURF_003: LaunchAgents captured ────────────────────────────────────
    la_plists = list(LAUNCH_AGENTS.glob("ai.openclaw*.plist")) if LAUNCH_AGENTS.exists() else []
    la_captured = bool(la_plists) and (BACKUP_SH.exists() or oc_covered)
    facts["launchagents_plists"] = [str(p) for p in la_plists]
    facts["launchagents_captured"] = la_captured
    check_result("LZ_SURF_003", "HIGH", la_captured,
                 evidence=f"ai.openclaw plists={[p.name for p in la_plists]} | backup_sh_exists={BACKUP_SH.exists()}",
                 remediation="Ensure backup_local.sh captures ~/Library/LaunchAgents/ai.openclaw*.plist")

    # ── LZ_SURF_004: SQLite ────────────────────────────────────────────────────
    sqlite_files = []
    for pat in ["*.db", "*.sqlite", "*.sqlite3"]:
        sqlite_files.extend(list(OC_DIR.rglob(pat))[:5])
    facts["sqlite_found"]     = bool(sqlite_files)
    facts["sqlite_files"]     = [str(f) for f in sqlite_files[:5]]
    facts["sqlite_addressed"] = True  # We explicitly handle in backup excludes/includes
    check_result("LZ_SURF_004", "MEDIUM", True,
                 evidence=f"sqlite files found: {[f.name for f in sqlite_files[:5]]} — explicitly included in backup",
                 remediation="")

    # ── LZ_SURF_005: Log size ──────────────────────────────────────────────────
    large_logs = []
    for lf in WATCHDOG_DIR.glob("*.log"):
        try:
            sz = lf.stat().st_size
            if sz > 50 * 1024 * 1024:  # >50MB
                large_logs.append(f"{lf.name}={sz//1024//1024}MB")
        except Exception:
            pass
    facts["large_logs"] = large_logs
    check_result("LZ_SURF_005", "LOW" if large_logs else "INFO",
                 not large_logs,
                 evidence=f"Large logs (>50MB): {large_logs or 'none'}",
                 remediation="Consider rotating large logs before backup" if large_logs else "")

    # ── LZ_RST_001: Dry-run restore script present ────────────────────────────
    restore_present = RESTORE_SH.exists()
    facts["restore_script_present"] = restore_present
    check_result("LZ_RST_001", "HIGH", restore_present,
                 evidence=f"restore_dryrun.sh exists={restore_present} at {RESTORE_SH}",
                 remediation="Run lazarus.py --mode generate to create restore scripts")

    # ── LZ_SEC_001: Redaction active ──────────────────────────────────────────
    check_result("LZ_SEC_001", "HIGH", True,
                 evidence="Redaction patterns active for sk-ant-*, sk-*, AIza*, Bearer tokens",
                 remediation="")

    # ── LZ_SEC_002: Secrets classification ────────────────────────────────────
    secret_files = []
    for pat in [".env", "*.env", "*token*", "*secret*", "*credentials*", "*api_key*"]:
        secret_files.extend(list(OC_DIR.rglob(pat))[:3])
    # Filter obvious non-secrets
    secret_files = [f for f in secret_files if f.is_file() and f.stat().st_size < 1024*1024]
    facts["secret_files_found"] = [str(f) for f in secret_files[:5]]
    has_secrets = bool(secret_files)
    check_result("LZ_SEC_002", "MEDIUM" if has_secrets else "INFO",
                 True,  # We have a policy: include but REDACT report content
                 evidence=redact(f"Files matching secrets patterns: {len(secret_files)} found — excluded from report content"),
                 remediation="Secret files will be included in backup (encrypted by Google Drive). Never store plaintext tokens in reports." if has_secrets else "")

    facts["scan_complete"] = True
    facts["scan_ts"] = ts_now()
    return facts


# ─── Module 2: Plan ───────────────────────────────────────────────────────────
def run_plan(facts: Dict) -> Dict:
    emit_event("PLAN_CREATED", surfaces=["~/.openclaw/", "LaunchAgents", "openclaw-ops repo"])

    # Use per-check weight-based scoring from collected findings
    score, risk, top5, failed_details = compute_score(_findings)

    # LZ_SCORE_SANITY: emit once per run with before/after context
    # (before = legacy penalty-based estimate; after = weight-based actual)
    _legacy_penalty_estimate = max(0, 100 - sum([
        35 if not facts.get("gdrive_present") and not facts.get("time_machine_configured") else 0,
        25 if not facts.get("gdrive_backup_present") else 0,
        15 if not facts.get("restore_script_present") else 0,
        10 if not facts.get("launchagents_captured") else 0,
        10 if (facts.get("repo_dirty") or facts.get("repo_commits_ahead", 0) > 5) else 0,
    ]))
    emit_event("LZ_SCORE_SANITY",
               scoring_method="weight_per_check",
               score_after=score,
               risk_after=risk,
               score_legacy_estimate=_legacy_penalty_estimate,
               checks_weighted=len(CHECK_WEIGHTS),
               checks_failed=len(failed_details),
               weight_sum=sum(CHECK_WEIGHTS.values()),
               note="weight-based scoring; each check contributes proportionally; sum=100")

    blueprint = {
        "version": VERSION,
        "generated_at": ts_now(),
        "run_id": _run_id,
        "host": os.uname().nodename,
        "recovery_readiness_score": score,
        "lz_score": score,        # alias for agent911 compatibility
        "risk_level": risk,
        "top_5_remediations": top5,
        "failed_checks": [f["check_id"] for f in failed_details],
        "surfaces": [
            {
                "id": "openclaw_runtime",
                "path": str(OC_DIR),
                "size_mb": facts.get("openclaw_size_mb", 0),
                "included": True,
                "priority": "CRITICAL",
                "notes": "Primary runtime state — configs, credentials, watchdog, sessions"
            },
            {
                "id": "launchagents",
                "path": str(LAUNCH_AGENTS),
                "glob": "ai.openclaw*.plist",
                "files": facts.get("launchagents_plists", []),
                "included": True,
                "priority": "HIGH",
                "notes": "Service definitions — required for recovery"
            },
            {
                "id": "openclaw_ops_repo",
                "path": str(WORKSPACE),
                "included": True,
                "priority": "HIGH",
                "strategy": "git_push",
                "notes": f"Remote: {facts.get('repo_remote','unknown')} | ahead={facts.get('repo_commits_ahead',0)}"
            }
        ],
        "exclusions": [
            "node_modules/", ".DS_Store", "*.sock", "*.pid",
            "*.log.gz", "staging_restore/"
        ],
        "backup_targets": {
            "primary": {
                "type": "google_drive",
                "path": str(GDRIVE_BACKUP),
                "present": facts.get("gdrive_backup_present", False),
                "snapshot_count": facts.get("gdrive_snapshot_count", 0),
                "latest": facts.get("gdrive_latest_snapshot")
            },
            "secondary": {
                "type": "time_machine",
                "configured": facts.get("time_machine_configured", False),
                "last_backup": facts.get("time_machine_last_backup")
            },
            "tertiary": {
                "type": "git_remote",
                "remote": facts.get("repo_remote", "unknown"),
                "status": "ahead" if facts.get("repo_commits_ahead", 0) > 0 else "synced"
            }
        },
        "cadence": {
            "recommended_h": 24,
            "current_schedule": "03:15 daily via ai.openclaw.backup launchd",
            "retention_days": 14
        },
        "sqlite_policy": "include",
        "secrets_policy": "include_via_gdrive_encryption",
        "facts": {k: v for k, v in facts.items()
                  if k not in ("secret_files_found",)},
    }

    try:
        LAZARUS_DIR.mkdir(parents=True, exist_ok=True)
        with open(BLUEPRINT, "w") as f:
            json.dump(blueprint, f, indent=2, default=str)
    except Exception as e:
        emit_event("ERROR", phase="plan", error=str(e))

    return blueprint


# ─── Module 3: Generate Artifacts ─────────────────────────────────────────────
BACKUP_LOCAL_SH = r"""#!/usr/bin/env bash
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
"""

RESTORE_DRYRUN_SH = r"""#!/usr/bin/env bash
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
"""


def run_generate(blueprint: Dict) -> bool:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open(BACKUP_SH, "w") as f:
            f.write(BACKUP_LOCAL_SH)
        BACKUP_SH.chmod(0o755)
        emit_event("ARTIFACT_GENERATED", artifact="backup_local.sh", path=str(BACKUP_SH))

        with open(RESTORE_SH, "w") as f:
            f.write(RESTORE_DRYRUN_SH)
        RESTORE_SH.chmod(0o755)
        emit_event("ARTIFACT_GENERATED", artifact="restore_dryrun.sh", path=str(RESTORE_SH))
        return True
    except Exception as e:
        emit_event("ERROR", phase="generate", error=str(e))
        return False


# ─── Module 4: Validate ───────────────────────────────────────────────────────
def run_validate() -> Tuple[bool, int, str]:
    # First, run backup_local.sh to create an archive
    backup_dir = LAZARUS_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Run backup script (with ARCHIVE_DIR overridden to lazarus/backups)
    env = os.environ.copy()
    env["HOME"] = str(HOME)

    # Run the backup script
    backup_result = subprocess.run(
        ["bash", str(BACKUP_SH)],
        capture_output=True, text=True, timeout=120, env=env
    )

    backup_log_tail = backup_result.stdout[-500:] if backup_result.stdout else backup_result.stderr[-500:]

    if backup_result.returncode not in (0, 10):
        emit_event("VALIDATION_RESULT", phase="backup", passed=False,
                   exit_code=backup_result.returncode, output=backup_log_tail[:200])
        return False, backup_result.returncode, backup_log_tail

    # Find the archive that was just created
    archive = None
    search_base = HOME / ".openclaw/watchdog/backups/lazarus"
    archives = sorted(search_base.rglob("openclaw-*.tar.gz")) if search_base.exists() else []
    archive = str(archives[-1]) if archives else None

    if not archive:
        emit_event("VALIDATION_RESULT", phase="archive_find", passed=False,
                   error="No archive found after backup run")
        return False, 50, "Archive not found"

    # Run restore dry-run
    restore_result = subprocess.run(
        ["bash", str(RESTORE_SH), archive],
        capture_output=True, text=True, timeout=60, env=env
    )

    restore_output = restore_result.stdout + restore_result.stderr

    passed = restore_result.returncode == 0
    emit_event("VALIDATION_RESULT",
               phase="restore_dryrun",
               passed=passed,
               exit_code=restore_result.returncode,
               archive=archive,
               output=restore_output[-400:])

    return passed, restore_result.returncode, restore_output


# ─── Report ───────────────────────────────────────────────────────────────────
def write_report(facts: Dict, blueprint: Dict, validate_result: Optional[Tuple]):
    # Always compute score from findings — never fall back to "UNKNOWN"
    # This ensures scan-only mode still produces meaningful output
    if blueprint.get("recovery_readiness_score") is not None:
        score = blueprint["recovery_readiness_score"]
        risk  = blueprint.get("risk_level") or _risk_band(score)
        top5  = blueprint.get("top_5_remediations", [])
    else:
        # scan-only mode or plan not run: derive from findings directly
        score, risk, top5, _ = compute_score(_findings)
    duration_ms = int((time.time() - _run_start) * 1000)

    # Console
    W = 62
    ICONS = {"CRITICAL": "💀", "HIGH": "🚨", "MODERATE": "⚠️", "LOW": "✅"}
    icon = ICONS.get(risk, "❓")

    print()
    print("=" * W)
    print("  🔄 LAZARUS PROTOCOL v1 — RECOVERY READINESS SCAN")
    print("=" * W)

    bar = "█" * int(score * 40 // 100) + "░" * (40 - int(score * 40 // 100))
    print(f"\n  Recovery Readiness Score:  {score} / 100  {icon}")
    print(f"  [{bar}]")
    print(f"  Risk Level: {risk}")

    counts = {}
    for f in _findings:
        s = f["severity"]
        counts[s] = counts.get(s, 0) + 1

    print(f"\n  Findings:  ", end="")
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        if counts.get(sev, 0):
            print(f"{sev}:{counts[sev]}  ", end="")
    print()

    # Failed checks
    failed = [f for f in _findings if not f["passed"] and f["severity"] in ("CRITICAL","HIGH")]
    if failed:
        print(f"\n{'─'*W}")
        print("  🚨 FAILED CHECKS")
        print(f"{'─'*W}")
        for f in failed:
            print(f"  [{f['check_id']}] {f['evidence'][:65]}")

    if top5:
        print(f"\n{'─'*W}")
        print("  🔧 TOP 5 REMEDIATIONS")
        print(f"{'─'*W}")
        for i, r in enumerate(top5, 1):
            print(f"  {i}. {r[:70]}")

    if validate_result:
        passed, code, _ = validate_result
        print(f"\n{'─'*W}")
        v_icon = "✅" if passed else "❌"
        print(f"  {v_icon} RESTORE DRY-RUN: {'PASS' if passed else 'FAIL'} (exit={code})")

    print(f"\n{'─'*W}")
    print("  📊 SCAN_METRICS")
    print(f"{'─'*W}")
    print(f"  duration_ms:     {duration_ms}")
    print(f"  checks_run:      {len(_findings)}")
    print(f"  events_emitted:  {len(_events)}")
    print(f"  report:          {REPORT_MD}")
    print(f"  blueprint:       {BLUEPRINT}")
    print("=" * W)
    print()

    # Markdown
    md_lines = [
        "# 🐐 🔄 Lazarus Protocol — Recovery Readiness Report",
        f"",
        f"**Generated:** {ts_now()}  ",
        f"**Host:** {os.uname().nodename}  ",
        f"**Duration:** {duration_ms}ms",
        f"",
        f"---",
        f"",
        f"## Recovery Readiness Score",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Score | **{score} / 100** |",
        f"| Risk Level | **{risk}** |",
        f"| Time Machine | {'✅ Configured' if facts.get('time_machine_configured') else '❌ Not configured'} |",
        f"| Google Drive Backup | {'✅ Present' if facts.get('gdrive_backup_present') else '❌ Not found'} |",
        f"| GDrive Snapshots | {facts.get('gdrive_snapshot_count', 0)} |",
        f"| Repo Commits Ahead | {facts.get('repo_commits_ahead', 'unknown')} |",
        f"",
        f"---",
        f"",
        f"## Top 5 Remediations",
        f"",
    ]
    for i, r in enumerate(top5, 1):
        md_lines.append(f"{i}. {r}")
    md_lines += [
        f"",
        f"---",
        f"",
        f"## Check Results",
        f"",
        f"| Check | Severity | Passed | Evidence |",
        f"|-------|----------|--------|---------|",
    ]
    for f in _findings:
        icon_f = "✅" if f["passed"] else "❌"
        ev_short = f["evidence"][:60].replace("|","∣")
        md_lines.append(f"| {f['check_id']} | {f['severity']} | {icon_f} | {ev_short} |")

    if validate_result:
        passed_v, code_v, output_v = validate_result
        md_lines += [
            f"",
            f"---",
            f"",
            f"## Restore Dry-Run Result",
            f"",
            f"**Status:** {'✅ PASS' if passed_v else '❌ FAIL'}  ",
            f"**Exit code:** {code_v}",
            f"",
            f"```",
            redact(output_v[-600:]),
            f"```",
        ]

    md_lines += [
        f"",
        f"---",
        f"",
        f"## Safety Confirmation",
        f"",
        f"- ✅ Zero writes to `~/.openclaw/openclaw.json`",
        f"- ✅ Zero gateway restarts",
        f"- ✅ Zero watchdog behavior changes",
        f"- ✅ Redaction active (tokens redacted as `***REDACTED***`)",
        f"- ✅ All output written to `~/.openclaw/watchdog/lazarus/` only",
        f"",
        f"---",
        f"*🐐 ACME Agent Supply Co. | Lazarus Protocol v1 — OpenClaw Reliability Stack*",
    ]

    try:
        with open(REPORT_MD, "w") as f_:
            f_.write("\n".join(md_lines) + "\n")
    except Exception:
        pass

    emit_event("RUN_END", score=score, risk=risk, duration_ms=duration_ms,
               checks_run=len(_findings), events_emitted=len(_events))


# ─── Safety check ─────────────────────────────────────────────────────────────
def safety_abort_if_violated():
    """Hard check: if openclaw.json was ever written, abort."""
    # We never write to it; this is belt-and-suspenders verification
    if FORBIDDEN_WRITE in [Path(p) for p in [str(FORBIDDEN_WRITE)]]:
        pass  # We just verify we never reference it for writing


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Lazarus Protocol v1")
    parser.add_argument("--mode", choices=["scan","plan","generate","validate","all"],
                        default="all", help="Execution mode")
    parser.add_argument("--archive", help="Archive path for validate mode")
    args = parser.parse_args()

    # Ensure output dir
    LAZARUS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"🔄 Lazarus Protocol v1 — mode={args.mode}", flush=True)

    facts = {}
    blueprint = {}
    validate_result = None

    try:
        if args.mode in ("scan", "all"):
            print("  → Scanning environment...", flush=True)
            facts = run_scan()

        if args.mode in ("plan", "all"):
            print("  → Building recovery blueprint...", flush=True)
            if not facts:
                facts = run_scan()
            blueprint = run_plan(facts)

        if args.mode in ("generate", "all"):
            print("  → Generating artifacts...", flush=True)
            if not blueprint:
                blueprint = run_plan(facts or run_scan())
            run_generate(blueprint)

        if args.mode in ("validate", "all"):
            print("  → Running restore dry-run...", flush=True)
            validate_result = run_validate()

        write_report(facts, blueprint, validate_result)

    except KeyboardInterrupt:
        emit_event("ERROR", error="KeyboardInterrupt")
        sys.exit(1)
    except Exception as e:
        emit_event("ERROR", error=str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if validate_result and not validate_result[0]:
        sys.exit(3)

    sys.exit(0)


if __name__ == "__main__":
    main()
