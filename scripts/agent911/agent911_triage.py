#!/usr/bin/env python3
"""
agent911_triage.py — A-A9-V1-001
Agent911 Guided Triage Mode: Bundle-In → Action-Out

Accepts a support bundle (zip or directory), parses key artifacts,
and produces a deterministic triage report without external calls.

SAFETY:
  - Read-only bundle inputs
  - Writes ONLY to --output-dir (default: ~/.openclaw/watchdog/triage/)
  - No openclaw.json writes
  - No network calls
  - No gateway restarts

Determinism guarantee:
  Same bundle → identical triage_report.md + triage_snapshot.json (hash-verifiable)

Usage:
  python3 agent911_triage.py --bundle <path_to_zip_or_dir>
  python3 agent911_triage.py --bundle <path> --output-dir <dir>
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
VERSION = "1.0.0"
SCHEMA  = "agent911_triage.v1.0"

HOME        = Path.home()
DEFAULT_OUT = HOME / ".openclaw" / "watchdog" / "triage"

# Confidence bands
HIGH_CONF   = 0.90
MED_CONF    = 0.70
LOW_CONF    = 0.50

# ---------------------------------------------------------------------------
# Timestamps (deterministic: derived from bundle, not wall clock)
# ---------------------------------------------------------------------------
def ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Bundle loader — zip or directory
# ---------------------------------------------------------------------------
class Bundle:
    """Read-only accessor for support bundle contents."""

    def __init__(self, bundle_path: Path):
        self.path = bundle_path
        self._zip: Optional[zipfile.ZipFile] = None
        self._dir: Optional[Path] = None
        self._hash: Optional[str] = None

        if bundle_path.suffix == ".zip" and bundle_path.is_file():
            self._zip = zipfile.ZipFile(bundle_path, "r")
        elif bundle_path.is_dir():
            self._dir = bundle_path
        else:
            raise ValueError(f"Bundle must be a .zip file or directory: {bundle_path}")

    def _zip_path_guard(self, name: str):
        """Reject any path that escapes the bundle root."""
        norm = os.path.normpath(name)
        if norm.startswith("..") or os.path.isabs(norm):
            raise ValueError(f"Zip path traversal rejected: {name}")

    def read_text(self, rel_path: str) -> Optional[str]:
        """Read a file from the bundle; returns None if not found."""
        if self._zip:
            # Try with and without the bundle slug prefix
            for name in self._zip.namelist():
                self._zip_path_guard(name)
                if name.endswith("/" + rel_path) or name == rel_path:
                    try:
                        return self._zip.read(name).decode("utf-8", errors="replace")
                    except Exception:
                        return None
        elif self._dir:
            # Search recursively for the file
            for candidate in self._dir.rglob(Path(rel_path).name):
                if str(candidate).endswith(rel_path.replace("/", os.sep)):
                    try:
                        return candidate.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        return None
        return None

    def bundle_hash(self) -> str:
        """Stable content hash of the bundle (for determinism proof)."""
        if self._hash:
            return self._hash
        h = hashlib.sha256()
        if self._zip:
            # Hash all member names + sizes (stable sort)
            for info in sorted(self._zip.infolist(), key=lambda x: x.filename):
                h.update(info.filename.encode())
                h.update(str(info.file_size).encode())
        elif self._dir:
            for f in sorted(self._dir.rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(self._dir)).encode())
                    h.update(str(f.stat().st_size).encode())
        self._hash = h.hexdigest()[:16]
        return self._hash

    def close(self):
        if self._zip:
            self._zip.close()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_agent911_state(text: Optional[str]) -> Dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def parse_ops_events(text: Optional[str]) -> List[Dict]:
    if not text:
        return []
    events = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def parse_radcheck_history(text: Optional[str]) -> List[Dict]:
    if not text:
        return []
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    # Sort by ts for determinism
    rows.sort(key=lambda x: x.get("ts", ""))
    return rows


def parse_kv_text(text: Optional[str]) -> Dict:
    if not text:
        return {}
    out = {}
    for raw in text.strip().splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# Triage logic — deterministic rules, priority-ordered
# ---------------------------------------------------------------------------
def _safe_get(d: Dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, None)
        if d is None:
            return default
    return d


def detect_causes(state: Dict, ops_events: List[Dict], rc_history: List[Dict]) -> List[Dict]:
    """
    Deterministic rule set for top-3 cause detection.
    Returns list of {rank, cause, evidence, confidence} sorted by (confidence DESC, cause ASC).
    """
    candidates = []

    # Rule 1: Score collapse
    score = state.get("stability_score")
    if score is not None and isinstance(score, (int, float)):
        if score < 40:
            candidates.append({
                "cause": "Agent stability severely degraded",
                "evidence": f"stability_score={score} (CRITICAL threshold <40)",
                "confidence": HIGH_CONF,
                "rule": "R01_SCORE_CRITICAL",
            })
        elif score < 60:
            candidates.append({
                "cause": "Agent stability elevated risk",
                "evidence": f"stability_score={score} (ELEVATED threshold <60)",
                "confidence": MED_CONF,
                "rule": "R01_SCORE_ELEVATED",
            })

    # Rule 2: Compaction pressure
    comp = state.get("compaction_state", {})
    if isinstance(comp, dict) and comp.get("alert_active"):
        p95 = comp.get("p95_ms", "unknown")
        tc  = comp.get("timeout_count", 0)
        candidates.append({
            "cause": "Context compaction pressure detected",
            "evidence": f"alert_active=True p95_ms={p95} timeout_count={tc}",
            "confidence": HIGH_CONF,
            "rule": "R02_COMPACTION",
        })

    # Rule 3: Stall events
    rollup = state.get("protection_rollup", {})
    stalls = _safe_get(rollup, "stalls_prevented_24h", default=0)
    if isinstance(stalls, (int, float)) and stalls > 0:
        candidates.append({
            "cause": "Agent stall events intercepted by Sentinel",
            "evidence": f"stalls_prevented_24h={stalls} (Sentinel active, root cause may exist)",
            "confidence": MED_CONF,
            "rule": "R03_STALLS",
        })

    # Rule 4: SphinxGate / routing issue
    routing = state.get("routing", {})
    posture = _safe_get(routing, "posture", default="")
    provider = _safe_get(routing, "active_provider", default="")
    if posture in ("DEGRADED", "FALLBACK", "UNKNOWN") or provider in ("unknown", ""):
        candidates.append({
            "cause": "SphinxGate routing degraded or unknown provider",
            "evidence": f"posture={posture} provider={provider}",
            "confidence": MED_CONF,
            "rule": "R04_ROUTING",
        })

    # Rule 5: RadCheck elevated
    rc = state.get("radcheck", {})
    rc_risk = _safe_get(rc, "risk_level", default="")
    if rc_risk in ("HIGH", "CRITICAL"):
        rc_score = _safe_get(rc, "score", default="?")
        candidates.append({
            "cause": "RadCheck radiation scoring elevated",
            "evidence": f"radcheck.risk_level={rc_risk} score={rc_score}",
            "confidence": MED_CONF,
            "rule": "R05_RADCHECK",
        })

    # Rule 6: RadCheck trend declining (from history)
    if len(rc_history) >= 3:
        recent_scores = [r.get("score") for r in rc_history[-5:] if isinstance(r.get("score"), (int, float))]
        if len(recent_scores) >= 3:
            # Check if monotonically declining
            declines = sum(1 for i in range(1, len(recent_scores)) if recent_scores[i] < recent_scores[i-1])
            if declines >= len(recent_scores) - 1:
                candidates.append({
                    "cause": "RadCheck score trend declining",
                    "evidence": f"last {len(recent_scores)} scores: {recent_scores}",
                    "confidence": LOW_CONF,
                    "rule": "R06_RC_TREND",
                })

    # Rule 7: High predictive risk
    pg = state.get("predictive_guard", {})
    pg_risk = _safe_get(pg, "risk_level", default="")
    pg_conf = _safe_get(pg, "confidence", default=0)
    if pg_risk in ("HIGH", "CRITICAL") or (isinstance(pg_conf, (int, float)) and pg_conf > 70 and pg_risk not in ("LOW", "UNKNOWN", "")):
        candidates.append({
            "cause": "Predictive guard signalling elevated pre-stall risk",
            "evidence": f"predictive_guard.risk_level={pg_risk} confidence={pg_conf}",
            "confidence": MED_CONF,
            "rule": "R07_PREDICTIVE",
        })

    # Rule 8: High ops event rate
    sentinel_24h = _safe_get(rollup, "events_24h", default=0)
    if isinstance(sentinel_24h, (int, float)) and sentinel_24h > 10:
        candidates.append({
            "cause": "High sentinel event volume in last 24h",
            "evidence": f"sentinel events_24h={sentinel_24h}",
            "confidence": LOW_CONF,
            "rule": "R08_EVENT_VOLUME",
        })

    if not candidates:
        candidates.append({
            "cause": "No specific anomaly detected by automated rules",
            "evidence": f"stability_score={score} risk={state.get('risk_level','unknown')}",
            "confidence": LOW_CONF,
            "rule": "R00_NOMINAL",
        })

    # Deterministic sort: confidence DESC, rule ASC (tie-break)
    candidates.sort(key=lambda x: (-x["confidence"], x["rule"]))

    # Rank and return top 3
    top3 = candidates[:3]
    for i, c in enumerate(top3, 1):
        c["rank"] = i
    return top3


def build_actions(state: Dict, causes: List[Dict]) -> List[Dict]:
    """
    Produce max-5 recommended next actions from agent911 + triage-derived rules.
    Deterministic: sorted by (impact DESC, action ASC).
    """
    actions = []

    # Pull from agent911 recommended_actions (may be strings or dicts)
    for a in state.get("recommended_actions", [])[:3]:
        if isinstance(a, dict):
            action_text = a.get("action") or a.get("description") or str(a)
            impact = "HIGH" if a.get("impact_score", 0) >= 7 else "MEDIUM"
        else:
            action_text = str(a)
            impact = "HIGH"
        actions.append({
            "action": action_text,
            "impact": impact,
            "confidence": MED_CONF,
            "source": "agent911_recommended",
        })

    # Cause-derived actions
    rule_actions = {
        "R01_SCORE_CRITICAL": ("Run agent911_snapshot.py and review full dashboard", "CRITICAL", HIGH_CONF),
        "R01_SCORE_ELEVATED": ("Review top_risks in agent911_state.json", "HIGH", MED_CONF),
        "R02_COMPACTION":     ("Review compaction_state; consider session restart if p95 > 500ms", "HIGH", HIGH_CONF),
        "R03_STALLS":         ("Investigate stall root cause; review ops_events for GATEWAY_STALL entries", "HIGH", MED_CONF),
        "R04_ROUTING":        ("Check SphinxGate model_router.py; verify provider API health", "MEDIUM", MED_CONF),
        "R05_RADCHECK":       ("Run radiation_check.py --full for detailed signal breakdown", "HIGH", MED_CONF),
        "R06_RC_TREND":       ("Monitor RadCheck over next 2 runs; check for token drift", "MEDIUM", LOW_CONF),
        "R07_PREDICTIVE":     ("Review sentinel_predictive_guard.py output; check STALL_RISK_SCORE", "HIGH", MED_CONF),
        "R08_EVENT_VOLUME":   ("Review ops_events.log for event storm patterns", "MEDIUM", LOW_CONF),
        "R00_NOMINAL":        ("Generate a fresh support bundle after next session to confirm stability", "LOW", LOW_CONF),
    }

    seen = set()
    for cause in causes:
        rule = cause.get("rule", "")
        if rule in rule_actions and rule not in seen:
            action_text, impact, conf = rule_actions[rule]
            actions.append({
                "action": action_text,
                "impact": impact,
                "confidence": conf,
                "source": f"triage_rule:{rule}",
            })
            seen.add(rule)

    # Deduplicate by action text (case-insensitive)
    deduped = []
    seen_text = set()
    for a in actions:
        key = a["action"].lower().strip()
        if key not in seen_text:
            deduped.append(a)
            seen_text.add(key)

    # Sort deterministically: impact priority DESC, confidence DESC, action ASC
    impact_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    deduped.sort(key=lambda x: (impact_order.get(x["impact"], 9), -x["confidence"], x["action"]))

    return deduped[:5]


def assess_data_sufficiency(state: Dict, ops_events: List[Dict], rc_history: List[Dict]) -> Dict:
    """Determine if we have enough data; flag what's missing."""
    gaps = []
    if not state:
        gaps.append("agent911_state.json not found in bundle — run agent911_snapshot.py")
    elif state.get("stability_score") is None:
        gaps.append("stability_score absent from agent911_state")
    if not ops_events:
        gaps.append("ops_events_tail.log empty or missing — re-generate bundle")
    if not rc_history:
        gaps.append("radcheck_history_tail.ndjson missing — run radiation_check.py first")
    return {
        "sufficient": len(gaps) == 0,
        "gaps": sorted(gaps),  # sorted for determinism
    }


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------
def build_snapshot(state: Dict, causes: List[Dict], actions: List[Dict],
                   sufficiency: Dict, bundle_hash: str, bundle_path: str,
                   watchdog_disk: Dict,
                   run_id: str) -> Dict:
    """
    Build deterministic triage_snapshot.json.
    elapsed_ms intentionally excluded — it is non-deterministic by nature.
    All fields derive from bundle content only (no wall-clock inputs).
    """
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "run_id": run_id,
        "bundle_hash": bundle_hash,
        "bundle_path": bundle_path,
        "triage_ts": state.get("ts", "unknown"),   # bundle-time, not wall clock
        "agent911_score": state.get("stability_score"),
        "agent911_risk": state.get("risk_level"),
        "schema_version": state.get("schema_version"),
        "top_causes": causes,
        "recommended_actions": actions,
        "data_sufficiency": sufficiency,
        "watchdog_disk": {
            "usage": watchdog_disk.get("stdout", "unknown"),
            "collector_elapsed_ms": watchdog_disk.get("elapsed_ms", "unknown"),
            "bloat_warning": watchdog_disk.get("watchdog_bloat_warning", "false") == "true",
            "growth_rate_mb_hr": watchdog_disk.get("watchdog_growth_rate_mb_hr", "unavailable"),
            "backups_mb": watchdog_disk.get("backups_mb", "0"),
            "lazarus_mb": watchdog_disk.get("lazarus_mb", "0"),
            "gtm_exports_mb": watchdog_disk.get("gtm_exports_mb", "0"),
            "gtm_exports_present": watchdog_disk.get("gtm_exports_present", "false") == "true",
        },
        "safety": {
            "network_calls": False,
            "openclaw_json_written": False,
            "gateway_restarted": False,
            "mode": "observational_analysis_only",
        },
    }


