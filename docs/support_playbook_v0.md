# ACME Support Playbook v0
**Operator Guide — "What to Do When Things Break"**

Version: 0.1.0 | Owner: ACME Agent Supply Co. | Status: ACTIVE

---

## Overview

This playbook covers:
1. What to send when opening a support request
2. How ACME responds (posture + triage order)
3. SLA language and escalation paths
4. Operator self-serve checklist before contacting support

---

## 1. Before You Contact Support — Self-Triage Checklist

Run through this first. Most issues are diagnosable locally.

### Step 1 — Get your current posture

```bash
python3 ~/.openclaw/workspace/openclaw-ops/scripts/agent911/agent911_snapshot.py
```

Check:
- `stability_score` — below 50 is ELEVATED, below 30 is CRITICAL
- `risk_level` — OK / ELEVATED / CRITICAL / UNKNOWN
- `recommended_actions` — follow these first

### Step 2 — Run RadCheck

```bash
python3 ~/.openclaw/workspace/openclaw-ops/scripts/radiation/radiation_check.py
```

Review radiation findings log for WARN/CRIT entries.

### Step 3 — Run Lazarus

```bash
python3 ~/.openclaw/workspace/openclaw-ops/scripts/lazarus/lazarus.py
```

Check `lz_score` and `failed_checks[]`. Address CRITICAL checks before opening a ticket.

### Step 4 — Check Sentinel state

```bash
tail -50 ~/.openclaw/watchdog/ops_events.log | grep SENTINEL
```

Look for `SENTINEL_PROTECTION_STALL_PREVENTED` or `SENTINEL_PREDICTIVE_GUARD` events.

### Step 5 — Check compaction health

```bash
cat ~/.openclaw/watchdog/compaction_alert_state.json
```

If `alert_active: true`, review `p95_ms` and `timeout_count`.

---

## 2. What to Send — Support Bundle

If you cannot resolve the issue with the self-triage checklist, generate a support bundle:

### Quick start

```bash
# Generate redacted bundle (default — recommended)
python3 ~/.openclaw/workspace/openclaw-ops/scripts/support/acme_support_bundle.py

# Generate + zip for easy attachment
python3 ~/.openclaw/workspace/openclaw-ops/scripts/support/acme_support_bundle.py --zip

# See consent & privacy notice before sending
python3 ~/.openclaw/workspace/openclaw-ops/scripts/support/acme_support_bundle.py --print-consent

# Include raw logs (redaction off — review before sending)
python3 ~/.openclaw/workspace/openclaw-ops/scripts/support/acme_support_bundle.py --include_raw --zip
```

### What the bundle contains

| File | Description |
|---|---|
| `summary.md` | Human-readable triage summary |
| `bundle_manifest.json` | Machine-readable manifest with triage fields |
| `redacted_logs/ops_events_tail.log` | Last 50 ops events |
| `redacted_logs/heartbeat_tail.log` | Last 50 heartbeat entries |
| `redacted_logs/launchd_out_tail.log` | Last 50 launchd stdout lines |
| `state_snapshots/agent911_state.json` | Current Agent911 control-plane state |
| `state_snapshots/agent911_dashboard.md` | Current dashboard |
| `state_snapshots/radcheck_history_tail.ndjson` | Last 50 RadCheck history events |
| `state_snapshots/mtl_snapshot.json` | Current Master Task List snapshot |

### What is NOT included

- `openclaw.json` (never included, always excluded)
- `auth-profiles.json` (never included)
- API keys, `.pem` files, `.env` files, private keys
- Any file matching the secrets blacklist

### Redaction

Redaction is ON by default. The following are masked before any file is written:

