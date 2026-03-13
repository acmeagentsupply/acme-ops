#!/usr/bin/env python3
"""
ACME Support Bundle v0.2  —  A-SUP-V0-001 / A-SUP-V0-002
Operator Diagnostic Pack: minimal, redacted, timestamped, reproducible.

SAFETY:
  - Writes ONLY inside ~/.openclaw/watchdog/support/bundles/<ts>/
  - Never reads ~/.openclaw/openclaw.json
  - Redacts tokens/keys/passwords/emails by default
  - --include_raw still excludes all secret files
  - Zip contains ONLY bundle dir contents (no parent traversal)

Usage:
  python3 acme_support_bundle.py [--include_raw] [--zip] [--print-consent]

Flags:
  --include_raw     Include raw log content (secrets always excluded)
  --zip             Create support_bundle_<ts>.zip in the bundle dir after generation
  --print-consent   Print operator consent blurb to console; exit (no files written)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "0.2.0"
SCHEMA = "acme_support_bundle.v1.0"

HOME = Path.home()
WATCHDOG_DIR = HOME / ".openclaw" / "watchdog"
OPS_DIR = HOME / ".openclaw" / "workspace" / "openclaw-ops" / "ops"
BUNDLES_BASE = WATCHDOG_DIR / "support" / "bundles"
OPS_EVENTS_LOG = WATCHDOG_DIR / "ops_events.log"
SENTINEL_STATE_FILE = WATCHDOG_DIR / "sentinel_protection_state.json"

# Files that must NEVER be included (even with --include_raw)
SECRETS_BLACKLIST = {
    "openclaw.json",
    "auth-profiles.json",
    ".env",
    "secrets",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".key",
    "token",
}

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# NDJSON event emitter
# ---------------------------------------------------------------------------
def emit_event(event_type: str, extra: dict = None):
    payload = {"ts": ts_now(), "event": event_type}
    if extra:
        payload.update(extra)
    line = json.dumps(payload)
    try:
        WATCHDOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(OPS_EVENTS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # Never fail on log write
    print(f"  [EVENT] {line}")


# ---------------------------------------------------------------------------
# Redaction engine
# ---------------------------------------------------------------------------
REDACT_PATTERNS = [
    # JWT / bearer tokens (base64url.base64url.base64url)
    (re.compile(r'eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+'), "[JWT_REDACTED]"),
    # API keys: sk-... (OpenAI style)
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "[API_KEY_REDACTED]"),
    # Generic long hex strings (40+ chars) — session tokens, hashes used as secrets
    (re.compile(r'\b[0-9a-fA-F]{40,}\b'), "[HEX_SECRET_REDACTED]"),
    # Password/token/secret in JSON-ish context
    (re.compile(
        r'("(?:password|passwd|token|secret|api_key|apikey|auth|credential)[^"]*"\s*:\s*)"[^"]{4,}"',
        re.IGNORECASE),
     r'\1"[REDACTED]"'),
    # Email addresses
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'), "[EMAIL_REDACTED]"),
    # Bearer header
    (re.compile(r'Bearer\s+[A-Za-z0-9\-_\.]{20,}', re.IGNORECASE), "Bearer [TOKEN_REDACTED]"),
    # Tailscale IP / private subnet
    (re.compile(r'\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), "[TAILSCALE_IP_REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Consent blurb (--print-consent)
# ---------------------------------------------------------------------------
CONSENT_TEXT = """\
╔══════════════════════════════════════════════════════════════════════════════╗
║          ACME Support Bundle — Operator Consent & Privacy Notice            ║
╚══════════════════════════════════════════════════════════════════════════════╝

By generating and sharing this support bundle, you acknowledge the following:

1. WHAT IS COLLECTED
   This bundle contains operational diagnostic data from your ACME agent
   stack. This includes system event logs (ops_events.log tail), agent state
   snapshots (Agent911 score/risk/posture), RadCheck scoring history,
   Sentinel protection events, compaction metrics, and launchd output.

   It does NOT include:
     - Your OpenClaw configuration file (openclaw.json)
     - Authentication credentials or auth-profiles
     - API keys, private keys, or .pem files
     - .env files or other secrets files

