# CONTROL PLANE NOTE — Watchdog Environment Requirements

> Customer-ready documentation. Safe to include in OpenClaw operator guides.

---

## ⚠️ CARDINAL RULE (lock this in)

> **If `openclaw.json` references a secret via env var**
> (e.g. `gateway.auth.password = ${OPENCLAW_GATEWAY_PASSWORD}`),
> then **every** LaunchAgent or script that invokes `openclaw` must either:
> 1. **Export that variable** before the call, or
> 2. **Guard and skip cleanly** if the variable is absent — never silently fail.
>
> **"Config resolution happens before command execution."**
> A missing env var breaks the entire config parse — not just the one feature.
> The gateway restart fails. The probe fails. The heal fails. All of it, silently.

---

## Summary

When gateway authentication is set to **password mode**, all background probes and
health checks must run with the same environment contract as the gateway client.

---

## Required Invariant

| Variable | Requirement |
|---|---|
| `OPENCLAW_GATEWAY_PASSWORD` | Must be present and match gateway config |
| `PATH` | Must include the same Node / OpenClaw resolution paths as the interactive shell |

---

## Failure Mode (if missing)

- `gateway.err.log` shows `reason=password_missing`
- Watchdog appears to flap or report false negatives
- Control UI may appear healthy while probes fail silently
- Operators may misdiagnose as gateway instability

---

## Operational Rule

Every **LaunchAgent** that executes OpenClaw CLI commands MUST include:

```xml
<key>EnvironmentVariables</key>
<dict>
  <key>OPENCLAW_GATEWAY_PASSWORD</key>
  <string>YOUR_PASSWORD_HERE</string>
  <key>PATH</key>
  <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
```

Use the **expanded, full PATH** — not a minimal one. launchd does not inherit
the user's interactive shell PATH.

---

## Agents That Require This

This is especially critical for:

- Watchdog agents (`hendrik_watchdog.sh`, etc.)
- Cron agents
- Drift / Sentinel jobs (`gmail_sentinel_drift_guard_v2_phase3.sh`, etc.)
- Gmail heartbeat sentinels (`gmail_heartbeat_sentinel_v2_phase2.sh`)
- Any external health probe that calls `openclaw gateway probe/status/restart`

---

## Design Principle

> OpenClaw assumes a **zero-trust local boundary**.
> Authentication is **not bypassed** for localhost once password mode is enabled.

This is intentional. Every caller — human or machine — must authenticate.

---

## Audit Checklist

Run this to check which LaunchAgents are missing the password variable:

```bash
for plist in ~/Library/LaunchAgents/ai.openclaw.*.plist; do
  has=$(grep -l "OPENCLAW_GATEWAY_PASSWORD" "$plist" 2>/dev/null && echo YES || echo NO)
  echo "$(basename $plist): $has"
done
```

---

*Last updated: 2026-02-28 | Source: Chip Ernst operational note*
