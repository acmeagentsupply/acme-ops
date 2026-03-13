#!/usr/bin/env python3
"""
FindMyAgent Classifier v1 — Presence State Machine
Task: A-FMA-V1-001

State machine:
  ACTIVE  — recent confirmed signal; forward progress observed
  IDLE    — healthy but quiet; no blocking indicators
  BLOCKED — explicit blocker detected in MTL or events
  STALLED — heartbeat present but no forward progress in window
  UNKNOWN — insufficient signal or edge-case conditions

presence_confidence (0-100):
  Deterministic scoring from available evidence;
  missing data subtracts; corroborating signals add.

Edge cases handled:
  • missing heartbeat           → UNKNOWN, confidence -30
  • missing MTL entry           → noted; classification proceeds
  • clock skew tolerance ±5s    → timestamps within 5s treated as equivalent
  • empty agent set             → returns empty result, no error
  • stale commit detection      → detected from repo_sync state; confidence -10

Safety: read-only; no writes; no subprocess; no openclaw.json access.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

# ── Constants ─────────────────────────────────────────────────────────────────
CLOCK_SKEW_TOLERANCE_S = 5       # ±5s treated as same instant
ACTIVE_WINDOW_H        = 2.0     # signal within 2h → ACTIVE candidate
IDLE_WINDOW_H          = 24.0    # signal within 24h → IDLE candidate
STALL_WINDOW_H         = 2.0     # heartbeat but no progress for 2h → STALLED
STALE_COMMIT_H         = 24.0    # no git push in 24h → stale commit flag

# Confidence scoring
CONF_BASE              = 50
CONF_FRESH_SIGNAL      = +20     # guard cycle / heartbeat < 2h
CONF_MTL_PRESENT       = +15     # MTL record exists for agent
CONF_RECENT_PUSH       = +10     # repo push within 24h
CONF_CORROBORATING_SIG = +5      # each additional corroborating event
CONF_MISSING_HEARTBEAT = -30     # no heartbeat in 24h
CONF_CLOCK_SKEW        = -10     # clock skew detected
CONF_STALE_COMMIT      = -10     # no git activity in 24h
CONF_NO_MTL            = -5      # no MTL entry found


# ── Date helpers ───────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(ts: str) -> Optional[datetime]:
    """Parse ISO-8601; handle Z suffix (Python 3.9 compat)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _age_hours(ts: str) -> Optional[float]:
    dt = _parse_dt(ts)
    if dt is None:
        return None
    return (_now_utc() - dt).total_seconds() / 3600.0


def _within_skew(dt1: Optional[datetime], dt2: Optional[datetime]) -> bool:
    """Return True if two timestamps are within CLOCK_SKEW_TOLERANCE_S of each other."""
    if dt1 is None or dt2 is None:
        return False
    return abs((dt1 - dt2).total_seconds()) <= CLOCK_SKEW_TOLERANCE_S


def _detect_clock_skew(ops_events: list) -> bool:
    """
    Detect clock skew: look for consecutive events with timestamps
    out of monotonic order by more than CLOCK_SKEW_TOLERANCE_S.
    """
    prev_dt = None
    skew_count = 0
    for evt in ops_events[-50:]:   # tail-scan only
        dt = _parse_dt(evt.get("ts", ""))
        if dt is None:
            continue
        if prev_dt is not None:
            delta = (dt - prev_dt).total_seconds()
            if delta < -CLOCK_SKEW_TOLERANCE_S:
                skew_count += 1
        prev_dt = dt
    return skew_count >= 2


# ── Signal extraction ──────────────────────────────────────────────────────────
def _extract_heartbeat_signal(ops_events: list) -> Optional[str]:
    """Most recent heartbeat/guard-cycle timestamp."""
    for evt in reversed(ops_events):
        if evt.get("event", "") in (
            "SENTINEL_GUARD_CYCLE", "HEARTBEAT", "WATCHDOG_PROBE",
            "GATEWAY_PROBE_OK", "MODEL_STATE_UPDATE",
        ):
            return evt.get("ts")
    return None


def _extract_progress_signal(ops_events: list) -> Optional[str]:
    """Most recent forward-progress indicator (excludes pure guard cycles)."""
    for evt in reversed(ops_events):
        etype = evt.get("event", "")
        if etype in (
            "MODEL_STATE_UPDATE", "GATEWAY_PROBE_OK",
            "SENTINEL_PROTECTION_STALL_PREVENTED",
            "SENTINEL_PREEMPTIVE_GUARD",
        ):
            return evt.get("ts")
    return None


