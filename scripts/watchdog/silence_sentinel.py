#!/usr/bin/env python3
"""
Heartbeat Silence Sentinel v1
Detects when watchdog heartbeats have not updated within a threshold.

Behavior:
  Reads last "HB ... watchdog run ..." line from heartbeat log
  Calculates age = now - last_hb_ts
  Emits WARN event to stall.log if age >= warn_after_s and rate-limit passed
  Writes compact line to stdout: silence_warn=0|1 silence_age_s=N|unknown

Does NOT raise exceptions. Degrades gracefully.

Config:
  ~/.openclaw/openclaw.json
  agents.silence_sentinel, with defaults if missing

State persistence:
  ~/.openclaw/watchdog/silence_state.json
  last_warn_ts

Exit code always 0
"""
import sys
import os
import json
from datetime import datetime, timezone, timedelta
import time

HEARTBEAT_LOG_PATH = os.path.expanduser("~/.openclaw/watchdog/heartbeat.log")
SILENCE_STATE_PATH = os.path.expanduser("~/.openclaw/watchdog/silence_state.json")
CONFIG_PATH = os.path.expanduser("~/.openclaw/openclaw.json")
STALL_LOG       = os.path.join(os.path.expanduser("~/.openclaw/watchdog/"), "stall.log")

DEFAULT_WARN_AFTER_S = 420
DEFAULT_RATE_LIMIT_S = 900


def ts():
    # Using UTC for consistency in state file and parsing
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        ss = cfg.get("agents", {}).get("silence_sentinel", {})
        enabled = ss.get("enabled", True)
        warn_after = int(ss.get("warn_after_s", DEFAULT_WARN_AFTER_S))
        rate_limit = int(ss.get("rate_limit_s", DEFAULT_RATE_LIMIT_S))
    except Exception:
        enabled = True
        warn_after = DEFAULT_WARN_AFTER_S
        rate_limit = DEFAULT_RATE_LIMIT_S
    return enabled, warn_after, rate_limit


def parse_hb_log():
    try:
        if not os.path.exists(HEARTBEAT_LOG_PATH):
            return None
        with open(HEARTBEAT_LOG_PATH, "r") as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "HB " in line and "watchdog run" in line:
                try:
                    # Extract timestamp part: "HB YYYY-MM-DD HH:MM:SS ZZZ ..."
                    ts_str = line.split("HB ", 1)[1].split(" watchdog run", 1)[0].strip()
                    # Make it UTC for consistent comparison
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                    return dt
                except Exception:
                    return None
        return None
    except Exception:
        return None


def load_silence_state():
    try:
        if not os.path.exists(SILENCE_STATE_PATH):
            return 0
        with open(SILENCE_STATE_PATH, "r") as f:
            state = json.load(f)
        return int(state.get("last_warn_ts", 0))
    except Exception:
        return 0


def save_silence_state(ts_val):
    try:
        os.makedirs(os.path.dirname(SILENCE_STATE_PATH), exist_ok=True)
        with open(SILENCE_STATE_PATH, "w") as f:
            json.dump({"last_warn_ts": ts_val}, f)
    except Exception:
        pass


def emit_warn(age, warn_after, rate_limit):
    now_ts = int(time.time())
    last_warn_ts = load_silence_state()

    if (now_ts - last_warn_ts) < rate_limit:
        return False  # Rate limited

    # Emit stall.log warn line
    line = f"AGENT_SILENCE_WARN agent=hendrik last_hb_age_s={age} warn_after_s={warn_after} rate_limit_s={rate_limit}"
    try:
        os.makedirs(os.path.dirname(STALL_LOG), exist_ok=True)
        with open(STALL_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

    save_silence_state(now_ts)
    return True


def main():
    enabled, warn_after, rate_limit = load_config()
    silence_warn = 0
    silence_age_s = "unknown"

    if not enabled:
        print(f"silence_warn={silence_warn} silence_age_s={silence_age_s}")
        sys.exit(0)

    hb_dt = parse_hb_log()

    if hb_dt is None:
        # If HB log is missing or unparseable, consider it silent for unknown duration
        silence_age_s = "unknown"
        if emit_warn(silence_age_s, warn_after, rate_limit):
            silence_warn = 1
    else:
        now_dt = datetime.now(timezone.utc)
        age_s = int((now_dt - hb_dt).total_seconds())
        silence_age_s = age_s

        if age_s >= warn_after:
            if emit_warn(age_s, warn_after, rate_limit):
                silence_warn = 1

    print(f"silence_warn={silence_warn} silence_age_s={silence_age_s}")

if __name__ == "__main__":
    main()
