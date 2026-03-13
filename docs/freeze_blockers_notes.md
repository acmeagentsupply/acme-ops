# Freeze Blockers — Resolution Notes

**Date resolved:** 2026-02-24
**Trilium:** `System / Reliability Doctrine Index`

These notes document the three pre-freeze issues identified during the doctrine gap review
and how each was resolved. Preserved for audit trail and future regression reference.

---

## B1 — Test Contamination of model_state.json

**Problem:** `model_router.py` calls `save_model_state()` unconditionally, including during
test runs. Test C (full chain exhausted) wrote `{"status": "failover_exhausted", "provider": "none"}`
to the live `model_state.json`. The next watchdog heartbeat reported `last_model_status=failover_exhausted`
to the operator — a false alarm from test code contaminating production state.

**Confirmed poisoned heartbeat line (before fix):**
```
[16:52:24] HEARTBEAT: gateway OK … last_model_provider=none last_model_status=failover_exhausted
```

**Fix:** Added `TEST_MODE=1` guard in `save_model_state()`:
```python
def save_model_state(provider, status):
    if os.environ.get("TEST_MODE") == "1":
        return
    ...
```

`test_forced_failover.py` sets `os.environ["TEST_MODE"] = "1"` as its first statement (before imports).

**Verification:** Run Test C, confirm `model_state.json` timestamp unchanged:
```bash
# Before
python3 -c "import json; d=json.load(open('$HOME/.openclaw/watchdog/model_state.json')); print(d['updated_at'])"

TEST_MODE=1 python3 tests/test_forced_failover.py

# After — timestamp must be identical
python3 -c "import json; d=json.load(open('$HOME/.openclaw/watchdog/model_state.json')); print(d['updated_at'])"
```

---

## B2 — Bare `&&` Under set -euo pipefail

**Problem:** In the status.log block of `hendrik_watchdog.sh`, the probe was written as:
```bash
openclaw gateway probe >/dev/null 2>&1 && PROBE="ok"
```
Under `set -euo pipefail`, if the probe returns non-zero, this boolean expression returns
non-zero, causing the script to exit silently — before writing `status.log` or logging
`WATCHDOG done`. This caused the stall detector block to be skipped on the first kickstart test.

**Fix:** Replaced with `if/fi` form that is `set -e` safe regardless of exit code:
```bash
if openclaw gateway probe >/dev/null 2>&1; then PROBE="ok"; else PROBE="fail"; fi
```
Applied to both the primary probe check and the post-kickstart re-probe.

**Verification:** Simulate probe failure, confirm `status.log` written and `WATCHDOG done` logged:
```bash
bash -c '
set -euo pipefail
PROBE="fail"
if false; then PROBE="ok"; else PROBE="fail"; fi   # simulated failure
echo "probe=$PROBE"
echo "$(date) status: probe=$PROBE" >> /tmp/b2_test.log
echo "WATCHDOG done"
cat /tmp/b2_test.log
rm -f /tmp/b2_test.log
'
# Expected: "WATCHDOG done" printed + status.log entry with probe=fail
```

---

## Chain Source-of-Truth — model_router.py

**Problem:** `model_router.py` had a hardcoded `DEFAULT_CHAIN` that mirrored `openclaw.json`
but was not read from it. If the fallback order in `openclaw.json` changed, `model_router.py`
would silently drift.

**Fix:** Added `load_chain_from_config()` function that reads `~/.openclaw/openclaw.json`
at runtime:
```python
def load_chain_from_config(openclaw_json=None):
    cfg_path = openclaw_json or os.path.expanduser("~/.openclaw/openclaw.json")
    ...
    chain = [_parse_provider_model(primary)]
    for fb in fallbacks:
        chain.append(_parse_provider_model(fb))
    return chain, "config"
```

`DEFAULT_CHAIN` is retained as fallback only if `openclaw.json` is missing or unparseable.

Each `route()` call logs the resolved chain:
```
[TS] MODEL_CHAIN_RESOLVED source=config chain=[anthropic/claude-sonnet-4-6 -> openai/gpt-4.1-mini -> ...] req=<req_id>
```

**Canonical source of truth for chain:** `~/.openclaw/openclaw.json` → `agents.defaults.model.primary` + `agents.defaults.model.fallbacks`

---

## Remaining Known Issues (post-freeze backlog)

| Issue | Severity | Notes |
|---|---|---|
| `stall_seen.txt` unbounded growth | low | No TTL/prune policy yet. Suggest 7-day trim. |
| No log rotation on any watchdog log | medium | watchdog.log at 800+ lines after 1 day |
| Hardcoded `/Users/AGENT/` in 2 places in watchdog script | low | Should use `$STATE_DIR` consistently |
| `send_msg` called on every heartbeat | low | Silent-fails in launchd env; restrict to RECOVERY/FAIL only |
| `LOOP_MS` is second-precision labeled as ms | low | `date +%s` granularity; cosmetic |
| `WATCHDOG_TARGET` / `WATCHDOG_WAIT_SECS` dead in plist | low | Script redefines them locally |
| Test files in production watchdog dir | low | Should move to `tests/` subdir |