def _find_blocked_event(ops_events: list) -> Optional[dict]:
    """
    Return most recent genuine blocker event in last 24h.
    Excludes protection SUCCESS events (STALL_PREVENTED, COMPACTION_STORM handled)
    which indicate the system responded, not that the agent is blocked.
    """
    # Events that signal protection success — NOT blockers
    _PROTECTION_SUCCESS = {
        "SENTINEL_PROTECTION_STALL_PREVENTED",
        "SENTINEL_PROTECTION_COMPACTION_STORM",
        "SENTINEL_PREEMPTIVE_GUARD",
        "SENTINEL_GUARD_CYCLE",
    }
    cutoff = _now_utc() - timedelta(hours=24)
    for evt in reversed(ops_events):
        dt = _parse_dt(evt.get("ts", ""))
        if dt is None or dt < cutoff:
            continue
        etype = evt.get("event", "")
        if etype in _PROTECTION_SUCCESS:
            continue   # protection success — skip
        # Genuine agent-level blockers only:
        # GATEWAY_STALL and similar transient system events are handled by Sentinel;
        # only flag explicit unresolved blocks (MTL BLOCKED or POLICY_FAIL).
        # v1: conservative — only explicit BLOCK_ events qualify.
        if "BLOCK" in etype or etype == "POLICY_FAIL":
            return evt
    return None


def _get_mtl_status(agent_name: str, mtl_snap: dict) -> Optional[dict]:
    """Return most relevant MTL task for agent (by owner matching)."""
    tasks = mtl_snap.get("tasks", {})
    if isinstance(tasks, dict):
        task_list = list(tasks.values())
    else:
        task_list = tasks
    # Match by owner (case-insensitive)
    matches = [
        t for t in task_list
        if agent_name.lower() in t.get("owner", "").lower()
        and t.get("status", "").upper() not in ("DONE", "CANCELLED")
    ]
    if not matches:
        return None
    # Prefer BLOCKED > ACTIVE > IN_PROGRESS
    priority_order = {"BLOCKED": 0, "ACTIVE": 1, "IN_PROGRESS": 2}
    matches.sort(key=lambda t: priority_order.get(t.get("status", "").upper(), 9))
    return matches[0]


# ── State machine ──────────────────────────────────────────────────────────────
def _classify_agent(
    name:       str,
    ops_events: list,
    mtl_snap:   dict,
    repo_sync:  dict,
    clock_skew: bool,
) -> dict:
    """
    Classify a single agent. Returns state + confidence + evidence.
    DETERMINISTIC: same inputs always produce same output.
    """
    hb_ts      = _extract_heartbeat_signal(ops_events)
    prog_ts    = _extract_progress_signal(ops_events)
    block_evt  = _find_blocked_event(ops_events)
    mtl_entry  = _get_mtl_status(name, mtl_snap)
    hb_age_h   = _age_hours(hb_ts) if hb_ts else None
    prog_age_h = _age_hours(prog_ts) if prog_ts else None

    # Stale commit detection
    last_push_ts  = repo_sync.get("last_push_ts") if repo_sync else None
    push_age_h    = _age_hours(last_push_ts) if last_push_ts else None
    stale_commit  = (push_age_h is not None and push_age_h > STALE_COMMIT_H)

    evidence = []

    # ── Confidence scoring (deterministic) ────────────────────────────────────
    conf = CONF_BASE

    if hb_age_h is None:
        # Missing heartbeat
        conf += CONF_MISSING_HEARTBEAT
        evidence.append("no heartbeat signal observed in ops_events.log")
    elif hb_age_h <= ACTIVE_WINDOW_H:
        conf += CONF_FRESH_SIGNAL
        evidence.append(f"heartbeat observed {int(hb_age_h * 60)}m ago")
    else:
        evidence.append(f"last heartbeat {hb_age_h:.1f}h ago")

    if mtl_entry:
        conf += CONF_MTL_PRESENT
        evidence.append(f"MTL entry found: {mtl_entry.get('task_id')} ({mtl_entry.get('status')})")
    else:
        conf += CONF_NO_MTL
        evidence.append("no open MTL entry found for this agent")

    if push_age_h is not None and push_age_h <= 24.0:
        conf += CONF_RECENT_PUSH
        evidence.append(f"repo push observed {push_age_h:.1f}h ago")
    elif stale_commit:
        conf += CONF_STALE_COMMIT
        evidence.append(f"no repo push in {push_age_h:.1f}h (stale commit)")

    if clock_skew:
        conf += CONF_CLOCK_SKEW
        evidence.append("clock skew detected in event stream (±>5s reversals)")

    # Corroborating signals (multiple independent sources agreeing)
    if prog_ts and hb_ts and prog_ts != hb_ts:
        conf += CONF_CORROBORATING_SIG
        evidence.append("progress signal corroborates heartbeat")

    conf = max(0, min(100, conf))   # clamp

    # ── State determination (deterministic priority order) ────────────────────
    # 1. BLOCKED — explicit block indicator wins
    if block_evt or (mtl_entry and mtl_entry.get("status", "").upper() == "BLOCKED"):
        blocked_reason = (
            mtl_entry.get("blocked_on", "unknown") if mtl_entry
            else block_evt.get("event", "unknown")
        )
        return _result(
            name=name, state="BLOCKED",
            last_signal=_signal_age_str(hb_age_h),
            confidence=conf,
            reason=f"blocker observed: {blocked_reason}",
            evidence=evidence,
        )

    # 2. UNKNOWN — insufficient signal
    if hb_age_h is None:
        return _result(
            name=name, state="UNKNOWN",
            last_signal="not observed",
            confidence=conf,
            reason="no heartbeat signal in ops_events.log",
            evidence=evidence,
        )

    # 3. STALLED — heartbeat exists but progress window stale
    if (hb_age_h is not None and hb_age_h <= IDLE_WINDOW_H and
            prog_age_h is not None and prog_age_h > STALL_WINDOW_H):
        return _result(
            name=name, state="STALLED",
            last_signal=_signal_age_str(hb_age_h),
            confidence=conf,
            reason=f"heartbeat present but no forward progress for {prog_age_h:.1f}h",
            evidence=evidence,
        )

    # 4. ACTIVE
    if hb_age_h is not None and hb_age_h <= ACTIVE_WINDOW_H:
        return _result(
            name=name, state="ACTIVE",
            last_signal=_signal_age_str(hb_age_h),
            confidence=conf,
            reason=f"signal observed {int(hb_age_h * 60)}m ago",
            evidence=evidence,
        )

    # 5. IDLE — signal present but quiet
    if hb_age_h is not None and hb_age_h <= IDLE_WINDOW_H:
        return _result(
            name=name, state="IDLE",
            last_signal=_signal_age_str(hb_age_h),
            confidence=conf,
            reason=f"last signal {hb_age_h:.1f}h ago; no blocking indicators",
            evidence=evidence,
        )

    # 6. UNKNOWN — signal too old
    return _result(
        name=name, state="UNKNOWN",
        last_signal=_signal_age_str(hb_age_h),
        confidence=conf,
        reason=f"signal too old ({hb_age_h:.1f}h); insufficient for classification",
        evidence=evidence,
    )