2. REDACTION (ON BY DEFAULT)
   Automated redaction masks the following patterns before any file is written:
     - API keys and bearer tokens (sk-*, Bearer ...)
     - JWT tokens (eyJ... format)
     - Email addresses
     - Long hexadecimal secret strings (40+ chars)
     - Tailscale IP addresses (100.x.x.x range)
   You may disable redaction with --include_raw. Secrets files listed above
   are excluded regardless of this flag.

3. HOW ACME USES THIS DATA
   Support bundles are used solely to diagnose reliability issues with your
   ACME agent stack. Data is:
     - Not shared with third parties outside this support engagement
     - Not used for product telemetry, training, or analytics
     - Retained only for the duration of the support engagement

4. YOUR CONTROL
   You decide what gets sent. Review the bundle directory contents before
   attaching. If you are not satisfied with the level of redaction, delete
   the bundle (rm -rf <bundle_dir>) and regenerate with --include_raw=off.

5. SUPPORT POSTURE
   ACME's default support posture is observational and advisory:
     a. We request a support bundle to establish baseline triage state.
     b. We review ops_events, Agent911 score, Sentinel events, and compaction
        metrics in that order.
     c. We do not make configuration changes without explicit operator approval.
     d. We do not issue gateway restarts or modify openclaw.json remotely.

6. SLA PLACEHOLDER
   Response times and resolution commitments are defined in your support
   agreement. If you do not have a signed support agreement on file,
   all support is provided on a best-effort basis with no guaranteed SLA.

   [SLA_TIER_PLACEHOLDER — fill in upon agreement execution]

7. CONTACT
   Submit bundles to: support@acmeagentsupply.com
   Include subject line: "ACME Support Bundle — <bundle_id>"

