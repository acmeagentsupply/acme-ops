# Model Failover Runbook

**Canonical path (code):** `openclaw-ops/scripts/watchdog/model_router.py`
**Runtime path:** `~/.openclaw/watchdog/model_router.py`
**Trilium:** `System / Model Failover Runbook`

---

## Purpose

`model_router.py` implements client-side hard timeout enforcement and sequential failover
across the configured provider chain. It provides:

- Hard 90s timeout per provider via `threading.Timer` + socket close (AbortController equivalent)
- Sequential failover only — no parallel fan-out, no duplicate provider calls
- Structured event logging to `stall.log`
- Heartbeat state written to `model_state.json` for watchdog enrichment
- Chain loaded from `~/.openclaw/openclaw.json` at runtime (single source of truth)
- Test isolation via `TEST_MODE=1` env var

---

## Failover Chain

Source of truth: `~/.openclaw/openclaw.json` → `agents.defaults.model`

Current chain (as of freeze):
```
1. anthropic/claude-sonnet-4-6    (primary)
2. openai/gpt-4.1-mini            (fallback 1)
3. google/gemini-2.5-flash-lite   (fallback 2)
4. openrouter/deepseek/deepseek-v3.1-terminus:exacto  (fallback 3)
```

The router reads this chain on every `route()` call. Changing `openclaw.json` takes effect immediately — no restart needed.

---

## Thresholds

| Parameter | Value | Notes |
|---|---|---|
| Hard timeout per provider | 90s | `DEFAULT_TIMEOUT_S` in `model_router.py` |
| Abort mechanism | `threading.Timer` + `http.client` conn.close() | stdlib only, no external deps |
| Retry policy | none — each provider tried once | no retry storms |
| Parallel calls | none — sequential only | |

---

## Event Log Format

All events written to `~/.openclaw/watchdog/stall.log` (and stdout):

```
[TS] MODEL_CHAIN_RESOLVED source=config chain=[p/m -> p/m -> ...] req=<req_id>
[TS] MODEL_TIMEOUT_ABORT    provider=<p> duration_ms=<d> model=<m> req=<req_id>
[TS] MODEL_FAILOVER_TRIGGERED from=<p>/<m> to=<p>/<m> req=<req_id>
[TS] MODEL_FAILOVER_SUCCESS   provider=<p> duration_ms=<d> model=<m> req=<req_id>
[TS] MODEL_FAILOVER_EXHAUSTED req=<req_id>
[TS] MODEL_PROVIDER_ERROR     provider=<p> error=<msg> req=<req_id>
```

---

## Canonical GitHub Paths

```
openclaw-ops/scripts/watchdog/model_router.py
openclaw-ops/scripts/watchdog/tests/test_forced_failover.py
openclaw-ops/templates/watchdog.env.example    ← HARD_S, TEST_MODE docs
```

Runtime state:
```
~/.openclaw/watchdog/model_state.json   ← {"provider":..., "status":..., "updated_at":...}
~/.openclaw/openclaw.json               ← chain source of truth
```

---

## How to Validate

```bash
# 1. Verify chain reads from openclaw.json
cd openclaw-ops/scripts/watchdog
python3 -c "
from model_router import load_chain_from_config
chain, source = load_chain_from_config()
print('source:', source)
for e in chain: print(' ', e['provider'] + '/' + e['model'])
"

# 2. Run full acceptance tests (13 assertions, TEST_MODE=1 set automatically)
TEST_MODE=1 python3 openclaw-ops/scripts/watchdog/tests/test_forced_failover.py

# 3. Check model_state.json is NOT written during tests
python3 -c "import json; d=json.load(open('$HOME/.openclaw/watchdog/model_state.json')); print(d)"
# timestamp should be unchanged after test run
```

---

## Expected Log Lines

```
# Test A — primary stalls, fallback fires
[2026-02-24 17:35:48 ] MODEL_CHAIN_RESOLVED source=config chain=[anthropic/claude-sonnet-4-6 -> openai/gpt-4.1-mini] req=req-test-a
[2026-02-24 17:35:51 ] MODEL_TIMEOUT_ABORT provider=anthropic duration_ms=3219 model=claude-sonnet-4-6 req=req-test-a
[2026-02-24 17:35:51 ] MODEL_FAILOVER_TRIGGERED from=anthropic/claude-sonnet-4-6 to=openai/gpt-4.1-mini req=req-test-a
[2026-02-24 17:35:51 ] MODEL_FAILOVER_SUCCESS provider=openai duration_ms=0 model=gpt-4.1-mini req=req-test-a

# Test C — full chain exhausted
[2026-02-24 17:35:51 ] MODEL_FAILOVER_EXHAUSTED req=req-test-c
```

---

## Test Isolation (B1 — freeze blocker resolved)

`TEST_MODE=1` must be set for all test runs. When set, `save_model_state()` is a no-op — `model_state.json` is never written by test code.

`test_forced_failover.py` sets `os.environ["TEST_MODE"] = "1"` as its first statement. This is enforced in the test file and must not be removed.

---

## Rollback Steps

1. **Chain diverged from openclaw.json:**
   - No action needed — `load_chain_from_config()` reads live from disk each `route()` call
   - Verify: `python3 -c "from model_router import load_chain_from_config; print(load_chain_from_config())"`

2. **model_state.json poisoned by test run:**
   ```bash
   python3 -c "import json,time; json.dump({'provider':'unknown','status':'unknown','updated_at':0}, open('$HOME/.openclaw/watchdog/model_state.json','w'))"
   # Stall detector will then fall back to session JSONL as source of truth
   ```

3. **Timeout too aggressive (legitimate slow calls timing out):**
   - Edit `DEFAULT_TIMEOUT_S` in `model_router.py` and `HARD_S` in `watchdog.env.example`
   - Update threshold in watchdog script call: `"45000" "90000"`
   - Commit change and redeploy

4. **Restore from repo:**
   ```bash
   cp openclaw-ops/scripts/watchdog/model_router.py ~/.openclaw/watchdog/
   ```
