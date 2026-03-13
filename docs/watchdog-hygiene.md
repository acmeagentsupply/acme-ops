# WATCHDOG HYGIENE — OPERATIONS NOTE

**Status:** ACTIVE  
**Owner:** Control Plane  
**Current Version:** Watchdog v1.2
**Last Updated:** 2026-03-03

---

## PURPOSE

Ensure OpenClaw local state remains bounded and compaction-safe via automated hygiene.
As of Watchdog v1.1, hygiene guard is part of Watchdog ownership rather than a separate cleanup utility.

---

## SERVICE

| Field | Value |
|---|---|
| Label | `ai.openclaw.hygiene_guard` |
| Location | `~/Library/LaunchAgents/ai.openclaw.hygiene_guard.plist` |
| Interval | 900 seconds (15 minutes) |
| RunAtLoad | false |
| Logging | Handled internally by script (single-pass, no duplication) |

**State expectations:**
- Job appears `not running` between intervals — this is normal
- `last exit code = 0` when healthy
- Runtime is version-stamped via `WATCHDOG_HYGIENE_VERSION=1.1`

---

## WHAT THE HYGIENE GUARD DOES

Every 15 minutes:

**1. Log control**
- Warns at 2MB
- Truncates at 10MB
- Watchdog logs capped at 1MB

**2. restore_staging control**
- Warn if >100MB
- Always log `restore_staging_pressure=<N>MB`
- Skip prune if `.restore_lock` or `.active` exists in `~/.openclaw/restore_staging/`
- Enforce 30-minute prune cool-down via `~/.openclaw/watchdog/restore_staging_prune_state.json`
- Prune if >250MB
- Partial prune (files older than 24h) before full wipe

**3. Monitored files**
- `status.log`
- `radiation_findings.log`
- Core watchdog logs: `watchdog.log`, `stall.log`, `ops_events.log`, `heartbeat.log`, `backup.log`

**Goal:** Prevent disk pressure → avoid pathological compaction.

## Operator-Driven Hardening

Watchdog v1.2 keeps the v1.1 guard model but hardens the operator-visible controls:

- `restore_staging_guard` now logs `restore_staging_pressure` every run
- Prune skip is based on explicit `.restore_lock` / `.active` files, not recent file mtime
- Any prune writes `restore_staging_prune_state.json`; next prune is suppressed for 30 minutes
- Sentinel and OCTriage surfaces now expose disk pressure growth and subtree breakdown so hygiene issues correlate cleanly with runtime pressure

## V1.1 to V1.2 Upgrade Note

- v1.1: restore staging warn at 100MB, prune at 250MB, generic lock-aware skip
- v1.2: explicit lock-file detection, prune cool-down state, always-on pressure metric
- v1.2 companion surfaces:
  - Sentinel predictive disk state: `pressure_state`, `time_to_pressure_hrs`, `sentinel_predictive_state.json`
  - OCTriage bundle telemetry: `watchdog_growth_rate_mb_hr`, `backups_mb`, `lazarus_mb`, `gtm_exports_mb`
  - Correlator pressure score: `COMPACTION_PRESSURE`

## V1.2 OPERATING SURFACES

- Hygiene enforcement is part of Watchdog ownership and runs continuously via LaunchAgent
- Sentinel adds advisory `SENTINEL_DISK_PRESSURE` visibility for watchdog footprint growth with `pressure_state=normal|rising|critical` and `time_to_pressure_hrs`
- Sentinel writes `~/.openclaw/watchdog/sentinel_predictive_state.json` each cycle
- OCTriageUnit captures `watchdog_disk_usage.txt` with total usage, subtree breakdown, and `watchdog_growth_rate_mb_hr`
- `restore_staging_guard` warns at 100MB, prunes at 250MB, skips on `.restore_lock` or `.active`, and suppresses repeat prunes for 30 minutes

---

## HEALTH CHECK COMMANDS

**Verify launch agent:**
```bash
launchctl print gui/$(id -u)/ai.openclaw.hygiene_guard
```

**Manual run (safe):**
```bash
~/.openclaw/watchdog/owned/hygiene_guard.sh
```

**Disk check:**
```bash
du -sh ~/.openclaw ~/.openclaw/watchdog
```

**View last run:**
```bash
tail -n 40 ~/.openclaw/watchdog/hygiene.log
cat ~/.openclaw/watchdog/hygiene_state.json
```

---

## SUCCESS CRITERIA

A healthy system shows:
- OpenClaw footprint stable (< ~1GB typical)
- Watchdog logs bounded and not growing unbounded
- `restore_staging` normally near 0MB
- No repeated restore staging prune cycles inside a 30-minute window
- No repeated long compactions
- If hygiene stops, production failure mode is silent disk growth that degrades watchdog cadence and compaction stability.

---

## OPERATOR NOTES

- This guard is **preventative, not corrective**.
- If compaction exceeds normal window repeatedly, investigate session size and model context pressure.
- Treat proof bundles and logs as sensitive.
- Runtime path: `~/.openclaw/watchdog/owned/hygiene_guard.sh`
- Plist path target: `/Users/AGENT/bin/openclaw-watchdog-hygiene` (symlink-safe shim)
- State file: `~/.openclaw/watchdog/hygiene_state.json`
- Hygiene log: `~/.openclaw/watchdog/hygiene.log`
- Restore staging prune cool-down state: `~/.openclaw/watchdog/restore_staging_prune_state.json`

---

*END OF NOTE*
