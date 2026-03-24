#!/usr/bin/env python3
"""
Compaction Budget Sentinel — A-RC-P3-001
Detects compaction storms and active compactions from ops_events.log.
Outputs one-line summary for watchdog status injection.
Emits NDJSON events on state change. Fires cron wake on new STORM.
Always exits 0 (safe for watchdog inclusion).
"""

import json
import os
import sys
import datetime
import subprocess

STATE_DIR = os.path.expanduser("~/.openclaw/watchdog")
OPS_LOG   = os.path.join(STATE_DIR, "ops_events.log")
ALERT_STATE = os.path.join(STATE_DIR, "compaction_alert_state.json")

STORM_WINDOW_S  = 7200   # 2h: look-back for storm detection
ACTIVE_WINDOW_S = 1800   # 30min: look-back for active compaction
COOLDOWN_S      = 1800   # 30min: min time between cron wake alerts
STORM_THRESH    = 2      # ≥N timeout-ENDs in window → STORM

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def read_recent_events(lookback_s):
    """Read ops_events.log, return events within lookback_s seconds of now."""
    cutoff = now_utc() - datetime.timedelta(seconds=lookback_s)
    events = []
    try:
        with open(OPS_LOG, "r") as f:
            # Read last 2000 lines for efficiency
            lines = f.readlines()[-2000:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(obj.get("ts", ""))
            if ts and ts >= cutoff:
                events.append(obj)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return events

def load_alert_state():
    try:
        with open(ALERT_STATE, "r") as f:
            return json.load(f)
    except Exception:
        return {"storm_active": False, "last_storm_ts": None, "last_wake_ts": None}

def save_alert_state(state):
    try:
        with open(ALERT_STATE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def append_ops_event(event_dict):
    try:
        event_dict["ts"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(OPS_LOG, "a") as f:
            f.write(json.dumps(event_dict) + "\n")
    except Exception:
        pass

def fire_cron_wake(msg):
    """Fire openclaw cron wake to inject alert into main session."""
    try:
        subprocess.run(
            ["openclaw", "cron", "wake", msg],
            timeout=10,
            capture_output=True
        )
    except Exception:
        pass

def main():
    storm_window_events  = read_recent_events(STORM_WINDOW_S)
    active_window_events = read_recent_events(ACTIVE_WINDOW_S)
    state = load_alert_state()
    now   = now_utc()

    # --- Count COMPACTION events ---
    timeout_ends_2h = [
        e for e in storm_window_events
        if e.get("event") == "COMPACTION_END" and e.get("reason") == "timeout"
    ]
    starts_30m = [
        e for e in active_window_events
        if e.get("event") == "COMPACTION_START"
    ]
    suspects_2h = [
        e for e in storm_window_events
        if e.get("event") == "COMPACTION_SUSPECT"
    ]

    total_comp_2h = len([
        e for e in storm_window_events
        if e.get("event", "").startswith("COMPACTION_")
    ])

    storm_now    = len(timeout_ends_2h) >= STORM_THRESH
    active_now   = len(starts_30m) > 0
    timeout_2h   = len(timeout_ends_2h)
    suspect_2h   = len(suspects_2h)

    # --- Determine alert level ---
    if storm_now:
        alert_level = "STORM"
    elif active_now:
        alert_level = "ACTIVE"
    elif suspect_2h > 0:
        alert_level = "SUSPECT"
    else:
        alert_level = "NOMINAL"

    # --- State change detection ---
    prev_storm = state.get("storm_active", False)
    new_storm  = storm_now and not prev_storm

    if new_storm:
        # Emit ops event
        append_ops_event({
            "event": "COMPACTION_STORM_ALERT",
            "timeout_count_2h": timeout_2h,
            "suspect_count_2h": suspect_2h,
            "note": f"Back-to-back timeout storms detected ({timeout_2h} timeouts in 2h window)"
        })

        # Cooldown check before cron wake
        last_wake = state.get("last_wake_ts")
        can_wake  = True
        if last_wake:
            lw = parse_ts(last_wake)
            if lw and (now - lw).total_seconds() < COOLDOWN_S:
                can_wake = False

        if can_wake:
            fire_cron_wake(
                f"[COMPACTION STORM] {timeout_2h} timeout compactions in last 2h. "
                f"Suspects: {suspect_2h}. Recommend starting a fresh session to clear context budget."
            )
            state["last_wake_ts"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    elif not storm_now and prev_storm:
        # Storm cleared
        append_ops_event({
            "event": "COMPACTION_STORM_CLEARED",
            "note": "Compaction storm window passed (no new timeout-ENDs in 2h)"
        })

    # Update state
    state["storm_active"]   = storm_now
    state["last_storm_ts"]  = now.strftime("%Y-%m-%dT%H:%M:%SZ") if storm_now else state.get("last_storm_ts")
    state["alert_level"]    = alert_level
    state["timeout_2h"]     = timeout_2h
    state["comp_events_2h"] = total_comp_2h
    save_alert_state(state)

    # --- Output for watchdog status line ---
    # Format: comp_storm=0 comp_active=0 comp_events_2h=N comp_alert=NOMINAL
    storm_flag  = 1 if storm_now  else 0
    active_flag = 1 if active_now else 0
    print(
        f"comp_storm={storm_flag} comp_active={active_flag} "
        f"comp_events_2h={total_comp_2h} comp_alert={alert_level}"
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never crash watchdog — emit safe fallback
        print("comp_storm=err comp_active=err comp_events_2h=err comp_alert=ERROR")
        sys.exit(0)
