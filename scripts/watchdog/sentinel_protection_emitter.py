#!/usr/bin/env python3
"""
sentinel_protection_emitter.py
A-SEN-P1-001: Sentinel Protection Event Surfacing

Reads existing Watchdog + Sentinel signals, detects qualifying protection
moments, and emits structured NDJSON events to ops_events.log.

Safety:
  - Read-only except:
      ~/.openclaw/watchdog/ops_events.log  (append-only)
      ~/.openclaw/watchdog/sentinel_protection_state.json  (cooldown state)
  - Never fabricates signals — gracefully skips when evidence unavailable
  - Always exits 0 (watchdog-safe)
  - Per-event 30-minute cooldown prevents event floods

TASK_ID: A-SEN-P1-001
OWNER: GP-OPS
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME          = Path.home()
WATCHDOG_DIR  = HOME / ".openclaw" / "watchdog"
METRICS_DIR   = HOME / ".openclaw" / "metrics"

OPS_EVENTS    = WATCHDOG_DIR / "ops_events.log"
STATE_FILE    = WATCHDOG_DIR / "sentinel_protection_state.json"
WATCHDOG_LOG  = WATCHDOG_DIR / "watchdog.log"
MODEL_EVENTS  = WATCHDOG_DIR / "model_events.log"

COOLDOWN_MINUTES = 30
WINDOW_24H_H     = 24

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _parse_dt(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp — handles both 'Z' and '+00:00' suffixes."""
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _is_on_cooldown(state: dict, event_type: str) -> bool:
    """Return True if event fired recently and should be suppressed."""
    last_ts_str = state.get(event_type)
    if not last_ts_str:
        return False  # never fired — not on cooldown
    try:
        last_ts = _parse_dt(last_ts_str)
        cutoff = _now_utc() - timedelta(minutes=COOLDOWN_MINUTES)
        return last_ts > cutoff  # True = still within cooldown window
    except Exception:
        return False


def _append_event(record: dict) -> bool:
    try:
        with open(OPS_EVENTS, "a") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


def _read_ops_events_24h() -> list:
    """Parse ops_events.log; return records from the last 24h."""
    cutoff = _now_utc() - timedelta(hours=WINDOW_24H_H)
    result = []
    try:
        if not OPS_EVENTS.exists():
            return result
        with open(OPS_EVENTS) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    ts_str = rec.get("ts", "")
                    dt = _parse_dt(ts_str)
                    if dt >= cutoff:
                        result.append(rec)
                except Exception:
                    continue
    except Exception:
        pass
    return result