| Pattern | Replacement |
|---|---|
| JWT tokens (`eyJ...`) | `[JWT_REDACTED]` |
| OpenAI API keys (`sk-...`) | `[API_KEY_REDACTED]` |
| Bearer tokens | `Bearer [TOKEN_REDACTED]` |
| Email addresses | `[EMAIL_REDACTED]` |
| Long hex secrets (40+ chars) | `[HEX_SECRET_REDACTED]` |
| Tailscale IPs (`100.x.x.x`) | `[TAILSCALE_IP_REDACTED]` |

---

## 3. How to Submit

1. Generate the bundle and zip: `acme_support_bundle.py --zip`
2. Review `summary.md` to confirm no sensitive data leaked through redaction
3. Email the `.zip` to: **support@acmeagentsupply.com**
4. Subject line: `ACME Support Bundle — <bundle_id>` (bundle_id is in summary.md)
5. Include a brief description of the observed behavior

---

## 4. ACME Support Posture

ACME's support posture is **observational and advisory**. We do not perform remote operations on your system.

### Triage Order

When we receive a bundle, we triage in this order:

1. **Agent911 score + risk level** — baseline system health
2. **ops_events.log** — recent event sequence (install, sentinel, compaction)
3. **Sentinel events** — stall prevention, predictive guard activity
4. **Compaction state** — p95 latency, timeout count, alert status
5. **RadCheck history** — radiation trend and velocity
6. **Recommended actions** — from Agent911 snapshot
7. **Routing/SphinxGate** — active provider and posture

### What we do NOT do

- We do not modify your `openclaw.json`
- We do not restart your gateway
- We do not make infrastructure changes without written operator approval
- We do not access your system directly (all diagnosis is from the bundle)

### Response Commitment

| Support Tier | First Response | Target Resolution |
|---|---|---|
| Emergency (CRITICAL score < 30) | [SLA_PLACEHOLDER] | [SLA_PLACEHOLDER] |
| Standard (ELEVATED score 30–60) | [SLA_PLACEHOLDER] | [SLA_PLACEHOLDER] |
| Advisory (OK score > 60) | [SLA_PLACEHOLDER] | [SLA_PLACEHOLDER] |
| No agreement (best-effort) | Next business day | No guarantee |

> **Note:** Fill in `[SLA_PLACEHOLDER]` entries upon support agreement execution.
> Do not communicate SLA times verbally or in email without an executed agreement.

---

## 5. Escalation Path

| Level | Trigger | Action |
|---|---|---|
| L1 — Bundle review | Any open ticket | ACME reviews bundle, provides advisory |
| L2 — Live session | L1 resolution > 48h | Operator + ACME screen share (operator drives) |
| L3 — Engineering | L2 unresolved | Engineering root cause review |

---

## 6. Operator Privacy Rights

- You may request deletion of your support bundle data at any time.
- You may request a redacted copy of any ACME support notes.
- You may revoke consent to use your bundle for training/analytics (consent is not granted by default — see consent blurb).

Run `acme_support_bundle.py --print-consent` for the full consent and privacy notice.

---

## 7. Quick Reference — Key File Paths

| File | Purpose |
|---|---|
| `~/.openclaw/watchdog/agent911_state.json` | Agent911 control-plane state |
| `~/.openclaw/watchdog/agent911_dashboard.md` | Human-readable dashboard |
| `~/.openclaw/watchdog/ops_events.log` | Unified NDJSON event log |
| `~/.openclaw/watchdog/compaction_alert_state.json` | Compaction alert state |
| `~/.openclaw/watchdog/sentinel_protection_state.json` | Sentinel protection state |
| `~/.openclaw/watchdog/radcheck_history.ndjson` | RadCheck score history |
| `~/.openclaw/watchdog/install/install_state.json` | Install state |
| `~/.openclaw/watchdog/install/bundles.lock.json` | Bundle lockfile (sha256 pins) |
| `~/.openclaw/watchdog/support/bundles/` | Support bundle output dir |

---

*ACME Agent Supply Co. — Support Playbook v0.1.0*
*For questions about this playbook: support@acmeagentsupply.com*