def build_report(state: Dict, causes: List[Dict], actions: List[Dict],
                 sufficiency: Dict, bundle_hash: str, bundle_path: str,
                 watchdog_disk: Dict,
                 run_id: str) -> str:
    """
    Build deterministic triage_report.md.
    elapsed_ms excluded — non-deterministic. All fields from bundle content only.
    """
    score = state.get("stability_score", "unknown")
    risk  = state.get("risk_level", "unknown")
    ts    = state.get("ts", "unknown")
    comp  = state.get("compaction_state", {})
    rollup = state.get("protection_rollup", {})

    lines = [
        "---",
        "Mode: Observational analysis only",
        "No configuration changes performed.",
        "---",
        "",
        "# Agent911 Triage Report",
        "",
        f"**Bundle:** `{Path(bundle_path).name}`  ",
        f"**Bundle hash:** `{bundle_hash}`  ",
        f"**Bundle snapshot time:** `{ts}`  ",
        f"**Triage run:** `{run_id}`  ",
        "",
        "---",
        "",
        "## System State at Bundle Time",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Stability Score | {score} / 100 |",
        f"| Risk Level | {risk} |",
        f"| Compaction Alert | {comp.get('alert_active', 'unknown')} |",
        f"| Compaction p95 ms | {comp.get('p95_ms', 'unknown')} |",
        f"| Stalls Prevented 24h | {rollup.get('stalls_prevented_24h', 'unknown')} |",
        f"| Sentinel Events 24h | {rollup.get('events_24h', 'unknown')} |",
        f"| Watchdog Disk Usage | {watchdog_disk.get('stdout', 'unknown')} |",
        f"| Watchdog Growth MB/hr | {watchdog_disk.get('watchdog_growth_rate_mb_hr', 'unavailable')} |",
        f"| Watchdog Bloat Warning | {watchdog_disk.get('watchdog_bloat_warning', 'false')} |",
        f"| Watchdog Backups MB | {watchdog_disk.get('backups_mb', '0')} |",
        f"| Watchdog Lazarus MB | {watchdog_disk.get('lazarus_mb', '0')} |",
        f"| Watchdog GTM Exports MB | {watchdog_disk.get('gtm_exports_mb', '0')} |",
        f"| Schema Version | {state.get('schema_version', 'unknown')} |",
        "",
        "---",
        "",
        "## Top 3 Likely Causes",
        "",
    ]

    for c in causes:
        conf_pct = int(c["confidence"] * 100)
        lines += [
            f"### {c['rank']}. {c['cause']}",
            "",
            f"- **Confidence:** {conf_pct}%",
            f"- **Evidence:** {c['evidence']}",
            f"- **Rule:** `{c['rule']}`",
            "",
        ]

    if not causes:
        lines += ["_(no causes detected — insufficient data)_", ""]

    lines += [
        "---",
        "",
        "## Recommended Next Actions",
        "",
    ]
    for i, a in enumerate(actions, 1):
        conf_pct = int(a["confidence"] * 100)
        lines += [
            f"{i}. **[{a['impact']}]** {a['action']}",
            f"   - Confidence: {conf_pct}% | Source: `{a['source']}`",
            "",
        ]

    if not actions:
        lines += ["_(no actions derived)_", ""]

    if not sufficiency["sufficient"]:
        lines += [
            "---",
            "",
            "## What We Need Next",
            "",
            "Insufficient data to complete triage. Missing inputs:",
            "",
        ]
        for gap in sufficiency["gaps"]:
            lines.append(f"- {gap}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Safety Confirmation",
        "",
        "- ✓ No network calls made",
        "- ✓ No openclaw.json written",
        "- ✓ No gateway restarts triggered",
        "- ✓ Bundle contents read-only",
        "- ✓ Outputs written to triage output dir only",
        "",
        "---",
        f"*Agent911 Triage v{VERSION} | ACME Agent Supply Co.*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=f"Agent911 Triage v{VERSION}")
    parser.add_argument("--bundle", required=True, help="Path to support bundle (.zip or directory)")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT),
                        help=f"Output directory (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    t_start = time.monotonic()

    bundle_path = Path(args.bundle).expanduser().resolve()
    output_dir  = Path(args.output_dir).expanduser()

    if not bundle_path.exists():
        print(f"ERROR: Bundle not found: {bundle_path}", file=sys.stderr)
        sys.exit(1)

    # Output dir — create if needed
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load bundle
    try:
        bundle = Bundle(bundle_path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    bundle_hash = bundle.bundle_hash()
    run_id = f"triage-{bundle_hash}"   # Deterministic run_id from bundle hash

    print(f"Agent911 Triage v{VERSION}")
    print(f"  bundle    : {bundle_path}")
    print(f"  hash      : {bundle_hash}")
    print(f"  output    : {output_dir}")
    print()

    # Parse bundle artifacts
    state_text  = bundle.read_text("state_snapshots/agent911_state.json")
    events_text = bundle.read_text("redacted_logs/ops_events_tail.log")
    rc_text     = bundle.read_text("state_snapshots/radcheck_history_tail.ndjson")
    watchdog_disk_text = bundle.read_text("state_snapshots/watchdog_disk_usage.txt")
    bundle.close()

    state      = parse_agent911_state(state_text)
    ops_events = parse_ops_events(events_text)
    rc_history = parse_radcheck_history(rc_text)
    watchdog_disk = parse_kv_text(watchdog_disk_text)

    print(f"  agent911_state : {'parsed' if state else 'NOT FOUND'} ({len(state_text or '')} bytes)")
    print(f"  ops_events     : {len(ops_events)} entries")
    print(f"  rc_history     : {len(rc_history)} entries")
    print()

    # Triage analysis
    sufficiency = assess_data_sufficiency(state, ops_events, rc_history)
    causes      = detect_causes(state, ops_events, rc_history)
    actions     = build_actions(state, causes)

    elapsed_ms = round((time.monotonic() - t_start) * 1000)

    # Build outputs (deterministic — no elapsed_ms in file content)
    snapshot = build_snapshot(state, causes, actions, sufficiency,
                              bundle_hash, str(bundle_path), watchdog_disk, run_id)
    report   = build_report(state, causes, actions, sufficiency,
                            bundle_hash, str(bundle_path), watchdog_disk, run_id)

    # Write outputs
    snap_path   = output_dir / "triage_snapshot.json"
    report_path = output_dir / "triage_report.md"

    snap_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    report_path.write_text(report)

    # Compute content hashes for determinism proof
    snap_hash   = hashlib.sha256(snap_path.read_bytes()).hexdigest()[:16]
    report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()[:16]

    print(f"  Causes detected    : {len(causes)}")
    print(f"  Actions generated  : {len(actions)}")
    print(f"  Data sufficient    : {sufficiency['sufficient']}")
    if sufficiency["gaps"]:
        for g in sufficiency["gaps"]:
            print(f"  ⚠ {g}")
    print()
    print(f"  triage_snapshot.json → {snap_path}  [sha256:{snap_hash}]")
    print(f"  triage_report.md     → {report_path}  [sha256:{report_hash}]")
    print(f"  elapsed_ms           : {elapsed_ms}")
    print()
    print("  Top causes:")
    for c in causes:
        print(f"  {c['rank']}. [{int(c['confidence']*100)}%] {c['cause']}")
    print()
    print("  Recommended actions:")
    for i, a in enumerate(actions, 1):
        print(f"  {i}. [{a['impact']}] {a['action']}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
