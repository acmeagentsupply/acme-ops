#!/usr/bin/env python3
"""
compaction_correlate.py — Compaction Impact Correlation Engine v1
Correlates ops_events.log compaction windows against:
  - stall.log (MODEL_STALL_*)
  - status.log (probe failures)
  - model_events.log / stall.log (failovers)

Produces JSON summary to stdout.
Usage: python3 compaction_correlate.py [--window-s 30]
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

WATCHDOG_DIR = os.path.expanduser("~/.openclaw/watchdog")

OPS_EVENTS_LOG    = os.path.join(WATCHDOG_DIR, "ops_events.log")
STALL_LOG         = os.path.join(WATCHDOG_DIR, "stall.log")
STATUS_LOG        = os.path.join(WATCHDOG_DIR, "status.log")
MODEL_EVENTS_LOG  = os.path.join(WATCHDOG_DIR, "model_events.log")
AGENT911_STATE    = os.path.join(WATCHDOG_DIR, "agent911_state.json")
SENTINEL_STATE    = os.path.join(WATCHDOG_DIR, "sentinel_protection_state.json")
BACKUPS_DIR       = os.path.join(WATCHDOG_DIR, "backups")

DEFAULT_WINDOW_S = 30


def parse_iso(ts_str):
    """Parse ISO8601 UTC timestamp to datetime."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_compaction_windows(path):
    """
    Parse ops_events.log for GATEWAY_COMPACTION start/end pairs.
    Returns list of (start_dt, end_dt) tuples. Unpaired starts get +30s synthetic end.
    """
    windows = []
    pending_start = None
    if not os.path.exists(path):
        return windows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "GATEWAY_COMPACTION":
                continue
            ts = parse_iso(ev.get("ts", ""))
            if ts is None:
                continue
            if ev.get("phase") == "start":
                pending_start = ts
            elif ev.get("phase") == "end" and pending_start is not None:
                windows.append((pending_start, ts))
                pending_start = None
    # Unpaired start — synthetic 30s window
    if pending_start is not None:
        windows.append((pending_start, pending_start + timedelta(seconds=30)))
    return windows


def parse_stall_log(path):
    """
    Returns list of (datetime, event_type) from stall.log.
    Lines look like: [2026-02-26 00:24:42] SPHINXGATE_POLICY_EXHAUSTED ...
    or MODEL_STALL_* events.
    """
    events = []
    if not os.path.exists(path):
        return events
    pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\S+)')
    with open(path) as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    events.append((dt, m.group(2)))
                except Exception:
                    pass
    return events


def parse_status_log(path):
    """
    Returns list of (datetime, probe_status) from status.log.
    Lines look like: 2026-02-26 01:55:14 EST status: port18789=yes probe=ok ...
    """
    events = []
    if not os.path.exists(path):
        return events
    pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \S+ status:.*probe=(\S+)')
    with open(path) as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    events.append((dt, m.group(2)))
                except Exception:
                    pass
    return events


def parse_model_events_log(path):
    """
    Returns list of (datetime, event_type) from model_events.log (NDJSON).
    """
    events = []
    if not os.path.exists(path):
        return events
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                ts = parse_iso(ev.get("ts", ""))
                etype = ev.get("event", "")
                if ts and etype:
                    events.append((ts, etype))
            except json.JSONDecodeError:
                pass
    return events


def events_in_window(events, start, end, window_s):
    """Count events within [start-window_s, end+window_s]."""
    lo = start - timedelta(seconds=window_s)
    hi = end   + timedelta(seconds=window_s)
    return [e for e in events if lo <= e[0] <= hi]


def backup_files_modified_last_hour(path):
    if not os.path.isdir(path):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = 0
    for root, _, files in os.walk(path):
        for name in files:
            file_path = os.path.join(root, name)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)
            except Exception:
                continue
            if mtime >= cutoff:
                count += 1
    return count


