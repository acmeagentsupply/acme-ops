#!/usr/bin/env python3
"""
compaction_log_parser.py — Parse gateway log (via watchdog.log) for compaction events.
Writes COMPACTION_START / COMPACTION_END / COMPACTION_SUSPECT NDJSON events to ops_events.log.

Patterns detected:
  COMPACTION_START:   [compaction-safeguard] Compaction safeguard: new content uses X% ...
  COMPACTION_END:     [agent/embedded] embedded run timeout: runId=... timeoutMs=...
                      [agent/embedded] using current snapshot: timed out during compaction
  COMPACTION_SUSPECT: Indirect: probe=fail in status.log with no logged compaction event

Usage:
  python3 compaction_log_parser.py [--dry-run] [--since ISO8601]
"""

import sys
import os
import re
import json
from datetime import datetime, timezone, timedelta

WATCHDOG_LOG  = os.path.expanduser("~/.openclaw/watchdog/watchdog.log")
OPS_EVENTS_LOG = os.path.expanduser("~/.openclaw/watchdog/ops_events.log")
STATUS_LOG    = os.path.expanduser("~/.openclaw/watchdog/status.log")
PARSER_STATE  = os.path.expanduser("~/.openclaw/watchdog/compaction_parser_state.json")

DRY_RUN = "--dry-run" in sys.argv

# Parse --since arg
SINCE_DT = None
for i, arg in enumerate(sys.argv):
    if arg == "--since" and i + 1 < len(sys.argv):
        try:
            SINCE_DT = datetime.fromisoformat(sys.argv[i+1].replace("Z", "+00:00"))
        except Exception:
            pass


def load_state():
    try:
        with open(PARSER_STATE) as f:
            return json.load(f)
    except Exception:
        return {"last_watchdog_pos": 0, "last_run_ts": None}


def save_state(state):
    if DRY_RUN:
        return
    os.makedirs(os.path.dirname(PARSER_STATE), exist_ok=True)
    with open(PARSER_STATE, "w") as f:
        json.dump(state, f)


def parse_ts_from_line(line):
    """Extract ISO8601 timestamp from watchdog.log tail line."""
    m = re.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)', line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except Exception:
            pass
    return None


def append_event(event_dict):
    line = json.dumps(event_dict)
    if DRY_RUN:
        print(f"[DRY-RUN] {line}")
        return
    with open(OPS_EVENTS_LOG, "a") as f:
        f.write(line + "\n")
    print(f"WROTE: {line}")


def parse_watchdog_log(since_pos=0):
    """
    Parse watchdog.log for compaction-related entries.
    Returns list of event dicts and new file position.
    """
    events = []
    pending_start = None  # Track open COMPACTION_START awaiting END

    if not os.path.exists(WATCHDOG_LOG):
        return events, since_pos

    with open(WATCHDOG_LOG, "r", errors="replace") as f:
        f.seek(since_pos)
        for line in f:
            line = line.strip()
            if not line:
                continue

            ts = parse_ts_from_line(line)
            if ts is None:
                continue
            if SINCE_DT and ts < SINCE_DT:
                continue

            ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Pattern 1: compaction-safeguard → COMPACTION_START
            if "[compaction-safeguard]" in line and "Compaction safeguard" in line:
                m_pct = re.search(r'(\d+\.?\d*)% of context', line)
                m_msg = re.search(r'dropped (\d+) older chunk\(s\) \((\d+) messages\)', line)
                ctx_pct = float(m_pct.group(1)) if m_pct else None
                chunks = int(m_msg.group(1)) if m_msg else None
                msgs = int(m_msg.group(2)) if m_msg else None
                ev = {
                    "ts": ts_str, "event": "COMPACTION_START", "source": "watchdog_log",
                    "context_pct": ctx_pct, "chunks_dropped": chunks, "messages_dropped": msgs
                }
                events.append(ev)
                pending_start = ts

            # Pattern 2: timed out during compaction → COMPACTION_END (timeout variant)
            elif "timed out during compaction" in line:
                m_run = re.search(r'runId=(\S+)', line)
                run_id = m_run.group(1) if m_run else None
                duration_s = round((ts - pending_start).total_seconds(), 1) if pending_start else None
                ev = {
                    "ts": ts_str, "event": "COMPACTION_END", "source": "watchdog_log",
                    "reason": "timeout", "run_id": run_id, "duration_s": duration_s
                }
                events.append(ev)
                pending_start = None

            # Pattern 3: embedded run timeout (may be compaction-related)
            elif "embedded run timeout" in line:
                m_run = re.search(r'runId=(\S+)', line)
                m_tmo = re.search(r'timeoutMs=(\d+)', line)
                run_id = m_run.group(1) if m_run else None
                timeout_ms = int(m_tmo.group(1)) if m_tmo else None
                # Only flag as compaction suspect if timeout is 600s (compaction default)
                if timeout_ms and timeout_ms >= 600000:
                    ev = {
                        "ts": ts_str, "event": "COMPACTION_SUSPECT", "source": "watchdog_log",
                        "reason": "embedded_run_timeout", "run_id": run_id,
                        "timeout_ms": timeout_ms,
                        "note": "600s timeout consistent with compaction stall"
                    }
                    events.append(ev)

        new_pos = f.tell()

    return events, new_pos


