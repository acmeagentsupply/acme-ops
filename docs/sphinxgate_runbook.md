# SphinxGate Runbook — v1

**Purpose:** Token discipline and lane enforcement for model routing.
**Status:** FROZEN v1
**Commit:** 82b77d8 (Phase 1B) → see freeze commit for final hash

---

## What SphinxGate Does

SphinxGate is the policy layer inside `model_router.py` that controls which
model providers are allowed per request lane. It eliminates background Claude
burn and makes routing behavior fully config-driven.

---

## Policy Precedence Rules (v1)

Order of operations applied to the provider chain on every `route()` call:

1. **allow_providers** (if present) is **AUTHORITATIVE** — only providers
   listed here are permitted. All others are stripped regardless of deny list.
2. **deny_providers SUBTRACTS** from the result of step 1.
   If both allow and deny are set: allow first, then deny from that result.
3. If filtering **empties the chain** → `SPHINXGATE_POLICY_EXHAUSTED` is emitted.
   Behavior then depends on `fail_open`:
   - `fail_open=false` (default) → return `status=POLICY_FAIL` immediately
   - `fail_open=true` → route the **original unfiltered chain** (logged)

**Example:**
```
allow: [openai, google]   → anthropic stripped (not in allow list)
deny:  [google]           → google then stripped from allow result
result: [openai]          → openai only
```

---

## Configuration

In `~/.openclaw/openclaw.json`:

```json
"sphinxgate": {
  "enabled": true,
  "lanes": {
    "interactive": {
      "allow_providers": ["anthropic", "openai", "google", "openrouter"],
      "max_latency_ms": 180000,
      "max_cost_tier": "high"
    },
    "background": {
      "deny_providers": ["anthropic"],
      "max_latency_ms": 60000,
      "max_cost_tier": "low"
    }
  },
  "failover": {
    "max_attempts": 4,
    "fail_open": false
  }
}
```

`agents.lanes.background.model` controls the background chain order:
```json
"agents": {
  "lanes": {
    "background": {
      "model": ["openai/gpt-4.1-mini", "google/gemini-2.5-flash-lite", "openrouter/..."]
    }
  }
}
```

**To change routing behavior: edit openclaw.json only. Zero code changes required.**

---

## Lane Resolution

Lane is resolved in this order per `route()` call:
1. Explicit `lane=` argument
2. `OPENCLAW_LANE` environment variable
3. Default: `"interactive"`

Watchdog launchd plist sets `OPENCLAW_LANE=background` in `EnvironmentVariables`.

---

## Log Events

| Event | When |
|---|---|
| `SPHINXGATE_LANE_RESOLVED` | Once per request — shows lane + allow/deny policy |
| `SPHINXGATE_PROVIDER_STRIPPED` | Per stripped provider — shows reason (deny_list / not_in_allow_list) |
| `SPHINXGATE_POLICY_EXHAUSTED` | Policy emptied the chain — shows fail_open value |
| `SPHINXGATE_FAILOPEN_FALLBACK` | fail_open=true triggered — routing original chain |
| `SPHINXGATE_POLICY_HARD_FAIL` | fail_open=false + exhausted — request failed |

---

## Overrides

| Override | Effect |
|---|---|
| `allow_premium=True` in route() | Bypasses deny list only (allow list still applies) |
| `OPENCLAW_ALLOW_PREMIUM=1` env | Same as above |
| Explicit `chain=` in route() | Still subject to policy unless allow_premium set |

---

## Token Logging

Every request attempt (FAIL and OK) appends one line to:
`~/.openclaw/metrics/tokens.log`

Format (CSV):
```
ts,req_id,lane,provider,model,input_tokens,output_tokens,total_tokens,status,latency_ms,cost_est
```

Run `tokens-status` command:
```bash
python3 ~/.openclaw/watchdog/model_router.py --tokens-status
```

Watchdog `status.log` includes per-cycle token totals:
```
tokens_total= tokens_in= tokens_out=
```

---

## Performance

| Path | Latency |
|---|---|
| Policy load (cold) | ~87ms |
| Policy load (warm/cached) | ~96ms |
| Policy cache TTL | 60 seconds |
| Config re-read trigger | mtime change or TTL expiry |
| Watchdog status.log token block | ~119ms |

---

## Proof Script

```bash
python3 openclaw-ops/scripts/watchdog/sphinxgate_v1_freeze_proof.py
```

Tests A (allow-only), B (deny-only), C1 (exhausted+fail_open=false),
C2 (exhausted+fail_open=true). All 5 assertions must pass.

---

## Rollback

```bash
git revert <freeze-commit-hash>
cp openclaw-ops/scripts/watchdog/model_router.py ~/.openclaw/watchdog/model_router.py
launchctl unload ~/Library/LaunchAgents/ai.openclaw.hendrik_watchdog.plist
launchctl load   ~/Library/LaunchAgents/ai.openclaw.hendrik_watchdog.plist
```

---

## Files Changed (v1)

| File | Change |
|---|---|
| `openclaw-ops/scripts/watchdog/model_router.py` | SphinxGate policy engine |
| `openclaw-ops/launchd/ai.openclaw.hendrik_watchdog.plist` | OPENCLAW_LANE=background |
| `openclaw-ops/config/openclaw.json` | sphinxgate block + background lane config |
| `openclaw-ops/scripts/watchdog/sphinxgate_proof.py` | Phase 1 proof bundle |
| `openclaw-ops/scripts/watchdog/sphinxgate_v1_freeze_proof.py` | v1 freeze proof bundle |
| `openclaw-ops/docs/sphinxgate_runbook.md` | This file |
