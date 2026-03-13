#!/usr/bin/env python3
"""
Sentinel Attach Bridge — deterministic recommendation signal.
TASK_ID: A-SEN-P4-001
OWNER:   GP-OPS

Evaluates current telemetry and emits a deterministic advisory signal
indicating whether enabling Sentinel would benefit the operator.

SAFETY:
  - Read-only: zero writes to openclaw.json
  - Zero gateway restarts
  - No network calls
  - All reads graceful on missing data (→ conservative / no recommendation)
  - Exits 0 always

Advisory only. Does NOT enable Sentinel automatically.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HIGH-confidence trigger weights (each = 20 pts toward confidence)
_HIGH_WEIGHT = 20
# MEDIUM-confidence trigger weights (each = 10 pts)
_MED_WEIGHT = 10
# Ceiling for confidence score
_MAX_CONF = 100

# Thresholds
_RADCHECK_HIGH_THRESHOLD = 75   # score < 75  → HIGH trigger
_RADCHECK_HEALTHY_THRESHOLD = 85  # score >= 85 → healthy
_PROT_EVENTS_HIGH = 2             # >= 2 prot events in 24h → HIGH trigger
_BACKUP_WEAK_HOURS = 48.0         # backup age > 48h → MEDIUM trigger

# Severity bands based on accumulated confidence
_SEV_HIGH_MIN = 80
_SEV_MED_MIN = 60

# ---------------------------------------------------------------------------
# Deterministic confidence scorer
# ---------------------------------------------------------------------------

def _score_to_severity(confidence: int) -> str:
    if confidence >= _SEV_HIGH_MIN:
        return "HIGH"
    if confidence >= _SEV_MED_MIN:
        return "MEDIUM"
    return "LOW"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------

def compute_sentinel_recommendation(snap: dict) -> dict:
    """
    Deterministic Sentinel recommendation signal.

    Input: agent911_state dict (or equivalent sub-dicts).
    Output: {recommended, confidence, severity, reasons, ts}

    Trigger logic (ordered for determinism — HIGH triggers evaluated first):

    HIGH triggers (20 pts each):
      1. predictive_risk_level in {MED, HIGH}
      2. compaction_risk == HIGH
      3. protection_events_24h >= 2
      4. routing_confidence == DEGRADED
      5. radcheck_score < 75

    MEDIUM triggers (10 pts each):
      6. compaction_state == SUSPECT (early warning)
      7. velocity == DEGRADING
      8. guard cycles present with suppressions (stall signatures)
      9. backup_age > 48h

    Healthy system guard (all must be true → recommended=False):
      - predictive LOW
      - compaction NOMINAL
      - routing HIGH
      - RadCheck >= 85
      - no recent protections (events_24h == 0)

    Confidence is bounded [0, 100]. Monotonic w.r.t. trigger count.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Extract sub-dicts safely ──────────────────────────────────────────────
    pred_guard   = snap.get("predictive_guard", {}) or {}
    comp         = snap.get("compaction_state", {}) or {}
    rollup       = snap.get("protection_rollup", {}) or {}
    routing      = snap.get("routing", {}) or {}
    rad          = snap.get("radcheck", {}) or {}
    backup       = snap.get("backup_state", {}) or {}
    prot_evts    = snap.get("protection_events_24h", {}) or {}

    # ── Field extraction ─────────────────────────────────────────────────────
    pred_risk_level   = str(pred_guard.get("risk_level", "")).upper()
    comp_risk         = str(comp.get("risk", "")).upper()
    comp_state        = str(comp.get("state", "")).upper()
    routing_conf      = str(routing.get("confidence", "")).upper()
    velocity          = str(rad.get("velocity_direction", "")).upper()
    rad_score         = _safe_float(rad.get("score"), 100.0)
    events_24h        = _safe_int(rollup.get("events_24h") or prot_evts.get("count"), 0)
    guard_cycles      = _safe_int(rollup.get("guard_cycles_24h"), 0)
    cooldown_suppr    = _safe_int(rollup.get("cooldown_suppressions_24h"), 0)
    backup_age_h      = _safe_float(backup.get("last_backup_age_hours"), 0.0)

    # ── Trigger evaluation (deterministic ordering) ───────────────────────────
    # HIGH triggers evaluated first, then MEDIUM.
    # The healthy-system guard is applied AFTER all triggers are evaluated,
    # so that MEDIUM signals (velocity, stall, backup) are not suppressed by
    # a partial healthy reading.
    reasons: list[str] = []
    confidence = 0

    # HIGH triggers (20 pts each)
    if pred_risk_level in ("MED", "MEDIUM", "HIGH"):
        reasons.append(f"Predictive risk elevated ({pred_risk_level})")
        confidence += _HIGH_WEIGHT

    if comp_risk == "HIGH":
        reasons.append("Compaction risk is HIGH")
        confidence += _HIGH_WEIGHT

    if events_24h >= _PROT_EVENTS_HIGH:
        reasons.append(f"{events_24h} Sentinel protection events in last 24h")
        confidence += _HIGH_WEIGHT

    if routing_conf == "DEGRADED":
        reasons.append("Routing confidence DEGRADED")
        confidence += _HIGH_WEIGHT

    if rad_score < _RADCHECK_HIGH_THRESHOLD:
        reasons.append(f"RadCheck score {int(rad_score)}/100 below threshold ({_RADCHECK_HIGH_THRESHOLD})")
        confidence += _HIGH_WEIGHT

    # MEDIUM triggers (10 pts each)
    if comp_state == "SUSPECT" and comp_risk not in ("HIGH",):
        reasons.append("Compaction early warning (SUSPECT state)")
        confidence += _MED_WEIGHT

    if velocity == "DEGRADING":
        reasons.append("Risk velocity trending upward (DEGRADING)")
        confidence += _MED_WEIGHT

    if guard_cycles > 0 and cooldown_suppr > 0:
        reasons.append(
            f"Stall signatures present ({guard_cycles} guard cycles, "
            f"{cooldown_suppr} cooldown suppressions)"
        )
        confidence += _MED_WEIGHT

    if backup_age_h > _BACKUP_WEAK_HOURS:
        reasons.append(f"Backup posture weak (last backup {backup_age_h:.0f}h ago)")
        confidence += _MED_WEIGHT

    # Clamp confidence
    confidence = min(confidence, _MAX_CONF)

    # ── Healthy system guard (FALSE POSITIVE prevention) ──────────────────────
    # Applied AFTER trigger evaluation.
    # When ALL five canonical healthy criteria are met AND no triggers fired,
    # the system is definitively healthy → recommended=False.
    # (If triggers fired, they override this guard — by design.)
    is_healthy = (
        pred_risk_level in ("LOW", "UNKNOWN", "")
        and comp_risk in ("LOW", "NOMINAL", "UNKNOWN", "")
        and routing_conf in ("HIGH", "IDLE", "UNKNOWN", "")
        and rad_score >= _RADCHECK_HEALTHY_THRESHOLD
        and events_24h == 0
        and len(reasons) == 0    # ← key: only if no triggers fired
    )
    if is_healthy:
        return {
            "recommended": False,
            "confidence":  0,
            "severity":    "LOW",
            "reasons":     [],
            "ts":          ts,
        }

    recommended = len(reasons) > 0

    return {
        "recommended": recommended,
        "confidence":  confidence,
        "severity":    _score_to_severity(confidence) if recommended else "LOW",
        "reasons":     reasons,
        "ts":          ts,
    }


