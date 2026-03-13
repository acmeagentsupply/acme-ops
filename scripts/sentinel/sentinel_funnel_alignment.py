#!/usr/bin/env python3
"""
Sentinel Attach ↔ Funnel Alignment Validator
TASK_ID: A-SEN-P4-002
OWNER:   GP-OPS

Validates that sentinel_recommendation (agent911_state.json) and
sentinel_enabled_present (gtm_funnel_weekly.json) are not contradicting
each other in steady state.

Alignment states:
  CONSISTENT        — recommendation and enablement are congruent
  EXPECTED_PRESSURE — recommended=True but not yet enabled (normal adoption gap)
  LEGACY_ENABLE     — enabled but not recommended (possible over-provisioning)
  DRIFT             — data unreadable or internally inconsistent

SAFETY:
  - Read-only: zero writes to openclaw.json
  - Zero gateway restarts
  - Zero subprocesses
  - Append-only to ops_events.log
  - Exits 0 always
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME     = os.path.expanduser("~")
WATCHDOG = os.path.join(HOME, ".openclaw", "watchdog")

SRC_A9_STATE    = os.path.join(WATCHDOG, "agent911_state.json")
SRC_FUNNEL_JSON = os.path.join(WATCHDOG, "gtm_funnel_weekly.json")
SRC_OPS         = os.path.join(WATCHDOG, "ops_events.log")

EVT_ALIGNMENT = "SENTINEL_FUNNEL_ALIGNMENT"

# ---------------------------------------------------------------------------
# Alignment state constants
# ---------------------------------------------------------------------------
CONSISTENT        = "CONSISTENT"
EXPECTED_PRESSURE = "EXPECTED_PRESSURE"
LEGACY_ENABLE     = "LEGACY_ENABLE"
DRIFT             = "DRIFT"

# Confidence per state (deterministic)
_STATE_CONFIDENCE = {
    CONSISTENT:        90,
    EXPECTED_PRESSURE: 75,
    LEGACY_ENABLE:     60,
    DRIFT:             0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_json_load(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _append_event(path: str, record: dict) -> bool:
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core alignment evaluator
# ---------------------------------------------------------------------------

def compute_alignment(a9_state: dict, funnel_weekly: dict) -> dict:
    """
    Deterministic alignment check.
    Derives (recommended, enabled_present) from input dicts and maps
    to one of four canonical states.

    State matrix:
      recommended=T, enabled=T  → CONSISTENT
      recommended=T, enabled=F  → EXPECTED_PRESSURE
      recommended=F, enabled=T  → LEGACY_ENABLE
      recommended=F, enabled=F  → CONSISTENT   (no conflict)

    DRIFT is returned when input data is unreadable or missing required fields.

    Input source fields:
      a9_state:     sentinel_recommendation.recommended (bool)
      funnel_weekly: sentinel_enabled_present (bool)

    Returns dict with: alignment_state, recommended, enabled_present,
    confidence, ts, notes.
    """
    ts = _ts_iso()

    # Extract recommended from agent911_state
    sen_rec = a9_state.get("sentinel_recommendation", {})
    rec_raw = sen_rec.get("recommended")

    # Extract enabled_present from gtm_funnel_weekly
    en_raw = funnel_weekly.get("sentinel_enabled_present")

    # DRIFT: can't determine either field
    if rec_raw is None and en_raw is None:
        return {
            "alignment_state":   DRIFT,
            "recommended":       None,
            "enabled_present":   None,
            "confidence":        _STATE_CONFIDENCE[DRIFT],
            "ts":                ts,
            "notes":             "Both sources missing or unreadable.",
        }

    # Treat None as False (conservative) if one side is missing
    recommended     = bool(rec_raw)  if rec_raw  is not None else False
    enabled_present = bool(en_raw)   if en_raw   is not None else False

    # Single-source DRIFT: partial data, reduced confidence
    data_partial = (rec_raw is None or en_raw is None)

    # ── State matrix (deterministic) ──────────────────────────────────────
    if recommended and enabled_present:
        state = CONSISTENT
        notes = "Sentinel recommended and observed as enabled — congruent."
    elif recommended and not enabled_present:
        state = EXPECTED_PRESSURE
        notes = (
            "Sentinel is recommended but not observed as enabled. "
            "This is a normal adoption gap — no action required unless persistent."
        )
    elif not recommended and enabled_present:
        state = LEGACY_ENABLE
        notes = (
            "Sentinel is enabled but not currently recommended. "
            "Possible legacy configuration or stable system — review optional."
        )
    else:
        # recommended=False, enabled_present=False
        state = CONSISTENT
        notes = "Neither Sentinel recommendation nor enablement observed — no conflict."

    # Downgrade confidence if data was partial
    confidence = _STATE_CONFIDENCE[state]
    if data_partial:
        confidence = max(0, confidence - 20)

    return {
        "alignment_state":   state,
        "recommended":       recommended,
        "enabled_present":   enabled_present,
        "confidence":        confidence,
        "ts":                ts,
        "notes":             notes,
    }


# ---------------------------------------------------------------------------
# NDJSON emitter
# ---------------------------------------------------------------------------

def emit_alignment_event(alignment: dict, ops_path: str = SRC_OPS) -> bool:
    """
    Append SENTINEL_FUNNEL_ALIGNMENT to ops_events.log.
    Emitted once per snapshot run. Append-only.
    """
    record = {
        "ts":              alignment.get("ts", _ts_iso()),
        "event":           EVT_ALIGNMENT,
        "severity":        "INFO",
        "source":          "sentinel_funnel_alignment",
        "alignment_state": alignment.get("alignment_state", DRIFT),
        "recommended":     alignment.get("recommended"),
        "enabled_present": alignment.get("enabled_present"),
        "confidence":      alignment.get("confidence", 0),
    }
    return _append_event(ops_path, record)


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

def render_alignment_block(alignment: dict) -> list[str]:
    """Return dashboard lines for the SENTINEL ↔ FUNNEL ALIGNMENT block."""
    state = alignment.get("alignment_state", DRIFT)
    rec   = alignment.get("recommended")
    en    = alignment.get("enabled_present")
    conf  = alignment.get("confidence", 0)
    notes = alignment.get("notes", "")

    rec_label = "YES" if rec else ("NO" if rec is not None else "unknown")
    en_label  = "YES" if en  else ("NO" if en  is not None else "unknown")

    lines = [
        f"  State:             {state}",
        f"  Recommended:       {rec_label}",
        f"  Enabled (present): {en_label}",
        f"  Confidence:        {conf}/100",
    ]
    if state in (EXPECTED_PRESSURE, LEGACY_ENABLE, DRIFT):
        lines.append(f"  Note: {notes[:80]}")
    return lines


# ---------------------------------------------------------------------------
# High-level gather (called from agent911_snapshot.py)
# ---------------------------------------------------------------------------

def gather_alignment(
    a9_state_path: str = SRC_A9_STATE,
    funnel_path: str   = SRC_FUNNEL_JSON,
    ops_path: str      = SRC_OPS,
) -> dict:
    """
    Load sources, compute alignment, emit event.
    Returns alignment dict. Exits 0 always.
    """
    try:
        a9_state     = _safe_json_load(a9_state_path)
        funnel_weekly = _safe_json_load(funnel_path)
        alignment    = compute_alignment(a9_state, funnel_weekly)
        emit_alignment_event(alignment, ops_path)
        return alignment
    except Exception:
        return {
            "alignment_state":   DRIFT,
            "recommended":       None,
            "enabled_present":   None,
            "confidence":        0,
            "ts":                _ts_iso(),
            "notes":             "Exception during alignment computation.",
        }


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import time

    t0 = time.monotonic()
    al = gather_alignment()
    elapsed = round((time.monotonic() - t0) * 1000, 2)

    print(f"ALIGNMENT_OK elapsed={elapsed}ms state={al['alignment_state']} "
          f"conf={al['confidence']}")
    print(json.dumps(al, indent=2))
    print("\n--- Dashboard block ---")
    for line in render_alignment_block(al):
        print(line)

    sys.exit(0)