def _read_watchdog_tail(n: int = 600) -> list:
    """Return up to last n lines of watchdog.log."""
    try:
        if not WATCHDOG_LOG.exists():
            return []
        lines = WATCHDOG_LOG.read_text(errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Detectors — return (detected: bool, evidence: str)
# ---------------------------------------------------------------------------

def _detect_stall_prevented(ops_24h: list) -> tuple:
    """GATEWAY_STALL events in ops_events.log within 24h."""
    stalls = [e for e in ops_24h if e.get("event") == "GATEWAY_STALL"]
    if not stalls:
        return False, ""
    last = stalls[-1]
    evidence = (
        f"{len(stalls)} GATEWAY_STALL event(s) in 24h; "
        f"last at {last.get('ts', 'unknown')} — "
        f"watchdog probe intervention logged"
    )
    return True, evidence


def _detect_compaction_storm(ops_24h: list) -> tuple:
    """3+ compaction events in 24h indicates storm condition."""
    comp_types = {"COMPACTION_START", "COMPACTION_SUSPECT", "COMPACTION_END"}
    comp = [e for e in ops_24h if e.get("event") in comp_types]
    if len(comp) < 3:
        return False, ""
    evidence = (
        f"{len(comp)} compaction events in 24h window — storm pattern; "
        f"sentinel compaction budget guard active"
    )
    return True, evidence


def _detect_heartbeat_recovered(wdlog: list) -> tuple:
    """silence_warn=1 → silence_warn=0 transition in watchdog.log."""
    saw_silence = False
    for line in wdlog:
        if "silence_warn=1" in line:
            saw_silence = True
        elif "silence_warn=0" in line and saw_silence:
            evidence = (
                "Heartbeat silence resolved — silence_warn 1→0 transition "
                "detected in watchdog.log"
            )
            return True, evidence
    return False, ""


def _detect_routing_anomaly(ops_24h: list) -> tuple:
    """POLICY_FAIL or routing anomaly events in ops_events or model_events."""
    anomalies = [
        e for e in ops_24h
        if "POLICY_FAIL" in str(e.get("event", ""))
        or e.get("event") == "ROUTING_ANOMALY"
    ]
    # Also scan model_events.log
    try:
        if MODEL_EVENTS.exists():
            cutoff = _now_utc() - timedelta(hours=WINDOW_24H_H)
            with open(MODEL_EVENTS) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        dt = _parse_dt(rec.get("ts", ""))
                        if dt >= cutoff and rec.get("event") == "POLICY_FAIL":
                            anomalies.append(rec)
                    except Exception:
                        continue
    except Exception:
        pass

    if not anomalies:
        return False, ""
    evidence = (
        f"{len(anomalies)} routing anomaly/policy-fail event(s) in 24h — "
        f"SphinxGate lane enforcement active"
    )
    return True, evidence


def _detect_silence_interrupted(ops_24h: list, wdlog: list) -> tuple:
    """HEARTBEAT_SILENCE event or silence_warn=1 in recent logs."""
    in_ops = [
        e for e in ops_24h
        if "HEARTBEAT_SILENCE" in str(e.get("event", ""))
    ]
    if in_ops:
        evidence = (
            f"HEARTBEAT_SILENCE event in ops_events.log "
            f"at {in_ops[-1].get('ts', 'unknown')} — "
            f"silence sentinel intercepted gap"
        )
        return True, evidence

    silence_lines = [l for l in wdlog if "silence_warn=1" in l]
    if silence_lines:
        evidence = (
            f"Silence condition active in watchdog.log "
            f"({len(silence_lines)} occurrence(s)) — "
            f"silence sentinel actively monitoring"
        )
        return True, evidence

    return False, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DETECTORS = [
    ("SENTINEL_PROTECTION_STALL_PREVENTED",      "HIGH",   _detect_stall_prevented),
    ("SENTINEL_PROTECTION_COMPACTION_STORM",     "HIGH",   _detect_compaction_storm),
    ("SENTINEL_PROTECTION_HEARTBEAT_RECOVERED",  "MEDIUM", _detect_heartbeat_recovered),
    ("SENTINEL_PROTECTION_ROUTING_ANOMALY",      "MEDIUM", _detect_routing_anomaly),
    ("SENTINEL_PROTECTION_SILENCE_INTERRUPTED",  "MEDIUM", _detect_silence_interrupted),
]


def main() -> None:
    now_ts   = _ts()
    state    = _load_state()
    ops_24h  = _read_ops_events_24h()
    wdlog    = _read_watchdog_tail()

    emitted   = []
    suppressed = []

    for event_type, severity, detector in DETECTORS:
        # Detectors take (ops_24h,) or (ops_24h, wdlog) depending on signature
        import inspect
        sig = inspect.signature(detector)
        n_params = len(sig.parameters)
        if n_params == 1:
            detected, evidence = detector(ops_24h)
        else:
            detected, evidence = detector(ops_24h, wdlog)

        if not detected:
            continue  # signal not present — skip, never fabricate

        if _is_on_cooldown(state, event_type):
            suppressed.append(event_type)
            continue  # within cooldown window — suppress

        record = {
            "ts":               now_ts,
            "event":            event_type,
            "severity":         severity,
            "source":           "sentinel",
            "evidence":         evidence,
            "cooldown_applied": False,
        }
        if _append_event(record):
            state[event_type] = now_ts
            emitted.append(event_type)

    _save_state(state)

    # Always emit SENTINEL_GUARD_CYCLE — heartbeat of the guard.
    # Fires every successful run (INFO, no cooldown), even when no interventions
    # are triggered. Carries suppressed_count so Agent911 can surface
    # cooldown_suppressions_24h without additional bookkeeping.
    # TASK_ID: A-SEN-P3-001
    guard_record = {
        "ts":               now_ts,
        "event":            "SENTINEL_GUARD_CYCLE",
        "severity":         "INFO",
        "source":           "sentinel",
        "suppressed_count": len(suppressed),
    }
    _append_event(guard_record)

    summary = {
        "ts":         now_ts,
        "emitted":    len(emitted),
        "suppressed": len(suppressed),
        "events":     emitted,
        "guard_cycle_emitted": True,
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Watchdog-safe: always exit 0
        print(json.dumps({"ts": _ts(), "error": str(exc), "emitted": 0}))
    sys.exit(0)