def detect_probe_fail_suspects():
    """
    Indirect detection: probe failures in status.log that have no COMPACTION event
    within ±10 min. These are COMPACTION_SUSPECT candidates.
    """
    suspects = []
    if not os.path.exists(STATUS_LOG):
        return suspects

    # Load existing ops_events to avoid duplication
    existing_ts = set()
    if os.path.exists(OPS_EVENTS_LOG):
        with open(OPS_EVENTS_LOG) as f:
            for line in f:
                try:
                    ev = json.loads(line.strip())
                    if ev.get("event", "").startswith("COMPACTION"):
                        existing_ts.add(ev.get("ts", ""))
                except Exception:
                    pass

    # Parse status.log for probe failures
    fail_pattern = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \S+ status:.*port18789=yes.*probe=fail'
    )
    with open(STATUS_LOG) as f:
        for line in f:
            m = fail_pattern.match(line.strip())
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    ts_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    # Check if we already have a compaction event near this time
                    # (simple dedup: if ts_str within 10min of any existing compaction)
                    already_covered = False
                    for ets in existing_ts:
                        try:
                            edt = datetime.fromisoformat(ets.replace("Z", "+00:00"))
                            if abs((dt - edt).total_seconds()) < 600:
                                already_covered = True
                                break
                        except Exception:
                            pass
                    if not already_covered:
                        ev = {
                            "ts": ts_str, "event": "COMPACTION_SUSPECT",
                            "source": "status_log_inference",
                            "reason": "port_up_probe_fail",
                            "note": "probe failed while port was up; possible compaction stall"
                        }
                        suspects.append(ev)
                        existing_ts.add(ts_str)  # prevent same-ts dupes
                except Exception:
                    pass
    return suspects


def main():
    state = load_state()
    since_pos = state.get("last_watchdog_pos", 0)

    print(f"Parsing watchdog.log from pos={since_pos} ...")
    events, new_pos = parse_watchdog_log(since_pos)
    print(f"  Found {len(events)} direct compaction events")

    suspects = detect_probe_fail_suspects()
    print(f"  Found {len(suspects)} probe-fail suspects (indirect)")

    # Dedup by (ts, event, run_id/reason) — watchdog.log has repeated entries
    seen = set()
    deduped = []
    for ev in events + suspects:
        key = (ev.get("ts"), ev.get("event"), ev.get("run_id",""), ev.get("reason",""))
        if key not in seen:
            seen.add(key)
            deduped.append(ev)
    all_events = sorted(deduped, key=lambda e: e.get("ts", ""))

    for ev in all_events:
        append_event(ev)

    if not DRY_RUN:
        state["last_watchdog_pos"] = new_pos
        state["last_run_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state(state)

    print(f"Done. Wrote {len(all_events)} events. New watchdog.log pos={new_pos}")
    return all_events


if __name__ == "__main__":
    main()
