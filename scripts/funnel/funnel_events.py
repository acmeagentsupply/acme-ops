#!/usr/bin/env python3
"""
Funnel Telemetry Instrumentation — RadCheck → Sentinel → Agent911
TASK_ID: A-FUN-P1-001
OWNER:   GP-OPS

Emits deterministic NDJSON funnel signals to ops_events.log.
Target overhead: <5ms. No subprocesses.

Signal chain:
  RADCHECK_RUN        — RadCheck execution (24h)
  SENTINEL_RECOMMENDED — Sentinel recommendation evaluated true (24h)
  SENTINEL_ENABLED    — Sentinel actively protecting (7d, if detectable)
  AGENT911_VIEWED     — Agent911 snapshot executed (24h)
  AGENT911_EXPANDED   — Agent911 deep-dive (weekly report generated, 7d)

Sources: existing state files and ops_events.log only.
No new probes. All reads graceful on missing files.

SAFETY:
  - Zero writes to openclaw.json
  - Zero gateway restarts
  - Zero subprocesses
  - Append-only to ops_events.log
  - Exits 0 always
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME     = os.path.expanduser("~")
WATCHDOG = os.path.join(HOME, ".openclaw", "watchdog")

SRC_RC_HIST   = os.path.join(WATCHDOG, "radcheck_history.ndjson")
SRC_A9_HIST   = os.path.join(WATCHDOG, "agent911_history.ndjson")
SRC_OPS       = os.path.join(WATCHDOG, "ops_events.log")
SRC_WEEKLY    = os.path.join(WATCHDOG, "agent911_weekly_report.md")
SRC_FUNNEL_ST = os.path.join(WATCHDOG, "funnel_state.json")

# Event type constants
EVT_RC_RUN      = "RADCHECK_RUN"
EVT_SEN_REC     = "SENTINEL_RECOMMENDED"
EVT_SEN_ENABLED = "SENTINEL_ENABLED"
EVT_A9_VIEWED   = "AGENT911_VIEWED"
EVT_A9_EXPANDED = "AGENT911_EXPANDED"
EVT_SNAPSHOT    = "FUNNEL_SNAPSHOT"

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ts_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _cutoff(hours: float) -> datetime:
    return _now_utc() - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# File readers (all graceful on missing)
# ---------------------------------------------------------------------------

def _safe_ndjson_tail(path: str, tail: int = 500) -> list[dict]:
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()[-tail:]
        rows = []
        for raw in lines:
            raw = raw.strip()
            if raw:
                try:
                    rows.append(json.loads(raw))
                except Exception:
                    pass
        return rows
    except Exception:
        return []


def _safe_json_load(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _safe_json_save(path: str, data: dict) -> None:
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Signal computers — all read-only, <1ms each
# ---------------------------------------------------------------------------

def _count_ndjson_in_window(path: str, hours: float) -> int:
    """Count NDJSON entries with a 'ts' field within the last N hours."""
    cut = _cutoff(hours)
    count = 0
    for row in _safe_ndjson_tail(path, tail=500):
        ts = row.get("ts", "")
        dt = _parse_dt(ts)
        if dt and dt >= cut:
            count += 1
    return count


def _count_ops_events(path: str, event_name: str, hours: float,
                      extra_filter: dict | None = None) -> int:
    """
    Count ops_events.log entries matching event_name within last N hours.
    Optional extra_filter: {field: expected_value} must all match.
    Single forward pass; tail-limited to last 2000 lines for performance.
    """
    cut = _cutoff(hours)
    count = 0
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()[-2000:]
        for raw in lines:
            raw = raw.strip()
            if not raw or event_name not in raw:
                continue
            try:
                rec = json.loads(raw)
                if rec.get("event") != event_name:
                    continue
                dt = _parse_dt(rec.get("ts", ""))
                if not dt or dt < cut:
                    continue
                if extra_filter:
                    if not all(rec.get(k) == v for k, v in extra_filter.items()):
                        continue
                count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def _sentinel_enabled_7d() -> tuple[bool, int]:
    """
    Detect Sentinel as 'enabled' if any SENTINEL_PROTECTION_* events in 7d.
    Returns (enabled: bool, event_count_7d: int).
    """
    cut = _cutoff(168.0)  # 7 days
    count = 0
    try:
        with open(SRC_OPS, errors="replace") as fh:
            lines = fh.readlines()[-2000:]
        for raw in lines:
            raw = raw.strip()
            if "SENTINEL_PROTECTION_" not in raw:
                continue
            try:
                rec = json.loads(raw)
                evt = rec.get("event", "")
                if not evt.startswith("SENTINEL_PROTECTION_"):
                    continue
                dt = _parse_dt(rec.get("ts", ""))
                if dt and dt >= cut:
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count > 0, count


def _agent911_expanded_7d() -> tuple[int, str]:
    """
    Proxy for Agent911 deep-dive: count of weekly report files generated in 7d.
    Returns (count, source_note).
    Uses mtime of SRC_WEEKLY and ops_events.log WEEKLY_REPORT_OK events.
    """
    cut_ts = _cutoff(168.0).timestamp()

    # Check weekly report mtime
    weekly_count = 0
    try:
        mtime = os.path.getmtime(SRC_WEEKLY)
        if mtime >= cut_ts:
            weekly_count = 1  # file exists and was written within 7d
    except Exception:
        pass

    # Also count any WEEKLY_REPORT_OK events in ops_events.log (7d)
    ops_count = _count_ops_events(SRC_OPS, "WEEKLY_REPORT_OK", 168.0)

    # Use whichever gives more signal, cap at ops_count if > 0
    total = ops_count if ops_count > 0 else weekly_count
    source = "ops_events" if ops_count > 0 else ("weekly_report_mtime" if weekly_count else "none")
    return total, source


# ---------------------------------------------------------------------------
# Confidence scorer
# ---------------------------------------------------------------------------

def _confidence_for(count: int, expected_per_day: float = 1.0) -> int:
    """
    Deterministic confidence: 0 if no count, scales up based on observed activity.
    Cap at 100.
    expected_per_day: how many events per day is "healthy" for this signal.
    """
    if count == 0:
        return 0
    ratio = count / max(expected_per_day, 0.001)
    if ratio >= 1.0:
        return min(90, 60 + int(ratio * 10))
    return max(30, int(ratio * 60))


# ---------------------------------------------------------------------------
# Core compute function
# ---------------------------------------------------------------------------

def compute_funnel_signals() -> dict:
    """
    Compute all funnel signals from existing state files. Read-only.
    Deterministic: same file contents → identical output.
    Returns dict with all signal counts, flags, and confidence values.
    """
    # 1. RadCheck runs (24h)
    rc_runs_24h = _count_ndjson_in_window(SRC_RC_HIST, 24.0)

    # 2. Sentinel recommended (24h) — SENTINEL_RECOMMENDATION_EVAL with recommended=true
    sen_recommended_24h = _count_ops_events(
        SRC_OPS, "SENTINEL_RECOMMENDATION_EVAL", 24.0,
        extra_filter={"recommended": True},
    )

    # 3. Sentinel enabled (7d proxy)
    sen_enabled, sen_prot_events_7d = _sentinel_enabled_7d()

    # 4. Agent911 viewed (24h)
    a9_viewed_24h = _count_ndjson_in_window(SRC_A9_HIST, 24.0)

    # 5. Agent911 expanded (7d proxy — weekly report)
    a9_expanded_7d, a9_expanded_source = _agent911_expanded_7d()

    return {
        "rc_runs_24h":           rc_runs_24h,
        "rc_confidence":         _confidence_for(rc_runs_24h, 1.0),
        "sen_recommended_24h":   sen_recommended_24h,
        "sen_recommended_confidence": _confidence_for(sen_recommended_24h, 1.0),
        "sen_enabled":           sen_enabled,
        "sen_prot_events_7d":    sen_prot_events_7d,
        "sen_enabled_confidence": 80 if sen_enabled else 0,
        "a9_viewed_24h":         a9_viewed_24h,
        "a9_viewed_confidence":  _confidence_for(a9_viewed_24h, 1.0),
        "a9_expanded_7d":        a9_expanded_7d,
        "a9_expanded_source":    a9_expanded_source,
        "a9_expanded_confidence": _confidence_for(a9_expanded_7d, 1.0),
    }


# ---------------------------------------------------------------------------
# State-tracked transition emitter
# ---------------------------------------------------------------------------

_STATE_KEYS = (
    "rc_runs_24h",
    "sen_recommended_24h",
    "sen_enabled",
    "a9_viewed_24h",
    "a9_expanded_7d",
)


def _load_funnel_state() -> dict:
    return _safe_json_load(SRC_FUNNEL_ST)


def _save_funnel_state(state: dict) -> None:
    _safe_json_save(SRC_FUNNEL_ST, state)


def _append_event(path: str, record: dict) -> bool:
    """Append one NDJSON line. Returns True on success."""
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


def emit_funnel_events(signals: dict, ops_path: str = SRC_OPS) -> list[str]:
    """
    Emit individual transition events and one FUNNEL_SNAPSHOT per run.

    Transition events fire when a signal count has increased since the last
    known state (tracked in funnel_state.json). This prevents duplicate
    emissions across runs when nothing changes.

    FUNNEL_SNAPSHOT always emitted (one per run — aggregate of all signals).

    Returns list of event names emitted this run.
    """
    ts = _ts_iso()
    emitted: list[str] = []
    prev = _load_funnel_state()

    # ── Individual transition events ─────────────────────────────────────────

    def _should_emit(key: str, current_val: Any) -> bool:
        """Fire if count increased OR boolean flipped to True."""
        prev_val = prev.get(key)
        if isinstance(current_val, bool):
            return bool(current_val) and not bool(prev_val)
        if isinstance(current_val, int):
            return current_val > (prev_val if isinstance(prev_val, int) else 0)
        return False

    # RADCHECK_RUN
    if _should_emit("rc_runs_24h", signals["rc_runs_24h"]):
        _append_event(ops_path, {
            "ts":         ts,
            "event":      EVT_RC_RUN,
            "severity":   "INFO",
            "source":     "funnel_events",
            "count_24h":  signals["rc_runs_24h"],
            "confidence": signals["rc_confidence"],
        })
        emitted.append(EVT_RC_RUN)

    # SENTINEL_RECOMMENDED
    if _should_emit("sen_recommended_24h", signals["sen_recommended_24h"]):
        _append_event(ops_path, {
            "ts":         ts,
            "event":      EVT_SEN_REC,
            "severity":   "INFO",
            "source":     "funnel_events",
            "count_24h":  signals["sen_recommended_24h"],
            "confidence": signals["sen_recommended_confidence"],
        })
        emitted.append(EVT_SEN_REC)

    # SENTINEL_ENABLED
    if _should_emit("sen_enabled", signals["sen_enabled"]):
        _append_event(ops_path, {
            "ts":            ts,
            "event":         EVT_SEN_ENABLED,
            "severity":      "INFO",
            "source":        "funnel_events",
            "prot_events_7d": signals["sen_prot_events_7d"],
            "confidence":    signals["sen_enabled_confidence"],
        })
        emitted.append(EVT_SEN_ENABLED)

    # AGENT911_VIEWED
    if _should_emit("a9_viewed_24h", signals["a9_viewed_24h"]):
        _append_event(ops_path, {
            "ts":         ts,
            "event":      EVT_A9_VIEWED,
            "severity":   "INFO",
            "source":     "funnel_events",
            "count_24h":  signals["a9_viewed_24h"],
            "confidence": signals["a9_viewed_confidence"],
        })
        emitted.append(EVT_A9_VIEWED)

    # AGENT911_EXPANDED
    if _should_emit("a9_expanded_7d", signals["a9_expanded_7d"]):
        _append_event(ops_path, {
            "ts":           ts,
            "event":        EVT_A9_EXPANDED,
            "severity":     "INFO",
            "source":       "funnel_events",
            "count_7d":     signals["a9_expanded_7d"],
            "proxy_source": signals["a9_expanded_source"],
            "confidence":   signals["a9_expanded_confidence"],
        })
        emitted.append(EVT_A9_EXPANDED)

    # ── FUNNEL_SNAPSHOT (always — one per run) ────────────────────────────────
    snapshot_rec = {
        "ts":     ts,
        "event":  EVT_SNAPSHOT,
        "severity": "INFO",
        "source": "funnel_events",
        "signals": {
            "rc_runs_24h":          signals["rc_runs_24h"],
            "sen_recommended_24h":  signals["sen_recommended_24h"],
            "sen_enabled":          signals["sen_enabled"],
            "a9_viewed_24h":        signals["a9_viewed_24h"],
            "a9_expanded_7d":       signals["a9_expanded_7d"],
        },
        "confidence": {
            "radcheck":    signals["rc_confidence"],
            "sentinel_rec": signals["sen_recommended_confidence"],
            "sentinel_en": signals["sen_enabled_confidence"],
            "a9_viewed":   signals["a9_viewed_confidence"],
            "a9_expanded": signals["a9_expanded_confidence"],
        },
    }
    _append_event(ops_path, snapshot_rec)
    emitted.append(EVT_SNAPSHOT)

    # ── Update state ──────────────────────────────────────────────────────────
    new_state = {k: signals.get(k) for k in _STATE_KEYS}
    new_state["last_snapshot_ts"] = ts
    _save_funnel_state(new_state)

    return emitted


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

def render_funnel_block(signals: dict) -> list[str]:
    """
    Return dashboard lines for the FUNNEL SIGNALS block.
    Exactly matches spec layout.
    """
    rc_count   = signals.get("rc_runs_24h",         "unknown")
    sen_count  = signals.get("sen_recommended_24h",  "unknown")
    a9_count   = signals.get("a9_expanded_7d",       "unknown")
    sen_enabled = signals.get("sen_enabled",         False)
    a9_viewed  = signals.get("a9_viewed_24h",        "unknown")

    enabled_label = "YES" if sen_enabled else "NO"

    return [
        f"  RadCheck runs (24h):          {rc_count}",
        f"  Sentinel recommended (24h):   {sen_count}",
        f"  Sentinel enabled (7d proxy):  {enabled_label}",
        f"  Agent911 views (24h):         {a9_viewed}",
        f"  Agent911 expansions (7d):     {a9_count}",
    ]


# ---------------------------------------------------------------------------
# A-FUN-P2-001 — Weekly GTM Funnel Rollup
# ---------------------------------------------------------------------------

# Output path for machine-readable weekly rollup
OUT_GTM_WEEKLY = os.path.join(WATCHDOG, "gtm_funnel_weekly.json")

# Fixed JSON key order for determinism (insertion order maintained in Python 3.7+)
_WEEKLY_KEY_ORDER = [
    "window_days",
    "radcheck_runs_7d",
    "sentinel_recommended_7d",
    "sentinel_enabled_present",
    "agent911_views_7d",
    "agent911_expansions_7d",
    "sentinel_attach_rate",
    "agent911_expansion_rate",
    "ts",
]

EVT_WEEKLY_ROLLUP = "FUNNEL_WEEKLY_ROLLUP"


def compute_weekly_rollup(ops_path: str = SRC_OPS) -> dict:
    """
    Aggregate FUNNEL_SNAPSHOT events from the last 7 days.

    Strategy:
      - Single forward pass of ops_events.log (tail-capped for performance)
      - Collect FUNNEL_SNAPSHOT events within 7-day UTC window
      - Group by UTC date bucket (YYYY-MM-DD)
      - For volume signals: take MAX per day, then SUM across days
        (avoids double-counting multiple snapshots per day)
      - For a9_expanded_7d: use LATEST value (it is already a 7d count)
      - For sen_enabled_present: True if ANY snapshot has sen_enabled=True
      - Compute conversion rates with fixed rounding (3 decimals)

    Returns dict with deterministic key ordering.
    Graceful on missing file or zero events.

    TASK_ID: A-FUN-P2-001
    """
    cut_7d = _cutoff(168.0)   # 7 days ago

    # Collect FUNNEL_SNAPSHOT events in window
    # Structure: {date_str: {"rc": max, "sen_rec": max, "a9v": max, "a9e": latest}}
    daily: dict[str, dict] = {}
    sen_enabled_any = False
    a9_expanded_latest = 0

    try:
        with open(ops_path, errors="replace") as fh:
            lines = fh.readlines()[-3000:]   # bounded tail for performance
        for raw in lines:
            raw = raw.strip()
            if not raw or EVT_SNAPSHOT not in raw:
                continue
            try:
                rec = json.loads(raw)
                if rec.get("event") != EVT_SNAPSHOT:
                    continue
                dt = _parse_dt(rec.get("ts", ""))
                if not dt or dt < cut_7d:
                    continue
                day_key = dt.strftime("%Y-%m-%d")
                sig = rec.get("signals", {})

                rc     = int(sig.get("rc_runs_24h", 0) or 0)
                sen_r  = int(sig.get("sen_recommended_24h", 0) or 0)
                sen_en = bool(sig.get("sen_enabled", False))
                a9v    = int(sig.get("a9_viewed_24h", 0) or 0)
                a9e    = int(sig.get("a9_expanded_7d", 0) or 0)

                if day_key not in daily:
                    daily[day_key] = {"rc": 0, "sen_rec": 0, "a9v": 0}
                # Take daily max for each volume signal
                daily[day_key]["rc"]      = max(daily[day_key]["rc"],      rc)
                daily[day_key]["sen_rec"] = max(daily[day_key]["sen_rec"], sen_r)
                daily[day_key]["a9v"]     = max(daily[day_key]["a9v"],     a9v)
                # Track latest a9_expanded (already a 7d count — use newest)
                a9_expanded_latest = max(a9_expanded_latest, a9e)
                # sen_enabled: True if ANY snapshot shows it enabled
                if sen_en:
                    sen_enabled_any = True

            except Exception:
                continue
    except Exception:
        pass

    # Sum daily maxima across the 7-day window
    rc_runs_7d       = sum(d["rc"]      for d in daily.values())
    sen_rec_7d       = sum(d["sen_rec"] for d in daily.values())
    a9_views_7d      = sum(d["a9v"]     for d in daily.values())
    a9_expansions_7d = a9_expanded_latest   # 7d count already from source

    # Conversion rates (deterministic, bounded)
    # Sentinel attach rate v1: simple presence proxy
    if sen_enabled_any and rc_runs_7d > 0:
        attach_rate = 1.000
    else:
        attach_rate = 0.000

    expansion_rate = round(a9_expansions_7d / max(a9_views_7d, 1), 3)

    ts = _ts_iso()

    # Build result with fixed key insertion order for JSON determinism
    rollup: dict = {}
    rollup["window_days"]             = 7
    rollup["radcheck_runs_7d"]        = rc_runs_7d
    rollup["sentinel_recommended_7d"] = sen_rec_7d
    rollup["sentinel_enabled_present"] = sen_enabled_any
    rollup["agent911_views_7d"]       = a9_views_7d
    rollup["agent911_expansions_7d"]  = a9_expansions_7d
    rollup["sentinel_attach_rate"]    = attach_rate
    rollup["agent911_expansion_rate"] = expansion_rate
    rollup["ts"]                      = ts

    return rollup


def write_weekly_json(rollup: dict, out_path: str = OUT_GTM_WEEKLY) -> bool:
    """
    Write gtm_funnel_weekly.json with fixed key ordering.
    Overwrites on each run (single-source-of-truth file).
    Returns True on success.
    """
    # Re-build with guaranteed key order (in case rollup came from elsewhere)
    ordered: dict = {}
    for k in _WEEKLY_KEY_ORDER:
        if k in rollup:
            ordered[k] = rollup[k]
    # Any extra keys appended at end (for forward-compat)
    for k, v in rollup.items():
        if k not in ordered:
            ordered[k] = v
    try:
        with open(out_path, "w") as fh:
            json.dump(ordered, fh, indent=2)
            fh.write("\n")
        return True
    except Exception:
        return False


def emit_weekly_rollup_event(rollup: dict, ops_path: str = SRC_OPS) -> bool:
    """
    Append FUNNEL_WEEKLY_ROLLUP NDJSON to ops_events.log.
    Emitted every snapshot run (no cooldown — lightweight aggregate).
    Returns True on success.
    """
    record = {
        "ts":                       rollup.get("ts", _ts_iso()),
        "event":                    EVT_WEEKLY_ROLLUP,
        "severity":                 "INFO",
        "source":                   "funnel_events",
        "radcheck_runs_7d":         rollup.get("radcheck_runs_7d", 0),
        "sentinel_recommended_7d":  rollup.get("sentinel_recommended_7d", 0),
        "agent911_views_7d":        rollup.get("agent911_views_7d", 0),
        "agent911_expansions_7d":   rollup.get("agent911_expansions_7d", 0),
        "attach_rate":              rollup.get("sentinel_attach_rate", 0.0),
        "expansion_rate":           rollup.get("agent911_expansion_rate", 0.0),
    }
    return _append_event(ops_path, record)


def render_gtm_funnel_block(rollup: dict) -> list[str]:
    """
    Return dashboard lines for the GTM FUNNEL (7D) block.
    Empty-safe: shows advisory message when no activity detected.
    """
    rc7   = rollup.get("radcheck_runs_7d",        0)
    sr7   = rollup.get("sentinel_recommended_7d",  0)
    en    = rollup.get("sentinel_enabled_present", False)
    a9v7  = rollup.get("agent911_views_7d",        0)
    a9e7  = rollup.get("agent911_expansions_7d",   0)
    atch  = rollup.get("sentinel_attach_rate",     0.0)
    exp   = rollup.get("agent911_expansion_rate",  0.0)

    # Empty-safe check
    if rc7 == 0 and sr7 == 0 and a9v7 == 0:
        return ["  Insufficient activity for stable funnel signal."]

    en_label = "YES" if en else "NO"

    return [
        f"  RadCheck runs (7d):          {rc7}",
        f"  Sentinel recommended (7d):   {sr7}",
        f"  Sentinel enabled:            {en_label}",
        f"  Attach rate:                 {atch:.3f}",
        f"",
        f"  Agent911 views (7d):         {a9v7}",
        f"  Agent911 expansions (7d):    {a9e7}",
        f"  Expansion rate:              {exp:.3f}",
    ]


# ---------------------------------------------------------------------------
# A-FUN-P3-001 — Weekly Funnel Report Export (GTM-Ready)
# ---------------------------------------------------------------------------

OUT_GTM_WEEKLY_MD = os.path.join(WATCHDOG, "gtm_funnel_weekly.md")
EVT_WEEKLY_REPORT = "FUNNEL_WEEKLY_REPORT"

# ── Conversion band classifiers (deterministic) ───────────────────────────

def _attach_band(rate: float) -> str:
    """Deterministic Sentinel attach rate band."""
    if rate == 0.0:
        return "WATCH"
    if rate < 0.5:
        return "DEVELOPING"
    return "HEALTHY"


def _expansion_band(rate: float) -> str:
    """Deterministic Agent911 expansion rate band."""
    if rate < 0.05:
        return "EARLY"
    if rate < 0.15:
        return "DEVELOPING"
    return "STRONG"


# ── Interpretation line (deterministic, single rule evaluation) ──────────

def _funnel_interpretation(rc7: int, attach: float) -> str:
    if rc7 == 0:
        return "No RadCheck activity observed."
    if attach == 0.0:
        return "Sentinel adoption opportunity present."
    return "Funnel progressing within expected bounds."


# ── Operator notes (guarded, deterministic) ──────────────────────────────

def _operator_notes(sen_enabled: bool, sen_rec_7d: int,
                    a9_views_7d: int, a9_exp_7d: int) -> str:
    if not sen_enabled and sen_rec_7d > 0:
        return (
            "Sentinel has been recommended but is not observed as enabled. "
            "A Sentinel configuration review may be warranted."
        )
    if a9_views_7d >= 5 and a9_exp_7d == 0:
        return (
            "Agent911 views observed but no expansion events detected. "
            "Consider reviewing Agent911 positioning and operator onboarding."
        )
    return "No material funnel blockers detected."


# ── Report renderer (pure function — same rollup → byte-identical output) ─

def render_weekly_report(rollup: dict, hostname: str = "") -> str:
    """
    Render gtm_funnel_weekly.md from a weekly rollup dict.
    Pure function: same input dict → byte-identical output.
    Uses rollup['ts'] as the Generated timestamp (not datetime.now()).

    TASK_ID: A-FUN-P3-001
    """
    # Detect insufficient data
    data_ok = bool(rollup.get("radcheck_runs_7d") is not None)
    ts_str  = rollup.get("ts", "unknown")

    if not data_ok:
        lines = [
            "# ACME Funnel Report — GTM Weekly Summary",
            "",
            f"**Status:**    INSUFFICIENT_DATA",
            f"**Generated:** {ts_str} (UTC)",
            "**Mode:**       Observational",
            "",
            "No weekly rollup data available. Run agent911_snapshot.py to generate.",
            "",
            "---",
            f"*🐐 ACME Agent Supply Co. | Funnel Report v1 | A-FUN-P3-001*",
        ]
        return "\n".join(lines) + "\n"

    # Extract fields with safe defaults
    rc7      = int(rollup.get("radcheck_runs_7d",        0))
    sr7      = int(rollup.get("sentinel_recommended_7d",  0))
    sen_en   = bool(rollup.get("sentinel_enabled_present", False))
    a9v7     = int(rollup.get("agent911_views_7d",        0))
    a9e7     = int(rollup.get("agent911_expansions_7d",   0))
    atch     = float(rollup.get("sentinel_attach_rate",   0.0))
    exp      = float(rollup.get("agent911_expansion_rate", 0.0))

    en_label   = "YES" if sen_en else "NO"
    interp     = _funnel_interpretation(rc7, atch)
    atch_band  = _attach_band(atch)
    exp_band   = _expansion_band(exp)
    notes      = _operator_notes(sen_en, sr7, a9v7, a9e7)
    host_str   = hostname if hostname else "unknown"

    L = []

    # ── Header ────────────────────────────────────────────────────────────
    L += [
        "# ACME Funnel Report — GTM Weekly Summary",
        "",
        f"**System:**    {host_str}",
        f"**Window:**    Last 7 days (rolling UTC)",
        f"**Generated:** {ts_str} (UTC)",
        "**Mode:**       Observational analysis only",
        "",
        "---",
        "",
    ]

    # ── Section 1 — Executive Funnel Summary ─────────────────────────────
    L += [
        "## 1. Executive Funnel Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| RadCheck runs (7d) | {rc7} |",
        f"| Sentinel recommended (7d) | {sr7} |",
        f"| Sentinel enabled (present) | {en_label} |",
        f"| Sentinel attach rate | {atch:.3f} |",
        f"| Agent911 views (7d) | {a9v7} |",
        f"| Agent911 expansions (7d) | {a9e7} |",
        f"| Agent911 expansion rate | {exp:.3f} |",
        "",
        f"**Interpretation:** {interp}",
        "",
        "---",
        "",
    ]

    # ── Section 2 — Conversion Health ────────────────────────────────────
    L += [
        "## 2. Conversion Health",
        "",
        f"| Signal | Rate | Band |",
        f"|--------|------|------|",
        f"| Sentinel Attach | {atch:.3f} | {atch_band} |",
        f"| Agent911 Expansion | {exp:.3f} | {exp_band} |",
        "",
        "---",
        "",
    ]

    # ── Section 3 — Operator Notes ────────────────────────────────────────
    L += [
        "## 3. Operator Notes",
        "",
        f"> {notes}",
        "",
        "*Language is observational. No autonomous actions were taken.*",
        "",
        "---",
        "",
    ]

    # ── Footer ────────────────────────────────────────────────────────────
    L += [
        f"*🐐 ACME Agent Supply Co. | Funnel Report v1 | A-FUN-P3-001*",
    ]

    return "\n".join(L) + "\n"


def write_weekly_report(content: str, out_path: str = OUT_GTM_WEEKLY_MD) -> bool:
    """
    Write rendered report to out_path.
    Overwrites on each run (deterministic: same content → same file).
    Returns True on success.
    """
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except Exception:
        return False


def emit_weekly_report_event(rollup: dict, report_hash: str,
                              ops_path: str = SRC_OPS) -> bool:
    """
    Append FUNNEL_WEEKLY_REPORT to ops_events.log.
    Emitted once per run. Append-only.
    Returns True on success.
    """
    record = {
        "ts":             rollup.get("ts", _ts_iso()),
        "event":          EVT_WEEKLY_REPORT,
        "severity":       "INFO",
        "source":         "weekly_rollup",
        "attach_rate":    rollup.get("sentinel_attach_rate",    0.0),
        "expansion_rate": rollup.get("agent911_expansion_rate", 0.0),
        "report_hash":    report_hash,
    }
    return _append_event(ops_path, record)


def generate_weekly_report(
    rollup: dict,
    hostname: str = "",
    md_path: str = OUT_GTM_WEEKLY_MD,
    ops_path: str = SRC_OPS,
) -> tuple:
    """
    High-level helper: render → write md → emit NDJSON event.
    Returns (content: str, report_hash: str, md_ok: bool, evt_ok: bool).
    Exits 0 always.
    """
    import hashlib
    try:
        content     = render_weekly_report(rollup, hostname=hostname)
        report_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        md_ok       = write_weekly_report(content, md_path)
        evt_ok      = emit_weekly_report_event(rollup, report_hash, ops_path)
        return content, report_hash, md_ok, evt_ok
    except Exception:
        return "", "error", False, False


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import time

    import hashlib
    import socket

    t0 = time.monotonic()
    sig     = compute_funnel_signals()
    emitted = emit_funnel_events(sig)
    rollup  = compute_weekly_rollup()
    write_weekly_json(rollup)
    emit_weekly_rollup_event(rollup)
    hostname = socket.gethostname()
    content, report_hash, md_ok, evt_ok = generate_weekly_report(rollup, hostname=hostname)
    elapsed = round((time.monotonic() - t0) * 1000, 2)

    print(f"FUNNEL_OK elapsed={elapsed}ms emitted={emitted}")
    print(f"REPORT  md_ok={md_ok} hash={report_hash[:16]} evt_ok={evt_ok}")
    print("\n--- Dashboard block (24h signals) ---")
    for line in render_funnel_block(sig):
        print(line)
    print("\n--- GTM FUNNEL (7D) ---")
    for line in render_gtm_funnel_block(rollup):
        print(line)
    print("\n--- First 40 lines of gtm_funnel_weekly.md ---")
    for line in content.splitlines()[:40]:
        print(line)

    sys.exit(0)
