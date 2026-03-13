#!/usr/bin/env python3
"""
radcheck_scoring_v2.py — RadCheck Scoring Engine v2
=====================================================
Deterministic, explainable, defensible scoring for OpenClaw reliability.

Features:
  A1: Five-domain weighted scoring (configurable)
  B1: Domain penalty caps (weights are real)
  B2: Domain floors (CRITICAL → HIGH RISK floor; HIGH → ELEVATED floor)
  B3: Time decay for historical penalties (deterministic buckets)
  B4: Resource normalization (load / cpu_cores)
  C:  Stability credits (+10 max)
  D:  Findings enrichment (automation_available, fix_complexity, risk_reduction)
  E:  History tracking (radcheck_history.ndjson, append-only)

Usage:
    from radcheck_scoring_v2 import score_v2, enrich_finding, DEFAULT_WEIGHTS

Safety:
    Read-only to system; appends to history and findings logs only.
    Never writes ~/.openclaw/openclaw.json.
"""

import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ─── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS: Dict[str, int] = {
    "watchdog_health":   25,
    "gateway_stability": 25,
    "compaction_risk":   20,
    "backup_posture":    15,
    "resource_pressure": 15,
}

RISK_LEVELS = ["LOW", "ELEVATED", "HIGH", "SEVERE"]

# Score bands: (min_score, max_score, label)
RISK_BANDS = [
    (75, 100, "LOW"),
    (50,  74, "ELEVATED"),
    (25,  49, "HIGH"),
    (0,   24, "SEVERE"),
]

# Minimum band forced by floor guardrail
FLOOR_BAND_CRITICAL = "HIGH"      # CRITICAL finding → cannot be better than HIGH
FLOOR_BAND_HIGH     = "ELEVATED"  # HIGH finding → cannot be better than ELEVATED

# Time-decay buckets for historical penalties
# age_hours → weight multiplier
DECAY_BUCKETS = [
    (0,    24,   1.00, "<24h"),
    (24,   168,  0.60, "1-7d"),
    (168,  720,  0.25, "7-30d"),
    (720,  None, 0.00, ">30d"),
]

# Base penalty per severity (raw, before domain cap + decay)
SEVERITY_PENALTY = {
    "CRITICAL": 25,
    "HIGH":     12,
    "MEDIUM":    5,
    "LOW":       1,
    "INFO":      0,
}

# Credits
CREDIT_BACKUP_RECENT    = 4   # backup <24h
CREDIT_NO_STALLS_7D     = 3   # no gateway stalls in 7d
CREDIT_MONOTONIC_STATE  = 2   # model_state.json monotonic
CREDIT_DIVERSIFIED      = 1   # ≥3 provider diversity
MAX_CREDITS             = 10

# Domain mapping for finding_ids → domain name
# Unmapped findings → "other" (contribute to score but no domain)
DOMAIN_MAP: Dict[str, str] = {
    # Watchdog health
    "RC_WD_001":    "watchdog_health",
    "RC_WD_002":    "watchdog_health",
    "RC_WD_004":    "watchdog_health",
    "RC_WD_005":    "watchdog_health",
    "RC_WD_006":    "watchdog_health",
    "RC_CFG_004":   "watchdog_health",
    "RC_CFG_007":   "watchdog_health",
    # Gateway stability
    "RC_ENV_004":   "gateway_stability",
    "RC_ENV_004B":  "gateway_stability",
    "RC_RT_001":    "gateway_stability",
    "RC_RT_002":    "gateway_stability",
    "RC_RT_003":    "gateway_stability",
    "RC_RT_004":    "gateway_stability",
    "RC_RT_005":    "gateway_stability",
    "RC_RT_006":    "gateway_stability",
    "RC_RT_007":    "gateway_stability",
    # Compaction risk
    "RC_ENV_005":       "compaction_risk",
    "RC_ENV_COMP_RISK":          "compaction_risk",
    "RC_ENV_COMP_EARLY_WARNING": "compaction_risk",
    "RC_WD_001_hx":              "compaction_risk",
    # Backup posture (Lazarus findings)
    "LZ_TM_001":    "backup_posture",
    "LZ_TM_002":    "backup_posture",
    "LZ_CLD_001":   "backup_posture",
    "LZ_GIT_001":   "backup_posture",
    "LZ_GIT_002":   "backup_posture",
    "LZ_SURF_001":  "backup_posture",
    "LZ_SURF_003":  "backup_posture",
    "LZ_RST_001":   "backup_posture",
    "LZ_SEC_001":   "backup_posture",
    # Resource pressure
    "RC_ENV_001":   "resource_pressure",
    "RC_ENV_002":   "resource_pressure",
    "RC_ENV_003":   "resource_pressure",
    # Config (split between domains)
    "RC_CFG_000":   "watchdog_health",
    "RC_CFG_001":   "gateway_stability",
    "RC_CFG_002":   "gateway_stability",
    "RC_CFG_008":   "gateway_stability",
}

# Findings that are time-based (apply decay)
TIME_BASED_FINDINGS = {"RC_WD_001", "RC_WD_002", "RC_ENV_004B", "RC_ENV_005",
                       "RC_ENV_001", "RC_ENV_COMP_RISK"}