# ---------------------------------------------------------------------------
# NDJSON event emitter (append-only; no cooldown — evaluation per run)
# ---------------------------------------------------------------------------

def emit_recommendation_event(rec: dict, ops_events_path: str) -> bool:
    """
    Append SENTINEL_RECOMMENDATION_EVAL to ops_events.log.
    Append-only; never overwrites existing content.
    Returns True on success, False on any error (safe to ignore).
    """
    event = {
        "ts":           rec["ts"],
        "event":        "SENTINEL_RECOMMENDATION_EVAL",
        "severity":     rec["severity"],
        "source":       "sentinel_attach_bridge",
        "recommended":  rec["recommended"],
        "confidence":   rec["confidence"],
        "reason_codes": rec["reasons"],
    }
    try:
        with open(ops_events_path, "a") as fh:
            fh.write(json.dumps(event) + "\n")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Dashboard renderer helpers
# ---------------------------------------------------------------------------

def render_sentinel_readiness_block(rec: dict) -> list:
    """
    Return dashboard lines for the SENTINEL READINESS section.
    Language is observational — no guarantees.
    """
    recommended = rec.get("recommended", False)
    confidence  = rec.get("confidence", 0)
    severity    = rec.get("severity", "LOW")
    reasons     = rec.get("reasons", [])
    ts          = rec.get("ts", "unknown")

    if not recommended:
        return [
            "  Sentinel recommendation: NOT NEEDED",
            "  System currently operating within expected bounds.",
            f"  Last evaluated: {ts}",
        ]

    lines = [
        "  Sentinel recommendation: ADVISED",
        f"  Confidence:             {confidence}/100",
        f"  Severity:               {severity}",
        "  Primary drivers:",
    ]
    for r in reasons[:4]:  # cap at 4 for dashboard readability
        lines.append(f"    - {r}")
    lines.append("  Suggested action: Consider enabling Sentinel for continuous protection coverage.")
    lines.append(f"  Last evaluated: {ts}")
    return lines


# ---------------------------------------------------------------------------
# Weekly report hook — returns advisory string or None
# ---------------------------------------------------------------------------

def weekly_report_advisory(rec: dict) -> str | None:
    """
    Returns the advisory sentence for the weekly report section 9,
    or None if no recommendation.
    """
    if rec.get("recommended"):
        return "Consider enabling Sentinel for continuous protection coverage."
    return None


# ---------------------------------------------------------------------------
# Standalone CLI (for testing/debugging)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    HOME     = os.path.expanduser("~")
    WATCHDOG = os.path.join(HOME, ".openclaw", "watchdog")
    STATE_F  = os.path.join(WATCHDOG, "agent911_state.json")
    OPS_F    = os.path.join(WATCHDOG, "ops_events.log")

    try:
        with open(STATE_F) as fh:
            snap = json.load(fh)
    except Exception as e:
        print(f"[WARN] Could not read state: {e}", file=sys.stderr)
        snap = {}

    rec = compute_sentinel_recommendation(snap)
    print(json.dumps(rec, indent=2))

    print("\n--- Dashboard block ---")
    for line in render_sentinel_readiness_block(rec):
        print(line)

    if "--emit" in sys.argv:
        ok = emit_recommendation_event(rec, OPS_F)
        print(f"\nEmit: {'OK' if ok else 'FAILED'}")

    sys.exit(0)
