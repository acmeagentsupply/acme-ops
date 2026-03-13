# Stall Detector Runbook

**Canonical path (code):** `openclaw-ops/scripts/watchdog/stall_detector.py`
**Runtime path:** `~/.openclaw/watchdog/stall_detector.py`
**Trilium:** `System / Stall Detector Runbook`

---

## Purpose

The stall detector is a Python 3.9 stdlib script invoked by the watchdog on every cycle.
It scans recent OpenClaw session JSONL files for model call latency anomalies and:

- Emits `MODEL_STALL_WARN` when a call exceeds 45s
- Emits `MODEL_STALL_HARD` when a call exceeds 90s
- Detects in-session model changes (potential failover indicator) and emits `MODEL_FAILOVER_TRIGGERED`
- Deduplicates events per `req_id` via `stall_seen.txt`
- Outputs `provider|age_s|status` to stdout for watchdog heartbeat enrichment
- Merges `model_state.json` (written by `model_router.py`) if more recent than JSONL data

---

## Thresholds

| Threshold | Value | Trigger |
|---|---|---|
| WARN | 45,000ms | one `MODEL_STALL_WARN` line per req_id |
| HARD | 90,000ms | one `MODEL_STALL_HARD` line per req_id (in addition to WARN) |
| Lookback window | 700s | sessions older than this are skipped |

Both WARN and HARD fire for the same call when duration >= 90s (HARD threshold includes WARN).

---

## Canonical GitHub Paths

```
openclaw-ops/scripts/watchdog/stall_detector.py
openclaw-ops/scripts/watchdog/tests/test_stall.sh
```

Runtime state files:
```
~/.openclaw/watchdog/stall.log           ← event log (WARN/HARD/FAILOVER lines)
~/.openclaw/watchdog/stall_seen.txt      ← dedup ledger (warn:<req> / hard:<req> / fo:<req>)
~/.openclaw/watchdog/model_state.json    ← last model stats from model_router.py
~/.openclaw/agents/main/sessions/*.jsonl ← scanned input (not modified)
```

---

## How to Validate

```bash
# 1. Check recent stall events
cat ~/.openclaw/watchdog/stall.log

# 2. Check dedup ledger
cat ~/.openclaw/watchdog/stall_seen.txt

# 3. Run acceptance test (creates synthetic 95s session, verifies WARN + HARD, verifies dedup)
bash openclaw-ops/scripts/watchdog/tests/test_stall.sh

# 4. Run detector manually against live sessions
python3 openclaw-ops/scripts/watchdog/stall_detector.py \
  ~/.openclaw/agents/main/sessions \
  /tmp/test_stall.log \
  /tmp/test_stall_seen.txt \
  45000 90000 "$(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "stdout (provider|age_s|status): $?"
```

---

## Expected Log Lines

```
# stall.log — WARN (call > 45s, < 90s)
[2026-02-24 16:50:39 EST] MODEL_STALL_WARN provider=anthropic duration_ms=71812 model=claude-sonnet-4-6 req=3423fbd7-...:df92f686

# stall.log — WARN + HARD (call > 90s, both lines emitted)
[2026-02-24 16:39:37 EST] MODEL_STALL_WARN provider=anthropic duration_ms=140470 model=claude-sonnet-4-6 req=3423fbd7-...:cd994483
[2026-02-24 16:39:37 EST] MODEL_STALL_HARD provider=anthropic duration_ms=140470 model=claude-sonnet-4-6 req=3423fbd7-...:cd994483

# stall.log — in-session failover detected
[2026-02-24 16:39:37 EST] MODEL_FAILOVER_TRIGGERED from=anthropic/claude-sonnet-4-6 to=openai/gpt-4.1-mini req=3423fbd7-...:cd994483
```

---

## Dedup Ledger Format

`stall_seen.txt` contains one entry per line:
```
warn:<session_id>:<message_id>
hard:<session_id>:<message_id>
fo:<session_id>:<message_id>
```

Each req_id is logged at most once per level. A HARD call produces two entries (warn: + hard:).

**Note:** `stall_seen.txt` is unbounded. Prune entries older than 7 days periodically (not yet automated).

---

## Test Isolation

The acceptance test (`test_stall.sh`) uses a private temp directory and never writes to the production `stall_seen.txt` or `stall.log`. Safe to run at any time.

---

## Rollback Steps

1. **False positives flooding stall.log:**
   - Check if a slow real session is being repeatedly detected
   - If the `stall_seen.txt` dedup is working correctly, each req_id appears at most once
   - If `stall_seen.txt` is corrupted: `echo "" > ~/.openclaw/watchdog/stall_seen.txt`

2. **Stall detector crashes silently (no heartbeat enrichment):**
   ```bash
   python3 ~/.openclaw/watchdog/stall_detector.py \
     ~/.openclaw/agents/main/sessions \
     /tmp/debug_stall.log \
     /tmp/debug_seen.txt \
     45000 90000 "$(date '+%Y-%m-%d %H:%M:%S %Z')"
   ```
   The watchdog catches failures with `|| STALL_RESULT="unknown|-1|unknown"` — heartbeat continues.

3. **Restore from repo:**
   ```bash
   cp openclaw-ops/scripts/watchdog/stall_detector.py ~/.openclaw/watchdog/
   ```
