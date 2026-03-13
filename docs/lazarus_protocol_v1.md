# 🔄 Lazarus Protocol v1 — Runbook

**Status:** SHIPPED  
**Version:** 1.0.0  
**Owner:** Reliability Stack  

---

## What It Does

Lazarus Protocol is a backup readiness scanner and recovery planner for OpenClaw environments. It scans the host, generates a recovery blueprint, creates backup/restore scripts, runs a dry-run restore, and scores recovery readiness from 0–100.

```bash
python3 openclaw-ops/scripts/lazarus/lazarus.py --mode all
```

---

## Quick Start

```bash
# Full pipeline (scan → plan → generate → validate)
python3 lazarus.py --mode all

# Scan only
python3 lazarus.py --mode scan

# Generate artifacts only
python3 lazarus.py --mode generate

# Validate specific archive
python3 lazarus.py --mode validate --archive /path/to/openclaw-20260226.tar.gz
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | Unexpected error |
| 2 | Policy blocked (secrets) |
| 3 | Validation failed |

---

## Output Files

All output lives under `~/.openclaw/watchdog/lazarus/`:

| File | Purpose |
|------|---------|
| `lazarus_events.ndjson` | Full NDJSON event stream (append-only) |
| `lazarus_report.md` | Human-readable recovery report |
| `recovery_blueprint.json` | Machine-readable recovery plan |
| `artifacts/backup_local.sh` | Generated tarball backup script |
| `artifacts/restore_dryrun.sh` | Generated dry-run restore script |
| `staging_restore/` | Restore staging area (validate mode) |

---

## Checks (v1)

### Backup Substrate
| ID | Check | Severity |
|----|-------|----------|
| LZ_TM_001 | Time Machine configured | CRITICAL |
| LZ_TM_002 | Time Machine last backup recent | HIGH |
| LZ_CLD_001 | Google Drive / cloud sync present | MEDIUM |
| LZ_GIT_001 | openclaw-ops git clean & pushed | HIGH |
| LZ_GIT_002 | Recent push (within 24h) | MEDIUM |

### Surface Coverage
| ID | Check | Severity |
|----|-------|----------|
| LZ_SURF_001 | ~/.openclaw/ in backup | CRITICAL |
| LZ_SURF_002 | ~/.openclaw/watchdog/ covered | HIGH |
| LZ_SURF_003 | LaunchAgents ai.openclaw* captured | HIGH |
| LZ_SURF_004 | SQLite files addressed | MEDIUM |
| LZ_SURF_005 | Log bloat (>50MB files) | LOW |

### Restore Readiness
| ID | Check | Severity |
|----|-------|----------|
| LZ_RST_001 | restore_dryrun.sh generated | HIGH |
| LZ_RST_002 | Dry-run passes (exit 0) | HIGH |
| LZ_RST_003 | Integrity checks pass | HIGH |

### Security
| ID | Check | Severity |
|----|-------|----------|
| LZ_SEC_001 | Redaction active | HIGH |
| LZ_SEC_002 | Secrets classified | MEDIUM |

---

## Scoring Model

```
Start: 100
NO_BACKUP_DEST:           -35  (no Time Machine AND no Google Drive)
NO_OPENCLAW_COVERAGE:     -25  (runtime not in any backup)
NO_RESTORE_EVIDENCE:      -15  (no restore dry-run)
SECRETS_PLAINTEXT:        -20  (policy gate)
NO_LAUNCHAGENTS_BACKUP:   -10
REPO_NOT_CLEAN_OR_PUSHED: -10  (dirty or >5 commits ahead)
SQLITE_UNADDRESSED:       -10
CADENCE_OVER_24H:          -8
RETENTION_UNDER_7D:        -5
Floor: 0
```

| Score | Risk Level |
|-------|-----------|
| 80–100 | LOW ✅ |
| 60–79 | MODERATE ⚠️ |
| 40–59 | HIGH 🚨 |
| 0–39 | CRITICAL 💀 |

---

## NDJSON Event Schema

```json
{"ts":"ISO8601","run_id":"lazarus-NNNN","event":"EVENT_TYPE",...}
```

Event types: `RUN_START`, `CHECK_RESULT`, `PLAN_CREATED`, `ARTIFACT_GENERATED`, `VALIDATION_RESULT`, `RUN_END`, `ERROR`

---

## Artifact Scripts

### backup_local.sh
```bash
bash ~/.openclaw/watchdog/lazarus/artifacts/backup_local.sh
```
- Creates dated tarball under `~/.openclaw/watchdog/backups/lazarus/YYYY-MM-DD/`
- Includes `~/.openclaw/` and `ai.openclaw*.plist` LaunchAgents
- Writes SHA256 manifest
- Exit codes: 0=success, 10=partial, 20=policy, 30=error

### restore_dryrun.sh
```bash
bash ~/.openclaw/watchdog/lazarus/artifacts/restore_dryrun.sh [archive.tar.gz]
```
- Extracts to `staging_restore/` ONLY — never touches live dirs
- Verifies: openclaw.json, henrik_watchdog.sh, silence_sentinel.py, model_router.py
- Validates JSON parses
- Exit codes: 0=success, 40=integrity_fail, 50=archive_missing

---

## Safety Guarantees

- ✅ Never writes to `~/.openclaw/openclaw.json`
- ✅ Never restarts gateway or watchdog  
- ✅ Redaction ON by default (sk-ant-*, sk-*, AIza*, Bearer tokens → `***REDACTED***`)
- ✅ All output to `~/.openclaw/watchdog/lazarus/` only
- ✅ Events log is append-only

---

## First Run Results (AGENTMacBook, 2026-02-26)

```
Score: 75/100 MODERATE
Duration: 10,831ms
Checks: 13
Restore dry-run: PASS (exit 0)

Failed: LZ_TM_001 (no Time Machine), LZ_GIT_001 (11 commits ahead)
Passed: LZ_CLD_001, LZ_SURF_001-005, LZ_RST_002, LZ_SEC_001-002
```

---

## Rollback

```bash
git revert <commit>
rm -rf ~/.openclaw/watchdog/lazarus/   # optional cleanup
```

Watchdog is unaffected — always exits 0.

---

## Roadmap (v2+)
- Auto-push git on clean state (with `--auto-push` flag)
- Time Machine configuration wizard
- Fleet mode (multiple hosts)
- S3 / Backblaze B2 as backup target
- Encrypted secret export

---

## Backup Push Discipline Rule

> **Operator must push main at least daily.**
> The backup job will log `REPO_AHEAD_COMMITS=N` whenever unpushed commits are detected.
> Backup of `~/.openclaw/` does NOT substitute for pushing the canonical git repo.

