#!/usr/bin/env python3
"""
Agent911 v0.1 — Unified Reliability Scoreboard (State Semantics v0.3)
TASK_ID: A-A9-V0-003
OWNER: GP-OPS

Read-only aggregator. Safe, deterministic, <250ms runtime (target <50ms).

Outputs:
  ~/.openclaw/watchdog/agent911_state.json      (machine-readable snapshot)
  ~/.openclaw/watchdog/agent911_dashboard.md    (human-readable dashboard)

Safety guarantees:
  - Zero writes to openclaw.json
  - Zero subprocess calls except bounded git fetch (timeout=2s, gated by FETCH_HEAD age)
  - Zero service restarts
  - All reads gracefully tolerate missing files
  - Writes only to agent911_state.json and agent911_dashboard.md
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# FindMyAgent classifier (A-FMA-V1-001) — optional import; graceful on missing
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
try:
    from findmyagent_classifier import classify_agents as _fma_classify
    _FMA_AVAILABLE = True
except ImportError:
    _FMA_AVAILABLE = False

# Sentinel Attach Bridge (A-SEN-P4-001) — optional import; graceful on missing
_SEN_BRIDGE_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "sentinel"))
if _SEN_BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _SEN_BRIDGE_DIR)
try:
    from sentinel_attach_bridge import (
        compute_sentinel_recommendation as _compute_sen_rec,
        emit_recommendation_event as _emit_sen_rec,
        render_sentinel_readiness_block as _render_sen_readiness,
    )
    _SEN_BRIDGE_AVAILABLE = True
except ImportError:
    _SEN_BRIDGE_AVAILABLE = False

# Sentinel Funnel Alignment (A-SEN-P4-002) — optional import; graceful on missing
try:
    from sentinel_funnel_alignment import (
        compute_alignment as _compute_alignment,
        emit_alignment_event as _emit_alignment,
        render_alignment_block as _render_alignment_block,
        DRIFT as _ALIGN_DRIFT,
    )
    _ALIGNMENT_AVAILABLE = True
except ImportError:
    _ALIGNMENT_AVAILABLE = False

# Funnel Telemetry (A-FUN-P1-001) — optional import; graceful on missing
_FUNNEL_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "funnel"))
if _FUNNEL_DIR not in sys.path:
    sys.path.insert(0, _FUNNEL_DIR)
try:
    from funnel_events import (
        compute_funnel_signals as _compute_funnel,
        emit_funnel_events as _emit_funnel,
        render_funnel_block as _render_funnel_block,
        compute_weekly_rollup as _compute_weekly_rollup,
        write_weekly_json as _write_weekly_json,
        emit_weekly_rollup_event as _emit_weekly_rollup,
        render_gtm_funnel_block as _render_gtm_funnel_block,
        generate_weekly_report as _generate_weekly_report,
    )
    _FUNNEL_AVAILABLE = True
except ImportError:
    _FUNNEL_AVAILABLE = False

# GTM Funnel Export (A-FUN-P4-001) — optional import; graceful on missing
_GTM_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "gtm"))
if _GTM_DIR not in sys.path:
    sys.path.insert(0, _GTM_DIR)
try:
    from gtm_funnel_export import (
        run_export as _run_gtm_export,
        read_export_status as _read_export_status,
    )
    _GTM_EXPORT_AVAILABLE = True
except ImportError:
    _GTM_EXPORT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME       = os.path.expanduser("~")
WATCHDOG   = os.path.join(HOME, ".openclaw", "watchdog")
REPO       = os.path.join(HOME, ".openclaw", "workspace")
OUT_STATE  = os.path.join(WATCHDOG, "agent911_state.json")
OUT_DASH   = os.path.join(WATCHDOG, "agent911_dashboard.md")

SRC_RADCHECK    = os.path.join(WATCHDOG, "radcheck_history.ndjson")
SRC_FINDINGS    = os.path.join(WATCHDOG, "radiation_findings.log")
SRC_OPS         = os.path.join(WATCHDOG, "ops_events.log")
SRC_BACKUP      = os.path.join(WATCHDOG, "backup.log")
SRC_MODEL       = os.path.join(WATCHDOG, "model_state.json")
SRC_COMP_ALERT  = os.path.join(WATCHDOG, "compaction_alert_state.json")
SRC_HEARTBEAT   = os.path.join(WATCHDOG, "heartbeat.log")
SRC_MODEL_RTR   = os.path.join(WATCHDOG, "model_router.py")
SRC_TOKENS_LOG  = os.path.join(HOME, ".openclaw", "metrics", "tokens.log")
FETCH_HEAD      = os.path.join(REPO, ".git", "FETCH_HEAD")
FETCH_MAX_AGE_S = 60    # skip git fetch if FETCH_HEAD is fresher than this
FETCH_TIMEOUT_S = 2     # hard cap on git fetch subprocess

# A-SEN-P4-001 — Predictive guard state
SRC_PRED_STATE = os.path.join(WATCHDOG, "sentinel_predictive_state.json")

# A-A9-PERF-001 — Performance guardrail paths + thresholds
PERF_HISTORY    = os.path.join(WATCHDOG, "agent911_history.ndjson")
PERF_STATE      = os.path.join(WATCHDOG, "agent911_perf_state.json")
PERF_THRESHOLDS = {
    "snapshot_ms":      150,
    "dashboard_ms":     250,
    "ops_events_bytes": 10_485_760,   # 10 MB
}
PERF_COOLDOWN_H = 6   # hours between repeated breach events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_iso() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

def safe_read_lines(path: str, tail: int = 500) -> list:
    try:
        with open(path, "r", errors="replace") as f:
            return f.readlines()[-tail:]
    except Exception:
        return []

def safe_json_load(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def safe_ndjson_last(path: str, n: int = 20) -> list:
    results = []
    for line in safe_read_lines(path, tail=n * 3):
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except Exception:
            pass
    return results[-n:]

def age_epoch_hours(epoch) -> float:
    try:
        return (now_utc().timestamp() - float(epoch)) / 3600
    except Exception:
        return -1.0

def bool_fmt(val, true_str="YES", false_str="NO") -> str:
    if val is True:
        return true_str
    if val is False:
        return false_str
    return str(val)


# ---------------------------------------------------------------------------
# A — Repo Sync (hardened: live git, FETCH_HEAD-gated)
# ---------------------------------------------------------------------------
def _fetch_head_age_s() -> float:
    """Return seconds since last git fetch. Large value if FETCH_HEAD missing."""
    try:
        mtime = os.path.getmtime(FETCH_HEAD)
        return time.time() - mtime
    except Exception:
        return 9999.0

def gather_repo_sync() -> dict:
    """
    Compute accurate repo divergence via git rev-list.
    Gates git fetch behind FETCH_HEAD age to stay within performance budget.
    Returns: repo_in_sync, repo_ahead, repo_behind, repo_status_label, fetch_status
    """
    fetch_status = "skipped"
    fetch_age = _fetch_head_age_s()

    if fetch_age >= FETCH_MAX_AGE_S:
        try:
            result = subprocess.run(
                ["git", "-C", REPO, "fetch", "--quiet"],
                timeout=FETCH_TIMEOUT_S,
                capture_output=True,
            )
            fetch_status = "ok" if result.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            fetch_status = "timeout"
        except Exception:
            fetch_status = "error"

    # Check upstream exists
    try:
        up_result = subprocess.run(
            ["git", "-C", REPO, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            timeout=2, capture_output=True, text=True
        )
        if up_result.returncode != 0 or not up_result.stdout.strip():
            return {
                "repo_in_sync": "unknown",
                "repo_ahead_commits": "unknown",
                "repo_behind_commits": "unknown",
                "repo_status_label": "UNKNOWN — no upstream",
                "fetch_status": fetch_status,
            }
        upstream = up_result.stdout.strip()
    except Exception:
        return {
            "repo_in_sync": "unknown",
            "repo_ahead_commits": "unknown",
            "repo_behind_commits": "unknown",
            "repo_status_label": "UNKNOWN — git unavailable",
            "fetch_status": fetch_status,
        }

    # Compute ahead/behind
    try:
        lr_result = subprocess.run(
            ["git", "-C", REPO, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
            timeout=2, capture_output=True, text=True
        )
        if lr_result.returncode != 0:
            raise RuntimeError("rev-list failed")
        parts = lr_result.stdout.strip().split()
        ahead  = int(parts[0])
        behind = int(parts[1])
    except Exception:
        return {
            "repo_in_sync": "unknown",
            "repo_ahead_commits": "unknown",
            "repo_behind_commits": "unknown",
            "repo_status_label": "UNKNOWN — divergence unresolvable",
            "fetch_status": fetch_status,
        }

    if fetch_status == "timeout":
        label   = "UNKNOWN_FETCH_TIMEOUT"
        in_sync = "unknown"
    elif ahead > 0 and behind > 0:
        label   = f"DIVERGED (ahead={ahead} behind={behind}) — reconcile recommended"
        in_sync = False
    elif ahead > 0:
        label   = f"AHEAD {ahead} — push recommended"
        in_sync = False
    elif behind > 0:
        label   = f"BEHIND {behind} — pull recommended"
        in_sync = False
    else:
        label   = "IN SYNC"
        in_sync = True

    return {
        "repo_in_sync":          in_sync,
        "repo_ahead_commits":    ahead,
        "repo_behind_commits":   behind,
        "repo_status_label":     label,
        "fetch_status":          fetch_status,
    }


# ---------------------------------------------------------------------------
# B — SphinxGate State (evidence-based tri-state, A-A9-V0-003)
# ---------------------------------------------------------------------------
def _parse_tokens_log_last(path: str, tail_lines: int = 200) -> dict:
    """
    Parse last routing decision from tokens.log.
    Format: timestamp,req_id,lane,provider,model,tok_in,tok_out,tok_total,status,latency_ms,cost
    Cap: last 200 lines only (performance budget).
    Returns: {ts_str, lane, provider, age_hours} or {} on any error.
    """
    lines = safe_read_lines(path, tail=tail_lines)
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        ts_str = parts[0].strip()
        lane   = parts[2].strip() if len(parts) > 2 else "unknown"
        provider = parts[3].strip() if len(parts) > 3 else "unknown"
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            age_h = (datetime.now() - dt).total_seconds() / 3600
            return {"ts_str": ts_str, "lane": lane, "provider": provider,
                    "age_hours": round(age_h, 2)}
        except Exception:
            continue
    return {}

def gather_sphinxgate_state() -> dict:
    """
    Evidence-based SphinxGate tri-state (A-A9-V0-003).
    Installation proof: model_router.py exists.
    Activity evidence: tokens.log last decision timestamp.
    Evidence source: ~/.openclaw/metrics/tokens.log (CSV, one line per routed call).

    Returns: state, reason, last_decision_ts, last_decision_age_hours, evidence_source
    """
    router_present = os.path.isfile(SRC_MODEL_RTR)
    if not router_present:
        return {
            "state": "UNKNOWN", "reason": "model_router.py not found",
            "last_decision_ts": None, "last_decision_age_hours": None,
            "evidence_source": "none",
        }

    # Attempt to read routing evidence from tokens.log
    tokens_exists = os.path.isfile(SRC_TOKENS_LOG)
    if not tokens_exists:
        return {
            "state": "UNKNOWN",
            "reason": "tokens.log not found — no routing evidence",
            "last_decision_ts": None, "last_decision_age_hours": None,
            "evidence_source": "none",
        }

    last = _parse_tokens_log_last(SRC_TOKENS_LOG)
    if not last:
        return {
            "state": "IDLE",
            "reason": "installed, tokens.log unreadable or empty",
            "last_decision_ts": None, "last_decision_age_hours": None,
            "evidence_source": "tokens.log",
        }

    age_h  = last["age_hours"]
    ts_str = last["ts_str"]

    if age_h < 1.0:
        mins = int(age_h * 60)
        reason = f"decision {mins}m ago (lane={last['lane']} provider={last['provider']})"
        state  = "ACTIVE"
    elif age_h < 24.0:
        reason = f"no decisions in last {age_h:.1f}h (last: {ts_str})"
        state  = "IDLE"
    else:
        reason = f"no decisions in last 24h (last: {ts_str})"
        state  = "IDLE"

    return {
        "state":                    state,
        "reason":                   reason,
        "last_decision_ts":         ts_str,
        "last_decision_age_hours":  age_h,
        "evidence_source":          "tokens.log",
    }


# ---------------------------------------------------------------------------
# C — Compaction State (sentinel primary, radcheck fallback)
# ---------------------------------------------------------------------------
COMP_RISK_MAP = {
    "NOMINAL": "LOW",
    "SUSPECT": "MEDIUM",
    "ACTIVE":  "HIGH",
    "STORM":   "HIGH",
}

def gather_compaction_state() -> dict:
    """
    Sentinel is primary truth. Radcheck fallback only if sentinel missing.
    Returns unified compaction block with source field.
    """
    sentinel = safe_json_load(SRC_COMP_ALERT)
    if sentinel and "alert_level" in sentinel:
        alert_level = sentinel.get("alert_level", "UNKNOWN")
        risk = COMP_RISK_MAP.get(alert_level, "UNKNOWN")

        # Pull richer metrics from radcheck if available (non-primary, supplement only)
        p95_ms = "unknown"
        accel  = None
        radcheck_entries = safe_ndjson_last(SRC_RADCHECK, n=3)
        for entry in reversed(radcheck_entries):
            comp = entry.get("domains", {}).get("compaction_risk", {})
            if comp:
                p95_ms = comp.get("p95_duration_ms", "unknown")
                accel  = comp.get("acceleration", None)
                break

        return {
            "state":            alert_level,
            "risk":             risk,
            "p95_ms":           p95_ms,
            "acceleration":     accel,
            "events_2h":        sentinel.get("comp_events_2h", "unknown"),
            "timeout_2h":       sentinel.get("timeout_2h", "unknown"),
            "source":           "sentinel",
        }

    # Fallback: radcheck
    radcheck_entries = safe_ndjson_last(SRC_RADCHECK, n=3)
    for entry in reversed(radcheck_entries):
        comp = entry.get("domains", {}).get("compaction_risk", {})
        if comp:
            risk_level = comp.get("risk_level", "UNKNOWN")
            # Map radcheck risk_level to sentinel-style state
            state_map  = {"HIGH": "ACTIVE", "MEDIUM": "SUSPECT", "LOW": "NOMINAL"}
            state      = state_map.get(risk_level, "UNKNOWN")
            return {
                "state":        state,
                "risk":         risk_level,
                "p95_ms":       comp.get("p95_duration_ms", "unknown"),
                "acceleration": comp.get("acceleration", None),
                "events_2h":    comp.get("compaction_count_24h", "unknown"),
                "timeout_2h":   comp.get("timeout_count_24h", "unknown"),
                "source":       "radcheck",
            }

    return {
        "state": "UNKNOWN", "risk": "UNKNOWN", "p95_ms": "unknown",
        "acceleration": None, "events_2h": "unknown", "timeout_2h": "unknown",
        "source": "unknown",
    }


# ---------------------------------------------------------------------------
# D — Radcheck / Stability
# ---------------------------------------------------------------------------
def gather_radcheck() -> dict:
    entries = safe_ndjson_last(SRC_RADCHECK, n=5)
    if not entries:
        return {"score": "unknown", "risk_level": "unknown",
                "last_scan_ts": "unknown", "velocity_direction": "unknown",
                "velocity_delta": "unknown"}
    latest = entries[-1]
    return {
        "score":              latest.get("score", "unknown"),
        "risk_level":         latest.get("risk_level", "unknown"),
        "last_scan_ts":       latest.get("ts", "unknown"),
        "velocity_direction": latest.get("velocity_direction", "unknown"),
        "velocity_delta":     latest.get("velocity_delta", "unknown"),
    }


# ---------------------------------------------------------------------------
# E — Top Risks
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

def gather_top_risks(max_risks: int = 3) -> list:
    lines = safe_read_lines(SRC_FINDINGS, tail=300)
    seen_ids = set()
    findings = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        fid = obj.get("finding_id", "")
        sev = obj.get("severity", "INFO")
        if fid and fid not in seen_ids and sev in ("CRITICAL", "HIGH"):
            seen_ids.add(fid)
            findings.append({
                "id":       fid,
                "severity": sev,
                "summary":  obj.get("summary", obj.get("title", ""))[:100],
                "domain":   obj.get("domain", "unknown"),
            })
        if len(findings) >= max_risks:
            break
    findings.sort(key=lambda x: SEVERITY_ORDER.get(x["severity"], 9))
    return findings[:max_risks]


# ---------------------------------------------------------------------------
# F — Protection State
# ---------------------------------------------------------------------------
def _watchdog_alive() -> str:
    lines = safe_read_lines(os.path.join(WATCHDOG, "heartbeat.log"), tail=10)
    if not lines:
        return "unknown"
    last = lines[-1].strip()
    m = re.search(r"HB (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", last)
    if not m:
        return "unknown"
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        age_s = (datetime.now() - dt).total_seconds()
        if 0 <= age_s < 900:
            return "ACTIVE"
        elif age_s < 3600:
            return "STALE"
        else:
            return "DOWN"
    except Exception:
        return "unknown"

def _sentinel_alive() -> str:
    state = safe_json_load(SRC_COMP_ALERT)
    if state and "alert_level" in state:
        return "ACTIVE"
    return "unknown"


# ---------------------------------------------------------------------------
# H — Protection Events (A-SEN-P1-001)
# ---------------------------------------------------------------------------
_PROTECTION_PREFIX  = "SENTINEL_PROTECTION_"
_GUARD_CYCLE_EVENT  = "SENTINEL_GUARD_CYCLE"  # A-SEN-P3-001: emitter heartbeat

def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp; handles 'Z' suffix (Python 3.9 compat)."""
    ts_str = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gather_protection_events_24h() -> dict:
    """
    Read ops_events.log for SENTINEL_PROTECTION_* events in the last 24h.
    Tolerates missing file gracefully.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    events = []
    try:
        if not os.path.exists(SRC_OPS):
            return {"count": 0, "top_event": "none", "last_event_ts": "unknown"}
        with open(SRC_OPS) as f:
            for raw in f:
                raw = raw.strip()
                if not raw or _PROTECTION_PREFIX not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                    if not str(rec.get("event", "")).startswith(_PROTECTION_PREFIX):
                        continue
                    ts_str = rec.get("ts", "")
                    dt = _parse_ts(ts_str)
                    if dt >= cutoff:
                        events.append(rec)
                except Exception:
                    continue
    except Exception:
        pass

    if not events:
        return {"count": 0, "top_event": "none", "last_event_ts": "unknown"}

    # Top event = most frequently occurring type
    from collections import Counter
    type_counts = Counter(e.get("event", "") for e in events)
    top_event = type_counts.most_common(1)[0][0] if type_counts else "none"
    last_event_ts = events[-1].get("ts", "unknown")

    return {
        "count":         len(events),
        "top_event":     top_event,
        "last_event_ts": last_event_ts,
    }


def gather_protection_rollups() -> dict:
    """
    Single-pass rollup of protection events from ops_events.log.

    Computes:
      - 24h/7d SENTINEL_PROTECTION_* event counts + severity histogram
      - guard_cycles_24h: SENTINEL_GUARD_CYCLE count in 24h (emitter heartbeat)
      - cooldown_suppressions_24h: sum of suppressed_count from guard cycles (24h)
      - posture: ACTIVE_GUARDING | MONITORING | QUIET
      - last event + last 3 events (most recent first)

    Constraints:
      - Single pass through ops_events.log
      - Pure stdlib, no external deps
      - Tolerates missing file gracefully (returns zero counts)

    TASK_ID: A-SEN-P2-001 / A-SEN-P3-001
    """
    from datetime import timedelta

    now_utc = datetime.now(timezone.utc)
    cut_24h = now_utc - timedelta(hours=24)
    cut_7d  = now_utc - timedelta(days=7)

    EMPTY = {
        "events_24h":                0,
        "events_7d":                 0,
        "by_severity":               {"INFO": 0, "MEDIUM": 0, "HIGH": 0},
        "last_event_type":           "none",
        "last_event_ts":             "unknown",
        "last_three_events":         [],
        "guard_cycles_24h":          0,
        "cooldown_suppressions_24h": 0,
        "posture":                   "QUIET",
    }

    if not os.path.exists(SRC_OPS):
        return EMPTY

    # ── Single forward pass — captures both event families ────────────────
    prot_events  = []   # (dt, rec) for SENTINEL_PROTECTION_* in 7d
    guard_events = []   # (dt, rec) for SENTINEL_GUARD_CYCLE in 7d
    try:
        with open(SRC_OPS) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                if _PROTECTION_PREFIX not in raw and _GUARD_CYCLE_EVENT not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                    evt = str(rec.get("event", ""))
                    dt  = _parse_ts(rec.get("ts", ""))
                    if evt.startswith(_PROTECTION_PREFIX) and dt >= cut_7d:
                        prot_events.append((dt, rec))
                    elif evt == _GUARD_CYCLE_EVENT and dt >= cut_7d:
                        guard_events.append((dt, rec))
                except Exception:
                    continue
    except Exception:
        return EMPTY

    if not prot_events and not guard_events:
        return EMPTY

    # ── Protection event metrics ──────────────────────────────────────────
    count_24h = 0
    count_7d  = len(prot_events)
    sev_hist  = {"INFO": 0, "MEDIUM": 0, "HIGH": 0}

    for dt, rec in prot_events:
        sev = rec.get("severity", "INFO")
        sev_hist[sev] = sev_hist.get(sev, 0) + 1
        if dt >= cut_24h:
            count_24h += 1

    last_event_type = "none"
    last_event_ts   = "unknown"
    last_three: list = []
    if prot_events:
        sorted_evts    = sorted(prot_events, key=lambda x: x[0], reverse=True)
        last_rec       = sorted_evts[0][1]
        last_event_type = last_rec.get("event", "unknown")
        last_event_ts   = last_rec.get("ts", "unknown")
        last_three = [
            {
                "event":    e.get("event", "unknown").replace(_PROTECTION_PREFIX, ""),
                "ts":       e.get("ts", "unknown"),
                "severity": e.get("severity", "INFO"),
            }
            for _, e in sorted_evts[:3]
        ]

    # ── Guard cycle metrics (A-SEN-P3-001) ───────────────────────────────
    guard_cycles_24h          = sum(1 for dt, _ in guard_events if dt >= cut_24h)
    cooldown_suppressions_24h = sum(
        int(r.get("suppressed_count", 0))
        for dt, r in guard_events
        if dt >= cut_24h
    )

    # Posture: based on 7d interventions + 24h guard cycles
    if count_7d > 0:
        posture = "ACTIVE_GUARDING"
    elif guard_cycles_24h > 0:
        posture = "MONITORING"
    else:
        posture = "QUIET"

    return {
        "events_24h":                count_24h,
        "events_7d":                 count_7d,
        "by_severity":               sev_hist,
        "last_event_type":           last_event_type,
        "last_event_ts":             last_event_ts,
        "last_three_events":         last_three,
        "guard_cycles_24h":          guard_cycles_24h,
        "cooldown_suppressions_24h": cooldown_suppressions_24h,
        "posture":                   posture,
    }


# ---------------------------------------------------------------------------
# K — Routing Confidence Block (A-SG-P1-001)
# ---------------------------------------------------------------------------

def gather_routing_confidence() -> dict:
    """
    Build routing confidence block from available telemetry.
    Sources (best-effort): tokens.log, ops_events.log, model_state.json.

    Returns:
        confidence:            HIGH | DEGRADED | IDLE | UNKNOWN
        provider_switches_24h: int
        anomalies_24h:         int
        last_provider:         str
        last_route_age_minutes: float | None

    TASK_ID: A-SG-P1-001
    """
    from datetime import timedelta

    EMPTY_ROUTING = {
        "confidence":            "UNKNOWN",
        "provider_switches_24h": 0,
        "anomalies_24h":         0,
        "last_provider":         "unknown",
        "last_route_age_minutes": None,
    }

    # 1. Last routing decision from tokens.log
    tokens_data: dict = {}
    if os.path.isfile(SRC_TOKENS_LOG):
        tokens_data = _parse_tokens_log_last(SRC_TOKENS_LOG) or {}

    if not tokens_data:
        return EMPTY_ROUTING

    last_provider         = tokens_data.get("provider", "unknown")
    age_h                 = tokens_data.get("age_hours")
    last_route_age_minutes = round(float(age_h) * 60, 1) if age_h is not None else None

    # 2. Anomalies from ops_events.log (last 24h)
    anomalies_24h = 0
    cut_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        if os.path.exists(SRC_OPS):
            for raw in safe_read_lines(SRC_OPS, tail=1000):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    evt = str(rec.get("event", ""))
                    if "ROUTING_ANOMALY" not in evt and "POLICY_FAIL" not in evt:
                        continue
                    dt = _parse_ts(rec.get("ts", ""))
                    if dt >= cut_24h:
                        anomalies_24h += 1
                except Exception:
                    continue
    except Exception:
        pass

    # 3. Provider switches from tokens.log (last 24h)
    provider_switches_24h = 0
    try:
        if os.path.isfile(SRC_TOKENS_LOG):
            lines = safe_read_lines(SRC_TOKENS_LOG, tail=500)
            prev_prov: str | None = None
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) < 4:
                    continue
                try:
                    ts_str   = parts[0].strip()
                    provider = parts[3].strip()
                    dt_naive = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    age_h_chk = (datetime.now() - dt_naive).total_seconds() / 3600
                    if age_h_chk > 24:
                        continue
                    if prev_prov is not None and provider != prev_prov:
                        provider_switches_24h += 1
                    prev_prov = provider
                except Exception:
                    continue
    except Exception:
        pass

    # 4. Confidence classification
    if age_h is None or (isinstance(age_h, float) and age_h > 48):
        confidence = "UNKNOWN"
    elif isinstance(age_h, float) and age_h > 24:
        confidence = "IDLE"
    elif anomalies_24h > 0:
        confidence = "DEGRADED"
    else:
        confidence = "HIGH"

    return {
        "confidence":            confidence,
        "provider_switches_24h": provider_switches_24h,
        "anomalies_24h":         anomalies_24h,
        "last_provider":         last_provider,
        "last_route_age_minutes": last_route_age_minutes,
    }


def _render_routing_confidence(routing: dict) -> list:
    """Return dashboard lines for the ROUTING CONFIDENCE block."""
    conf     = routing.get("confidence",            "UNKNOWN")
    switches = routing.get("provider_switches_24h", 0)
    anomalies = routing.get("anomalies_24h",        0)
    last_prov = routing.get("last_provider",        "unknown")
    age_min   = routing.get("last_route_age_minutes")

    age_str = f"{int(age_min)}m ago" if age_min is not None else "unknown"

    return [
        f"  Confidence:       {conf}",
        f"  Last provider:    {last_prov} ({age_str})",
        f"  Switches (24h):   {switches}",
        f"  Anomalies (24h):  {anomalies}",
    ]


# ---------------------------------------------------------------------------
# M — Predictive Guard integration (A-SEN-P4-001)
# ---------------------------------------------------------------------------

# Signal weights mirror sentinel_predictive_guard.py (kept in sync)
_PRED_WEIGHTS: dict = {
    "comp": 0.30, "watchdog_latency": 0.20, "gateway": 0.25,
    "model": 0.10, "resource": 0.15,
}
_PRED_DRIVER_LABELS: dict = {
    "comp":             "COMP_SIGNAL",
    "watchdog_latency": "WATCHDOG_LATENCY_SIGNAL",
    "gateway":          "GATEWAY_STALL_SIGNAL",
    "model":            "MODEL_INSTABILITY_SIGNAL",
    "resource":         "RESOURCE_PRESSURE_SIGNAL",
}


def _pred_top_drivers(signals: dict, n: int = 2) -> list:
    """Return top N driver labels by weighted contribution (descending, non-zero)."""
    contribs = {k: signals.get(k, 0.0) * _PRED_WEIGHTS.get(k, 0.0) for k in _PRED_WEIGHTS}
    sorted_keys = sorted(contribs, key=lambda x: -contribs[x])
    return [
        _PRED_DRIVER_LABELS.get(k, k.upper())
        for k in sorted_keys[:n]
        if contribs[k] > 0.0
    ]


def gather_predictive_guard() -> dict:
    """
    Read sentinel_predictive_state.json and recent ops_events.log to build
    the predictive guard block for Agent911.

    Returns: risk_score, risk_level, top_drivers, last_emit_ts, last_run_ts.
    TASK_ID: A-SEN-P4-001 / SCOPE E
    """
    EMPTY = {
        "risk_score":            "unknown",
        "risk_level":            "unknown",
        "predictive_confidence": "unknown",
        "top_drivers":           [],
        "reason_codes":          [],
        "last_emit_ts":          "none",
        "last_run_ts":           "unknown",
    }
    state = safe_json_load(SRC_PRED_STATE)
    if not state or "last_risk_level" not in state:
        return EMPTY

    risk_level = state.get("last_risk_level", "unknown")
    risk_score = state.get("last_risk_score", "unknown")
    last_run   = state.get("last_run_ts",    "unknown")
    signals    = state.get("signals",         {})

    top_drivers = _pred_top_drivers(signals) if signals else []

    # Last emit from ops_events.log (bounded tail scan)
    # Accepts both old SENTINEL_PREEMPTIVE_GUARD and new SENTINEL_PREDICTIVE_RISK
    last_emit_ts = "none"
    for raw in reversed(safe_read_lines(SRC_OPS, tail=200)):
        raw = raw.strip()
        if "SENTINEL_PREDICTIVE_RISK" not in raw and "SENTINEL_PREEMPTIVE_GUARD" not in raw:
            continue
        try:
            rec = json.loads(raw)
            if rec.get("event") in ("SENTINEL_PREDICTIVE_RISK", "SENTINEL_PREEMPTIVE_GUARD"):
                last_emit_ts = rec.get("ts", "unknown")
                break
        except Exception:
            continue

    # Deterministic confidence: mirrors sentinel_predictive_guard._compute_predictive_confidence
    def _confidence(level: str, n: int) -> int:
        if level == "HIGH":   return 90 if n >= 2 else 75
        if level in ("MED", "MEDIUM"): return 74 if n >= 2 else 50
        if level == "LOW":    return 49 if n >= 2 else 25
        return 20

    confidence = _confidence(risk_level, len(top_drivers))

    return {
        "risk_score":            risk_score,
        "risk_level":            risk_level,
        "predictive_confidence": confidence,
        "top_drivers":           top_drivers,
        "reason_codes":          top_drivers,   # alias of top_drivers
        "last_emit_ts":          last_emit_ts,
        "last_run_ts":           last_run,
    }


def _render_predictive_guard(pg: dict) -> list:
    """Return dashboard lines for the PREDICTIVE GUARD block."""
    score   = pg.get("risk_score",   "unknown")
    level   = pg.get("risk_level",   "unknown")
    drivers = pg.get("top_drivers",  [])
    emit_ts = pg.get("last_emit_ts", "none")
    run_ts  = pg.get("last_run_ts",  "unknown")

    score_str  = f"{level} ({score}/100)" if score != "unknown" else "unknown"
    driver_str = ", ".join(drivers) if drivers else "none"
    conf       = pg.get("predictive_confidence", "unknown")
    conf_str   = f"{conf}/100" if isinstance(conf, int) else str(conf)

    lines = [
        f"  Risk:                  {score_str}",
        f"  Confidence:            {conf_str}",
        f"  Top drivers:           {driver_str}",
        f"  Last guard emit:       {emit_ts}",
        f"  Last guard run:        {run_ts}",
    ]
    if level == "HIGH":
        lines.append("  [!] HIGH stall risk — review recommended actions")
    elif level == "MED":
        lines.append("  [~] Moderate stall risk — monitoring elevated")
    return lines


# ---------------------------------------------------------------------------
# N — Sentinel Recommendation (A-SEN-P4-001 — Sentinel Attach Bridge)
# ---------------------------------------------------------------------------

def gather_sentinel_recommendation(snap_partial: dict) -> dict:
    """
    Compute the Sentinel recommendation signal from current telemetry.
    Emits SENTINEL_RECOMMENDATION_EVAL to ops_events.log (append-only).
    Graceful fallback when sentinel_attach_bridge module is unavailable.

    TASK_ID: A-SEN-P4-001 / Sentinel Attach Bridge
    """
    _EMPTY = {
        "recommended": False,
        "confidence":  0,
        "severity":    "LOW",
        "reasons":     [],
        "ts":          ts_iso(),
    }
    if not _SEN_BRIDGE_AVAILABLE:
        return _EMPTY
    try:
        rec = _compute_sen_rec(snap_partial)
        # Emit NDJSON event every run (no cooldown — observational signal only)
        _emit_sen_rec(rec, SRC_OPS)
        return rec
    except Exception:
        return _EMPTY


def _render_sentinel_readiness(rec: dict) -> list:
    """Return dashboard lines for the SENTINEL READINESS block."""
    if not _SEN_BRIDGE_AVAILABLE:
        return ["  Sentinel Attach Bridge not available."]
    try:
        return _render_sen_readiness(rec)
    except Exception:
        return ["  (render error)"]


# ---------------------------------------------------------------------------
# P — Sentinel ↔ Funnel Alignment (A-SEN-P4-002)
# ---------------------------------------------------------------------------

def gather_sentinel_alignment(
    sentinel_rec: dict,
    weekly_rollup: dict,
) -> dict:
    """
    Compute sentinel/funnel alignment using current-run data directly
    (no disk re-read required — data already in memory).
    Emits SENTINEL_FUNNEL_ALIGNMENT to ops_events.log.

    Wraps compute_alignment from sentinel_funnel_alignment module.
    Graceful fallback when module unavailable.

    TASK_ID: A-SEN-P4-002
    """
    _DRIFT_RESULT = {
        "alignment_state":   "DRIFT",
        "recommended":       None,
        "enabled_present":   None,
        "confidence":        0,
        "ts":                ts_iso(),
        "notes":             "Alignment module unavailable.",
    }
    if not _ALIGNMENT_AVAILABLE:
        return _DRIFT_RESULT
    try:
        # Build minimal dicts matching what compute_alignment expects
        a9_state_partial = {"sentinel_recommendation": sentinel_rec}
        alignment = _compute_alignment(a9_state_partial, weekly_rollup)
        _emit_alignment(alignment, SRC_OPS)
        return alignment
    except Exception:
        return _DRIFT_RESULT


def _render_alignment(alignment: dict) -> list:
    """Return dashboard lines for the SENTINEL ↔ FUNNEL ALIGNMENT block."""
    if not _ALIGNMENT_AVAILABLE:
        return ["  Alignment module not available."]
    try:
        return _render_alignment_block(alignment)
    except Exception:
        return ["  (render error)"]


# ---------------------------------------------------------------------------
# O — Funnel Telemetry (A-FUN-P1-001)
# ---------------------------------------------------------------------------

def gather_funnel_signals() -> tuple:
    """
    Compute funnel signal counts + weekly rollup, emit NDJSON events.
    Graceful fallback when funnel_events module is unavailable.
    Returns (funnel_signals, weekly_rollup) tuple.

    TASK_ID: A-FUN-P1-001 / A-FUN-P2-001
    """
    _EMPTY_SIG = {
        "rc_runs_24h":           "unknown",
        "sen_recommended_24h":   "unknown",
        "sen_enabled":           False,
        "a9_viewed_24h":         "unknown",
        "a9_expanded_7d":        "unknown",
        "a9_expanded_source":    "unavailable",
    }
    _EMPTY_ROLLUP = {
        "window_days":             7,
        "radcheck_runs_7d":        0,
        "sentinel_recommended_7d": 0,
        "sentinel_enabled_present": False,
        "agent911_views_7d":       0,
        "agent911_expansions_7d":  0,
        "sentinel_attach_rate":    0.0,
        "agent911_expansion_rate": 0.0,
        "ts":                      ts_iso(),
    }
    if not _FUNNEL_AVAILABLE:
        return _EMPTY_SIG, _EMPTY_ROLLUP
    try:
        import socket as _socket
        _hostname = _socket.gethostname()
        sig       = _compute_funnel()
        _emit_funnel(sig, SRC_OPS)
        rollup    = _compute_weekly_rollup(SRC_OPS)
        _write_weekly_json(rollup)
        _emit_weekly_rollup(rollup, SRC_OPS)
        # A-FUN-P3-001: render + write gtm_funnel_weekly.md + emit NDJSON
        _generate_weekly_report(rollup, hostname=_hostname, ops_path=SRC_OPS)
        # A-FUN-P4-001: GTM export bundle (must run after files are written)
        if _GTM_EXPORT_AVAILABLE:
            try:
                _run_gtm_export(ops_path=SRC_OPS, hostname=_hostname)
            except Exception:
                pass
        return sig, rollup
    except Exception:
        return _EMPTY_SIG, _EMPTY_ROLLUP


def _render_funnel(signals: dict) -> list:
    """Return dashboard lines for the FUNNEL SIGNALS block."""
    if not _FUNNEL_AVAILABLE:
        return ["  Funnel telemetry module not available."]
    try:
        return _render_funnel_block(signals)
    except Exception:
        return ["  (render error)"]


def _render_gtm_funnel(rollup: dict) -> list:
    """Return dashboard lines for the GTM FUNNEL (7D) block."""
    if not _FUNNEL_AVAILABLE:
        return ["  Weekly rollup module not available."]
    try:
        return _render_gtm_funnel_block(rollup)
    except Exception:
        return ["  (render error)"]


def gather_protection_state() -> dict:
    sg = gather_sphinxgate_state()
    return {
        "sentinel":                    _sentinel_alive(),
        "watchdog":                    _watchdog_alive(),
        "sphinxgate_state":            sg["state"],
        "sphinxgate_reason":           sg["reason"],
        "sphinxgate_last_decision_ts": sg.get("last_decision_ts"),
        "sphinxgate_last_decision_age_hours": sg.get("last_decision_age_hours"),
        "sphinxgate_evidence_source":  sg.get("evidence_source", "none"),
    }


# ---------------------------------------------------------------------------
# G — Backup State
# ---------------------------------------------------------------------------
def gather_backup_state() -> dict:
    lines = safe_read_lines(SRC_BACKUP, tail=100)
    last_backup_ts  = "unknown"
    last_backup_age = "unknown"
    restore_ready   = "unknown"

    for line in reversed(lines):
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \w+\]", line)
        if m:
            last_backup_ts = m.group(1)
            try:
                dt = datetime.strptime(last_backup_ts, "%Y-%m-%d %H:%M:%S")
                age_h = (datetime.now() - dt).total_seconds() / 3600
                last_backup_age = round(age_h, 1)
            except Exception:
                pass
            break

    for line in reversed(lines):
        if "RESTORE_DRILL_AGE_HOURS=" in line:
            m = re.search(r"RESTORE_DRILL_AGE_HOURS=(\d+)", line)
            if m:
                restore_ready = int(m.group(1)) < 48
            break

    return {
        "last_backup_ts":        last_backup_ts,
        "last_backup_age_hours": last_backup_age,
        "restore_ready":         restore_ready,
    }


# ---------------------------------------------------------------------------
# H — Model State
# ---------------------------------------------------------------------------
def gather_model_state() -> dict:
    state = safe_json_load(SRC_MODEL)
    if not state:
        return {"last_provider": "unknown", "last_status": "unknown",
                "updated_at": "unknown", "age_hours": "unknown"}
    updated_at = state.get("updated_at")
    age_h  = age_epoch_hours(updated_at) if updated_at else "unknown"
    ts_str = "unknown"
    try:
        ts_str = datetime.fromtimestamp(float(updated_at), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return {
        "last_provider": state.get("provider", "unknown"),
        "last_status":   state.get("status", "unknown"),
        "updated_at":    ts_str,
        "age_hours":     round(age_h, 2) if isinstance(age_h, float) else age_h,
    }


# ---------------------------------------------------------------------------
# FindMyAgent Classification (A-FMA-V1-001)
# ---------------------------------------------------------------------------
_MTL_SNAP = os.path.join(REPO, "openclaw-ops", "ops", "MTL.snapshot.json")
_KNOWN_AGENTS = ["Hendrik"]   # extend as stack grows


def _read_mtl_snap() -> dict:
    try:
        with open(_MTL_SNAP, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def gather_fma_classification(repo_sync: dict) -> dict:
    """
    Run FindMyAgent classifier v1 against current telemetry.
    Reads ops_events.log internally (tail-limited); graceful on missing file.
    Graceful fallback to empty result if classifier module unavailable.
    Returns dict with agent_presence_summary + agents_requiring_attention.
    """
    _empty = {
        "agent_presence_summary":    {"total": 0, "active": 0, "idle": 0,
                                      "blocked": 0, "stalled": 0, "unknown": 0},
        "agents_requiring_attention": [],
        "agents":                    [],
        "source":                    "unavailable",
    }
    if not _FMA_AVAILABLE:
        return _empty
    try:
        ops_rows = []
        if os.path.exists(SRC_OPS):
            for raw in safe_read_lines(SRC_OPS, tail=500):
                raw = raw.strip()
                if raw:
                    try:
                        ops_rows.append(json.loads(raw))
                    except Exception:
                        pass
        mtl_snap = _read_mtl_snap()
        return _fma_classify(
            known_agents=_KNOWN_AGENTS,
            ops_events=ops_rows,
            mtl_snap=mtl_snap,
            repo_sync=repo_sync,
        )
    except Exception:
        return _empty


# ---------------------------------------------------------------------------
# Weekly Operator Report stanza (SCOPE F — A-FMA-P1-001)
# ---------------------------------------------------------------------------
def gather_weekly_report() -> dict:
    """
    Read agent911_weekly_report.md metadata for the weekly_report stanza.
    Graceful on missing or malformed file — returns 'unknown' for all fields.
    Read-only; no writes.
    """
    report_path = os.path.join(WATCHDOG, "agent911_weekly_report.md")
    stanza = {
        "last_generated_ts":  "unknown",
        "confidence_posture": "unknown",
        "report_path":        report_path,
    }
    try:
        with open(report_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip()
                if "**Generated:**" in line:
                    stanza["last_generated_ts"] = line.split("**Generated:**")[-1].strip()
                # posture line: "Operator posture: **🚨 NEEDS_ATTENTION**"
                if "Operator posture:" in line or (line.startswith("**") and "posture" not in line):
                    for p in ("NEEDS_ATTENTION", "WATCH", "STABLE"):
                        if p in line:
                            stanza["confidence_posture"] = p
                            break
                if stanza["last_generated_ts"] != "unknown" and \
                   stanza["confidence_posture"] != "unknown":
                    break
    except Exception:
        pass
    return stanza


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------
def p95_fmt(val) -> str:
    if val in ("unknown", None):
        return "unknown"
    try:
        ms = float(val)
        return f"{ms/1000:.0f}s ({ms:.0f}ms)"
    except Exception:
        return str(val)

# ---------------------------------------------------------------------------
# I — Operator Delta (A-A9-V0-004)
# ---------------------------------------------------------------------------

def compute_operator_delta(current_risks: list) -> dict:
    """
    Compare current run against previous to produce an operator-facing delta.

    Inputs:
      - radcheck_history.ndjson  — last 2 entries for score delta
      - agent911_state.json      — previous run's top_risks for risk diff

    Returns dict with keys:
      delta_status      READY | INSUFFICIENT_HISTORY
      score_delta       int or None
      direction         IMPROVING | DEGRADING | STABLE | None
      new_risks         list of risk dicts (present now, absent before)
      cleared_risks     list of risk dicts (absent now, present before)
    """
    # ── 1. Score delta from radcheck_history.ndjson ─────────────────────────
    history = []
    try:
        if os.path.exists(SRC_RADCHECK):
            with open(SRC_RADCHECK) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if isinstance(rec.get("score"), (int, float)):
                            history.append(rec)
                    except Exception:
                        continue
    except Exception:
        pass

    if len(history) < 2:
        return {
            "delta_status":  "INSUFFICIENT_HISTORY",
            "score_delta":   None,
            "direction":     None,
            "new_risks":     [],
            "cleared_risks": [],
        }

    prev_entry = history[-2]
    curr_entry = history[-1]
    score_prev = int(prev_entry["score"])
    score_curr = int(curr_entry["score"])
    score_delta = score_curr - score_prev

    if score_delta > 0:
        direction = "IMPROVING"
    elif score_delta < 0:
        direction = "DEGRADING"
    else:
        direction = "STABLE"

    # ── 2. Risk diff against previous agent911_state.json ───────────────────
    prev_risks = []
    try:
        prev_snap = safe_json_load(OUT_STATE)
        if prev_snap and isinstance(prev_snap.get("top_risks"), list):
            prev_risks = prev_snap["top_risks"]
    except Exception:
        pass

    prev_ids = {r.get("id") for r in prev_risks if r.get("id")}
    curr_ids = {r.get("id") for r in current_risks if r.get("id")}

    new_risks     = sorted(
        [r for r in current_risks if r.get("id") not in prev_ids],
        key=lambda x: (SEVERITY_ORDER.get(x.get("severity", "INFO"), 9), x.get("id", ""))
    )
    cleared_risks = sorted(
        [r for r in prev_risks if r.get("id") not in curr_ids],
        key=lambda x: (SEVERITY_ORDER.get(x.get("severity", "INFO"), 9), x.get("id", ""))
    )

    return {
        "delta_status":  "READY",
        "score_delta":   score_delta,
        "direction":     direction,
        "new_risks":     new_risks,
        "cleared_risks": cleared_risks,
    }


# ---------------------------------------------------------------------------
# J — Recommended Actions Panel (A-A9-P1-001)
# ---------------------------------------------------------------------------

_ACTION_RULES = [
    # (trigger_fn, action_text, reason_template, complexity, impact_score)
    # Rule 1: Reduce session context
    (
        lambda snap: snap.get("compaction_state", {}).get("risk") == "HIGH",
        "Reduce session context",
        lambda snap: (
            f"Compaction risk is HIGH "
            f"(state={snap.get('compaction_state',{}).get('state','unknown')})"
        ),
        "MED", 9,
    ),
    # Rule 2: Investigate compaction acceleration
    (
        lambda snap: snap.get("compaction_state", {}).get("acceleration") is True,
        "Investigate compaction acceleration",
        lambda snap: "Acceleration pattern detected in compaction metrics",
        "HIGH", 7,
    ),
    # Rule 3: Enable SphinxGate routing policy
    (
        lambda snap: snap.get("protection_state", {}).get("sphinxgate_state") in ("UNKNOWN", "IDLE"),
        "Enable SphinxGate routing policy",
        lambda snap: (
            f"SphinxGate is "
            f"{snap.get('protection_state',{}).get('sphinxgate_state','UNKNOWN')}"
            f" — no active routing decisions detected"
        ),
        "LOW", 6,
    ),
    # Rule 4: Verify backup recency
    (
        lambda snap: _backup_age_over(snap, 24),
        "Verify backup recency",
        lambda snap: (
            f"Last backup was "
            f"{snap.get('backup_state',{}).get('last_backup_age_hours','unknown')}h ago"
            f" (threshold: 24h)"
        ),
        "LOW", 5,
    ),
]


def _backup_age_over(snap: dict, threshold_h: float) -> bool:
    """Safe helper: return True if backup_age_hours > threshold_h."""
    try:
        return float(snap.get("backup_state", {}).get("last_backup_age_hours", 0)) > threshold_h
    except (TypeError, ValueError):
        return False


def compute_recommended_actions(snap: dict, max_actions: int = 3) -> list:
    """
    Deterministic advisory panel. Evaluates trigger rules against snap data.
    Sorted by impact_score DESC; max_actions returned (default 3).
    Advisory only — no automated changes.

    TASK_ID: A-A9-P1-001
    """
    candidates = []
    for trigger_fn, action_text, reason_fn, complexity, impact_score in _ACTION_RULES:
        try:
            if trigger_fn(snap):
                candidates.append({
                    "action":       action_text,
                    "reason":       reason_fn(snap),
                    "complexity":   complexity,
                    "impact_score": impact_score,
                })
        except Exception:
            continue

    # Sort: impact_score DESC, then action text for determinism
    candidates.sort(key=lambda x: (-x["impact_score"], x["action"]))
    return candidates[:max_actions]


def _render_recommended_actions(actions: list) -> list:
    """Return dashboard lines for the RECOMMENDED ACTIONS block."""
    if not actions:
        return ["  No actions recommended at this time."]
    lines = []
    for i, a in enumerate(actions, 1):
        lines.append(
            f"  {i}. {a['action']}"
            f" [impact={a['impact_score']}, effort={a['complexity']}]"
        )
        lines.append(f"     {a['reason']}")
    return lines


def _render_operator_delta(delta: dict) -> list:
    """Return dashboard lines for the OPERATOR DELTA section."""
    if delta.get("delta_status") == "INSUFFICIENT_HISTORY":
        return ["  Trend forming — insufficient history."]

    score_delta = delta.get("score_delta")
    direction   = delta.get("direction", "STABLE")
    new_risks   = delta.get("new_risks", [])
    cleared     = delta.get("cleared_risks", [])

    sign = "+" if score_delta and score_delta > 0 else ""
    delta_str = f"{sign}{score_delta}" if score_delta is not None else "—"

    lines = [
        f"  Score change: {delta_str} ({direction})",
        f"  New risks:    {len(new_risks)}",
        f"  Cleared:      {len(cleared)}",
    ]

    if new_risks:
        top = new_risks[0]
        lines.append(f"")
        lines.append(f"  Top new risk:")
        lines.append(f"  {top.get('id', '?')} — {top.get('summary', '')[:80]}")

    if cleared:
        top_c = cleared[0]
        lines.append(f"")
        lines.append(f"  Top cleared:")
        lines.append(f"  {top_c.get('id', '?')} — {top_c.get('summary', '')[:80]}")

    return lines


def _render_protection_activity(prot_evts: dict) -> list:
    """Return lines for the PROTECTION ACTIVITY dashboard section."""
    count   = prot_evts.get("count", 0)
    top     = prot_evts.get("top_event", "none")
    last_ts = prot_evts.get("last_event_ts", "unknown")

    # Strip prefix for readability
    top_short = top.replace("SENTINEL_PROTECTION_", "") if top and top != "none" else "none"

    if count == 0:
        status = "MONITORING — no protection events in last 24h"
        return [
            f"  Protection events (24h): {count}",
            f"  Status: {status}",
        ]

    status = "ACTIVE GUARDING"
    return [
        f"  Protection events (24h): {count}",
        f"  Last protection event:   {top_short}",
        f"  Last event timestamp:    {last_ts}",
        f"  Status:                  {status}",
    ]


def _render_protection_summary(rollup: dict) -> list:
    """
    Return dashboard lines for the PROTECTION SUMMARY block.
    Includes quiet protection counters (A-SEN-P3-001):
      guard_cycles_24h, cooldown_suppressions_24h, posture.
    TASK_ID: A-SEN-P2-001 / A-SEN-P3-001
    """
    c24     = rollup.get("events_24h",                0)
    c7d     = rollup.get("events_7d",                 0)
    sev     = rollup.get("by_severity",               {"INFO": 0, "MEDIUM": 0, "HIGH": 0})
    last3   = rollup.get("last_three_events",         [])
    gcycles = rollup.get("guard_cycles_24h",          0)
    suppr   = rollup.get("cooldown_suppressions_24h", 0)
    posture = rollup.get("posture",                   "QUIET")

    # Zero-intervention path — show sentinel is still guarding
    if c7d == 0:
        lines = [
            f"  Posture:                  {posture}",
            f"  Guard cycles (24h):       {gcycles}",
            f"  Cooldown suppressions:    {suppr}",
            f"  Protection events (24h):  0",
        ]
        if gcycles > 0:
            lines.append("  Sentinel actively monitored system — no interventions required.")
        else:
            lines.append("  Status: Monitoring — no recent activity detected")
        return lines

    lines = [
        f"  Posture:                  {posture}",
        f"  Guard cycles (24h):       {gcycles}",
        f"  Cooldown suppressions:    {suppr}",
        f"  Protection events (24h):  {c24}",
        f"  Protection events (7d):   {c7d}",
        "",
        f"  By severity:",
        f"    HIGH:   {sev.get('HIGH',   0)}",
        f"    MEDIUM: {sev.get('MEDIUM', 0)}",
        f"    INFO:   {sev.get('INFO',   0)}",
    ]

    if last3:
        lines.append("")
        lines.append("  Recent protection events:")
        for i, evt in enumerate(last3, 1):
            name = evt.get("event", "unknown")
            ts   = evt.get("ts",    "unknown")
            lines.append(f"    {i}. {name} — {ts}")

    return lines


def _render_protection_proof(rollup: dict) -> list:
    """
    Return dashboard lines for the PROTECTION PROOF section.
    Compact, operator-grade. TASK_ID: A-A9-V0-006 / SCOPE D
    """
    c7d       = rollup.get("events_7d",       0)
    last_type = rollup.get("last_event_type", "none")
    last_ts   = rollup.get("last_event_ts",   "unknown")

    last_short = last_type.replace("SENTINEL_PROTECTION_", "") if last_type else last_type
    status     = "ACTIVE GUARDING" if c7d > 0 else "MONITORING"

    if c7d == 0:
        return [
            f"  7d protections: 0",
            f"  Status: {status}",
        ]

    return [
        f"  7d protections: {c7d}",
        f"  Last event: {last_short} — {last_ts}",
        f"  Status: {status}",
    ]


def render_dashboard(snap: dict) -> str:
    rad             = snap["radcheck"]
    risks           = snap["top_risks"]
    delta           = snap.get("delta", {"delta_status": "INSUFFICIENT_HISTORY", "new_risks": [], "cleared_risks": []})
    prot            = snap["protection_state"]
    prot_evts       = snap.get("protection_events_24h", {})
    prot_rollup     = snap.get("protection_rollup", {})
    bkp             = snap["backup_state"]
    mdl             = snap["model_state"]
    comp            = snap["compaction_state"]
    repo            = snap["repo_sync"]
    ts              = snap["ts"]
    sen_rec         = snap.get("sentinel_recommendation", {})
    weekly_rollup   = snap.get("funnel_weekly_rollup", {})
    # sentinel_alignment used via snap.get in _render_alignment call below

    vdir   = rad.get("velocity_direction", "unknown")
    vdelta = rad.get("velocity_delta", "unknown")
    vel_str = "unknown"
    if vdir == "DEGRADING":
        vel_str = f"DEGRADING ({vdelta:+} pts)" if isinstance(vdelta, (int, float)) else "DEGRADING"
    elif vdir == "IMPROVING":
        vel_str = f"IMPROVING ({vdelta:+} pts)" if isinstance(vdelta, (int, float)) else "IMPROVING"
    elif vdir == "STABLE":
        vel_str = "STABLE"

    sg_state = prot.get('sphinxgate_state', 'unknown')
    sg_age_h = prot.get('sphinxgate_last_decision_age_hours')
    sg_src   = prot.get('sphinxgate_evidence_source', 'none')
    if sg_state == "ACTIVE" and sg_age_h is not None:
        sg_mins = int(float(sg_age_h) * 60)
        sg_line = f"ACTIVE (decision {sg_mins}m ago)"
    elif sg_state == "IDLE":
        if sg_age_h is not None:
            sg_line = f"IDLE (no decisions in {float(sg_age_h):.1f}h)"
        else:
            sg_line = "IDLE (no recent decisions)"
    elif sg_state == "UNKNOWN":
        sg_line = f"UNKNOWN (no evidence source)" if sg_src == "none" else f"UNKNOWN ({prot.get('sphinxgate_reason','')})"
    else:
        sg_line = f"{sg_state} ({prot.get('sphinxgate_reason', '')})"

    lines = [
        "# 🐐 AGENT911 — RELIABILITY SNAPSHOT",
        f"Updated: {ts}",
        "",
        "## SYSTEM STABILITY",
        f"Score:    {rad.get('score', 'unknown')} / 100",
        f"Risk:     {rad.get('risk_level', 'unknown')}",
        f"Velocity: {vel_str}",
        f"Scan:     {rad.get('last_scan_ts', 'unknown')}",
        "",
        "## PREDICTIVE GUARD",
    ] + _render_predictive_guard(snap.get("predictive_guard", {})) + [
        "",
        "## SENTINEL READINESS",
    ] + _render_sentinel_readiness(sen_rec) + [
        "",
        "## SENTINEL \u2194 FUNNEL ALIGNMENT",
    ] + _render_alignment(snap.get("sentinel_alignment", {})) + [
        "",
        "## ACTIVE RISKS",
    ]
    if risks:
        for r in risks:
            lines.append(f"  [{r['severity']}] {r['id']} — {r['summary']}")
    else:
        lines.append("  (none detected)")

    lines += ["", "## OPERATOR DELTA"]
    lines += _render_operator_delta(delta)

    lines += ["", "## RECOMMENDED ACTIONS"]
    lines += _render_recommended_actions(snap.get("recommended_actions", []))

    lines += [
        "",
        "## PROTECTION STATE",
        f"  Sentinel:   {prot.get('sentinel', 'unknown')}",
        f"  Watchdog:   {prot.get('watchdog', 'unknown')}",
        f"  SphinxGate: {sg_line}",
        "",
        "## PROTECTION ACTIVITY",
    ] + _render_protection_activity(prot_evts) + [
        "",
        "## PROTECTION SUMMARY",
    ] + _render_protection_summary(prot_rollup) + [
        "",
        "## BACKUP & RESURRECTION",
        f"  Last Backup Age:   {bkp.get('last_backup_age_hours', 'unknown')}h",
        f"  Restore Readiness: {bool_fmt(bkp.get('restore_ready'), 'READY', 'STALE')}",
        f"  Repo Sync:         {repo.get('repo_status_label', 'unknown')}",
        f"  Last Backup At:    {bkp.get('last_backup_ts', 'unknown')}",
        "",
        "## MODEL HEALTH",
        f"  Provider:    {mdl.get('last_provider', 'unknown')}",
        f"  Status:      {mdl.get('last_status', 'unknown')}",
        f"  Last Update: {mdl.get('updated_at', 'unknown')} ({mdl.get('age_hours', '?')}h ago)",
        "",
        "## COMPACTION RISK",
        f"  State:        {comp.get('state', 'unknown')}",
        f"  Risk:         {comp.get('risk', 'unknown')}",
        f"  p95 Duration: {p95_fmt(comp.get('p95_ms', 'unknown'))}",
        f"  Timeouts 2h:  {comp.get('timeout_2h', 'unknown')}",
        f"  Events 2h:    {comp.get('events_2h', 'unknown')}",
        f"  Acceleration: {bool_fmt(comp.get('acceleration'), 'YES', 'NO')}",
        f"  Source:       {comp.get('source', 'unknown')}",
        "",
        "## PROTECTION PROOF",
    ] + _render_protection_proof(prot_rollup) + [
        "",
        "## ROUTING CONFIDENCE",
    ] + _render_routing_confidence(snap.get("routing", {})) + [
        "",
        "## FUNNEL SIGNALS",
    ] + _render_funnel(snap.get("funnel_signals", {})) + [
        "",
        "## GTM FUNNEL (7D)",
    ] + _render_gtm_funnel(snap.get("funnel_weekly_rollup", {})) + [
        f"  Export status:               {snap.get('gtm_export_status', 'NONE')}",
        "",
        "---",
        "🐐 ACME Agent Supply Co. | Agent911 v1.0 | Read-only | openclaw-ops/scripts/agent911/agent911_snapshot.py",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# L — Performance Guardrails (A-A9-PERF-001)
# ---------------------------------------------------------------------------

def compute_perf_metrics(snapshot_ms: int, dashboard_ms: int,
                         ops_events_bytes: int) -> dict:
    """
    Evaluate agent911 performance against ARCH/OPS guardrail thresholds.
    Returns dict with snapshot_ms, dashboard_ms, ops_events_bytes, breaches list.
    Breaches list is deterministically sorted.
    TASK_ID: A-A9-PERF-001 / SCOPE A
    """
    breaches = []
    if snapshot_ms > PERF_THRESHOLDS["snapshot_ms"]:
        breaches.append("SNAPSHOT_MS")
    if dashboard_ms > PERF_THRESHOLDS["dashboard_ms"]:
        breaches.append("DASHBOARD_MS")
    if ops_events_bytes > PERF_THRESHOLDS["ops_events_bytes"]:
        breaches.append("OPS_EVENTS_SIZE")
    return {
        "snapshot_ms":      snapshot_ms,
        "dashboard_ms":     dashboard_ms,
        "ops_events_bytes": ops_events_bytes,
        "breaches":         sorted(breaches),
    }


def _perf_breach_on_cooldown() -> bool:
    """Return True if a breach was recently emitted (shared 6h cooldown)."""
    from datetime import timedelta
    try:
        state = safe_json_load(PERF_STATE)
        last_ts_str = state.get("A9_GUARDRAIL_BREACH")
        if not last_ts_str:
            return False
        last_dt = _parse_ts(last_ts_str)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=PERF_COOLDOWN_H)
        return last_dt > cutoff
    except Exception:
        return False


def emit_perf_breach(perf: dict, ts: str) -> bool:
    """
    Append A9_GUARDRAIL_BREACH to ops_events.log when thresholds exceeded.
    Respects 6h shared cooldown. Updates PERF_STATE on emission.
    Returns True if event was emitted, False if suppressed or no breach.
    TASK_ID: A-A9-PERF-001 / SCOPE C
    """
    breaches = perf.get("breaches", [])
    if not breaches:
        return False

    if _perf_breach_on_cooldown():
        return False  # suppressed — still within cooldown

    record = {
        "ts":       ts,
        "event":    "A9_GUARDRAIL_BREACH",
        "severity": "MEDIUM",
        "source":   "agent911",
        "breaches": breaches,
        "values": {
            "snapshot_ms":      perf["snapshot_ms"],
            "dashboard_ms":     perf["dashboard_ms"],
            "ops_events_bytes": perf["ops_events_bytes"],
        },
        "thresholds":      PERF_THRESHOLDS,
        "cooldown_applied": False,
    }
    try:
        with open(SRC_OPS, "a") as f:
            f.write(json.dumps(record) + "\n")
        # Update cooldown state
        try:
            state = safe_json_load(PERF_STATE)
            state["A9_GUARDRAIL_BREACH"] = ts
            with open(PERF_STATE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass
        return True
    except Exception:
        return False


def append_perf_history(ts: str, snapshot_ms: int, dashboard_ms: int,
                        ops_bytes: int, breaches: list) -> None:
    """
    Append one NDJSON line to agent911_history.ndjson. Append-only, never rewrite.
    TASK_ID: A-A9-PERF-001 / SCOPE B
    """
    record = {
        "ts":               ts,
        "snapshot_ms":      snapshot_ms,
        "dashboard_ms":     dashboard_ms,
        "ops_events_bytes": ops_bytes,
        "breaches":         breaches,
    }
    try:
        os.makedirs(WATCHDOG, exist_ok=True)
        with open(PERF_HISTORY, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _render_perf_health(perf: dict) -> list:
    """
    Return dashboard lines for the PERF HEALTH block.
    TASK_ID: A-A9-PERF-001 / SCOPE D
    """
    snap_ms  = perf.get("snapshot_ms",      0)
    dash_ms  = perf.get("dashboard_ms",     0)
    ops_bytes = perf.get("ops_events_bytes", 0)
    breaches  = perf.get("breaches",         [])

    ops_mb = ops_bytes / 1_048_576

    def _ok_or_breach(key: str) -> str:
        return "BREACH" if key in breaches else "OK"

    lines = [
        f"  Snapshot:       {snap_ms}ms ({_ok_or_breach('SNAPSHOT_MS')})",
        f"  Dashboard:      {dash_ms}ms ({_ok_or_breach('DASHBOARD_MS')})",
        f"  ops_events.log: {ops_mb:.2f}MB ({_ok_or_breach('OPS_EVENTS_SIZE')})",
    ]
    if breaches:
        lines.append(f"  Status: PERF DEGRADED — triggers: {', '.join(breaches)}")
    else:
        lines.append("  Status: OK")
    return lines


def _check_perf_mtl_needed(ts: str) -> None:
    """
    Scope E (optional): if >=3 breach-runs in last 24h, auto-create MTL task
    A-A9-PERF-OPT-001. Only creates once (idempotent). Runs mtl_update.sh.
    TASK_ID: A-A9-PERF-001 / SCOPE E
    """
    from datetime import timedelta
    cut = datetime.now(timezone.utc) - timedelta(hours=24)

    breach_runs  = 0
    breach_counts: dict = {}

    try:
        if not os.path.exists(PERF_HISTORY):
            return
        with open(PERF_HISTORY) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    dt = _parse_ts(rec.get("ts", ""))
                    if dt < cut:
                        continue
                    bl = rec.get("breaches", [])
                    if bl:
                        breach_runs += 1
                        for b in bl:
                            breach_counts[b] = breach_counts.get(b, 0) + 1
                except Exception:
                    continue
    except Exception:
        return

    if breach_runs < 3:
        return

    MTL_FILE   = os.path.join(REPO, "openclaw-ops", "ops", "mtl_updates.ndjson")
    MTL_SCRIPT = os.path.join(REPO, "openclaw-ops", "scripts", "ops", "mtl_update.sh")

    # Idempotency check — don't create if already in MTL
    try:
        if os.path.exists(MTL_FILE):
            with open(MTL_FILE) as f:
                if "A-A9-PERF-OPT-001" in f.read():
                    return
    except Exception:
        return

    dominant = max(breach_counts, key=lambda x: breach_counts[x]) if breach_counts else "unknown"
    mtl_record = {
        "ts":       ts,
        "actor":    "agent911",
        "task_id":  "A-A9-PERF-OPT-001",
        "op":       "ADD",
        "owner":    "GP-OPS",
        "status_to": "ACTIVE",
        "priority": "MED",
        "title":    "Agent911 Performance Optimization (auto-created by perf guardrail)",
        "note":     (f"Persistent breaches: {breach_runs} breach-runs in 24h, "
                     f"dominant={dominant}"),
    }
    try:
        with open(MTL_FILE, "a") as f:
            f.write(json.dumps(mtl_record) + "\n")
        subprocess.run(
            ["bash", MTL_SCRIPT],
            timeout=30,
            capture_output=True,
            cwd=REPO,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.monotonic()
    now = ts_iso()

    radcheck        = gather_radcheck()
    top_risks       = gather_top_risks(3)
    repo_sync       = gather_repo_sync()
    protection      = gather_protection_state()
    backup          = gather_backup_state()
    model           = gather_model_state()
    compaction      = gather_compaction_state()
    prot_events     = gather_protection_events_24h()
    prot_rollup     = gather_protection_rollups()
    routing         = gather_routing_confidence()
    pred_guard      = gather_predictive_guard()
    fma             = gather_fma_classification(repo_sync)
    weekly_report   = gather_weekly_report()
    # Delta must be computed BEFORE OUT_STATE is overwritten this run
    op_delta        = compute_operator_delta(top_risks)

    # Assemble partial snap for recommended actions computation
    _snap_for_actions = {
        "compaction_state":  compaction,
        "protection_state":  protection,
        "backup_state":      backup,
        "protection_rollup": prot_rollup,
        "routing":           routing,
    }
    recommended_actions = compute_recommended_actions(_snap_for_actions)

    # A-FUN-P1-001/P2-001: Funnel Telemetry — MUST run BEFORE any emissions this run
    # (before gather_sentinel_recommendation writes SENTINEL_RECOMMENDATION_EVAL)
    # This ensures funnel counts reflect the pre-run state, making consecutive
    # standalone funnel reads deterministic.
    funnel_signals, weekly_rollup = gather_funnel_signals()

    # A-SEN-P4-001: Sentinel Attach Bridge recommendation signal
    # Must have all telemetry assembled before calling; emits NDJSON event
    _snap_for_sentinel = {
        "predictive_guard":        pred_guard,
        "compaction_state":        compaction,
        "protection_rollup":       prot_rollup,
        "protection_events_24h":   prot_events,
        "routing":                 routing,
        "radcheck":                radcheck,
        "backup_state":            backup,
    }
    sentinel_recommendation = gather_sentinel_recommendation(_snap_for_sentinel)

    # A-SEN-P4-002: Sentinel ↔ Funnel Alignment (uses current-run data)
    sentinel_alignment = gather_sentinel_alignment(
        sentinel_rec=sentinel_recommendation,
        weekly_rollup=weekly_rollup,
    )

    snap = {
        "ts":                      now,
        "schema_version":          "agent911.v1.0",
        "stability_score":         radcheck.get("score"),
        "risk_level":              radcheck.get("risk_level"),
        "top_risks":               top_risks,
        "delta":                   op_delta,
        "recommended_actions":     recommended_actions,
        "predictive_guard":        pred_guard,
        "sentinel_recommendation": sentinel_recommendation,
        "sentinel_alignment_state": sentinel_alignment.get("alignment_state", "DRIFT"),
        "sentinel_alignment":      sentinel_alignment,
        "protection_state":        protection,
        "protection_events_24h":   prot_events,
        "protection_rollup":       prot_rollup,
        "routing":                 routing,
        "backup_state":            backup,
        "repo_sync":               repo_sync,
        "model_state":             model,
        "compaction_state":        compaction,
        "radcheck":                    radcheck,
        "weekly_report":               weekly_report,
        "funnel_signals":              funnel_signals,
        "funnel_weekly_rollup":        weekly_rollup,
        "gtm_export_status":           _read_export_status() if _GTM_EXPORT_AVAILABLE else "NONE",
        "agent_presence_summary":      fma.get("agent_presence_summary", {}),
        "agents_requiring_attention":  fma.get("agents_requiring_attention", []),
        "duration_ms":                 None,
    }

    # ── SCOPE A: time render separately ─────────────────────────────────────
    t_render = time.monotonic()
    dashboard = render_dashboard(snap)
    dashboard_ms = round((time.monotonic() - t_render) * 1000)

    # ── SCOPE A: ops_events.log size ─────────────────────────────────────────
    ops_events_bytes = os.path.getsize(SRC_OPS) if os.path.exists(SRC_OPS) else 0

    # ── SCOPE A: approximate total (pre-file-writes) ─────────────────────────
    snapshot_ms_approx = round((time.monotonic() - t0) * 1000)

    # ── SCOPE A/B/C: compute perf metrics + emit breach + append history ─────
    perf = compute_perf_metrics(snapshot_ms_approx, dashboard_ms, ops_events_bytes)
    emit_perf_breach(perf, now)
    append_perf_history(now, snapshot_ms_approx, dashboard_ms, ops_events_bytes,
                        perf["breaches"])

    # ── SCOPE D: append PERF HEALTH block to dashboard ───────────────────────
    perf_lines = ["", "## PERF HEALTH"] + _render_perf_health(perf)
    dashboard = dashboard.rstrip() + "\n" + "\n".join(perf_lines) + "\n"

    try:
        with open(OUT_DASH, "w") as f:
            f.write(dashboard + "\n")
    except Exception as e:
        print(f"[WARN] Could not write dashboard: {e}", file=sys.stderr)

    # ── SCOPE E: optional MTL auto-create on persistent breaches ────────────
    _check_perf_mtl_needed(now)

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    snap["duration_ms"]     = elapsed_ms
    snap["dashboard_ms"]    = dashboard_ms
    snap["ops_events_bytes"] = ops_events_bytes
    snap["perf_health"]     = perf

    try:
        with open(OUT_STATE, "w") as f:
            json.dump(snap, f, indent=2, default=str)
            f.write("\n")
    except Exception as e:
        print(f"[WARN] Could not write state: {e}", file=sys.stderr)

    perf_status = "PERF_DEGRADED" if perf["breaches"] else "PERF_OK"
    print(f"AGENT911_SNAPSHOT_OK ts={now} score={snap['stability_score']} "
          f"risk={snap['risk_level']} repo={repo_sync.get('repo_status_label')} "
          f"sphinxgate={protection.get('sphinxgate_state')} "
          f"comp_source={compaction.get('source')} duration_ms={elapsed_ms} "
          f"dashboard_ms={dashboard_ms} {perf_status}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"AGENT911_SNAPSHOT_ERROR: {e}", file=sys.stderr)
        sys.exit(0)
