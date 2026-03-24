#!/usr/bin/env python3
"""
sentinel_predictive_guard.py
A-SEN-P4-001: Sentinel Predictive Guard v1

Computes a deterministic STALL_RISK_SCORE (0-100) and STALL_RISK_LEVEL
(LOW/MED/HIGH) from 5 weighted signals. Emits SENTINEL_PREDICTIVE_RISK
to ops_events.log on upward level transitions or HIGH persistence (6h cooldown).

Signals (5 pillars):
  1. COMP_SIGNAL             weight=0.30
  2. WATCHDOG_LATENCY_SIGNAL weight=0.20
  3. GATEWAY_STALL_SIGNAL    weight=0.25
  4. MODEL_INSTABILITY_SIGNAL weight=0.10
  5. RESOURCE_PRESSURE_SIGNAL weight=0.15

Safety:
  - Always exits 0 (watchdog-safe)
  - Zero writes to openclaw.json
  - No gateway restarts
  - Append-only to ops_events.log
  - State file overwrite: sentinel_predictive_state.json only
  - No subprocesses — Python stdlib only
  - Bounded log scan: max TAIL_LINES lines

Usage:
  python3 sentinel_predictive_guard.py [--test-risk LOW|MED|HIGH]

TASK_ID: A-SEN-P4-001
OWNER: GP-OPS
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME           = Path.home()
WATCHDOG       = HOME / ".openclaw" / "watchdog"

SRC_STATUS     = WATCHDOG / "status.log"
SRC_OPS        = WATCHDOG / "ops_events.log"
SRC_COMP_ALERT = WATCHDOG / "compaction_alert_state.json"
SRC_MODEL      = WATCHDOG / "model_state.json"
SRC_RC_HISTORY = WATCHDOG / "radcheck_history.ndjson"
OUT_STATE      = WATCHDOG / "sentinel_predictive_state.json"

# ── Tuning constants ──────────────────────────────────────────────────────────
WEIGHTS: Dict[str, float] = {
    "comp":             0.30,
    "watchdog_latency": 0.20,
    "gateway":          0.25,
    "model":            0.10,
    "resource":         0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"

COOLDOWN_H  = 6       # hours between repeated breach events
TAIL_LINES  = 500     # max ops_events.log lines to scan (performance budget)
OPS_WINDOW_H = 2      # hours for gateway stall scan


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601; handles 'Z' suffix (Python 3.9 compat)."""
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _tail(path: Path, n: int = TAIL_LINES) -> List[str]:
    """Return up to last n lines from path; empty list if missing/error."""
    try:
        text = path.read_text(errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


# ── Signal 1: COMP_SIGNAL (weight 0.30) ──────────────────────────────────────
_COMP_STATE_MAP: Dict[str, float] = {
    "NOMINAL": 0.0, "SUSPECT": 0.3, "ACTIVE": 0.7, "STORM": 1.0,
}
_COMP_PRESSURE_MAP: Dict[str, float] = {
    "LOW": 0.0, "MEDIUM": 0.4, "HIGH": 0.8,
}


def signal_comp() -> float:
    """
    Compaction risk signal (0.0–1.0).
    Primary: compaction_alert_state.json alert_level.
    Supplement: radcheck_history.ndjson compaction_risk domain (risk_level + acceleration).
    Returns max of both, capped at 1.0.
    """
    # Primary: sentinel alert level
    comp_state = _safe_json(SRC_COMP_ALERT)
    state_level = comp_state.get("alert_level", "NOMINAL")
    base = _COMP_STATE_MAP.get(state_level, 0.0)

    # Supplement: RadCheck compaction domain from recent history
    rc_base     = 0.0
    accel_bump  = 0.0
    for line in reversed(_tail(SRC_RC_HISTORY, n=5)):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            comp_domain = rec.get("domains", {}).get("compaction_risk", {})
            if not comp_domain:
                continue
            risk_level  = comp_domain.get("risk_level", "LOW")
            rc_base     = _COMP_PRESSURE_MAP.get(risk_level, 0.0)
            acceleration = comp_domain.get("acceleration", False)
            if acceleration is True:
                accel_bump = 0.2
            break
        except Exception:
            continue

    rc_signal = _clamp(rc_base + accel_bump)
    return _clamp(max(base, rc_signal))


# ── Signal 2: WATCHDOG_LATENCY_SIGNAL (weight 0.20) ──────────────────────────
def signal_watchdog_latency() -> float:
    """
    Watchdog loop latency + silence age from status.log (0.0–1.0).
    Parses: loop_ms=N, silence_age_s=N fields from last status.log entry.
    """
    lines         = _tail(SRC_STATUS, n=50)
    loop_ms       = None
    silence_age_s = None

    for line in reversed(lines):
        if loop_ms is None:
            m = re.search(r"loop_ms=(\d+)", line)
            if m:
                loop_ms = int(m.group(1))
        if silence_age_s is None:
            m = re.search(r"silence_age_s=(\d+)", line)
            if m:
                silence_age_s = int(m.group(1))
        if loop_ms is not None and silence_age_s is not None:
            break

    # Normalize loop_ms: <=1000 → 0.0; 1000-10000 → linear 0.2-0.7; >10000 → 1.0
    if loop_ms is None:
        base = 0.0
    elif loop_ms <= 1000:
        base = 0.0
    elif loop_ms <= 10_000:
        base = 0.2 + 0.5 * (loop_ms - 1000) / 9000.0
    else:
        base = 1.0

    # Silence bump: >300s active silence adds +0.2
    silence_bump = 0.2 if (silence_age_s is not None and silence_age_s > 300) else 0.0

    return _clamp(base + silence_bump)


# ── Signal 3: GATEWAY_STALL_SIGNAL (weight 0.25) ─────────────────────────────
def signal_gateway_stall() -> float:
    """
    Gateway stalls from ops_events.log within last OPS_WINDOW_H hours (0.0–1.0).
    Scans at most TAIL_LINES lines. Counts GATEWAY_STALL events with valid ts.
    """
    cutoff = _now_utc() - timedelta(hours=OPS_WINDOW_H)
    count  = 0
    for line in _tail(SRC_OPS, n=TAIL_LINES):
        line = line.strip()
        if not line or "GATEWAY_STALL" not in line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("event") != "GATEWAY_STALL":
                continue
            dt = _parse_ts(rec.get("ts", ""))
            if dt >= cutoff:
                count += 1
        except Exception:
            continue

    if count == 0:
        return 0.0
    elif count == 1:
        return 0.4
    elif count <= 3:
        return 0.7
    else:
        return 1.0


# ── Signal 4: MODEL_INSTABILITY_SIGNAL (weight 0.10) ─────────────────────────
def signal_model_instability() -> float:
    """
    Model state health from model_state.json (0.0–1.0).
    status != ok → 0.7 base; age_hours > 2 → +0.2; failover_count > 0 → +0.2.
    """
    state = _safe_json(SRC_MODEL)
    if not state:
        return 0.0  # missing → neutral

    base = 0.0
    if state.get("status", "ok") != "ok":
        base = 0.7

    updated_at = state.get("updated_at")
    if updated_at:
        try:
            age_h = (datetime.now().timestamp() - float(updated_at)) / 3600
            if age_h > 2:
                base += 0.2
        except Exception:
            pass

    failover_count = state.get("failover_count", 0) or 0
    if failover_count > 0:
        base += 0.2

    return _clamp(base)


# ── Signal 5: RESOURCE_PRESSURE_SIGNAL (weight 0.15) ─────────────────────────
def signal_resource_pressure() -> float:
    """
    System load via os.getloadavg() / os.cpu_count() (0.0–1.0).
    <=0.7 → 0.0; 0.7-1.5 → linear 0.3-0.7; >1.5 → 1.0.
    """
    try:
        load_5 = os.getloadavg()[1]      # 5-min load average
        cores  = os.cpu_count() or 1
        norm   = load_5 / cores
        if norm <= 0.7:
            return 0.0
        elif norm <= 1.5:
            return 0.3 + 0.4 * (norm - 0.7) / 0.8
        else:
            return 1.0
    except (AttributeError, OSError):
        return 0.0  # Windows or unavailable → neutral


# ── Score + Level ─────────────────────────────────────────────────────────────
def compute_stall_risk(override_level: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute STALL_RISK_SCORE (0-100) and STALL_RISK_LEVEL (LOW/MED/HIGH).

    Args:
        override_level: if set (test mode), forces the given level and synthetic score.

    Returns dict with: risk_score, risk_level, signals, test_mode.
    """
    if override_level:
        level_score_map = {"LOW": 20, "MED": 55, "HIGH": 85}
        norm = override_level.upper()
        if norm not in level_score_map:
            norm = "HIGH"
        score   = level_score_map[norm]
        level   = norm
        signals = {k: round(WEIGHTS[k], 3) for k in WEIGHTS}  # synthetic but weighted
        return {
            "risk_score":  score,
            "risk_level":  level,
            "signals":     signals,
            "test_mode":   True,
        }

    signals = {
        "comp":             signal_comp(),
        "watchdog_latency": signal_watchdog_latency(),
        "gateway":          signal_gateway_stall(),
        "model":            signal_model_instability(),
        "resource":         signal_resource_pressure(),
    }
    weighted_sum = sum(signals[k] * WEIGHTS[k] for k in WEIGHTS)
    score        = round(100 * weighted_sum)

    if score >= 70:
        level = "HIGH"
    elif score >= 40:
        level = "MED"
    else:
        level = "LOW"

    return {
        "risk_score":  score,
        "risk_level":  level,
        "signals":     signals,
        "test_mode":   False,
    }


# ── Driver identification ─────────────────────────────────────────────────────
_DRIVER_LABELS: Dict[str, str] = {
    "comp":             "COMP_SIGNAL",
    "watchdog_latency": "WATCHDOG_LATENCY_SIGNAL",
    "gateway":          "GATEWAY_STALL_SIGNAL",
    "model":            "MODEL_INSTABILITY_SIGNAL",
    "resource":         "RESOURCE_PRESSURE_SIGNAL",
}

_DRIVER_ACTIONS: Dict[str, str] = {
    "comp":             "Reduce session context; investigate acceleration",
    "gateway":          "Kickstart window watch; reduce load; isolate agent sessions",
    "resource":         "Reduce concurrent agents; inspect top CPU offenders",
    "model":            "Review routing chain; enable stronger fallback lane",
    "watchdog_latency": "Inspect watchdog loop timing; check for hung processes",
}


def top_drivers(signals: Dict[str, float], n: int = 2) -> List[str]:
    """Return top N signal keys by weighted contribution, descending. Excludes zeros."""
    contribs = {k: signals[k] * WEIGHTS[k] for k in WEIGHTS}
    sorted_keys = sorted(contribs, key=lambda x: -contribs[x])
    return [k for k in sorted_keys[:n] if contribs[k] > 0.0]


def recommended_actions_for(drivers: List[str]) -> List[str]:
    """Map driver keys to recommended action strings."""
    return [_DRIVER_ACTIONS[d] for d in drivers if d in _DRIVER_ACTIONS]


# ── State management + cooldown ───────────────────────────────────────────────
def _load_state() -> dict:
    return _safe_json(OUT_STATE)


def _save_state(state: dict) -> None:
    try:
        OUT_STATE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _on_cooldown(state: dict, key: str) -> bool:
    """Return True if key was emitted within the cooldown window."""
    ts_str = state.get(key)
    if not ts_str:
        return False
    try:
        last = _parse_ts(ts_str)
        return (_now_utc() - last).total_seconds() < COOLDOWN_H * 3600
    except Exception:
        return False


def _should_emit(risk_level: str, state: dict, force: bool = False) -> Tuple[bool, str]:
    """
    Determine whether to emit a SENTINEL_PREDICTIVE_RISK event.
    emission cooldown only — scoring always runs

    Emit when:
      - Level transitions upward (LOW→MED, LOW→HIGH, MED→HIGH) — cooldown per transition
      - Level is HIGH and persists beyond 6h since last HIGH emit
      - force=True (test mode) — always emit

    Returns (should_emit: bool, reason: str).
    """
    if force:
        return True, "test_mode"

    prev_level = state.get("last_risk_level", "LOW")

    # Upward transition: LOW → MED or LOW → HIGH
    if prev_level == "LOW" and risk_level in ("MED", "HIGH"):
        if not _on_cooldown(state, "LOW_MED_last_emit"):
            return True, "UPWARD_TRANSITION"

    # Upward transition: MED → HIGH (or skipped LOW → HIGH)
    if prev_level in ("LOW", "MED") and risk_level == "HIGH":
        if not _on_cooldown(state, "MED_HIGH_last_emit"):
            return True, "UPWARD_TRANSITION"

    # HIGH persistence: already HIGH, re-emit if >6h since last HIGH emit
    if risk_level == "HIGH" and prev_level == "HIGH":
        if not _on_cooldown(state, "HIGH_persist_last_emit"):
            return True, "HIGH_PERSIST"

    return False, "cooldown"


# ── Event emission ────────────────────────────────────────────────────────────
def _append_event(record: dict) -> bool:
    try:
        with open(SRC_OPS, "a") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


def _compute_predictive_confidence(risk_level: str, driver_count: int) -> int:
    """
    Deterministic confidence score (0-100, integer, no randomness).
    Guidance:
      HIGH + >=2 drivers → 75-90
      MED               → 50-74
      LOW               → 25-49
      UNKNOWN           → <=24
    """
    if risk_level == "HIGH":
        return 90 if driver_count >= 2 else 75
    elif risk_level in ("MED", "MEDIUM"):
        return 74 if driver_count >= 2 else 50
    elif risk_level == "LOW":
        return 49 if driver_count >= 2 else 25
    else:   # UNKNOWN or unrecognized
        return 20


def emit_predictive_risk(result: dict, reason: str, ts: str) -> bool:
    """Build and emit SENTINEL_PREDICTIVE_RISK event to ops_events.log."""
    risk_level = result["risk_level"]
    risk_score = result["risk_score"]
    signals    = result["signals"]

    drivers       = top_drivers(signals)
    actions       = recommended_actions_for(drivers)
    driver_labels = [_DRIVER_LABELS.get(d, d.upper()) for d in drivers]
    confidence    = _compute_predictive_confidence(risk_level, len(driver_labels))

    severity = "HIGH" if risk_level == "HIGH" else "MEDIUM"

    record = {
        "ts":                     ts,
        "event":                  "SENTINEL_PREDICTIVE_RISK",
        "severity":               severity,
        "source":                 "sentinel",
        "risk_score":             risk_score,
        "risk_level":             risk_level,
        "predictive_confidence":  confidence,
        "signals":                {k: round(v, 3) for k, v in signals.items()},
        "top_drivers":            driver_labels,
        "reason_codes":           driver_labels,   # alias of top_drivers
        "recommended_actions":    actions,
        "emit_reason":            reason,
        "cooldown_applied":       False,
    }
    return _append_event(record)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    t0     = time.monotonic()
    now_ts = _ts()

    # Parse CLI args — support --test-risk LOW|MED|HIGH
    test_risk  = None
    force_emit = False
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--test-risk" and i + 1 < len(args):
            test_risk  = args[i + 1].upper()
            force_emit = True   # test mode always emits regardless of cooldown

    result     = compute_stall_risk(override_level=test_risk)
    risk_level = result["risk_level"]
    risk_score = result["risk_score"]
    signals    = result["signals"]

    state = _load_state()
    emit, reason = _should_emit(risk_level, state, force=force_emit)

    emitted = False
    if emit:
        emitted = emit_predictive_risk(result, reason, now_ts)
        if emitted:
            # Update per-transition cooldown keys
            prev_level = state.get("last_risk_level", "LOW")
            if force_emit or (prev_level == "LOW" and risk_level in ("MED", "HIGH")):
                state["LOW_MED_last_emit"] = now_ts
            if force_emit or (prev_level in ("LOW", "MED") and risk_level == "HIGH"):
                state["MED_HIGH_last_emit"] = now_ts
            if force_emit or risk_level == "HIGH":
                state["HIGH_persist_last_emit"] = now_ts

    # Always persist current level + run metadata
    state["last_risk_level"] = risk_level
    state["last_risk_score"] = risk_score
    state["last_run_ts"]     = now_ts
    state["signals"]         = {k: round(v, 3) for k, v in signals.items()}
    _save_state(state)

    elapsed_ms = round((time.monotonic() - t0) * 1000)

    summary = {
        "ts":          now_ts,
        "risk_score":  risk_score,
        "risk_level":  risk_level,
        "emitted":     emitted,
        "emit_reason": reason if emitted else "no_emit",
        "test_mode":   result.get("test_mode", False),
        "elapsed_ms":  elapsed_ms,
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Watchdog-safe: always exit 0
        print(json.dumps({"ts": _ts(), "error": str(exc), "emitted": False}))
    sys.exit(0)
