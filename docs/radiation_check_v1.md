# ☢️ Radiation Check v1 — Runbook

**Status:** MVP SHIPPED  
**Version:** 1.0.0  
**Owner:** Reliability Stack  

---

## What It Does

Radiation Check is a read-only diagnostic scanner for OpenClaw environments. It identifies hidden reliability risks before they cause production failure. Single-shot, zero side effects.

```
python3 radiation_check.py
```

Output:
- Human console report (scored, ranked)
- NDJSON findings stream → `~/.openclaw/watchdog/radiation_findings.log`
- Markdown report → `~/.openclaw/watchdog/radiation_report.md`

---

## Quick Start

```bash
# Run scan (console output)
python3 openclaw-ops/scripts/radiation/radiation_check.py

# JSON output for piping
python3 openclaw-ops/scripts/radiation/radiation_check.py --json

# Quiet mode (no console, files only)
python3 openclaw-ops/scripts/radiation/radiation_check.py --quiet
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | SUCCESS — scan complete |
| 10 | SAFETY_ABORT — guardrail violated |
| 11 | CONFIG_UNREADABLE |
| 12 | LOG_ACCESS_FAILURE |
| 13 | PROBE_FAILURE |
| 20 | PARTIAL_SCAN — some modules skipped |

---

## Scan Modules

### Module 1: Configuration (`RC_CFG_*`)
Checks openclaw.json for structural reliability risks.

| ID | Check | Severity |
|----|-------|----------|
| RC_CFG_001 | Missing failover chain | CRITICAL |
| RC_CFG_002 | Single provider dependency | HIGH |
| RC_CFG_004 | Watchdog not installed | CRITICAL |
| RC_CFG_007 | Probe debounce missing | HIGH |
| RC_CFG_008 | Token visibility missing | LOW |

### Module 2: Watchdog Health (`RC_WD_*`)
Validates watchdog correctness from status.log and script inspection.

| ID | Check | Severity |
|----|-------|----------|
| RC_WD_001 | Watchdog loop gap abnormal | CRITICAL/HIGH |
| RC_WD_002 | High probe failure rate | HIGH/LOW |
| RC_WD_004 | Missing consecutive-failure guard | HIGH |
| RC_WD_005 | Silence sentinel absent | MEDIUM |
| RC_WD_006 | model_state.json stale | MEDIUM |

### Module 3: Model Routing (`RC_RT_*`)
Validates SphinxGate and routing robustness.

| ID | Check | Severity |
|----|-------|----------|
| RC_RT_001 | No fallback providers | CRITICAL |
| RC_RT_002 | Provider diversity insufficient | LOW |
| RC_RT_003 | Policy exhaustion risk | HIGH |
| RC_RT_004 | Allow/deny conflict | HIGH |
| RC_RT_005 | Lane separation missing | MEDIUM |
| RC_RT_006 | Token telemetry absent | LOW |
| RC_RT_007 | model_state persistence missing | MEDIUM |

### Module 4: Environment (`RC_ENV_*`)
Detects host instability and compaction risk.

| ID | Check | Severity |
|----|-------|----------|
| RC_ENV_001 | Load average elevated | CRITICAL/HIGH/MEDIUM |
| RC_ENV_002 | Memory pressure | HIGH/MEDIUM |
| RC_ENV_003 | Process count abnormal | MEDIUM |
| RC_ENV_004 | Port up / probe fail (frozen loop) | HIGH/CRITICAL |
| RC_ENV_004B | Historical frozen-loop pattern | HIGH |
| RC_ENV_005 | Compaction frequency elevated | HIGH/MEDIUM |

### Module 5: Port vs Probe (Signature Feature)
The flagship check — detects the exact failure mode of gateway compaction stalls.

```
If port 18789 = LISTENING and RPC probe = FAILING:
  → GATEWAY_STALL detected
  → severity = HIGH
  → "classic frozen event loop risk"
```

This pattern is invisible to naive health checks that only test port availability.

---

## Scoring Model

```
Start: 100
CRITICAL finding: -25
HIGH finding:     -12
MEDIUM finding:    -5
LOW finding:       -1
INFO finding:       0
Floor: 0
```

| Score | Risk Level |
|-------|-----------|
| 80–100 | LOW ✅ |
| 60–79 | MODERATE ⚠️ |
| 40–59 | HIGH 🚨 |
| 0–39 | SEVERE 💀 |

---

## Finding Schema (NDJSON)

Each line in `radiation_findings.log`:

```json
{
  "ts": "2026-02-26T14:09:06Z",
  "tool": "radiation_check",
  "finding_id": "RC_ENV_004",
  "severity": "HIGH",
  "component": "gateway",
  "summary": "GATEWAY PORT UP but RPC probe FAILING — frozen event loop detected",
  "evidence": "port 18789: LISTENING | gateway probe: FAIL",
  "recommended_fix": "Enable probe debounce. This is a compaction stall signature.",
  "confidence": 0.97
}
```

---

## Safety Guarantees

Radiation Check:
- ✅ Never modifies `~/.openclaw/openclaw.json`
- ✅ Never restarts gateway or watchdog
- ✅ Never acquires exclusive locks
- ✅ Completes in <60s (target; actual: ~7s on AGENTMacBook)
- ✅ Degrades gracefully if any log file is missing
- ✅ Exit code 0 on clean run (even with HIGH findings)

---

## Integration Points

### PLG Funnel
Radiation Check is designed as a free-tier entry point:

1. User runs `radiation-check scan`
2. Gets score + ranked risks
3. Recommendations reference SphinxGate, Sentinel, Drift Guard, Agent911
4. Each fix drives deeper product adoption

### Watchdog Integration
Run Radiation Check on-demand or post-incident:

```bash
# Post-incident sweep
bash ~/.openclaw/watchdog/ops_event_marker.sh RADIATION_SCAN start manual
python3 radiation_check.py
bash ~/.openclaw/watchdog/ops_event_marker.sh RADIATION_SCAN end manual
```

### CI/CD Gate
Use exit code to gate deployments:

```bash
python3 radiation_check.py --quiet
if [ $? -ne 0 ]; then
  echo "Radiation check failed — blocking deploy"
  exit 1
fi
```

---

## First Run Results (AGENTMacBook, 2026-02-26)

```
System Stability Score: 45 / 100  🚨
Overall Risk: HIGH

CRITICAL (1): RC_WD_001 — 101min watchdog gap (overnight outage)
HIGH (2):     RC_ENV_005 — 6 compaction safeguard triggers, 7 timeouts
              RC_ENV_004B — 10 historical port-up/probe-fail events

duration_ms: 6902
findings_count: 14
files_scanned: 10
errors_encountered: 0
```

The scan correctly diagnosed the overnight outage and linked it to compaction stall history. The score of 45/100 reflects real system risk on the day after a gateway failure.

---

## Roadmap (v2+)

- `--diff` mode: compare against previous scan baseline
- Fleet mode: scan multiple OpenClaw instances
- TUI dashboard with live refresh
- Transmission integration hints in routing findings
- Auto-generate remediation scripts (read-only by default; requires --fix flag)
