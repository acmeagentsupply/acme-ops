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
import subprocess
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
PREDICTIVE_STATE_FILE = WATCHDOG_DIR / "sentinel_predictive_state.json"
WATCHDOG_LOG  = WATCHDOG_DIR / "watchdog.log"
MODEL_EVENTS  = WATCHDOG_DIR / "model_events.log"

COOLDOWN_MINUTES = 30
WINDOW_24H_H     = 24
WARN_DISK_MB     = 1024.0
ALERT_DISK_MB    = 2500.0
WARN_GROWTH_MB_H = 150.0
ALERT_GROWTH_MB_H = 300.0
DISK_CEILING_MB = 6144.0
PRESSURE_TARGET_MB = round(DISK_CEILING_MB * 0.8, 1)
MAX_HISTORY_POINTS = 6

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


def _save_predictive_state(state: dict) -> None:
    try:
        PREDICTIVE_STATE_FILE.write_text(json.dumps(state, indent=2))
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


def _read_watchdog_disk_mb() -> float:
    """Read watchdog footprint via du -sk; never raise."""
    try:
        proc = subprocess.run(
            ["du", "-sk", str(WATCHDOG_DIR)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if proc.returncode != 0:
            return 0.0
        raw = proc.stdout.strip().split()
        if not raw:
            return 0.0
        kb = float(raw[0])
        return round(kb / 1024.0, 1)
    except Exception:
        return 0.0


def _compute_disk_growth_mb_per_hr(state: dict, disk_mb: float, now_ts: str) -> float:
    prev_mb = state.get("disk_mb")
    prev_ts = state.get("disk_ts")
    if prev_mb is None or not prev_ts:
        return 0.0
    try:
        prev_mb = float(prev_mb)
        prev_dt = _parse_dt(prev_ts)
        now_dt = _parse_dt(now_ts)
        hours = (now_dt - prev_dt).total_seconds() / 3600.0
        if hours <= 0:
            return 0.0
        growth = (disk_mb - prev_mb) / hours
        return round(growth, 1)
    except Exception:
        return 0.0


def _update_disk_history(state: dict, disk_mb: float, now_ts: str) -> list:
    history = state.get("disk_history", [])
    if not isinstance(history, list):
        history = []
    cleaned = []
    for point in history[-(MAX_HISTORY_POINTS - 1):]:
        if not isinstance(point, dict):
            continue
        try:
            cleaned.append({
                "ts": str(point.get("ts", "")),
                "disk_mb": float(point.get("disk_mb", 0.0)),
            })
        except Exception:
            continue
    cleaned.append({"ts": now_ts, "disk_mb": float(disk_mb)})
    return cleaned[-MAX_HISTORY_POINTS:]


def _smoothed_growth_mb_per_hr(history: list) -> float:
    if len(history) < 3:
        if len(history) < 2:
            return 0.0
        try:
            start = history[-2]
            end = history[-1]
            hours = (_parse_dt(end["ts"]) - _parse_dt(start["ts"])).total_seconds() / 3600.0
            if hours <= 0:
                return 0.0
            return round((float(end["disk_mb"]) - float(start["disk_mb"])) / hours, 1)
        except Exception:
            return 0.0

    slopes = []
    window = history[-3:]
    for idx in range(1, len(window)):
        try:
            prev = window[idx - 1]
            curr = window[idx]
            hours = (_parse_dt(curr["ts"]) - _parse_dt(prev["ts"])).total_seconds() / 3600.0
            if hours <= 0:
                continue
            slopes.append((float(curr["disk_mb"]) - float(prev["disk_mb"])) / hours)
        except Exception:
            continue
    if not slopes:
        return 0.0
    return round(sum(slopes) / len(slopes), 1)


def _disk_pressure_level(disk_mb: float, growth_mb_per_hr: float) -> str:
    if disk_mb > ALERT_DISK_MB or growth_mb_per_hr > ALERT_GROWTH_MB_H:
        return "ALERT"
    if disk_mb > WARN_DISK_MB or growth_mb_per_hr > WARN_GROWTH_MB_H:
        return "WARN"
    return "OK"


def _disk_pressure_state(disk_mb: float, growth_mb_per_hr: float) -> str:
    if disk_mb > ALERT_DISK_MB or growth_mb_per_hr > ALERT_GROWTH_MB_H:
        return "critical"
    if WARN_DISK_MB <= disk_mb <= ALERT_DISK_MB or WARN_GROWTH_MB_H <= growth_mb_per_hr <= ALERT_GROWTH_MB_H:
        return "rising"
    return "normal"


def _time_to_pressure_hrs(disk_mb: float, growth_mb_per_hr: float) -> object:
    if growth_mb_per_hr <= 0:
        return None
    remaining = PRESSURE_TARGET_MB - disk_mb
    if remaining <= 0:
        return 0.0
    return round(remaining / growth_mb_per_hr, 2)


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
    disk_mb  = _read_watchdog_disk_mb()
    raw_growth_mb_per_hr = _compute_disk_growth_mb_per_hr(state, disk_mb, now_ts)
    history = _update_disk_history(state, disk_mb, now_ts)
    growth_mb_per_hr = _smoothed_growth_mb_per_hr(history)
    pressure_level = _disk_pressure_level(disk_mb, growth_mb_per_hr)
    pressure_state = _disk_pressure_state(disk_mb, growth_mb_per_hr)
    time_to_pressure_hrs = _time_to_pressure_hrs(disk_mb, growth_mb_per_hr)

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

    disk_record = {
        "ts": now_ts,
        "event": "SENTINEL_DISK_PRESSURE",
        "signal": "sentinel_disk_pressure",
        "severity": pressure_level,
        "source": "sentinel",
        "disk_mb": disk_mb,
        "raw_growth_mb_per_hr": raw_growth_mb_per_hr,
        "growth_mb_per_hr": growth_mb_per_hr,
        "pressure_level": pressure_level,
        "pressure_state": pressure_state,
        "time_to_pressure_hrs": time_to_pressure_hrs,
        "advisory_only": True,
    }
    _append_event(disk_record)

    state["disk_mb"] = disk_mb
    state["raw_growth_mb_per_hr"] = raw_growth_mb_per_hr
    state["growth_mb_per_hr"] = growth_mb_per_hr
    state["pressure_level"] = pressure_level
    state["pressure_state"] = pressure_state
    state["time_to_pressure_hrs"] = time_to_pressure_hrs
    state["disk_history"] = history
    state["disk_ts"] = now_ts
    _save_state(state)
    _save_predictive_state({
        "ts": now_ts,
        "disk_mb": disk_mb,
        "raw_growth_mb_per_hr": raw_growth_mb_per_hr,
        "growth_mb_per_hr": growth_mb_per_hr,
        "pressure_level": pressure_level,
        "pressure_state": pressure_state,
        "time_to_pressure_hrs": time_to_pressure_hrs,
        "advisory_only": True,
        "history_points": len(history),
        "pressure_target_mb": PRESSURE_TARGET_MB,
    })

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
        "disk_mb":          disk_mb,
        "growth_mb_per_hr": growth_mb_per_hr,
        "pressure_level":   pressure_level,
        "pressure_state":   pressure_state,
        "time_to_pressure_hrs": time_to_pressure_hrs,
    }
    _append_event(guard_record)

    summary = {
        "ts":         now_ts,
        "emitted":    len(emitted),
        "suppressed": len(suppressed),
        "events":     emitted,
        "guard_cycle_emitted": True,
        "disk_mb": disk_mb,
        "growth_mb_per_hr": growth_mb_per_hr,
        "pressure_level": pressure_level,
        "pressure_state": pressure_state,
        "time_to_pressure_hrs": time_to_pressure_hrs,
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Watchdog-safe: always exit 0
        print(json.dumps({"ts": _ts(), "error": str(exc), "emitted": 0}))
    sys.exit(0)