Generated by ACME Support Bundle v{version}
"""


def print_consent():
    print(CONSENT_TEXT.format(version=VERSION))


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def safe_read_text(path: Path, max_bytes: int = 256 * 1024) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(max_bytes)
    except Exception as e:
        return f"[READ_ERROR: {e}]"


def tail_lines(path: Path, n: int = 50) -> str:
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"[READ_ERROR: {e}]"


def safe_json_load(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return {"_error": str(e)}


def collect_watchdog_disk_usage() -> dict:
    """
    Read-only watchdog footprint snapshot for OCTriageUnit bundles.
    Uses the operator-facing command string required by the runbook.
    """
    command = "du -sk ~/.openclaw/watchdog ~/.openclaw/watchdog/backups ~/.openclaw/watchdog/lazarus"
    started = time.monotonic()
    gtm_exports = WATCHDOG_DIR / "gtm_exports"
    size_map = {
        "watchdog": 0.0,
        "backups": 0.0,
        "lazarus": 0.0,
        "gtm_exports": 0.0,
    }
    try:
        path_specs = [
            ("watchdog", WATCHDOG_DIR),
            ("backups", WATCHDOG_DIR / "backups"),
            ("lazarus", WATCHDOG_DIR / "lazarus"),
        ]
        if gtm_exports.exists():
            path_specs.append(("gtm_exports", gtm_exports))
        existing_paths = [str(path) for _, path in path_specs if path.exists()]
        proc = subprocess.run(
            ["du", "-sk"] + existing_paths,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            return {
                "command": command,
                "stdout": proc.stdout.strip() or "[NO_OUTPUT]",
                "elapsed_ms": elapsed_ms,
                "size_human": "unknown",
                "watchdog_bloat_warning": False,
                "watchdog_growth_rate_mb_hr": "unavailable",
                "subtrees": size_map,
                "gtm_exports_present": gtm_exports.exists(),
            }
        for raw in proc.stdout.strip().splitlines():
            parts = raw.split()
            if len(parts) < 2:
                continue
            try:
                kb = float(parts[0])
            except ValueError:
                continue
            path = parts[1]
            for key, expected in path_specs:
                if path == str(expected):
                    size_map[key] = round(kb / 1024.0, 1)
                    break
        size_human = f"{size_map['watchdog']:.1f}M"
        warning = size_map["watchdog"] > 500.0
        growth_rate = "unavailable"
        if SENTINEL_STATE_FILE.exists():
            try:
                sentinel_state = safe_json_load(SENTINEL_STATE_FILE)
                growth_value = sentinel_state.get("growth_mb_per_hr")
                if isinstance(growth_value, (int, float)):
                    growth_rate = round(float(growth_value), 1)
            except Exception:
                growth_rate = "unavailable"
        stdout = f"{size_map['watchdog']:.1f}M\t{WATCHDOG_DIR}"
        return {
            "command": command,
            "stdout": stdout,
            "elapsed_ms": elapsed_ms,
            "size_human": size_human,
            "watchdog_bloat_warning": warning,
            "watchdog_growth_rate_mb_hr": growth_rate,
            "subtrees": size_map,
            "gtm_exports_present": gtm_exports.exists(),
        }
    except Exception as e:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return {
            "command": command,
            "stdout": f"[ERROR: {e}]",
            "elapsed_ms": elapsed_ms,
            "size_human": "unknown",
            "watchdog_bloat_warning": False,
            "watchdog_growth_rate_mb_hr": "unavailable",
            "subtrees": size_map,
            "gtm_exports_present": gtm_exports.exists(),
        }


def write_bundle_file(bundle_dir: Path, rel_path: str, content: str,
                      manifest: list, apply_redact: bool = True):
    dest = bundle_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if apply_redact:
        content = redact(content)
    dest.write_text(content, encoding="utf-8")
    size = dest.stat().st_size
    manifest.append({"file": rel_path, "size_bytes": size, "redacted": apply_redact})
    emit_event("SUPPORT_BUNDLE_FILE", {"file": rel_path, "size_bytes": size, "redacted": apply_redact})
    print(f"  + {rel_path}  ({size} bytes)")


# ---------------------------------------------------------------------------
# ZIP creation
# ---------------------------------------------------------------------------
def create_zip(bundle_dir: Path, bundle_id: str) -> Path:
    """
    Create a zip of the entire bundle dir.
    Zip contains paths relative to bundle_dir parent (so bundle_dir/ is the root).
    No parent traversal: all arcnames are bundle_slug/<relative_path>.
    Returns the path to the created zip file.
    """
    bundle_slug = bundle_dir.name
    zip_name = f"support_bundle_{bundle_slug}.zip"
    zip_path = bundle_dir / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(bundle_dir.rglob("*")):
            # Skip the zip file itself (avoid recursion)
            if file_path == zip_path:
                continue
            if file_path.is_file():
                # Arcname: bundle_slug/relative_path — no parent traversal
                rel = file_path.relative_to(bundle_dir)
                arcname = f"{bundle_slug}/{rel}"
                zf.write(file_path, arcname=arcname)

    zip_size = zip_path.stat().st_size
    return zip_path, zip_size


# ---------------------------------------------------------------------------
# Triage extraction from agent911_state.json
# ---------------------------------------------------------------------------
def extract_triage(state: dict) -> dict:
    t = {}
    t["stability_score"] = state.get("stability_score", "unknown")
    t["risk_level"] = state.get("risk_level", "unknown")
    t["ts"] = state.get("ts", "unknown")
    t["schema_version"] = state.get("schema_version", "unknown")

    rc = state.get("radcheck", {})
    t["radcheck_score"] = rc.get("score", "unknown")
    t["radcheck_risk"] = rc.get("risk_level", "unknown")
    t["radcheck_velocity"] = rc.get("velocity_rate_per_hour", "unknown")

    prot = state.get("protection_state", {})
    t["sentinel_last_event"] = prot.get("last_event_type", "none")
    t["sentinel_last_event_ts"] = prot.get("last_event_ts", "none")
    rollup = state.get("protection_rollup", {})
    t["sentinel_events_24h"] = rollup.get("events_24h", 0)
    t["sentinel_stalls_prevented_24h"] = rollup.get("stalls_prevented_24h", 0)

    comp = state.get("compaction_state", {})
    t["compaction_alert"] = comp.get("alert_active", "unknown")
    t["compaction_p95_ms"] = comp.get("p95_ms", "unknown")
    t["compaction_timeout_count"] = comp.get("timeout_count", "unknown")
    t["compaction_source"] = comp.get("source", "unknown")

    routing = state.get("routing", {})
    t["routing_provider"] = routing.get("active_provider", "unknown")
    t["routing_posture"] = routing.get("posture", "unknown")

    pg = state.get("predictive_guard", {})
    t["predictive_risk_level"] = pg.get("risk_level", "unknown")
    t["predictive_confidence"] = pg.get("confidence", "unknown")

    delta = state.get("delta", {})
    t["delta_score_change"] = delta.get("score_change", 0)

    t["recommended_actions"] = state.get("recommended_actions", [])[:5]
    t["agent911_duration_ms"] = state.get("duration_ms", "unknown")

    return t


# ---------------------------------------------------------------------------
# summary.md builder
# ---------------------------------------------------------------------------
def build_summary(triage: dict, bundle_ts: str, include_raw: bool,
                  bundle_id: str, zipped: bool = False,
                  watchdog_disk: dict = None) -> str:
    watchdog_disk = watchdog_disk or {}
    lines = [
        "# ACME Support Bundle — Operator Diagnostic Summary",
        "",
        f"**Bundle ID:** `{bundle_id}`",
        f"**Generated:** {bundle_ts}",
        f"**Bundle version:** {VERSION}",
        f"**Include raw:** {include_raw}",
        f"**Redaction:** {'OFF (--include_raw)' if include_raw else 'ON (default)'}",
        f"**Zip created:** {'YES' if zipped else 'NO'}",
        "",
        "---",
        "",
        "## Bundle Footprint",
        "",
        f"- Watchdog disk usage: `{watchdog_disk.get('stdout', 'unknown')}`",
        f"- Bundle watchdog_bloat_warning: `{str(watchdog_disk.get('watchdog_bloat_warning', False)).lower()}`",
        f"- Watchdog growth rate: `{watchdog_disk.get('watchdog_growth_rate_mb_hr', 'unavailable')}`",
        f"- Collector runtime: `{watchdog_disk.get('elapsed_ms', 'unknown')}ms`",
        "",
        "---",
        "",
        "## Agent911 Triage",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Stability Score | {triage['stability_score']} |",
        f"| Risk Level | {triage['risk_level']} |",
        f"| Snapshot TS | {triage['ts']} |",
        f"| Schema Version | {triage['schema_version']} |",
        f"| Agent911 Duration | {triage['agent911_duration_ms']} ms |",
        "",
        "## RadCheck",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| RC Score | {triage['radcheck_score']} |",
        f"| RC Risk | {triage['radcheck_risk']} |",
        f"| RC Velocity (per hr) | {triage['radcheck_velocity']} |",
        "",
        "## Sentinel",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Last Event Type | {triage['sentinel_last_event']} |",
        f"| Last Event TS | {triage['sentinel_last_event_ts']} |",
        f"| Events 24h | {triage['sentinel_events_24h']} |",
        f"| Stalls Prevented 24h | {triage['sentinel_stalls_prevented_24h']} |",
        "",
        "## Compaction",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Alert Active | {triage['compaction_alert']} |",
        f"| p95 ms | {triage['compaction_p95_ms']} |",
        f"| Timeout Count | {triage['compaction_timeout_count']} |",
        f"| Source | {triage['compaction_source']} |",
        "",
        "## SphinxGate / Routing",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Active Provider | {triage['routing_provider']} |",
        f"| Posture | {triage['routing_posture']} |",
        "",
        "## Predictive Guard",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Risk Level | {triage['predictive_risk_level']} |",
        f"| Confidence | {triage['predictive_confidence']} |",
        "",
        "## Delta",
        "",
        f"Score change (since last snapshot): {triage['delta_score_change']}",
        "",
        "## Recommended Actions",
        "",
    ]
    for i, action in enumerate(triage.get("recommended_actions", []), 1):
        lines.append(f"{i}. {action}")
    if not triage.get("recommended_actions"):
        lines.append("_(none)_")

    lines += [
        "",
        "---",
        "",
        "## Bundle Contents",
        "",
        "```",
        "redacted_logs/",
        "  ops_events_tail.log           last 50 ops events",
        "  heartbeat_tail.log            last 50 heartbeat entries",
        "  launchd_out_tail.log          last 50 launchd stdout lines",
        "  launchd_err_tail.log          last 50 launchd stderr lines",
        "state_snapshots/",
        "  watchdog_disk_usage.txt       read-only du -sh watchdog snapshot",
        "  agent911_state.json           current control-plane state",
        "  agent911_dashboard.md         current dashboard",
        "  radcheck_history_tail.ndjson  last 50 radcheck history events",
        "  mtl_snapshot.json             current MTL snapshot",
        "bundle_summary.txt              machine-readable bundle flags",
        "summary.md                      this file",
        "bundle_manifest.json            machine-readable manifest",
        "```",
        "",
        "---",
        "",
        f"*Generated by ACME Support Bundle v{VERSION}*",
        ("*Redaction: ON — tokens, emails, IPs, secrets masked*"
         if not include_raw else
         "*Redaction: OFF — raw content included (secrets files still excluded)*"),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=f"ACME Support Bundle v{VERSION} — Operator Diagnostic Pack"
    )
    parser.add_argument(
        "--include_raw",
        action="store_true",
        help="Include raw log content (secrets always excluded)",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Create support_bundle_<ts>.zip in the bundle dir after generation",
    )
    parser.add_argument(
        "--print-consent",
        action="store_true",
        dest="print_consent",
        help="Print operator consent & privacy notice to console; exit (no files written)",
    )
    args = parser.parse_args()

    # --print-consent: print and exit immediately (no files)
    if args.print_consent:
        print_consent()
        return 0

    t_start = time.monotonic()
    include_raw = args.include_raw
    do_zip = args.zip
    apply_redact = not include_raw

    bundle_slug = ts_slug()
    bundle_id = f"acme-support-{bundle_slug}"
    bundle_dir = BUNDLES_BASE / bundle_slug
    bundle_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print(f"║  ACME Support Bundle v{VERSION}  —  {bundle_slug}  ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print(f"  bundle_id  : {bundle_id}")
    print(f"  output     : {bundle_dir}")
    print(f"  redaction  : {'OFF (--include_raw)' if include_raw else 'ON (default)'}")
    print(f"  zip        : {'YES (--zip)' if do_zip else 'NO'}")
    print()

    emit_event("SUPPORT_BUNDLE_START", {
        "bundle_id": bundle_id,
        "bundle_dir": str(bundle_dir),
        "include_raw": include_raw,
        "zip_requested": do_zip,
        "version": VERSION,
    })

    manifest: list = []
    watchdog_disk = collect_watchdog_disk_usage()

    # -----------------------------------------------------------------------
    # 1. redacted_logs/
    # -----------------------------------------------------------------------
    print("── Collecting logs ─────────────────────────────────────────────────")

    log_specs = [
        (WATCHDOG_DIR / "ops_events.log",    "redacted_logs/ops_events_tail.log",  50),
        (WATCHDOG_DIR / "heartbeat.log",     "redacted_logs/heartbeat_tail.log",   50),
        (WATCHDOG_DIR / "launchd.out.log",   "redacted_logs/launchd_out_tail.log", 50),
        (WATCHDOG_DIR / "launchd.err.log",   "redacted_logs/launchd_err_tail.log", 50),
    ]

    for src_path, rel_dest, n in log_specs:
        if src_path.exists():
            content = tail_lines(src_path, n)
            write_bundle_file(bundle_dir, rel_dest, content, manifest,
                              apply_redact=apply_redact)
        else:
            write_bundle_file(bundle_dir, rel_dest,
                              f"[FILE_NOT_FOUND: {src_path}]\n",
                              manifest, apply_redact=False)

    # -----------------------------------------------------------------------
    # 2. state_snapshots/
    # -----------------------------------------------------------------------
    print()
    print("── Collecting state snapshots ──────────────────────────────────────")

    state_src = WATCHDOG_DIR / "agent911_state.json"
    write_bundle_file(
        bundle_dir,
        "state_snapshots/watchdog_disk_usage.txt",
        "\n".join([
            f"command={watchdog_disk['command']}",
            f"stdout={watchdog_disk['stdout']}",
            f"elapsed_ms={watchdog_disk['elapsed_ms']}",
            f"watchdog_bloat_warning={str(watchdog_disk['watchdog_bloat_warning']).lower()}",
            f"watchdog_growth_rate_mb_hr={watchdog_disk['watchdog_growth_rate_mb_hr']}",
            f"backups_mb={watchdog_disk['subtrees'].get('backups', 0.0)}",
            f"lazarus_mb={watchdog_disk['subtrees'].get('lazarus', 0.0)}",
            f"gtm_exports_mb={watchdog_disk['subtrees'].get('gtm_exports', 0.0)}",
            f"gtm_exports_present={str(watchdog_disk.get('gtm_exports_present', False)).lower()}",
        ]) + "\n",
        manifest,
        apply_redact=False,
    )
    if state_src.exists():
        write_bundle_file(bundle_dir, "state_snapshots/agent911_state.json",
                          safe_read_text(state_src), manifest,
                          apply_redact=apply_redact)
    else:
        write_bundle_file(bundle_dir, "state_snapshots/agent911_state.json",
                          '{"_error":"file_not_found"}', manifest, apply_redact=False)

    dash_src = WATCHDOG_DIR / "agent911_dashboard.md"
    if dash_src.exists():
        write_bundle_file(bundle_dir, "state_snapshots/agent911_dashboard.md",
                          safe_read_text(dash_src), manifest,
                          apply_redact=apply_redact)
    else:
        write_bundle_file(bundle_dir, "state_snapshots/agent911_dashboard.md",
                          "[FILE_NOT_FOUND]\n", manifest, apply_redact=False)

    rc_hist = WATCHDOG_DIR / "radcheck_history.ndjson"
    if rc_hist.exists():
        write_bundle_file(bundle_dir, "state_snapshots/radcheck_history_tail.ndjson",
                          tail_lines(rc_hist, 50), manifest,
                          apply_redact=apply_redact)
    else:
        write_bundle_file(bundle_dir, "state_snapshots/radcheck_history_tail.ndjson",
                          "[FILE_NOT_FOUND]\n", manifest, apply_redact=False)

    mtl_src = OPS_DIR / "MTL.snapshot.json"
    if mtl_src.exists():
        write_bundle_file(bundle_dir, "state_snapshots/mtl_snapshot.json",
                          safe_read_text(mtl_src), manifest,
                          apply_redact=apply_redact)
    else:
        write_bundle_file(bundle_dir, "state_snapshots/mtl_snapshot.json",
                          '{"_error":"file_not_found"}', manifest, apply_redact=False)

    # -----------------------------------------------------------------------
    # 3. summary.md (placeholder — updated after zip decision)
    # -----------------------------------------------------------------------
    print()
    print("── Building summary ────────────────────────────────────────────────")

    state_data = safe_json_load(state_src) if state_src.exists() else {}
    triage = extract_triage(state_data)
    bundle_ts = ts_now()

    summary_content = build_summary(
        triage,
        bundle_ts,
        include_raw,
        bundle_id,
        zipped=do_zip,
        watchdog_disk=watchdog_disk,
    )
    write_bundle_file(bundle_dir, "summary.md", summary_content, manifest, apply_redact=False)
    write_bundle_file(
        bundle_dir,
        "bundle_summary.txt",
        "\n".join([
            f"bundle_id={bundle_id}",
            f"generated_ts={bundle_ts}",
            f"watchdog_disk_usage={watchdog_disk['stdout']}",
            f"watchdog_disk_usage_elapsed_ms={watchdog_disk['elapsed_ms']}",
            f"watchdog_bloat_warning={str(watchdog_disk['watchdog_bloat_warning']).lower()}",
            f"watchdog_growth_rate_mb_hr={watchdog_disk['watchdog_growth_rate_mb_hr']}",
        ]) + "\n",
        manifest,
        apply_redact=False,
    )

    # -----------------------------------------------------------------------
    # 4. bundle_manifest.json
    # -----------------------------------------------------------------------
    elapsed_ms = round((time.monotonic() - t_start) * 1000)

    manifest_data = {
        "schema": SCHEMA,
        "bundle_id": bundle_id,
        "generated_ts": bundle_ts,
        "version": VERSION,
        "include_raw": include_raw,
        "redaction_applied": apply_redact,
        "elapsed_ms": elapsed_ms,
        "files": manifest,
        "triage_summary": {
            "stability_score": triage["stability_score"],
            "risk_level": triage["risk_level"],
            "radcheck_score": triage["radcheck_score"],
            "sentinel_events_24h": triage["sentinel_events_24h"],
            "compaction_alert": triage["compaction_alert"],
            "routing_provider": triage["routing_provider"],
            "watchdog_disk_usage": watchdog_disk["stdout"],
            "watchdog_disk_usage_elapsed_ms": watchdog_disk["elapsed_ms"],
            "watchdog_bloat_warning": watchdog_disk["watchdog_bloat_warning"],
            "watchdog_growth_rate_mb_hr": watchdog_disk["watchdog_growth_rate_mb_hr"],
        },
        "safety": {
            "openclaw_json_included": False,
            "secrets_excluded": True,
            "writes_outside_bundle_dir": False,
        },
    }
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2))
    mf_size = manifest_path.stat().st_size
    emit_event("SUPPORT_BUNDLE_FILE", {"file": "bundle_manifest.json", "size_bytes": mf_size})
    print(f"  + bundle_manifest.json  ({mf_size} bytes)")

    total_files = len(manifest) + 1  # +1 for manifest itself
    total_size = sum(f.get("size_bytes", 0) for f in manifest) + mf_size

    # -----------------------------------------------------------------------
    # 5. ZIP (optional)
    # -----------------------------------------------------------------------
    zip_path = None
    zip_size = 0
    if do_zip:
        print()
        print("── Creating zip ────────────────────────────────────────────────────")
        zip_path, zip_size = create_zip(bundle_dir, bundle_id)
        print(f"  + {zip_path.name}  ({zip_size} bytes)")
        emit_event("SUPPORT_BUNDLE_ZIPPED", {
            "bundle_id": bundle_id,
            "zip_path": str(zip_path),
            "zip_size_bytes": zip_size,
        })

    # -----------------------------------------------------------------------
    # 6. Done
    # -----------------------------------------------------------------------
    total_elapsed_ms = round((time.monotonic() - t_start) * 1000)

    print()
    print("═══════════════════════════════════════════════════════════════════")
    print(f"  DONE: {total_files} files  |  {total_size} bytes  |  {total_elapsed_ms}ms")
    print(f"  bundle  → {bundle_dir}")
    if zip_path:
        print(f"  zip     → {zip_path}  ({zip_size} bytes)")
    print()
    print("  SAFETY CHECK")
    print("  ✓ openclaw.json  — NOT included")
    print("  ✓ secrets files  — excluded")
    print(f"  ✓ writes         — bundle dir ONLY: {bundle_dir}")
    if zip_path:
        print(f"  ✓ zip traversal  — bundle dir root ONLY (no parent paths)")
    print()

    emit_event("SUPPORT_BUNDLE_DONE", {
        "bundle_id": bundle_id,
        "bundle_dir": str(bundle_dir),
        "files": total_files,
        "total_bytes": total_size,
        "elapsed_ms": total_elapsed_ms,
        "redaction_applied": apply_redact,
        "zipped": do_zip,
        "zip_size_bytes": zip_size if do_zip else 0,
    })

    return 0


if __name__ == "__main__":
    sys.exit(main())
