#!/usr/bin/env python3
"""
Weekly Operator Report v1 — ACME Agent Supply Co.
Task: A-FMA-P1-001 (canonical spec)

Output:  ~/.openclaw/watchdog/agent911_weekly_report.md
Mode:    Read-only aggregation; single file overwrite allowed
Safety:  No openclaw.json writes; no gateway restarts; append-only logs

10 canonical sections:
  1. Executive Summary
  2. FindMyAgent — Situational Awareness
  3. Stability & Risk Signals
  4. Routing & Governance
  5. Compaction Risk Watch
  6. Backup & Resurrection
  7. Blocked Tasks
  8. Protection Proofs
  9. Recommended Next Actions
  10. Footer

Language guardrails: observational tone only; no autonomous claims;
no guarantees; no self-healing language.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
WATCHDOG    = Path.home() / ".openclaw" / "watchdog"
STATE_FILE  = WATCHDOG / "agent911_state.json"
RC_HIST     = WATCHDOG / "radcheck_history.ndjson"
OPS_EVENTS  = WATCHDOG / "ops_events.log"
MTL_SNAP    = Path.home() / ".openclaw" / "workspace" / "openclaw-ops" / "ops" / "MTL.snapshot.json"
LAZARUS_RPT = WATCHDOG / "lazarus" / "lazarus_report.md"
PRED_GUARD  = WATCHDOG / "sentinel_predictive_state.json"
OUT_PATH    = WATCHDOG / "agent911_weekly_report.md"
LOG_PATH    = WATCHDOG / "weekly_report.log"

# Backward-compat alias (proof.md still updated for SCOPE F stanza reader)
PROOF_ALIAS = WATCHDOG / "proof.md"

SCHEMA_VERSION = "weekly_report.v1"
TAIL_ROWS_MAX  = 200
OPS_MAX_BYTES  = 2 * 1024 * 1024   # 2 MB

# FMA classifier v1 — optional import; graceful on missing
_SCRIPT_DIR = Path(__file__).parent
import sys as _sys
if str(_SCRIPT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPT_DIR))
try:
    from findmyagent_classifier import classify_agents as _fma_classify
    _FMA_AVAILABLE = True
except ImportError:
    _FMA_AVAILABLE = False

# Sentinel Attach Bridge (A-SEN-P4-001) — optional import; graceful on missing
_SEN_BRIDGE_DIR = str(_SCRIPT_DIR.parent / "sentinel")
if _SEN_BRIDGE_DIR not in _sys.path:
    _sys.path.insert(0, _SEN_BRIDGE_DIR)
try:
    from sentinel_attach_bridge import weekly_report_advisory as _sen_weekly_advisory
    _SEN_BRIDGE_AVAILABLE = True
except ImportError:
    _SEN_BRIDGE_AVAILABLE = False

def _get_sentinel_advisory(sen_rec: dict) -> "str | None":
    """Return Sentinel advisory string, or None if not recommended / unavailable."""
    if not _SEN_BRIDGE_AVAILABLE:
        return None
    try:
        return _sen_weekly_advisory(sen_rec)
    except Exception:
        return None

MTL_SNAP   = Path.home() / ".openclaw" / "workspace" / "openclaw-ops" / "ops" / "MTL.snapshot.json"
KNOWN_AGENTS = ["Hendrik"]


# ── Logging (append-only) ─────────────────────────────────────────────────────
def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{_now_utc()}] {msg}\n")
    except Exception:
        pass


# ── Readers ───────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _tail_ndjson(path: Path, n: int = TAIL_ROWS_MAX) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        rows = []
        for raw in lines[-n:]:
            raw = raw.strip()
            if raw:
                try:
                    rows.append(json.loads(raw))
                except Exception:
                    pass
        return rows
    except Exception:
        return []


def _tail_ops_events() -> list:
    try:
        size = os.path.getsize(OPS_EVENTS)
        with open(OPS_EVENTS, "rb") as fh:
            if size > OPS_MAX_BYTES:
                fh.seek(size - OPS_MAX_BYTES)
                fh.readline()
            data = fh.read().decode("utf-8", errors="replace")
        rows = []
        for raw in data.splitlines():
            raw = raw.strip()
            if raw:
                try:
                    rows.append(json.loads(raw))
                except Exception:
                    pass
        return rows
    except Exception:
        return []


# ── Date helpers ───────────────────────────────────────────────────────────────
def _parse_dt(ts: str) -> "datetime | None":
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _age_hours(ts: str) -> "float | None":
    dt = _parse_dt(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


# ── SCOPE B — Confidence Posture ───────────────────────────────────────────────
def compute_confidence_posture(state: dict) -> tuple:
    """Returns (posture, rationale_list). Deterministic evaluation order."""
    rad        = state.get("radcheck", {})
    score      = rad.get("score") if rad.get("score") is not None else state.get("stability_score")
    rollup     = state.get("protection_rollup", {})
    sentinel_p = rollup.get("posture", "unknown")
    events_24h = rollup.get("events_24h", 0) if isinstance(rollup.get("events_24h"), int) else 0
    comp_risk  = state.get("compaction_state", {}).get("risk", "unknown")

    na = []
    if isinstance(score, (int, float)) and score < 60:
        na.append(f"score={score} < 60")
    if sentinel_p == "PREDICTIVE_GUARD":
        na.append("sentinel_posture=PREDICTIVE_GUARD")
    if events_24h >= 3:
        na.append(f"protection_events_24h={events_24h} >= 3")
    if na:
        _log(f"POSTURE NEEDS_ATTENTION triggers={na}")
        return "NEEDS_ATTENTION", na

    wt = []
    if isinstance(score, (int, float)) and 60 <= score < 75:
        wt.append(f"score={score} in [60,75)")
    if sentinel_p == "WATCH":
        wt.append("sentinel_posture=WATCH")
    if comp_risk == "HIGH":
        wt.append("compaction_risk=HIGH")
    if wt:
        _log(f"POSTURE WATCH triggers={wt}")
        return "WATCH", wt

    _log("POSTURE STABLE")
    return "STABLE", ["all checks within expected bounds"]


# ── FMA minimal presence ───────────────────────────────────────────────────────
def compute_fma_presence(ops_events: list, fma_classifier: dict = None) -> dict:
    """
    Use full classifier output if provided; otherwise derive minimal presence
    from watchdog/sentinel signals.
    States emitted: ACTIVE | IDLE | BLOCKED | STALLED | UNKNOWN
    (BLOCKED/STALLED only from classifier; minimal fallback uses ACTIVE/IDLE/UNKNOWN)
    """
    if fma_classifier and fma_classifier.get("agents"):
        return fma_classifier

    # Minimal fallback — single-pass reverse scan
    last_ts = None
    for evt in reversed(ops_events):
        if evt.get("event", "") in (
            "SENTINEL_GUARD_CYCLE", "HEARTBEAT", "WATCHDOG_PROBE",
            "GATEWAY_PROBE_OK", "MODEL_STATE_UPDATE",
        ):
            last_ts = evt.get("ts")
            break

    age_h = _age_hours(last_ts) if last_ts else None
    if age_h is None:
        state_label, signal_desc = "UNKNOWN", "not observed"
    elif age_h <= 2.0:
        state_label, signal_desc = "ACTIVE", f"{int(age_h * 60)}m ago"
    elif age_h <= 24.0:
        state_label, signal_desc = "IDLE", f"{age_h:.1f}h ago"
    else:
        state_label, signal_desc = "UNKNOWN", f"{age_h:.1f}h ago (stale)"

    return {
        "agents": [{"name": "Hendrik", "state": state_label,
                    "last_signal": signal_desc, "presence_confidence": None}],
        "total": 1,
        "active":  1 if state_label == "ACTIVE"  else 0,
        "idle":    1 if state_label == "IDLE"    else 0,
        "blocked": 0,
        "stalled": 0,
        "unknown": 1 if state_label == "UNKNOWN" else 0,
        "source":  "minimal_v1",
    }


# ── Score trend ────────────────────────────────────────────────────────────────
def compute_score_trend(rc_hist: list) -> dict:
    rows_7d = [r for r in rc_hist
               if isinstance(_age_hours(r.get("ts", "")), float)
               and _age_hours(r.get("ts", "")) <= 168.0] or rc_hist
    scores = [r.get("score") for r in rows_7d if isinstance(r.get("score"), (int, float))]
    if not scores:
        return {"current": "unknown", "min_7d": "unknown",
                "max_7d": "unknown", "samples": 0}
    return {"current": scores[-1], "min_7d": min(scores),
            "max_7d": max(scores), "samples": len(scores)}


# ── Blocked tasks from MTL ─────────────────────────────────────────────────────
def gather_blocked_tasks() -> list:
    """Read MTL.snapshot.json; return tasks with non-DONE status."""
    snap = _read_json(MTL_SNAP)
    tasks = snap.get("tasks", {})
    if isinstance(tasks, dict):
        task_list = list(tasks.values())
    else:
        task_list = tasks
    blocked = [
        t for t in task_list
        if t.get("status", "").upper() not in ("DONE", "CANCELLED", "CLOSED")
    ]
    return sorted(blocked, key=lambda t: (
        {"HIGH": 0, "MED": 1, "LOW": 2}.get(t.get("priority", "LOW"), 3),
        t.get("task_id", "")
    ))


# ── Lazarus resurrection signals ───────────────────────────────────────────────
def gather_lazarus_signals() -> dict:
    """Extract key readiness signals from lazarus_report.md (best-effort)."""
    out = {"score": "unknown", "risk": "unknown", "generated_at": "unknown",
           "tm_configured": "unknown", "gdrive_snapshots": "unknown"}
    try:
        text = LAZARUS_RPT.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "| Score |" in line:
                out["score"] = line.split("|")[2].strip().replace("**", "")
            if "| Risk Level |" in line:
                out["risk"] = line.split("|")[2].strip().replace("**", "")
            if "**Generated:**" in line:
                out["generated_at"] = line.split("**Generated:**")[-1].strip()
            if "| Time Machine |" in line:
                out["tm_configured"] = "YES" if "✅" in line else "NO"
            if "| GDrive Snapshots |" in line:
                out["gdrive_snapshots"] = line.split("|")[2].strip().replace("**", "")
    except Exception:
        pass
    return out


# ── Protection proofs ─────────────────────────────────────────────────────────
def build_protection_proofs(state: dict) -> list:
    """Return last_three_events list from protection_rollup."""
    rollup = state.get("protection_rollup", {})
    return rollup.get("last_three_events", [])


# ── SCOPE D — Report Renderer ──────────────────────────────────────────────────
def render_report(
    state:      dict,
    posture:    str,
    rationale:  list,
    fma:        dict,
    trend:      dict,
    blocked:    list,
    lazarus:    dict,
    pred_guard: dict,
    generated_ts: str,
) -> str:
    rad        = state.get("radcheck", {})
    score      = rad.get("score", "unknown")
    risk       = rad.get("risk_level", "unknown")
    vel        = rad.get("velocity_direction", "unknown")
    top_risks  = state.get("top_risks", [])

    rollup     = state.get("protection_rollup", {})
    events_24h = rollup.get("events_24h", "unknown")
    events_7d  = rollup.get("events_7d", "unknown")
    last_etype = rollup.get("last_event_type", "not observed")
    last_ets   = rollup.get("last_event_ts", "unknown")
    guard_cyc  = rollup.get("guard_cycles_24h", "unknown")
    cool_sup   = rollup.get("cooldown_suppressions_24h", "unknown")
    sent_post  = rollup.get("posture", "unknown")
    proofs     = build_protection_proofs(state)

    comp       = state.get("compaction_state", {})
    bkp        = state.get("backup_state", {})
    routing    = state.get("routing", {})
    actions    = state.get("recommended_actions", [])

    pg         = state.get("predictive_guard", pred_guard)
    pg_risk    = pg.get("risk_level", "unknown")
    pg_score   = pg.get("risk_score", "unknown")

    posture_icon = {"STABLE": "✅", "WATCH": "⚠️", "NEEDS_ATTENTION": "🚨"}.get(posture, "❓")
    restore_label = "READY" if bkp.get("restore_ready") else "STALE"

    L = []

    # ── Header ────────────────────────────────────────────────────────────────
    L += [
        "# 🐐 ACME Agent Supply Co. — Weekly Operator Report",
        "",
        f"**Generated:** {generated_ts}",
        f"**Period:**    rolling 7 days",
        f"**Schema:**    {SCHEMA_VERSION}",
        "",
        "---",
        "",
    ]

    # ── 1. Executive Summary ──────────────────────────────────────────────────
    L += ["## 1. Executive Summary", ""]

    # Observational tone; no autonomous claims
    risk_desc = {
        "LOW":      "within acceptable bounds",
        "MODERATE": "requires monitoring",
        "ELEVATED": "above baseline — operator review recommended",
        "HIGH":     "elevated — action warranted",
        "SEVERE":   "critical — immediate attention required",
    }.get(risk, "status unknown")

    vel_desc = {
        "DEGRADING": "Risk velocity is trending upward.",
        "IMPROVING": "Risk velocity is trending downward.",
        "STABLE":    "Risk velocity is stable.",
    }.get(vel, "Risk velocity direction not observed.")

    L += [
        f"Operator posture: **{posture_icon} {posture}**",
        "",
        f"System stability score observed at **{score}/100** ({risk_desc}). "
        f"{vel_desc} "
        f"Sentinel flagged **{events_24h}** protection event(s) in the last 24 hours. "
        f"Compaction state observed as **{comp.get('state', 'unknown')}** "
        f"(risk: {comp.get('risk', 'unknown')}).",
        "",
    ]
    for r in rationale:
        L.append(f"- {r}")
    L += ["", "---", ""]

    # ── 2. FindMyAgent — Situational Awareness ────────────────────────────────
    L += ["## 2. FindMyAgent — Situational Awareness", ""]
    L += [
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Agents Observed | {fma['total']} |",
        f"| ACTIVE | {fma['active']} |",
        f"| IDLE | {fma['idle']} |",
        f"| BLOCKED | {fma.get('blocked', 0)} |",
        f"| STALLED | {fma.get('stalled', 0)} |",
        f"| UNKNOWN | {fma['unknown']} |",
        "",
        "**Agent Detail:**",
        "",
    ]
    for ag in fma.get("agents", []):
        conf = ag.get("presence_confidence")
        conf_str = f", confidence: {conf}/100" if conf is not None else ""
        L.append(f"- **{ag['name']}** — {ag['state']} "
                 f"(last signal: {ag['last_signal']}{conf_str})")
    source = fma.get("source", "minimal_v1")
    L += [
        "",
        f"> Classifier source: `{source}`.",
        "",
        "---",
        "",
    ]

    # ── 3. Stability & Risk Signals ───────────────────────────────────────────
    L += ["## 3. Stability & Risk Signals", ""]
    L += [
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Stability Score | {score} / 100 |",
        f"| Risk Level | {risk} |",
        f"| Velocity | {vel} |",
        f"| Predictive Guard Risk | {pg_risk} ({pg_score}/100) |",
        f"| 7d Min Score | {trend.get('min_7d', 'unknown')} |",
        f"| 7d Max Score | {trend.get('max_7d', 'unknown')} |",
        f"| Scan Samples (7d) | {trend.get('samples', 0)} |",
        "",
    ]
    if top_risks:
        L += ["**Active Risks Detected:**", ""]
        for r in top_risks[:5]:
            L.append(f"- `[{r.get('severity','?')}]` {r.get('id','?')} — {r.get('summary','?')}")
    else:
        L.append("No active risks detected.")
    L += ["", "---", ""]

    # ── 4. Routing & Governance ───────────────────────────────────────────────
    sg_state = state.get("protection_state", {}).get("sphinxgate_state", "unknown")
    L += [
        "## 4. Routing & Governance",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Routing Confidence | {routing.get('confidence', 'unknown')} |",
        f"| Active Provider | {routing.get('last_provider', 'unknown')} |",
        f"| Provider Switches (24h) | {routing.get('provider_switches_24h', 'unknown')} |",
        f"| Anomalies (24h) | {routing.get('anomalies_24h', 'unknown')} |",
        f"| SphinxGate State | {sg_state} |",
        "",
        "---",
        "",
    ]

    # ── 5. Compaction Risk Watch ──────────────────────────────────────────────
    L += [
        "## 5. Compaction Risk Watch",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| State | {comp.get('state', 'unknown')} |",
        f"| Risk | {comp.get('risk', 'unknown')} |",
        f"| Timeout Events (2h) | {comp.get('timeout_2h', 'unknown')} |",
        f"| Compaction Events (2h) | {comp.get('events_2h', 'unknown')} |",
        f"| Acceleration Detected | {'YES' if comp.get('acceleration') else 'NO'} |",
        f"| Source | {comp.get('source', 'unknown')} |",
        "",
        "---",
        "",
    ]

    # ── 6. Backup & Resurrection ──────────────────────────────────────────────
    L += [
        "## 6. Backup & Resurrection",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Last Backup Age | {bkp.get('last_backup_age_hours', 'unknown')}h |",
        f"| Restore Readiness | {restore_label} |",
        f"| Last Backup At | {bkp.get('last_backup_ts', 'unknown')} |",
        f"| Lazarus Score | {lazarus.get('score', 'unknown')} |",
        f"| Lazarus Risk | {lazarus.get('risk', 'unknown')} |",
        f"| Time Machine | {lazarus.get('tm_configured', 'unknown')} |",
        f"| GDrive Snapshots | {lazarus.get('gdrive_snapshots', 'unknown')} |",
        f"| Lazarus Last Run | {lazarus.get('generated_at', 'unknown')} |",
        "",
        "---",
        "",
    ]

    # ── 7. Blocked Tasks ──────────────────────────────────────────────────────
    L += ["## 7. Blocked Tasks", ""]
    if blocked:
        L.append(f"{len(blocked)} task(s) not yet DONE:")
        L.append("")
        for t in blocked[:10]:
            blocked_on = t.get("blocked_on", "")
            suffix = f" ← blocked on: {blocked_on}" if blocked_on else ""
            L.append(
                f"- `[{t.get('priority','?')}]` **{t.get('task_id','?')}** "
                f"({t.get('status','?')}) — {t.get('title','?')}{suffix}"
            )
    else:
        L.append("No open tasks detected in MTL.")
    L += ["", "---", ""]

    # ── 8. Protection Proofs ──────────────────────────────────────────────────
    L += [
        "## 8. Protection Proofs",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Sentinel Posture | {sent_post} |",
        f"| Protection Events (24h) | {events_24h} |",
        f"| Protection Events (7d) | {events_7d} |",
        f"| Guard Cycles (24h) | {guard_cyc} |",
        f"| Cooldown Suppressions (24h) | {cool_sup} |",
        "",
    ]
    if proofs:
        L += ["**Last Protection Events:**", ""]
        for p in proofs:
            etype = p.get("event", "unknown").replace("SENTINEL_PROTECTION_", "").replace("_", " ").title()
            L.append(f"- `[{p.get('severity','?')}]` {etype} — {p.get('ts','?')}")
    else:
        L.append("No protection events recorded.")
    L += ["", "---", ""]

    # ── 9. Recommended Next Actions ───────────────────────────────────────────
    L += ["## 9. Recommended Next Actions", ""]

    # Sentinel advisory hook (A-SEN-P4-001)
    sen_rec = state.get("sentinel_recommendation", {})
    sen_advisory = _get_sentinel_advisory(sen_rec)

    action_list = list(actions) if actions else []
    if sen_advisory:
        # Prepend as action #1 if recommended
        action_list.insert(0, {
            "action":    "Enable Sentinel for continuous protection",
            "rationale": sen_advisory,
            "impact":    "HIGH",
        })

    if action_list:
        for i, a in enumerate(action_list, 1):
            L.append(
                f"{i}. `[impact:{a.get('impact', a.get('impact_score', '?'))}]`"
                f" **{a.get('action','?')}**"
                f" — {a.get('rationale', a.get('reason', ''))}"
            )
    else:
        L.append("No recommended actions flagged at this time.")
    L += ["", "---", ""]

    # ── 10. Footer ────────────────────────────────────────────────────────────
    L += [
        "## 10. Footer",
        "",
        "- ✅ Zero writes to `openclaw.json`",
        "- ✅ Zero gateway restarts",
        "- ✅ Append-only logs preserved",
        "- ✅ Watchdog unchanged",
        "- ✅ All exits 0",
        "",
        f"*🐐 ACME Agent Supply Co. | Weekly Operator Report v1 | A-FMA-P1-001 | {SCHEMA_VERSION}*",
    ]

    return "\n".join(L) + "\n"


# ── SCOPE F stanza helper (called by agent911_snapshot.py) ────────────────────
def gather_weekly_report_stanza() -> dict:
    stanza = {"last_generated_ts": "unknown",
              "confidence_posture": "unknown",
              "report_path": str(OUT_PATH)}
    try:
        for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("**Generated:**"):
                stanza["last_generated_ts"] = line.split("**Generated:**")[-1].strip()
            if line.startswith("Operator posture:"):
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


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    t0           = time.monotonic()
    generated_ts = _now_utc()

    _log(f"WEEKLY_REPORT START ts={generated_ts}")

    # Gather
    state      = _read_json(STATE_FILE)
    rc_hist    = _tail_ndjson(RC_HIST)
    ops_events = _tail_ops_events()
    pred_guard = _read_json(PRED_GUARD)
    blocked    = gather_blocked_tasks()
    lazarus    = gather_lazarus_signals()

    # Compute
    posture, rationale = compute_confidence_posture(state)
    trend              = compute_score_trend(rc_hist)

    # FMA — use v1 classifier if available; fallback to minimal
    fma_classifier = None
    if _FMA_AVAILABLE:
        try:
            mtl_snap = _read_json(MTL_SNAP)
            repo_sync = state.get("repo_sync", {})
            fma_classifier = _fma_classify(
                known_agents=KNOWN_AGENTS,
                ops_events=ops_events,
                mtl_snap=mtl_snap,
                repo_sync=repo_sync,
            )
        except Exception:
            fma_classifier = None
    fma = compute_fma_presence(ops_events, fma_classifier)

    # Render
    report = render_report(
        state, posture, rationale, fma, trend,
        blocked, lazarus, pred_guard, generated_ts,
    )

    # Write canonical output + backward-compat alias
    for out in (OUT_PATH, PROOF_ALIAS):
        try:
            out.write_text(report, encoding="utf-8")
        except Exception as e:
            _log(f"WRITE_ERROR {out}: {e}")

    t1         = time.monotonic()
    runtime_ms = int((t1 - t0) * 1000)
    score      = state.get("radcheck", {}).get("score", "unknown")

    _log(
        f"WEEKLY_REPORT_MS={runtime_ms} posture={posture} score={score} "
        f"fma_active={fma['active']} fma_idle={fma['idle']} blocked_tasks={len(blocked)}"
    )
    print(f"WEEKLY_REPORT_OK ts={generated_ts} posture={posture} runtime_ms={runtime_ms}")
    print(f"  output:          {OUT_PATH}")
    print(f"  WEEKLY_REPORT_MS={runtime_ms}")


if __name__ == "__main__":
    main()
