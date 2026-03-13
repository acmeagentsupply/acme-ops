# Agent911 v0.1 — Unified Reliability Scoreboard
TASK_ID: A-A9-V0-001
STATUS: ACTIVE
OWNER: GP-OPS (Lars / Hendrik)
LAST_UPDATED: 2026-02-26

---

## What Agent911 v0.1 Shows

A single-pane read-only reliability snapshot assembled from existing telemetry.
Run it any time to get an operator-grade view of system health in < 2 seconds.

### Dashboard Sections

| Section            | What It Shows                                              |
|--------------------|-----------------------------------------------------------|
| SYSTEM STABILITY   | RadCheck score (0-100), risk level, velocity trend        |
| ACTIVE RISKS       | Top 3 CRITICAL/HIGH findings from radiation_findings.log  |
| PROTECTION STATE   | Sentinel / Watchdog / SphinxGate liveness                 |
| BACKUP & RESURRECTION | Backup age, restore readiness, repo sync status        |
| MODEL HEALTH       | Active provider, status, staleness age                    |
| COMPACTION RISK    | Risk level, p95, timeout count, acceleration flag         |

---

## Data Sources (All Read-Only)

| Source | What It Feeds | Graceful If Missing |
|--------|--------------|---------------------|
| `~/.openclaw/watchdog/radcheck_history.ndjson` | Stability score, risk level, velocity | score=unknown |
| `~/.openclaw/watchdog/radiation_findings.log` | Top 3 CRITICAL/HIGH risks | risks=[] |
| `~/.openclaw/watchdog/ops_events.log` | Compaction events (via radcheck) | compaction=unknown |
| `~/.openclaw/watchdog/backup.log` | Backup age, restore drill age, repo sync | backup=unknown |
| `~/.openclaw/watchdog/model_state.json` | Provider, status, updated_at | model=unknown |
| `~/.openclaw/watchdog/compaction_alert_state.json` | Sentinel alert level | alert=unknown |
| `~/.openclaw/watchdog/heartbeat.log` | Watchdog liveness (last HB timestamp) | watchdog=unknown |

---

## How to Regenerate Snapshot

```bash
# Run from anywhere
python3 ~/.openclaw/workspace/openclaw-ops/scripts/agent911/agent911_snapshot.py

# Outputs:
#   ~/.openclaw/watchdog/agent911_state.json      (machine-readable)
#   ~/.openclaw/watchdog/agent911_dashboard.md    (human-readable)
```

Or view the last dashboard directly:
```bash
cat ~/.openclaw/watchdog/agent911_dashboard.md
```

---

## Protection State Definitions

| State    | Sentinel         | Watchdog            | SphinxGate          |
|----------|-----------------|---------------------|---------------------|
| ACTIVE   | alert_level key present in alert_state.json | HB in last 15 min | model_state updated < 1h |
| IDLE     | —                | HB 15-60 min ago    | model_state 1-24h ago |
| STALE    | —                | HB 15-60 min ago    | model_state > 24h    |
| DOWN     | —                | HB > 60 min ago     | —                    |
| unknown  | File missing     | File missing         | File missing         |

---

## Known Limitations (v0.1)

1. **No live subprocess calls** — SphinxGate/watchdog liveness based on file timestamps, not live process checks. A zombie process that isn't writing could show as ACTIVE.

2. **Backup age uses log parse, not filesystem** — If `backup.log` is rotated or missing, backup age shows `unknown`. Actual GDrive snapshots may be fresher.

3. **Repo sync from backup.log** — `REPO_AHEAD_COMMITS` value is from the last backup run, not the current git state. May be stale between backups.

4. **Velocity from last 2 history entries** — requires ≥2 RadCheck runs to populate. Shows `unknown` on first-ever run.

5. **RadCheck data can be stale** — Agent911 reads the last RadCheck scan. If RadCheck hasn't run recently, scores reflect old state. Run `radiation_check.py` first for freshest data.

6. **No autonomous healing in v0.1** — This is an observability tool only. No actions taken.

---

## Safety Guarantees

- **Zero writes** to `~/.openclaw/openclaw.json`
- **Zero subprocess calls** — no gateway queries, no shell commands
- **Zero service restarts**
- **Read-only** to all telemetry sources
- **Overwrite-safe output** — only writes `agent911_state.json` and `agent911_dashboard.md`
- **Always exits 0** — outer try/except prevents any crash from propagating
- **Graceful degradation** — every missing file → `"unknown"` values; never raises

