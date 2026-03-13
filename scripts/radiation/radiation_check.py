#!/usr/bin/env python3
"""
Radiation Check v1 — OpenClaw Reliability Scanner
==================================================
Read-only pre-flight diagnostic. Detects hidden reliability risks
before they cause production failure.

Usage:
    python3 radiation_check.py [--json] [--report <path>] [--quiet]

Exit codes:
    0  — SUCCESS
    10 — SAFETY_ABORT
    11 — CONFIG_UNREADABLE
    12 — LOG_ACCESS_FAILURE
    13 — PROBE_FAILURE (probe test itself errored)
    20 — PARTIAL_SCAN  (some modules skipped)

Never modifies system state. Safe to run repeatedly under load.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

# ─── v2 Scoring engine (optional, graceful fallback to v1) ────────────────────
_V2_SCORING = None
try:
    _RC_DIR = os.path.dirname(os.path.abspath(__file__))
    if _RC_DIR not in sys.path:
        sys.path.insert(0, _RC_DIR)
    from radcheck_scoring_v2 import (
        score_v2, enrich_finding, append_history, print_domain_subscores,
        compute_compaction_histogram, build_comp_risk_finding,
        print_compaction_summary, build_comp_early_warning_finding,
    )
    _V2_SCORING = True
except ImportError:
    _V2_SCORING = False

# ─── Paths ────────────────────────────────────────────────────────────────────
HOME             = os.path.expanduser("~")
OC_DIR           = os.path.join(HOME, ".openclaw")
WATCHDOG_DIR     = os.path.join(OC_DIR, "watchdog")
CONFIG_PATH      = os.path.join(OC_DIR, "openclaw.json")
POLICY_PATH      = os.path.join(WATCHDOG_DIR, "sphinxgate_policy.json")
WATCHDOG_SCRIPT  = os.path.join(WATCHDOG_DIR, "hendrik_watchdog.sh")
MODEL_ROUTER     = os.path.join(WATCHDOG_DIR, "model_router.py")
MODEL_STATE      = os.path.join(WATCHDOG_DIR, "model_state.json")
STATUS_LOG       = os.path.join(WATCHDOG_DIR, "status.log")
STALL_LOG        = os.path.join(WATCHDOG_DIR, "stall.log")
OPS_EVENTS_LOG   = os.path.join(WATCHDOG_DIR, "ops_events.log")
METRICS_LOG      = os.path.join(WATCHDOG_DIR, "compaction_metrics.log")
FINDINGS_LOG     = os.path.join(WATCHDOG_DIR, "radiation_findings.log")
REPORT_MD        = os.path.join(WATCHDOG_DIR, "radiation_report.md")
HISTORY_LOG      = os.path.join(WATCHDOG_DIR, "radcheck_history.ndjson")
GATEWAY_PORT     = 18789
SCAN_TIMEOUT_S   = 60

# ─── Data model ───────────────────────────────────────────────────────────────
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SCORE_MAP  = {"CRITICAL": -25, "HIGH": -12, "MEDIUM": -5, "LOW": -1, "INFO": 0}


@dataclass
class Finding:
    finding_id:      str
    severity:        str
    component:       str
    summary:         str
    evidence:        str
    recommended_fix: str
    confidence:      float = 1.0
    ts:              str   = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def to_dict(self):
        return {
            "ts": self.ts,
            "tool": "radiation_check",
            "finding_id": self.finding_id,
            "severity": self.severity,
            "component": self.component,
            "summary": self.summary,
            "evidence": self.evidence,
            "recommended_fix": self.recommended_fix,
            "confidence": self.confidence,
        }

    def to_ndjson(self) -> str:
        return json.dumps(self.to_dict())


# ─── Scanner state ────────────────────────────────────────────────────────────
findings: List[Finding] = []
errors_encountered: int = 0
files_scanned: int = 0
_scan_start: float = time.time()


def emit(finding: Finding):
    findings.append(finding)
    # Append to NDJSON log (never truncate)
    try:
        os.makedirs(WATCHDOG_DIR, exist_ok=True)
        with open(FINDINGS_LOG, "a") as f:
            f.write(finding.to_ndjson() + "\n")
    except Exception as e:
        global errors_encountered
        errors_encountered += 1


def safe_read(path: str) -> Optional[str]:
    """Read a file safely; return None on failure."""
    global files_scanned, errors_encountered
    try:
        with open(path) as f:
            files_scanned += 1
            return f.read()
    except FileNotFoundError:
        return None
    except Exception:
        errors_encountered += 1
        return None


def safe_json(path: str) -> Optional[dict]:
    raw = safe_read(path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        errors_encountered += 1
        return None


def get_provider_chain(cfg: dict) -> List[str]:
    """
    Parse openclaw.json for the configured model chain.
    Handles: agents.defaults.model.{primary,fallbacks} structure.
    """
    try:
        model_cfg = cfg.get("agents", {}).get("defaults", {}).get("model", {})
        primary   = model_cfg.get("primary", "")
        fallbacks = model_cfg.get("fallbacks", [])
        chain = []
        if primary:
            chain.append(primary)
        if fallbacks:
            chain.extend(fallbacks)
        if chain:
            return chain
    except Exception:
        pass
    # Legacy: agents.main.models list
    try:
        models = cfg.get("agents", {}).get("main", {}).get("models", [])
        if models and isinstance(models, list):
            return [m.get("provider", m) if isinstance(m, dict) else str(m) for m in models]
    except Exception:
        pass
    return []


def get_providers_from_chain(chain: List[str]) -> List[str]:
    """Extract provider names from model strings like 'anthropic/claude-sonnet-4-6'."""
    providers = []
    for m in chain:
        parts = str(m).split("/")
        providers.append(parts[0] if parts else m)
    return providers


def info_skip(check_id: str, component: str, reason: str):
    """Emit INFO finding when a check cannot run."""
    emit(Finding(
        finding_id=check_id, severity="INFO", component=component,
        summary=f"Check {check_id} skipped: {reason}",
        evidence="file not found or unreadable",
        recommended_fix="Ensure relevant logs and config files exist.",
        confidence=0.3,
    ))


# ─── Module 1: Configuration Scan ─────────────────────────────────────────────
def scan_configuration():
    cfg = safe_json(CONFIG_PATH)

    if cfg is None:
        emit(Finding(
            finding_id="RC_CFG_000", severity="HIGH", component="configuration",
            summary="openclaw.json missing or unreadable",
            evidence=f"Path: {CONFIG_PATH}",
            recommended_fix="Ensure ~/.openclaw/openclaw.json exists and is valid JSON.",
            confidence=1.0,
        ))
        return  # Can't run other cfg checks without config

    # RC_CFG_001 — missing failover chain
    chain = get_provider_chain(cfg)
    providers = get_providers_from_chain(chain)

    if len(chain) < 2:
        emit(Finding(
            finding_id="RC_CFG_001", severity="CRITICAL", component="configuration",
            summary="Missing failover chain — single model or no chain configured",
            evidence=f"chain has {len(chain)} model(s): {chain[:3]}",
            recommended_fix="Configure ≥2 models in agents.defaults.model.fallbacks for automatic failover.",
            confidence=0.9,
        ))
    else:
        emit(Finding(
            finding_id="RC_CFG_001", severity="INFO", component="configuration",
            summary=f"Failover chain present ({len(chain)} models, {len(set(providers))} providers)",
            evidence=f"primary={chain[0] if chain else '?'}, fallbacks={chain[1:]}",
            recommended_fix="No action required.",
            confidence=1.0,
        ))

    # RC_CFG_002 — single provider dependency
    unique_providers = set(p for p in providers if p)
    if len(unique_providers) == 1:
        emit(Finding(
            finding_id="RC_CFG_002", severity="HIGH", component="configuration",
            summary="Single provider dependency — all models from same provider",
            evidence=f"All {len(providers)} models use provider: {list(unique_providers)[0]}",
            recommended_fix="Add providers from different vendors (e.g. google, openai) for true redundancy.",
            confidence=0.95,
        ))

    # RC_CFG_004 — watchdog not installed
    wd_present = os.path.exists(WATCHDOG_SCRIPT)
    wd_service  = _check_launchd_service("ai.openclaw.gateway")
    if not wd_present:
        emit(Finding(
            finding_id="RC_CFG_004", severity="CRITICAL", component="watchdog",
            summary="Watchdog script not found",
            evidence=f"Expected: {WATCHDOG_SCRIPT}",
            recommended_fix="Install the hendrik_watchdog.sh script and register it with launchd.",
            confidence=1.0,
        ))
    elif not wd_service:
        emit(Finding(
            finding_id="RC_CFG_004", severity="HIGH", component="watchdog",
            summary="Watchdog script present but launchd service not confirmed active",
            evidence=f"Script: {WATCHDOG_SCRIPT} — launchd service: not found in list",
            recommended_fix="Register the watchdog with launchd: launchctl load ~/Library/LaunchAgents/ai.openclaw.watchdog.plist",
            confidence=0.7,
        ))
    else:
        emit(Finding(
            finding_id="RC_CFG_004", severity="INFO", component="watchdog",
            summary="Watchdog installed and launchd service active",
            evidence=f"Script and service confirmed",
            recommended_fix="No action required.",
            confidence=1.0,
        ))

    # RC_CFG_007 — probe debounce missing
    wd_content = safe_read(WATCHDOG_SCRIPT) or ""
    if wd_content and "PROBE_DEBOUNCE" not in wd_content:
        emit(Finding(
            finding_id="RC_CFG_007", severity="HIGH", component="watchdog",
            summary="Probe debounce not implemented — single failure triggers kickstart",
            evidence="PROBE_DEBOUNCE not found in watchdog script",
            recommended_fix="Add consecutive-failure guard (N=3) before triggering launchctl kickstart.",
            confidence=0.95,
        ))
    elif "PROBE_DEBOUNCE" in wd_content:
        emit(Finding(
            finding_id="RC_CFG_007", severity="INFO", component="watchdog",
            summary="Probe debounce present in watchdog",
            evidence="PROBE_DEBOUNCE found in watchdog script",
            recommended_fix="No action required.",
            confidence=1.0,
        ))

    # RC_CFG_008 — token visibility
    token_vis = cfg.get("agents", {}).get("main", {}).get("tokenTracking", False)
    metrics_exists = os.path.exists(os.path.join(OC_DIR, "metrics", "tokens.log"))
    if not token_vis and not metrics_exists:
        emit(Finding(
            finding_id="RC_CFG_008", severity="LOW", component="configuration",
            summary="Token usage telemetry not confirmed active",
            evidence="tokenTracking not set in config; tokens.log not found",
            recommended_fix="Enable token tracking for cost visibility and anomaly detection.",
            confidence=0.6,
        ))


def _check_launchd_service(label: str) -> bool:
    try:
        r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


# ─── Module 2: Watchdog Health Scan ───────────────────────────────────────────
def scan_watchdog():
    status_raw = safe_read(STATUS_LOG)
    if status_raw is None:
        info_skip("RC_WD_001", "watchdog", "status.log not found")
        info_skip("RC_WD_002", "watchdog", "status.log not found")
        return

    lines = [l.strip() for l in status_raw.strip().splitlines() if l.strip()]

    # RC_WD_001 — watchdog loop duration abnormal
    # Parse last 20 status lines, compute intervals
    ts_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
    timestamps = []
    for line in lines[-30:]:
        m = ts_pattern.match(line)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                timestamps.append(dt)
            except Exception:
                pass

    if len(timestamps) >= 3:
        intervals = [(timestamps[i+1] - timestamps[i]).total_seconds()
                     for i in range(len(timestamps)-1)]
        max_interval = max(intervals)
        avg_interval = sum(intervals) / len(intervals)
        abnormal = [i for i in intervals if i > 600]

        if max_interval > 3600:
            emit(Finding(
                finding_id="RC_WD_001", severity="CRITICAL", component="watchdog",
                summary=f"Watchdog gap detected: {int(max_interval/60)}min silence in status.log",
                evidence=f"Max interval: {int(max_interval)}s, Avg: {int(avg_interval)}s, Gaps >10min: {len(abnormal)}",
                recommended_fix="Investigate watchdog crash or launchd scheduling failure. Check watchdog.log.",
                confidence=0.98,
            ))
        elif max_interval > 600:
            emit(Finding(
                finding_id="RC_WD_001", severity="HIGH", component="watchdog",
                summary=f"Watchdog cadence degraded: max gap {int(max_interval/60)}min (expected ≤5min)",
                evidence=f"Max interval: {int(max_interval)}s, Avg: {int(avg_interval)}s",
                recommended_fix="Check watchdog.log for probe failures or compaction-related stalls.",
                confidence=0.9,
            ))
        else:
            emit(Finding(
                finding_id="RC_WD_001", severity="INFO", component="watchdog",
                summary=f"Watchdog cadence normal (avg {int(avg_interval)}s, max {int(max_interval)}s)",
                evidence=f"{len(timestamps)} samples analyzed",
                recommended_fix="No action required.",
                confidence=1.0,
            ))

    # RC_WD_002 — restart thrash risk
    probe_fails = [l for l in lines if "probe=fail" in l]
    if len(lines) > 0:
        fail_rate = len(probe_fails) / len(lines)
        if fail_rate > 0.15:
            emit(Finding(
                finding_id="RC_WD_002", severity="HIGH", component="watchdog",
                summary=f"High probe failure rate: {len(probe_fails)}/{len(lines)} checks ({int(fail_rate*100)}%)",
                evidence=f"Recent failures: {[l[:60] for l in probe_fails[-3:]]}",
                recommended_fix="Investigate root cause. Enable probe debounce to reduce kickstart thrash.",
                confidence=0.95,
            ))
        elif len(probe_fails) > 0:
            emit(Finding(
                finding_id="RC_WD_002", severity="LOW", component="watchdog",
                summary=f"Probe failures present: {len(probe_fails)}/{len(lines)} ({int(fail_rate*100)}%)",
                evidence=f"Most recent: {probe_fails[-1][:80] if probe_fails else 'none'}",
                recommended_fix="Monitor. If frequency increases, investigate compaction impact.",
                confidence=0.85,
            ))

    # RC_WD_004 — missing consecutive-failure guard
    wd_content = safe_read(WATCHDOG_SCRIPT) or ""
    if wd_content and "probe_fail_count" not in wd_content and "PROBE_DEBOUNCE" not in wd_content:
        emit(Finding(
            finding_id="RC_WD_004", severity="HIGH", component="watchdog",
            summary="Consecutive-failure guard absent — kickstart fires on first probe failure",
            evidence="probe_fail_count / PROBE_DEBOUNCE not found in watchdog script",
            recommended_fix="Implement N=3 consecutive-failure counter before triggering kickstart.",
            confidence=0.95,
        ))

    # RC_WD_005 — silence sentinel missing
    sentinel_path = os.path.join(WATCHDOG_DIR, "silence_sentinel.py")
    sentinel_present = os.path.exists(sentinel_path)
    sentinel_integrated = wd_content and "silence_sentinel" in wd_content
    if not sentinel_present:
        emit(Finding(
            finding_id="RC_WD_005", severity="MEDIUM", component="watchdog",
            summary="Silence sentinel not installed",
            evidence=f"Expected: {sentinel_path}",
            recommended_fix="Deploy silence_sentinel.py to detect heartbeat silence events.",
            confidence=1.0,
        ))
    elif not sentinel_integrated:
        emit(Finding(
            finding_id="RC_WD_005", severity="MEDIUM", component="watchdog",
            summary="Silence sentinel present but not integrated in watchdog",
            evidence="silence_sentinel not called in watchdog script",
            recommended_fix="Add SILENCE_SUMMARY call to watchdog status line.",
            confidence=0.9,
        ))

    # RC_WD_006 — model_state.json not updating
    model_state = safe_json(MODEL_STATE)
    if model_state is None:
        emit(Finding(
            finding_id="RC_WD_006", severity="MEDIUM", component="watchdog",
            summary="model_state.json not found — model activity not being tracked",
            evidence=f"Expected: {MODEL_STATE}",
            recommended_fix="Enable model_state.json writes in model_router.py.",
            confidence=1.0,
        ))
    else:
        updated_at = model_state.get("updated_at", 0)
        age_s = time.time() - updated_at if updated_at else None
        if age_s and age_s > 3600:
            emit(Finding(
                finding_id="RC_WD_006", severity="MEDIUM", component="watchdog",
                summary=f"model_state.json stale ({int(age_s/60)}min old) — model activity not updating",
                evidence=f"updated_at={updated_at}, age={int(age_s)}s, provider={model_state.get('provider','?')}",
                recommended_fix="Check model_router.py state persistence. Verify TEST_MODE is not set.",
                confidence=0.85,
            ))
        else:
            age_str = f"{int(age_s)}s ago" if age_s else "unknown"
            emit(Finding(
                finding_id="RC_WD_006", severity="INFO", component="watchdog",
                summary=f"model_state.json updating normally (last update: {age_str})",
                evidence=f"provider={model_state.get('provider','?')}, status={model_state.get('status','?')}",
                recommended_fix="No action required.",
                confidence=1.0,
            ))


# ─── Module 3: Model Routing Scan ─────────────────────────────────────────────
def scan_routing():
    cfg = safe_json(CONFIG_PATH)
    policy = safe_json(POLICY_PATH)

    # RC_RT_001 — no fallback providers
    chain = get_provider_chain(cfg) if cfg else []
    providers = get_providers_from_chain(chain)

    if len(chain) < 2:
        emit(Finding(
            finding_id="RC_RT_001", severity="CRITICAL", component="model_routing",
            summary="No fallback providers configured — single point of failure",
            evidence=f"Chain: {chain}",
            recommended_fix="Add ≥2 models to agents.defaults.model.fallbacks.",
            confidence=0.95,
        ))
    else:
        emit(Finding(
            finding_id="RC_RT_001", severity="INFO", component="model_routing",
            summary=f"Fallback chain present: {len(chain)} models, {len(set(providers))} providers",
            evidence=f"Chain: {chain[:3]}{'...' if len(chain)>3 else ''}",
            recommended_fix="No action required.",
            confidence=1.0,
        ))

    # RC_RT_002 — provider diversity
    unique = set(p for p in providers if p and p not in ("?", ""))
    if len(unique) >= 3:
        emit(Finding(
            finding_id="RC_RT_002", severity="INFO", component="model_routing",
            summary=f"Good provider diversity: {len(unique)} unique providers",
            evidence=f"Providers: {sorted(unique)}",
            recommended_fix="No action required.",
            confidence=1.0,
        ))
    elif len(unique) == 2:
        emit(Finding(
            finding_id="RC_RT_002", severity="LOW", component="model_routing",
            summary=f"Moderate provider diversity: {len(unique)} providers",
            evidence=f"Providers: {sorted(unique)}",
            recommended_fix="Consider adding a 3rd provider (e.g. openrouter) for deeper redundancy.",
            confidence=0.8,
        ))

    # RC_RT_003 — policy exhaustion risk (SphinxGate)
    if policy:
        allow = set(policy.get("allow_providers") or [])
        deny  = set(policy.get("deny_providers") or [])
        fail_open = policy.get("fail_open", True)
        conflict = allow & deny
        if conflict:
            emit(Finding(
                finding_id="RC_RT_003", severity="HIGH", component="model_routing",
                summary=f"SphinxGate policy exhaustion risk — provider(s) in both allow and deny lists",
                evidence=f"Conflicting providers: {conflict}, fail_open={fail_open}",
                recommended_fix="Remove conflicting entries. If intentional, ensure fail_open=true.",
                confidence=0.99,
            ))
        if allow:
            chain_providers = set(providers)  # provider names extracted from chain
            allowed_in_chain = allow & chain_providers
            if not allowed_in_chain:
                emit(Finding(
                    finding_id="RC_RT_004", severity="HIGH", component="model_routing",
                    summary="SphinxGate allow_providers has no overlap with configured chain",
                    evidence=f"allow_providers={allow}, chain_providers={chain_providers}",
                    recommended_fix="Align SphinxGate allow list with actual model chain providers.",
                    confidence=0.95,
                ))

    # RC_RT_005 — lane separation
    router_content = safe_read(MODEL_ROUTER) or ""
    if router_content and "lane" not in router_content:
        emit(Finding(
            finding_id="RC_RT_005", severity="MEDIUM", component="model_routing",
            summary="Lane separation not detected in model_router.py",
            evidence="'lane' not found in model_router.py",
            recommended_fix="Implement lane-based routing (interactive/background) for workload separation.",
            confidence=0.7,
        ))

    # RC_RT_006 — token telemetry absent
    tokens_log = os.path.join(OC_DIR, "metrics", "tokens.log")
    if not os.path.exists(tokens_log):
        emit(Finding(
            finding_id="RC_RT_006", severity="LOW", component="model_routing",
            summary="Token telemetry log absent — usage visibility missing",
            evidence=f"Expected: {tokens_log}",
            recommended_fix="Enable token tracking in model_router.py for cost and anomaly visibility.",
            confidence=0.9,
        ))

    # RC_RT_007 — model_state persistence
    if not os.path.exists(MODEL_STATE):
        emit(Finding(
            finding_id="RC_RT_007", severity="MEDIUM", component="model_routing",
            summary="model_state.json absent — model activity not persisted",
            evidence=f"Expected: {MODEL_STATE}",
            recommended_fix="Enable model_state.json writes. Used by watchdog for model health checks.",
            confidence=1.0,
        ))


# ─── Module 4: Environment Baseline Scan ──────────────────────────────────────
def scan_environment():
    # RC_ENV_001 — load average
    try:
        la1, la5, la15 = os.getloadavg()
        if la5 > 20:
            sev = "CRITICAL" if la5 > 40 else "HIGH"
            emit(Finding(
                finding_id="RC_ENV_001", severity=sev, component="environment",
                summary=f"System load critically elevated: {la1:.1f} / {la5:.1f} / {la15:.1f}",
                evidence=f"1min={la1:.2f}, 5min={la5:.2f}, 15min={la15:.2f} — high load amplifies compaction pauses",
                recommended_fix="Identify CPU-intensive processes (ps aux | sort -rk3 | head). Sustained load >20 correlates with gateway stalls.",
                confidence=1.0,
            ))
        elif la5 > 8:
            emit(Finding(
                finding_id="RC_ENV_001", severity="MEDIUM", component="environment",
                summary=f"Load average elevated: {la1:.1f} / {la5:.1f} / {la15:.1f}",
                evidence=f"5min={la5:.2f} above comfortable baseline (~4)",
                recommended_fix="Monitor load trends. Check for runaway processes.",
                confidence=0.9,
            ))
        else:
            emit(Finding(
                finding_id="RC_ENV_001", severity="INFO", component="environment",
                summary=f"Load average normal: {la1:.1f} / {la5:.1f} / {la15:.1f}",
                evidence=f"All load averages within acceptable range",
                recommended_fix="No action required.",
                confidence=1.0,
            ))
    except Exception:
        info_skip("RC_ENV_001", "environment", "getloadavg() failed")

    # RC_ENV_002 — memory pressure
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        free_pages = 0
        for line in r.stdout.splitlines():
            if "Pages free" in line:
                val = line.split(":")[-1].strip().rstrip(".")
                free_pages = int(val)
                break
        free_mb = free_pages * 4096 // 1048576
        if free_mb < 200:
            emit(Finding(
                finding_id="RC_ENV_002", severity="HIGH", component="environment",
                summary=f"Low free memory: {free_mb}MB — compaction GC pressure risk",
                evidence=f"vm_stat free pages → {free_mb}MB available",
                recommended_fix="Free memory before running heavy agent workloads. Consider reducing model context size.",
                confidence=0.85,
            ))
        elif free_mb < 500:
            emit(Finding(
                finding_id="RC_ENV_002", severity="MEDIUM", component="environment",
                summary=f"Memory pressure moderate: {free_mb}MB free",
                evidence=f"vm_stat free → {free_mb}MB",
                recommended_fix="Monitor memory trends during compaction windows.",
                confidence=0.8,
            ))
        else:
            emit(Finding(
                finding_id="RC_ENV_002", severity="INFO", component="environment",
                summary=f"Memory adequate: {free_mb}MB free",
                evidence=f"vm_stat: {free_mb}MB free pages",
                recommended_fix="No action required.",
                confidence=1.0,
            ))
    except Exception:
        info_skip("RC_ENV_002", "environment", "vm_stat unavailable")

    # RC_ENV_003 — process count
    try:
        r = subprocess.run(["ps", "ax"], capture_output=True, text=True, timeout=5)
        proc_count = len(r.stdout.strip().splitlines())
        if proc_count > 600:
            emit(Finding(
                finding_id="RC_ENV_003", severity="MEDIUM", component="environment",
                summary=f"High process count: {proc_count} processes",
                evidence=f"ps ax: {proc_count} entries",
                recommended_fix="Check for zombie or runaway processes. High counts increase scheduling pressure.",
                confidence=0.7,
            ))
        else:
            emit(Finding(
                finding_id="RC_ENV_003", severity="INFO", component="environment",
                summary=f"Process count normal: {proc_count}",
                evidence=f"ps ax: {proc_count} processes",
                recommended_fix="No action required.",
                confidence=0.9,
            ))
    except Exception:
        info_skip("RC_ENV_003", "environment", "ps ax unavailable")

    # RC_ENV_005 — compaction frequency heuristic
    _scan_compaction_frequency()


def _scan_compaction_frequency():
    """Heuristic: count compaction events from ops_events.log and watchdog.log."""
    compaction_events = 0
    compaction_timeouts = 0

    ops_raw = safe_read(OPS_EVENTS_LOG) or ""
    for line in ops_raw.splitlines():
        try:
            ev = json.loads(line.strip())
            if ev.get("event") in ("COMPACTION_START", "COMPACTION_END", "COMPACTION_SUSPECT"):
                compaction_events += 1
            if ev.get("reason") == "embedded_run_timeout":
                compaction_timeouts += 1
        except Exception:
            pass

    # Also check watchdog.log directly
    wl_raw = safe_read(os.path.join(WATCHDOG_DIR, "watchdog.log")) or ""
    safeguard_hits = wl_raw.count("[compaction-safeguard]")
    timeout_hits   = wl_raw.count("timed out during compaction")

    total_signals = safeguard_hits + timeout_hits + compaction_timeouts

    if total_signals >= 3:
        emit(Finding(
            finding_id="RC_ENV_005", severity="HIGH", component="environment",
            summary=f"Compaction frequency elevated: {safeguard_hits} safeguard triggers, {timeout_hits} timeouts logged",
            evidence=f"compaction-safeguard hits={safeguard_hits}, timed-out-during-compaction={timeout_hits}, ops_events={compaction_events}",
            recommended_fix="Compaction is frequent and causing 10min stalls. Consider reducing session history retention or increasing compaction budget.",
            confidence=0.88,
        ))
    elif total_signals >= 1:
        emit(Finding(
            finding_id="RC_ENV_005", severity="MEDIUM", component="environment",
            summary=f"Compaction events detected: {safeguard_hits} safeguard triggers, {timeout_hits} timeouts",
            evidence=f"compaction-safeguard={safeguard_hits}, timeout={timeout_hits}",
            recommended_fix="Monitor. Compaction is occurring but not yet at alarming frequency.",
            confidence=0.75,
        ))
    else:
        emit(Finding(
            finding_id="RC_ENV_005", severity="INFO", component="environment",
            summary="No compaction signals detected in logs",
            evidence="ops_events.log and watchdog.log show no compaction events",
            recommended_fix="Run compaction_log_parser.py to backfill historical events.",
            confidence=0.6,
        ))


# ─── Module 5: Port vs Probe Test (Signature Feature) ─────────────────────────
def scan_port_probe() -> Tuple[bool, bool]:
    """
    Flagship check: detect port-up / probe-fail frozen loop pattern.
    Returns (port_up, probe_ok).
    """
    port_up  = False
    probe_ok = False

    # Step 1: port check via socket
    try:
        with socket.create_connection(("127.0.0.1", GATEWAY_PORT), timeout=1.0):
            port_up = True
    except Exception:
        port_up = False

    # Step 2: RPC probe via openclaw CLI
    try:
        r = subprocess.run(
            ["openclaw", "gateway", "probe"],
            capture_output=True, text=True, timeout=10
        )
        probe_ok = (r.returncode == 0)
    except FileNotFoundError:
        # openclaw not in PATH — try curl probe
        try:
            r2 = subprocess.run(
                ["curl", "-sf", "--max-time", "5", f"http://127.0.0.1:{GATEWAY_PORT}/"],
                capture_output=True, text=True, timeout=8
            )
            probe_ok = (r2.returncode == 0)
        except Exception:
            probe_ok = False
    except Exception:
        probe_ok = False

    # RC_ENV_004 — classify the result
    if port_up and not probe_ok:
        emit(Finding(
            finding_id="RC_ENV_004", severity="HIGH", component="gateway",
            summary="GATEWAY PORT UP but RPC probe FAILING — frozen event loop detected",
            evidence=f"port 18789: LISTENING | gateway probe: FAIL — classic compaction stall signature",
            recommended_fix="This indicates a frozen Node.js event loop, typically caused by compaction GC. "
                            "Kickstart will not help until the freeze resolves. Enable probe debounce (N=3). "
                            "Consider: context reduction, compaction budget tuning.",
            confidence=0.97,
        ))
    elif not port_up and not probe_ok:
        emit(Finding(
            finding_id="RC_ENV_004", severity="CRITICAL", component="gateway",
            summary="Gateway port NOT listening AND probe failing — gateway is down",
            evidence=f"port 18789: NOT LISTENING | probe: FAIL",
            recommended_fix="Restart gateway: launchctl kickstart -k gui/$UID/ai.openclaw.gateway",
            confidence=0.99,
        ))
    elif port_up and probe_ok:
        emit(Finding(
            finding_id="RC_ENV_004", severity="INFO", component="gateway",
            summary="Gateway healthy: port listening and probe responding",
            evidence=f"port 18789: LISTENING | probe: OK",
            recommended_fix="No action required.",
            confidence=1.0,
        ))
    else:
        emit(Finding(
            finding_id="RC_ENV_004", severity="MEDIUM", component="gateway",
            summary="Gateway port not listening but probe may have partial response",
            evidence=f"port={port_up} probe={probe_ok}",
            recommended_fix="Investigate gateway startup state.",
            confidence=0.7,
        ))

    # Also check historical port_up+probe_fail pattern in status.log
    status_raw = safe_read(STATUS_LOG) or ""
    historical_stalls = len([l for l in status_raw.splitlines()
                              if "port18789=yes" in l and "probe=fail" in l])
    if historical_stalls > 0:
        emit(Finding(
            finding_id="RC_ENV_004B", severity="HIGH", component="gateway",
            summary=f"Historical frozen-loop pattern: {historical_stalls} prior port-up/probe-fail events",
            evidence=f"status.log contains {historical_stalls} lines with port=yes+probe=fail",
            recommended_fix="This system has a known compaction stall history. Prioritize context reduction and probe debounce.",
            confidence=0.95,
        ))

    return port_up, probe_ok


# ─── Facts builder (for v2 credits + guardrails) ──────────────────────────────
def gather_facts() -> dict:
    """
    Collect lightweight system facts for v2 scoring credits.
    All reads are non-blocking; missing data → safe default.
    """
    facts = {}

    # backup_recent_hours: check latest GDrive snapshot mtime
    gdrive_backup = os.path.expanduser(
        "~/Library/CloudStorage/GoogleDrive-hendrik.homarus@gmail.com"
        "/My Drive/OpenClawBackups/AGENTMacBook"
    )
    try:
        snapshots = sorted([
            d for d in os.listdir(gdrive_backup)
            if d.startswith("openclaw-")
        ]) if os.path.isdir(gdrive_backup) else []
        if snapshots:
            latest_path = os.path.join(gdrive_backup, snapshots[-1])
            mtime = os.path.getmtime(latest_path)
            age_h = (time.time() - mtime) / 3600
            facts["backup_recent_hours"] = round(age_h, 1)
        else:
            facts["backup_recent_hours"] = None
    except Exception:
        facts["backup_recent_hours"] = None

    # gateway_stalls_7d: count GATEWAY_STALL events in ops_events.log within 7d
    try:
        cutoff = time.time() - 7 * 86400
        ops_raw = safe_read(OPS_EVENTS_LOG) or ""
        stalls = 0
        for line in ops_raw.splitlines():
            try:
                ev = json.loads(line)
                if ev.get("event") == "GATEWAY_STALL":
                    ev_ts = ev.get("ts_epoch", 0)
                    if ev_ts and ev_ts > cutoff:
                        stalls += 1
            except Exception:
                pass
        facts["gateway_stalls_7d"] = stalls
    except Exception:
        facts["gateway_stalls_7d"] = -1

    # model_state_monotonic: check probe log for PASS
    probe_log = os.path.join(WATCHDOG_DIR, "model_state_probe.log")
    try:
        probe_raw = safe_read(probe_log) or ""
        facts["model_state_monotonic"] = "MODEL_STATE_MONOTONIC=PASS" in probe_raw
    except Exception:
        facts["model_state_monotonic"] = False

    # provider_diversity: count unique providers in chain
    cfg = safe_json(CONFIG_PATH)
    if cfg:
        chain = get_provider_chain(cfg)
        providers = get_providers_from_chain(chain)
        facts["provider_diversity"] = len(set(p for p in providers if p))
    else:
        facts["provider_diversity"] = 0

    return facts


# ─── Risk Velocity console section ───────────────────────────────────────────
def print_velocity_section(v2_result):
    """Print the Risk Velocity section after domain subscores."""
    W = 60
    print(f"\n{'─'*W}")
    print("  === Risk Velocity ===")
    print(f"{'─'*W}")

    vel = v2_result.get("risk_velocity") if v2_result else None

    if not isinstance(vel, dict):
        print("  Velocity: insufficient history (need 2+ runs)")
        return

    delta      = vel.get("delta", 0)
    score_now  = vel.get("score_now", "?")
    score_prev = vel.get("score_prev", "?")
    hours      = vel.get("hours_elapsed")
    rate       = vel.get("rate_per_hour")
    direction  = vel.get("direction", "STABLE")

    print(f"  Score delta:  {delta:+d} ({score_prev} → {score_now})")
    if hours is not None:
        print(f"  Elapsed:      {hours:.2f}h")
    if rate is not None:
        print(f"  Rate:         {rate:+.1f} pts/hr  [{direction}]")
    else:
        print(f"  Direction:    [{direction}]")


# ─── Scoring Engine (v1 fallback) ─────────────────────────────────────────────
def compute_score(findings_list: List[Finding]) -> Tuple[int, str]:
    score = 100
    for f in findings_list:
        score += SCORE_MAP.get(f.severity, 0)
    score = max(0, score)

    if score >= 80:
        risk = "LOW"
    elif score >= 60:
        risk = "MODERATE"
    elif score >= 40:
        risk = "HIGH"
    else:
        risk = "SEVERE"

    return score, risk


# ─── Console Report ────────────────────────────────────────────────────────────
def print_console_report(score: int, risk: str, duration_ms: int):
    W = 60
    SEV_ICONS = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "⚪"}

    print()
    print("=" * W)
    print("  🐐 ☢️  RADIATION CHECK — SYSTEM RELIABILITY SCAN")
    print("=" * W)

    # Score bar
    bar_len = int(score * 40 // 100)
    bar = "█" * bar_len + "░" * (40 - bar_len)
    risk_icons = {"LOW": "✅", "MODERATE": "⚠️", "HIGH": "🚨", "SEVERE": "💀"}
    print(f"\n  System Stability Score: {score} / 100  {risk_icons.get(risk,'')}")
    print(f"  [{bar}]")
    print(f"  Overall Risk: {risk}")

    # Severity counts
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    print(f"\n  Findings: ", end="")
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        if counts[sev] > 0:
            print(f"{SEV_ICONS[sev]} {sev}: {counts[sev]}  ", end="")
    print()

    # Top critical/high
    critical_high = [f for f in findings if f.severity in ("CRITICAL", "HIGH")]
    if critical_high:
        print(f"\n{'─'*W}")
        print("  🚨 TOP RISKS")
        print(f"{'─'*W}")
        for f in critical_high[:6]:
            print(f"  {SEV_ICONS[f.severity]} [{f.finding_id}] {f.summary}")
            print(f"     Evidence: {f.evidence[:70]}{'...' if len(f.evidence)>70 else ''}")
    else:
        print(f"\n  ✅ No critical or high severity findings.")

    # Top fixes
    actionable = [f for f in findings if f.severity in ("CRITICAL", "HIGH", "MEDIUM")]
    if actionable:
        print(f"\n{'─'*W}")
        print("  🔧 TOP FIXES")
        print(f"{'─'*W}")
        for i, f in enumerate(actionable[:5], 1):
            print(f"  {i}. [{f.finding_id}] {f.recommended_fix[:70]}{'...' if len(f.recommended_fix)>70 else ''}")

    # Scan metrics
    print(f"\n{'─'*W}")
    print("  📊 SCAN_METRICS")
    print(f"{'─'*W}")
    print(f"  duration_ms:        {duration_ms}")
    print(f"  findings_count:     {len(findings)}")
    print(f"  files_scanned:      {files_scanned}")
    print(f"  errors_encountered: {errors_encountered}")
    print(f"  findings_log:       {FINDINGS_LOG}")
    print(f"  report_md:          {REPORT_MD}")
    print(f"{'─'*W}")
    print(f"  🐐 ACME Agent Supply Co.")
    print("=" * W)
    print()


# ─── Markdown Report ──────────────────────────────────────────────────────────
def write_markdown_report(score: int, risk: str, duration_ms: int, v2_result=None):
    global errors_encountered
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}

    lines = [
        f"# 🐐 ☢️ Radiation Check Report",
        f"",
        f"**Generated:** {ts}  ",
        f"**Host:** {os.uname().nodename}  ",
        f"**Scan Duration:** {duration_ms}ms",
        f"",
        f"---",
        f"",
        f"## Executive Summary",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| System Stability Score | **{score} / 100** |",
        f"| Overall Risk | **{risk}** |",
        f"| CRITICAL findings | {counts['CRITICAL']} |",
        f"| HIGH findings | {counts['HIGH']} |",
        f"| MEDIUM findings | {counts['MEDIUM']} |",
        f"| LOW findings | {counts['LOW']} |",
        f"| INFO findings | {counts['INFO']} |",
        f"",
    ]

    # v2 domain subscores section
    if v2_result:
        lines += [
            f"---",
            f"",
            f"## Domain Subscores (v2)",
            f"",
            f"| Domain | Score | Weight | Cap Triggered |",
            f"|--------|-------|--------|---------------|",
        ]
        for d, info in v2_result["domain_subscores"].items():
            cap = "⚠️ YES" if info["cap_triggered"] else "No"
            lines.append(f"| {d} | {info['subscore']}/{info['weight']} | {info['weight']} | {cap} |")
        if v2_result.get("credits_applied"):
            lines += ["", f"**Credits applied:** +{v2_result.get('credits_total', 0)}"]
            for c in v2_result["credits_applied"]:
                lines.append(f"- {c['credit_id']}: +{c['amount']} — {c['reason']}")
        vel = v2_result.get("risk_velocity")
        if isinstance(vel, dict):
            d = vel.get("delta", 0)
            direction = vel.get("direction", "STABLE")
            rate = vel.get("rate_per_hour")
            rate_str = f"{rate:+.1f} pts/hr" if rate is not None else "N/A"
            lines.append(
                f"\n**Risk velocity:** {d:+d} pts ({vel.get('score_prev')} → {vel.get('score_now')})  "
                f"Rate: {rate_str}  [{direction}]"
            )
        elif vel is not None:
            lines.append(f"\n**Risk velocity:** {vel:+.1f} (Δ vs 3 runs ago)")
        grd = v2_result.get("guardrails", {})
        if grd.get("B2_floor_clamp"):
            lines.append(f"\n> ⚠️ **FLOOR CLAMP applied:** {grd['B2_floor_clamp']}")
        lines += ["", f"---", f""]

    lines += [
        f"## Top Risks",
        f"",
    ]

    critical_high = [f for f in findings if f.severity in ("CRITICAL", "HIGH")]
    if critical_high:
        for f in critical_high[:8]:
            lines.append(f"### 🚨 [{f.finding_id}] {f.summary}")
            lines.append(f"**Severity:** {f.severity} | **Confidence:** {f.confidence:.0%}  ")
            lines.append(f"**Evidence:** {f.evidence}  ")
            lines.append(f"**Fix:** {f.recommended_fix}")
            lines.append("")
    else:
        lines.append("No critical or high severity findings detected. ✅")
        lines.append("")

    lines += [
        f"---",
        f"",
        f"## Full Findings",
        f"",
        f"| ID | Severity | Component | Summary | Confidence |",
        f"|----|----------|-----------|---------|------------|",
    ]

    for f in sorted(findings, key=lambda x: SEVERITIES.index(x.severity)):
        short_summary = f.summary[:60] + ("..." if len(f.summary) > 60 else "")
        lines.append(f"| {f.finding_id} | {f.severity} | {f.component} | {short_summary} | {f.confidence:.0%} |")

    lines += [
        f"",
        f"---",
        f"",
        f"## Scan Metadata",
        f"",
        f"```",
        f"duration_ms:        {duration_ms}",
        f"findings_count:     {len(findings)}",
        f"files_scanned:      {files_scanned}",
        f"errors_encountered: {errors_encountered}",
        f"findings_log:       {FINDINGS_LOG}",
        f"```",
        f"",
        f"---",
        f"*🐐 ACME Agent Supply Co. | Radiation Check v1 — OpenClaw Reliability Stack*",
    ]

    try:
        with open(REPORT_MD, "w") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        errors_encountered += 1


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _scan_start, errors_encountered
    _scan_start = time.time()

    # Safety check — ensure we never exceed timeout
    import signal
    def _timeout_handler(sig, frame):
        print("\n⚠️  SCAN TIMEOUT (60s) — partial results saved")
        sys.exit(20)

    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(SCAN_TIMEOUT_S)
    except AttributeError:
        pass  # Windows: no SIGALRM

    quiet = "--quiet" in sys.argv

    if not quiet:
        print("☢️  Radiation Check v1 — scanning...", flush=True)

    exit_code = 0
    partial = False

    # Run all modules with graceful degradation
    modules = [
        ("Configuration", scan_configuration),
        ("Watchdog Health", scan_watchdog),
        ("Model Routing", scan_routing),
        ("Environment", scan_environment),
        ("Port/Probe", scan_port_probe),
    ]

    for name, fn in modules:
        if not quiet:
            print(f"  → {name}...", flush=True)
        try:
            fn()
        except Exception as e:
            errors_encountered += 1
            partial = True
            emit(Finding(
                finding_id=f"RC_ERR_{name.upper()[:4]}",
                severity="INFO",
                component="scanner",
                summary=f"Module '{name}' encountered an error: {e}",
                evidence=str(e),
                recommended_fix="Check scanner logs. Module degraded gracefully.",
                confidence=0.5,
            ))

    # ── Compaction histogram (P2) ────────────────────────────────────────────
    comp_hist = None
    if _V2_SCORING:
        try:
            if not quiet:
                print("  → Compaction Histogram...", flush=True)
            comp_hist = compute_compaction_histogram()
            # Emit RC_ENV_COMP_RISK finding if ≥ELEVATED
            _run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            comp_finding = build_comp_risk_finding(comp_hist, _run_ts)
            if comp_finding:
                # Emit as a structured Finding
                emit(Finding(
                    finding_id=comp_finding["finding_id"],
                    severity=comp_finding["severity"],
                    component=comp_finding["component"],
                    summary=comp_finding["summary"],
                    evidence=comp_finding["evidence"],
                    recommended_fix=comp_finding["recommended_fix"],
                    confidence=comp_finding.get("confidence", 0.9),
                ))

            # Emit RC_ENV_COMP_EARLY_WARNING (forward-looking, A-RC-P4-001)
            # Distinct finding_id from RC_ENV_COMP_RISK — not deduplicated against it
            ew_finding = build_comp_early_warning_finding(comp_hist, _run_ts)
            if ew_finding:
                emit(Finding(
                    finding_id=ew_finding["finding_id"],
                    severity=ew_finding["severity"],
                    component=ew_finding["component"],
                    summary=ew_finding["summary"],
                    evidence=ew_finding["evidence"],
                    recommended_fix=ew_finding["recommended_fix"],
                    confidence=ew_finding.get("confidence", 0.85),
                ))
        except Exception as e:
            errors_encountered += 1
            comp_hist = None

    elapsed_ms = int((time.time() - _scan_start) * 1000)

    if elapsed_ms > SCAN_TIMEOUT_S * 1000:
        exit_code = 20

    # ── v2 scoring pipeline ──────────────────────────────────────────────────
    v2_result = None
    if _V2_SCORING:
        try:
            facts = gather_facts()
            v2_result = score_v2(findings, facts=facts)
            score = v2_result["score"]
            risk  = v2_result["risk_level"]
            # Write enriched findings back to log (with domain/complexity fields)
            try:
                os.makedirs(WATCHDOG_DIR, exist_ok=True)
                with open(FINDINGS_LOG, "a") as flog:
                    for ef in v2_result["findings_enriched"]:
                        ef_out = dict(ef)
                        ef_out["tool"] = "radiation_check_v2"
                        ef_out["ts"]   = ef_out.get("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                        flog.write(json.dumps(ef_out) + "\n")
            except Exception:
                pass
            # Append to history (with compaction enrichment)
            append_history(v2_result, len(findings), elapsed_ms, comp_hist)
        except Exception as e:
            # v2 failed — fall back to v1
            v2_result = None
            score, risk = compute_score(findings)
    else:
        score, risk = compute_score(findings)

    # Reports
    if not quiet:
        print_console_report(score, risk, elapsed_ms)
        if v2_result:
            print_domain_subscores(v2_result)
            print_velocity_section(v2_result)
        if comp_hist:
            print_compaction_summary(comp_hist)
            # Log COMPACTION_STATS and ACCELERATION lines to console
            for ll in comp_hist.get("log_lines", []):
                if ll.startswith("COMPACTION_"):
                    print(f"  {ll}")

    write_markdown_report(score, risk, elapsed_ms, v2_result)

    # JSON output mode
    if "--json" in sys.argv:
        output = {
            "score": score,
            "risk": risk,
            "findings": [f.to_dict() for f in findings],
            "metrics": {
                "duration_ms": elapsed_ms,
                "findings_count": len(findings),
                "files_scanned": files_scanned,
                "errors_encountered": errors_encountered,
            }
        }
        print(json.dumps(output, indent=2))

    try:
        signal.alarm(0)
    except AttributeError:
        pass

    if partial:
        sys.exit(20)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