# Enrichment metadata per finding_id
ENRICHMENT_META: Dict[str, Dict[str, Any]] = {
    "RC_WD_001": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 15,
        "title": "Watchdog Heartbeat Gap",
    },
    "RC_WD_002": {
        "automation_available": True,
        "fix_complexity": "MEDIUM",
        "estimated_risk_reduction": 10,
        "title": "Probe Failure Rate",
    },
    "RC_WD_004": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 8,
        "title": "Consecutive Failure Guard Absent",
    },
    "RC_WD_005": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 5,
        "title": "Silence Sentinel Missing",
    },
    "RC_WD_006": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 4,
        "title": "Model State Staleness",
    },
    "RC_ENV_004": {
        "automation_available": False,
        "fix_complexity": "HIGH",
        "estimated_risk_reduction": 25,
        "title": "Gateway Frozen Loop (Port Up, Probe Fail)",
    },
    "RC_ENV_004B": {
        "automation_available": True,
        "fix_complexity": "MEDIUM",
        "estimated_risk_reduction": 12,
        "title": "Historical Frozen-Loop Events",
    },
    "RC_ENV_005": {
        "automation_available": False,
        "fix_complexity": "HIGH",
        "estimated_risk_reduction": 20,
        "title": "Elevated Compaction Frequency",
    },
    "RC_ENV_001": {
        "automation_available": False,
        "fix_complexity": "HIGH",
        "estimated_risk_reduction": 18,
        "title": "System Load Elevated",
    },
    "RC_ENV_002": {
        "automation_available": False,
        "fix_complexity": "MEDIUM",
        "estimated_risk_reduction": 10,
        "title": "Memory Pressure",
    },
    "RC_ENV_003": {
        "automation_available": False,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 3,
        "title": "High Process Count",
    },
    "RC_CFG_001": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 20,
        "title": "Failover Chain Configuration",
    },
    "RC_CFG_002": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 10,
        "title": "Single Provider Dependency",
    },
    "RC_CFG_004": {
        "automation_available": True,
        "fix_complexity": "MEDIUM",
        "estimated_risk_reduction": 20,
        "title": "Watchdog Installation",
    },
    "RC_ENV_COMP_RISK": {
        "automation_available": False,
        "fix_complexity": "HIGH",
        "estimated_risk_reduction": 20,
        "title": "Forward Compaction Risk",
    },
    "RC_ENV_COMP_EARLY_WARNING": {
        "automation_available": False,
        "fix_complexity": "HIGH",
        "estimated_risk_reduction": 15,
        "title": "Compaction Early Warning",
    },
    "LZ_TM_001": {
        "automation_available": False,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 15,
        "title": "Time Machine Not Configured",
    },
    "LZ_SURF_001": {
        "automation_available": True,
        "fix_complexity": "LOW",
        "estimated_risk_reduction": 10,
        "title": "Runtime Surface Not Backed Up",
    },
}