---

## How to Run in Watchdog (Future)

Not integrated into the watchdog loop in v0.1. Safe to add:
```bash
# In hendrik_watchdog.sh, after COMP_SUMMARY:
A911_RESULT=$(python3 "$REPO/openclaw-ops/scripts/agent911/agent911_snapshot.py" 2>/dev/null || echo "AGENT911_ERROR")
```

---

## Upgrade Path — v0.2 Placeholder

| Feature | v0.1 | v0.2 |
|---------|------|------|
| Runtime | <2s read-only | <2s |
| Gateway liveness | File-based | Live probe (with timeout) |
| Healing actions | None | Controlled playbook invocation |
| Snapshot history | Single overwrite | Append-only NDJSON log |
| Repo sync | From backup.log | Live git status |
| Watchdog integration | Manual | Auto-run each cycle |
| Alert delivery | None | cron wake on score drop >10 |

---

## Rollback

```bash
git revert <commit-hash>
rm ~/.openclaw/watchdog/agent911_state.json
rm ~/.openclaw/watchdog/agent911_dashboard.md
# System otherwise unchanged — no services modified
```

---

## Hardening Patch (A-A9-V0-002)

### Changes
- **Repo sync**: Now computed via live `git rev-list --left-right --count HEAD...@{upstream}`. Fetch gated by `FETCH_HEAD` mtime (skip if <60s old). Labels: IN SYNC / AHEAD N — push recommended / BEHIND N — pull recommended / UNKNOWN — no upstream.
- **SphinxGate state**: Explicit tri-state (ACTIVE / IDLE / UNKNOWN) with reason string. Detection: `model_router.py` presence + `model_state.json` age. ACTIVE = <1h, IDLE = 1–24h, UNKNOWN = router missing.
- **Compaction truth**: Sentinel (`compaction_alert_state.json`) is primary truth. RadCheck fallback only when sentinel missing. `source` field in output shows which path was taken.
- **Compaction block**: Now includes `state` (NOMINAL/SUSPECT/ACTIVE/STORM), `risk` (LOW/MEDIUM/HIGH), `source` field.

### Determinism Guarantee
Two consecutive runs produce *identical* JSON except for `ts` and `duration_ms`.
Verified: sha256 `ddab5d8018288cc8` matched on both runs (2026-02-27).

### Performance Profile
- Run 1 (FETCH_HEAD stale): ~803ms (includes git fetch ~757ms)
- Run 2+ (FETCH_HEAD <60s): ~64ms
- Absolute max: <250ms when fetch is skipped (normal operating condition)
- Fetch cap: `subprocess timeout=2s`; on timeout → `repo_status_label = "UNKNOWN — fetch timeout"`

---

## State Semantics Patch (A-A9-V0-003)

### SphinxGate Evidence Source
- **File**: `~/.openclaw/metrics/tokens.log`
- **Format**: CSV — `timestamp,req_id,lane,provider,model,tok_in,tok_out,tok_total,status,latency_ms,cost`
- **Cap**: Last 200 lines scanned (performance budget)
- **Thresholds**:
  - ACTIVE: last decision < 1h ago
  - IDLE: last decision ≥ 1h ago (or no entries in last 24h)
  - UNKNOWN: model_router.py missing OR tokens.log missing/unreadable
- **New fields**: `last_decision_ts`, `last_decision_age_hours`, `evidence_source`
- **Dashboard line**: `ACTIVE (decision 4m ago)` / `IDLE (no decisions in 11.1h)` / `UNKNOWN (no evidence source)`

### Repo Status Label Definitions
| Label | Meaning |
|-------|---------|
| `IN SYNC` | ahead=0, behind=0 |
| `AHEAD N — push recommended` | N local commits not yet pushed |
| `BEHIND N — pull recommended` | N remote commits not yet pulled |
| `DIVERGED (ahead=A behind=B) — reconcile recommended` | Both ahead and behind |
| `UNKNOWN — no upstream` | Branch has no tracking upstream |
| `UNKNOWN — git unavailable` | git subprocess failed |
| `UNKNOWN_FETCH_TIMEOUT` | git fetch exceeded 2s timeout |

### Determinism Guarantee
Two consecutive runs produce identical JSON except `ts` and `duration_ms`.
Verified: sha256 `951456b97f63c113` matched on both runs (2026-02-27, A-A9-V0-003).
