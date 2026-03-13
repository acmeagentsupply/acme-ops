#!/usr/bin/env python3
"""
agent911_proof_snippet.py
A-A9-V0-006: Protection Proof Snippets

Reads agent911_state.json (primary) or ops_events.log (fallback) and produces
two lightweight, web-embeddable proof artifacts:

  ~/.openclaw/watchdog/agent911_proof.json   (structured, overwrite allowed)
  ~/.openclaw/watchdog/agent911_proof.md     (flat human-readable paragraph)

Safety guarantees:
  - Zero reads/writes to openclaw.json
  - Zero gateway restarts
  - All input reads are graceful on missing files
  - Writes ONLY to agent911_proof.json and agent911_proof.md
  - Always exits 0

TASK_ID: A-A9-V0-006
OWNER: GP-OPS
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME         = os.path.expanduser("~")
WATCHDOG_DIR = os.path.join(HOME, ".openclaw", "watchdog")

SRC_STATE    = os.path.join(WATCHDOG_DIR, "agent911_state.json")
SRC_OPS      = os.path.join(WATCHDOG_DIR, "ops_events.log")
OUT_JSON     = os.path.join(WATCHDOG_DIR, "agent911_proof.json")
OUT_MD       = os.path.join(WATCHDOG_DIR, "agent911_proof.md")

_PROT_PREFIX = "SENTINEL_PROTECTION_"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp — handles 'Z' and '+00:00' (Python 3.9 compat)."""
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _short_event(evt: str) -> str:
    """Strip SENTINEL_PROTECTION_ prefix for human-readable output."""
    return evt.replace(_PROT_PREFIX, "") if evt else evt


# ---------------------------------------------------------------------------
# Primary source — agent911_state.json protection_rollup
# ---------------------------------------------------------------------------

def _from_state_json():
    """
    Read protection_rollup from agent911_state.json.
    Returns None if file is missing or lacks rollup data.
    """
    try:
        if not os.path.exists(SRC_STATE):
            return None
        with open(SRC_STATE) as f:
            snap = json.load(f)
        rollup = snap.get("protection_rollup")
        if not rollup or not isinstance(rollup, dict):
            return None
        return rollup
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fallback source — ops_events.log direct scan
# ---------------------------------------------------------------------------

def _from_ops_log() -> dict:
    """
    Single-pass scan of ops_events.log for SENTINEL_PROTECTION_* events.
    Returns a rollup-shaped dict — same structure as protection_rollup.
    Gracefully returns zero counts on any error.
    """
    EMPTY = {
        "events_24h":        0,
        "events_7d":         0,
        "by_severity":       {"INFO": 0, "MEDIUM": 0, "HIGH": 0},
        "last_event_type":   "none",
        "last_event_ts":     "unknown",
        "last_three_events": [],
    }

    now_utc = datetime.now(timezone.utc)
    cut_24h = now_utc - timedelta(hours=24)
    cut_7d  = now_utc - timedelta(days=7)

    try:
        if not os.path.exists(SRC_OPS):
            return EMPTY

        prot_events = []
        with open(SRC_OPS) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or _PROT_PREFIX not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                    if not str(rec.get("event", "")).startswith(_PROT_PREFIX):
                        continue
                    dt = _parse_ts(rec.get("ts", ""))
                    if dt >= cut_7d:
                        prot_events.append((dt, rec))
                except Exception:
                    continue

        if not prot_events:
            return EMPTY

        count_24h = 0
        sev_hist  = {"INFO": 0, "MEDIUM": 0, "HIGH": 0}
        for dt, rec in prot_events:
            sev = rec.get("severity", "INFO")
            sev_hist[sev] = sev_hist.get(sev, 0) + 1
            if dt >= cut_24h:
                count_24h += 1

        sorted_evts = sorted(prot_events, key=lambda x: x[0], reverse=True)
        last_three  = [
            {
                "event":    e.get("event", "unknown"),
                "ts":       e.get("ts", "unknown"),
                "severity": e.get("severity", "INFO"),
            }
            for _, e in sorted_evts[:3]
        ]
        last_rec = sorted_evts[0][1]

        return {
            "events_24h":        count_24h,
            "events_7d":         len(prot_events),
            "by_severity":       sev_hist,
            "last_event_type":   last_rec.get("event", "unknown"),
            "last_event_ts":     last_rec.get("ts", "unknown"),
            "last_three_events": last_three,
        }

    except Exception:
        return EMPTY


# ---------------------------------------------------------------------------
# Status resolver
# ---------------------------------------------------------------------------

def _resolve_status(rollup: dict, source_available: bool) -> str:
    """
    ACTIVE_GUARDING  — if events_24h > 0 OR events_7d > 0
    MONITORING       — if zero events but telemetry healthy
    UNKNOWN          — if required inputs missing
    """
    if not source_available:
        return "UNKNOWN"
    if rollup.get("events_24h", 0) > 0 or rollup.get("events_7d", 0) > 0:
        return "ACTIVE_GUARDING"
    return "MONITORING"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_json(proof: dict) -> None:
    """Write agent911_proof.json (overwrite allowed)."""
    with open(OUT_JSON, "w") as f:
        json.dump(proof, f, indent=2, sort_keys=True)
        f.write("\n")


def _write_md(proof: dict) -> None:
    """Write agent911_proof.md — flat single-paragraph web embed."""
    prot   = proof.get("protection", {})
    c7d    = prot.get("events_7d", 0)
    status = proof.get("status", "UNKNOWN")

    if status == "UNKNOWN":
        text = (
            "Sentinel monitoring status is currently unavailable. "
            "System telemetry is being collected."
        )
    elif c7d > 0:
        last_type = _short_event(prot.get("last_event_type", "unknown"))
        last_ts   = prot.get("last_event_ts", "unknown")
        text = (
            f"Sentinel protected this system {c7d} time{'s' if c7d != 1 else ''} "
            f"in the last 7 days. "
            f"Last intervention: {last_type} at {last_ts}."
        )
    else:
        text = (
            "Sentinel is actively monitoring this system. "
            "No interventions were required in the last 7 days."
        )

    with open(OUT_MD, "w") as f:
        f.write(text + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    now_ts = _now_ts()

    # Try primary source first
    rollup = _from_state_json()
    source_available = True
    source_label = "agent911_rollup"

    if rollup is None:
        # Fall back to direct ops_events.log scan
        rollup = _from_ops_log()
        source_label = "ops_events_fallback"
        # Mark unavailable only if both sources yielded nothing AND files missing
        if not os.path.exists(SRC_STATE) and not os.path.exists(SRC_OPS):
            source_available = False

    status = _resolve_status(rollup, source_available)

    proof = {
        "ts": now_ts,
        "protection": {
            "events_24h":         rollup.get("events_24h",       0),
            "events_7d":          rollup.get("events_7d",        0),
            "last_event_type":    rollup.get("last_event_type",  "none"),
            "last_event_ts":      rollup.get("last_event_ts",    "unknown"),
            "severity_histogram": rollup.get("by_severity",      {"INFO": 0, "MEDIUM": 0, "HIGH": 0}),
        },
        "status": status,
        "source": source_label,
    }

    _write_json(proof)
    _write_md(proof)

    print(json.dumps({
        "ts":        now_ts,
        "status":    status,
        "events_24h": proof["protection"]["events_24h"],
        "events_7d":  proof["protection"]["events_7d"],
        "source":    source_label,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Safety: always exit 0
        print(json.dumps({"ts": _now_ts(), "error": str(exc), "status": "UNKNOWN"}))
    sys.exit(0)
