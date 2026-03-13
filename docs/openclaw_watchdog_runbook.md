# OpenClaw Watchdog Runbook

**Canonical path (code):** `openclaw-ops/scripts/watchdog/hendrik_watchdog.sh`
**Runtime path:** `~/.openclaw/watchdog/hendrik_watchdog.sh`
**launchd plist:** `openclaw-ops/launchd/ai.openclaw.hendrik_watchdog.plist`
**Trilium:** `System / Watchdog Runbook`

---

## Purpose

The watchdog is a launchd-managed bash script that runs every 300s and:

1. Verifies the OpenClaw gateway is listening on port 18789
2. Probes the gateway and auto-recovers via `launchctl kickstart` if down
3. Invokes the stall detector to scan recent model calls for latency anomalies
4. Emits an enriched heartbeat line to `watchdog.log` and `status.log`
5. Sends RECOVERY/FAIL alerts via `openclaw channels send` (WhatsApp)
6. Owns hygiene governance surfaces in Watchdog v1.2 via the 15-minute hygiene loop

---

## Thresholds and Timeouts

| Parameter | Value | Location |
|---|---|---|
| Watchdog interval | 300s | plist `StartInterval` |
| Gateway recovery wait | 15s | `WAIT_SECS` in script |
| Model WARN threshold | 45,000ms | passed to `stall_detector.py` |
| Model HARD threshold | 90,000ms | passed to `stall_detector.py` |
| Model router hard cap | 90s | `DEFAULT_TIMEOUT_S` in `model_router.py` |
| Hygiene interval | 900s | `ai.openclaw.hygiene_guard` LaunchAgent |

---

## Canonical GitHub Paths

```
openclaw-ops/
  scripts/watchdog/
    hendrik_watchdog.sh       ← main watchdog script
    stall_detector.py         ← JSONL scanner + dedup
    model_router.py           ← timeout enforcement + failover
    tests/
      test_stall.sh           ← P1 acceptance test
      test_forced_failover.py ← P1.1 acceptance tests (13 assertions)
  launchd/
    ai.openclaw.hendrik_watchdog.plist
  templates/
    watchdog.env.example
```

Runtime state (not in repo):
```
~/.openclaw/watchdog/
  heartbeat.log        ← one line per run: HB timestamp + host
  watchdog.log         ← verbose run log
  status.log           ← structured per-cycle status line
  stall.log            ← WARN/HARD/TIMEOUT_ABORT/FAILOVER events
  stall_seen.txt       ← dedup ledger
  model_state.json     ← last model provider/status (written by model_router.py)
  hygiene.log          ← hygiene pass output
  hygiene_state.json   ← hygiene state and last-pass data
  owned/hygiene_guard.sh ← Watchdog-owned hygiene capability (v1.2 surfaces; script stamp remains 1.1)
```

---

## Watchdog v1.2 Hygiene Ownership

Watchdog v1.2 keeps hygiene guard as owned Watchdog behavior. It is not documented or operated as a separate external cleanup utility.

- LaunchAgent label: `ai.openclaw.hygiene_guard`
- Runtime target: `~/.openclaw/watchdog/owned/hygiene_guard.sh`
- Version stamp: `WATCHDOG_HYGIENE_VERSION=1.1`
- `restore_staging_guard`: warn at 100MB, prune at 250MB, always log `restore_staging_pressure`, skip on `.restore_lock` / `.active`, and suppress repeat prunes for 30 minutes
- Sentinel companion signal: `SENTINEL_DISK_PRESSURE` with `disk_mb`, smoothed `growth_mb_per_hr`, `pressure_state`, `time_to_pressure_hrs`
- Sentinel predictive state: `~/.openclaw/watchdog/sentinel_predictive_state.json`
- OCTriage companion artifact: `watchdog_disk_usage.txt`; includes total usage, subtree breakdown (`backups`, `lazarus`, optional `gtm_exports`), `watchdog_growth_rate_mb_hr`, and `watchdog_bloat_warning=true` when watchdog exceeds 500MB
- Compaction correlator companion event: `COMPACTION_PRESSURE`

---

## How to Validate

```bash
# 1. Check cadence (expect entry every ~300s)
tail -n 5 ~/.openclaw/watchdog/heartbeat.log

# 2. Check last structured status
tail -n 5 ~/.openclaw/watchdog/status.log

# 3. Check launchd registration
launchctl print gui/$UID/ai.openclaw.hendrik_watchdog | grep -E "run interval|last exit|runs"

# 4. Force a run and inspect enriched heartbeat
launchctl kickstart -k gui/$UID/ai.openclaw.hendrik_watchdog
sleep 45
grep "HEARTBEAT" ~/.openclaw/watchdog/watchdog.log | tail -1

# 5. Run acceptance tests from repo
cd openclaw-ops/scripts/watchdog/tests
bash test_stall.sh
TEST_MODE=1 python3 test_forced_failover.py

# 6. Verify hygiene ownership surfaces (v1.2)
launchctl print gui/$UID/ai.openclaw.hygiene_guard
tail -n 20 ~/.openclaw/watchdog/hygiene.log
cat ~/.openclaw/watchdog/hygiene_state.json
cat ~/.openclaw/watchdog/restore_staging_prune_state.json
cat ~/.openclaw/watchdog/sentinel_predictive_state.json
```

---

## Expected Log Lines

```
# heartbeat.log
HB 2026-02-24 16:30:15 EST watchdog run user=AGENT uid=503 host=AGENTMacBook.localdomain

# status.log
2026-02-24 16:30:46 EST status: port18789=yes probe=ok model_primary=anthropic/claude-sonnet-4-6

# watchdog.log (enriched heartbeat)
[2026-02-24 16:30:46 EST] HEARTBEAT: [openclaw][watchdog] HB 2026-02-24 16:30:46 EST: gateway OK (port 18789 listening, probe OK) loop_ms=7000 last_model_age_s=120 last_model_provider=anthropic last_model_status=ok
```

---

## Rollback Steps

1. **Watchdog fires too frequently or storms:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/ai.openclaw.hendrik_watchdog.plist
   # Edit StartInterval in plist
   launchctl load ~/Library/LaunchAgents/ai.openclaw.hendrik_watchdog.plist
   ```

2. **Watchdog script broken (exits early):**
   ```bash
   bash -x ~/.openclaw/watchdog/hendrik_watchdog.sh 2>&1 | head -40
   # Check launchd.err.log for stderr output
   tail -n 20 ~/.openclaw/watchdog/launchd.err.log
   ```

3. **Restore from repo:**
   ```bash
   cp openclaw-ops/scripts/watchdog/hendrik_watchdog.sh ~/.openclaw/watchdog/
   chmod +x ~/.openclaw/watchdog/hendrik_watchdog.sh
   launchctl kickstart -k gui/$UID/ai.openclaw.hendrik_watchdog
   ```

4. **Disable watchdog entirely:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/ai.openclaw.hendrik_watchdog.plist
   ```