def compute_pressure_score():
    state = load_json(AGENT911_STATE)
    sentinel = load_json(SENTINEL_STATE)
    compaction = state.get("compaction_state", {})
    risk = str(compaction.get("risk", "LOW")).upper()
    growth = sentinel.get("growth_mb_per_hr", 0)
    if not isinstance(growth, (int, float)):
        growth = 0
    backups_1hr = backup_files_modified_last_hour(BACKUPS_DIR)

    risk_points = {"LOW": 0, "MEDIUM": 40, "HIGH": 80}.get(risk, 0)
    growth_points = 0
    if growth > 150:
        growth_points = 15
    elif growth >= 50:
        growth_points = 10

    backup_points = 0
    if backups_1hr > 3:
        backup_points = 10
    elif backups_1hr >= 1:
        backup_points = 5

    score = risk_points + growth_points + backup_points
    inputs = {
        "compaction_risk": risk,
        "disk_growth_mb_hr": round(float(growth), 1),
        "backup_files_1hr": backups_1hr,
    }
    return score, inputs


def append_pressure_event(score, inputs):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "event": "COMPACTION_PRESSURE",
        "score": score,
        "compaction_risk": inputs["compaction_risk"],
        "disk_growth_mb_hr": inputs["disk_growth_mb_hr"],
        "backup_files_1hr": inputs["backup_files_1hr"],
        "source": "correlator",
    }
    try:
        with open(OPS_EVENTS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass
    return record


def main():
    args = sys.argv[1:]
    window_s = DEFAULT_WINDOW_S
    pressure_mode = "--pressure" in args
    if "--window-s" in args:
        idx = args.index("--window-s")
        try:
            window_s = int(args[idx + 1])
        except Exception:
            window_s = DEFAULT_WINDOW_S

    windows = load_compaction_windows(OPS_EVENTS_LOG)
    stall_events   = parse_stall_log(STALL_LOG)
    status_events  = parse_status_log(STATUS_LOG)
    model_events   = parse_model_events_log(MODEL_EVENTS_LOG)

    compaction_count = len(windows)

    # Duration stats
    durations = [(e - s).total_seconds() for s, e in windows if (e - s).total_seconds() < 300]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    # Correlate each window
    stalls_during       = 0
    failovers_during    = 0
    rpc_failures_during = 0

    for start, end in windows:
        # Stalls: MODEL_STALL_* in stall.log
        stall_hits = events_in_window(stall_events, start, end, window_s)
        stalls_during += sum(1 for _, t in stall_hits if "STALL" in t)

        # RPC failures: probe != ok in status.log
        probe_hits = events_in_window(status_events, start, end, window_s)
        rpc_failures_during += sum(1 for _, p in probe_hits if p != "ok")

        # Failovers: MODEL_FAILOVER or SPHINXGATE_FAILOPEN in stall.log or model_events
        failover_stall = events_in_window(stall_events, start, end, window_s)
        failovers_during += sum(1 for _, t in failover_stall if "FAILOVER" in t or "FAILOPEN" in t)
        failover_model = events_in_window(model_events, start, end, window_s)
        failovers_during += sum(1 for _, t in failover_model if "FAILOVER" in t)

    # Baseline probe failure rate (all time)
    total_probes      = len(status_events)
    total_failures    = sum(1 for _, p in status_events if p != "ok")
    baseline_fail_pct = round(100 * total_failures / total_probes, 1) if total_probes else 0

    summary = {
        "compaction_count":            compaction_count,
        "avg_compaction_duration_s":   avg_duration,
        "stalls_during_compaction":    stalls_during,
        "failovers_during_compaction": failovers_during,
        "rpc_failures_during_compaction": rpc_failures_during,
        "baseline_probe_fail_pct":     baseline_fail_pct,
        "total_probes_in_log":         total_probes,
        "total_stall_events_in_log":   len(stall_events),
        "total_model_events_in_log":   len(model_events),
        "correlation_window_s":        window_s,
        "note": "compaction_count=0 means no GATEWAY_COMPACTION events in ops_events.log yet; populate with manual markers or auto-detection to build history",
    }

    if pressure_mode:
        score, inputs = compute_pressure_score()
        summary["compaction_pressure_score"] = score
        summary["pressure_inputs"] = inputs
        append_pressure_event(score, inputs)

    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