def _signal_age_str(age_h: Optional[float]) -> str:
    if age_h is None:
        return "not observed"
    if age_h < 1.0:
        return f"{int(age_h * 60)}m ago"
    return f"{age_h:.1f}h ago"


def _result(name, state, last_signal, confidence, reason, evidence) -> dict:
    return {
        "name":               name,
        "state":              state,
        "last_signal":        last_signal,
        "presence_confidence": confidence,
        "reason":             reason,
        "evidence":           evidence,
    }


# ── Public API ────────────────────────────────────────────────────────────────
def classify_agents(
    known_agents: List[str],
    ops_events:   list,
    mtl_snap:     dict,
    repo_sync:    dict,
) -> dict:
    """
    Classify all known agents.
    Returns dict suitable for agent_presence_summary + agents_requiring_attention.
    Handles empty agent set gracefully.
    Deterministic: same inputs → same output.
    """
    if not known_agents:
        return _empty_result()

    clock_skew = _detect_clock_skew(ops_events)
    agents = []
    for name in known_agents:
        classification = _classify_agent(
            name=name,
            ops_events=ops_events,
            mtl_snap=mtl_snap,
            repo_sync=repo_sync,
            clock_skew=clock_skew,
        )
        agents.append(classification)

    total   = len(agents)
    active  = sum(1 for a in agents if a["state"] == "ACTIVE")
    idle    = sum(1 for a in agents if a["state"] == "IDLE")
    blocked = sum(1 for a in agents if a["state"] == "BLOCKED")
    stalled = sum(1 for a in agents if a["state"] == "STALLED")
    unknown = sum(1 for a in agents if a["state"] == "UNKNOWN")

    requiring_attention = [
        a for a in agents if a["state"] in ("BLOCKED", "STALLED")
    ]

    return {
        "agents":                    agents,
        "total":                     total,
        "active":                    active,
        "idle":                      idle,
        "blocked":                   blocked,
        "stalled":                   stalled,
        "unknown":                   unknown,
        "clock_skew_detected":       clock_skew,
        "agents_requiring_attention": requiring_attention,
        "source":                    "fma_classifier_v1",
        "agent_presence_summary": {
            "total":   total,
            "active":  active,
            "idle":    idle,
            "blocked": blocked,
            "stalled": stalled,
            "unknown": unknown,
        },
    }


def _empty_result() -> dict:
    return {
        "agents":                    [],
        "total":                     0,
        "active":                    0,
        "idle":                      0,
        "blocked":                   0,
        "stalled":                   0,
        "unknown":                   0,
        "clock_skew_detected":       False,
        "agents_requiring_attention": [],
        "source":                    "fma_classifier_v1",
        "agent_presence_summary": {
            "total": 0, "active": 0, "idle": 0,
            "blocked": 0, "stalled": 0, "unknown": 0,
        },
    }