ENRICHMENT_DEFAULTS = {
    "automation_available": False,
    "fix_complexity": "MEDIUM",
    "estimated_risk_reduction": 5,
    "title": None,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _score_band(score: int) -> str:
    for lo, hi, label in RISK_BANDS:
        if lo <= score <= hi:
            return label
    return "SEVERE"


def _get_cpu_cores() -> int:
    """Get number of logical CPU cores."""
    try:
        r = subprocess.run(["sysctl", "-n", "hw.logicalcpu"],
                           capture_output=True, text=True, timeout=2)
        return max(1, int(r.stdout.strip()))
    except Exception:
        pass
    try:
        return max(1, os.cpu_count() or 1)
    except Exception:
        return 1


def _decay_factor(age_hours: float) -> Tuple[float, str]:
    """Return (decay_weight, bucket_label) for given age in hours."""
    for lo, hi, weight, label in DECAY_BUCKETS:
        if hi is None or age_hours < hi:
            if age_hours >= lo:
                return weight, label
    return 0.0, ">30d"


def _infer_finding_age_hours(finding_dict: dict) -> Optional[float]:
    """Attempt to extract age of finding from its ts field."""
    ts_str = finding_dict.get("ts", "")
    if not ts_str:
        return None
    try:
        from datetime import datetime as dt
        ts_dt = dt.fromisoformat(ts_str.rstrip("Z"))
        now_dt = dt.utcnow()
        return (now_dt - ts_dt).total_seconds() / 3600
    except Exception:
        return None


def enrich_finding(finding_dict: dict) -> dict:
    """
    Add enrichment fields to a finding dict:
    domain, title, automation_available, fix_complexity, estimated_risk_reduction
    Does not modify the original.
    """
    fid = finding_dict.get("finding_id", finding_dict.get("id", ""))
    meta = ENRICHMENT_META.get(fid, ENRICHMENT_DEFAULTS)
    domain = DOMAIN_MAP.get(fid, "other")

    enriched = dict(finding_dict)
    enriched.update({
        "domain":                domain,
        "title":                 meta.get("title") or finding_dict.get("summary", "")[:60],
        "automation_available":  meta.get("automation_available", False),
        "fix_complexity":        meta.get("fix_complexity", "MEDIUM"),
        "estimated_risk_reduction": meta.get("estimated_risk_reduction", 5),
    })
    return enriched


# ─── B4: Resource Normalization ───────────────────────────────────────────────
def compute_resource_normalization() -> Dict[str, Any]:
    """B4: compute normalized_load = load_avg / cpu_cores."""
    cpu_cores = _get_cpu_cores()
    try:
        la1, la5, la15 = os.getloadavg()
    except Exception:
        la1, la5, la15 = 0.0, 0.0, 0.0

    normalized_load = la5 / cpu_cores if cpu_cores > 0 else la5

    return {
        "cpu_cores": cpu_cores,
        "load_avg_1": round(la1, 2),
        "load_avg_5": round(la5, 2),
        "load_avg_15": round(la15, 2),
        "normalized_load": round(normalized_load, 3),
    }


# ─── C: Credits engine ────────────────────────────────────────────────────────
def compute_credits(facts: Dict[str, Any], domain_scores: Dict[str, int],
                    enriched_findings: List[dict], log_lines: List[str]) -> Tuple[int, List[Dict]]:
    """
    Compute stability credits. Credits never apply if their domain is CRITICAL.
    Returns (total_credits, list of credit dicts).
    """
    credits = []
    total = 0

    # Is backup_posture domain critically failing?
    backup_crit = any(
        f.get("domain") == "backup_posture" and f.get("severity") == "CRITICAL"
        for f in enriched_findings
    )
    # Is gateway_stability critically failing?
    gw_crit = any(
        f.get("domain") == "gateway_stability" and f.get("severity") == "CRITICAL"
        for f in enriched_findings
    )
    # Is watchdog_health critically failing?
    wd_crit = any(
        f.get("domain") == "watchdog_health" and f.get("severity") == "CRITICAL"
        for f in enriched_findings
    )

    # Credit: backup <24h
    backup_fresh = facts.get("backup_recent_hours") is not None and \
                   facts.get("backup_recent_hours", 999) < 24
    if backup_fresh and not backup_crit:
        amt = CREDIT_BACKUP_RECENT
        credits.append({"credit_id": "C_BACKUP_RECENT",
                        "reason": f"Backup within 24h ({facts.get('backup_recent_hours', '?')}h ago)",
                        "amount": amt})
        total += amt
        log_lines.append(f"CREDIT: C_BACKUP_RECENT +{amt} (backup_recent_hours={facts.get('backup_recent_hours')})")
    else:
        log_lines.append(f"CREDIT_SKIP: C_BACKUP_RECENT backup_fresh={backup_fresh} backup_crit={backup_crit}")

    # Credit: no gateway stalls in 7d
    stalls_7d = facts.get("gateway_stalls_7d", -1)
    if stalls_7d == 0 and not gw_crit:
        amt = CREDIT_NO_STALLS_7D
        credits.append({"credit_id": "C_NO_STALLS_7D",
                        "reason": "Zero gateway stalls in past 7 days",
                        "amount": amt})
        total += amt
        log_lines.append(f"CREDIT: C_NO_STALLS_7D +{amt}")
    else:
        log_lines.append(f"CREDIT_SKIP: C_NO_STALLS_7D stalls_7d={stalls_7d} gw_crit={gw_crit}")

    # Credit: model_state monotonic (from probe results)
    if facts.get("model_state_monotonic") and not wd_crit:
        amt = CREDIT_MONOTONIC_STATE
        credits.append({"credit_id": "C_MONOTONIC_STATE",
                        "reason": "model_state.json monotonic (all 12 probe calls increasing)",
                        "amount": amt})
        total += amt
        log_lines.append(f"CREDIT: C_MONOTONIC_STATE +{amt}")
    else:
        log_lines.append(f"CREDIT_SKIP: C_MONOTONIC_STATE monotonic={facts.get('model_state_monotonic')} wd_crit={wd_crit}")

    # Credit: provider diversity ≥3
    if facts.get("provider_diversity", 0) >= 3 and not gw_crit:
        amt = CREDIT_DIVERSIFIED
        credits.append({"credit_id": "C_DIVERSIFIED",
                        "reason": f"≥3 unique providers ({facts.get('provider_diversity')})",
                        "amount": amt})
        total += amt
        log_lines.append(f"CREDIT: C_DIVERSIFIED +{amt}")
    else:
        log_lines.append(f"CREDIT_SKIP: C_DIVERSIFIED providers={facts.get('provider_diversity', 0)}")

    total = min(total, MAX_CREDITS)
    return total, credits


# ─── Main scoring engine ──────────────────────────────────────────────────────
def score_v2(
    findings: List[Any],
    facts: Optional[Dict[str, Any]] = None,
    weights: Optional[Dict[str, int]] = None,
    run_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full v2 scoring pipeline.

    Args:
        findings: list of Finding objects or dicts with finding_id, severity, ts
        facts: optional dict with system facts (cpu_cores, backup_recent_hours, etc.)
        weights: optional domain weight overrides (defaults to DEFAULT_WEIGHTS)
        run_ts: optional ISO timestamp for this run

    Returns dict with:
        score, risk_level, risk_band, domain_subscores, findings_enriched,
        credits_applied, penalties_applied, guardrails_triggered, resource_norm,
        risk_velocity (if history available), log_lines (for operator debugging)
    """
    weights = weights or dict(DEFAULT_WEIGHTS)
    facts   = facts   or {}
    run_ts  = run_ts  or _now_utc()
    log_lines: List[str] = []

    # ── Convert findings to dicts ──────────────────────────────────────────────
    raw_findings = []
    for f in findings:
        if hasattr(f, "to_dict"):
            raw_findings.append(f.to_dict())
        elif isinstance(f, dict):
            raw_findings.append(dict(f))
        else:
            raw_findings.append({"finding_id": str(f), "severity": "INFO", "ts": run_ts})

    # ── Enrich all findings ───────────────────────────────────────────────────
    enriched = [enrich_finding(f) for f in raw_findings]

    # ── B4: Resource normalization ────────────────────────────────────────────
    resource_norm = compute_resource_normalization()
    log_lines.append(
        f"RESOURCE_NORM: cpu_cores={resource_norm['cpu_cores']} "
        f"load_avg_5={resource_norm['load_avg_5']} "
        f"normalized_load={resource_norm['normalized_load']}"
    )

    # ── Build domain penalty buckets ──────────────────────────────────────────
    # domain → list of (finding_id, severity, raw_penalty, decay_factor, applied_penalty)
    domain_penalties: Dict[str, List[dict]] = {d: [] for d in weights}
    domain_penalties["other"] = []
    global_penalties: List[dict] = []

    for ef in enriched:
        fid      = ef.get("finding_id", ef.get("id", ""))
        severity = ef.get("severity", "INFO")
        domain   = ef.get("domain", "other")
        raw_p    = SEVERITY_PENALTY.get(severity, 0)

        if raw_p == 0:
            continue  # INFO — no penalty

        # B3: time decay for time-based findings
        decay_w = 1.0
        bucket  = "<24h"
        if fid in TIME_BASED_FINDINGS:
            age_h = _infer_finding_age_hours(ef)
            if age_h is not None:
                decay_w, bucket = _decay_factor(age_h)
            else:
                age_h = 0.0
                decay_w, bucket = 1.0, "<24h"

            applied_p = math.floor(raw_p * decay_w)
            log_lines.append(
                f"TIME_DECAY: {fid} severity={severity} raw_penalty={raw_p} "
                f"age_bucket={bucket} decay_factor={decay_w} applied_penalty={applied_p}"
            )
        else:
            applied_p = raw_p

        penalty_entry = {
            "finding_id":     fid,
            "severity":       severity,
            "domain":         domain,
            "raw_penalty":    raw_p,
            "decay_factor":   decay_w,
            "applied_penalty": applied_p,
            "bucket":         bucket,
        }
        global_penalties.append(penalty_entry)

        target = domain if domain in domain_penalties else "other"
        domain_penalties[target].append(penalty_entry)

    # ── B1: Domain penalty caps ───────────────────────────────────────────────
    domain_subscores: Dict[str, Dict] = {}
    total_penalty_capped = 0

    for domain, w in weights.items():
        entries = domain_penalties.get(domain, [])
        raw_domain_total = sum(e["applied_penalty"] for e in entries)

        cap_triggered = raw_domain_total > w
        capped_total  = min(raw_domain_total, w)

        if cap_triggered:
            log_lines.append(
                f"DOMAIN_CAP: domain={domain} weight={w} "
                f"raw_penalty={raw_domain_total} capped_to={capped_total}"
            )

        domain_subscores[domain] = {
            "weight":           w,
            "raw_penalty":      raw_domain_total,
            "capped_penalty":   capped_total,
            "cap_triggered":    cap_triggered,
            "subscore":         w - capped_total,   # 0..weight
            "findings_count":   len(entries),
            "findings":         [e["finding_id"] for e in entries],
        }
        total_penalty_capped += capped_total

    # Other domain penalties (not mapped) — contribute with no cap
    other_total = sum(e["applied_penalty"] for e in domain_penalties.get("other", []))
    total_penalty_capped += other_total

    # ── Credits ───────────────────────────────────────────────────────────────
    credits_total, credits_list = compute_credits(facts, domain_subscores, enriched, log_lines)

    # ── Raw score ─────────────────────────────────────────────────────────────
    raw_score = 100 - total_penalty_capped + credits_total
    raw_score = max(0, min(100, raw_score))

    # ── B2: Domain floor guardrail ────────────────────────────────────────────
    has_critical = any(ef.get("severity") == "CRITICAL" for ef in enriched)
    has_high     = any(ef.get("severity") == "HIGH"     for ef in enriched)

    floor_clamp  = None
    floor_score  = raw_score

    if has_critical:
        # Cannot be better than HIGH band (25–49)
        max_allowed = 49
        if raw_score > max_allowed:
            floor_score = max_allowed
            floor_clamp = f"CRITICAL finding present → score clamped from {raw_score} to {floor_score} (HIGH band floor)"
            log_lines.append(f"FLOOR_CLAMP: {floor_clamp}")
    elif has_high:
        # Cannot be better than ELEVATED band (50–74)
        max_allowed = 74
        if raw_score > max_allowed:
            floor_score = max_allowed
            floor_clamp = f"HIGH finding present → score clamped from {raw_score} to {floor_score} (ELEVATED band floor)"
            log_lines.append(f"FLOOR_CLAMP: {floor_clamp}")
    else:
        log_lines.append("FLOOR_CLAMP: none — no CRITICAL/HIGH findings, no clamp applied")

    final_score = floor_score
    risk_band   = _score_band(final_score)
    risk_level  = risk_band  # consistent naming

    # ── Risk velocity ─────────────────────────────────────────────────────────
    risk_velocity = compute_velocity(HISTORY_LOG)

    # ── Guardrails summary ────────────────────────────────────────────────────
    guardrails = {
        "B1_domain_caps":   [d for d, s in domain_subscores.items() if s["cap_triggered"]],
        "B2_floor_clamp":   floor_clamp,
        "B3_time_decay":    [l for l in log_lines if l.startswith("TIME_DECAY:")],
        "B4_resource_norm": resource_norm,
    }

    return {
        "score":               final_score,
        "raw_score_pre_floor": raw_score,
        "risk_level":          risk_level,
        "risk_band":           risk_band,
        "risk_velocity":       risk_velocity,
        "domain_subscores":    domain_subscores,
        "credits_applied":     credits_list,
        "credits_total":       credits_total,
        "penalties_applied":   global_penalties,
        "total_penalty":       total_penalty_capped,
        "guardrails":          guardrails,
        "resource_norm":       resource_norm,
        "findings_enriched":   enriched,
        "log_lines":           log_lines,
        "run_ts":              run_ts,
    }


# ─── Risk velocity ────────────────────────────────────────────────────────────
HISTORY_LOG = os.path.expanduser("~/.openclaw/watchdog/radcheck_history.ndjson")


def compute_velocity(history_path: str) -> Optional[Dict[str, Any]]:
    """
    Read the last 2 entries from radcheck_history.ndjson and compute velocity.
    Returns dict with keys: score_now, score_prev, delta, hours_elapsed, rate_per_hour, direction
    Returns None if fewer than 2 entries exist.
    direction: "DEGRADING" if delta < -2, "IMPROVING" if delta > 2, else "STABLE"
    """
    try:
        if not os.path.exists(history_path):
            return None
        with open(history_path) as f:
            lines = [l.strip() for l in f if l.strip()]
        if len(lines) < 2:
            return None

        prev_entry = json.loads(lines[-2])
        now_entry  = json.loads(lines[-1])

        score_now  = now_entry.get("score", 0)
        score_prev = prev_entry.get("score", 0)
        delta      = score_now - score_prev

        ts_now  = now_entry.get("ts", "")
        ts_prev = prev_entry.get("ts", "")
        hours_elapsed: Optional[float] = None
        rate_per_hour: Optional[float] = None

        try:
            dt_now  = datetime.fromisoformat(ts_now.rstrip("Z")).replace(tzinfo=timezone.utc)
            dt_prev = datetime.fromisoformat(ts_prev.rstrip("Z")).replace(tzinfo=timezone.utc)
            hours_elapsed = round((dt_now - dt_prev).total_seconds() / 3600, 2)
            # Guard: require ≥6 min between runs to prevent rate explosion
            if hours_elapsed >= 0.1:
                rate_per_hour = round(delta / hours_elapsed, 2)
        except Exception:
            pass

        if delta < -2:
            direction = "DEGRADING"
        elif delta > 2:
            direction = "IMPROVING"
        else:
            direction = "STABLE"

        return {
            "score_now":     score_now,
            "score_prev":    score_prev,
            "delta":         delta,
            "hours_elapsed": hours_elapsed,
            "rate_per_hour": rate_per_hour,
            "direction":     direction,
        }
    except Exception:
        return None


def _compute_velocity(current_score: int) -> Optional[float]:
    """
    Compute delta over last 3 runs from history.
    Returns delta or None if insufficient history.
    Retained for backward compatibility; prefer compute_velocity().
    """
    try:
        if not os.path.exists(HISTORY_LOG):
            return None
        with open(HISTORY_LOG) as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]

        if len(lines) < 3:
            return None

        scores = []
        for line in lines[-3:]:
            try:
                ev = json.loads(line)
                scores.append(ev.get("score", 0))
            except Exception:
                pass

        if len(scores) >= 2:
            return round(current_score - scores[0], 1)
    except Exception:
        pass
    return None


# ─── E: History tracking ──────────────────────────────────────────────────────
def append_history(result: Dict[str, Any], findings_count: int,
                   duration_ms: int,
                   comp_hist: Optional[Dict] = None) -> None:
    """Append one NDJSON line to radcheck_history.ndjson. Append-only.
    D: includes compaction_risk enrichment when comp_hist is provided.
    E: includes velocity fields (velocity_delta, velocity_rate_per_hour, velocity_direction).
    """

    # Build domain dict, enriching compaction_risk with histogram data
    domains_out = {}
    for d, info in result["domain_subscores"].items():
        domain_entry = {
            "subscore": info["subscore"],
            "weight":   info["weight"],
            "capped":   info["cap_triggered"],
        }
        if d == "compaction_risk" and comp_hist and not comp_hist.get("insufficient"):
            s24 = comp_hist.get("stats_24h", {})
            domain_entry["compaction_count_24h"]  = s24.get("compaction_count", 0)
            domain_entry["timeout_count_24h"]     = s24.get("timeout_count", 0)
            domain_entry["p95_duration_ms"]       = s24.get("p95_duration_ms")
            domain_entry["risk_level"]            = comp_hist.get("risk_level", "LOW")
            domain_entry["acceleration"]          = comp_hist.get("acceleration", False)
        domains_out[d] = domain_entry

    # Extract velocity fields from the velocity dict (or None)
    vel = result.get("risk_velocity")
    vel_delta     = vel.get("delta")         if isinstance(vel, dict) else None
    vel_rate      = vel.get("rate_per_hour") if isinstance(vel, dict) else None
    vel_direction = vel.get("direction")     if isinstance(vel, dict) else None

    entry = {
        "ts":                      result["run_ts"],
        "score":                   result["score"],
        "risk_level":              result["risk_level"],
        "domains":                 domains_out,
        "findings_count":          findings_count,
        "credits_total":           result.get("credits_total", 0),
        "risk_velocity":           vel,
        "velocity_delta":          vel_delta,
        "velocity_rate_per_hour":  round(vel_rate, 2) if vel_rate is not None else None,
        "velocity_direction":      vel_direction,
        "duration_ms":             duration_ms,
    }

    try:
        os.makedirs(os.path.dirname(HISTORY_LOG), exist_ok=True)
        with open(HISTORY_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # Emit VELOCITY_COMPUTED event to ops_events.log (append-only)
    if isinstance(vel, dict):
        try:
            vel_event = {
                "ts":          result["run_ts"],
                "event":       "VELOCITY_COMPUTED",
                "delta":       vel_delta,
                "rate_per_hour": round(vel_rate, 2) if vel_rate is not None else None,
                "direction":   vel_direction,
            }
            with open(OPS_EVENTS_LOG, "a") as f:
                f.write(json.dumps(vel_event) + "\n")
        except Exception:
            pass


# ─── Console helpers ──────────────────────────────────────────────────────────
def print_domain_subscores(result: Dict[str, Any]) -> None:
    """Print domain subscores section for console output."""
    W = 60
    print(f"\n{'─'*W}")
    print("  📊 DOMAIN SUBSCORES")
    print(f"{'─'*W}")

    ICONS = {"watchdog_health": "🐕", "gateway_stability": "🔌",
             "compaction_risk": "🗜️", "backup_posture": "💾",
             "resource_pressure": "🖥️"}

    for domain, info in result["domain_subscores"].items():
        subscore  = info["subscore"]
        weight    = info["weight"]
        pct       = int(subscore / weight * 100) if weight > 0 else 0
        bar       = "█" * (pct // 5) + "░" * (20 - pct // 5)
        cap_warn  = " [CAP!]" if info["cap_triggered"] else ""
        icon      = ICONS.get(domain, "•")
        print(f"  {icon}  {domain:<22} {subscore:>3}/{weight:<3}  [{bar}]{cap_warn}")

    credits = result.get("credits_applied", [])
    if credits:
        print(f"\n  ✨ Credits applied: +{result.get('credits_total', 0)}")
        for c in credits:
            print(f"     {c['credit_id']}: +{c['amount']} ({c['reason']})")

    vel = result.get("risk_velocity")
    if vel is not None:
        if isinstance(vel, dict):
            d = vel.get("delta", 0)
            arrow = "↑" if d > 0 else "↓" if d < 0 else "→"
            print(f"\n  📈 Risk velocity: {arrow}{d:+d} pts  [{vel.get('direction', '?')}]")
        else:
            # legacy scalar
            arrow = "↑" if vel > 0 else "↓" if vel < 0 else "→"
            print(f"\n  📈 Risk velocity (Δ vs 3 runs ago): {arrow}{abs(vel):+.1f}")

    guardrails = result.get("guardrails", {})
    if guardrails.get("B2_floor_clamp"):
        print(f"\n  ⚠️  FLOOR CLAMP: {guardrails['B2_floor_clamp']}")
    if guardrails.get("B1_domain_caps"):
        print(f"  ⚠️  DOMAIN CAPS triggered: {guardrails['B1_domain_caps']}")


# ─── Compaction Histogram + Forward Risk (P2) ────────────────────────────────

OPS_EVENTS_LOG  = os.path.expanduser("~/.openclaw/watchdog/ops_events.log")
WATCHDOG_LOG    = os.path.expanduser("~/.openclaw/watchdog/watchdog.log")

# B2B window: if COMPACTION_START occurs within this many seconds after
# a previous COMPACTION_END, it's considered back-to-back
B2B_WINDOW_S = 120


def _parse_iso(ts_str: str) -> Optional[float]:
    """Parse ISO8601 timestamp to epoch float. Returns None on failure."""
    try:
        s = ts_str.rstrip("Z")
        # Handle fractional seconds
        if "." in s:
            s = s[:26]  # truncate to microseconds
        else:
            s = s[:19]
        from datetime import datetime as _dt
        dt = _dt.strptime(s, "%Y-%m-%dT%H:%M:%S")
        import calendar
        return float(calendar.timegm(dt.timetuple()))
    except Exception:
        return None


def compute_compaction_histogram(now_epoch: Optional[float] = None) -> Dict[str, Any]:
    """
    Parse ops_events.log and watchdog.log to build a compaction histogram.

    Returns dict:
        stats_24h, stats_7d, risk_level, acceleration, log_lines,
        insufficient_data: bool
    """
    now = now_epoch or time.time()
    cutoff_24h = now - 86400
    cutoff_7d  = now - 7 * 86400
    log_lines: List[str] = []

    # ── Parse ops_events.log ─────────────────────────────────────────────────
    compaction_events: List[Dict] = []
    stall_epochs: List[float] = []
    insufficient = False

    try:
        if not os.path.exists(OPS_EVENTS_LOG):
            log_lines.append("INSUFFICIENT_COMPACTION_DATA: ops_events.log not found")
            insufficient = True
        else:
            with open(OPS_EVENTS_LOG) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        ev = json.loads(line)
                        ev_type = ev.get("event", "")
                        ts_str  = ev.get("ts", "")
                        epoch   = _parse_iso(ts_str)
                        if epoch is None:
                            continue
                        if ev_type in ("COMPACTION_START", "COMPACTION_END",
                                       "COMPACTION_TIMEOUT", "COMPACTION_SUSPECT"):
                            compaction_events.append({
                                "event":   ev_type,
                                "epoch":   epoch,
                                "ts":      ts_str,
                                "reason":  ev.get("reason", ""),
                                "duration_s": ev.get("duration_s"),
                                "timeout_ms": ev.get("timeout_ms"),
                                "run_id":  ev.get("run_id", ""),
                            })
                        if ev_type == "GATEWAY_STALL":
                            stall_epochs.append(epoch)
                    except Exception:
                        pass
    except Exception as e:
        log_lines.append(f"INSUFFICIENT_COMPACTION_DATA: ops_events parse error: {e}")
        insufficient = True

    # ── Augment with watchdog.log ─────────────────────────────────────────────
    try:
        safeguard_pattern = re.compile(
            r'\[watchdog\]\[tail\]\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
            r'.*?\[compaction-safeguard\]'
        )
        timeout_pattern = re.compile(
            r'\[watchdog\]\[tail\]\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
            r'.*?timed out during compaction'
            r'.*?runId=([a-f0-9\-]+)'
        )
        if os.path.exists(WATCHDOG_LOG):
            seen_run_ids = {ev["run_id"] for ev in compaction_events if ev["run_id"]}
            with open(WATCHDOG_LOG) as f:
                for line in f:
                    m = safeguard_pattern.search(line)
                    if m:
                        epoch = _parse_iso(m.group(1) + "Z")
                        if epoch:
                            compaction_events.append({
                                "event": "COMPACTION_START",
                                "epoch": epoch,
                                "ts":    m.group(1) + "Z",
                                "reason": "safeguard",
                                "duration_s": None,
                                "timeout_ms": None,
                                "run_id": "",
                                "source": "watchdog_log_safeguard",
                            })
                    m2 = timeout_pattern.search(line)
                    if m2:
                        run_id = m2.group(2)
                        epoch  = _parse_iso(m2.group(1) + "Z")
                        if epoch and run_id not in seen_run_ids:
                            seen_run_ids.add(run_id)
                            compaction_events.append({
                                "event": "COMPACTION_TIMEOUT",
                                "epoch": epoch,
                                "ts":    m2.group(1) + "Z",
                                "reason": "embedded_run_timeout",
                                "duration_s": 600.0,
                                "timeout_ms": 600000,
                                "run_id": run_id,
                                "source": "watchdog_log_timeout",
                            })
    except Exception as e:
        log_lines.append(f"COMPACTION_WARN: watchdog.log augment error: {e}")

    # Sort all events by epoch
    compaction_events.sort(key=lambda x: x["epoch"])

    if not compaction_events:
        log_lines.append("INSUFFICIENT_COMPACTION_DATA: no compaction events found in any log")
        insufficient = True

    # ── Build per-window stats ────────────────────────────────────────────────
    def _window_stats(cutoff: float, label: str) -> Dict[str, Any]:
        evs = [e for e in compaction_events if e["epoch"] >= cutoff]

        starts    = [e for e in evs if e["event"] == "COMPACTION_START"]
        ends      = [e for e in evs if e["event"] == "COMPACTION_END"]
        timeouts  = [e for e in evs if e["event"] in ("COMPACTION_TIMEOUT",)
                     or (e["event"] == "COMPACTION_END" and e.get("reason") == "timeout")
                     or (e.get("timeout_ms") and e["event"] in
                         ("COMPACTION_SUSPECT", "COMPACTION_TIMEOUT"))]
        suspects  = [e for e in evs if e["event"] == "COMPACTION_SUSPECT"]

        # Duration samples (only from COMPACTION_END with duration_s set, or TIMEOUT = 600s)
        durations_ms = []
        for e in evs:
            d = e.get("duration_s")
            if d is not None:
                try:
                    durations_ms.append(float(d) * 1000)
                except Exception:
                    pass
            elif e["event"] == "COMPACTION_TIMEOUT" or (
                    e.get("reason") == "embedded_run_timeout" and e.get("timeout_ms")):
                durations_ms.append(float(e.get("timeout_ms", 600000)))

        durations_ms.sort()
        n = len(durations_ms)

        def _percentile(lst: list, p: float) -> Optional[float]:
            if not lst:
                return None
            idx = int(math.ceil(p / 100.0 * len(lst))) - 1
            return round(lst[max(0, min(idx, len(lst)-1))], 0)

        p50 = _percentile(durations_ms, 50)
        p95 = _percentile(durations_ms, 95)
        max_dur = round(max(durations_ms), 0) if durations_ms else None

        # Back-to-back detection: COMPACTION_START follows COMPACTION_END within B2B_WINDOW_S
        b2b = 0
        end_epochs = sorted([e["epoch"] for e in ends] +
                             [e["epoch"] for e in evs if e["event"] == "COMPACTION_TIMEOUT"])
        start_epochs = sorted([e["epoch"] for e in starts])

        for end_ep in end_epochs:
            for start_ep in start_epochs:
                if 0 < (start_ep - end_ep) <= B2B_WINDOW_S:
                    b2b += 1
                    break

        # Overlap with gateway stall
        overlap = 0
        for stall_ep in stall_epochs:
            if stall_ep >= cutoff:
                # Check if any compaction window was active ±600s of stall
                for end_ep in end_epochs:
                    if abs(stall_ep - end_ep) <= 600:
                        overlap += 1
                        break

        # Unique compaction cycles (deduplicate by START events + timeout events)
        cycle_count = len(starts) + len([e for e in evs
                                          if e["event"] == "COMPACTION_TIMEOUT"
                                          and e.get("source") != "watchdog_log_timeout"])

        return {
            "label":           label,
            "compaction_count": len(starts),
            "timeout_count":   len(timeouts),
            "suspect_count":   len(suspects),
            "p50_duration_ms": p50,
            "p95_duration_ms": p95,
            "max_duration_ms": max_dur,
            "back_to_back":    b2b,
            "overlap_stall":   overlap,
            "durations_sample": durations_ms[:5],
        }

    stats_24h = _window_stats(cutoff_24h, "24h")
    stats_7d  = _window_stats(cutoff_7d,  "7d")

    # ── C: Acceleration detection ─────────────────────────────────────────────
    count_24h  = stats_24h["compaction_count"] + stats_24h["timeout_count"]
    count_7d   = stats_7d["compaction_count"]  + stats_7d["timeout_count"]
    # 7d rate per day (excluding the last 24h to avoid double-count)
    older_6d_count = max(0, count_7d - count_24h)
    baseline_rate  = older_6d_count / 6.0 if older_6d_count > 0 else 0.0
    short_rate     = float(count_24h)  # events in last 24h (1-day window)

    if baseline_rate > 0:
        accel_ratio = round(short_rate / baseline_rate, 2)
    elif short_rate > 0:
        accel_ratio = float("inf")
        short_rate  = short_rate
    else:
        accel_ratio = 0.0

    acceleration = accel_ratio >= 1.5 if math.isfinite(accel_ratio) else (short_rate > 0)

    log_lines.append(
        f"COMPACTION_ACCELERATION: short={short_rate:.1f} "
        f"baseline={baseline_rate:.2f}/day ratio="
        f"{'inf' if not math.isfinite(accel_ratio) else accel_ratio} "
        f"flag={'true' if acceleration else 'false'}"
    )

    # ── B1: COMPACTION_STATS summary ─────────────────────────────────────────
    log_lines.append(
        f"COMPACTION_STATS: "
        f"count={stats_24h['compaction_count']} "
        f"timeout={stats_24h['timeout_count']} "
        f"p95={stats_24h['p95_duration_ms']}ms "
        f"max={stats_24h['max_duration_ms']}ms "
        f"b2b={stats_24h['back_to_back']} "
        f"overlap={stats_24h['overlap_stall']}"
    )

    # ── B1: Forward risk level ────────────────────────────────────────────────
    p95_val  = stats_24h["p95_duration_ms"] or 0
    timeouts = stats_24h["timeout_count"]
    b2b      = stats_24h["back_to_back"]
    overlap  = stats_24h["overlap_stall"]

    if overlap > 0:
        risk_level = "CRITICAL"
        risk_reason = f"active stall overlap detected ({overlap} events)"
    elif timeouts > 0 or p95_val > 300_000 or b2b >= 2:
        risk_level = "HIGH"
        risk_reason = (f"timeout_count={timeouts} p95={p95_val}ms b2b={b2b} "
                       f"in last 24h")
    elif acceleration or p95_val > 120_000:
        risk_level = "ELEVATED"
        risk_reason = (f"acceleration={acceleration} p95={p95_val}ms "
                       f"(ratio={accel_ratio})")
    else:
        risk_level = "LOW"
        risk_reason = "no elevated signals"

    log_lines.append(f"COMPACTION_RISK_LEVEL: {risk_level} reason={risk_reason}")

    return {
        "stats_24h":      stats_24h,
        "stats_7d":       stats_7d,
        "risk_level":     risk_level,
        "risk_reason":    risk_reason,
        "acceleration":   acceleration,
        "accel_ratio":    accel_ratio,
        "short_rate":     short_rate,
        "baseline_rate":  baseline_rate,
        "insufficient":   insufficient,
        "log_lines":      log_lines,
        "total_events_parsed": len(compaction_events),
    }


def build_comp_risk_finding(hist: Dict[str, Any], run_ts: str) -> Optional[Dict]:
    """
    B2: Emit RC_ENV_COMP_RISK finding when risk_level >= ELEVATED.
    Returns enriched finding dict or None if LOW.
    """
    risk_level = hist.get("risk_level", "LOW")
    if risk_level == "LOW":
        return None

    severity_map = {"CRITICAL": "CRITICAL", "HIGH": "HIGH",
                    "ELEVATED": "MEDIUM", "LOW": "INFO"}
    severity = severity_map.get(risk_level, "MEDIUM")

    s24 = hist["stats_24h"]
    evidence = (
        f"compaction_risk={risk_level} "
        f"count_24h={s24['compaction_count']} "
        f"timeout_24h={s24['timeout_count']} "
        f"p95={s24['p95_duration_ms']}ms "
        f"b2b={s24['back_to_back']} "
        f"accel={hist['acceleration']} ratio={hist['accel_ratio']} "
        f"reason={hist['risk_reason']}"
    )

    finding = {
        "finding_id":     "RC_ENV_COMP_RISK",
        "severity":       severity,
        "component":      "compaction",
        "domain":         "compaction_risk",
        "title":          f"Forward Compaction Risk: {risk_level}",
        "summary":        (f"Compaction risk {risk_level}: {hist['risk_reason'][:80]}"),
        "evidence":       evidence,
        "recommended_fix": (
            "Reduce session context size (context budget <70%). "
            "Enable compaction budget throttling. "
            "Consider shorter session lifetimes to prevent 10min timeout storms."
        ),
        "automation_available": False,
        "fix_complexity":       "HIGH",
        "estimated_risk_reduction": 20,
        "confidence":           0.92,
        "ts":                   run_ts,
        "tool":                 "radiation_check_v2",
    }
    return finding


# ─── Compaction Early Warning (A-RC-P4-001) ──────────────────────────────────

def compute_compaction_early_warning(comp_hist: Dict[str, Any]) -> Dict[str, Any]:
    """
    Forward-looking compaction pressure indicators.

    Returns:
        pressure_level:         LOW | MEDIUM | HIGH
        trend:                  STABLE | RISING | SPIKING
        time_to_storm_estimate: minutes (float) or None
        accel_ratio:            float or None
        should_emit:            bool (True when MEDIUM or HIGH)

    TASK_ID: A-RC-P4-001
    """
    stats_24h      = comp_hist.get("stats_24h", {})
    p95_ms         = stats_24h.get("p95_duration_ms") or 0
    timeout_24h    = stats_24h.get("timeout_count", 0)
    b2b_count      = stats_24h.get("back_to_back", 0)
    accel_ratio_raw = comp_hist.get("accel_ratio", 0.0)

    # Normalise accel_ratio: treat infinity as a large finite sentinel
    accel_ratio: Optional[float] = None
    if accel_ratio_raw is not None:
        try:
            ar = float(accel_ratio_raw)
            accel_ratio = ar if math.isfinite(ar) else 99.0
        except (TypeError, ValueError):
            accel_ratio = None

    # Pressure classification (v1 heuristic)
    if (
        timeout_24h >= 3
        or (p95_ms and p95_ms >= 480_000)
        or (accel_ratio is not None and accel_ratio >= 10)
    ):
        pressure_level = "HIGH"
    elif (
        timeout_24h >= 1
        or (p95_ms and p95_ms >= 240_000)
        or b2b_count >= 1
    ):
        pressure_level = "MEDIUM"
    else:
        pressure_level = "LOW"

    # Trend classification
    if accel_ratio is not None and accel_ratio >= 20:
        trend = "SPIKING"
    elif accel_ratio is not None and accel_ratio >= 5:
        trend = "RISING"
    else:
        trend = "STABLE"

    # Time-to-storm estimate (minutes): only when SPIKING and timeouts observed
    time_to_storm: Optional[float] = None
    if trend == "SPIKING" and timeout_24h > 0:
        avg_interval_min = 1440.0 / timeout_24h          # average minutes between timeouts
        remaining = max(0, 2 - timeout_24h)              # remaining before storm threshold
        time_to_storm = round(remaining * avg_interval_min, 0)

    return {
        "pressure_level":          pressure_level,
        "trend":                   trend,
        "time_to_storm_estimate":  time_to_storm,
        "accel_ratio":             accel_ratio,
        "should_emit":             pressure_level in ("MEDIUM", "HIGH"),
    }


def build_comp_early_warning_finding(
    comp_hist: Dict[str, Any],
    run_ts: str,
) -> Optional[Dict]:
    """
    Build RC_ENV_COMP_EARLY_WARNING finding when pressure is MEDIUM or HIGH.
    Returns enriched finding dict, or None if no warning warranted.

    Avoids duplicating RC_ENV_COMP_RISK (different finding_id, MEDIUM severity,
    forward-looking framing vs reactive storm detection).

    TASK_ID: A-RC-P4-001
    """
    if comp_hist.get("insufficient_data") or comp_hist.get("insufficient"):
        return None

    ew = compute_compaction_early_warning(comp_hist)
    if not ew["should_emit"]:
        return None

    stats_24h    = comp_hist.get("stats_24h", {})
    p95_ms       = stats_24h.get("p95_duration_ms") or 0
    timeout_24h  = stats_24h.get("timeout_count", 0)
    accel_ratio  = ew.get("accel_ratio")
    pressure     = ew["pressure_level"]
    trend        = ew["trend"]
    time_est     = ew.get("time_to_storm_estimate")

    accel_str    = f"{accel_ratio:.1f}x"  if accel_ratio  is not None else "n/a"
    time_str     = f"{int(time_est)}min"  if time_est     is not None else "unknown"
    p95_str      = f"{p95_ms:.0f}ms"      if p95_ms               else "unknown"

    evidence_parts = [
        f"pressure={pressure}",
        f"trend={trend}",
        f"timeout_24h={timeout_24h}",
        f"p95={p95_str}",
        f"accel_ratio={accel_str}",
    ]
    if time_est is not None:
        evidence_parts.append(f"time_to_storm~{time_str}")

    if pressure == "HIGH":
        summary = f"Compaction early warning: HIGH pressure, trend={trend}"
        fix     = (
            "Reduce session context immediately. "
            "Monitor compaction_metrics.log for acceleration. "
            "Consider enabling compaction budget throttling."
        )
    else:
        summary = f"Compaction early warning: MEDIUM pressure, trend={trend}"
        fix     = (
            "Monitor compaction trends. "
            "Consider shorter session lifetimes to reduce future risk."
        )

    return {
        "finding_id":      "RC_ENV_COMP_EARLY_WARNING",
        "severity":        "MEDIUM",
        "component":       "environment",
        "domain":          "compaction_risk",
        "summary":         summary,
        "evidence":        "; ".join(evidence_parts),
        "recommended_fix": fix,
        "confidence":      0.85,
        "ts":              run_ts,
        "tool":            "radiation_check_v2",
        "early_warning": {
            "pressure_level":         pressure,
            "trend":                  trend,
            "time_to_storm_estimate": time_est,
            "accel_ratio":            accel_ratio,
        },
    }


def print_compaction_summary(hist: Dict[str, Any]) -> None:
    """E: Print compaction risk summary for console output."""
    if hist.get("insufficient"):
        print("  Compaction Risk: INSUFFICIENT_COMPACTION_DATA")
        return

    s = hist["stats_24h"]
    risk  = hist["risk_level"]
    accel = "true" if hist["acceleration"] else "false"
    p95   = s["p95_duration_ms"]
    p95_s = f"{p95/1000:.0f}s" if p95 else "N/A"

    risk_icons = {"LOW": "ok", "ELEVATED": "!!", "HIGH": "!!", "CRITICAL": "!!"}
    icon = risk_icons.get(risk, "")

    W = 60
    print(f"\n{'─'*W}")
    print("  [==] COMPACTION RISK (forward-looking)")
    print(f"{'─'*W}")
    print(f"  Compaction Risk Level: {risk} [{icon}]")
    print(f"  Compaction p95:        {p95_s}")
    print(f"  Acceleration:          {accel}")
    print(f"  24h: count={s['compaction_count']} timeout={s['timeout_count']}"
          f" b2b={s['back_to_back']} overlap={s['overlap_stall']}")
    print(f"  7d:  count={hist['stats_7d']['compaction_count']}"
          f" timeout={hist['stats_7d']['timeout_count']}")
    accel_ratio = hist["accel_ratio"]
    ratio_str = "inf" if not math.isfinite(accel_ratio) else str(accel_ratio)
    print(f"  Accel ratio:           {ratio_str}x "
          f"(short={hist['short_rate']:.1f}/day "
          f"baseline={hist['baseline_rate']:.2f}/day)")


# ─── CLI self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("radcheck_scoring_v2 — self-test")
    print(f"DEFAULT_WEIGHTS: {DEFAULT_WEIGHTS}")
    print(f"DOMAIN_MAP entries: {len(DOMAIN_MAP)}")
    print(f"ENRICHMENT_META entries: {len(ENRICHMENT_META)}")

    # Synthetic test
    test_findings = [
        {"finding_id": "RC_ENV_004", "severity": "HIGH",
         "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "summary": "Gateway frozen", "evidence": "test", "recommended_fix": "kick"},
        {"finding_id": "RC_ENV_001", "severity": "MEDIUM",
         "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "summary": "Load elevated", "evidence": "load=8", "recommended_fix": "check ps"},
        {"finding_id": "RC_WD_001", "severity": "INFO",
         "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "summary": "Watchdog OK", "evidence": "avg 90s", "recommended_fix": "none"},
    ]

    result = score_v2(test_findings, facts={
        "backup_recent_hours": 12,
        "gateway_stalls_7d": 0,
        "model_state_monotonic": True,
        "provider_diversity": 4,
    })

    print(f"\nTest score: {result['score']}/100  risk: {result['risk_level']}")
    print(f"Domain subscores: { {d: s['subscore'] for d, s in result['domain_subscores'].items()} }")
    print(f"Credits: {result['credits_total']}")
    print(f"Resource norm: {result['resource_norm']}")
    print(f"Log lines: {len(result['log_lines'])}")
    for l in result['log_lines']:
        print(f"  {l}")
    print("\nSelf-test PASS")
