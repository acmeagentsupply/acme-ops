# Compaction Mitigation v1 — Runbook
TASK_ID: A-RC-P3-001
STATUS: ACTIVE
OWNER: GP-OPS (Lars / Hendrik)
LAST_UPDATED: 2026-02-26

---

## Problem Statement
OpenClaw compaction triggers at ~86% context budget. When the compaction process
times out (≥10 min), the next agent turn immediately re-triggers compaction at
the same 86% threshold — producing back-to-back 10-minute timeout storms.

### Evidence (2026-02-26)
- Two consecutive COMPACTION_END(timeout) events: duration_s=599.9 and 600.0
- Context at trigger: 86% (chunks_dropped=2, messages_dropped=669)
- Acceleration ratio: 36x (12.0/day observed vs 0.33/day historical baseline)
- Gateway dead by 08:10 EST; required full reboot

---

## Architecture

### compaction_budget_sentinel.py
Location: `~/.openclaw/watchdog/compaction_budget_sentinel.py`
Called by: `hendrik_watchdog.sh` once per loop
Output: one-line summary injected into `status.log`

Alert levels:
| Level   | Condition                                    |
|---------|----------------------------------------------|
| NOMINAL | No compaction events in detection windows    |
| SUSPECT | ≥1 COMPACTION_SUSPECT in last 2h             |
| ACTIVE  | ≥1 COMPACTION_START in last 30min            |
| STORM   | ≥2 COMPACTION_END(timeout) in last 2h        |

On STORM (first detection, cooldown 30min):
1. Emits `COMPACTION_STORM_ALERT` to `ops_events.log`
2. Fires `openclaw cron wake` with human-readable alert text
3. Writes state to `compaction_alert_state.json`

On STORM clear:
1. Emits `COMPACTION_STORM_CLEARED` to `ops_events.log`
2. Resets `storm_active=false` in state file

### Status log enrichment
Every watchdog cycle now includes:
```
comp_storm=0 comp_active=0 comp_events_2h=N comp_alert=NOMINAL
```

---

## What Compaction Mitigation v1 Does NOT Do
- Cannot change OpenClaw's compaction trigger threshold (would require openclaw.json edit)
- Cannot abort a running compaction
- Cannot directly prevent context accumulation

These are architectural limits. The mitigation layer is observability + early warning.

---

## Operator Playbook

### When you see comp_alert=STORM in status.log:
1. **Start a fresh session** — this resets context budget to 0%
2. If you can't start fresh, check if current task is near completion; finish it
3. After fresh session, verify: `python3 ~/.openclaw/watchdog/compaction_budget_sentinel.py`
   → Should show `comp_alert=NOMINAL`

### When you see comp_alert=ACTIVE:
- Compaction is running now; avoid large context operations (long pastes, file reads)
- Consider finishing current thought and starting fresh if not urgent

### When you see comp_alert=SUSPECT:
- Port up but probe failing — possible frozen loop mid-compaction
- Monitor for 2-3 more watchdog cycles; if probe stays down → `openclaw gateway restart`

---

## Session Hygiene Recommendations (ROOT CAUSE MITIGATION)
The only way to prevent compaction storms is to never let context hit 86%.

1. **Target fresh session every ~2 hours** of intensive work
2. **Avoid pasting large files** (>500 lines) into context — use file paths instead
3. **Compact early** — if context feels heavy, start a new session proactively
4. **Watch comp_events_2h** in status.log; if it's climbing, refresh soon

---

## Files

| File | Purpose |
|------|---------|
| `~/.openclaw/watchdog/compaction_budget_sentinel.py` | Sentinel script |
| `~/.openclaw/watchdog/compaction_alert_state.json` | Alert state (storm/level/ts) |
| `~/.openclaw/watchdog/ops_events.log` | COMPACTION_STORM_ALERT events |
| `~/.openclaw/watchdog/status.log` | comp_* fields per cycle |

---

## Rollback
```bash
# Remove sentinel call from watchdog (revert to prior version)
git revert HEAD   # or git revert <commit-hash>

# Remove state file if needed
rm ~/.openclaw/watchdog/compaction_alert_state.json
```

---

## Future Work (P4 candidates)
- **A-RC-P4-001**: Context budget monitor — detect context_pct via gateway probe before
  compaction triggers (requires reliable probe; blocked by RC_ENV_004 HIGH)
- **A-RC-P4-002**: Compaction budget alerting via WhatsApp (via watchdog send_msg path)
  when STORM detected with active_flag=1
- **A-RC-P4-003**: Auto-throttle mode — pause non-urgent watchdog sub-tasks during active compaction
